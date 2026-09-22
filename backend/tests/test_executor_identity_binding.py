"""Durable DP attempt identity with a one-way, opaque native identity binding.

The ledger executor learns its native identity only after native acceptance;
core persists the prepared correlation first, binds the native identity once
through one repository mutation, and never parses either opaque map.
"""
from __future__ import annotations

from dataclasses import replace
import json
import sqlite3

import pytest

import db.database as database
from executor_fakes import LedgerExecutor, artifact_of, ledger_core, submit_ledger
from transfers import codec
from transfers.input_required import SubmittedInput
from transfers.models import (
    ExecutionHandle, ExecutionObservation, ExecutionState, InputField, InputMethod,
)

pytestmark = pytest.mark.asyncio


async def _row(attempt_id):
    async with database.get_db() as db:
        return await db.fetchone("SELECT * FROM execution_attempts WHERE id=?", (attempt_id,))


async def test_prepare_persists_correlation_before_native_start(tmp_path, monkeypatch):
    seen = {}

    class Inspecting(LedgerExecutor):
        async def start(self, request, handle):
            seen["row"] = await _row(handle.attempt_id)
            return await super().start(request, handle)

    core = await ledger_core(tmp_path, monkeypatch, executors=lambda authorize: (Inspecting(authorize),))
    transfer = await submit_ledger(core)
    persisted = json.loads(seen["row"]["handle"])
    assert persisted["correlation"] == {"ticket": persisted["attempt_id"]}
    assert persisted["native"] is None  # native identity unknown until acceptance
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution.native == {"job": "srv-ledger-copy-1"}


async def test_start_may_bind_native_identity_once(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer = await submit_ledger(core)
    artifact = await artifact_of(core, transfer.id)
    row = await _row(artifact.execution.attempt_id)
    assert json.loads(row["handle"])["native"] == {"job": "srv-ledger-copy-1"}
    await core.engine.reconcile_executions()
    assert (await artifact_of(core, transfer.id)).execution == artifact.execution


async def test_native_identity_binding_is_idempotent(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer = await submit_ledger(core)
    bound = (await artifact_of(core, transfer.id)).execution
    prepared = replace(bound, native=None)
    assert await core.repository.bind_execution_handle(prepared, bound)
    assert await core.repository.bind_execution_handle(prepared, bound)
    assert (await artifact_of(core, transfer.id)).execution == bound


async def test_native_identity_cannot_be_replaced(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer = await submit_ledger(core)
    bound = (await artifact_of(core, transfer.id)).execution
    assert not await core.repository.bind_execution_handle(bound, replace(bound, native={"job": "other"}))
    assert not await core.repository.bind_execution_handle(replace(bound, native=None),
                                                           replace(bound, native={"job": "other"}))
    assert not await core.repository.bind_execution_handle(bound, replace(bound, native=None))  # removal
    assert (await artifact_of(core, transfer.id)).execution == bound


async def test_correlation_cannot_change_during_binding(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    core.executor.lose_start_ack = True
    transfer = await submit_ledger(core)
    prepared = (await artifact_of(core, transfer.id)).execution
    assert prepared.native is None
    forged = ExecutionHandle(prepared.executor_id, prepared.attempt_id, {"ticket": "forged"}, {"job": "x"})
    assert not await core.repository.bind_execution_handle(prepared, forged)
    assert (await artifact_of(core, transfer.id)).execution == prepared


async def test_executor_id_or_attempt_id_cannot_change_during_binding(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    core.executor.lose_start_ack = True
    transfer = await submit_ledger(core)
    prepared = (await artifact_of(core, transfer.id)).execution
    for forged in (replace(prepared, executor_id="other-executor", native={"job": "x"}),
                   replace(prepared, attempt_id="f" * 32, native={"job": "x"})):
        assert not await core.repository.bind_execution_handle(prepared, forged)
    # The engine's one acceptance function rejects the same mutations.
    artifact = await artifact_of(core, transfer.id)
    with pytest.raises(Exception):
        await core.engine._accept_observation(prepared, ExecutionObservation(
            replace(prepared, executor_id="other-executor", native={"job": "x"}), ExecutionState.RUNNING))
    assert (await artifact_of(core, transfer.id)).execution == artifact.execution


async def test_lost_start_ack_reconciles_existing_native_job_without_second_start(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    core.executor.lose_start_ack = True
    transfer = await submit_ledger(core)
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution.native is None and artifact.state == "unknown"
    core.executor.lose_start_ack = False
    for _ in range(3):
        await core.engine.reconcile_executions()
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution.native == {"job": "srv-ledger-copy-1"}
    assert [call for call in core.executor.calls if call[0] == "start"] == [("start", artifact.execution.attempt_id)]
    assert len(core.executor.jobs) == 1


async def test_observe_many_may_bind_native_identity_after_restart(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    core.executor.lose_start_ack = True
    transfer = await submit_ledger(core)
    jobs = core.executor.jobs
    restarted = await ledger_core(tmp_path, monkeypatch)  # same database, fresh engine/executor objects
    restarted.executor.jobs = jobs
    await restarted.engine.reconcile_executions()
    artifact = await artifact_of(restarted, transfer.id)
    assert artifact.execution.native == {"job": "srv-ledger-copy-1"}
    assert not [call for call in restarted.executor.calls if call[0] == "start"]


async def test_execution_handle_rejects_known_secret_bearing_objects():
    secret = SubmittedInput("challenge", 1, InputMethod.USERNAME_PASSWORD,
                            {InputField.USERNAME: "user-sentinel", InputField.PASSWORD: "password-sentinel"})
    for handle in (ExecutionHandle("ledger-copy", "a" * 32, {"credential": secret}),
                   ExecutionHandle("ledger-copy", "a" * 32, {"ticket": "t"}, {"credential": secret})):
        with pytest.raises(TypeError):
            codec.dump(handle)


async def test_transient_executor_input_is_never_persisted_in_handle_or_observation(tmp_path, monkeypatch):
    from fake_integrations import VaultExecutor, VaultProvider

    def executors(authorize):
        return (VaultExecutor(authorize, objects={"locked.example/item.bin": b"x" * 32},
                              locks={"locked.example": ("vault-user-sentinel", "vault-pass-sentinel")}),)

    core = await ledger_core(tmp_path, monkeypatch, executors=executors, providers=(VaultProvider(),))
    from transfers.models import TransferRequest
    transfer = await core.engine.submit((TransferRequest("vault", "locked.example/item.bin", name="item.bin"),))
    for _ in range(4):
        await core.engine.tick()
    challenge = await core.engine.challenges.current(transfer.id)
    assert challenge is not None
    await core.engine.submit_input(transfer.id, challenge.id, "username_password",
                                   {"username": "vault-user-sentinel", "password": "vault-pass-sentinel"})
    for _ in range(4):
        await core.engine.tick()
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT handle,progress,error,materialization FROM execution_attempts")
    dumped = json.dumps([dict(row) for row in rows])
    assert rows and "vault-user-sentinel" not in dumped and "vault-pass-sentinel" not in dumped


async def test_historical_execution_handle_decodes_to_canonical_shape(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer = await submit_ledger(core)
    artifact = await artifact_of(core, transfer.id)
    legacy = {"executor_id": "ledger-copy", "attempt_id": artifact.execution.attempt_id,
              "context": {"ticket": artifact.execution.attempt_id}}
    async with database.get_db() as db:
        await db.execute("UPDATE execution_attempts SET handle=? WHERE id=?",
                         (json.dumps(legacy), artifact.execution.attempt_id))
        await db.commit()
    await database.init_db()  # one bounded one-way migration at initialization
    row = await _row(artifact.execution.attempt_id)
    assert json.loads(row["handle"]) == {"executor_id": "ledger-copy", "attempt_id": artifact.execution.attempt_id,
                                         "correlation": {"ticket": artifact.execution.attempt_id}, "native": None}
    handle = codec.handle(json.loads(row["handle"]))
    assert handle == ExecutionHandle("ledger-copy", artifact.execution.attempt_id,
                                     {"ticket": artifact.execution.attempt_id}, None)
    with pytest.raises(TypeError):
        codec.handle(legacy)  # the old layout is never a live runtime representation


async def test_historical_transferring_state_migrates_once_to_running(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer = await submit_ledger(core)
    artifact = await artifact_of(core, transfer.id)
    async with database.get_db() as db:
        await db.execute("UPDATE execution_attempts SET state='transferring' WHERE id=?",
                         (artifact.execution.attempt_id,))
        await db.commit()
    await database.init_db()
    await database.init_db()
    assert (await _row(artifact.execution.attempt_id))["state"] == "running"
    assert ExecutionState("running") == ExecutionState.RUNNING
    assert "TRANSFERRING" not in ExecutionState.__members__


async def test_historical_aria2_inflight_handle_retains_binding_authority(tmp_path, monkeypatch):
    from executors.aria2.executor import Aria2Configuration, Aria2Executor, execution_binding

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "aria2-history.db")
    await database.init_db()
    from transfers.recovery_repository import TransferRepository
    repository = TransferRepository()
    root = tmp_path / "downloads"
    root.mkdir()
    client = type("Client", (), {"url": "http://127.0.0.1:6800/jsonrpc"})()
    executor = Aria2Executor(client, Aria2Configuration(str(root)), repository.authorize_execution)
    attempt_id = "b" * 32
    gid = executor._gid(attempt_id)
    target = str(root / "movie.mkv")
    legacy = {"executor_id": "aria2", "attempt_id": attempt_id,
              "context": {"gid": gid, "target": target, "redactions": [], "binding": executor.binding}}
    async with database.get_db() as db:
        transfer_id = await db.execute_returning_id("INSERT INTO torrents(hash,name,status,source) VALUES('h','m','downloading','manual')")
        artifact_id = await db.execute_returning_id(
            "INSERT INTO download_files(torrent_id,filename,status,local_path,execution_attempt_id) VALUES(?,?,?,?,?)",
            (transfer_id, "movie.mkv", "downloading", target, attempt_id))
        await db.execute("INSERT INTO execution_attempts(id,transfer_id,artifact_id,executor_id,handle,state) "
                         "VALUES(?,?,?,?,?,'transferring')", (attempt_id, transfer_id, artifact_id, "aria2",
                                                                json.dumps(legacy)))
        await db.commit()
    await database.init_db()
    attempt = (await repository.executions(transfer_id))[0]
    assert attempt.state == "running"
    assert attempt.handle.native is None and attempt.handle.correlation["binding"] == execution_binding(
        str(root), client.url)
    assert await repository.authorize_execution(attempt.handle, "observe")
    bound = executor._bound(attempt.handle)
    assert bound.native == {"gid": gid}
    assert await repository.bind_execution_handle(attempt.handle, bound)
    assert await repository.authorize_execution(bound, "observe")
    assert not await repository.authorize_execution(attempt.handle, "observe")


async def test_fresh_schema_contains_only_new_canonical_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fresh.db")
    await database.init_db()
    connection = sqlite3.connect(tmp_path / "fresh.db")
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(execution_attempts)")}
    finally:
        connection.close()
    assert "materialization" in columns
    assert not {"gid", "nzo_id", "pid", "native_id", "context"} & columns
    assert database._TRANSFER_REPOSITORY_REQUIRED_COLUMNS["execution_attempts"] >= {"materialization"}


async def test_historical_schema_upgrade_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "upgrade.db")
    connection = sqlite3.connect(tmp_path / "upgrade.db")
    connection.execute("""CREATE TABLE execution_attempts (id TEXT PRIMARY KEY, transfer_id INTEGER NOT NULL,
        artifact_id INTEGER NOT NULL, executor_id TEXT NOT NULL, handle TEXT NOT NULL, state TEXT NOT NULL,
        authorized INTEGER NOT NULL DEFAULT 1, progress TEXT, error TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    connection.execute("INSERT INTO execution_attempts(id,transfer_id,artifact_id,executor_id,handle,state) "
                       "VALUES('c','1','1','ledger-copy',?,'transferring')",
                       (json.dumps({"executor_id": "ledger-copy", "attempt_id": "c", "context": {"ticket": "c"}}),))
    connection.commit()
    connection.close()
    await database.init_db()
    first = dict(await _row("c"))
    await database.init_db()
    second = dict(await _row("c"))
    assert first == second
    assert first["state"] == "running" and first["materialization"] is None
    assert json.loads(first["handle"])["correlation"] == {"ticket": "c"}


async def test_aria2_durable_handle_never_contains_endpoint_capabilities(tmp_path):
    from execution_requests import file_request
    from executors.aria2.client import Aria2DownloadStatus
    from executors.aria2.executor import Aria2Configuration, Aria2Executor
    from transfers.models import Endpoint, TransferCandidate

    signed = "https://cdn.example/file.bin?token=SIGNED-URL-SENTINEL&sig=abc"
    bearer = "Bearer HEADER-SECRET-SENTINEL"
    candidate = TransferCandidate("file.bin", (Endpoint("https", signed, {"Authorization": bearer}),), expected_bytes=4)
    client = type("Client", (), {"url": "http://127.0.0.1:6800/jsonrpc"})()
    executor = Aria2Executor(client, Aria2Configuration(str(tmp_path)), None)
    handle = executor.prepare(file_request(candidate, str(tmp_path / "file.bin"), "a" * 32, root=tmp_path))
    dumped = codec.dump(handle)
    assert "SIGNED-URL-SENTINEL" not in dumped and "HEADER-SECRET-SENTINEL" not in dumped
    assert "cdn.example" not in dumped
    # Native diagnostics echoing the capability are still redacted without any durable copy.
    native = Aria2DownloadStatus(executor._gid("a" * 32), "error", 4, 0, 0, error_code="1",
                                 error_message=f"failed {signed} with {bearer}",
                                 files=[{"path": str(tmp_path / "file.bin"), "uris": [{"uri": signed}]}])
    observed = executor._observation(handle, native)
    diagnostic = observed.error.diagnostic
    assert "SIGNED-URL-SENTINEL" not in diagnostic and "HEADER-SECRET-SENTINEL" not in diagnostic
