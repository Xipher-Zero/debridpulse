"""Readiness and ownership corrections (1.0.13, Gate 9 review of the torrent
readiness task).

* 8 -- automatic readiness promotion decides after provider I/O, so its
  decisive route replacement re-reads every operator control in its own
  commit: a Pause or Pause All that lands during that I/O wins; one that
  lands after the commit applies to the new route.
* 9 -- an inventory observation's admission and binding are one
  transaction: a resource another transfer takes after the observation was
  judged eligible leaves no import, demand or request behind and the
  competing owner untouched; adopting an EXISTING import refuses a cancelled
  one or a resource held elsewhere; a genuine import is still bound.
* 4 -- a promoted backup is provenance: once its provider no longer holds the
  root's route, that provider may cover the root again through the next
  generation of the same claim, and the promoted resource is never offered
  as a fresh backup.
* 5 -- whether a provider's failure contradicts a readiness observation is
  decided by one durable evidence order, never by timestamps that tie.
* 7 -- a takeover whose observation merely fails hands the known backup over
  unready instead of failing the route; an invalid adapter answer still
  fails closed.
* 10 -- a torrent with exactly one selectable member is selected whole; more
  than one is never decided for the operator.

Every provider is a local neutral fake (Real-Debrid's adapter over its fake
client where the provider capability is the fact under test).
"""
from __future__ import annotations

import inspect

import pytest
from test_v113_readiness_promotion import POLL, history, offer, preparing, rows
from test_v113_realdebrid_upstream_selection import (
    MOVIE, MOVIE_BYTES, FakeRealDebrid, lab as rd_lab, preparing_parcel, root as rd_root, submit as rd_submit,
    ticks as rd_ticks,
)
from test_v113_root_provider_switch import MAGNET

from db.database import get_db
from providers.realdebrid.provider import RealDebridProvider
from transfers import codec, file_selection as fs
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.manual_route_switch import route_providers, switch_root_provider
from transfers.models import Ownership, ProviderObservation, ProviderResource, ResourceState, TransferRequest

pytestmark = pytest.mark.asyncio


async def operations(lab):
    return [row["operation"] for row in await history((await lab.root()).id)]


async def ready_backup_lab(tmp_path, monkeypatch, *identities):
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b", *identities)
    _standby, backup = await lab.backup("parcel-b")
    return lab, backup


def during_observe(monkeypatch, provider, action):
    """Run ``action`` once, inside the ``observe`` of ``provider`` that the
    readiness decision itself performs (its provider I/O before it commits),
    never an ordinary poll's."""
    original, fired = provider.observe, []

    async def observe(resource):
        result = await original(resource)
        deciding = any(frame.function == "_readiness_promotion" for frame in inspect.stack(0))
        if deciding and not fired:
            fired.append(True)
            await action()
        return result

    monkeypatch.setattr(provider, "observe", observe)
    return fired


# -- 8: operator controls are authoritative over the readiness yield --------------------------------------------

@pytest.mark.parametrize("control, where", [
    ("pause", "parcel-b"), ("pause", "parcel-a"), ("pause_all", "parcel-b"), ("pause_all", "parcel-a")],
    ids=["pause-during-target-read", "pause-during-primary-read", "pause-all-during-target-read",
         "pause-all-during-primary-read"])
async def test_a_pause_landing_during_the_readiness_reads_wins(tmp_path, monkeypatch, control, where):
    lab, backup = await ready_backup_lab(tmp_path, monkeypatch)

    async def operator():
        if control == "pause":
            await lab.engine.pause(lab.transfer.id)
        else:
            await lab.engine.pause_all()

    fired = during_observe(monkeypatch, lab.providers[where], operator)
    await lab.tick(POLL)
    assert fired                                                          # the race was actually run
    root = await lab.root()
    assert root.resource.provider_id == "parcel-a" and root.resource.id == lab.primary.id
    assert "readiness_promotion" not in await operations(lab)
    # The operator's control, not a lost race, held it: released, the
    # same yield applies.
    await (lab.engine.resume(lab.transfer.id) if control == "pause" else lab.engine.resume_all())
    await lab.tick(POLL, times=2)
    assert (await lab.root()).resource.id == backup.id
    assert (await operations(lab)).count("readiness_promotion") == 1


async def test_a_pause_committed_after_the_yield_applies_to_the_new_route(tmp_path, monkeypatch):
    lab, backup = await ready_backup_lab(tmp_path, monkeypatch)
    await lab.tick(POLL)
    assert (await lab.root()).resource.id == backup.id
    await lab.engine.pause(lab.transfer.id)
    assert (await lab.repository.get(lab.transfer.id)).paused
    await lab.tick(POLL, times=2)
    assert (await lab.root()).resource.id == backup.id                   # nothing moved, nothing undone


async def test_the_decisive_replacement_refuses_a_paused_or_executing_root_in_its_own_commit(tmp_path, monkeypatch):
    lab, _backup = await ready_backup_lab(tmp_path, monkeypatch)
    root = await lab.root()
    repository = lab.repository
    latest = await repository.latest_root_route(root.id)

    async def promote():
        return await repository.replace_root_route(
            root.id, expected_attempt_id=str(latest["id"]), expected_provider_id="parcel-a",
            target_provider_id="parcel-b", operation=repository.READINESS_PROMOTION)

    await repository.set_pause_and_fence(lab.transfer.id, True)
    assert await promote() == "paused"
    await repository.set_pause_and_fence(lab.transfer.id, False)
    async with get_db() as db:
        await db.execute("INSERT INTO transfer_controls(key,value) VALUES('paused','1') "
                         "ON CONFLICT(key) DO UPDATE SET value='1'")
        await db.commit()
    assert await promote() == "paused"
    async with get_db() as db:
        await db.execute("UPDATE transfer_controls SET value='0' WHERE key='paused'")
        # The root crossed its execution boundary meanwhile (its selection
        # committed): too late for any automatic route change.
        committed = await db.execute("UPDATE transfer_file_selections SET manifest_committed_at=1 WHERE request_id=?",
                                     (root.id,))
        assert committed.rowcount == 1
        await db.commit()
    assert await promote() == "stale"
    assert (await repository.latest_root_route(root.id))["id"] == latest["id"]   # nothing changed


# -- 9: inventory adoption never binds terminal or already-held work --------------------------------------------

def listing(resource, *, request=None):
    request = request or TransferRequest("magnet", MAGNET, name="Show")
    return ProviderObservation(
        ProviderResource(resource.provider_id, dict(resource.context), Ownership.OBSERVED, id=resource.id),
        ResourceState.AVAILABLE, request.name, request=request)


OTHER = TransferRequest("magnet", "magnet:?xt=urn:btih:" + "d" * 40, name="Other", fingerprint="d" * 40)


async def _inventory_rows():
    async with get_db() as db:
        return await db.fetchall("SELECT id,status FROM torrents WHERE source='inventory'")


async def bindings_of(transfer_id):
    async with get_db() as db:
        return await db.fetchall("SELECT resource_key FROM provider_resources WHERE transfer_id=?", (transfer_id,))


async def ledger():
    """Everything an inventory observation could have created or touched."""
    return {name: (await rows(sql))[0]["n"] for name, sql in {
        "transfers": "SELECT COUNT(*) AS n FROM torrents",
        "requests": "SELECT COUNT(*) AS n FROM transfer_requests",
        "demands": "SELECT COUNT(*) AS n FROM transfer_demands",
        "attempts": "SELECT COUNT(*) AS n FROM resolution_attempts",
        "bindings": "SELECT COUNT(*) AS n FROM provider_resources",
    }.items()}


@pytest.mark.parametrize("seam", ["after-eligibility", "before-admission", "after-admission-commit"])
async def test_a_resource_contested_after_inventory_eligibility_leaves_no_ghost_and_no_damage(
        tmp_path, monkeypatch, seam):
    """Another live transfer tries to take the native resource after the
    inventory pass judged it eligible (no historical binding): before the
    import's admission, or right after the import's admission commit and
    before anything else of the pass. Admission and binding are one
    transaction, so exactly one owner ever holds the resource, and the
    loser is whole: an observation that loses leaves no transfer, demand,
    request or attempt of its own; a competitor that loses is refused
    (``OWNERSHIP_CONFLICT``) and left exactly as it was. No live inventory
    import ever exists without its binding."""
    lab, _backup = await ready_backup_lab(tmp_path, monkeypatch)
    provider = lab.providers["parcel-a"]
    other = offer(provider, "unrelated", ResourceState.AVAILABLE)
    provider.responses.clear()
    competitor = await lab.engine.submit((TransferRequest("magnet", OTHER.payload, name="Competitor"),),
                                         name="Competitor", deduplicate=False)
    refused = []

    async def competitor_takes_it():
        try:
            async with get_db() as db:
                await lab.repository._resource(db, competitor.id, other, ResourceState.AVAILABLE)
                await db.commit()
        except TransferError as exc:
            refused.append(exc.error.category)

    if seam == "after-eligibility":
        eligible = lab.repository.historical_binding

        async def historical_binding(resource_key):
            found = await eligible(resource_key)                          # eligible: never bound
            if resource_key == other.id:
                await competitor_takes_it()
            return found

        monkeypatch.setattr(lab.repository, "historical_binding", historical_binding)
    else:
        admit = lab.repository.admit

        async def racing_admit(*args, **kwargs):
            if seam == "before-admission":
                await competitor_takes_it()
            result = await admit(*args, **kwargs)
            if seam == "after-admission-commit":
                await competitor_takes_it()                               # before any adoption step
            return result

        monkeypatch.setattr(lab.repository, "admit", racing_admit)
    provider.inventory_items = (listing(other, request=OTHER),)
    resolves = len([call for call in provider.calls if call[0] == "resolve"])
    async with get_db() as db:
        competitor_demands = await db.fetchall("SELECT id,withdrawn_at FROM transfer_demands WHERE transfer_id=?",
                                               (competitor.id,))
    before = await ledger()

    await lab.engine.reconcile_inventory()

    after = await ledger()
    owners = [row["transfer_id"] for row in await rows(
        "SELECT transfer_id FROM provider_resources WHERE resource_key=?", (other.id,))]
    imports = await _inventory_rows()
    assert len(owners) == 1                                               # never a duplicate binding
    for imported in imports:                                              # never an unbound live import
        assert [row["resource_key"] for row in await bindings_of(imported["id"])] == [other.id]
    if seam == "after-admission-commit":
        # The import committed first and owns it; the competitor was refused.
        assert refused == [Category.OWNERSHIP_CONFLICT]
        assert [row["id"] for row in imports] == owners
        assert await bindings_of(competitor.id) == []
    else:
        # The competitor committed first; the observation left nothing.
        assert refused == [] and imports == [] and owners == [competitor.id]
        assert after == {**before, "bindings": before["bindings"] + 1}   # only the competitor's binding
    async with get_db() as db:
        assert await db.fetchall("SELECT id,withdrawn_at FROM transfer_demands WHERE transfer_id=?",
                                 (competitor.id,)) == competitor_demands
    assert (await lab.repository.get(competitor.id)).state == competitor.state
    # Later passes change nothing, and nothing is asked of the provider on a
    # losing import's behalf.
    for _ in range(2):
        await lab.engine.reconcile_inventory()
    assert await _inventory_rows() == imports
    assert len([call for call in provider.calls if call[0] == "resolve"]) == resolves


async def test_an_import_is_bound_in_its_own_admission_commit_and_a_later_owner_conflicts(tmp_path, monkeypatch):
    lab, _backup = await ready_backup_lab(tmp_path, monkeypatch)
    other = offer(lab.providers["parcel-a"], "unrelated", ResourceState.AVAILABLE)
    lab.providers["parcel-a"].inventory_items = (listing(other, request=OTHER),)
    observed = []
    admit = lab.repository.admit

    async def watching_admit(*args, **kwargs):
        result = await admit(*args, **kwargs)
        observed.append([row["resource_key"] for row in await bindings_of(result[0].id)])   # right after commit
        return result

    monkeypatch.setattr(lab.repository, "admit", watching_admit)
    await lab.engine.reconcile_inventory()
    (imported,) = await _inventory_rows()
    assert observed == [[other.id]]                                       # bound by the admission itself
    assert [row["resource_key"] for row in await bindings_of(imported["id"])] == [other.id]
    with pytest.raises(TransferError) as conflict:                        # a later live owner cannot take it
        async with get_db() as db:
            await lab.repository._resource(db, lab.transfer.id, other, ResourceState.AVAILABLE)
    assert conflict.value.error.category == Category.OWNERSHIP_CONFLICT
    assert [row["resource_key"] for row in await bindings_of(imported["id"])] == [other.id]


@pytest.mark.parametrize("race", ["cancelled", "held_elsewhere"])
async def test_adopting_an_existing_import_refuses_work_that_changed(tmp_path, monkeypatch, race):
    """The converge path: an observation that dedupes onto an EXISTING
    unbound import is adopted by ``adopt_inventory_resource``, which refuses a
    cancelled import or a resource another transfer holds -- in its commit."""
    lab, _backup = await ready_backup_lab(tmp_path, monkeypatch)
    other = offer(lab.providers["parcel-a"], "unrelated", ResourceState.AVAILABLE)
    existing, created = await lab.repository.admit((OTHER,), name="Other", source="inventory", independent=False)
    assert created and await bindings_of(existing.id) == []
    if race == "cancelled":
        await lab.engine.cancel(existing.id)
    else:
        async with get_db() as db:
            await lab.repository._resource(db, lab.transfer.id, other, ResourceState.AVAILABLE)
            await db.commit()
    assert await lab.repository.adopt_inventory_resource(existing.id, other, ResourceState.AVAILABLE) is False
    assert await bindings_of(existing.id) == []


async def test_a_genuine_unrelated_import_is_still_adopted(tmp_path, monkeypatch):
    lab, _backup = await ready_backup_lab(tmp_path, monkeypatch)
    other = offer(lab.providers["parcel-a"], "unrelated", ResourceState.AVAILABLE)
    lab.providers["parcel-a"].inventory_items = (listing(other, request=OTHER),)
    await lab.engine.reconcile_inventory()
    (imported,) = await _inventory_rows()
    assert [row["resource_key"] for row in await bindings_of(imported["id"])] == [other.id]


# -- 4: a promoted backup's provider may cover the root again, as a new generation -------------------------------

async def standby_rows(request_id, provider_id):
    return await rows("SELECT generation,state,promoted_at,binding_id FROM standby_resources "
                      "WHERE request_id=? AND provider_id=? ORDER BY generation", (request_id, provider_id))


async def test_a_promoted_backup_never_blocks_later_coverage_by_its_provider(tmp_path, monkeypatch):
    lab, backup = await ready_backup_lab(tmp_path, monkeypatch, "parcel-c")
    await lab.tick(POLL)
    root = await lab.root()
    assert root.resource.id == backup.id                                  # parcel-b's backup was promoted
    repository, now = lab.repository, lab.engine.clock()
    # While parcel-b holds the route it is never its own root's backup.
    assert await repository.begin_standby(lab.transfer.id, root.id, "parcel-b", now) is None
    assert await repository.promotable_standby(root.id, "parcel-b") is None   # provenance, never a backup
    offer(lab.providers["parcel-c"], "chosen", ResourceState.PREPARING)
    await switch_root_provider(lab.engine, lab.transfer.id, "parcel-c", expected_provider_id="parcel-b")
    await lab.tick(times=2)
    # parcel-b's route ended: the same (root, provider) claim's next
    # generation, fresh and unbound -- once.
    claimed = await repository.begin_standby(lab.transfer.id, root.id, "parcel-b", lab.engine.clock())
    assert claimed is not None and claimed[1] == 0
    again = await repository.begin_standby(lab.transfer.id, root.id, "parcel-b", lab.engine.clock())
    assert again == claimed                                               # reconciled, never a duplicate
    generations = await standby_rows(root.id, "parcel-b")
    assert [(row["generation"], row["state"], row["promoted_at"] is not None) for row in generations] == [
        (1, "bound", True), (2, "creating", False)]
    assert generations[1]["binding_id"] is None
    assert await repository.promotable_standby(root.id, "parcel-b") is None   # the stale resource is not it


# -- 5: one total evidence order decides contradiction ------------------------------------------------------------

async def tie_timestamps(request_id, binding_id):
    """Both facts in the same wall-clock second, as coarse timestamps leave
    them: only the evidence order can tell them apart."""
    async with get_db() as db:
        await db.execute("UPDATE resolution_attempts SET updated_at='2026-10-15 12:00:00' WHERE request_id=?",
                         (request_id,))
        await db.execute("UPDATE provider_resources SET updated_at='2026-10-15 12:00:00' WHERE id=?", (binding_id,))
        await db.commit()


async def contradicted(lab):
    return next(item for item in await lab.repository.standbys(lab.transfer.id)
                if item["provider_id"] == "parcel-b")["contradicted"]


async def test_a_failure_and_a_readiness_observation_in_one_second_are_ordered(tmp_path, monkeypatch):
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    standby_id, backup = await lab.backup("parcel-b", ResourceState.PREPARING)
    root = await lab.root()
    binding = next(item for item in await lab.repository.standbys(lab.transfer.id))["binding_id"]
    now = lab.engine.clock()
    await lab.repository.observe_standby(standby_id, lab.transfer.id, backup, ResourceState.AVAILABLE, now)
    error = NormalizedError(Domain.PROVIDER, Category.SOURCE_TEMPORARILY_UNAVAILABLE, Stage.RESOLUTION,
                            integration_id="parcel-b")
    async with get_db() as db:                                            # parcel-b's own route of the root fails
        await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state,error) "
                         "VALUES('b-failed',?,'parcel-b','failed',?)", (root.id, codec.dump(error)))
        await db.commit()
    await tie_timestamps(root.id, binding)
    assert await contradicted(lab) is True                                # the failure came after
    choice = {entry["provider_id"]: entry["status"] for entry in (await route_providers(lab.engine,
                                                                                       lab.transfer.id))["providers"]}
    assert choice["parcel-b"] == "failed_earlier"
    await lab.repository.observe_standby(standby_id, lab.transfer.id, backup, ResourceState.AVAILABLE, now)
    await tie_timestamps(root.id, binding)
    assert await contradicted(lab) is False                               # ready again, observed after it
    choice = {entry["provider_id"]: entry["status"] for entry in (await route_providers(lab.engine,
                                                                                       lab.transfer.id))["providers"]}
    assert choice["parcel-b"] == "prepared"


# -- 7: a takeover observation failure is not a route failure ------------------------------------------------------

def failing_observe(monkeypatch, provider, error):
    async def observe(resource):
        raise TransferError(error)
    monkeypatch.setattr(provider, "observe", observe)


async def test_a_transient_takeover_read_failure_hands_the_backup_over_unready(tmp_path, monkeypatch):
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    _standby, backup = await lab.backup("parcel-b")
    provider = lab.providers["parcel-b"]
    real_observe = provider.observe
    await switch_root_provider(lab.engine, lab.transfer.id, "parcel-b", expected_provider_id="parcel-a")
    failing_observe(monkeypatch, provider, NormalizedError(
        Domain.PROVIDER, Category.READ_TIMEOUT, Stage.RESOLUTION, retryability=Retryability.BACKOFF, integration_id="parcel-b"))
    await lab.engine.resolve_pending()
    root = await lab.root()
    assert root.resource is not None and root.resource.id == backup.id    # taken over, identity kept
    assert await rows("SELECT 1 FROM resolution_attempts WHERE request_id=? AND provider_id='parcel-b' "
                      "AND state='exhausted'", (root.id,)) == []
    assert await rows("SELECT 1 FROM provider_resources WHERE resource_key=? AND cleanup_authority IS NOT NULL",
                      (backup.id,)) == []                                # the prepared backup is not given back
    (row,) = await standby_rows(root.id, "parcel-b")
    assert row["promoted_at"] is not None
    monkeypatch.setattr(provider, "observe", real_observe)
    await lab.tick(POLL, times=3)                                         # the ordinary observation continues
    assert (await lab.root()).resource.id == backup.id


async def test_an_invalid_adapter_answer_during_takeover_still_fails_closed(tmp_path, monkeypatch):
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    _standby, backup = await lab.backup("parcel-b")
    await switch_root_provider(lab.engine, lab.transfer.id, "parcel-b", expected_provider_id="parcel-a")
    failing_observe(monkeypatch, lab.providers["parcel-b"], NormalizedError(
        Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION, retryability=Retryability.NEVER,
        integration_id="parcel-b"))
    await lab.engine.resolve_pending()
    root = await lab.root()
    assert root.resource is None or root.resource.id != backup.id
    (row,) = await standby_rows(root.id, "parcel-b")
    assert row["promoted_at"] is None                                     # nothing was taken over


# -- 10: singleton == select all ---------------------------------------------------------------------------------

async def test_the_singleton_rule_is_one_neutral_predicate():
    assert fs.unambiguous_selection(1) is True
    assert fs.unambiguous_selection(2) is False
    assert fs.unambiguous_selection(1, explicit_only=True) is False      # a collection is still the operator's


SINGLE = [(1, f"Show/{MOVIE}", MOVIE_BYTES)]


async def test_an_interactive_single_file_torrent_s_backup_is_selected_whole_and_promoted(tmp_path, monkeypatch):
    fake = FakeRealDebrid(SINGLE, ready=False)
    first = preparing_parcel("parcel-a", [(MOVIE, f"Show/{MOVIE}", MOVIE_BYTES)])
    repository, engine = await rd_lab(tmp_path, monkeypatch, first,
                                      RealDebridProvider(fake, prepare_backup_torrents=True))
    transfer = await rd_submit(engine)                                    # interactive, nothing chosen
    await rd_ticks(engine, 6, step=POLL)
    assert fake.selects() == ["1"]                                        # nothing to choose: the whole torrent
    view = await route_providers(engine, transfer.id)
    assert {entry["provider_id"]: entry["status"] for entry in view["providers"]}["realdebrid"] == "preparing"
    for torrent in fake.torrents.values():
        torrent.ready = True
    await rd_ticks(engine, 4, step=POLL)
    assert (await rd_root(repository, transfer)).resource.provider_id == "realdebrid"
    assert len(fake.selects()) == 1


async def test_more_than_one_member_is_never_decided_for_the_operator(tmp_path, monkeypatch):
    two = [(1, f"Show/{MOVIE}", MOVIE_BYTES), (2, "Show/extra.nfo", 400)]
    fake = FakeRealDebrid(two)
    first = preparing_parcel("parcel-a", [(MOVIE, f"Show/{MOVIE}", MOVIE_BYTES), ("extra.nfo", "Show/extra.nfo", 400)])
    _repository, engine = await rd_lab(tmp_path, monkeypatch, first,
                                       RealDebridProvider(fake, prepare_backup_torrents=True))
    transfer = await rd_submit(engine)
    await rd_ticks(engine, 6, step=POLL)
    assert fake.selects() == []
    view = await route_providers(engine, transfer.id)
    assert {entry["provider_id"]: entry["status"] for entry in view["providers"]}["realdebrid"] == \
        "waiting_for_selection"
