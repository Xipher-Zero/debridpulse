"""Readiness-driven promotion (D4, D6) and truthful route history (D7).

A root whose bound resource its provider still reports PREPARING yields to a
backup already prepared for it -- before anything executes -- through the one
route replacement (``replace_root_route`` under ``readiness_promotion``) and
the one promotion seam. The released route is not a failure. A route the
operator chose, a route a readiness yield opened, a preferred primary, a
paused or collection-authority root, or a root past its execution boundary
never yields. Backups exist only where the operator turned Prepare Backup
Torrents on (here recorded through the repository, as that preparation does);
with none, nothing changes.

Route history tells a genuine retry after a failure from a renewed
resolution after a release, and names the two deliberate replacements.

Every provider is a neutral local fixture.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from test_v113_collection_route_generic_closure import Clock
from test_v113_live_provider_routing_corrective import native_refusal, refusing
from test_v113_root_provider_switch import MAGNET, magnet_provider, root_of, rows
from test_v113_root_provider_switch_corrective import big_lab

from db import database
from fake_integrations import MemoryExecutor
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Stage
from transfers.manual_route_switch import switch_root_provider
from transfers.models import Ownership, ResolutionResult, ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

FILES = [("one.bin", "Show/one.bin", 4), ("two.bin", "Show/two.bin", 4)]
POLL = 30.0


def offer(provider, native, state, *, ownership=Ownership.CREATED):
    result = provider.parcel(native, state=state, ownership=ownership, files=FILES)
    observed = replace(result.observation, request=TransferRequest("magnet", MAGNET))
    provider.resources[observed.resource.id] = observed
    provider.responses.append(replace(result, observation=observed))
    return observed.resource


def set_state(provider, resource, state):
    provider.resources[resource.id] = replace(provider.resources[resource.id], state=state)


class Lab:
    def __init__(self, repository, engine, providers, transfer, primary):
        self.repository, self.engine, self.providers = repository, engine, providers
        self.transfer, self.primary = transfer, primary

    async def root(self):
        return await root_of(self.repository, self.transfer.id)

    async def backup(self, identity, state=ResourceState.AVAILABLE, *, native=None):
        """``identity`` holds a backup of the root, as the operator's Prepare
        Backup Torrents preparation records one."""
        provider = self.providers[identity]
        resource = offer(provider, native or f"backup-{identity}", state)
        provider.responses.clear()                                     # promotion reuses it: never a create
        root = await self.root()
        standby_id, _attempts = await self.repository.begin_standby(self.transfer.id, root.id, identity,
                                                                    self.engine.clock())
        await self.repository.bind_standby(standby_id, self.transfer.id, resource, state, self.engine.clock())
        return standby_id, resource

    async def tick(self, seconds=0.0, times=1):
        """``times`` scheduler turns ``seconds`` apart. A turn that leaves a
        readiness deadline due now is followed by the pass the scheduler then
        runs at once (``core.scheduler.sync_status_loop``), clock unmoved."""
        for _ in range(times):
            self.engine.clock.now += seconds
            await self.engine.tick()
            if self.engine.resolution_deadline is not None and self.engine.resolution_deadline <= self.engine.clock():
                await self.engine.tick()


async def preparing(tmp_path, monkeypatch, *identities, ownership=Ownership.CREATED, **request):
    """A BitTorrent root bound to ``identities[0]``, whose resource that
    provider keeps reporting PREPARING."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "readiness.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    providers = {identity: magnet_provider(identity) for identity in identities}
    for provider in providers.values():
        registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3, resource_poll_interval=POLL),
                            clock=Clock())
    await engine.initialize()
    primary = offer(providers[identities[0]], "primary", ResourceState.PREPARING, ownership=ownership)
    transfer = await engine.submit((TransferRequest("magnet", MAGNET, name="Show", **request),),
                                   name="Show", deduplicate=False)
    lab = Lab(repository, engine, providers, transfer, primary)
    await lab.tick(times=2)
    root = await lab.root()
    assert root.resource.id == primary.id and root.resource.provider_id == identities[0]
    return lab


async def history(request_id):
    return await rows("""SELECT a.provider_id,a.state,a.error,p.operation,p.transition_kind,p.transition_reason
        FROM route_attempt_provenance p JOIN resolution_attempts a ON a.id=p.resolution_attempt_id
        WHERE a.request_id=? ORDER BY p.ordinal""", (request_id,))


# -- the #591 shape ------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_preparing_primary_yields_to_a_prepared_backup_released_never_failed(tmp_path, monkeypatch):
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    standby_id, backup = await lab.backup("parcel-b")
    await lab.tick(POLL)                                               # the primary's next observation
    root = await lab.root()
    assert root.resource.id == backup.id and await lab.repository.bound_route_provider(root.id) == "parcel-b"
    rows_ = await history(root.id)
    assert [(row["provider_id"], row["state"], row["error"]) for row in rows_[:1]] == [("parcel-a", "released", None)]
    assert (rows_[-1]["provider_id"], rows_[-1]["operation"], rows_[-1]["transition_kind"],
            rows_[-1]["transition_reason"]) == ("parcel-b", "readiness_promotion", "provider_change",
                                                "readiness_promotion")
    assert len([row for row in rows_ if row["provider_id"] == "parcel-b"]) == 1   # one adopted attempt
    assert not await lab.repository.exhausted_route_providers(root.id)            # nothing failed, no budget
    assert not await lab.repository.provider_reentries(root.id)
    promoted = [item for item in await lab.repository.standbys(lab.transfer.id) if item["promoted_at"]]
    assert [item["id"] for item in promoted] == [standby_id]
    assert ("resolve", MAGNET) not in lab.providers["parcel-b"].calls                 # the root was never created again
    # The released primary was DebridPulse's own (CREATED): given back through the one cleanup owner.
    (released,) = [state for resource, state, _pending in await lab.repository.resources(lab.transfer.id)
                   if resource.id == lab.primary.id]
    assert released == ResourceState.ABSENT
    events = await rows("SELECT event_type,detail FROM event_journal WHERE transfer_id=? AND event_type LIKE 'routing.%'",
                        (lab.transfer.id,))
    assert ("routing.provider_changed", "Provider parcel-b; previously parcel-a (readiness promotion)") in [
        (row["event_type"], row["detail"]) for row in events]
    assert not await rows("SELECT 1 FROM application_events WHERE transfer_id=? AND kind='error'", (lab.transfer.id,))
    await lab.tick(times=2)
    assert any(artifact.execution is not None or artifact.state == "completed"
               for artifact in await lab.repository.artifacts(lab.transfer.id))


@pytest.mark.asyncio
async def test_a_backup_newly_observed_ready_wakes_the_root_without_another_poll(tmp_path, monkeypatch):
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    _standby, backup = await lab.backup("parcel-b", ResourceState.PREPARING)
    set_state(lab.providers["parcel-b"], backup, ResourceState.AVAILABLE)
    lab.engine.clock.now += POLL                                       # readiness is discovered at the poll cadence
    await lab.engine.resolve_pending()                                 # the backup is observed ready, the yield commits
    root = await lab.root()
    assert root.resource is None and root.state == "pending" and root.retry_at == 0
    assert lab.engine.resolution_deadline == lab.engine.clock()        # the next pass is due now, no further poll
    await lab.engine.resolve_pending()                                 # the clock has not moved
    assert (await lab.root()).resource.id == backup.id


@pytest.mark.asyncio
async def test_several_ready_backups_follow_the_canonical_competition_order(tmp_path, monkeypatch):
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b", "parcel-c")
    lab.providers["parcel-c"].descriptor = replace(lab.providers["parcel-c"].descriptor, priority=5)
    await lab.backup("parcel-b")
    _standby, preferred = await lab.backup("parcel-c")
    await lab.tick(POLL)
    assert (await lab.root()).resource.id == preferred.id


# -- exclusions ----------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_with_no_prepared_backup_nothing_yields(tmp_path, monkeypatch):
    """Prepare Backup Torrents off (its default): no backup exists, so a
    preparing root behaves exactly as before."""
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    await lab.tick(POLL, times=3)
    root = await lab.root()
    assert root.resource.id == lab.primary.id
    assert [row["operation"] for row in await history(root.id)] == ["resolve"]


@pytest.mark.asyncio
async def test_a_preferred_primary_never_yields(tmp_path, monkeypatch):
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b", preferred_provider="parcel-a")
    await lab.backup("parcel-b")
    await lab.tick(POLL, times=2)
    assert (await lab.root()).resource.id == lab.primary.id


@pytest.mark.asyncio
async def test_a_route_the_operator_chose_never_yields_even_while_it_prepares(tmp_path, monkeypatch):
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b", "parcel-c")
    chosen = offer(lab.providers["parcel-c"], "chosen", ResourceState.PREPARING)
    await switch_root_provider(lab.engine, lab.transfer.id, "parcel-c", expected_provider_id="parcel-a")
    await lab.tick(times=2)
    assert (await lab.root()).resource.id == chosen.id
    await lab.backup("parcel-b")
    await lab.tick(POLL, times=2)
    root = await lab.root()
    assert root.resource.id == chosen.id
    assert [row["operation"] for row in await history(root.id)][-1] == "operator_switch"


@pytest.mark.asyncio
async def test_a_route_a_readiness_yield_opened_never_yields_again(tmp_path, monkeypatch):
    """Anti-flapping by route state: the promoted backup turned out still
    preparing; another ready backup does not move the route again."""
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b", "parcel-c")
    _standby, backup = await lab.backup("parcel-b")
    replace_root_route = lab.repository.replace_root_route

    async def stale(*args, **kwargs):
        outcome = await replace_root_route(*args, **kwargs)
        set_state(lab.providers["parcel-b"], backup, ResourceState.PREPARING)   # ready when chosen, preparing once taken
        return outcome

    monkeypatch.setattr(lab.repository, "replace_root_route", stale)
    await lab.tick(POLL)
    assert (await lab.root()).resource.id == backup.id
    await lab.backup("parcel-c")
    await lab.tick(POLL, times=3)
    root = await lab.root()
    assert root.resource.id == backup.id
    assert [row["operation"] for row in await history(root.id)].count("readiness_promotion") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ["paused", "globally_paused", "collection_authority"])
async def test_a_held_root_never_yields(tmp_path, monkeypatch, hold):
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    await lab.backup("parcel-b")
    if hold == "paused":
        await lab.engine.pause(lab.transfer.id)
    elif hold == "globally_paused":
        await lab.engine.pause_all()
    else:
        async with database.get_db() as db:
            await db.execute("UPDATE torrents SET collection_route_authority=1 WHERE id=?", (lab.transfer.id,))
            await db.commit()
    root = await lab.root()
    assert await lab.engine._readiness_promotion(root) is False
    await lab.tick(POLL, times=2)
    assert (await lab.root()).resource.id == lab.primary.id


@pytest.mark.asyncio
async def test_the_execution_boundary_is_any_commitment_fan_out_or_download(tmp_path, monkeypatch):
    repository, engine, _providers, _executor, transfer = await big_lab(tmp_path, monkeypatch, "parcel-a")
    root = await root_of(repository, transfer.id)
    assert await repository.root_crossed_execution_boundary(root.id)
    lab = await preparing(tmp_path / "fresh", monkeypatch, "parcel-a", "parcel-b")
    assert not await lab.repository.root_crossed_execution_boundary((await lab.root()).id)


@pytest.mark.asyncio
async def test_a_backup_its_provider_no_longer_holds_is_never_taken_over(tmp_path, monkeypatch):
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    _standby, backup = await lab.backup("parcel-b")
    lab.providers["parcel-b"].resources.pop(backup.id)                # gone since it was last seen ready
    root = await lab.root()
    assert await lab.engine._readiness_promotion(root) is False
    assert (await lab.root()).resource.id == lab.primary.id
    (standby,) = await lab.repository.standbys(lab.transfer.id)
    assert standby["resource_state"] == ResourceState.ABSENT.value


@pytest.mark.asyncio
async def test_two_concurrent_decisions_promote_once(tmp_path, monkeypatch):
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    await lab.backup("parcel-b")
    root = await lab.root()
    outcomes = await asyncio.gather(*(lab.engine._readiness_promotion(root)
                                      for _ in range(2)))
    assert sorted(outcomes) == [False, True]
    assert [row["operation"] for row in await history(root.id)].count("readiness_promotion") == 1


@pytest.mark.asyncio
async def test_an_observed_primary_is_released_without_any_deletion(tmp_path, monkeypatch):
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b", ownership=Ownership.OBSERVED)
    await lab.backup("parcel-b")
    await lab.tick(POLL)
    assert (await lab.root()).resource.provider_id == "parcel-b"
    assert not [call for call in lab.providers["parcel-a"].calls if call[0] == "cleanup"]
    (row,) = await rows("SELECT state,cleanup_authority FROM provider_resources WHERE provider_id='parcel-a'")
    assert row["cleanup_authority"] is None


# -- after a yield: ordinary failure recovery, restart ------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_promoted_route_that_really_fails_continues_through_ordinary_failover(tmp_path, monkeypatch):
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    refusing(lab.providers["parcel-b"], native_refusal(35, "infringing_file"))
    await lab.backup("parcel-b")
    offer(lab.providers["parcel-a"], "again", ResourceState.AVAILABLE)
    await lab.tick(POLL)
    await lab.tick(times=3)
    root = await lab.root()
    failed = [row for row in await history(root.id) if row["provider_id"] == "parcel-b"]
    assert [(row["state"], row["operation"]) for row in failed] == [("exhausted", "readiness_promotion")]
    assert '"category":"candidate_rejected"' in failed[0]["error"]
    assert "parcel-b" in await lab.repository.exhausted_route_providers(root.id)
    assert await lab.repository.bound_route_provider(root.id) == "parcel-a"      # the competition continued


@pytest.mark.asyncio
async def test_a_committed_yield_survives_a_restart_and_is_adopted_once(tmp_path, monkeypatch):
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    _standby, backup = await lab.backup("parcel-b")
    root = await lab.root()
    assert await lab.engine._readiness_promotion(root)
    restarted = TransferEngine(TransferRepository(), lab.engine.registry, download_root=lab.engine.root,
                               policy=lab.engine.policy, clock=lab.engine.clock)
    await restarted.initialize()
    await restarted.tick()
    root = await root_of(restarted.repository, lab.transfer.id)
    assert root.resource.id == backup.id
    promoted = [row for row in await history(root.id) if row["operation"] == "readiness_promotion"]
    assert [(row["provider_id"], row["state"]) for row in promoted] == [("parcel-b", "succeeded")]


# -- D7: route history ---------------------------------------------------------------------------------------------

async def _attempt(repository, request_id, provider_id, *, fail=False):
    async with database.get_db() as db:                                   # requeued, as the engine does
        await db.execute("UPDATE transfer_requests SET state='pending' WHERE id=?", (request_id,))
        await db.commit()
    attempt = await repository.begin_resolution(request_id, provider_id)
    error = NormalizedError(Domain.PROVIDER, Category.SOURCE_TEMPORARILY_UNAVAILABLE, Stage.RESOLUTION) if fail else None
    await repository.resolution(attempt, ResolutionResult(ResourceState.UNKNOWN if fail else ResourceState.AVAILABLE,
                                                          error=error))
    return attempt


@pytest.mark.asyncio
async def test_a_renewed_resolution_after_a_release_is_no_retry_and_a_genuine_retry_still_is(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "history.sqlite3")
    await database.init_db()
    repository = TransferRepository()
    transfer, _ = await repository.admit((TransferRequest("https", "https://files.example/a.bin", "a.bin"),),
                                         name="a.bin", deduplicate=False)
    (request,) = await repository.requests(transfer.id)
    first = await _attempt(repository, request.id, "parcel-a")
    async with database.get_db() as db:                                   # released by a route change
        await db.execute("UPDATE resolution_attempts SET state='released' WHERE id=?", (first.id,))
        await db.commit()
    before = await history(request.id)
    renewed = await _attempt(repository, request.id, "parcel-a", fail=True)
    retried = await _attempt(repository, request.id, "parcel-a")
    rows_ = await history(request.id)
    assert rows_[:len(before)] == before                                  # earlier rows untouched
    assert [(row["transition_kind"], row["transition_reason"]) for row in rows_] == [
        (None, None),                                                     # first resolution
        (None, None),                                                     # renewed after a release
        ("resolution_retry", "source_temporarily_unavailable"),           # retried after its failure
    ]
    events = [row["event_type"] for row in await rows(
        "SELECT event_type FROM event_journal WHERE transfer_id=? AND event_type IN "
        "('routing.route_started','routing.route_retried') ORDER BY id", (transfer.id,))]
    assert events == ["routing.route_started", "routing.route_started", "routing.route_retried"]   # one per attempt
    assert renewed.id != retried.id


@pytest.mark.asyncio
async def test_an_operator_switch_is_recorded_as_such(tmp_path, monkeypatch):
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    offer(lab.providers["parcel-b"], "chosen", ResourceState.AVAILABLE)
    await switch_root_provider(lab.engine, lab.transfer.id, "parcel-b", expected_provider_id="parcel-a")
    root = await lab.root()
    last = (await history(root.id))[-1]
    assert (last["operation"], last["transition_kind"], last["transition_reason"]) == (
        "operator_switch", "provider_change", "operator_switch")
    details = [row["detail"] for row in await rows(
        "SELECT detail FROM event_journal WHERE transfer_id=? AND event_type='routing.provider_changed'",
        (lab.transfer.id,))]
    assert details == ["Provider parcel-b; previously parcel-a (operator switch)"]
