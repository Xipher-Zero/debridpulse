"""DP 1.0.12 recovery leveling, Phase 3 (base document Sections 14-20):
canonical recovery state must be state, not an event log.

Named regressions required by the base document's Section 33 "Recovery
persistence" group:

    test_progress_observations_do_not_append_full_recovery_snapshot_each_tick
    test_meaningful_progress_advances_current_epoch_and_clears_current_decision
    test_historical_failure_remains_audit_only_after_epoch_advance
    test_migration_recovers_latest_current_state_without_fabrication
    test_migration_is_idempotent

Before this leveling pass, "current" recovery state for an artifact was
reconstructed by scanning the latest ``application_events`` row of kind
``transfer_recovery:<artifact_id>`` -- an unbounded, append-only history that
grew a full-snapshot row on every meaningful mutation, including ordinary
progress-byte advancement at roughly scheduler cadence (``transfers.
repository.TransferRepository.execution()``). Current state now lives in
exactly one row per artifact in ``artifact_recovery_state``
(db/database.py), updated in place; semantically meaningful transitions
additionally get a small, separate, sparse audit record in
``application_events`` (kind ``recovery_audit``) via ``transfers.repository.
TransferRepository._append_recovery_audit``.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
import pytest_asyncio

import db.database as database
from db.database import get_db
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage
from transfers.models import ExecutionState, TransferProgress, TransferRequest
from transfers.policy import TransferPolicy
from transfers.recovery_execution import RecoveryTrigger
from transfers.recovery_repository import TransferRepository as RecoveryTransferRepository
from transfers.registry import IntegrationRegistry


def transient_error():
    return NormalizedError(
        Domain.NETWORK,
        Category.REMOTE_READ_FAILED,
        Stage.EXECUTION,
        retryability=Retryability.BACKOFF,
        origin=Origin.REMOTE_SOURCE,
        integration_id="memory-copy",
    )


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    """Lower-stack fixture (mirrors test_transfer_recovery_phase2.py's
    ``runtime``): sufficient for these tests, since the current-state storage
    change under test lives in transfers.repository.TransferRepository
    itself, the common base every recovery-repository layer builds on."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "recovery_state.db")
    await database.init_db()
    repository = RecoveryTransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    now = [9000.0]
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0),
        clock=lambda: now[0],
    )
    await engine.initialize()
    return repository, registry, provider, executor, engine, now


async def start_transfer(runtime):
    repository, _registry, _provider, _executor, engine, _now = runtime
    transfer = await engine.submit((TransferRequest("parcel", "box", name="payload.bin"),))
    await engine.resolve_pending()
    await engine.reconcile_executions()
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.execution is not None
    return transfer, artifact


async def _recovery_audit_rows(transfer_id: int, transition: str | None = None) -> list[dict]:
    from transfers import codec

    async with get_db() as db:
        rows = await db.fetchall(
            "SELECT detail FROM application_events WHERE transfer_id=? AND kind='recovery_audit' ORDER BY id",
            (transfer_id,),
        )
    decoded = [codec.load(row["detail"], {}) for row in rows]
    if transition is not None:
        decoded = [item for item in decoded if item.get("transition") == transition]
    return decoded


async def _state_row_count(artifact_id: int) -> int:
    async with get_db() as db:
        row = await db.fetchone(
            "SELECT COUNT(*) AS n FROM artifact_recovery_state WHERE artifact_id=?", (artifact_id,),
        )
    return int((row or {}).get("n") or 0)


@pytest.mark.asyncio
async def test_progress_observations_do_not_append_full_recovery_snapshot_each_tick(runtime):
    """Section 14/18's core named regression: ordinary sub-threshold progress
    advancement (the fake parcel payload is 4 bytes, so the meaningful-
    progress threshold equals the whole file -- see transfers.policy.
    meaningful_progress_threshold) updates the ONE current-state row in
    place and never appends to the audit log, no matter how many times it
    observes forward progress."""
    repository, _registry, _provider, executor, _engine, _now = runtime
    transfer, artifact = await start_transfer(runtime)
    handle = artifact.execution
    assert await _state_row_count(artifact.id) == 1
    baseline_audit = len(await _recovery_audit_rows(transfer.id))

    for completed in (2, 3):
        current = executor.jobs[handle.attempt_id]
        updated = replace(current, progress=TransferProgress(4, completed, 1))
        executor.jobs[handle.attempt_id] = updated
        await repository.execution(updated)

    # Ordinary sub-threshold progress: the current-state row is still
    # exactly one row (upserted in place, never appended), and no audit
    # event was written for mere byte advancement.
    assert await _state_row_count(artifact.id) == 1
    assert len(await _recovery_audit_rows(transfer.id)) == baseline_audit
    context = await repository.recovery_context(artifact.id)
    assert context["recovery_epoch"] == 0


@pytest.mark.asyncio
async def test_meaningful_progress_advances_current_epoch_and_clears_current_decision(runtime):
    """Crossing the meaningful-progress threshold (Section 16) advances the
    current recovery epoch and clears current, policy-relevant decision/
    quiescence state through the one canonical reset transition (Section 17)
    -- and, unlike ordinary progress, appends exactly one sparse audit
    record for this transition (Section 18)."""
    repository, _registry, _provider, executor, _engine, _now = runtime
    transfer, artifact = await start_transfer(runtime)
    handle = artifact.execution

    await repository.record_recovery_decision(artifact.id, "backoff", "pretend_backoff")
    before = await repository.recovery_context(artifact.id)
    assert before["decision_action"] == "backoff"
    assert before["recovery_epoch"] == 0
    baseline_audit = len(await _recovery_audit_rows(transfer.id, "meaningful_progress"))

    current = executor.jobs[handle.attempt_id]
    executor.jobs[handle.attempt_id] = replace(current, progress=TransferProgress(4, 4, 0))
    await repository.execution(replace(current, progress=TransferProgress(4, 4, 0)))

    after = await repository.recovery_context(artifact.id)
    assert after["recovery_epoch"] == before["recovery_epoch"] + 1
    assert after["decision_action"] is None
    assert after["decision_reason"] is None
    assert await _state_row_count(artifact.id) == 1

    audit_rows = await _recovery_audit_rows(transfer.id, "meaningful_progress")
    assert len(audit_rows) == baseline_audit + 1
    assert audit_rows[-1]["recovery_epoch"] == after["recovery_epoch"]


@pytest.mark.asyncio
async def test_historical_failure_remains_audit_only_after_epoch_advance(runtime):
    """Section 15: a purely historical/audit fact (failure_classification,
    never read back for a policy decision anywhere in the codebase) survives
    an unrelated current-epoch advance untouched -- proving it is genuinely
    separate storage from the current-state fields the reset transition
    actually mutates, not a flat object where advancing the epoch could
    plausibly (and wrongly) wipe or reinterpret it."""
    repository, _registry, _provider, executor, _engine, now = runtime
    transfer, artifact = await start_transfer(runtime)
    handle = artifact.execution

    claim = await repository.claim_recovery(artifact.id, RecoveryTrigger.AUTO_RETRY, now[0])
    assert claim is not None
    error = transient_error()
    assert await repository.record_phase3_decision(
        claim, decision_id="d1", action="backoff", reason="transient_network_failure",
        error=error, completed_bytes=0,
    )
    mid = await repository.recovery_context(artifact.id)
    assert mid["failure_classification"]["category"] == Category.REMOTE_READ_FAILED.value
    assert mid["failure_classification"]["domain"] == Domain.NETWORK.value
    assert mid["decision_action"] == "backoff"

    current = executor.jobs[handle.attempt_id]
    executor.jobs[handle.attempt_id] = replace(current, progress=TransferProgress(4, 4, 0))
    await repository.execution(replace(current, progress=TransferProgress(4, 4, 0)))

    after = await repository.recovery_context(artifact.id)
    assert after["recovery_epoch"] == mid["recovery_epoch"] + 1
    # Current, policy-relevant decision state was cleared by the meaningful-
    # progress reset transition (Section 17) ...
    assert after["decision_action"] is None
    # ... but the historical classification fact from the earlier failure is
    # still readable -- preserved audit trivia, not current policy state,
    # and therefore not something the reset transition touches at all.
    assert after["failure_classification"]["category"] == Category.REMOTE_READ_FAILED.value
    assert after["failure_classification"]["domain"] == Domain.NETWORK.value


@pytest.mark.asyncio
async def test_migration_recovers_latest_current_state_without_fabrication(tmp_path, monkeypatch):
    """Section 19: the one-time backfill reconstructs current state from a
    pre-leveling artifact's latest legacy ``transfer_recovery:<id>``
    snapshot event, using the exact same "known column facts win" rule the
    old read path used -- and fabricates nothing for an artifact that has no
    such history."""
    from transfers import codec

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "migrate.db")
    await database.init_db()

    async with get_db() as db:
        await db.execute(
            "INSERT INTO torrents(id,hash,name,status,source) VALUES(1,'h1','T1','queued','magnet')",
        )
        await db.execute(
            """INSERT INTO download_files(id,torrent_id,filename,status,recovery_failures,recovery_refreshes)
               VALUES(101,1,'legacy-history.bin','queued',3,1)""",
        )
        await db.execute(
            """INSERT INTO download_files(id,torrent_id,filename,status,recovery_failures,recovery_refreshes)
               VALUES(102,1,'no-history.bin','queued',0,0)""",
        )
        legacy_snapshot = {
            "version": 2, "recovery_epoch": 2, "progress_anchor": 512,
            "consecutive_no_progress_failures": 3, "failures_since_meaningful_progress": 1,
            "failure_signature": "network:remote_read_failed", "same_signature_failures": 2,
            "candidate_refreshes": 1, "candidate_switches": 1,
            "decision_action": "try_alternate_candidate", "decision_reason": "budget_exhausted",
            "quiescence_reason": None, "wake_condition": None,
            "candidate_attempt_history": ["cand-a", "cand-b"],
        }
        await db.execute(
            "INSERT INTO application_events(transfer_id,kind,detail,claimed) VALUES(1,?,?,1)",
            ("transfer_recovery:101", codec.dump(legacy_snapshot)),
        )
        await db.commit()

    # Re-run the bootstrap: this is what performs the one-time backfill.
    await database.init_db()

    async with get_db() as db:
        migrated = await db.fetchone(
            "SELECT * FROM artifact_recovery_state WHERE artifact_id=101",
        )
        untouched = await db.fetchone(
            "SELECT * FROM artifact_recovery_state WHERE artifact_id=102",
        )

    assert migrated is not None
    assert migrated["recovery_epoch"] == 2
    assert migrated["progress_anchor"] == 512
    assert migrated["consecutive_no_progress_failures"] == 3
    assert migrated["candidate_refreshes"] == 1
    assert migrated["candidate_switches"] == 1
    assert migrated["decision_action"] == "try_alternate_candidate"
    assert codec.load(migrated["candidate_attempt_history"], []) == ["cand-a", "cand-b"]

    # Section 19 item 5: an artifact with no legacy history is left with no
    # fabricated current-state row at all -- exactly like the pre-leveling
    # behavior where an absent event seeded all-defaults at READ time
    # instead of manufacturing a durable row for it.
    assert untouched is None

    repository = RecoveryTransferRepository()
    await repository.initialize()
    context = await repository.recovery_context(101)
    assert context["recovery_epoch"] == 2
    assert context["candidate_attempt_history"] == ["cand-a", "cand-b"]

    # The historical application_events row is untouched, byte-identical --
    # this migration only ever adds, never mutates or deletes.
    async with get_db() as db:
        preserved = await db.fetchone(
            "SELECT detail FROM application_events WHERE kind='transfer_recovery:101'",
        )
    assert codec.load(preserved["detail"], {}) == legacy_snapshot


@pytest.mark.asyncio
async def test_migration_covers_representative_pre_leveling_states(tmp_path, monkeypatch):
    """Base document Section 36's exact required coverage matrix, in one
    mixed-multi-artifact transfer: many progress snapshots (only the LATEST
    legacy event must win, never an earlier one), retry backoff, an active
    recovery claim, wait-for-operator quiescence, and meaningful progress
    recorded alongside stale historical fields -- proving migration does not
    fabricate, cross-contaminate between sibling artifacts, or resurrect a
    superseded snapshot."""
    from transfers import codec

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "migrate_matrix.db")
    await database.init_db()

    async with get_db() as db:
        await db.execute(
            "INSERT INTO torrents(id,hash,name,status,source) VALUES(9,'h9','T9','queued','magnet')",
        )
        for artifact_id, name in ((901, "many-snapshots.bin"), (902, "backoff-claim.bin"), (903, "operator-wait.bin")):
            await db.execute(
                """INSERT INTO download_files(id,torrent_id,filename,status,recovery_failures,recovery_refreshes)
                   VALUES(?,9,?,'queued',0,0)""",
                (artifact_id, name),
            )

        # Artifact 901: TWO historical snapshots (many-progress-snapshots
        # scenario) -- an earlier one a naive "any row" read could wrongly
        # pick, and the genuinely latest one (higher id, inserted second)
        # carrying meaningful progress alongside a stale historical decision
        # field that must not itself become fabricated current policy.
        stale_earlier = {
            "version": 2, "recovery_epoch": 1, "progress_anchor": 10,
            "consecutive_no_progress_failures": 1, "decision_action": "backoff",
            "decision_reason": "first_failure",
        }
        latest_meaningful_progress = {
            "version": 2, "recovery_epoch": 4, "progress_anchor": 99999,
            "consecutive_no_progress_failures": 0, "failures_since_meaningful_progress": 0,
            "decision_action": None, "decision_reason": None,
            "quiescence_reason": None, "wake_condition": None,
        }
        await db.execute(
            "INSERT INTO application_events(transfer_id,kind,detail,claimed) VALUES(9,?,?,1)",
            ("transfer_recovery:901", codec.dump(stale_earlier)),
        )
        await db.execute(
            "INSERT INTO application_events(transfer_id,kind,detail,claimed) VALUES(9,?,?,1)",
            ("transfer_recovery:901", codec.dump(latest_meaningful_progress)),
        )

        # Artifact 902: retry backoff decision plus an ACTIVE recovery claim
        # (token/trigger/until all set) -- migration must carry every claim
        # field forward verbatim, not just the counters.
        backoff_with_claim = {
            "version": 2, "recovery_epoch": 1, "decision_action": "backoff",
            "decision_reason": "transient_network_failure",
            "recovery_claim_token": "claim-abc", "recovery_claim_trigger": "auto_retry",
            "recovery_claim_until": 5000.0,
        }
        await db.execute(
            "INSERT INTO application_events(transfer_id,kind,detail,claimed) VALUES(9,?,?,1)",
            ("transfer_recovery:902", codec.dump(backoff_with_claim)),
        )

        # Artifact 903: wait-for-operator quiescence (the exact combination
        # presentation_repository.recovery_presentation reads to render
        # "Requires attention").
        wait_for_operator = {
            "version": 2, "recovery_epoch": 2, "decision_action": "wait_for_operator",
            "decision_reason": "recovery_exhausted", "quiescence_reason": "recovery_exhausted",
            "wake_condition": "operator_retry",
        }
        await db.execute(
            "INSERT INTO application_events(transfer_id,kind,detail,claimed) VALUES(9,?,?,1)",
            ("transfer_recovery:903", codec.dump(wait_for_operator)),
        )
        await db.commit()

    await database.init_db()

    async with get_db() as db:
        row_901 = await db.fetchone("SELECT * FROM artifact_recovery_state WHERE artifact_id=901")
        row_902 = await db.fetchone("SELECT * FROM artifact_recovery_state WHERE artifact_id=902")
        row_903 = await db.fetchone("SELECT * FROM artifact_recovery_state WHERE artifact_id=903")

    # Many-snapshots: the LATEST event wins, never the earlier, superseded one.
    assert row_901["recovery_epoch"] == 4
    assert row_901["progress_anchor"] == 99999
    assert row_901["decision_action"] is None, (
        "the latest snapshot's cleared decision must win over the earlier "
        "snapshot's stale 'backoff' decision -- migration must not resurrect it"
    )

    # Retry backoff + active claim: every claim field carried forward verbatim.
    assert row_902["decision_action"] == "backoff"
    assert row_902["recovery_claim_token"] == "claim-abc"
    assert row_902["recovery_claim_trigger"] == "auto_retry"
    assert row_902["recovery_claim_until"] == 5000.0

    # Wait-for-operator quiescence carried forward exactly.
    assert row_903["decision_action"] == "wait_for_operator"
    assert row_903["quiescence_reason"] == "recovery_exhausted"
    assert row_903["wake_condition"] == "operator_retry"

    # Mixed multi-artifact: none of the three rows leaked a field from a
    # sibling (e.g. 903 never picked up 902's claim token).
    assert row_901["recovery_claim_token"] is None
    assert row_903["recovery_claim_token"] is None

    repository = RecoveryTransferRepository()
    await repository.initialize()
    for artifact_id in (901, 902, 903):
        # Every migrated row must actually be readable through the ONE
        # canonical current-state read path, not just present in the table.
        context = await repository.recovery_context(artifact_id)
        assert context["recovery_epoch"] == {901: 4, 902: 1, 903: 2}[artifact_id]


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path, monkeypatch):
    """Section 19 item 6: safe to run repeatedly -- a second bootstrap must
    not duplicate, error, or drift the already-migrated row."""
    from transfers import codec

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "migrate_idempotent.db")
    await database.init_db()

    async with get_db() as db:
        await db.execute(
            "INSERT INTO torrents(id,hash,name,status,source) VALUES(1,'h1','T1','queued','magnet')",
        )
        await db.execute(
            """INSERT INTO download_files(id,torrent_id,filename,status,recovery_failures,recovery_refreshes)
               VALUES(201,1,'legacy.bin','queued',5,0)""",
        )
        await db.execute(
            "INSERT INTO application_events(transfer_id,kind,detail,claimed) VALUES(1,?,?,1)",
            ("transfer_recovery:201", codec.dump({"recovery_epoch": 1, "consecutive_no_progress_failures": 5})),
        )
        await db.commit()

    await database.init_db()
    async with get_db() as db:
        first = await db.fetchone("SELECT * FROM artifact_recovery_state WHERE artifact_id=201")
        count_first = await db.fetchone(
            "SELECT COUNT(*) AS n FROM artifact_recovery_state WHERE artifact_id=201",
        )
    assert count_first["n"] == 1

    # A live mutation after the first migration run -- proves the SECOND
    # bootstrap does not clobber it back to the stale legacy snapshot.
    repository = RecoveryTransferRepository()
    await repository.initialize()
    await repository.record_recovery_decision(201, "retry", "operator_requested")

    for _ in range(2):
        await database.init_db()

    async with get_db() as db:
        final = await db.fetchone("SELECT * FROM artifact_recovery_state WHERE artifact_id=201")
        count_final = await db.fetchone(
            "SELECT COUNT(*) AS n FROM artifact_recovery_state WHERE artifact_id=201",
        )
    assert count_final["n"] == 1
    assert final["decision_action"] == "retry"
    assert final["decision_reason"] == "operator_requested"
    assert first["artifact_id"] == final["artifact_id"] == 201


def _legacy_reconstruct(db_path, artifact_id: int) -> dict:
    """The pre-Section-14 reconstruction algorithm this leveling pass
    replaced (the historical shape of ``transfers.repository.
    TransferRepository._recovery_snapshot``), preserved here ONLY as a
    comparison oracle for the mandatory live-database safeguard below --
    never imported by, or reachable from, production code.
    """
    import json
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
            (artifact_id,),
        ).fetchone()
        snapshot = {
            "version": 2, "recovery_epoch": 0, "progress_anchor": None,
            "consecutive_no_progress_failures": row["recovery_failures"] or 0,
            "failures_since_meaningful_progress": row["recovery_failures"] or 0,
            "failure_signature": None, "same_signature_failures": 0,
            "candidate_refreshes": row["recovery_refreshes"] or 0,
            "candidate_switches": 0, "decision_action": None, "decision_reason": None,
            "quiescence_reason": None, "wake_condition": None, "candidate_attempt_history": [],
        }
        event = conn.execute(
            "SELECT detail FROM application_events WHERE kind=? ORDER BY id DESC LIMIT 1",
            (f"transfer_recovery:{artifact_id}",),
        ).fetchone()
        if event and event["detail"]:
            stored = json.loads(event["detail"])
            if isinstance(stored, dict):
                for key in snapshot:
                    if key in stored:
                        snapshot[key] = stored[key]
        snapshot["consecutive_no_progress_failures"] = max(
            snapshot["consecutive_no_progress_failures"], row["recovery_failures"] or 0,
        )
        snapshot["candidate_refreshes"] = max(
            snapshot["candidate_refreshes"], row["recovery_refreshes"] or 0,
        )
        return snapshot
    finally:
        conn.close()


def _sqlite_backup(source_path, target_path) -> None:
    """A real SQLite-consistent backup (the same ``.backup()`` API
    ``db/migrations/v112.py::_backup`` uses), not a raw file copy -- WAL mode
    means the live database's true state can span a ``-wal`` sidecar file, so
    a byte-copy of only the main file is not a reliable snapshot."""
    import sqlite3

    with sqlite3.connect(source_path) as source, sqlite3.connect(target_path) as target:
        source.backup(target)


@pytest.mark.asyncio
async def test_migration_then_restored_backup_reads_identically_to_never_migrated(tmp_path, monkeypatch):
    """Mandatory Phase 3 live-database safeguard (overlay item 2): a database
    migrated by this phase's schema change, then rolled back to a pre-
    migration backup, must produce recovery-state behavior identical to what
    it would have produced had the migration never run -- not merely that
    the backup restores structurally, but that the OLD reconstruction logic
    (reading application_events) still functions correctly against a
    database that was briefly migrated and then restored.
    """
    db_path = tmp_path / "safeguard.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()

    async with get_db() as db:
        await db.execute(
            "INSERT INTO torrents(id,hash,name,status,source) VALUES(1,'h1','T1','queued','magnet')",
        )
        await db.execute(
            """INSERT INTO download_files(id,torrent_id,filename,status,recovery_failures,recovery_refreshes)
               VALUES(301,1,'pre-existing.bin','queued',4,1)""",
        )
        legacy_snapshot = {
            "version": 2, "recovery_epoch": 3, "progress_anchor": 2048,
            "consecutive_no_progress_failures": 4, "same_signature_failures": 2,
            "failure_signature": "network:remote_read_failed",
            "candidate_refreshes": 1, "candidate_switches": 2,
            "decision_action": "try_alternate_candidate", "decision_reason": "budget_exhausted",
            "quiescence_reason": None, "wake_condition": None,
            "candidate_attempt_history": ["cand-x", "cand-y"],
        }
        from transfers import codec
        await db.execute(
            "INSERT INTO application_events(transfer_id,kind,detail,claimed) VALUES(1,?,?,1)",
            ("transfer_recovery:301", codec.dump(legacy_snapshot)),
        )
        # A genuinely pre-Section-14 database never had this table at all;
        # dropping the empty one this bootstrap already created simulates
        # that starting condition precisely.
        await db.execute("DROP TABLE IF EXISTS artifact_recovery_state")
        await db.commit()

    before_migration = _legacy_reconstruct(db_path, 301)
    assert before_migration["recovery_epoch"] == 3
    assert before_migration["candidate_attempt_history"] == ["cand-x", "cand-y"]

    backup_path = tmp_path / "safeguard.db.pre-migration-backup"
    _sqlite_backup(db_path, backup_path)

    # The migration itself: re-running the bootstrap recreates
    # artifact_recovery_state and backfills it for artifact 301.
    await database.init_db()
    async with get_db() as db:
        migrated = await db.fetchone("SELECT * FROM artifact_recovery_state WHERE artifact_id=301")
    assert migrated is not None
    assert migrated["recovery_epoch"] == 3

    # Roll back to the pre-migration backup.
    _sqlite_backup(backup_path, db_path)

    after_restore = _legacy_reconstruct(db_path, 301)
    assert after_restore == before_migration, (
        "the pre-leveling reconstruction algorithm must read a restored "
        "pre-migration backup identically to how it would have read the "
        "database had this leveling pass's migration never run"
    )

    # The restore is structural, not just logical: the migrated table is
    # genuinely gone again in the restored file, not merely logically stale.
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "artifact_recovery_state" not in tables


@pytest.mark.asyncio
async def test_sub_threshold_progress_causes_zero_recovery_state_writes(runtime, monkeypatch):
    """Section 18/20 (post-review correction): NOT merely bounded growth --
    ordinary, non-semantic progress advancement must write to
    artifact_recovery_state exactly zero times. The only two write
    occasions for one artifact's recovery lifecycle are (a) the one-time
    anchor initialization on the very first observation, and (b) each actual
    meaningful-progress crossing -- both O(epochs), never O(polls). This is
    a direct, unambiguous proof: it hooks the raw write path itself and
    asserts it is never invoked for sub-threshold observations, rather than
    inferring "no write happened" from the row count staying flat.
    """
    repository, _registry, _provider, executor, _engine, _now = runtime
    transfer, artifact = await start_transfer(runtime)
    handle = artifact.execution

    async with get_db() as db:
        before = await db.fetchone(
            "SELECT updated_at FROM artifact_recovery_state WHERE artifact_id=?", (artifact.id,),
        )
    assert before is not None, "the one-time initialization write must already have happened by dispatch"

    calls = []
    original = RecoveryTransferRepository._save_recovery_snapshot.__func__

    async def counting_save(cls, db, transfer_id, artifact_id, snapshot):
        calls.append(artifact_id)
        return await original(cls, db, transfer_id, artifact_id, snapshot)

    monkeypatch.setattr(RecoveryTransferRepository, "_save_recovery_snapshot", classmethod(counting_save))
    for completed in (2, 3):
        current = executor.jobs[handle.attempt_id]
        updated = replace(current, progress=TransferProgress(4, completed, 1))
        executor.jobs[handle.attempt_id] = updated
        await repository.execution(updated)

    assert calls == [], (
        f"expected zero artifact_recovery_state writes for sub-threshold progress, got {calls}"
    )


@pytest.mark.asyncio
async def test_recovery_snapshot_never_reads_application_events_as_current_state(runtime):
    """Base document Section 35's architecture assertion, direct form: current
    recovery policy must no longer query ``application_events`` as its
    current-state store. This hooks the real SQLite cursor's ``execute`` and
    asserts no SQL text issued by ``_recovery_snapshot`` -- the ONE canonical
    current-state read (Section 14) -- ever names ``application_events``. A
    behavioral
    write-side proof already exists above
    (``test_progress_observations_do_not_append_full_recovery_snapshot_each_tick``,
    ``test_sub_threshold_progress_causes_zero_recovery_state_writes``); this is
    the matching read-side proof."""
    repository, _registry, _provider, _executor, _engine, _now = runtime
    _transfer, artifact = await start_transfer(runtime)

    queries = []
    async with get_db() as db:
        # ``fetchone``/``fetchall`` (what ``_recovery_snapshot`` actually
        # calls) issue SQL via the wrapped raw aiosqlite connection directly,
        # not through ``_DbConnection.execute`` -- spy at that layer so every
        # query text is actually observed.
        real_execute = db._raw.execute

        async def spying_execute(sql, *args, **kwargs):
            queries.append(sql)
            return await real_execute(sql, *args, **kwargs)

        db._raw.execute = spying_execute
        await repository._recovery_snapshot(db, artifact.id)

    assert queries, "the spy must have observed at least one query to be a meaningful assertion"
    offenders = [sql for sql in queries if "application_events" in sql]
    assert offenders == [], (
        f"_recovery_snapshot must never query application_events as current state, got: {offenders}"
    )


@pytest.mark.asyncio
async def test_scheduler_execution_cycle_does_not_grow_recovery_state_or_history_with_several_active_artifacts(tmp_path, monkeypatch):
    """Base document Section 37's required scheduler-cycle performance
    evidence, with SEVERAL genuinely active artifacts at once (not one
    artifact in isolation, unlike the unit-level tests above). Runs against
    the real production stack (Section 32) with low capacity so both
    dispatched and capacity-queued artifacts are exercised in the same
    cycle.

    Key Section 37 acceptance criterion: "ordinary progress no longer grows
    recovery state/history linearly with polling frequency." This proves it
    at the scheduler-cycle level: across repeated ordinary (sub-threshold,
    no state transition) ``reconcile_executions()`` ticks over 5 live
    executions, ``artifact_recovery_state`` stays at exactly one row per
    artifact (never appending), ``application_events`` gains zero new
    ``recovery_audit`` rows, and the per-cycle SQLite acquisition count is
    IDENTICAL between two consecutive steady-state cycles -- it does not
    grow with elapsed polling count."""
    from dataclasses import replace as _replace

    from production_stack_harness import build_production_runtime

    repository, _registry, _providers, executor, engine, _now_box = await build_production_runtime(
        tmp_path, monkeypatch, max_active_executions=5, provider_ids=("p1",),
    )

    transfers = []
    for i in range(8):
        transfers.append(await engine.submit((TransferRequest("parcel", f"box{i}", name=f"payload{i}.bin"),)))
    await engine.resolve_pending()
    await engine.reconcile_executions()  # initial dispatch: 5 active, 3 capacity-queued

    live_count = len(executor.jobs)
    assert live_count == 5, "the low-capacity fixture must actually leave some artifacts capacity-queued"

    async with get_db() as db:
        rows_before = await db.fetchone("SELECT COUNT(*) AS n FROM artifact_recovery_state")
        audit_before = await db.fetchone(
            "SELECT COUNT(*) AS n FROM application_events WHERE kind='recovery_audit'",
        )

    # Two consecutive steady-state cycles: sub-threshold progress advances on
    # every live execution, no failures, no meaningful-progress crossings.
    acquisitions_per_cycle = []
    for _ in range(2):
        for artifact_id, job in list(executor.jobs.items()):
            executor.jobs[artifact_id] = _replace(job, progress=TransferProgress(4, 2, 1))
        before = database.db_runtime_metrics()["sqlite_acquires"]
        await engine.reconcile_executions()
        after = database.db_runtime_metrics()["sqlite_acquires"]
        acquisitions_per_cycle.append(after - before)

    async with get_db() as db:
        rows_after = await db.fetchone("SELECT COUNT(*) AS n FROM artifact_recovery_state")
        audit_after = await db.fetchone(
            "SELECT COUNT(*) AS n FROM application_events WHERE kind='recovery_audit'",
        )

    assert rows_after["n"] == rows_before["n"], (
        "artifact_recovery_state must not grow across ordinary steady-state "
        f"scheduler cycles with {live_count} live executions "
        f"(before={rows_before['n']}, after={rows_after['n']})"
    )
    assert audit_after["n"] == audit_before["n"], (
        "no new recovery_audit rows may be written for ordinary sub-threshold "
        f"progress across a real scheduler cycle (before={audit_before['n']}, after={audit_after['n']})"
    )
    assert acquisitions_per_cycle[0] == acquisitions_per_cycle[1], (
        "the SQLite acquisition cost of one ordinary steady-state scheduler "
        f"cycle must not grow with elapsed polling count, got {acquisitions_per_cycle}"
    )
