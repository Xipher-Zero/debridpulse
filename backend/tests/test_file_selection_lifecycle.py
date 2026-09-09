"""Gate A — durable file-selection lifecycle under an injected fake clock.

Every window/hold assertion here advances a fake clock. No test waits a real
second, and restart is always a fresh repository reading the persisted absolute
deadline.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from file_selection_support import Clock, executable, file_manifest, seed_window
from transfers import file_selection as fs
from transfers.engine import TransferEngine
from transfers.errors import TransferError
from transfers.models import ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


@pytest_asyncio.fixture
async def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "file-selection.db")
    await database.init_db()
    return TransferRepository()


class RecordingExecutor(MemoryExecutor):
    def __init__(self, authorize):
        super().__init__(authorize)
        self.started = []

    async def start(self, request, handle):
        self.started.append(request)
        return await super().start(request, handle)


@pytest_asyncio.fixture
async def core(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fs-engine.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider(file_manifest=True)
    executor = RecordingExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    clock = Clock(1000.0)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(adoption_stability_seconds=0, resource_poll_interval=1,
                              retry_delay=0, resolution_retry_delay=0, max_active_executions=8),
        clock=clock,
    )
    await engine.initialize()
    from types import SimpleNamespace
    return SimpleNamespace(engine=engine, repository=repository, registry=registry,
                           provider=provider, executor=executor, clock=clock)


async def _engine_submit(core, *, payload="show"):
    return await core.engine.submit((TransferRequest("parcel", payload, name="show"),), deduplicate=False)


async def window(repo, clock, *, initially_available, tag="t"):
    seed = await seed_window(transfer_hash=tag * 40)
    row = await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=initially_available, now=clock(),
    )
    assert row is not None
    return seed


# --------------------------------------------------------------------------- #
# Default ALL
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_no_window_means_full_list_and_proceed(repo):
    seed = await seed_window(transfer_hash="n" * 40)
    entries = executable(("a", "a", 1), ("b", "b", 2))
    assert await repo.file_selection_gate(seed.request_id, seed.provider_resource_id, now=1000.0) == fs.SelectionGate.PROCEED
    assert await repo.commit_selected_manifest(seed.record, entries, now=1000.0) == entries


# --------------------------------------------------------------------------- #
# Cached / immediately-available lifecycle
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_cached_holds_for_manifest_then_times_out_to_all(repo):
    clock = Clock(1000.0)
    seed = await window(repo, clock, initially_available=True)
    assert await repo.file_selection_gate(seed.request_id, seed.provider_resource_id, now=clock()) == fs.SelectionGate.WAIT_FOR_MANIFEST
    clock.set(1060.0)
    assert await repo.file_selection_gate(seed.request_id, seed.provider_resource_id, now=clock()) == fs.SelectionGate.PROCEED
    view = await repo.file_selection_presentation(seed.transfer_id, now=clock())
    assert view["decision"] == "all" and view["decision_reason"] == fs.DecisionReason.MANIFEST_TIMEOUT


@pytest.mark.asyncio
async def test_cached_single_file_manifest_proceeds_as_all_without_a_hold(repo):
    clock = Clock(1000.0)
    seed = await window(repo, clock, initially_available=True)
    clock.advance(5)
    await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, file_manifest(("only", "only.bin", 9)), now=clock())
    assert await repo.file_selection_gate(seed.request_id, seed.provider_resource_id, now=clock()) == fs.SelectionGate.PROCEED
    view = await repo.file_selection_presentation(seed.transfer_id, now=clock())
    assert view["decision_reason"] == fs.DecisionReason.SINGLE_FILE
    assert view["decision_deadline"] is None


@pytest.mark.asyncio
async def test_cached_multi_file_manifest_opens_a_120s_hold_anchored_to_arrival(repo):
    clock = Clock(1000.0)
    seed = await window(repo, clock, initially_available=True)
    clock.set(1005.0)
    canonical = await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, file_manifest(("a", "s/a", 1), ("b", "s/b", 2), ("c", "s/c", 3)), now=clock(),
    )
    assert canonical is not None and canonical.file_count == 3
    view = await repo.file_selection_presentation(seed.transfer_id, now=clock())
    assert view["decision_deadline"] == 1005.0 + fs.IMMEDIATE_DECISION_HOLD_SECONDS
    assert view["auto_offer"] is True
    assert await repo.file_selection_gate(seed.request_id, seed.provider_resource_id, now=1100.0) == fs.SelectionGate.WAIT_FOR_DECISION


@pytest.mark.asyncio
async def test_cached_confirm_before_deadline_commits_explicit_subset(repo):
    clock = Clock(1000.0)
    seed = await window(repo, clock, initially_available=True)
    clock.set(1005.0)
    canonical = await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, file_manifest(("a", "s/a", 10), ("b", "s/b", 20), ("c", "s/c", 30)), now=clock(),
    )
    keep = [canonical.entries[0].entry_id, canonical.entries[2].entry_id]
    clock.set(1050.0)
    result = await repo.confirm_file_selection(seed.transfer_id, canonical.manifest_id, keep, now=clock(),
    )
    assert result.outcome == fs.SelectionOutcome.CONFIRMED
    assert await repo.file_selection_gate(seed.request_id, seed.provider_resource_id, now=clock()) == fs.SelectionGate.PROCEED
    full = executable(("a", "s/a", 10), ("b", "s/b", 20), ("c", "s/c", 30))
    authorized = await repo.commit_selected_manifest(seed.record, full, now=clock())
    assert [e.relative_path for e in authorized] == ["s/a", "s/c"]


@pytest.mark.asyncio
async def test_cached_close_releases_hold_and_all_wins(repo):
    clock = Clock(1000.0)
    seed = await window(repo, clock, initially_available=True)
    clock.set(1005.0)
    canonical = await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, file_manifest(("a", "s/a", 1), ("b", "s/b", 2)), now=clock(),
    )
    clock.set(1050.0)
    result = await repo.dismiss_file_selection(seed.transfer_id, canonical.manifest_id, now=clock(),
    )
    assert result.outcome == fs.SelectionOutcome.DISMISSED and result.decision == "all"
    view = await repo.file_selection_presentation(seed.transfer_id, now=clock())
    assert view["decision"] == "all" and view["decision_reason"] == fs.DecisionReason.CLOSED
    full = executable(("a", "s/a", 1), ("b", "s/b", 2))
    assert await repo.commit_selected_manifest(seed.record, full, now=clock()) == full


@pytest.mark.asyncio
async def test_cached_timeout_at_120s_settles_all_and_discards_no_draft(repo):
    clock = Clock(1000.0)
    seed = await window(repo, clock, initially_available=True)
    clock.set(1010.0)
    await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, file_manifest(("a", "s/a", 1), ("b", "s/b", 2)), now=clock(),
    )
    clock.set(1010.0 + fs.IMMEDIATE_DECISION_HOLD_SECONDS)
    assert await repo.file_selection_gate(seed.request_id, seed.provider_resource_id, now=clock()) == fs.SelectionGate.PROCEED
    view = await repo.file_selection_presentation(seed.transfer_id, now=clock())
    assert view["decision"] == "all" and view["decision_reason"] == fs.DecisionReason.DECISION_TIMEOUT
    assert view["selected_entry_ids"] == []


# --------------------------------------------------------------------------- #
# Restart / deadline survival
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_hold_deadline_survives_restart_without_resetting(repo):
    clock = Clock(1000.0)
    seed = await window(repo, clock, initially_available=True)
    clock.set(1000.0)
    canonical = await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, file_manifest(("a", "s/a", 1), ("b", "s/b", 2), ("c", "s/c", 3)), now=clock(),
    )
    deadline = 1000.0 + fs.IMMEDIATE_DECISION_HOLD_SECONDS

    restarted = TransferRepository()
    # 50 seconds still remain when the process comes back at fake t = deadline - 50.
    assert await restarted.file_selection_gate(seed.request_id, seed.provider_resource_id, now=deadline - 50) == fs.SelectionGate.WAIT_FOR_DECISION
    view = await restarted.file_selection_presentation(seed.transfer_id, now=deadline - 50)
    assert view["decision_deadline"] == deadline

    # A restart after the persisted absolute deadline processes expiry at once.
    after = TransferRepository()
    assert await after.file_selection_gate(seed.request_id, seed.provider_resource_id, now=deadline + 1) == fs.SelectionGate.PROCEED
    settled = await after.file_selection_presentation(seed.transfer_id, now=deadline + 1)
    assert settled["decision"] == "all"

    # Re-opening the window with a much later clock must not move either deadline.
    reopened = await after.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=99999.0,
    )
    assert reopened["manifest_wait_until"] == 1000.0 + fs.AUTO_MANIFEST_WINDOW_SECONDS
    assert reopened["hold_until"] == deadline


@pytest.mark.asyncio
async def test_manifest_window_is_not_reset_by_a_later_begin_call(repo):
    clock = Clock(2000.0)
    seed = await window(repo, clock, initially_available=True)
    again = await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=2500.0,
    )
    assert again["manifest_wait_until"] == 2000.0 + fs.AUTO_MANIFEST_WINDOW_SECONDS


# --------------------------------------------------------------------------- #
# Uncached / preparing lifecycle
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_uncached_never_receives_a_decision_hold(repo):
    clock = Clock(1000.0)
    seed = await window(repo, clock, initially_available=False)
    clock.set(1010.0)
    await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, file_manifest(("a", "s/a", 1), ("b", "s/b", 2)), now=clock(),
    )
    view = await repo.file_selection_presentation(seed.transfer_id, now=clock())
    assert view["decision_deadline"] is None
    assert view["auto_offer"] is True                    # within the 60s window
    assert await repo.file_selection_gate(seed.request_id, seed.provider_resource_id, now=clock()) == fs.SelectionGate.PROCEED


@pytest.mark.asyncio
async def test_uncached_manifest_after_60s_never_auto_opens(repo):
    clock = Clock(1000.0)
    seed = await window(repo, clock, initially_available=False)
    clock.set(1061.0)
    await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, file_manifest(("a", "s/a", 1), ("b", "s/b", 2)), now=clock(),
    )
    view = await repo.file_selection_presentation(seed.transfer_id, now=clock())
    assert view["auto_offer"] is False
    assert await repo.active_file_selection_offers(now=clock()) == []


@pytest.mark.asyncio
async def test_uncached_confirm_persists_and_survives_until_materialization(repo):
    clock = Clock(1000.0)
    seed = await window(repo, clock, initially_available=False)
    clock.set(1010.0)
    canonical = await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, file_manifest(("a", "s/a", 10), ("b", "s/b", 20)), now=clock(),
    )
    result = await repo.confirm_file_selection(seed.transfer_id, canonical.manifest_id, [canonical.entries[0].entry_id], now=clock(),
    )
    assert result.outcome == fs.SelectionOutcome.CONFIRMED

    # Provider only becomes AVAILABLE much later; the subset is still authoritative.
    clock.set(5000.0)
    restarted = TransferRepository()
    view = await restarted.file_selection_presentation(seed.transfer_id, now=clock())
    assert view["decision"] == "explicit" and view["selected_entry_ids"] == [canonical.entries[0].entry_id]
    authorized = await restarted.commit_selected_manifest(
        seed.record, executable(("a", "s/a", 10), ("b", "s/b", 20)), now=clock(),
    )
    assert [e.relative_path for e in authorized] == ["s/a"]


@pytest.mark.asyncio
async def test_available_before_confirmation_settles_all_and_locks_selection(repo):
    clock = Clock(1000.0)
    seed = await window(repo, clock, initially_available=False)
    clock.set(1010.0)
    await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, file_manifest(("a", "s/a", 1), ("b", "s/b", 2)), now=clock(),
    )
    full = executable(("a", "s/a", 1), ("b", "s/b", 2))
    assert await repo.commit_selected_manifest(seed.record, full, now=clock()) == full
    view = await repo.file_selection_presentation(seed.transfer_id, now=clock())
    assert view["decision"] == "all" and view["mutable"] is False


# --------------------------------------------------------------------------- #
# Close/X on an uncached offer, and the Details re-entry point staying open
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_uncached_dismiss_keeps_default_all_but_leaves_selection_mutable(repo):
    clock = Clock(1000.0)
    seed = await window(repo, clock, initially_available=False)
    clock.set(1010.0)
    canonical = await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, file_manifest(("a", "s/a", 1), ("b", "s/b", 2)), now=clock(),
    )
    result = await repo.dismiss_file_selection(seed.transfer_id, canonical.manifest_id, now=clock(),
    )
    assert result.outcome == fs.SelectionOutcome.DISMISSED
    view = await repo.file_selection_presentation(seed.transfer_id, now=clock())
    assert view["decision"] == "pending" and view["mutable"] is True
    assert view["auto_offer"] is False                   # dismissed: no repeat auto-open
    # A later explicit confirmation is still accepted.
    later = await repo.confirm_file_selection(seed.transfer_id, canonical.manifest_id, [canonical.entries[1].entry_id], now=clock(),
    )
    assert later.outcome == fs.SelectionOutcome.CONFIRMED


@pytest.mark.asyncio
async def test_stale_manifest_id_cannot_confirm_or_dismiss(repo):
    clock = Clock(1000.0)
    seed = await window(repo, clock, initially_available=True)
    clock.set(1005.0)
    await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, file_manifest(("a", "s/a", 1), ("b", "s/b", 2)), now=clock(),
    )
    stale = "0" * 32
    confirm = await repo.confirm_file_selection(seed.transfer_id, stale, ["x"], now=clock())
    assert confirm.outcome == fs.SelectionOutcome.CONFLICT and confirm.detail == "stale_manifest"
    dismiss = await repo.dismiss_file_selection(seed.transfer_id, stale, now=clock())
    assert dismiss.outcome == fs.SelectionOutcome.CONFLICT and dismiss.detail == "stale_manifest"


# --------------------------------------------------------------------------- #
# A non-debrid provider drives the real core (specification section 58)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_fake_non_debrid_provider_exercises_file_manifest_through_real_core(repo):
    clock = Clock(1000.0)
    provider = ParcelProvider(identity="parcel-lab", file_manifest=True)
    registry = IntegrationRegistry()
    registry.register_provider(provider)

    files = [("s01.mkv", "Season 1/s01.mkv", 100), ("s02.mkv", "Season 1/s02.mkv", 200),
             ("notes.txt", "notes.txt", 0)]
    resolution = provider.parcel("show", state=ResourceState.AVAILABLE, files=files)
    observation = resolution.observation
    assert observation.file_manifest is not None                 # provider reports facts only

    seed = await seed_window(transfer_hash="p" * 40, provider_id=provider.descriptor.id)
    await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, provider.descriptor.id,
        initially_available=True, now=clock(),
    )
    clock.advance(3)
    canonical = await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, observation.file_manifest, now=clock())
    assert canonical.file_count == 3

    keep = [e.entry_id for e in canonical.entries if e.relative_path != "notes.txt"]
    clock.advance(20)
    confirmed = await repo.confirm_file_selection(seed.transfer_id, canonical.manifest_id, keep, now=clock())
    assert confirmed.outcome == fs.SelectionOutcome.CONFIRMED

    # The provider later returns its ordinary full executable SourceEntry list.
    executable_manifest = executable(*files)
    authorized = await repo.commit_selected_manifest(seed.record, executable_manifest, now=clock())
    assert sorted(e.relative_path for e in authorized) == ["Season 1/s01.mkv", "Season 1/s02.mkv"]

    # No concrete provider/executor identity is stored in the neutral manifest tables.
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT name, relative_path FROM transfer_file_manifest_entries")
    blob = repr(rows).casefold()
    for token in ("alldebrid", "aria2", "memory:", "http", "endpoint"):
        assert token not in blob


@pytest.mark.asyncio
async def test_duplicate_confirm_is_idempotent_and_never_double_commits(repo):
    clock = Clock(1000.0)
    seed = await window(repo, clock, initially_available=True)
    clock.set(1005.0)
    canonical = await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, file_manifest(("a", "s/a", 10), ("b", "s/b", 20)), now=clock(),
    )
    keep = [canonical.entries[0].entry_id]
    first = await repo.confirm_file_selection(seed.transfer_id, canonical.manifest_id, keep, now=clock())
    second = await repo.confirm_file_selection(seed.transfer_id, canonical.manifest_id, keep, now=clock())
    assert first.outcome == fs.SelectionOutcome.CONFIRMED
    assert second.outcome == fs.SelectionOutcome.CONFIRMED and second.detail == "idempotent"
    async with database.get_db() as db:
        rows = await db.fetchall(
            """SELECT e.entry_id FROM transfer_file_selection_entries e
               JOIN transfer_file_selections s ON s.id=e.selection_id
               WHERE s.request_id=?""", (seed.request_id,))
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# Full lifecycle through the real engine + fake provider + executor
# --------------------------------------------------------------------------- #

FILES6 = [
    ("e1.mkv", "S1/e1.mkv", 10), ("e2.mkv", "S1/e2.mkv", 20), ("e3.mkv", "S1/e3.mkv", 30),
    ("e4.mkv", "S1/e4.mkv", 40), ("e5.mkv", "S1/e5.mkv", 50), ("nfo.txt", "nfo.txt", 0),
]


async def _selection_row(transfer_id):
    async with database.get_db() as db:
        return await db.fetchone(
            "SELECT * FROM transfer_file_selections WHERE transfer_id=? ORDER BY created_at DESC LIMIT 1",
            (transfer_id,))


async def _members(core, transfer_id):
    return [r for r in await core.repository.requests(transfer_id) if r.parent_id]


@pytest.mark.asyncio
async def test_engine_cached_multi_file_holds_then_times_out_to_all(core):
    core.provider.responses.append(
        core.provider.parcel("A", state=ResourceState.AVAILABLE, files=FILES6))
    transfer = await _engine_submit(core)
    await core.engine.resolve_pending()

    # Held: no executable manifest fetched, no children yet.
    assert ("manifest", "parcel-lab:A") not in core.provider.calls
    assert await _members(core, transfer.id) == []
    view = await core.repository.file_selection_presentation(transfer.id, now=core.clock())
    assert view["decision_deadline"] == 1000.0 + fs.IMMEDIATE_DECISION_HOLD_SECONDS

    core.clock.set(1000.0 + fs.IMMEDIATE_DECISION_HOLD_SECONDS + 1)
    await core.engine.tick()
    members = await _members(core, transfer.id)
    assert sorted(r.entry.relative_path for r in members) == sorted(f[1] for f in FILES6)
    view = await core.repository.file_selection_presentation(transfer.id, now=core.clock())
    assert view["decision"] == "all" and view["decision_reason"] == fs.DecisionReason.DECISION_TIMEOUT


@pytest.mark.asyncio
async def test_engine_cached_confirm_materializes_only_subset_and_executor_is_selection_blind(core):
    core.provider.responses.append(
        core.provider.parcel("A", state=ResourceState.AVAILABLE, files=FILES6))
    transfer = await _engine_submit(core)
    await core.engine.resolve_pending()

    view = await core.repository.file_selection_presentation(transfer.id, now=core.clock())
    keep = [view["entries"][1]["entry_id"], view["entries"][3]["entry_id"]]   # e2, e4
    result = await core.repository.confirm_file_selection(
        transfer.id, view["manifest_id"], keep, now=core.clock())
    assert result.outcome == fs.SelectionOutcome.CONFIRMED

    core.clock.advance(5)
    for _ in range(6):
        await core.engine.tick()
        core.clock.advance(1)

    members = await _members(core, transfer.id)
    assert sorted(r.entry.relative_path for r in members) == ["S1/e2.mkv", "S1/e4.mkv"]
    artifacts = await core.repository.artifacts(transfer.id)
    assert sorted(a.name for a in artifacts) == ["e2.mkv", "e4.mkv"]

    # The executor was invoked only for those 2 and received ordinary canonical
    # work: no manifest id, selection state, provider file index, or native tree.
    assert len(core.executor.started) == 2
    for request in core.executor.started:
        candidate = request.candidate
        blob = repr(request).casefold()
        for token in ("manifest", "selection", "entry_id", "file_manifest", "bitmask"):
            assert token not in blob
        assert not candidate.context or set(candidate.context) <= {"copy_ticket", "destination"}
        assert candidate.relative_path in {"S1/e2.mkv", "S1/e4.mkv"}


@pytest.mark.asyncio
async def test_engine_confirmed_subset_never_broadens_when_late_manifest_drops_a_path(core):
    core.provider.responses.append(
        core.provider.parcel("A", state=ResourceState.AVAILABLE, files=FILES6))
    transfer = await _engine_submit(core)
    await core.engine.resolve_pending()
    view = await core.repository.file_selection_presentation(transfer.id, now=core.clock())
    keep = [view["entries"][0]["entry_id"], view["entries"][4]["entry_id"]]     # e1, e5
    await core.repository.confirm_file_selection(transfer.id, view["manifest_id"], keep, now=core.clock())

    # The executable manifest the provider later returns is missing e5.
    core.provider.members["parcel-lab:A"] = executable(
        ("e1.mkv", "S1/e1.mkv", 10), ("e2.mkv", "S1/e2.mkv", 20))

    core.clock.advance(5)
    for _ in range(4):
        await core.engine.tick()
        core.clock.advance(1)

    view = await core.repository.file_selection_presentation(transfer.id, now=core.clock())
    assert view["decision"] == "explicit"                       # never degraded to ALL
    row = await _selection_row(transfer.id)
    assert row["manifest_committed_at"] is None                 # request contained, not committed
    assert await _members(core, transfer.id) == []              # no children, no broadening


@pytest.mark.asyncio
async def test_initial_preparing_then_later_available_never_applies_the_120s_cached_hold(core):
    from dataclasses import replace as _replace

    prepare = core.provider.parcel("A", state=ResourceState.PREPARING)
    core.provider.members["parcel-lab:A"] = executable(*FILES6)
    core.provider.responses.append(prepare)
    transfer = await _engine_submit(core)
    await core.engine.resolve_pending()

    row = await _selection_row(transfer.id)
    assert row["initially_available"] == 0 and row["hold_until"] is None

    # t=10: a complete multi-file manifest appears while still PREPARING.
    core.clock.set(1010.0)
    core.provider.resources["parcel-lab:A"] = _replace(
        prepare.observation, file_manifest=file_manifest(*FILES6))
    await core.engine.resolve_pending()
    view = await core.repository.file_selection_presentation(transfer.id, now=core.clock())
    assert view["auto_offer"] is True                           # within the 60s window
    assert view["decision_deadline"] is None                    # NO cached hold
    assert view["initially_available"] is False

    # t=50: resource transitions to AVAILABLE. No 120s hold is retroactively added.
    core.clock.set(1050.0)
    core.provider.resources["parcel-lab:A"] = _replace(
        prepare.observation, state=ResourceState.AVAILABLE, file_manifest=file_manifest(*FILES6))
    for _ in range(6):
        await core.engine.tick()
        core.clock.advance(1)

    row = await _selection_row(transfer.id)
    assert row["initially_available"] == 0
    assert row["hold_until"] is None                            # never a cached hold
    assert row["decision"] == "all"
    # A usable early manifest was already present, so the settled fallback is the
    # normal materialization path (§18), NOT the cached decision timeout and NOT
    # the manifest-wait timeout (which only applies to an initially-AVAILABLE
    # resource that never exposed a usable manifest inside its 60s window).
    assert row["decision_reason"] == fs.DecisionReason.DEFAULT_MATERIALIZATION
    members = await _members(core, transfer.id)
    assert sorted(r.entry.relative_path for r in members) == sorted(f[1] for f in FILES6)


@pytest.mark.asyncio
async def test_initial_preparing_confirm_before_available_persists_subset_only(core):
    from dataclasses import replace as _replace

    prepare = core.provider.parcel("A", state=ResourceState.PREPARING)
    core.provider.members["parcel-lab:A"] = executable(*FILES6)
    core.provider.responses.append(prepare)
    transfer = await _engine_submit(core)
    await core.engine.resolve_pending()

    core.clock.set(1010.0)
    core.provider.resources["parcel-lab:A"] = _replace(
        prepare.observation, file_manifest=file_manifest(*FILES6))
    await core.engine.resolve_pending()
    view = await core.repository.file_selection_presentation(transfer.id, now=core.clock())
    keep = [view["entries"][2]["entry_id"]]                     # e3 only
    await core.repository.confirm_file_selection(transfer.id, view["manifest_id"], keep, now=core.clock())

    core.clock.set(1050.0)
    core.provider.resources["parcel-lab:A"] = _replace(
        prepare.observation, state=ResourceState.AVAILABLE, file_manifest=file_manifest(*FILES6))
    for _ in range(6):
        await core.engine.tick()
        core.clock.advance(1)

    members = await _members(core, transfer.id)
    assert [r.entry.relative_path for r in members] == ["S1/e3.mkv"]
    row = await _selection_row(transfer.id)
    assert row["hold_until"] is None                            # confirmed during PREPARING; no cached hold


@pytest.mark.asyncio
async def test_retry_after_child_creation_keeps_only_the_selected_children(core):
    core.provider.responses.append(
        core.provider.parcel("A", state=ResourceState.AVAILABLE, files=FILES6))
    transfer = await _engine_submit(core)
    await core.engine.resolve_pending()
    view = await core.repository.file_selection_presentation(transfer.id, now=core.clock())
    keep = [view["entries"][0]["entry_id"], view["entries"][1]["entry_id"]]
    await core.repository.confirm_file_selection(transfer.id, view["manifest_id"], keep, now=core.clock())

    core.clock.advance(5)
    for _ in range(6):
        await core.engine.tick()
        core.clock.advance(1)
    before = sorted(r.entry.relative_path for r in await _members(core, transfer.id))
    assert before == ["S1/e1.mkv", "S1/e2.mkv"]

    for _ in range(8):
        await core.engine.tick()
        core.clock.advance(1)
    after = sorted(r.entry.relative_path for r in await _members(core, transfer.id))
    assert after == ["S1/e1.mkv", "S1/e2.mkv"]           # no unselected child ever appears
    artifacts = await core.repository.artifacts(transfer.id)
    assert sorted(a.name for a in artifacts) == ["e1.mkv", "e2.mkv"]
