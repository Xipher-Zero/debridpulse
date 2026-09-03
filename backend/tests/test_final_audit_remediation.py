"""Permanent regressions for the final v1.0.12 adversarial-audit remediation."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import sqlite3

import pytest

import core.config as config
import db.database as database
import providers.alldebrid.host_runtime as host_runtime
from integrations.runtime_state import RuntimeStateConflict, RuntimeStateRecord
from providers.alldebrid.host_runtime import (
    AllDebridHostMaintenance,
    AllDebridHostSnapshotError,
    AllDebridRequestApplicability,
    decode_host_snapshot,
    parse_native_host_snapshot,
)
from providers.alldebrid.provider import AllDebridProvider
from providers.general_http.provider import GeneralHttpProvider
from services.notifications import NotificationService
from transfers.applicability import (
    ApplicabilityClass,
    ProviderApplicabilityInput,
    classify_provider_applicability,
)
from transfers.models import TransferRequest


PREDECESSOR = Path(__file__).with_name("fixtures") / "v1.0.11.1.sql"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _main(monkeypatch):
    """Import the production composition with its checked-in frontend available."""
    monkeypatch.setenv("STATIC_DIR", str(REPO_ROOT / "frontend" / "static"))
    import main as app_main
    return app_main


def _request(url: str) -> TransferRequest:
    return TransferRequest(url.split(":", 1)[0], url)


def _native_snapshot(domain: str, regexp: str) -> dict:
    return {
        "hosts": {
            "svc": {
                "name": "service",
                "type": "premium",
                "domains": [domain],
                "regexps": [regexp],
                "status": True,
            }
        }
    }


@pytest.mark.asyncio
async def test_startup_sanitization_is_authoritative_for_external_aria2_migration(
    tmp_path, monkeypatch
):
    """A malformed legacy mode must not mint authority over a foreign aria2 GID."""
    main = _main(monkeypatch)
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(PREDECESSOR.read_text())
        conn.execute(
            "INSERT INTO torrents(id,hash,name,status,source) VALUES(901,?,?,?,?)",
            ("9" * 40, "foreign execution", "downloading", "manual"),
        )
        conn.execute(
            """INSERT INTO download_files(
                id,torrent_id,filename,size_bytes,source_url,download_url,local_path,
                status,download_id,download_client,blocked
            ) VALUES(902,901,'foreign.bin',10,?,?,?,'downloading','foreign-gid','aria2',0)""",
            (
                "https://files.example.test/source",
                "https://cdn.example.test/unlocked",
                "/downloads/foreign.bin",
            ),
        )
        conn.commit()

    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    malformed = config.AppSettings(aria2_mode="legacy-garbage", paused=False)
    monkeypatch.setattr(config, "_settings", malformed)

    effective = await main._prepare_startup_settings_and_migrate()

    assert effective.aria2_mode == "external"
    assert config.get_settings().aria2_mode == "external"
    persisted = json.loads((tmp_path / "config.json").read_text())
    assert persisted["aria2_mode"] == "external"

    with sqlite3.connect(db_path) as conn:
        attempt = conn.execute(
            "SELECT authorized,error FROM execution_attempts WHERE id IS NOT NULL"
        ).fetchone()
        file_row = conn.execute(
            "SELECT status,normalized_error FROM download_files WHERE id=902"
        ).fetchone()
        ownership = conn.execute(
            "SELECT 1 FROM debridpulse_aria2_owned_gids WHERE gid='foreign-gid'"
        ).fetchone()

    assert attempt is not None
    assert attempt[0] == 0
    assert "ownership_conflict" in str(attempt[1]).casefold()
    assert file_row is not None and file_row[0] == "error"
    assert "ownership_conflict" in str(file_row[1]).casefold()
    assert ownership is None


@pytest.mark.asyncio
async def test_startup_refuses_ownership_migration_without_validated_settings(monkeypatch):
    main = _main(monkeypatch)
    called = False

    def explode(_settings):
        raise ValueError("invalid persisted configuration")

    async def migration_spy(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("core.config_validator.validate_and_sanitise", explode)
    monkeypatch.setattr("db.migrations.v112.migrate", migration_spy)

    with pytest.raises(RuntimeError, match="before ownership-sensitive migration"):
        await main._prepare_startup_settings_and_migrate()
    assert called is False


def test_provider_regexps_use_bounded_engine_and_reject_backtracking_only_features():
    catastrophic = r"^https?://files\.example\.test/(a+)+$"
    # The pattern is intentionally syntactically valid to Python's backtracking
    # engine; the production boundary must not depend on Python re execution.
    re.compile(catastrophic)
    snapshot = parse_native_host_snapshot(
        _native_snapshot("files.example.test", catastrophic)
    )
    applicability = AllDebridRequestApplicability(snapshot)
    compiled = applicability._compiled[0][1][0]
    assert compiled.__class__.__module__.startswith("re2")

    hostile = "https://files.example.test/" + ("a" * 8000) + "!"
    assert applicability(_request(hostile)).specialized_hosts == ()

    python_valid_re2_unsafe = r"^https?://files\.example\.test/(a+)\1$"
    re.compile(python_valid_re2_unsafe)
    with pytest.raises(AllDebridHostSnapshotError):
        parse_native_host_snapshot(
            _native_snapshot("files.example.test", python_valid_re2_unsafe)
        )


def test_provider_snapshot_has_aggregate_resource_budgets(monkeypatch):
    monkeypatch.setattr(host_runtime, "_MAX_TOTAL_PATTERNS", 1)
    oversized = {
        "hosts": {
            "one": {
                "name": "one",
                "type": "premium",
                "domains": ["one.example.test"],
                "regexps": [r"^https?://one\.example\.test/.+$"],
                "status": True,
            },
            "two": {
                "name": "two",
                "type": "premium",
                "domains": ["two.example.test"],
                "regexps": [r"^https?://two\.example\.test/.+$"],
                "status": True,
            },
        }
    }
    with pytest.raises(AllDebridHostSnapshotError, match="too many regexps"):
        parse_native_host_snapshot(oversized)

    monkeypatch.setattr(host_runtime, "_MAX_TOTAL_PATTERNS", 8192)
    monkeypatch.setattr(host_runtime, "_MAX_SNAPSHOT_BYTES", 64)
    with pytest.raises(AllDebridHostSnapshotError, match="snapshot size limit"):
        parse_native_host_snapshot(
            _native_snapshot("files.example.test", r"^https?://files\.example\.test/.+$")
        )


class _MemoryStateStore:
    def __init__(self):
        self.record: RuntimeStateRecord | None = None

    async def load(self, integration_id, state_key):
        return self.record

    async def replace(
        self,
        integration_id,
        payload,
        *,
        schema_version,
        state_key,
        observed_at,
        successful_at,
        stale_after,
        expected_generation,
    ):
        current = 0 if self.record is None else self.record.generation
        if expected_generation != current:
            raise RuntimeStateConflict("stale generation")
        generation = current + 1
        created = observed_at if self.record is None else self.record.created_at
        self.record = RuntimeStateRecord(
            integration_id,
            state_key,
            schema_version,
            bytes(payload),
            observed_at,
            stale_after,
            successful_at,
            created,
            observed_at,
            generation,
        )
        return self.record


class _HostClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def get_user_hosts(self):
        self.calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


@pytest.mark.asyncio
async def test_rejected_hostile_refresh_preserves_lkg_across_restart_and_generic_routing():
    good = _native_snapshot(
        "files.example.test", r"^https?://files\.example\.test/allowed/.+$"
    )
    hostile = _native_snapshot(
        "files.example.test", r"^https?://files\.example\.test/(a+)\1$"
    )
    clock = [100.0]
    store = _MemoryStateStore()
    client = _HostClient(good)
    provider = AllDebridProvider(client=client)
    maintenance = AllDebridHostMaintenance(
        store, clock=lambda: clock[0], refresh_seconds=10, retry_seconds=1
    )
    maintenance.bind(provider, initial=True)
    await maintenance.maintain()

    assert store.record is not None
    first_payload = store.record.payload
    first_generation = store.record.generation
    assert decode_host_snapshot(first_payload).hosts[0].domains == ("files.example.test",)

    clock[0] = 111.0
    client.payload = hostile
    await maintenance.maintain()
    assert store.record.payload == first_payload
    assert store.record.generation == first_generation

    allowed = _request("https://files.example.test/allowed/file.bin")
    assert provider.applicability_for(allowed).specialized_hosts

    # Restart restoration re-validates the durable payload and therefore cannot
    # resurrect the rejected hostile replacement.
    restored_provider = AllDebridProvider(client=_HostClient(RuntimeError("offline")))
    restored = AllDebridHostMaintenance(store, clock=lambda: clock[0])
    restored.bind(restored_provider, initial=True)
    await restored.start()
    assert restored_provider.applicability_for(allowed).specialized_hosts

    generic = GeneralHttpProvider()
    unrelated = _request("https://downloads.example.test/file.bin")
    matches = classify_provider_applicability(
        unrelated,
        (
            ProviderApplicabilityInput(
                restored_provider.descriptor.id,
                restored_provider.descriptor.request_types,
                restored_provider.descriptor.enabled,
                restored_provider.applicability,
            ),
            ProviderApplicabilityInput(
                generic.descriptor.id,
                generic.descriptor.request_types,
                generic.descriptor.enabled,
                generic.applicability,
            ),
        ),
    )
    assert [(m.provider_id, m.classification) for m in matches] == [
        ("general_http", ApplicabilityClass.GENERIC)
    ]

    # RE2 evaluation is synchronous but bounded; yielding after a maximum-size
    # rejected-path match proves no stuck Python backtracking operation remains.
    pathological = _request(
        "https://files.example.test/" + ("a" * 8000) + "!"
    )
    restored_provider.applicability_for(pathological)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_generic_transfer_added_notification_is_provider_neutral(monkeypatch):
    captured = {}

    async def capture(self, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(NotificationService, "_send", capture)
    service = NotificationService("https://hooks.example.test/main")
    await service.send_added(
        "ordinary-file.bin",
        source="direct_link",
        transfer_id="transfer-1",
    )
    assert captured["title"] == "📥 Transfer Added"
    assert "torrent" not in captured["title"].casefold()


def test_application_metadata_owns_the_multi_provider_runtime(monkeypatch):
    main = _main(monkeypatch)
    assert main.logger.name == "debridpulse.main"
    description = main.app.description
    assert "Universal Transfer Core" in description
    assert "AllDebrid" in description
    assert "General HTTP(S)" in description
