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
from postprocessors.archive.secure import _extract_secure_sync


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


