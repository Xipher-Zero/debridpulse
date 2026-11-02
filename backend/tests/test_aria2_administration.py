"""Native administration preserves canonical intent and shared-daemon isolation."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_aria2_executor_contract import execution
from executors.aria2.admin import Aria2Administration
from executors.aria2.client import Aria2DownloadStatus
from executors.aria2.definition import Aria2Options
from executors.aria2.runtime import NATIVE_ACTIVE_DOWNLOADS


def _runtime(**options):
    return SimpleNamespace(options=Aria2Options(**options))


def _repository(attempts, authorize):
    """A repository double answering ``authorize_executions`` through the
    fixture's single-handle grant."""
    async def authorize_executions(handles, action):
        return tuple([await authorize(handle, action) for handle in handles])
    return SimpleNamespace(executions=AsyncMock(return_value=tuple(attempts)), authorize_executions=authorize_executions)


@pytest.mark.asyncio
async def test_low_level_pause_and_resume_use_core_transfer_identity(execution):
    attempt = SimpleNamespace(handle=execution.handle, transfer_id=47, artifact_id=9)
    repository = _repository((attempt,), execution.executor.authorize)
    application = SimpleNamespace(pause=AsyncMock(), resume=AsyncMock())
    admin = Aria2Administration(execution.executor, repository, application, _runtime())
    await admin.control(execution.handle.native["gid"], "pause")
    await admin.control(execution.handle.native["gid"], "resume")
    application.pause.assert_awaited_once_with(47)
    application.resume.assert_awaited_once_with(47)
    with pytest.raises(PermissionError):
        await admin.control("foreign", "pause")
    assert not execution.daemon.calls


@pytest.mark.asyncio
async def test_owned_gid_with_foreign_path_is_excluded_from_native_admin(execution):
    attempt = SimpleNamespace(handle=execution.handle)
    repository = _repository((attempt,), execution.executor.authorize)
    admin = Aria2Administration(execution.executor, repository, None, _runtime())
    forged = Aria2DownloadStatus(execution.handle.native["gid"], "active", 4, 1, 1, files=[{"path": "/foreign/file"}])
    assert await admin.filter_owned([forged]) == []


class _FakeGlobalOptionsClient:
    """Minimal RPC-client double exposing only ``change_global_options`` --
    ``Aria2Administration.apply_memory_tuning()`` needs nothing else from
    ``self.executor.client``."""

    def __init__(self):
        self.calls = []

    async def change_global_options(self, options):
        self.calls.append(options)
        return "OK"


@pytest.mark.asyncio
async def test_apply_memory_tuning_never_mirrors_global_policy_into_native_options():
    """Universal Executor Leveling: DebridPulse global concurrency and global
    bandwidth have one core owner each. aria2 tuning reapplies only aria2-owned
    options: its native queue width is an executor-local constant (never the
    DP global concurrency), and its download ceiling is changed only through
    the core-assigned ceiling operation, never from stale configuration."""
    client = _FakeGlobalOptionsClient()
    admin = Aria2Administration(SimpleNamespace(client=client), None, None, _runtime())
    await admin.apply_memory_tuning()
    applied = client.calls[-1]
    assert applied["max-concurrent-downloads"] == str(NATIVE_ACTIVE_DOWNLOADS)
    assert "max-overall-download-limit" not in applied


@pytest.mark.asyncio
async def test_waiting_count_prevents_restart_even_when_snapshot_window_is_empty(execution, monkeypatch):
    """Specification section 9.3: ``Aria2Administration`` consumes the
    injected aria2 runtime directly -- it never calls
    ``core.config.get_settings()`` itself, so this test injects tuning
    through the constructor instead of monkeypatching settings lookups."""
    import executors.aria2.admin as module

    runtime = SimpleNamespace(options=Aria2Options(purge_interval_minutes=0, restart_interval_hours=1),
                              _started_at=1, restart=AsyncMock(), ensure_log_rotation=AsyncMock(),
                              process_running=True)
    monkeypatch.setattr(module.time, "time", lambda: 5000)
    execution.daemon.get_global_stat = AsyncMock(return_value={"active": 0, "waiting": 1})
    execution.daemon.get_all = AsyncMock(return_value=[])
    admin = Aria2Administration(execution.executor, None, None, runtime)
    await admin.maintain()
    runtime.restart.assert_not_awaited()
    execution.daemon.get_all.assert_not_awaited()


# ── ownership of native jobs: one authorization read, the core's own decision ──

async def _baseline_authorize(handle, action):
    """``TransferRepository.authorize_execution`` as it stood before
    (DebridPulse 1.0.14, 523d3c7b), verbatim: the per-handle oracle."""
    from db.database import get_db
    from transfers import codec
    from transfers.repository import TransferRepository

    async with get_db() as db:
        row = await db.fetchone("""SELECT e.*,t.status AS transfer_status,COALESCE(p.paused,0) AS paused_intent,
            f.execution_attempt_id AS current_execution_id
            FROM execution_attempts e JOIN torrents t ON t.id=e.transfer_id
            JOIN download_files f ON f.id=e.artifact_id
            LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id WHERE e.id=?""", (handle.attempt_id,))
    if not row or not row["authorized"] or row["executor_id"] != handle.executor_id or codec.load(row["handle"]) != codec.load(codec.dump(handle)):
        return False
    is_current = row.get("current_execution_id") == handle.attempt_id
    if action in {"start", "resume", "pause"} and not is_current:
        return False
    if action == "cancel" and not is_current:
        cleanup_owned = row["transfer_status"] in {"deleted", "cancelled"} and row.get("cleanup_state") in {"pending", "blocked"}
        if not cleanup_owned:
            return False
    if action in {"start", "resume"} and row.get("native_transition_from"):
        return False
    if action in {"start", "resume"} and (row["transfer_status"] in {"deleted", "completed", "consolidated", "cancelled"} or row["paused_intent"]):
        return False
    if action in {"start", "resume"} and await TransferRepository().globally_paused():
        return False
    return action != "start" or row["state"] == "prepared"


async def _baseline_owned(admin, repository):
    """``Aria2Administration._owned`` as it stood before, verbatim, one
    authorization query per candidate attempt."""
    result = {}
    for attempt in await repository.executions():
        handle = attempt.handle
        if (handle.executor_id == admin.executor.descriptor.id
                and handle.correlation.get("binding") == admin.executor.binding
                and await _baseline_authorize(handle, "observe")):
            result[admin.executor._handle_gid(handle)] = attempt
    return result


def _handle(executor, attempt_id, gid, *, executor_id=None, binding=None, native=True):
    return {"executor_id": executor_id or executor.descriptor.id, "attempt_id": attempt_id,
            "correlation": {"target": "/payloads/file", "binding": binding or executor.binding,
                            **({} if native else {"gid": gid})},
            "native": {"gid": gid} if native else None}


@pytest.fixture
def ownership(execution, tmp_path, monkeypatch):
    """A real database holding one attempt for every authorization case the
    admin route meets, written row by row so each case is exactly what it
    names; ``history`` adds that many superseded attempts of one artifact."""
    import itertools
    import json
    import sqlite3

    from db import database
    from transfers.repository import TransferRepository

    executor = execution.executor
    databases = itertools.count()

    async def build(history=0, globally_paused=False):
        monkeypatch.setattr(database, "DB_PATH", tmp_path / f"ownership-{next(databases)}.db")
        await database.init_db()
        conn = sqlite3.connect(database.DB_PATH)
        with conn:
            def transfer(tid, status):
                conn.execute("INSERT INTO torrents(id, hash, status) VALUES(?,?,?)", (tid, f"h{tid}", status))

            def artifact(fid, tid, current):
                conn.execute("INSERT INTO download_files(id, torrent_id, execution_attempt_id) VALUES(?,?,?)", (fid, tid, current))

            def attempt(aid, tid, fid, handle, *, executor_id=None, authorized=1, state="running", **extra):
                columns = {"id": aid, "transfer_id": tid, "artifact_id": fid,
                           "executor_id": executor_id or handle.get("executor_id"), "handle": json.dumps(handle, sort_keys=True),
                           "state": state, "authorized": authorized, **extra}
                conn.execute(f"INSERT INTO execution_attempts({','.join(columns)}) VALUES({','.join('?' * len(columns))})",
                             tuple(columns.values()))

            transfer(1, "downloading"); artifact(10, 1, "current")
            for index in range(history):                                     # superseded, still authorized
                attempt(f"old-{index}", 1, 10, _handle(executor, f"old-{index}", f"g-old-{index}"), state="cancelled")
            attempt("current", 1, 10, _handle(executor, "current", "g-current"))
            attempt("revoked", 1, 10, _handle(executor, "revoked", "g-revoked"), authorized=0)
            attempt("foreign-binding", 1, 10, _handle(executor, "foreign-binding", "g-fb", binding="another-daemon"))
            attempt("other-executor", 1, 10, _handle(executor, "other-executor", "g-oe", executor_id="rsync"))
            attempt("column-other", 1, 10, _handle(executor, "column-other", "g-co"), executor_id="rsync")
            # a handle naming another attempt whose stored handle differs
            attempt("points-elsewhere", 1, 10, _handle(executor, "current", "g-pe"))
            # two rows carrying one identical handle: the row it names decides
            attempt("twin", 1, 10, _handle(executor, "twin-target", "g-twin"))
            attempt("twin-target", 1, 10, _handle(executor, "twin-target", "g-twin"))
            # one native job named by two attempts: the later attempt is the owner shown
            attempt("dup-first", 1, 10, _handle(executor, "dup-first", "g-dup"))
            attempt("dup-second", 1, 10, _handle(executor, "dup-second", "g-dup"))
            attempt("pre-binding", 1, 10, _handle(executor, "pre-binding", "g-pre", native=False))
            attempt("prepared", 1, 10, _handle(executor, "prepared", "g-prepared"), state="prepared")
            attempt("transition", 1, 10, _handle(executor, "transition", "g-transition"), native_transition_from="older")
            attempt(77, 1, 10, _handle(executor, 77, "g-numeric"))                # an attempt id that is not text
            transfer(2, "deleted"); artifact(20, 2, "deleted-current")
            attempt("deleted-current", 2, 20, _handle(executor, "deleted-current", "g-del"))
            attempt("deleted-cleanup", 2, 20, _handle(executor, "deleted-cleanup", "g-cleanup"), cleanup_state="pending")
            transfer(3, "downloading"); artifact(30, 3, "paused-current")
            conn.execute("INSERT INTO transfer_pause_intents(torrent_id, paused) VALUES(3, 1)")
            attempt("paused-current", 3, 30, _handle(executor, "paused-current", "g-paused"))
            transfer(4, "completed"); artifact(40, 4, "completed-current")
            attempt("completed-current", 4, 40, _handle(executor, "completed-current", "g-completed"))
            attempt("no-artifact", 1, 99, _handle(executor, "no-artifact", "g-no-artifact"))
            attempt("no-transfer", 9, 10, _handle(executor, "no-transfer", "g-no-transfer"))
            if globally_paused:
                conn.execute("INSERT OR REPLACE INTO transfer_controls(key, value) VALUES('paused', '1')")
        conn.close()
        repository = TransferRepository()
        application = SimpleNamespace(pause=AsyncMock(), resume=AsyncMock(), cancel_artifact=AsyncMock())
        return Aria2Administration(executor, repository, application, _runtime()), repository, application

    return build


@pytest.mark.asyncio
@pytest.mark.parametrize("globally_paused", [False, True], ids=["running", "globally_paused"])
async def test_batch_authorization_decides_exactly_as_each_handle_alone(ownership, globally_paused):
    """Every attempt's handle, under every action, gets the per-handle
    oracle's answer from ``authorize_executions`` -- and from the current
    ``authorize_execution``, which shares its decision."""
    admin, repository, _application = await ownership(history=3, globally_paused=globally_paused)
    handles = [attempt.handle for attempt in await repository.executions()]
    assert len(handles) == 23
    for action in ("observe", "start", "resume", "pause", "cancel"):
        expected = tuple([await _baseline_authorize(handle, action) for handle in handles])
        assert await repository.authorize_executions(handles, action) == expected, action
        assert tuple([await repository.authorize_execution(handle, action) for handle in handles]) == expected, action
    observed = zip(handles, await repository.authorize_executions(handles, "observe"))
    # Authorization answers for the attempt, whichever daemon it ran on; the
    # admin route's own binding filter is separate (next test).
    assert sorted(admin.executor._handle_gid(handle) for handle, allowed in observed if allowed) == sorted([
        "g-old-0", "g-old-1", "g-old-2", "g-current", "g-fb", "g-oe", "g-twin", "g-twin", "g-dup", "g-dup",
        "g-pre", "g-prepared", "g-transition", "g-numeric", "g-del", "g-cleanup", "g-paused", "g-completed"])
    assert await repository.authorize_executions([], "observe") == ()


@pytest.mark.asyncio
async def test_owned_native_jobs_match_the_per_attempt_oracle_and_control_follows_them(ownership):
    admin, repository, application = await ownership(history=3)
    expected = await _baseline_owned(admin, repository)
    owned = await admin._owned()
    assert {gid: attempt.handle.attempt_id for gid, attempt in owned.items()} == {
        gid: attempt.handle.attempt_id for gid, attempt in expected.items()}
    assert {gid: attempt.handle.attempt_id for gid, attempt in owned.items()} == {
        "g-old-0": "old-0", "g-old-1": "old-1", "g-old-2": "old-2", "g-current": "current",
        "g-twin": "twin-target", "g-dup": "dup-second", "g-pre": "pre-binding", "g-prepared": "prepared",
        "g-transition": "transition", "g-numeric": 77, "g-del": "deleted-current", "g-cleanup": "deleted-cleanup",
        "g-paused": "paused-current", "g-completed": "completed-current"}
    await admin.control("g-current", "pause")
    application.pause.assert_awaited_once_with(1)
    await admin.control("g-cleanup", "remove")
    application.cancel_artifact.assert_awaited_once_with(2, 20)
    for gid in ("g-revoked", "g-fb", "g-oe", "g-co", "g-pe", "g-no-artifact", "g-no-transfer", "foreign"):
        with pytest.raises(PermissionError):
            await admin.control(gid, "pause")
    application.resume.assert_not_awaited()


@pytest.mark.asyncio
async def test_owning_native_jobs_reads_the_database_a_fixed_number_of_times(ownership):
    """The per-attempt oracle's database reads grow with the attempt history;
    ownership's do not."""
    from db import database

    async def acquires(call):
        before = database.db_runtime_metrics()["sqlite_acquires"]
        await call()
        return database.db_runtime_metrics()["sqlite_acquires"] - before

    counts = {}
    for history in (0, 200):
        admin, repository, _application = await ownership(history=history)
        counts[history] = (await acquires(admin._owned),
                           await acquires(lambda admin=admin, repository=repository: _baseline_owned(admin, repository)))
    assert counts[0][0] == counts[200][0] <= 3, counts
    assert counts[200][1] - counts[0][1] == 200, counts


@pytest.mark.asyncio
async def test_a_malformed_handle_fails_the_listing_as_it_did(ownership):
    """An attempt whose stored handle is not in the canonical layout makes the
    attempt history unreadable, for the oracle and for ownership alike: no
    partial listing is offered in its place."""
    import sqlite3

    from db import database

    admin, repository, _application = await ownership()
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute("INSERT INTO execution_attempts(id, transfer_id, artifact_id, executor_id, handle, state) "
                     "VALUES('malformed', 1, 10, 'aria2', '{\"attempt_id\": \"malformed\"}', 'running')")
    with pytest.raises(TypeError):
        await _baseline_owned(admin, repository)
    with pytest.raises(TypeError):
        await admin._owned()
