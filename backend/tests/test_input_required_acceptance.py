"""Roadmap Item 3 persistence, restart, backup, and secrecy acceptance proofs."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import db.database as database
import services.db_maintenance as db_maintenance
from fake_integrations import MemoryExecutor
from transfers.engine import TransferEngine
from transfers.input_required import auth_required, username_password
from transfers.models import (
    Capability,
    InputField,
    InputMethod,
    IntegrationDescriptor,
    ResourceState,
    ResolutionResult,
    TransferRequest,
    TransferState,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


class AcceptanceAuthProvider:
    def __init__(self, *, explode=False):
        self.explode = explode
        self.descriptor = IntegrationDescriptor(
            "acceptance-auth-provider",
            "Acceptance auth provider",
            frozenset({Capability.RESOLVE}),
            request_types=frozenset({"acceptance-auth"}),
        )

    async def resolve(self, request):
        return ResolutionResult(ResourceState.UNKNOWN, input_required=auth_required(username_password()))

    async def resolve_with_input(self, request, submitted):
        if self.explode:
            raise RuntimeError("provider continuation rejected supplied credential material")
        assert submitted.method == InputMethod.USERNAME_PASSWORD
        assert submitted.value(InputField.USERNAME)
        assert submitted.value(InputField.PASSWORD)
        return ResolutionResult(ResourceState.UNKNOWN, input_required=auth_required(username_password()))


def assert_sensitive_absent(text: str, markers: tuple[str, ...]) -> None:
    assert not any(marker in text for marker in markers), "sensitive marker leaked"


async def make_waiting_engine(tmp_path, monkeypatch, *, explode=False):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = AcceptanceAuthProvider(explode=explode)
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(adoption_stability_seconds=0),
    )
    await engine.initialize()
    transfer = await engine.submit((TransferRequest("acceptance-auth", "opaque-source"),), deduplicate=False)
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    assert challenge is not None
    return repository, engine, transfer, challenge


@pytest.mark.asyncio
async def test_item2_database_shape_upgrades_in_place_and_is_idempotent(tmp_path, monkeypatch):
    """Simulate the qualified Item 2 DB: runtime state exists, challenge table does not."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "item2.db")
    await database.init_db()
    async with database.get_db() as db:
        await db.execute(
            "INSERT INTO torrents(id,hash,name,status,source) VALUES(41,'item2-fingerprint','Item 2 transfer','pending','manual')"
        )
        await db.execute(
            """INSERT INTO integration_runtime_state(
                integration_id,state_key,schema_version,payload,observed_at,stale_after,successful_at,created_at,updated_at,generation
            ) VALUES('provider-a','inventory','1',?,1,NULL,1,1,1,3)""",
            (b'{"healthy":true}',),
        )
        await db.execute("DROP TABLE transfer_input_challenges")
        await db.commit()

    await database.init_db()
    await database.init_db()

    async with database.get_db() as db:
        transfer = await db.fetchone("SELECT id,hash,name,status,source FROM torrents WHERE id=41")
        runtime = await db.fetchone(
            "SELECT integration_id,state_key,schema_version,payload,generation FROM integration_runtime_state WHERE integration_id='provider-a'"
        )
        columns = {row["name"] for row in await db.fetchall("PRAGMA table_info(transfer_input_challenges)")}
    assert transfer == {
        "id": 41,
        "hash": "item2-fingerprint",
        "name": "Item 2 transfer",
        "status": "pending",
        "source": "manual",
    }
    assert runtime["integration_id"] == "provider-a" and runtime["state_key"] == "inventory"
    assert runtime["schema_version"] == "1" and runtime["generation"] == 3
    assert bytes(runtime["payload"]) == b'{"healthy":true}'
    assert {
        "transfer_id", "challenge_id", "generation", "reason", "origin", "integration_id",
        "operation_id", "request_id", "artifact_id", "methods", "created_at", "updated_at",
    } <= columns


@pytest.mark.asyncio
async def test_backup_persists_only_challenge_metadata_not_pending_credentials(tmp_path, monkeypatch):
    repository, engine, transfer, challenge = await make_waiting_engine(tmp_path, monkeypatch)
    markers = (
        "backup-user-sensitive-marker",
        "backup-password-sensitive-marker",
    )
    await engine.submit_input(
        transfer.id,
        challenge.id,
        "username_password",
        {"username": markers[0], "password": markers[1]},
    )
    assert await engine.inputs.has(challenge)

    backup_root = tmp_path / "backups"
    monkeypatch.setattr(
        db_maintenance,
        "get_settings",
        lambda: SimpleNamespace(
            db_backup_enabled=True,
            db_backup_folder=str(backup_root),
            db_backup_keep_days=7,
        ),
    )
    result = await db_maintenance.run_database_backup()
    assert not result["errors"]
    payload = Path(result["file"]).read_text(encoding="utf-8")
    assert_sensitive_absent(payload, markers)
    decoded = json.loads(payload)
    rows = decoded["tables"]["transfer_input_challenges"]
    assert len(rows) == 1
    assert rows[0]["challenge_id"] == challenge.id
    assert rows[0]["reason"] == "auth_required"
    assert rows[0]["origin"] == "provider"
    assert "username_password" in rows[0]["methods"]
    assert await engine.inputs.has(challenge)
    assert (await repository.get(transfer.id)).state == TransferState.INPUT_REQUIRED


@pytest.mark.asyncio
async def test_internal_continuation_exception_does_not_persist_or_echo_credentials(tmp_path, monkeypatch, caplog):
    repository, engine, transfer, challenge = await make_waiting_engine(tmp_path, monkeypatch, explode=True)
    markers = (
        "exception-user-sensitive-marker",
        "exception-password-sensitive-marker",
    )
    await engine.submit_input(
        transfer.id,
        challenge.id,
        "username_password",
        {"username": markers[0], "password": markers[1]},
    )
    await engine.tick()

    async with database.get_db() as db:
        tables = [row["name"] for row in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")]
        snapshot = {}
        for table in tables:
            if table.startswith("sqlite_"):
                continue
            snapshot[table] = await db.fetchall(f"SELECT * FROM {table}")
    encoded = json.dumps(snapshot, sort_keys=True, default=str)
    assert_sensitive_absent(encoded, markers)
    assert_sensitive_absent(caplog.text, markers)
    assert not await engine.inputs.has(challenge)
    assert await engine.challenges.current(transfer.id) is None
    assert (await repository.get(transfer.id)).state == TransferState.FAILED


@pytest.mark.asyncio
async def test_challenge_schema_and_runtime_state_are_distinct_nonsecret_categories(tmp_path, monkeypatch):
    repository, engine, transfer, challenge = await make_waiting_engine(tmp_path, monkeypatch)
    async with database.get_db() as db:
        challenge_columns = {row["name"] for row in await db.fetchall("PRAGMA table_info(transfer_input_challenges)")}
        runtime_columns = {row["name"] for row in await db.fetchall("PRAGMA table_info(integration_runtime_state)")}
        runtime_count = (await db.fetchone("SELECT COUNT(*) AS n FROM integration_runtime_state"))["n"]
    forbidden = {"username", "password", "private_key", "passphrase", "credentials", "secret"}
    assert not (challenge_columns & forbidden), "credential field reached durable challenge schema"
    assert not (runtime_columns & forbidden), "credential field reached runtime-state schema"
    assert runtime_count == 0
    assert challenge.transfer_id == transfer.id
    assert (await repository.presentation(transfer.id))["input_required"]["id"] == challenge.id
