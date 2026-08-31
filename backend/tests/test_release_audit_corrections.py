from __future__ import annotations

import io
import lzma
import tarfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config import AppSettings
from core.config_validator import validate_and_sanitise
from services.aria2_error_recovery import Aria2ErrorRecovery
from services.extractor_secure import _extract_secure_sync


class _Cursor:
    def __init__(self, rowcount: int = 1):
        self.rowcount = rowcount


class _FakeDb:
    def __init__(self, rows, trace):
        self.rows = list(rows)
        self.trace = trace

    async def fetchall(self, query, params=()):
        if "FROM download_files f" in query and "f.status='error'" in query:
            return list(self.rows)
        return []

    async def execute(self, query, params=()):
        normalized = " ".join(query.split())
        if "SET retry_count=?" in normalized:
            self.trace.append(("claim", tuple(params)))
        elif "SET download_id=?, status='queued'" in normalized:
            self.trace.append(("install", tuple(params)))
        elif "INSERT INTO events" in normalized:
            self.trace.append(("event", tuple(params)))
        return _Cursor(1)

    async def commit(self):
        return None


class _FakeAria2:
    def __init__(self, trace, *, fail=False):
        self.trace = trace
        self.fail = fail

    async def ensure_download(
        self,
        url,
        options,
        start_paused=False,
        max_retries=5,
        cached_downloads=None,
    ):
        self.trace.append(
            (
                "ensure",
                url,
                dict(options),
                max_retries,
                [str(getattr(item, "gid", "")) for item in (cached_downloads or [])],
            )
        )
        if self.fail:
            raise RuntimeError("synthetic aria2 restart failure")
        return "new-gid"


class _FakeEngine:
    def __init__(self, trace, *, fail=False, snapshot=None):
        self.trace = trace
        self.client = _FakeAria2(trace, fail=fail)
        self.snapshot = list(snapshot or [])

    def download_client_name(self):
        return "aria2"

    async def _engine_aria2_get_all(self):
        return list(self.snapshot)

    def _aria2_slot_limit(self):
        return 3

    def _remote_aria2_path(self, path: Path):
        return str(path)

    def _aria2_job_options(self, options):
        return dict(options)

    def aria2(self):
        return self.client

    async def _remove_owned_aria2_gid(self, gid):
        self.trace.append(("remove", gid))
        return True


class _FakeOwnership:
    def __init__(self, trace, owned=None):
        self.trace = trace
        self.owned = set(owned or [])

    async def owned_gids(self):
        return set(self.owned)

    async def record(self, gid, *, download_file_id=None, transfer_id=None):
        self.trace.append(("record", gid, download_file_id, transfer_id))


def _error_row(*, retry_count=0, age=120):
    return {
        "file_id": 11,
        "torrent_id": 7,
        "local_path": "/download/example.bin",
        "download_id": "old-gid",
        "download_url": "https://provider.example/file",
        "filename": "example.bin",
        "block_reason": "3: temporary server error",
        "retry_count": retry_count,
        "retry_age_seconds": age,
        "torrent_name": "Example",
    }


def _settings(*, count=3, delay=60):
    return SimpleNamespace(
        paused=False,
        aria2_error_retry_count=count,
        aria2_error_retry_delay_seconds=delay,
    )


@pytest.mark.asyncio
async def test_aria2_retry_claims_budget_removes_failed_gid_then_restarts_once(monkeypatch):
    import services.aria2_error_recovery as module

    trace = []
    db = _FakeDb([_error_row()], trace)

    @asynccontextmanager
    async def fake_get_db():
        yield db

    monkeypatch.setattr(module, "get_db", fake_get_db)
    monkeypatch.setattr(module, "get_settings", lambda: _settings())
    monkeypatch.setattr(module, "is_builtin_mode", lambda _cfg=None: True)

    failed_state = SimpleNamespace(gid="old-gid", status="error")
    recovery = Aria2ErrorRecovery(
        _FakeEngine(trace, snapshot=[failed_state]),
        _FakeOwnership(trace),
    )
    result = await recovery.run()

    assert result["retried"] == 1
    claim_index = next(i for i, item in enumerate(trace) if item[0] == "claim")
    remove_index = next(i for i, item in enumerate(trace) if item[0] == "remove")
    ensure_index = next(i for i, item in enumerate(trace) if item[0] == "ensure")
    assert claim_index < remove_index < ensure_index
    ensure = trace[ensure_index]
    assert ensure[3] == 1
    assert "old-gid" not in ensure[4]
    assert any(item[0] == "install" for item in trace)
    assert any(item[0] == "record" for item in trace)


@pytest.mark.asyncio
async def test_live_aria2_gid_is_not_duplicated_by_error_recovery(monkeypatch):
    import services.aria2_error_recovery as module

    trace = []
    db = _FakeDb([_error_row()], trace)

    @asynccontextmanager
    async def fake_get_db():
        yield db

    monkeypatch.setattr(module, "get_db", fake_get_db)
    monkeypatch.setattr(module, "get_settings", lambda: _settings(delay=0))
    monkeypatch.setattr(module, "is_builtin_mode", lambda _cfg=None: True)

    live_state = SimpleNamespace(gid="old-gid", status="active")
    result = await Aria2ErrorRecovery(
        _FakeEngine(trace, snapshot=[live_state]),
        _FakeOwnership(trace),
    ).run()

    assert result["retried"] == 0
    assert not any(item[0] in {"claim", "remove", "ensure"} for item in trace)


@pytest.mark.asyncio
async def test_aria2_retry_delay_defers_without_consuming_budget(monkeypatch):
    import services.aria2_error_recovery as module

    trace = []
    db = _FakeDb([_error_row(age=59)], trace)

    @asynccontextmanager
    async def fake_get_db():
        yield db

    monkeypatch.setattr(module, "get_db", fake_get_db)
    monkeypatch.setattr(module, "get_settings", lambda: _settings(delay=60))
    monkeypatch.setattr(module, "is_builtin_mode", lambda _cfg=None: True)

    result = await Aria2ErrorRecovery(
        _FakeEngine(trace), _FakeOwnership(trace)
    ).run()

    assert result["deferred"] == 1
    assert not any(item[0] in {"claim", "remove", "ensure"} for item in trace)


@pytest.mark.asyncio
async def test_aria2_retry_ceiling_prevents_another_attempt(monkeypatch):
    import services.aria2_error_recovery as module

    trace = []
    db = _FakeDb([_error_row(retry_count=3)], trace)

    @asynccontextmanager
    async def fake_get_db():
        yield db

    monkeypatch.setattr(module, "get_db", fake_get_db)
    monkeypatch.setattr(module, "get_settings", lambda: _settings(count=3, delay=0))
    monkeypatch.setattr(module, "is_builtin_mode", lambda _cfg=None: True)

    result = await Aria2ErrorRecovery(
        _FakeEngine(trace), _FakeOwnership(trace)
    ).run()

    assert result["retried"] == 0
    assert not any(item[0] in {"claim", "remove", "ensure"} for item in trace)


@pytest.mark.asyncio
async def test_failed_aria2_restart_still_consumes_claimed_attempt(monkeypatch):
    import services.aria2_error_recovery as module

    trace = []
    db = _FakeDb([_error_row()], trace)

    @asynccontextmanager
    async def fake_get_db():
        yield db

    monkeypatch.setattr(module, "get_db", fake_get_db)
    monkeypatch.setattr(module, "get_settings", lambda: _settings(delay=0))
    monkeypatch.setattr(module, "is_builtin_mode", lambda _cfg=None: True)

    result = await Aria2ErrorRecovery(
        _FakeEngine(trace, fail=True), _FakeOwnership(trace)
    ).run()

    assert result["failed"] == 1
    assert any(item[0] == "claim" for item in trace)
    assert any(item[0] == "remove" for item in trace)
    ensure = next(item for item in trace if item[0] == "ensure")
    assert ensure[3] == 1
    assert not any(item[0] == "install" for item in trace)


def _tar_payload() -> bytes:
    payload = b"release-safe composite extraction\n"
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        info = tarfile.TarInfo("nested/payload.txt")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return stream.getvalue()


def test_tar_lzma_uses_exact_codec_then_validated_tar(tmp_path):
    source = tmp_path / "payload.tar.lzma"
    source.write_bytes(lzma.compress(_tar_payload(), format=lzma.FORMAT_ALONE))
    dest = tmp_path / "out"

    created = _extract_secure_sync(source, dest)

    extracted = dest / "nested" / "payload.txt"
    assert extracted.read_text() == "release-safe composite extraction\n"
    assert extracted in created
    assert not (dest / ".debridpulse-composite.tar").exists()


def test_retry_delay_zero_is_a_valid_immediate_retry_configuration():
    cfg = validate_and_sanitise(AppSettings(aria2_error_retry_delay_seconds=0))
    assert cfg.aria2_error_retry_delay_seconds == 0


def test_release_corrections_use_canonical_owners():
    root = Path(__file__).resolve().parents[2]
    transfer_service = (root / "backend/services/transfer_service.py").read_text()
    reconciliation = (root / "backend/services/reconciliation_service.py").read_text()
    gateway = (root / "backend/services/aria2_gateway.py").read_text()
    recovery = (root / "backend/services/aria2_error_recovery.py").read_text()
    extraction = (root / "backend/services/extraction_service.py").read_text()
    secure_extractor = (root / "backend/services/extractor_secure.py").read_text()
    scheduler = (root / "backend/core/scheduler.py").read_text()
    routes = (root / "backend/api/routes.py").read_text()
    dockerfile = (root / "Dockerfile").read_text()
    licenses = (root / "docs/DEPENDENCY_LICENSES.md").read_text()

    assert "Aria2ErrorRecovery" in transfer_service
    assert "await self.recovery.run()" in reconciliation
    assert "return await self.recovery.run()" in gateway
    assert "max_retries=1" in recovery
    assert "await self.engine._remove_owned_aria2_gid(old_gid)" in recovery
    assert "get_secure_extractor" in extraction
    assert '"-t*:r"' not in secure_extractor
    assert "_decompress_zstd" in secure_extractor
    assert "_decompress_lzma" in secure_extractor
    assert "    zstd " in dockerfile
    assert "| zstd | BSD-3-Clause |" in licenses
    assert "is_version_newer" in scheduler
    assert "is_version_newer" in routes
