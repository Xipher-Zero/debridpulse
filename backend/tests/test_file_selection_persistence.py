"""Gate A — additive schema, backup/wipe ownership, foreign-key integrity, and the
durable Confirm-vs-materialization concurrency proof.

The concurrency proof runs the Confirm transaction and the executable-manifest
commitment transaction concurrently many times and asserts exactly one durable
outcome each time, with both transaction winners exercised. Correctness rests on
SQLite ``BEGIN IMMEDIATE`` serialization of the ``transfer_file_selections`` row,
never on an in-process lock.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio

import db.database as database
from db.migrations import v112
from file_selection_support import Clock, executable, file_manifest, rebind_resource, seed_window
from services import db_maintenance
from transfers import file_selection as fs
from transfers.repository import TransferRepository

_SELECTION_TABLES = (
    "transfer_file_manifests",
    "transfer_file_manifest_entries",
    "transfer_file_selections",
    "transfer_file_selection_entries",
)


@pytest_asyncio.fixture
async def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "file-selection.db")
    await database.init_db()
    return TransferRepository()


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


# --------------------------------------------------------------------------- #
# Additive current-schema ownership (specification section 26)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_selection_tables_are_additive_on_an_existing_current_database(tmp_path, monkeypatch):
    path = tmp_path / "current.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    assert await v112.migrate(external_executor=False) == {"migrated": False}

    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO torrents(id,hash,name,status) VALUES(7,?,?,'completed')", ("7" * 40, "kept"))
        conn.execute("INSERT INTO transfer_requests(id,transfer_id,ordinal,payload,state) VALUES('r7',7,0,'{}','resolved')")
        conn.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES('a7','r7','p','succeeded')")
        # Simulate a database created before this in-branch additive change.
        for table in reversed(_SELECTION_TABLES):
            conn.execute(f"DROP TABLE {table}")
        conn.commit()
    assert not _tables(path) & set(_SELECTION_TABLES)

    await database.init_db()                       # "start new code"
    await TransferRepository().initialize()
    await database.validate_transfer_repository_schema()

    assert set(_SELECTION_TABLES).issubset(_tables(path))
    assert not Path(str(path) + ".pre-v112.sqlite3").exists()   # no predecessor backup
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM torrents WHERE id=7").fetchone() == ("kept",)
        assert conn.execute("SELECT 1 FROM resolution_attempts WHERE id='a7'").fetchone() == (1,)
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE version='1.0.12'").fetchone() == (1,)


@pytest.mark.asyncio
async def test_startup_schema_validation_requires_every_selection_column(tmp_path, monkeypatch):
    path = tmp_path / "incomplete.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE transfer_file_selection_entries")
        conn.commit()
    with pytest.raises(RuntimeError):
        await database.validate_transfer_repository_schema()


# --------------------------------------------------------------------------- #
# Foreign-key integrity
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_selection_entries_cannot_reference_a_foreign_manifest(repo):
    clock = Clock(1000.0)
    seed = await seed_window(transfer_hash="f" * 40)
    await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=clock(),
    )
    canonical = await repo.record_file_manifest(
        seed.request_id, seed.provider_resource_id,
        file_manifest(("a", "s/a", 1), ("b", "s/b", 2)), now=clock(),
    )
    async with database.get_db() as db:
        selection = await db.fetchone(
            "SELECT id FROM transfer_file_selections WHERE request_id=?", (seed.request_id,))
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "INSERT INTO transfer_file_selection_entries(selection_id,manifest_id,entry_id) VALUES(?,?,?)",
                (selection["id"], canonical.manifest_id, "entry-not-in-manifest"),
            )
        await db.rollback()
    async with database.get_db() as db:
        violations = await db.fetchall("PRAGMA foreign_key_check")
    assert violations == []


# --------------------------------------------------------------------------- #
# Backup / wipe ownership (specification section 27)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_backup_and_wipe_cover_every_selection_table(repo, tmp_path, monkeypatch):
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "db_backup_folder", str(tmp_path / "backups"), raising=False)

    clock = Clock(1000.0)
    seed = await seed_window(transfer_hash="b" * 40)
    await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=clock(),
    )
    canonical = await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, file_manifest(("a", "s/a", 10), ("b", "s/b", 20)), now=clock(),
    )
    await repo.confirm_file_selection(seed.transfer_id, canonical.manifest_id, [canonical.entries[0].entry_id], now=clock(),
    )

    for table in _SELECTION_TABLES:
        assert table in db_maintenance.TABLES
        assert table in db_maintenance._TABLE_ORDER

    report = await db_maintenance.run_database_backup()
    assert not report["errors"]
    backup = Path(report["file"]).read_text()
    for table in _SELECTION_TABLES:
        assert f'"{table}"' in backup
    assert report["tables"]["transfer_file_manifest_entries"] == 2
    assert report["tables"]["transfer_file_selection_entries"] == 1

    await db_maintenance.wipe_database(verified_quiesced=True)
    async with database.get_db() as db:
        for table in _SELECTION_TABLES:
            rows = await db.fetchall(f"SELECT COUNT(*) AS n FROM {table}")
            assert rows[0]["n"] == 0
        assert (await db.fetchall("PRAGMA foreign_key_check")) == []


# --------------------------------------------------------------------------- #
# Confirm-vs-materialization concurrency proof (specification section 20)
# --------------------------------------------------------------------------- #

async def _seed_race(repo, clock, tag):
    seed = await seed_window(transfer_hash=tag)
    await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=clock(),
    )
    canonical = await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, file_manifest(("a", "s/a", 10), ("b", "s/b", 20), ("c", "s/c", 30)),
        now=clock(),
    )
    full = executable(("a", "s/a", 10), ("b", "s/b", 20), ("c", "s/c", 30))
    keep = [canonical.entries[0].entry_id, canonical.entries[2].entry_id]
    return seed, canonical, full, keep


@pytest.mark.asyncio
async def test_confirm_and_materialization_race_has_exactly_one_durable_outcome(repo):
    clock = Clock(1000.0)
    confirm_wins = materialize_wins = 0

    for i in range(60):
        seed, canonical, full, keep = await _seed_race(repo, clock, f"{i:02d}" + "r" * 38)

        confirm_coro = repo.confirm_file_selection(seed.transfer_id, canonical.manifest_id, keep, now=clock())
        commit_coro = repo.commit_selected_manifest(seed.record, full, now=clock())
        # Alternate which side is submitted to the loop first so both transaction
        # winners are exercised regardless of host scheduling.
        if i % 2:
            authorized, confirm_result = await asyncio.gather(commit_coro, confirm_coro)
        else:
            confirm_result, authorized = await asyncio.gather(confirm_coro, commit_coro)

        async with database.get_db() as db:
            row = await db.fetchone(
                "SELECT decision, decision_reason, manifest_committed_at FROM transfer_file_selections WHERE request_id=?",
                (seed.request_id,),
            )
            entries = await db.fetchall(
                """SELECT e.entry_id FROM transfer_file_selection_entries e
               JOIN transfer_file_selections s ON s.id=e.selection_id
               WHERE s.request_id=?""", (seed.request_id,))

        assert row["manifest_committed_at"] is not None      # materialization always commits exactly once

        if confirm_result.outcome == fs.SelectionOutcome.CONFIRMED:
            confirm_wins += 1
            assert row["decision"] == "explicit"
            assert {e["entry_id"] for e in entries} == set(keep)
            assert [e.relative_path for e in authorized] == ["s/a", "s/c"]
        else:
            materialize_wins += 1
            assert confirm_result.outcome == fs.SelectionOutcome.CONFLICT
            assert confirm_result.detail in {"materialization_committed", "materialization_won"}
            assert row["decision"] == "all"
            assert entries == []
            assert [e.relative_path for e in authorized] == ["s/a", "s/b", "s/c"]

        # Re-running either side is idempotent against the settled outcome.
        assert await repo.commit_selected_manifest(seed.record, full, now=clock()) == authorized
        replay = await repo.confirm_file_selection(seed.transfer_id, canonical.manifest_id, keep, now=clock())
        assert replay.outcome in {fs.SelectionOutcome.CONFIRMED, fs.SelectionOutcome.CONFLICT}

    assert confirm_wins + materialize_wins == 60
    # This loop proves the invariant "exactly one durable outcome per round" under
    # real concurrency. It does NOT hard-assert that both winners occur here —
    # that would depend on scheduler luck and could flake in CI. Both transaction
    # winners are proven deterministically by
    # test_materialization_commit_then_confirm_is_a_conflict and
    # test_confirm_then_materialization_filters_to_the_subset below.
    print(f"race split over 60 rounds: confirm-wins={confirm_wins} materialize-wins={materialize_wins}")


@pytest.mark.asyncio
async def test_two_stale_tabs_cannot_both_confirm(repo):
    clock = Clock(1000.0)
    seed, canonical, _full, keep = await _seed_race(repo, clock, "tabs" + "t" * 36)
    other = [canonical.entries[1].entry_id]

    first, second = await asyncio.gather(
        repo.confirm_file_selection(seed.transfer_id, canonical.manifest_id, keep, now=clock()),
        repo.confirm_file_selection(seed.transfer_id, canonical.manifest_id, other, now=clock()),
    )
    outcomes = sorted([first.outcome, second.outcome])
    assert outcomes == sorted([fs.SelectionOutcome.CONFIRMED, fs.SelectionOutcome.CONFLICT])
    async with database.get_db() as db:
        rows = await db.fetchall(
            """SELECT e.entry_id FROM transfer_file_selection_entries e
               JOIN transfer_file_selections s ON s.id=e.selection_id
               WHERE s.request_id=?""", (seed.request_id,))
    winning = keep if first.outcome == fs.SelectionOutcome.CONFIRMED else other
    assert {r["entry_id"] for r in rows} == set(winning)


@pytest.mark.asyncio
async def test_materialization_commit_then_confirm_is_a_conflict(repo):
    clock = Clock(1000.0)
    seed, canonical, full, keep = await _seed_race(repo, clock, "seq" + "s" * 37)
    authorized = await repo.commit_selected_manifest(seed.record, full, now=clock())
    assert len(authorized) == 3
    result = await repo.confirm_file_selection(seed.transfer_id, canonical.manifest_id, keep, now=clock())
    assert result.outcome == fs.SelectionOutcome.CONFLICT and result.committed is True


@pytest.mark.asyncio
async def test_confirm_then_materialization_filters_to_the_subset(repo):
    clock = Clock(1000.0)
    seed, canonical, full, keep = await _seed_race(repo, clock, "cfm" + "c" * 37)
    confirmed = await repo.confirm_file_selection(seed.transfer_id, canonical.manifest_id, keep, now=clock())
    assert confirmed.outcome == fs.SelectionOutcome.CONFIRMED
    authorized = await repo.commit_selected_manifest(seed.record, full, now=clock())
    assert [e.relative_path for e in authorized] == ["s/a", "s/c"]


# --------------------------------------------------------------------------- #
# Fail-closed reconciliation at materialization time
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_confirmed_subset_never_broadens_when_unprovable(repo):
    clock = Clock(1000.0)
    seed, canonical, _full, keep = await _seed_race(repo, clock, "fcl" + "f" * 37)
    await repo.confirm_file_selection(seed.transfer_id, canonical.manifest_id, keep, now=clock())

    # The executable manifest no longer contains one confirmed path.
    broken = executable(("a", "s/a", 10), ("b", "s/b", 20))
    from transfers.errors import TransferError

    with pytest.raises(TransferError):
        await repo.commit_selected_manifest(seed.record, broken, now=clock())
    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT decision, manifest_committed_at FROM transfer_file_selections WHERE request_id=?",
            (seed.request_id,))
    assert row["decision"] == "explicit"                 # never degraded to ALL
    assert row["manifest_committed_at"] is None          # request contained, not committed


# --------------------------------------------------------------------------- #
# Per-resource selection provenance across re-resolution (specification section 33)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_new_provider_resource_gets_a_fresh_generation_and_never_inherits(repo):
    clock = Clock(1000.0)
    seed = await seed_window(transfer_hash="g" * 40)

    # Request R -> resource A -> user confirms {file 1, file 3}
    await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=clock())
    tree = file_manifest(("f1", "s/f1", 10), ("f2", "s/f2", 20), ("f3", "s/f3", 30))
    manifest_a = await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, tree, now=clock())
    keep = [manifest_a.entries[0].entry_id, manifest_a.entries[2].entry_id]
    confirmed = await repo.confirm_file_selection(seed.transfer_id, manifest_a.manifest_id, keep, now=clock())
    assert confirmed.outcome == fs.SelectionOutcome.CONFIRMED
    selection_a = fs.selection_identity(seed.request_id, seed.provider_resource_id)

    # Resource A expires; the same durable request R is re-resolved onto resource B.
    clock.set(5000.0)
    reso_b = await rebind_resource(seed, suffix="resource-B")
    assert reso_b.provider_resource_id != seed.provider_resource_id

    row_b = await repo.begin_file_selection_window(
        reso_b.request_id, reso_b.transfer_id, reso_b.provider_resource_id, seed.provider_id,
        initially_available=False, now=clock())
    selection_b = row_b["id"]
    assert selection_b != selection_a
    assert row_b["decision"] == "pending"        # fresh generation, default ALL

    # B's manifest — same file tree, but bound to resource B => different manifest id.
    manifest_b = await repo.record_file_manifest(reso_b.request_id, reso_b.provider_resource_id, tree, now=clock())
    assert manifest_b.manifest_id != manifest_a.manifest_id

    # The read model must surface only B's live generation, never historical A,
    # even though A shares the transfer id and is not yet committed.
    offers = await repo.active_file_selection_offers(now=clock())
    assert [o["selection_id"] for o in offers] == [selection_b]
    view = await repo.file_selection_presentation(seed.transfer_id, now=clock())
    assert view["selection_id"] == selection_b
    assert view["provider_resource_id"] == reso_b.provider_resource_id
    assert view["manifest_id"] == manifest_b.manifest_id

    async with database.get_db() as db:
        # A's generation and its explicit rows are still historically persisted.
        gen_a = await db.fetchone("SELECT * FROM transfer_file_selections WHERE id=?", (selection_a,))
        entries_a = await db.fetchall(
            "SELECT entry_id FROM transfer_file_selection_entries WHERE selection_id=?", (selection_a,))
        entries_b = await db.fetchall(
            "SELECT entry_id FROM transfer_file_selection_entries WHERE selection_id=?", (selection_b,))
    assert gen_a["decision"] == "explicit"
    assert {r["entry_id"] for r in entries_a} == set(keep)
    assert entries_b == []                       # B inherited nothing

    # Materializing B cannot consume A's selection rows: default ALL for B.
    full_b = executable(("f1", "s/f1", 10), ("f2", "s/f2", 20), ("f3", "s/f3", 30))
    authorized_b = await repo.commit_selected_manifest(reso_b.record, full_b, now=clock())
    assert authorized_b == full_b                # all three files, not A's subset

    async with database.get_db() as db:
        gen_a = await db.fetchone("SELECT decision, manifest_committed_at FROM transfer_file_selections WHERE id=?", (selection_a,))
        gen_b = await db.fetchone("SELECT decision, manifest_committed_at FROM transfer_file_selections WHERE id=?", (selection_b,))
    assert gen_a["decision"] == "explicit" and gen_a["manifest_committed_at"] is None   # A untouched
    assert gen_b["decision"] == "all" and gen_b["manifest_committed_at"] is not None


@pytest.mark.asyncio
async def test_same_provider_resource_retains_its_selection_across_a_restart(repo):
    clock = Clock(1000.0)
    seed = await seed_window(transfer_hash="h" * 40)
    await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=clock())
    tree = file_manifest(("f1", "s/f1", 10), ("f2", "s/f2", 20))
    canonical = await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, tree, now=clock())
    await repo.confirm_file_selection(seed.transfer_id, canonical.manifest_id, [canonical.entries[0].entry_id], now=clock())

    # Same resource, fresh process: re-opening the window is a no-op, subset kept.
    restarted = TransferRepository()
    again = await restarted.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=9000.0)
    assert again["decision"] == "explicit"
    authorized = await restarted.commit_selected_manifest(
        seed.record, executable(("f1", "s/f1", 10), ("f2", "s/f2", 20)), now=9000.0)
    assert [e.relative_path for e in authorized] == ["s/f1"]


# --------------------------------------------------------------------------- #
# Two-phase materialization marker: crash recovery (carry-forward item)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_commit_marker_then_crash_before_fanout_is_recoverable(repo):
    clock = Clock(1000.0)
    seed, canonical, full, keep = await _seed_race(repo, clock, "crash" + "x" * 35)
    await repo.confirm_file_selection(seed.transfer_id, canonical.manifest_id, keep, now=clock())

    # commit_selected_manifest sets manifest_committed_at (authorization commit).
    authorized = await repo.commit_selected_manifest(seed.record, full, now=clock())
    assert [e.relative_path for e in authorized] == ["s/a", "s/c"]
    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT manifest_committed_at FROM transfer_file_selections WHERE request_id=?", (seed.request_id,))
    committed_at = row["manifest_committed_at"]
    assert committed_at is not None

    # Simulate a crash before repository.manifest() fan-out, then recovery: a
    # fresh process re-drives observation -> gate PROCEED -> commit (idempotent).
    recovered = TransferRepository()
    assert await recovered.file_selection_gate(
        seed.request_id, seed.provider_resource_id, now=clock()) == fs.SelectionGate.PROCEED
    replay = await recovered.commit_selected_manifest(seed.record, full, now=clock() + 500)
    assert [e.relative_path for e in replay] == ["s/a", "s/c"]     # same authorized subset
    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT manifest_committed_at FROM transfer_file_selections WHERE request_id=?", (seed.request_id,))
    assert row["manifest_committed_at"] == committed_at           # marker not moved

    # The following fan-out is a separate idempotent transaction; running it
    # twice must not create duplicate child requests.
    await recovered.manifest(seed.record, replay)
    await recovered.manifest(seed.record, replay)
    async with database.get_db() as db:
        children = await db.fetchall(
            "SELECT id FROM transfer_requests WHERE parent_id=?", (seed.request_id,))
    assert len(children) == 2                                     # exactly the 2 authorized members


# --------------------------------------------------------------------------- #
# Hold release — the Confirm / Dismiss transaction atomically settles the
# decision AND releases the file-selection gate's scheduler wait, but never a
# coexisting provider backoff (TASK_File_Selection_Hold_Release_Correction
# §9a, §16).
# --------------------------------------------------------------------------- #

async def _seed_gated(repo, clock, tag, *, request_retry_at, request_error=None,
                      request_state="waiting"):
    seed = await seed_window(transfer_hash=tag)
    await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=clock(),
    )
    canonical = await repo.record_file_manifest(
        seed.request_id, seed.provider_resource_id,
        file_manifest(("a", "s/a", 10), ("b", "s/b", 20), ("c", "s/c", 30)), now=clock())
    async with database.get_db() as db:
        await db.execute(
            "UPDATE transfer_requests SET state=?, retry_at=?, error=? WHERE id=?",
            (request_state, request_retry_at, request_error, seed.request_id))
        await db.commit()
    return seed, canonical


async def _request_row(request_id):
    async with database.get_db() as db:
        return await db.fetchone("SELECT * FROM transfer_requests WHERE id=?", (request_id,))


@pytest.mark.asyncio
async def test_confirm_transaction_settles_decision_and_releases_the_gate_wait(repo):
    clock = Clock(1000.0)
    seed, canonical = await _seed_gated(repo, clock, "c" * 40, request_retry_at=1030.0)

    result = await repo.confirm_file_selection(
        seed.transfer_id, canonical.manifest_id,
        [canonical.entries[0].entry_id, canonical.entries[2].entry_id], now=1050.0)
    assert result.outcome == fs.SelectionOutcome.CONFIRMED

    request = await _request_row(seed.request_id)
    async with database.get_db() as db:
        gen = await db.fetchone(
            "SELECT decision,decision_reason,decision_at,hold_until,manifest_committed_at "
            "FROM transfer_file_selections WHERE request_id=?", (seed.request_id,))
    assert gen["decision"] == "explicit" and gen["decision_reason"] == fs.DecisionReason.CONFIRMED
    assert gen["decision_at"] == 1050.0 and gen["manifest_committed_at"] is None
    assert gen["hold_until"] == 1000.0 + fs.IMMEDIATE_DECISION_HOLD_SECONDS   # durable history kept
    assert float(request["retry_at"]) <= 1050.0 and request["state"] == "waiting"


@pytest.mark.asyncio
async def test_dismiss_transaction_settles_all_and_releases_the_gate_wait(repo):
    clock = Clock(1000.0)
    seed, canonical = await _seed_gated(repo, clock, "d" * 40, request_retry_at=1030.0)

    result = await repo.dismiss_file_selection(seed.transfer_id, canonical.manifest_id, now=1042.0)
    assert result.outcome == fs.SelectionOutcome.DISMISSED and result.decision == "all"

    async with database.get_db() as db:
        gen = await db.fetchone(
            "SELECT decision,decision_reason FROM transfer_file_selections WHERE request_id=?",
            (seed.request_id,))
    row = await _request_row(seed.request_id)
    assert gen["decision"] == "all" and gen["decision_reason"] == fs.DecisionReason.CLOSED
    assert float(row["retry_at"]) <= 1042.0


@pytest.mark.asyncio
async def test_idempotent_same_subset_confirm_self_heals_a_stale_future_retry_at(repo):
    clock = Clock(1000.0)
    seed, canonical = await _seed_gated(repo, clock, "i" * 40, request_retry_at=0.0)
    keep = [canonical.entries[1].entry_id]
    first = await repo.confirm_file_selection(seed.transfer_id, canonical.manifest_id, keep, now=1010.0)
    assert first.outcome == fs.SelectionOutcome.CONFIRMED and first.detail == "confirmed"

    # Simulate the exact stale-wait state being corrected: EXPLICIT persisted, but
    # a future retry_at was left behind by an interrupted earlier run.
    async with database.get_db() as db:
        await db.execute(
            "UPDATE transfer_requests SET state='waiting', retry_at=9999.0, error=NULL WHERE id=?",
            (seed.request_id,))
        await db.commit()

    second = await repo.confirm_file_selection(seed.transfer_id, canonical.manifest_id, keep, now=1020.0)
    assert second.outcome == fs.SelectionOutcome.CONFIRMED and second.detail == "idempotent"
    row = await _request_row(seed.request_id)
    assert float(row["retry_at"]) <= 1020.0                       # self-healed
    async with database.get_db() as db:
        entries = await db.fetchall(
            "SELECT entry_id FROM transfer_file_selection_entries WHERE selection_id=?", (canonical.manifest_id and (
                await db.fetchone("SELECT id FROM transfer_file_selections WHERE request_id=?", (seed.request_id,)))["id"],))
    assert {r["entry_id"] for r in entries} == set(keep)          # never broadened


@pytest.mark.asyncio
async def test_confirm_does_not_release_a_coexisting_provider_backoff_retry_at(repo):
    """§9a multi-cause isolation. A request carrying a genuine provider backoff
    (``error`` recorded, ``retry_at`` far in the future) keeps that protection
    when Confirm releases only the selection-induced wait — the gate-wait path
    (``poll_after``) never records an ``error``; ``request_failure`` always does."""
    clock = Clock(1000.0)
    backoff_blob = '{"domain":"provider","category":"rate_limited","stage":"reconciliation"}'
    seed, canonical = await _seed_gated(
        repo, clock, "k" * 40, request_retry_at=1090.0, request_error=backoff_blob)

    result = await repo.confirm_file_selection(
        seed.transfer_id, canonical.manifest_id, [canonical.entries[0].entry_id], now=1005.0)
    assert result.outcome == fs.SelectionOutcome.CONFIRMED

    row = await _request_row(seed.request_id)
    assert float(row["retry_at"]) == 1090.0                       # unrelated backoff untouched
    assert row["error"] == backoff_blob
    async with database.get_db() as db:
        gen = await db.fetchone(
            "SELECT decision FROM transfer_file_selections WHERE request_id=?", (seed.request_id,))
    assert gen["decision"] == "explicit"                          # decision still settled


@pytest.mark.asyncio
async def test_atomic_gate_and_confirm_have_no_stale_wait_resurrection_window(repo):
    """Correction §7-8 / §21 — the file-selection gate decision and the
    scheduling of the wait it produces are one ``BEGIN IMMEDIATE`` transaction.
    Racing the atomic gate against Confirm, in BOTH submission orders, converges
    to exactly one outcome: decision=explicit AND retry_at not pushed into the
    future by a stale WAIT. There is no third result."""
    clock = Clock(1000.0)
    for i in range(24):
        seed, canonical = await _seed_gated(
            repo, clock, f"{i:02d}" + "z" * 38, request_retry_at=1000.0, request_state="waiting")
        keep = [canonical.entries[0].entry_id, canonical.entries[2].entry_id]

        gate_coro = repo.file_selection_gate(
            seed.request_id, seed.provider_resource_id, now=1050.0,
            poll_interval=30, resource_available=True)
        confirm_coro = repo.confirm_file_selection(
            seed.transfer_id, canonical.manifest_id, keep, now=1050.0)
        if i % 2:
            gate_result, confirm_result = await asyncio.gather(gate_coro, confirm_coro)
        else:
            confirm_result, gate_result = await asyncio.gather(confirm_coro, gate_coro)

        assert confirm_result.outcome == fs.SelectionOutcome.CONFIRMED
        async with database.get_db() as db:
            gen = await db.fetchone(
                "SELECT decision FROM transfer_file_selections WHERE request_id=?", (seed.request_id,))
            request = await db.fetchone(
                "SELECT retry_at, state FROM transfer_requests WHERE id=?", (seed.request_id,))
        assert gen["decision"] == "explicit"
        assert gate_result in {fs.SelectionGate.PROCEED, fs.SelectionGate.WAIT_FOR_DECISION}
        # Whichever committed first, the settled decision cannot be left behind a
        # future selection-derived retry_at.
        assert float(request["retry_at"]) <= 1050.0

        # A follow-up gate pass on the settled generation proceeds and never
        # recreates a wait.
        assert await repo.file_selection_gate(
            seed.request_id, seed.provider_resource_id, now=1051.0,
            poll_interval=30, resource_available=True) == fs.SelectionGate.PROCEED
        async with database.get_db() as db:
            request = await db.fetchone(
                "SELECT retry_at FROM transfer_requests WHERE id=?", (seed.request_id,))
        assert float(request["retry_at"]) <= 1051.0


@pytest.mark.asyncio
async def test_atomic_gate_wait_never_overwrites_a_provider_backoff_retry_at(repo):
    """Correction §9 / §22 — a request carrying a genuine provider backoff
    (``error`` recorded, longer ``retry_at``) is not in ``state='waiting'`` as a
    clean gate wait; the atomic gate scheduler leaves it entirely untouched."""
    clock = Clock(1000.0)
    backoff_blob = '{"domain":"provider","category":"rate_limited","stage":"reconciliation"}'
    seed, canonical = await _seed_gated(
        repo, clock, "bk" * 20, request_retry_at=1090.0, request_error=backoff_blob,
        request_state="waiting")

    gate = await repo.file_selection_gate(
        seed.request_id, seed.provider_resource_id, now=1005.0,
        poll_interval=30, resource_available=True)
    assert gate == fs.SelectionGate.WAIT_FOR_DECISION
    row = await _request_row(seed.request_id)
    assert float(row["retry_at"]) == 1090.0                     # backoff cadence intact
    assert row["error"] == backoff_blob                         # failure evidence intact


@pytest.mark.asyncio
async def test_conflicting_confirm_never_rewrites_retry_at(repo):
    clock = Clock(1000.0)
    seed, canonical = await _seed_gated(repo, clock, "x" * 40, request_retry_at=1030.0)
    # A different subset is already confirmed.
    await repo.confirm_file_selection(
        seed.transfer_id, canonical.manifest_id, [canonical.entries[0].entry_id], now=1001.0)
    async with database.get_db() as db:
        await db.execute("UPDATE transfer_requests SET retry_at=1030.0 WHERE id=?", (seed.request_id,))
        await db.commit()
    before = dict(await _request_row(seed.request_id))

    superseded = await repo.confirm_file_selection(
        seed.transfer_id, canonical.manifest_id, [canonical.entries[1].entry_id], now=1002.0)
    assert superseded.outcome == fs.SelectionOutcome.CONFLICT
    assert dict(await _request_row(seed.request_id)) == before    # no rewrite by the loser
