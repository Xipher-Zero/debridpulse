"""TASK3d-3 corrective: the live manual-switch failure, false Queued state and
switch churn, on a decomposition of the live failure's size (>= 222 members).

The switch decides every knowable refusal before it touches a writer; its
fence is the durable pause intent that every admission reads plus a recovery
fence over exactly the artifacts holding a recovery claim (never one audit per
member); a writer that already succeeded is delivered through the canonical
success path rather than refusing the switch; the fence is lifted without an
operator Resume sweep. One reconcile cycle aggregates a transfer once, so a
RUNNING observation becomes durable within one cycle whatever the
decomposition size.
"""
from __future__ import annotations

import asyncio
import collections
import itertools
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fake_integrations import MemoryExecutor
from test_v113_collection_route_generic_closure import Clock
from test_v113_root_provider_switch import (
    MAGNET,
    magnet_provider,
    root_of,
    route_attempts,
    rows,
    running,
)

from db import database
from db.database import get_db
from transfers import manual_route_switch
from transfers.continuation import ContinuationCapability
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, TransferError
from transfers.manual_route_switch import switch_root_provider
from transfers.models import (
    ActiveCapacity,
    ExecutionActivity,
    ExecutionState,
    ResourceState,
    TransferProgress,
    TransferRequest,
)
from transfers.policy import TransferPolicy
from transfers.recovery_execution import RecoveryTrigger
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

MEMBERS = 222                     # the live failure's size: the large-scale bounds run at it
SMALL = 8                         # every logic-only proof runs at this size
FILES = [(f"e{index:03}.bin", f"Show/e{index:03}.bin", 4) for index in range(MEMBERS)]
# Every table a refused switch must leave byte-for-byte unchanged.
DURABLE = ("download_files", "execution_attempts", "artifact_material_state", "resolution_attempts",
           "route_attempt_provenance", "standby_resources", "provider_resources", "transfer_requests",
           "transfer_file_selections", "transfer_pause_intents", "artifact_recovery_state")


class ParkingExecutor(MemoryExecutor):
    """A memory copier that quiesces natively and resumes its own quiesced job,
    as aria2 does (``parks_on_pause``)."""

    capabilities = replace(MemoryExecutor.capabilities, continuation=MemoryExecutor.capabilities.continuation | {
        ContinuationCapability.NATIVE_QUIESCE, ContinuationCapability.NATIVE_PRIVATE_RESUME})


class WaitingFirstExecutor(ParkingExecutor):
    """Reports a started job as waiting first, as aria2 reports ``waiting``."""

    async def start(self, request, handle):
        started = await super().start(request, handle)
        self.jobs[handle.attempt_id] = replace(started, state=ExecutionState.QUEUED,   # waiting: no bytes, no rate
                                               progress=TransferProgress(4, 0, 0))
        return self.jobs[handle.attempt_id]

    def transferring(self, rate=1000):
        for attempt, job in list(self.jobs.items()):
            if job.state == ExecutionState.QUEUED:
                self.jobs[attempt] = replace(job, state=ExecutionState.RUNNING,
                                             progress=TransferProgress(4, 2, rate),
                                             activity=ExecutionActivity(network_active=True,
                                                                        bandwidth_reservation_required=True))


def offer(provider, *, native="x", files=FILES):
    result = provider.parcel(native, state=ResourceState.AVAILABLE, files=files)
    observed = replace(result.observation, request=TransferRequest("magnet", MAGNET))
    provider.resources[observed.resource.id] = observed
    provider.responses.append(replace(result, observation=observed))
    return observed.resource


async def big_lab(tmp_path, monkeypatch, *identities, executor_type=ParkingExecutor, width=5, members=SMALL):
    """A >= 222-member decomposed torrent root on ``identities[0]``, with its
    first writers running."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "corrective.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    providers = {identity: magnet_provider(identity) for identity in identities}
    for provider in providers.values():
        registry.register_provider(provider)
    executor = executor_type(repository.authorize_execution)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3, max_active_executions=width),
                            clock=Clock())
    await engine.initialize()
    offer(providers[identities[0]], files=FILES[:members])
    transfer = await engine.submit((TransferRequest("magnet", MAGNET, name="Show", selection_mode="all"),),
                                   name="Show", deduplicate=False)
    for _ in range(6):
        await engine.tick()
    assert len(await repository.artifacts(transfer.id)) == members
    return repository, engine, providers, executor, transfer


async def prepare_backup(repository, engine, provider, transfer_id):
    """``provider`` holds a prepared (bound) backup of the transfer's root."""
    root = await root_of(repository, transfer_id)
    prepared = offer(provider, native="prepared")
    provider.responses.clear()                                       # reuse or nothing: never a second create
    standby_id, _attempts = await repository.begin_standby(transfer_id, root.id, provider.descriptor.id, engine.clock())
    await repository.bind_standby(standby_id, transfer_id, prepared, ResourceState.AVAILABLE, engine.clock())
    return standby_id, prepared


async def durable_state():
    async with get_db() as db:
        return {table: [tuple(row.values()) for row in await db.fetchall(f"SELECT * FROM {table} ORDER BY 1")]
                for table in DURABLE}


async def event_counts():
    async with get_db() as db:
        kinds = {row["kind"]: row["n"] for row in await db.fetchall(
            "SELECT kind,COUNT(*) AS n FROM application_events GROUP BY kind")}
        legacy = (await db.fetchone("SELECT COUNT(*) AS n FROM events"))["n"]
    return collections.Counter(kinds), legacy


class Probe:
    """Counts every owner a switch may reach, and the events it caused."""

    def __init__(self, monkeypatch, engine):
        self.calls, self.claims, self.outcomes, self.retired = collections.Counter(), collections.Counter(), [], []
        repository = engine.repository
        retire = manual_route_switch.retire_writer

        async def retire_writer(*args, **kwargs):
            self.calls["retire_writer"] += 1
            result = await retire(*args, **kwargs)
            self.retired.append(result)
            return result

        monkeypatch.setattr(manual_route_switch, "retire_writer", retire_writer)
        replace_root_route = repository.replace_root_route

        async def replaced(*args, **kwargs):
            outcome = await replace_root_route(*args, **kwargs)
            self.outcomes.append(outcome)
            return outcome

        monkeypatch.setattr(repository, "replace_root_route", replaced)
        claim_recovery = repository.claim_recovery

        async def claim(artifact_id, trigger, *args, **kwargs):
            self.claims[RecoveryTrigger(trigger).value] += 1
            return await claim_recovery(artifact_id, trigger, *args, **kwargs)

        monkeypatch.setattr(repository, "claim_recovery", claim)
        for owner, name in ((repository, "set_pause_and_fence"), (repository, "pause_intent"),
                            (engine, "pause"), (engine, "resume"), (engine, "recover_artifact")):
            original = getattr(owner, name)

            async def counted(*args, _original=original, _name=name, **kwargs):
                self.calls[_name] += 1
                return await _original(*args, **kwargs)

            monkeypatch.setattr(owner, name, counted)

    async def switch(self, engine, transfer_id, provider_id, expected):
        self.before = await event_counts()
        started = time.perf_counter()
        try:
            return await switch_root_provider(engine, transfer_id, provider_id, expected_provider_id=expected)
        finally:
            self.elapsed = time.perf_counter() - started
            after = await event_counts()
            self.events = after[0] - self.before[0]
            self.legacy_events = after[1] - self.before[1]

    @property
    def event_total(self):
        return sum(self.events.values()) + self.legacy_events

    def report(self, label, *, members, writers):
        print(f"\n[8.10 {label}] members={members} active_writers={writers} elapsed={self.elapsed:.3f}s "
              f"calls={dict(self.calls)} claims={dict(self.claims)} replace={self.outcomes} "
              f"events={dict(self.events)} legacy_events={self.legacy_events} total={self.event_total}")


async def settle(engine, ticks=6):
    for _ in range(ticks):
        engine.clock.now += 30
        await engine.tick()


async def operator_switches(request_id):
    return [attempt for attempt in await route_attempts(request_id) if attempt[2] == "operator_switch"]


# -- 8.1 / 8.10: a prepared provider switch over the live failure's size commits exactly once ---------------------

async def test_a_prepared_switch_over_222_members_commits_once_promotes_once_and_sweeps_nothing(tmp_path, monkeypatch):
    repository, engine, providers, executor, transfer = await big_lab(tmp_path, monkeypatch, "parcel-a", "parcel-b", members=MEMBERS)
    live = [artifact for artifact in await repository.artifacts(transfer.id) if artifact.execution is not None]
    for artifact in live[:2]:
        executor.finish(artifact.execution)                          # completed members
    await engine.tick()
    artifacts = await repository.artifacts(transfer.id)
    completed = [artifact for artifact in artifacts if artifact.state == "completed"]
    active = [artifact for artifact in artifacts if artifact.execution is not None and artifact.state != "completed"]
    assert completed and active and any(artifact.state == "queued" and artifact.execution is None
                                        for artifact in artifacts)
    partial = active[0]
    Path(partial.target).parent.mkdir(parents=True, exist_ok=True)
    Path(partial.target).write_bytes(b"pa")                          # retained valid material
    old_writers = running(executor)
    standby_id, prepared = await prepare_backup(repository, engine, providers["parcel-b"], transfer.id)
    root = await root_of(repository, transfer.id)

    probe = Probe(monkeypatch, engine)
    await probe.switch(engine, transfer.id, "parcel-b", "parcel-a")
    probe.report("prepared switch", members=MEMBERS, writers=len(active))

    assert probe.outcomes == ["replaced"]                            # the atomic replacement committed
    assert not (running(executor) & old_writers), "an old writer survived the route commit"
    assert probe.calls["retire_writer"] == len(active)               # exactly the live writers
    assert sum(probe.claims.values()) == 0 and probe.calls["recover_artifact"] == 0, "the switch ran a recovery sweep"
    assert probe.calls["pause"] == probe.calls["resume"] == 0        # never the operator Pause/Resume pair
    assert probe.events["recovery_audit"] <= 2 * len(active)         # scales with live writers, not members
    assert probe.event_total <= 24 + 2 * len(active)
    assert len(await operator_switches(root.id)) == 1

    await settle(engine)
    root = await root_of(repository, transfer.id)
    assert root.resource.id == prepared.id                           # the prepared resource, promoted
    assert [call for call in providers["parcel-b"].calls if call == ("resolve", MAGNET)] == []
    promoted = [item for item in await repository.standbys(transfer.id) if item.get("promoted_at")]
    assert [item["id"] for item in promoted] == [standby_id]
    attempts = await route_attempts(root.id)
    assert attempts[0][:2] == ("parcel-a", "released") and len(await operator_switches(root.id)) == 1
    assert attempts[-1][:3] == ("parcel-b", "succeeded", "operator_switch")
    after = {artifact.id: artifact for artifact in await repository.artifacts(transfer.id)}
    assert set(after) == {artifact.id for artifact in artifacts}     # the same logical targets
    for artifact in completed:
        assert after[artifact.id].state == "completed" and Path(artifact.target).read_bytes() == b"done"
    assert Path(partial.target).exists()
    writers = [artifact for artifact in after.values() if artifact.execution and artifact.state != "completed"]
    assert len({artifact.id for artifact in writers}) == len(writers) == len(running(executor))
    assert {candidate.provider_id for artifact in writers for candidate in artifact.candidates} == {"parcel-b"}
    selection = await rows("""SELECT s.manifest_committed_at,s.continuity FROM transfer_file_selections s
        JOIN provider_resources r ON r.id=s.provider_resource_id WHERE s.request_id=? AND r.resource_key=?""",
                           (root.id, prepared.id))
    assert selection and selection[0]["manifest_committed_at"] and selection[0]["continuity"] == "proven"


# -- 8.2: a retired writer is not live; a genuinely live writer still refuses --------------------------------------

@pytest.mark.parametrize("state", ["prepared", "queued", "running", "paused", "unknown"])
async def test_only_a_retired_writer_leaves_the_writer_live_gate(tmp_path, monkeypatch, state):
    repository, engine, _providers, _executor, transfer = await big_lab(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    root = await root_of(repository, transfer.id)
    latest = await repository.latest_root_route(root.id)
    live = [artifact for artifact in await repository.artifacts(transfer.id) if artifact.execution is not None]
    await repository.pause_intent(transfer.id, True)                 # nothing re-admits meanwhile
    from transfers.candidate_activation import retire_writer
    for artifact in live:
        candidate = artifact.candidates[artifact.selected]
        assert not (await retire_writer(engine, artifact, candidate, artifact, candidate,
                                        boundary="operator_route_switch")).reason
        assert await repository.detach_retired_writer(artifact.id, artifact.execution.attempt_id, state="queued")
    held = live[0].execution.attempt_id
    async with get_db() as db:                                       # one attempt is (made) genuinely live again
        await db.execute("UPDATE execution_attempts SET state=?,authorized=1 WHERE id=?", (state, held))
        await db.execute("UPDATE download_files SET execution_attempt_id=? WHERE id=?", (held, live[0].id))
        await db.commit()
    assert await repository.replace_root_route(root.id, expected_attempt_id=str(latest["id"]),
                                               expected_provider_id="parcel-a",
                                               target_provider_id="parcel-b") == "writer_live"
    assert (await repository.latest_root_route(root.id))["id"] == latest["id"]
    async with get_db() as db:                                       # retired: terminal and detached
        await db.execute("UPDATE execution_attempts SET state='cancelled' WHERE id=?", (held,))
        await db.commit()
    assert await repository.detach_retired_writer(live[0].id, held, state="queued")
    assert await repository.replace_root_route(root.id, expected_attempt_id=str(latest["id"]),
                                               expected_provider_id="parcel-a",
                                               target_provider_id="parcel-b") == "replaced"


# -- 8.3a: every knowable refusal is decided before any writer is touched ----------------------------------------

async def _stale(repository, engine, providers, transfer):
    return "parcel-b", "parcel-x", Category.RESOURCE_STATE_CONFLICT


async def _current(repository, engine, providers, transfer):
    return "parcel-a", "parcel-a", Category.INVALID_REQUEST


async def _disabled(repository, engine, providers, transfer):
    providers["parcel-b"].descriptor = replace(providers["parcel-b"].descriptor, enabled=False)
    return "parcel-b", "parcel-a", Category.PROVIDER_UNAVAILABLE


async def _not_entitled(repository, engine, providers, transfer):
    providers["parcel-b"].entitlement_for = lambda request: False
    return "parcel-b", "parcel-a", Category.ACCOUNT_LIMITED


async def _not_a_claimant(repository, engine, providers, transfer):
    providers["parcel-b"].descriptor = replace(providers["parcel-b"].descriptor,
                                               request_types=frozenset({"parcel-member"}))
    return "parcel-b", "parcel-a", Category.PROVIDER_UNAVAILABLE


async def _full(request):
    return ActiveCapacity(1, 1)


async def _capacity(repository, engine, providers, transfer):
    providers["parcel-b"].active_capacity = _full
    return "parcel-b", "parcel-a", Category.CONCURRENCY_LIMITED


async def _unpromotable_backup(repository, engine, providers, transfer):
    """A bound backup whose resource is known gone is not a prepared target:
    the target is cold, so its full capacity refuses."""
    _standby_id, prepared = await prepare_backup(repository, engine, providers["parcel-b"], transfer.id)
    async with get_db() as db:
        await db.execute("UPDATE provider_resources SET state='absent' WHERE resource_key=?", (prepared.id,))
        await db.commit()
    providers["parcel-b"].active_capacity = _full
    return "parcel-b", "parcel-a", Category.CONCURRENCY_LIMITED


async def _preparing_backup(repository, engine, providers, transfer):
    """Transfer 516: the target's backup is bound, but its provider is still
    acquiring the content (resource PREPARING, no manifest). It is not a
    switch target at all: refused before anything is fenced."""
    root = await root_of(repository, transfer.id)
    standby_id, _attempts = await repository.begin_standby(transfer.id, root.id, "parcel-b", engine.clock())
    acquiring = providers["parcel-b"].parcel("acquiring", state=ResourceState.PREPARING)
    await repository.bind_standby(standby_id, transfer.id, acquiring.observation.resource, ResourceState.PREPARING,
                                  engine.clock())
    status = await manual_route_switch.route_providers(engine, transfer.id)
    assert {entry["provider_id"]: (entry["status"], entry["selectable"]) for entry in status["providers"]}[
        "parcel-b"] == ("preparing", False)
    return "parcel-b", "parcel-a", Category.RESOURCE_STATE_CONFLICT


async def _root_busy(repository, engine, providers, transfer):
    root = await root_of(repository, transfer.id)
    async with get_db() as db:                                       # a resolution of the root is in flight
        await db.execute("UPDATE transfer_requests SET state='resolving' WHERE id=?", (root.id,))
        await db.commit()
    return "parcel-b", "parcel-a", Category.RESOURCE_STATE_CONFLICT


@pytest.mark.parametrize("arrange", [_stale, _current, _disabled, _not_entitled, _not_a_claimant, _capacity,
                                     _unpromotable_backup, _preparing_backup, _root_busy],
                         ids=lambda item: item.__name__.strip("_"))
async def test_a_knowable_refusal_touches_no_writer_and_changes_nothing(tmp_path, monkeypatch, arrange):
    members = MEMBERS if arrange is _stale else SMALL       # O(1) proven once at full size; zero-touch at any size
    repository, engine, providers, executor, transfer = await big_lab(tmp_path, monkeypatch, "parcel-a", "parcel-b",
                                                                      members=members)
    target, expected, category = await arrange(repository, engine, providers, transfer)
    writers = running(executor)
    before = await durable_state()

    probe = Probe(monkeypatch, engine)
    with pytest.raises(TransferError) as refused:
        await probe.switch(engine, transfer.id, target, expected)
    probe.report(f"preflight refusal {arrange.__name__.strip('_')}", members=members, writers=len(writers))

    assert refused.value.error.category == category
    assert probe.calls["retire_writer"] == 0, "a knowable refusal retired a writer"
    assert probe.calls["set_pause_and_fence"] == probe.calls["pause_intent"] == probe.calls["pause"] == 0
    assert probe.calls["resume"] == probe.calls["recover_artifact"] == 0 and sum(probe.claims.values()) == 0
    assert probe.outcomes == []                                      # no replacement was even attempted
    assert probe.event_total == 0                                    # bound: <= 12 attributable events
    assert running(executor) == writers
    assert await durable_state() == before                           # route, material, pause: unchanged


# -- 8.3b: a genuinely unpredictable refusal after fencing cleans up without a sweep -----------------------------

class UnconfirmedCancelExecutor(ParkingExecutor):
    """One designated writer's stop can never be confirmed."""

    unconfirmed = None

    async def cancel(self, handle):
        if handle.attempt_id == self.unconfirmed:
            self.calls.append(("cancel", handle))
            return replace(await self.observe(handle), state=ExecutionState.UNKNOWN)
        return await super().cancel(handle)


async def test_an_unpredictable_refusal_after_fencing_restores_the_old_route_without_a_resume_sweep(tmp_path, monkeypatch):
    repository, engine, providers, executor, transfer = await big_lab(
        tmp_path, monkeypatch, "parcel-a", "parcel-b", executor_type=UnconfirmedCancelExecutor, members=MEMBERS)
    active = [artifact for artifact in await repository.artifacts(transfer.id) if artifact.execution is not None]
    executor.unconfirmed = active[-1].execution.attempt_id           # discovered only by trying to stop it
    Path(active[0].target).parent.mkdir(parents=True, exist_ok=True)
    Path(active[0].target).write_bytes(b"pa")
    root = await root_of(repository, transfer.id)
    resource = root.resource
    await prepare_backup(repository, engine, providers["parcel-b"], transfer.id)

    probe = Probe(monkeypatch, engine)
    with pytest.raises(TransferError) as refused:
        await probe.switch(engine, transfer.id, "parcel-b", "parcel-a")
    retired = sum(1 for item in probe.retired if not item.reason)
    probe.report("post-fence refusal", members=MEMBERS, writers=len(active))

    assert refused.value.error.category == Category.RESOURCE_STATE_CONFLICT
    assert probe.calls["retire_writer"] == len(active) and retired == len(active) - 1
    assert await operator_switches(root.id) == []
    assert (await root_of(repository, transfer.id)).resource == resource
    assert await repository.bound_route_provider(root.id) == "parcel-a"
    assert not (await repository.get(transfer.id)).paused            # no switch fence left behind
    assert probe.claims.get(RecoveryTrigger.RESUME.value, 0) == 0 and probe.calls["recover_artifact"] == 0
    assert probe.calls["resume"] == 0
    assert probe.event_total <= 24 + 2 * retired
    assert Path(active[0].target).read_bytes() == b"pa"              # valid material retained

    await settle(engine, ticks=3)                                    # the old route simply continues
    after = await repository.artifacts(transfer.id)
    writers = [artifact for artifact in after if artifact.execution and artifact.state != "completed"]
    assert writers and {candidate.provider_id for artifact in writers
                        for candidate in artifact.candidates} == {"parcel-a"}
    assert not [artifact for artifact in after if artifact.state == "paused"]


async def test_a_refused_switch_never_clears_an_operator_pause(tmp_path, monkeypatch):
    repository, engine, _providers, executor, transfer = await big_lab(
        tmp_path, monkeypatch, "parcel-a", "parcel-b", executor_type=UnconfirmedCancelExecutor)
    await engine.pause(transfer.id)
    active = [artifact for artifact in await repository.artifacts(transfer.id) if artifact.execution is not None]
    executor.unconfirmed = active[0].execution.attempt_id if active else None
    async with get_db() as db:
        await db.execute("UPDATE transfer_requests SET state='resolving' WHERE parent_id IS NULL AND transfer_id=?",
                         (transfer.id,))
        await db.commit()
    with pytest.raises(TransferError):
        await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    assert (await repository.get(transfer.id)).paused


# -- the writer that already succeeded during the fence is delivered, never a refusal ----------------------------

async def test_a_writer_succeeding_during_the_fence_is_delivered_and_the_switch_commits_once(tmp_path, monkeypatch):
    repository, engine, providers, executor, transfer = await big_lab(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    active = [artifact for artifact in await repository.artifacts(transfer.id) if artifact.execution is not None]
    winner = active[0]
    await prepare_backup(repository, engine, providers["parcel-b"], transfer.id)
    root = await root_of(repository, transfer.id)
    fence = engine.repository.set_pause_and_fence

    async def fenced_then_native_success(transfer_id, paused, **kwargs):
        await fence(transfer_id, paused, **kwargs)
        if paused:                                                   # the native writer crosses SUCCEEDED now
            executor.finish(winner.execution)

    monkeypatch.setattr(engine.repository, "set_pause_and_fence", fenced_then_native_success)
    probe = Probe(monkeypatch, engine)
    await probe.switch(engine, transfer.id, "parcel-b", "parcel-a")

    assert probe.outcomes == ["replaced"] and len(await operator_switches(root.id)) == 1
    assert any(item.reason == "writer_already_succeeded" for item in probe.retired)
    assert not running(executor), "an old writer survived the route commit"
    delivered = next(artifact for artifact in await repository.artifacts(transfer.id) if artifact.id == winner.id)
    assert delivered.state == "completed" and Path(winner.target).read_bytes() == b"done"
    provenance = await rows("SELECT delivered,outcome FROM execution_attempt_provenance WHERE execution_attempt_id=?",
                            (winner.execution.attempt_id,))
    assert [tuple(row.values()) for row in provenance] == [(1, "completed")]
    starts = len([call for call in executor.calls if call[0] == "start"])

    await settle(engine)
    final = next(artifact for artifact in await repository.artifacts(transfer.id) if artifact.id == winner.id)
    assert final.state == "completed" and Path(winner.target).read_bytes() == b"done"
    assert not [call for call in executor.calls[starts:] if call[0] == "start"
                and call[1].correlation["destination"] == winner.target], "a duplicate successor writer started"
    assert len(await operator_switches(root.id)) == 1


# -- the fence invalidates a recovery claim acquired before it ---------------------------------------------------

async def test_a_recovery_claim_held_across_the_switch_fence_can_commit_nothing(tmp_path, monkeypatch):
    repository, engine, providers, _executor, transfer = await big_lab(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    engine.policy = replace(engine.policy, max_active_executions=MEMBERS)   # a free slot: admission is reachable
    victim = next(artifact for artifact in await repository.artifacts(transfer.id)
                  if artifact.execution is None and artifact.state == "queued")
    await prepare_backup(repository, engine, providers["parcel-b"], transfer.id)
    at_barrier, release_claim = asyncio.Event(), asyncio.Event()
    authorization = repository.materialization_authorization

    async def barrier(artifact):
        if artifact.id == victim.id and not release_claim.is_set():  # the claim's productive step, outside every lock
            at_barrier.set()
            await release_claim.wait()
        return await authorization(artifact)

    monkeypatch.setattr(repository, "materialization_authorization", barrier)
    claims = []
    claim_recovery = repository.claim_recovery

    async def recorded(*args, **kwargs):
        claim = await claim_recovery(*args, **kwargs)
        if claim is not None and claim.artifact_id == victim.id:
            claims.append(claim)
        return claim

    monkeypatch.setattr(repository, "claim_recovery", recorded)
    recovering = asyncio.create_task(engine.recover_artifact(victim, trigger=RecoveryTrigger.AUTO_RETRY))
    await asyncio.wait_for(at_barrier.wait(), 5)
    old = claims[0]

    fenced, release_switch = asyncio.Event(), asyncio.Event()
    commit = repository.replace_root_route

    async def held(*args, **kwargs):                                  # the switch is fenced, not yet committed
        fenced.set()
        await release_switch.wait()
        return await commit(*args, **kwargs)

    monkeypatch.setattr(repository, "replace_root_route", held)
    switching = asyncio.create_task(switch_root_provider(engine, transfer.id, "parcel-b",
                                                         expected_provider_id="parcel-a"))
    await asyncio.wait_for(fenced.wait(), 5)
    assert not await repository.recovery_claim_current(old)          # the fence invalidated the old owner
    release_claim.set()
    await asyncio.wait_for(recovering, 5)

    current = next(artifact for artifact in await repository.artifacts(transfer.id) if artifact.id == victim.id)
    assert current.execution is None, "a fenced recovery claim authorized an execution"
    assert current.selected == victim.selected and current.candidates == victim.candidates
    assert not await repository.renew_recovery_claim(old, engine.clock())
    assert not await repository.record_recovery_quiescence(old, reason="paused", wake_condition="resume")
    assert not await repository.retire_stale_materialization_if_claim_current(
        old, victim.id, victim.transfer_id, victim.request_id)
    assert not await repository.finish_recovery_claim(old, action="reconcile", reason="stale")
    release_switch.set()
    await asyncio.wait_for(switching, 10)
    claimed = await rows("SELECT COUNT(*) AS n FROM artifact_recovery_state WHERE transfer_id=? "
                         "AND recovery_claim_token IS NOT NULL", (transfer.id,))
    assert claimed[0]["n"] == 0


# -- 8.4-8.7: one aggregation per cycle; running work is Downloading with live progress and speed -----------------

async def test_one_reconcile_cycle_aggregates_the_transfer_once_and_persists_running_work(tmp_path, monkeypatch):
    repository, engine, _providers, executor, transfer = await big_lab(
        tmp_path, monkeypatch, "parcel-a", "parcel-b", executor_type=WaitingFirstExecutor, members=MEMBERS)
    started = [artifact for artifact in await repository.artifacts(transfer.id) if artifact.execution is not None]
    assert started and (await repository.get(transfer.id)).state.value == "queued"   # aria2: waiting at start
    executor.finish(started[0].execution)
    await engine.reconcile_executions()                              # one member completed
    executor.transferring()                                          # natively the rest now move bytes

    aggregations = collections.Counter()
    aggregate = engine._aggregate

    async def counted(transfer_id):
        aggregations[transfer_id] += 1
        return await aggregate(transfer_id)

    monkeypatch.setattr(engine, "_aggregate", counted)
    began = time.perf_counter()
    await engine.reconcile_executions()
    print(f"\n[8.4 cycle] members={MEMBERS} aggregations={dict(aggregations)} "
          f"elapsed={time.perf_counter() - began:.3f}s")
    assert aggregations[transfer.id] == 1, "a reconcile cycle aggregated the transfer once per member"

    live = [artifact for artifact in await repository.artifacts(transfer.id) if artifact.execution is not None
            and artifact.state != "completed"]
    executions = {row["id"]: row for row in await rows(
        "SELECT id,state,authorized FROM execution_attempts WHERE transfer_id=?", (transfer.id,))}
    assert live and all(executions[artifact.execution.attempt_id]["state"] == "running" for artifact in live)
    assert all(artifact.state == "downloading" for artifact in live)                     # 8.4 artifact truth
    states = collections.Counter(artifact.state for artifact in await repository.artifacts(transfer.id))
    assert states["completed"] and states["queued"] and states["downloading"]           # 8.5 mixed members
    projected = next(item for item in await repository.active() if item.id == transfer.id)
    assert projected.state.value == "downloading" and projected.active_execution_progress   # 8.4 / 8.6
    assert await repository.occupied_execution_slots(engine.clock()) == len(live)        # 8.6 slot truth
    listed = await bounded_row(engine, transfer.id)
    assert listed["status"] == "downloading" and listed.get("active_execution_progress")
    assert engine.throughput.current() > 0                                              # 8.7 telemetry

    for artifact in live:
        executor.finish(artifact.execution)
    await engine.reconcile_executions()
    await engine.sample_throughput()
    assert engine.throughput.current() == 0                                             # work stopped: zero


async def bounded_row(engine, transfer_id):
    from types import SimpleNamespace

    from api.operational_downloads import list_operational_torrents
    application = SimpleNamespace(engine=engine, repository=engine.repository, definitions={})
    listed = await list_operational_torrents(status=None, search=None, limit=0, offset=0, order=None,
                                             application=application)
    return next(item for item in (listed["items"] if isinstance(listed, dict) else listed) if item["id"] == transfer_id)


# -- 8.9: the live overlay and a fresh reload agree --------------------------------------------------------------

async def test_the_live_overlay_and_a_fresh_reload_agree_on_running_work(tmp_path, monkeypatch):
    _repository, engine, _providers, executor, transfer = await big_lab(
        tmp_path, monkeypatch, "parcel-a", "parcel-b", executor_type=WaitingFirstExecutor)
    from application.service import ApplicationService
    application = ApplicationService(engine)
    published = []

    async def publish(kind, payload):
        published.append((kind, payload))

    monkeypatch.setattr("application.service.publish", publish)
    executor.transferring(rate=2048)
    await application.reconcile_executions()
    overlay = [item for kind, payload in published if kind == "torrent_updated"
               for item in payload["items"] if item["id"] == transfer.id]
    assert overlay, "running work was not published to the live page"
    fresh = await bounded_row(engine, transfer.id)
    live = overlay[-1]
    assert live["status"] == fresh["status"] == "downloading"
    assert live["progress"] == pytest.approx(float(fresh["progress"]))
    assert live["active_execution_progress"] == pytest.approx(float(fresh["active_execution_progress"]))
    assert fresh.get("route_provider_id") == "parcel-a"
    assert (await application.execution_throughput())["download_bytes_per_second"] > 0


# -- A: completion verification never occupies the reconcile cycle ------------------------------------------------

class TimedLock(asyncio.Lock):
    """``_execution_cycle_lock`` that accounts how long it is held."""

    def __init__(self):
        super().__init__()
        self.held, self._since = 0.0, None

    async def acquire(self):
        await super().acquire()
        self._since = time.monotonic()
        return True

    def release(self):
        self.held += time.monotonic() - self._since
        super().release()


async def live_rows(transfer_id):
    return await rows("""SELECT f.id,f.status,e.state FROM download_files f JOIN execution_attempts e
        ON e.id=f.execution_attempt_id WHERE f.torrent_id=? AND e.state NOT IN ('succeeded','cancelled')""",
                      (transfer_id,))


async def test_three_succeeded_writers_verify_concurrently_off_the_cycle_lock_while_observation_continues(
        tmp_path, monkeypatch):
    repository, engine, _providers, executor, transfer = await big_lab(
        tmp_path, monkeypatch, "parcel-a", "parcel-b", executor_type=WaitingFirstExecutor, width=3)
    assert engine.policy.adoption_stability_seconds == 3.25          # the rule itself is unchanged
    finished = [artifact for artifact in await repository.artifacts(transfer.id) if artifact.execution is not None]
    assert len(finished) == 3
    for artifact in finished:
        executor.finish(artifact.execution)
    lock = engine._execution_cycle_lock = TimedLock()

    overlap = []

    async def watch_lock():                                           # is the lock ever held while verifying?
        while not cycle.done():
            if engine._verifications and lock.locked():
                overlap.append(time.monotonic())
            await asyncio.sleep(0.01)

    began = time.monotonic()
    cycle = asyncio.create_task(engine.reconcile_executions())       # dispatches the next three writers too
    watcher = asyncio.create_task(watch_lock())
    samples, moved = [], None
    while not cycle.done():
        sampled = time.monotonic()
        await engine.sample_throughput()                             # the fast observation, at its cadence
        samples.append((sampled, time.monotonic() - sampled))
        if moved is None and engine._verifications and not lock.locked():   # verifications in flight
            executor.transferring(rate=3000)                         # another writer moves bytes mid-verification
            await engine.sample_throughput()
            moved = await live_rows(transfer.id)
            projected = next(item for item in await repository.active() if item.id == transfer.id)
            moved = (moved, projected.state.value, engine.throughput.current())
        await asyncio.sleep(0.5)
    await cycle
    await watcher
    elapsed = time.monotonic() - began
    gaps = [later[0] - earlier[0] for earlier, later in itertools.pairwise(samples)]
    print(f"\n[A verification] writers=3 stability=3.25s elapsed={elapsed:.2f}s cycle_lock_held={lock.held:.2f}s "
          f"samples={len(samples)} max_sample_gap={max(gaps):.2f}s max_sample_cost={max(c for _t, c in samples):.3f}s")

    assert not overlap, "the reconcile lock was held while a stability interval was being waited out"
    assert elapsed < lock.held + 3.25 + 1.5, "three verifications serialized"
    assert max(gaps) <= 0.6 + 0.25 and max(cost for _sampled, cost in samples) < 0.5
    rows_moved, state, speed = moved
    assert rows_moved and all(row["state"] == "running" and row["status"] == "downloading" for row in rows_moved)
    assert state == "downloading" and speed > 0                      # seen while the others were verifying
    done = {artifact.id: artifact.state for artifact in await repository.artifacts(transfer.id)}
    assert all(done[artifact.id] == "completed" for artifact in finished)


# -- B: one fast observation makes a running writer durable, Downloading, with progress and speed -----------------

async def test_one_fast_observation_projects_a_running_writer_without_any_reconcile_cycle(tmp_path, monkeypatch):
    repository, engine, _providers, executor, transfer = await big_lab(
        tmp_path, monkeypatch, "parcel-a", "parcel-b", executor_type=WaitingFirstExecutor, width=3, members=MEMBERS)
    writers = [artifact for artifact in await repository.artifacts(transfer.id) if artifact.execution is not None]
    assert len(writers) == 3 and (await repository.get(transfer.id)).state.value == "queued"
    observed_handles = []
    observe_batch = engine._observe_batch

    async def counted(executor_, handles):
        observed_handles.append(len(handles))
        return await observe_batch(executor_, handles)

    monkeypatch.setattr(engine, "_observe_batch", counted)
    probe = Probe(monkeypatch, engine)
    readers = {name: getattr(repository, name) for name in ("artifacts", "active", "occupied_execution_slots")}

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("the fast observation walked the decomposition")

    for name in readers:
        setattr(repository, name, forbidden)

    executor.transferring(rate=4096)                                  # native waiting -> running
    async with engine._execution_cycle_lock:                          # no reconcile cycle can help
        began = time.monotonic()
        await engine.sample_throughput()                              # ONE fast observation
        cost = time.monotonic() - began
    for name, reader in readers.items():
        setattr(repository, name, reader)
    print(f"\n[B fast observation] members={MEMBERS} live_handles={sum(observed_handles)} cost={cost:.3f}s")

    assert observed_handles == [3]                                    # live width, not 222 members
    executions = {row["id"]: row["state"] for row in await rows(
        "SELECT id,state FROM execution_attempts WHERE transfer_id=?", (transfer.id,))}
    assert all(executions[artifact.execution.attempt_id] == "running" for artifact in writers)
    current = {artifact.id: artifact for artifact in await repository.artifacts(transfer.id)}
    assert all(current[artifact.id].state == "downloading" for artifact in writers)
    projected = next(item for item in await repository.active() if item.id == transfer.id)
    assert projected.state.value == "downloading" and projected.active_execution_progress > 0
    assert engine.throughput.current() == 3 * 4096
    assert sum(probe.claims.values()) == 0 and probe.calls["recover_artifact"] == 0
    assert (await bounded_row(engine, transfer.id))["status"] == "downloading"

    for artifact in writers:                                          # ~5 s later the writers finish
        executor.finish(artifact.execution)
    await engine.reconcile_executions()
    done = {artifact.id: artifact.state for artifact in await repository.artifacts(transfer.id)}
    assert all(done[artifact.id] == "completed" for artifact in writers)
    assert all(Path(artifact.target).read_bytes() == b"done" for artifact in writers)


async def test_the_fast_observation_never_acts_on_terminal_or_failed_writers(tmp_path, monkeypatch):
    repository, engine, _providers, executor, transfer = await big_lab(
        tmp_path, monkeypatch, "parcel-a", "parcel-b", executor_type=WaitingFirstExecutor, width=3)
    writers = [artifact for artifact in await repository.artifacts(transfer.id) if artifact.execution is not None]
    failed, finished = writers[0], writers[1]
    executor.jobs[failed.execution.attempt_id] = replace(executor.jobs[failed.execution.attempt_id],
                                                         state=ExecutionState.FAILED)
    executor.finish(finished.execution)
    ended = (failed.execution.attempt_id, finished.execution.attempt_id)

    async def their_rows():
        return (await rows("SELECT * FROM execution_attempts WHERE id IN (?,?) ORDER BY id", ended),
                await rows("SELECT * FROM download_files WHERE id IN (?,?) ORDER BY id", (failed.id, finished.id)),
                await rows("SELECT * FROM artifact_recovery_state WHERE artifact_id IN (?,?) ORDER BY artifact_id",
                           (failed.id, finished.id)))

    before = await their_rows()
    probe = Probe(monkeypatch, engine)
    await engine.sample_throughput()
    assert sum(probe.claims.values()) == 0 and probe.calls["recover_artifact"] == 0
    assert probe.calls["retire_writer"] == 0
    assert await their_rows() == before                               # terminal truth stays the cycle's


async def test_the_live_page_learns_of_running_work_from_the_fast_observation_alone(tmp_path, monkeypatch):
    _repository, engine, _providers, executor, transfer = await big_lab(
        tmp_path, monkeypatch, "parcel-a", "parcel-b", executor_type=WaitingFirstExecutor, width=3)
    from application.service import ApplicationService
    application = ApplicationService(engine)
    published = []

    async def publish(kind, payload):
        published.append((kind, payload))

    monkeypatch.setattr("application.service.publish", publish)
    executor.transferring(rate=2048)
    await application.observe_live_executions()
    overlay = [item for kind, payload in published if kind == "torrent_updated"
               for item in payload["items"] if item["id"] == transfer.id]
    assert overlay and not [kind for kind, _payload in published if kind == "stats_changed"]
    fresh = await bounded_row(engine, transfer.id)
    assert overlay[-1]["status"] == fresh["status"] == "downloading"
    assert overlay[-1]["active_execution_progress"] == pytest.approx(float(fresh["active_execution_progress"]))
    assert (await application.execution_throughput())["download_bytes_per_second"] == 3 * 2048


# -- the reconcile cycle's older batched observation never overwrites newer fast-observation truth -----------------

MIB = 1024 * 1024


def native(executor, handle, state, completed, rate=0):
    job = executor.jobs[handle.attempt_id]
    executor.jobs[handle.attempt_id] = replace(
        job, state=state, progress=TransferProgress(16 * MIB, completed, rate),
        activity=ExecutionActivity(network_active=state == ExecutionState.RUNNING,
                                   bandwidth_reservation_required=True))


async def durable(attempt_id, artifact_id, transfer_id):
    execution = (await rows("SELECT state,progress FROM execution_attempts WHERE id=?", (attempt_id,)))[0]
    status = (await rows("SELECT status FROM download_files WHERE id=?", (artifact_id,)))[0]["status"]
    parent = (await rows("SELECT status FROM torrents WHERE id=?", (transfer_id,)))[0]["status"]
    return execution["state"], json.loads(execution["progress"] or "{}").get("completed_bytes", 0), status, parent


@pytest.mark.parametrize("older", [(ExecutionState.QUEUED, 0), (ExecutionState.RUNNING, 1 * MIB)],
                         ids=["queued-then-running", "running-lower-then-higher"])
async def test_an_older_reconcile_observation_never_overwrites_newer_fast_observation_truth(
        tmp_path, monkeypatch, older):
    repository, engine, _providers, executor, transfer = await big_lab(
        tmp_path, monkeypatch, "parcel-a", "parcel-b", executor_type=WaitingFirstExecutor, width=1, members=3)
    writer = next(artifact for artifact in await repository.artifacts(transfer.id) if artifact.execution is not None)
    handle = writer.execution
    native(executor, handle, *older)                                   # observation A, as the cycle will see it
    held, release = asyncio.Event(), asyncio.Event()
    result = engine._execution_result

    async def barrier(artifact, *args, **kwargs):
        if artifact.id == writer.id and not release.is_set():          # A obtained, not yet persisted
            held.set()
            await release.wait()
        return await result(artifact, *args, **kwargs)

    monkeypatch.setattr(engine, "_execution_result", barrier)
    cycle = asyncio.create_task(engine.reconcile_executions())
    await asyncio.wait_for(held.wait(), 10)

    native(executor, handle, ExecutionState.RUNNING, 8 * MIB, rate=2 * MIB)   # native truth advances: B
    await engine.sample_throughput()                                   # the fast observation persists B
    assert await durable(handle.attempt_id, writer.id, transfer.id) == ("running", 8 * MIB, "downloading",
                                                                         "downloading")

    release.set()                                                      # the cycle resumes with A
    await asyncio.wait_for(cycle, 30)
    state, completed, status, parent = await durable(handle.attempt_id, writer.id, transfer.id)
    assert (state, status, parent) == ("running", "downloading", "downloading"), "A overwrote newer truth"
    assert completed >= 8 * MIB, "completed bytes moved backwards"
    projected = next(item for item in await repository.active() if item.id == transfer.id)
    assert projected.state.value == "downloading" and projected.active_execution_progress


# -- a verification that went stale while it waited publishes nothing when it raises -------------------------------

async def test_a_stale_verification_that_raises_writes_no_error_and_no_failure_outcome(tmp_path, monkeypatch):
    repository, engine, _providers, executor, transfer = await big_lab(
        tmp_path, monkeypatch, "parcel-a", "parcel-b", executor_type=WaitingFirstExecutor, width=1, members=3)
    writer = next(artifact for artifact in await repository.artifacts(transfer.id) if artifact.execution is not None)
    executor.finish(writer.execution)
    waiting, resume = asyncio.Event(), asyncio.Event()

    async def raising_verifier(*_args, **_kwargs):
        waiting.set()                                                  # inside the stability wait
        await resume.wait()
        raise RuntimeError("verification failed after its lifecycle moved on")

    import transfers._engine_base as engine_base
    monkeypatch.setattr(engine_base, "verify_materialization", raising_verifier)
    cycle = asyncio.create_task(engine.reconcile_executions())
    await asyncio.wait_for(waiting.wait(), 10)
    assert (await rows("SELECT status FROM download_files WHERE id=?", (writer.id,)))[0]["status"] == "verifying"

    await repository.artifact_state(writer.id, "queued", release=True)   # the lifecycle moved on
    outcomes = (await rows("SELECT COUNT(*) AS n FROM transfer_outcomes WHERE transfer_id=?",
                           (transfer.id,)))[0]["n"]
    resume.set()
    await asyncio.wait_for(cycle, 30)

    after = (await rows("SELECT status,normalized_error FROM download_files WHERE id=?", (writer.id,)))[0]
    assert after["status"] != "error" and after["normalized_error"] is None, "a stale verification published an error"
    assert (await rows("SELECT COUNT(*) AS n FROM transfer_outcomes WHERE transfer_id=?",
                       (transfer.id,)))[0]["n"] == outcomes, "a stale verification recorded a failure outcome"
