"""Same-object resubmission: cleanup-claim liveness + interactive-selection fail-closed.

Two proven lifecycle defects, exposed by re-adding the same multi-file torrent
after deleting it, are core invariants -- not torrent behaviors -- so every
scenario here is driven twice where it matters: once through a provider-neutral
fake (``parcel`` requests, no BitTorrent anywhere) and once through the real
``AllDebridProvider`` (magnet requests) over a stateful fake client.

* Defect 1 (276 -> 279): a cleanup claim was a bare boolean. A claim whose owner
  was cancelled / crashed / failed before finalizing stayed "claimed" forever, the
  predecessor-cleanup fence (correctly) held the fresh same-fingerprint transfer
  behind it, and nothing but a process restart could ever release it. The claim is
  now a durable lease/token: it expires, the ordinary cleanup cadence re-claims it,
  and a stale owner can never finalize over a newer one.
* Defect 2 (276 -> 278): inventory reconciliation bound an observed provider
  resource straight onto a *user-submitted* interactive root with no resolution
  attempt, no provenance, no fence check and no selection generation; the engine
  then read "no generation" as "select all" and fanned out every file. A missing
  generation is never ALL any more, and an unowned adoption of a user transfer is
  refused.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from file_selection_support import Clock
from providers.alldebrid.client import API_V4, AllDebridAPIError
from providers.alldebrid.provider import AllDebridProvider
from transfers.contracts import Cleanup
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage
from transfers.models import (
    Capability, IntegrationDescriptor, OutcomeKind, Ownership, ProviderObservation, ProviderResource,
    ResourceState, TransferOutcome, TransferRequest, TransferState,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.recovery_repository import TransferRepository

LEASE = TransferEngine.CLEANUP_CLAIM_LEASE_SECONDS
FILES = [("e1", "s/e1", 10), ("e2", "s/e2", 20), ("e3", "s/e3", 30)]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

class NoCleanupProvider(ParcelProvider):
    """A resource-owning provider that does NOT offer the Cleanup capability."""

    cleanup = None

    def __init__(self):
        super().__init__("no-cleanup-lab")
        self.descriptor = IntegrationDescriptor(
            "no-cleanup-lab", "No cleanup lab",
            self.descriptor.capabilities - {Capability.CLEANUP},
            request_types=self.descriptor.request_types)


class HttpsExecutor(MemoryExecutor):
    """Executes the https members AllDebrid fans a magnet out into."""

    descriptor = IntegrationDescriptor("https-lab", "HTTPS lab", frozenset())
    claim_schemes = frozenset({"https"})

    def __init__(self, authorize):
        super().__init__(authorize)
        self.started = []

    async def start(self, request, handle):
        self.started.append(request)
        return await super().start(request, handle)


class FakeAllDebridClient:
    """Stateful, deterministic stand-in for the AllDebrid HTTP client: same
    magnet hash -> same native id (AllDebrid de-duplicates), a status list that
    still shows a magnet until it is deleted, and a nested file tree."""

    def __init__(self, tree=FILES):
        self.tree = tree
        self.magnets = {}
        self.deleted = []
        self.uploads = 0
        self._next = 758656541

    def _record(self, magnet_id, *, links=False):
        meta = self.magnets[magnet_id]
        nodes = [{"n": name, "s": size, **({"l": f"https://example.org/{name}"} if links else {})}
                 for name, _path, size in self.tree]
        return {"id": magnet_id, "filename": "Show", "hash": meta["hash"], "statusCode": 4,
                "status": "Ready", "size": sum(size for _n, _p, size in self.tree), "downloaded": 0,
                "downloadSpeed": 0, "files": [{"n": "Show", "e": nodes}]}

    async def upload_magnet(self, magnet):
        self.uploads += 1
        digest = re.search(r"btih:([0-9a-fA-F]{40})", magnet).group(1).lower()
        for magnet_id, meta in self.magnets.items():
            if meta["hash"] == digest:
                return {"id": magnet_id, "hash": digest, "ready": True}
        magnet_id = str(self._next)
        self._next += 1
        self.magnets[magnet_id] = {"hash": digest}
        return {"id": magnet_id, "hash": digest, "ready": True}

    async def get_magnet_status(self, magnet_id=None):
        if magnet_id is not None:
            return [self._record(magnet_id)] if str(magnet_id) in self.magnets else []
        return [self._record(magnet_id) for magnet_id in sorted(self.magnets)]

    async def get_magnet_files(self, ids):
        return [{"id": str(i), "files": [{"n": "Show", "e": [
            {"n": name, "s": size, "l": f"https://example.org/{name}"} for name, _p, size in self.tree]}]}
            for i in ids if str(i) in self.magnets]

    async def unlock_link(self, link):
        name = str(link).rsplit("/", 1)[-1]
        return {"link": str(link), "filename": name, "filesize": 10}

    async def _post(self, base, endpoint, data=None):
        assert base == API_V4 and endpoint == "magnet/delete"
        magnet_id = str(data["id"])
        if magnet_id not in self.magnets:
            raise AllDebridAPIError("MAGNET_INVALID_ID", "no such magnet")
        del self.magnets[magnet_id]
        self.deleted.append(magnet_id)
        return {}


def build_engine(tmp_path, provider, executor_cls=MemoryExecutor, *, clock=None):
    repository = TransferRepository()
    registry = IntegrationRegistry()
    executor = executor_cls(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    clock = clock or Clock(1000.0)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(max_attempts=50, retry_delay=0, resolution_retry_delay=0,
                              adoption_stability_seconds=0, resource_poll_interval=5,
                              max_active_executions=4),
        clock=clock)
    return SimpleNamespace(engine=engine, repository=repository, registry=registry, provider=provider,
                           executor=executor, clock=clock, tmp_path=tmp_path)


@pytest_asyncio.fixture
async def core(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "resubmit.db")
    await database.init_db()
    built = build_engine(tmp_path, ParcelProvider(file_manifest=True))
    await built.engine.initialize()
    return built


@pytest_asyncio.fixture
async def ad(tmp_path, monkeypatch):
    """The same core, wired to the REAL AllDebrid provider over a fake client."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "resubmit-ad.db")
    await database.init_db()
    client = FakeAllDebridClient()
    built = build_engine(tmp_path, AllDebridProvider(client=client), HttpsExecutor)
    built.client = client
    await built.engine.initialize()
    return built


def parcel_request(fingerprint="fp-x", payload="box", *, mode="all"):
    return TransferRequest("parcel", payload, name="payload.bin", fingerprint=fingerprint, selection_mode=mode)


HASH = "c031dbd23d8b8c6ebb81478af7eddfd7d9f06974"


def magnet_request(*, mode="interactive", digest=HASH):
    return TransferRequest("magnet", f"magnet:?xt=urn:btih:{digest}", name="Show",
                           fingerprint=digest, selection_mode=mode)


async def rows(sql, params=()):
    async with database.get_db() as db:
        return await db.fetchall(sql, params)


async def binding(transfer_id):
    found = await rows("SELECT * FROM provider_resources WHERE transfer_id=?", (transfer_id,))
    return found[0] if found else None


async def children(transfer_id):
    return await rows(
        "SELECT * FROM transfer_requests WHERE transfer_id=? AND parent_id IS NOT NULL", (transfer_id,))


async def generations(transfer_id):
    return await rows("SELECT * FROM transfer_file_selections WHERE transfer_id=?", (transfer_id,))


async def attempts(transfer_id):
    return (await rows("SELECT COUNT(*) AS n FROM resolution_attempts a JOIN transfer_requests r "
                       "ON r.id=a.request_id WHERE r.transfer_id=?", (transfer_id,)))[0]["n"]


async def provenance(transfer_id):
    return (await rows("SELECT COUNT(*) AS n FROM route_attempt_provenance WHERE transfer_id=?",
                       (transfer_id,)))[0]["n"]


def transient_failure():
    return TransferOutcome(OutcomeKind.FAILURE, NormalizedError(
        Domain.CLEANUP, Category.REMOTE_CLEANUP_FAILED, Stage.CLEANUP,
        retryability=Retryability.BACKOFF, recovery=Recovery.RETRY))


def permanent_failure():
    return TransferOutcome(OutcomeKind.FAILURE, NormalizedError(
        Domain.CLEANUP, Category.REMOTE_CLEANUP_FAILED, Stage.CLEANUP,
        retryability=Retryability.NEVER, recovery=Recovery.NONE))


async def resolved_parcel(core, fingerprint="fp-x", *, mode="all", files=None, payload="box"):
    core.provider.responses.append(core.provider.parcel(payload, state=ResourceState.AVAILABLE, files=files))
    transfer = await core.engine.submit((parcel_request(fingerprint, payload, mode=mode),), name="A")
    await core.engine.resolve_pending()
    return transfer


async def orphan_cleanup_claim(core, transfer_id):
    """Delete with remote cleanup, then lose the cleanup owner mid-provider-call
    by CANCELLING its task (no restart): the exact 276 shape."""
    entered, release = asyncio.Event(), asyncio.Event()
    original = core.provider.cleanup

    async def blocking(directive):
        entered.set()
        await release.wait()
        return await original(directive)

    core.provider.cleanup = blocking
    await core.repository.delete(transfer_id, remote=True, now=core.clock())
    task = asyncio.create_task(core.engine._cleanup_resources(transfer_id, explicit=True))
    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    core.provider.cleanup = original
    return await binding(transfer_id)


# --------------------------------------------------------------------------- #
# 8.1  Cleanup liveness -- cancellation, no restart, clock advance, automatic
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_cancelled_cleanup_owner_never_strands_a_same_object_readd(core):
    a = await resolved_parcel(core)
    orphan = await orphan_cleanup_claim(core, a.id)
    # 276: the claim was acquired and never finalized -- but it is a bounded lease.
    assert orphan["cleanup_authority"] == "user_request" and orphan["cleanup_attempts"] == 1
    assert orphan["cleanup_claim_token"] and orphan["cleanup_claim_until"] == 1000.0 + LEASE
    assert orphan["cleanup_blocked"] == 0 and orphan["cleanup_abandoned"] == 0

    b = await core.engine.submit((parcel_request(),), name="B")
    assert b.id != a.id
    resolves_before = [c for c in core.provider.calls if c[0] == "resolve"]

    # B is conservatively fenced while the claim is legitimately active.
    while core.clock() < 1000.0 + LEASE - 10:
        await core.engine.resolve_pending()
        core.clock.advance(6)
    assert [c for c in core.provider.calls if c[0] == "resolve"] == resolves_before
    assert await attempts(b.id) == 0 and await provenance(b.id) == 0
    assert await core.repository.predecessor_cleanup_barrier(b.id) is True
    assert await core.repository.pending_cleanup(core.clock()) == ()     # nobody may steal a live lease
    assert not [c for c in core.provider.calls if c[0] == "cleanup"]     # the lost owner never returned

    # Past the lease, the ORDINARY tick (no restart, no pause/resume, no Retry)
    # re-claims and completes the cleanup, and B proceeds on the same cadence.
    core.clock.advance(LEASE)
    await core.engine.resolve_pending()
    row = await binding(a.id)
    assert row["cleanup_authority"] is None and row["cleanup_claim_token"] is None
    assert row["cleanup_claim_until"] == 0 and row["state"] == "absent" and row["cleanup_blocked"] == 0
    assert len([c for c in core.provider.calls if c[0] == "cleanup"]) == 1   # exactly one re-drive
    assert (await binding(a.id))["cleanup_attempts"] == 2                    # the lost claim consumed an attempt
    assert await core.repository.predecessor_cleanup_barrier(b.id) is False
    assert len([c for c in core.provider.calls if c[0] == "resolve"]) == len(resolves_before) + 1
    assert await attempts(b.id) == 1                                     # attempts begin only after the fence cleared
    assert (await core.repository.get(b.id)).state not in {
        TransferState.FAILED, TransferState.DELETED, TransferState.CANCELLED}
    assert (await core.repository.get(b.id)).error is None


@pytest.mark.asyncio
async def test_claim_is_atomic_and_a_stale_token_can_never_finalize_a_newer_owner(core):
    a = await resolved_parcel(core)
    await core.repository.delete(a.id, remote=True, now=core.clock())
    await core.repository.cleanup_intent(
        a.id, (await core.repository.resources(a.id))[0][0].id, "user_request")
    binding_id = (await binding(a.id))["id"]
    now = core.clock()

    first = await core.repository.claim_cleanup(binding_id, now=now, lease_until=now + 60)
    assert first
    # No duplicate simultaneous owner while the lease is current.
    assert await core.repository.claim_cleanup(binding_id, now=now + 1, lease_until=now + 61) is None
    assert await core.repository.pending_cleanup(now + 1) == ()

    # Expiry hands the row to exactly one new owner.
    later = now + 61
    assert [item[4] for item in await core.repository.pending_cleanup(later)] == [binding_id]
    second = await core.repository.claim_cleanup(binding_id, now=later, lease_until=later + 60)
    assert second and second != first
    assert await core.repository.claim_cleanup(binding_id, now=later, lease_until=later + 60) is None

    # The stale owner's late outcome is rejected in every form and changes nothing.
    error = transient_failure().error
    assert await core.repository.cleanup_complete(binding_id, first) is False
    assert await core.repository.cleanup_retry(binding_id, first, error, later + 5) is False
    assert await core.repository.cleanup_retry(binding_id, first, error, None) is False
    row = await binding(a.id)
    assert row["cleanup_claim_token"] == second and row["cleanup_authority"] == "user_request"
    assert row["cleanup_abandoned"] == 0 and row["cleanup_retry_at"] == 0

    assert await core.repository.cleanup_complete(binding_id, second) is True
    assert (await binding(a.id))["cleanup_authority"] is None
    assert await core.repository.cleanup_complete(binding_id, second) is False    # not replayable


@pytest.mark.asyncio
async def test_a_late_stale_owner_cannot_overwrite_the_reclaimed_cleanup(core):
    """The provider call of an owner that outlives its lease finishes AFTER a
    newer owner took over: its finalization is dropped, the newer owner's stands."""
    a = await resolved_parcel(core)
    core.engine.CLEANUP_HEARTBEAT_POLL_SECONDS = 3600.0     # a STALLED owner: its heartbeat never gets to run
    entered, release = asyncio.Event(), asyncio.Event()
    original = core.provider.cleanup

    async def slow(directive):
        entered.set()
        await release.wait()
        return transient_failure()                # the slow owner will report FAILURE

    core.provider.cleanup = slow
    await core.repository.delete(a.id, remote=True, now=core.clock())
    slow_owner = asyncio.create_task(core.engine._cleanup_resources(a.id, explicit=True))
    await entered.wait()

    core.provider.cleanup = original              # the newer owner really cleans up
    core.clock.advance(LEASE + 1)
    await core.engine.cleanup_pending()
    assert (await binding(a.id))["cleanup_authority"] is None

    release.set()                                 # the stale owner now returns its FAILURE
    await slow_owner
    row = await binding(a.id)
    assert row["cleanup_authority"] is None and row["cleanup_abandoned"] == 0
    assert row["cleanup_claim_token"] is None and row["cleanup_error"] is None
    assert row["state"] == "absent"


# --------------------------------------------------------------------------- #
# Live ownership: the lease is renewed by the owner while its provider call
# runs, so a LIVE owner can never be reclaimed and a DEAD one never blocks
# --------------------------------------------------------------------------- #

class CleanupProbe:
    """Wraps a provider's cleanup: the FIRST call is held open until released,
    later calls pass straight through; counts starts and simultaneous calls."""

    def __init__(self, provider):
        self.original = provider.cleanup
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.starts = self.active = self.max_active = self.cancelled = 0
        provider.cleanup = self.cleanup

    async def cleanup(self, directive):
        self.starts += 1
        first = self.starts == 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if first:
                self.entered.set()
                await self.release.wait()
            return await self.original(directive)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1


async def _wait_for(predicate, *, seconds=3.0):
    """Real-time bound only for letting the heartbeat task run; every lease
    decision itself is taken on the injected fake clock."""
    for _ in range(int(seconds / 0.005)):
        if await predicate():
            return True
        await asyncio.sleep(0.005)
    return False


@pytest.mark.asyncio
async def test_a_live_cleanup_owner_renews_and_is_never_reclaimed_but_a_dead_one_is(core):
    core.engine.CLEANUP_HEARTBEAT_POLL_SECONDS = 0.001      # heartbeat polling; the lease runs on the fake clock
    other = build_engine(core.tmp_path, core.provider, clock=core.clock)   # a second worker, same database
    other.engine.CLEANUP_HEARTBEAT_POLL_SECONDS = 0.001
    await other.engine.initialize()
    a = await resolved_parcel(core)
    probe = CleanupProbe(core.provider)
    await core.repository.delete(a.id, remote=True, now=core.clock())

    # 1-2. owner A claims cleanup and its provider call stays open
    owner_a = asyncio.create_task(core.engine._cleanup_resources(a.id, explicit=True))
    await probe.entered.wait()
    first = await binding(a.id)
    token_a = first["cleanup_claim_token"]
    assert token_a and first["cleanup_claim_until"] == 1000.0 + LEASE

    # 3-6. far beyond one nominal lease (2.5 leases), ordinary cadence of BOTH workers each step
    for _ in range(6):
        core.clock.advance(50)
        expected = core.clock() + LEASE

        async def renewed():
            return (await binding(a.id))["cleanup_claim_until"] >= expected

        renewed_in_time = await _wait_for(renewed)
        await other.engine.cleanup_pending()
        await core.engine.cleanup_pending()
        assert probe.starts == 1 and probe.max_active == 1           # never a second simultaneous remote cleanup
        assert renewed_in_time, "the live owner did not renew its lease"
        row = await binding(a.id)
        assert row["cleanup_claim_token"] == token_a                 # same token, renewed in place
        assert row["cleanup_authority"] == "user_request" and row["cleanup_attempts"] == 1
    assert core.clock() >= 1000.0 + 2 * LEASE                        # the ORIGINAL lease is long gone

    # 7-8. A is cancelled/terminated: its heartbeat stops with it
    owner_a.cancel()
    await asyncio.gather(owner_a, return_exceptions=True)
    assert probe.cancelled == 1 and probe.active == 0
    held = (await binding(a.id))["cleanup_claim_until"]
    assert held == 1000.0 + 300 + LEASE                              # the last renewal, never extended again
    core.clock.advance(20)
    await asyncio.sleep(0.05)                                        # give a (dead) heartbeat every chance to run
    assert (await binding(a.id))["cleanup_claim_until"] == held

    # 9-11. not reclaimable until the renewed lease itself runs out; then automatically, no restart
    core.provider.cleanup_response = transient_failure()             # B's outcome: a retryable failure
    core.clock.set(held - 1)
    await other.engine.cleanup_pending()
    assert probe.starts == 1 and (await binding(a.id))["cleanup_claim_token"] == token_a
    core.clock.set(held)
    await other.engine.cleanup_pending()
    assert probe.starts == 2 and probe.max_active == 1
    row = await binding(a.id)
    assert row["cleanup_claim_token"] is None and row["cleanup_authority"] == "user_request"
    assert row["cleanup_attempts"] == 2 and row["cleanup_error"]

    # 12. late finalization by A can never overwrite B's outcome
    assert await core.repository.cleanup_complete(row["id"], token_a) is False
    assert await core.repository.cleanup_retry(row["id"], token_a, transient_failure().error, None) is False
    assert await binding(a.id) == row


@pytest.mark.asyncio
async def test_an_owner_that_discovers_it_lost_the_claim_aborts_its_cleanup_call(core):
    """If the heartbeat finds the claim is no longer ours, the in-flight provider
    call is aborted at once -- a lost owner must not keep acting on the resource --
    and nothing is finalized on the thief's row."""
    core.engine.CLEANUP_HEARTBEAT_POLL_SECONDS = 0.001
    a = await resolved_parcel(core)
    probe = CleanupProbe(core.provider)
    await core.repository.delete(a.id, remote=True, now=core.clock())
    outcomes_before = len(await rows("SELECT id FROM transfer_outcomes WHERE transfer_id=?", (a.id,)))
    owner = asyncio.create_task(core.engine._cleanup_resources(a.id, explicit=True))
    await probe.entered.wait()

    async with database.get_db() as db:                               # ownership is taken over elsewhere
        await db.execute("UPDATE provider_resources SET cleanup_claim_token='thief', cleanup_claim_until=? "
                         "WHERE transfer_id=?", (core.clock() + 500, a.id))
        await db.commit()
    core.clock.advance(50)                                            # heartbeat is due
    await asyncio.wait_for(owner, timeout=3.0)                        # returns normally: it stood down

    assert probe.cancelled == 1 and probe.active == 0
    row = await binding(a.id)
    assert row["cleanup_claim_token"] == "thief" and row["cleanup_authority"] == "user_request"
    assert len(await rows("SELECT id FROM transfer_outcomes WHERE transfer_id=?", (a.id,))) == outcomes_before


@pytest.mark.asyncio
async def test_the_heartbeat_stops_with_the_call_and_leaves_nothing_running(core):
    core.engine.CLEANUP_HEARTBEAT_POLL_SECONDS = 0.001
    a = await resolved_parcel(core)
    before = {t for t in asyncio.all_tasks()}
    await core.engine.delete(a.id, remote=True)                       # ordinary completion
    await asyncio.sleep(0.02)
    assert {t for t in asyncio.all_tasks() if not t.done()} <= before
    row = await binding(a.id)
    assert row["cleanup_authority"] is None and row["cleanup_claim_token"] is None


class SerialCleanupProbe:
    """Holds the FIRST cleanup call of every resource open (released per resource)
    and records, across all resources, starts and simultaneous calls."""

    def __init__(self, provider):
        self.original = provider.cleanup
        self.entered = asyncio.Queue()
        self.release = {}
        self.starts = {}
        self.active = self.max_active = 0
        provider.cleanup = self.cleanup

    async def cleanup(self, directive):
        key = directive.resource.id
        self.starts[key] = self.starts.get(key, 0) + 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.starts[key] == 1:
                self.release[key] = asyncio.Event()
                self.entered.put_nowait(key)
                await self.release[key].wait()
            return await self.original(directive)
        finally:
            self.active -= 1


async def _owed_pair(core):
    """Two resolved transfers, both deleted with remote cleanup owed but NOT yet run
    (resolve both first: an ordinary tick would otherwise clean up the first)."""
    for payload in ("box-a", "box-b"):
        core.provider.responses.append(core.provider.parcel(payload, state=ResourceState.AVAILABLE))
    a = await core.engine.submit((parcel_request("fp-serial-a", "box-a"),), name="A")
    b = await core.engine.submit((parcel_request("fp-serial-b", "box-b"),), name="B")
    await core.engine.resolve_pending()                       # both roots resolve in ONE tick
    for transfer in (a, b):
        resource = (await core.repository.resources(transfer.id))[0][0]
        await core.repository.delete(transfer.id, remote=True, now=core.clock())
        await core.repository.cleanup_intent(transfer.id, resource.id, "user_request")
    return a, b


@pytest.mark.asyncio
async def test_each_serial_claim_starts_its_lease_when_that_claim_is_taken_not_when_the_scan_began(core):
    """One cadence pass scans A and B at time T and runs them serially. A runs past
    a whole lease; B is then claimed at T+150. B's lease must start at ITS claim
    time -- a lease dated from the scan (T+120) would already be expired at birth,
    and a competing worker could reclaim B before its first heartbeat."""
    core.engine.CLEANUP_HEARTBEAT_POLL_SECONDS = 0.001
    other = build_engine(core.tmp_path, core.provider, clock=core.clock)          # a competing worker
    other.engine.CLEANUP_HEARTBEAT_POLL_SECONDS = 0.001
    await other.engine.initialize()
    await _owed_pair(core)
    probe = SerialCleanupProbe(core.provider)
    scan_time = core.clock()
    assert len(await core.repository.pending_cleanup(scan_time)) == 2               # both scanned at T

    runner = asyncio.create_task(core.engine.cleanup_pending())
    first = await asyncio.wait_for(probe.entered.get(), 3)                          # A claims normally
    core.clock.advance(LEASE + 30)                                                  # A outlives a whole lease
    expected = core.clock() + LEASE

    async def a_renewed():
        rows_ = await rows("SELECT cleanup_claim_until FROM provider_resources WHERE resource_key=?", (first,))
        return rows_[0]["cleanup_claim_until"] >= expected

    assert await _wait_for(a_renewed)                                               # (live owner renews)
    probe.release[first].set()                                                      # complete A ...
    second = await asyncio.wait_for(probe.entered.get(), 3)                         # ... the SAME loop moves on to B
    assert second != first

    claimed_at = core.clock()
    row = (await rows("SELECT * FROM provider_resources WHERE resource_key=?", (second,)))[0]
    assert claimed_at == scan_time + LEASE + 30
    assert row["cleanup_claim_until"] == claimed_at + LEASE                          # dated from B's claim, not the scan
    assert row["cleanup_claim_until"] > claimed_at and row["cleanup_claim_until"] != scan_time + LEASE

    # a competing worker's cadence runs BEFORE B's first heartbeat renewal is due
    assert await core.repository.pending_cleanup(claimed_at) == ()
    await other.engine.cleanup_pending()
    assert probe.starts[second] == 1 and probe.max_active == 1                       # B never runs twice at once
    assert (await rows("SELECT cleanup_claim_token FROM provider_resources WHERE resource_key=?", (second,)))[0][
        "cleanup_claim_token"] == row["cleanup_claim_token"]

    probe.release[second].set()
    await asyncio.wait_for(runner, 3)
    assert probe.max_active == 1 and probe.starts == {first: 1, second: 1}
    assert await core.repository.pending_cleanup(core.clock() + 10 ** 6) == ()       # both completed


@pytest.mark.asyncio
async def test_a_provider_that_swallows_cancellation_still_cannot_record_an_outcome_after_losing_the_claim(core):
    """The heartbeat aborts a lost owner's call by cancelling it. A provider that
    catches the cancellation and returns normally must NOT get its outcome
    recorded: ownership already moved, so the stale owner stands down."""
    core.engine.CLEANUP_HEARTBEAT_POLL_SECONDS = 0.001
    a = await resolved_parcel(core)
    entered, release = asyncio.Event(), asyncio.Event()
    swallowed = []

    async def stubborn(directive):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            swallowed.append(True)                                   # ignores the abort ...
        return TransferOutcome(OutcomeKind.SUCCESS)                  # ... and reports success anyway

    core.provider.cleanup = stubborn
    await core.repository.delete(a.id, remote=True, now=core.clock())
    before = len(await rows("SELECT id FROM transfer_outcomes WHERE transfer_id=?", (a.id,)))
    owner = asyncio.create_task(core.engine._cleanup_resources(a.id, explicit=True))
    await entered.wait()
    async with database.get_db() as db:
        await db.execute("UPDATE provider_resources SET cleanup_claim_token='thief', cleanup_claim_until=? "
                         "WHERE transfer_id=?", (core.clock() + 500, a.id))
        await db.commit()
    core.clock.advance(50)
    await asyncio.wait_for(owner, timeout=3.0)                        # the owner stood down

    assert swallowed == [True]                                        # the provider really did swallow the abort
    assert len(await rows("SELECT id FROM transfer_outcomes WHERE transfer_id=?", (a.id,))) == before
    row = await binding(a.id)
    assert row["cleanup_claim_token"] == "thief" and row["cleanup_authority"] == "user_request"
    assert row["state"] != "absent"                                   # no ABSENT observation from the stale outcome


@pytest.mark.asyncio
async def test_every_post_claim_exit_converges_by_lease_not_by_restart(core):
    """Cancellation is not the only escape: a failure while RECORDING the outcome
    (after the provider call) strands the claim just as surely."""
    a = await resolved_parcel(core)
    await core.repository.delete(a.id, remote=True, now=core.clock())

    real_outcome = core.repository.outcome

    async def broken_outcome(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    core.repository.outcome = broken_outcome
    with pytest.raises(RuntimeError):
        await core.engine._cleanup_resources(a.id, explicit=True)
    core.repository.outcome = real_outcome

    row = await binding(a.id)
    assert row["cleanup_claim_token"] and row["cleanup_authority"] == "user_request"
    assert await core.repository.predecessor_cleanup_barrier(a.id + 1) is False   # (different object: no fence)
    core.clock.advance(LEASE + 1)
    await core.engine.cleanup_pending()
    row = await binding(a.id)
    assert row["cleanup_authority"] is None and row["cleanup_claim_token"] is None


@pytest.mark.asyncio
async def test_an_invalid_provider_cleanup_response_is_a_failure_not_a_stranded_claim(core):
    a = await resolved_parcel(core)

    async def garbage(_directive):
        return "not-an-outcome"

    core.provider.cleanup = garbage
    await core.engine.delete(a.id, remote=True)
    row = await binding(a.id)
    assert row["cleanup_claim_token"] is None and row["cleanup_authority"] == "user_request"
    assert row["cleanup_abandoned"] == 1        # INVALID_ADAPTER_RESPONSE is never retryable
    assert row["cleanup_error"]


# --------------------------------------------------------------------------- #
# 8.2  Every terminal outcome leaves a consistent claim and the right fence
# --------------------------------------------------------------------------- #

async def _delete_with(core, outcome):
    a = await resolved_parcel(core)
    core.provider.cleanup_response = outcome
    await core.engine.delete(a.id, remote=True)
    b = await core.engine.submit((parcel_request(),), name="B")
    return a, b, await binding(a.id)


@pytest.mark.asyncio
async def test_cleanup_success_clears_intent_claim_and_fence(core):
    a, b, row = await _delete_with(core, TransferOutcome(OutcomeKind.SUCCESS))
    assert row["cleanup_authority"] is None and row["cleanup_claim_token"] is None
    assert row["cleanup_claim_until"] == 0 and row["cleanup_retry_at"] == 0 and row["state"] == "absent"
    assert row["cleanup_blocked"] == 0 and row["cleanup_error"] is None
    assert await core.repository.predecessor_cleanup_barrier(b.id) is False


@pytest.mark.asyncio
async def test_cleanup_skipped_clears_intent_and_claim_without_marking_absent(core):
    a, b, row = await _delete_with(core, TransferOutcome(OutcomeKind.SKIPPED, detail="retained"))
    assert row["cleanup_authority"] is None and row["cleanup_claim_token"] is None
    assert row["cleanup_claim_until"] == 0 and row["state"] == "available"
    assert await core.repository.predecessor_cleanup_barrier(b.id) is False


@pytest.mark.asyncio
async def test_retryable_cleanup_failure_releases_the_claim_and_schedules_the_retry(core):
    core.engine.policy = replace(core.engine.policy, retry_delay=30, max_retry_delay=300)
    a, b, row = await _delete_with(core, transient_failure())
    assert row["cleanup_authority"] == "user_request" and row["cleanup_abandoned"] == 0
    assert row["cleanup_claim_token"] is None and row["cleanup_claim_until"] == 0
    assert row["cleanup_retry_at"] > core.clock() and row["cleanup_error"]
    assert await core.repository.predecessor_cleanup_barrier(b.id) is True    # can still act -> still fenced
    assert await core.repository.pending_cleanup(core.clock()) == ()          # not before retry_at
    assert [item[4] for item in await core.repository.pending_cleanup(row["cleanup_retry_at"])] == [row["id"]]


@pytest.mark.asyncio
async def test_terminal_cleanup_failure_abandons_releases_claim_and_unfences(core):
    a, b, row = await _delete_with(core, permanent_failure())
    assert row["cleanup_abandoned"] == 1 and row["cleanup_authority"] == "user_request"
    assert row["cleanup_claim_token"] is None and row["cleanup_claim_until"] == 0
    assert await core.repository.predecessor_cleanup_barrier(b.id) is False
    assert await core.repository.pending_cleanup(core.clock() + 10 ** 6) == ()
    core.provider.responses.append(core.provider.parcel("box", state=ResourceState.AVAILABLE))
    await core.engine.resolve_pending()
    assert await core.repository.resources(b.id)                # B proceeds -- no deadlock


@pytest.mark.asyncio
async def test_an_absent_resource_never_fences_a_new_generation(core):
    a = await resolved_parcel(core)
    resource = (await core.repository.resources(a.id))[0][0]
    await core.repository.delete(a.id, remote=True, now=core.clock())
    await core.repository.cleanup_intent(a.id, resource.id, "user_request")
    b = await core.engine.submit((parcel_request(),), name="B")
    assert await core.repository.predecessor_cleanup_barrier(b.id) is True
    await core.repository.resource_observation(a.id, resource, ResourceState.ABSENT)
    assert await core.repository.predecessor_cleanup_barrier(b.id) is False   # owed, but nothing left to act on


@pytest.mark.asyncio
async def test_a_provider_without_cleanup_capability_cannot_deadlock_the_fence(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "no-cleanup.db")
    await database.init_db()
    core = build_engine(tmp_path, NoCleanupProvider())
    await core.engine.initialize()
    assert not isinstance(core.provider, Cleanup)
    core.provider.responses.append(core.provider.parcel("box", state=ResourceState.AVAILABLE))
    a = await core.engine.submit((parcel_request(),), name="A")
    await core.engine.resolve_pending()
    await core.engine.delete(a.id, remote=True)
    row = await binding(a.id)
    # No cleanup operation can ever act: the obligation is abandoned (evidence and
    # reason kept), not left owed forever behind a fence nothing can lift.
    assert row["cleanup_abandoned"] == 1 and row["cleanup_authority"] == "user_request"
    assert row["cleanup_claim_token"] is None and row["cleanup_error"]
    b = await core.engine.submit((parcel_request(),), name="B")
    assert await core.repository.predecessor_cleanup_barrier(b.id) is False
    core.provider.responses.append(core.provider.parcel("box", state=ResourceState.AVAILABLE))
    await core.engine.resolve_pending()
    assert await core.repository.resources(b.id)


# --------------------------------------------------------------------------- #
# 8.3  Legacy boolean-claim databases self-heal, idempotently, without SQL
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_legacy_blocked_claim_rows_self_heal_on_upgrade_without_manual_sql(core):
    a = await resolved_parcel(core)
    resource = (await core.repository.resources(a.id))[0][0]
    await core.repository.delete(a.id, remote=True, now=core.clock())
    await core.repository.cleanup_intent(a.id, resource.id, "user_request")
    b = await core.engine.submit((parcel_request(),), name="B")

    # Old-format equivalent of transfer 276: owed, claimed via the boolean, no lease.
    async with database.get_db() as db:
        await db.execute("UPDATE provider_resources SET cleanup_blocked=1, cleanup_attempts=1 WHERE transfer_id=?",
                         (a.id,))
        await db.commit()
    assert await core.repository.predecessor_cleanup_barrier(b.id) is True

    await database.init_db()                                      # first normal initialization after upgrade
    await database.init_db()                                      # ...and it is idempotent
    row = await binding(a.id)
    assert row["cleanup_blocked"] == 0 and row["cleanup_claim_token"] is None
    assert row["cleanup_authority"] == "user_request" and row["cleanup_abandoned"] == 0
    assert [item[4] for item in await core.repository.pending_cleanup(core.clock())] == [row["id"]]

    await core.engine.cleanup_pending()                           # the ordinary cadence heals it
    assert (await binding(a.id))["cleanup_authority"] is None
    assert await core.repository.predecessor_cleanup_barrier(b.id) is False


@pytest.mark.asyncio
async def test_legacy_terminal_markers_are_normalized_and_live_leases_are_not_stolen(core):
    a = await resolved_parcel(core)
    resource = (await core.repository.resources(a.id))[0][0]
    await core.repository.delete(a.id, remote=True, now=core.clock())
    await core.repository.cleanup_intent(a.id, resource.id, "user_request")
    binding_id = (await binding(a.id))["id"]

    # (a) a completed/absent row that kept the old boolean; (b) an active new lease.
    live = await core.repository.claim_cleanup(binding_id, now=core.clock(), lease_until=core.clock() + 500)
    async with database.get_db() as db:
        await db.execute("UPDATE provider_resources SET cleanup_blocked=1 WHERE id=?", (binding_id,))
        await db.commit()
    await database.init_db()
    row = await binding(a.id)
    assert row["cleanup_blocked"] == 0
    assert row["cleanup_claim_token"] == live and row["cleanup_claim_until"] == core.clock() + 500
    assert await core.repository.pending_cleanup(core.clock()) == ()

    await core.repository.cleanup_intent(a.id, resource.id, None)
    row = await binding(a.id)
    assert row["cleanup_authority"] is None and row["cleanup_claim_token"] is None and row["cleanup_claim_until"] == 0


# --------------------------------------------------------------------------- #
# 8.8  A different object is never blocked by someone else's cleanup claim
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_different_object_is_not_fenced_by_an_orphaned_claim(core):
    a = await resolved_parcel(core, "fp-a", payload="box-a")
    await orphan_cleanup_claim(core, a.id)
    core.provider.responses.append(core.provider.parcel("box-c", state=ResourceState.AVAILABLE))
    c = await core.engine.submit((parcel_request("fp-c", "box-c"),), name="C")
    assert await core.repository.predecessor_cleanup_barrier(c.id) is False
    await core.engine.resolve_pending()
    assert await core.repository.resources(c.id)
    # Not fenced: C's ROOT reached the provider -- exactly once, never twice.
    # The member its AVAILABLE resource fanned out is that same cycle's work
    # and owns its own attempt, so attempts are counted per request identity.
    records = await core.repository.requests(c.id)
    root = next(item for item in records if item.parent_id is None)
    members = [item for item in records if item.parent_id == root.id]
    per_request = {row["request_id"]: row["n"] for row in await rows(
        "SELECT a.request_id, COUNT(*) AS n FROM resolution_attempts a JOIN transfer_requests r "
        "ON r.id=a.request_id WHERE r.transfer_id=? GROUP BY a.request_id", (c.id,))}
    assert per_request.pop(root.id) == 1
    assert members and set(per_request) <= {item.id for item in members}
    assert all(count == 1 for count in per_request.values())
    # Every provider resolve for C is backed by exactly one of those durable
    # attempts (root + members): no unrecorded second contact for the root.
    assert len([call for call in core.provider.calls if call == ("resolve", "box-c")]) == 1 + len(per_request)


# --------------------------------------------------------------------------- #
# 8.11  Provider-neutral cleanup control: the same claim rules on the real
#       AllDebrid magnet path -- one lease model, not a torrent special case
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_the_orphaned_claim_liveness_holds_on_the_alldebrid_magnet_path_too(ad):
    a = await ad.engine.submit((magnet_request(mode="all"),), name="A")
    await ad.engine.resolve_pending()
    native_id = next(iter(ad.client.magnets))
    assert (await binding(a.id))["cleanup_authority"] is None

    # Same 276 shape: lose the cleanup owner after the claim, without a restart.
    entered, release = asyncio.Event(), asyncio.Event()
    original = ad.provider.cleanup

    async def blocking(directive):
        entered.set()
        await release.wait()
        return await original(directive)

    ad.provider.cleanup = blocking
    await ad.repository.delete(a.id, remote=True, now=ad.clock())
    task = asyncio.create_task(ad.engine._cleanup_resources(a.id, explicit=True))
    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    ad.provider.cleanup = original
    assert (await binding(a.id))["cleanup_claim_token"] and native_id in ad.client.magnets

    b = await ad.engine.submit((magnet_request(mode="all"),), name="B")
    uploads = ad.client.uploads
    await ad.engine.resolve_pending()
    assert ad.client.uploads == uploads and await attempts(b.id) == 0      # fenced

    ad.clock.advance(LEASE + 1)
    await ad.engine.resolve_pending()
    assert native_id in ad.client.deleted                                   # reclaimed and re-driven
    assert (await binding(a.id))["cleanup_claim_token"] is None
    assert ad.client.uploads == uploads + 1 and await attempts(b.id) == 1   # B then proceeds by itself


# --------------------------------------------------------------------------- #
# 8.4  Exact interactive re-add: fresh generation, never the old subset
# --------------------------------------------------------------------------- #

async def _subset_transfer(core, fingerprint, chosen, *, complete=False):
    core.provider.responses.append(core.provider.parcel("box", state=ResourceState.AVAILABLE, files=FILES))
    transfer = await core.engine.submit((parcel_request(fingerprint, mode="interactive"),), name="T")
    await core.engine.resolve_pending()
    view = await core.repository.file_selection_presentation(transfer.id, now=core.clock())
    assert view and view["decision"] == "pending" and await children(transfer.id) == []
    picked = [entry["entry_id"] for entry in view["entries"] if entry["name"] in chosen]
    result = await core.repository.confirm_file_selection(transfer.id, view["manifest_id"], picked, now=core.clock())
    assert result.outcome == "confirmed"
    await core.engine.resolve_pending()
    return transfer, view


async def _child_names(core, transfer_id):
    return sorted(r.request.name for r in await core.repository.requests(transfer_id) if r.parent_id)


async def _complete(core, transfer):
    """Drive every child to a finished payload on disk, then let the core settle."""
    for _ in range(6):
        await core.engine.tick()
        for artifact in await core.repository.artifacts(transfer.id):
            if artifact.execution is not None and artifact.execution.attempt_id in core.executor.jobs:
                current = core.executor.jobs[artifact.execution.attempt_id]
                if current.state.value not in {"succeeded"}:
                    core.executor.finish(artifact.execution)
        core.clock.advance(1)
    assert (await core.repository.get(transfer.id)).state == TransferState.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("payload_present", [True, False], ids=["files-present", "files-deleted"])
async def test_interactive_readd_gets_a_fresh_generation_and_never_inherits_the_prior_subset(core, payload_present):
    a, view_a = await _subset_transfer(core, "fp-re", {"e1"})
    assert await _child_names(core, a.id) == ["e1"]
    await _complete(core, a)                                     # A finished: payload is on disk
    targets = [Path(artifact.target) for artifact in await core.repository.artifacts(a.id)]
    assert targets and all(path.exists() for path in targets)
    if not payload_present:
        for path in targets:
            path.unlink()
    core.clock.advance(10)
    await core.engine.delete(a.id, remote=True)
    started = list(core.executor.calls)

    core.provider.responses.append(core.provider.parcel("box", state=ResourceState.AVAILABLE, files=FILES))
    b = await core.engine.submit((parcel_request("fp-re", mode="interactive"),), name="B")
    await core.engine.resolve_pending()

    view_b = await core.repository.file_selection_presentation(b.id, now=core.clock())
    assert view_b["selection_id"] != view_a["selection_id"] and view_b["manifest_id"] != view_a["manifest_id"]
    assert view_b["decision"] == "pending" and view_b["selected_entry_ids"] == []
    assert await children(b.id) == [] and await core.repository.artifacts(b.id) == ()
    assert core.executor.calls == started                        # nothing executes before a decision
    assert (await binding(b.id))["resource_key"] == (await binding(a.id))["resource_key"]   # identity reused...
    assert (await binding(b.id))["id"] != (await binding(a.id))["id"]                       # ...lifecycle is not

    picked = [entry["entry_id"] for entry in view_b["entries"] if entry["name"] == "e3"]
    assert (await core.repository.confirm_file_selection(
        b.id, view_b["manifest_id"], picked, now=core.clock())).outcome == "confirmed"
    await core.engine.resolve_pending()
    assert await _child_names(core, b.id) == ["e3"]              # only the NEW subset, never A's e1


@pytest.mark.asyncio
async def test_alldebrid_interactive_readd_reaches_selection_after_an_orphaned_cleanup_without_restart(ad):
    a = await ad.engine.submit((magnet_request(),), name="A")
    await ad.engine.resolve_pending()
    view_a = await ad.repository.file_selection_presentation(a.id, now=ad.clock())
    picked = [entry["entry_id"] for entry in view_a["entries"] if entry["name"] == "e1"]
    await ad.repository.confirm_file_selection(a.id, view_a["manifest_id"], picked, now=ad.clock())
    await ad.engine.resolve_pending()
    assert len(await children(a.id)) == 1

    entered, release = asyncio.Event(), asyncio.Event()
    original = ad.provider.cleanup

    async def blocking(directive):
        entered.set()
        await release.wait()
        return await original(directive)

    ad.provider.cleanup = blocking
    await ad.repository.delete(a.id, remote=True, now=ad.clock())
    task = asyncio.create_task(ad.engine._cleanup_resources(a.id, explicit=True))
    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    ad.provider.cleanup = original

    b = await ad.engine.submit((magnet_request(),), name="B")            # 279
    await ad.engine.resolve_pending()
    assert await binding(b.id) is None and await attempts(b.id) == 0

    ad.clock.advance(LEASE + 1)
    await ad.engine.resolve_pending()                                    # ordinary cadence only
    view_b = await ad.repository.file_selection_presentation(b.id, now=ad.clock())
    assert view_b["selection_id"] != view_a["selection_id"] and view_b["decision"] == "pending"
    assert await children(b.id) == [] and await attempts(b.id) == 1


# --------------------------------------------------------------------------- #
# 8.5  Transfer 278: interactive + no generation + N children is unreachable
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_inventory_never_rebinds_a_user_submitted_interactive_root_the_278_writer(ad):
    """The exact 278 writer: ``reconcile_inventory`` saw the still-listed native
    magnet of a deleted predecessor, de-duplicated onto the user's fresh
    interactive transfer, and bound it with no attempt / provenance / fence /
    generation. That transition no longer exists for a user transfer."""
    a = await ad.engine.submit((magnet_request(),), name="A")
    await ad.engine.resolve_pending()
    await ad.repository.delete(a.id, remote=True, now=ad.clock())
    await ad.repository.cleanup_intent(a.id, (await ad.repository.resources(a.id))[0][0].id, "user_request")
    held = await ad.repository.claim_cleanup((await binding(a.id))["id"], now=ad.clock(), lease_until=ad.clock() + 500)
    assert held                                                          # cleanup legitimately in flight

    b = await ad.engine.submit((magnet_request(),), name="B", source="manual")
    assert ad.client.magnets                                             # the native magnet is still listed
    await ad.engine.reconcile_inventory()
    await ad.engine.resolve_pending()

    root = (await ad.repository.requests(b.id))[0]
    assert root.resource is None and root.state == "pending" and root.attempts == 0
    assert await binding(b.id) is None and await children(b.id) == []
    assert await generations(b.id) == []
    assert (await ad.repository.get(b.id)).source == "manual"


@pytest.mark.asyncio
async def test_a_bound_interactive_root_with_no_generation_fails_closed_at_materialization(ad):
    """Whatever path (present or future) leaves a root ``waiting`` with a bound
    resource and NO generation -- the durable transfer-278 shape -- the
    observation boundary establishes the generation and holds. It can never
    fan out every file."""
    b = await ad.engine.submit((magnet_request(),), name="B")
    upload = await ad.client.upload_magnet(f"magnet:?xt=urn:btih:{HASH}")
    resource = ProviderResource("alldebrid", {"id": upload["id"]}, Ownership.OBSERVED,
                                (await ad.provider.inventory()).observations[0].resource.id)
    async with database.get_db() as db:                                  # the 278 row, verbatim
        await db.execute("BEGIN IMMEDIATE")
        await TransferRepository._resource(db, b.id, resource, ResourceState.AVAILABLE)
        await db.execute("UPDATE transfer_requests SET state='waiting', resource=? WHERE transfer_id=?",
                         (__import__("transfers.codec", fromlist=["dump"]).dump(resource), b.id))
        await db.commit()
    assert await generations(b.id) == [] and await attempts(b.id) == 0 and await provenance(b.id) == 0

    for _ in range(3):
        await ad.engine.resolve_pending()
        ad.clock.advance(2)
    gens = await generations(b.id)
    assert len(gens) == 1 and gens[0]["decision"] == "pending"           # the current binding's OWN generation
    assert gens[0]["provider_resource_id"] == (await binding(b.id))["id"]
    assert await children(b.id) == [] and await ad.repository.artifacts(b.id) == ()
    assert ad.executor.started == []

    view = await ad.repository.file_selection_presentation(b.id, now=ad.clock())
    picked = [entry["entry_id"] for entry in view["entries"] if entry["name"] == "e2"]
    await ad.repository.confirm_file_selection(b.id, view["manifest_id"], picked, now=ad.clock())
    await ad.engine.resolve_pending()
    assert len(await children(b.id)) == 1                                # never 3 (== "all")


@pytest.mark.asyncio
async def test_inventory_adoption_of_an_interactive_import_opens_its_generation_and_stays_truthful(core):
    """Adoption is a real binding path with no resolution attempt. For an
    inventory-created import it is valid, records NO fabricated attempt, and is
    still normalized by the one selection owner before anything can expand."""
    tree = core.provider.parcel("imported", state=ResourceState.AVAILABLE, files=FILES).observation
    core.provider.inventory_items = (ProviderObservation(
        ProviderResource(core.provider.descriptor.id, {"box_ticket": "imported"}, Ownership.OBSERVED,
                         id=tree.resource.id),
        ResourceState.AVAILABLE, "Imported",
        request=parcel_request("fp-import", "imported", mode="interactive"), file_manifest=tree.file_manifest),)
    await core.engine.reconcile_inventory()

    transfer = next(t for t in await core.repository.active() if t.source == "inventory")
    root = (await core.repository.requests(transfer.id))[0]
    assert root.state == "waiting" and root.resource is not None and root.attempts == 0
    assert root.resource.ownership == Ownership.OBSERVED                 # provenance: inventory + observed
    assert await attempts(transfer.id) == 0 and await provenance(transfer.id) == 0   # nothing fabricated
    assert not [c for c in core.provider.calls if c[0] == "resolve"]
    gens = await generations(transfer.id)                                # exists at BINDING time, before any tick
    assert len(gens) == 1 and gens[0]["decision"] == "pending"

    for _ in range(3):
        await core.engine.resolve_pending()
    assert await children(transfer.id) == [] and await core.repository.artifacts(transfer.id) == ()


@pytest.mark.asyncio
async def test_inventory_adoption_honours_the_predecessor_cleanup_fence(core):
    a = await resolved_parcel(core, "fp-inv")
    resource = (await core.repository.resources(a.id))[0][0]
    await core.repository.delete(a.id, remote=True, now=core.clock())
    await core.repository.cleanup_intent(a.id, resource.id, "user_request")
    fenced = core.provider.resources[resource.id]
    core.provider.inventory_items = (ProviderObservation(
        ProviderResource(resource.provider_id, dict(resource.context), Ownership.OBSERVED, id=resource.id),
        ResourceState.AVAILABLE, "Parcel", request=parcel_request("fp-inv", mode="all")),)
    await core.engine.reconcile_inventory()
    imported = [t for t in await core.repository.active() if t.source == "inventory"]
    assert imported and await binding(imported[0].id) is None            # not adopted while cleanup can still act
    assert fenced is core.provider.resources[resource.id]

    await core.engine.cleanup_pending()
    assert await core.repository.predecessor_cleanup_barrier(imported[0].id) is False


# --------------------------------------------------------------------------- #
# 8.6  No control action can turn missing input into "all"
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_no_control_action_converts_an_undecided_interactive_root_into_all(core):
    core.provider.responses.append(core.provider.parcel("box", state=ResourceState.AVAILABLE, files=FILES))
    t = await core.engine.submit((parcel_request("fp-ctl", mode="interactive"),), name="T")
    await core.engine.resolve_pending()
    view = await core.repository.file_selection_presentation(t.id, now=core.clock())
    assert view["decision"] == "pending"

    async def undecided():
        core.clock.advance(3)                                            # stays inside the 120s user hold
        await core.engine.resolve_pending()
        await core.engine.reconcile_inventory()                          # provider recovery
        await core.engine.reconcile_executions()                         # executor recovery
        current = await core.repository.file_selection_presentation(t.id, now=core.clock())
        assert current["decision"] == "pending" and current["selection_id"] == view["selection_id"]
        assert await children(t.id) == [] and await core.repository.artifacts(t.id) == ()

    await core.engine.pause(t.id)
    await undecided()
    await core.engine.resume(t.id)
    await undecided()
    await core.engine.pause_all()
    await undecided()
    await core.engine.resume_all()
    await undecided()
    await core.engine.retry(t.id)
    await undecided()

    restarted = build_engine(core.tmp_path, core.provider)               # startup reconcile on a fresh process
    restarted.clock.set(core.clock())
    await restarted.engine.initialize()
    await restarted.engine.resolve_pending()
    await restarted.engine.reconcile_inventory()
    await restarted.engine.reconcile_executions()
    current = await core.repository.file_selection_presentation(t.id, now=core.clock())
    assert current["decision"] == "pending" and await children(t.id) == []
    assert (await core.repository.file_selection_presentation(t.id, now=core.clock()))["selection_id"] == view["selection_id"]
    assert not [c for c in core.executor.calls if c[0] == "start"]


# --------------------------------------------------------------------------- #
# 8.9 / 8.10  Controls: explicit ALL and non-manifest transfers are unaffected
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_explicit_all_never_opens_a_generation_and_materializes_everything(core):
    core.provider.responses.append(core.provider.parcel("box", state=ResourceState.AVAILABLE, files=FILES))
    t = await core.engine.submit((parcel_request("fp-all", mode="all"),), name="T")
    await core.engine.resolve_pending()
    assert await generations(t.id) == []
    assert len(await children(t.id)) == 3


@pytest.mark.asyncio
async def test_a_non_manifest_provider_is_unaffected_by_an_interactive_request(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "plain.db")
    await database.init_db()
    core = build_engine(tmp_path, ParcelProvider(file_manifest=False))
    await core.engine.initialize()
    core.provider.responses.append(core.provider.parcel("box", state=ResourceState.AVAILABLE, files=FILES))
    t = await core.engine.submit((parcel_request("fp-plain", mode="interactive"),), name="T")
    await core.engine.resolve_pending()
    assert await generations(t.id) == [] and len(await children(t.id)) == 3   # capability boundary, not request kind


# --------------------------------------------------------------------------- #
# 8.12  Provider-neutral manifest contract at the core capability boundary
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_generic_core_guard_creates_or_holds_and_never_infers_all(core):
    """Capability-level contract, no request kind involved: for a FILE_MANIFEST
    root the owner either yields a governing generation or a HOLD -- never the
    ungoverned 'required=False' that would let fan-out proceed."""
    t = await core.engine.submit((parcel_request("fp-g", mode="interactive"),), name="T")
    record = (await core.repository.requests(t.id))[0]
    resource = core.provider.parcel("box", state=ResourceState.AVAILABLE, files=FILES).observation.resource
    await core.repository.resource_observation(t.id, resource, ResourceState.AVAILABLE)

    authority = await core.repository.ensure_selection_generation(
        record, core.provider.descriptor.id, resource, available=True, file_manifest=None, now=core.clock())
    assert authority.required and authority.governed and not authority.held
    again = await core.repository.ensure_selection_generation(
        record, core.provider.descriptor.id, resource, available=True, file_manifest=None, now=core.clock() + 50)
    assert again == authority and len(await generations(t.id)) == 1      # idempotent, never reset

    await core.repository.delete(t.id, remote=False)                     # a settled transfer can't own a generation
    deleted = await core.repository.ensure_selection_generation(
        record, core.provider.descriptor.id, core.provider.parcel("other", state=ResourceState.AVAILABLE).observation.resource,
        available=True, file_manifest=None, now=core.clock())
    assert deleted.held and not deleted.governed

    plain = await core.engine.submit((parcel_request("fp-plain2", mode="all"),), name="P")
    plain_record = (await core.repository.requests(plain.id))[0]
    ungoverned = await core.repository.ensure_selection_generation(
        plain_record, core.provider.descriptor.id, resource, available=True, file_manifest=None, now=core.clock())
    assert not ungoverned.required and await generations(plain.id) == []


@pytest.mark.asyncio
async def test_commit_selected_manifest_refuses_to_read_a_missing_generation_as_all(core):
    from file_selection_support import executable
    t = await core.engine.submit((parcel_request("fp-c", mode="interactive"),), name="T")
    record = (await core.repository.requests(t.id))[0]
    with pytest.raises(Exception) as excinfo:
        await core.repository.commit_selected_manifest(record, executable(*FILES), now=core.clock())
    assert getattr(excinfo.value, "error", None) is not None
    assert excinfo.value.error.category == Category.RESOURCE_STATE_CONFLICT

    plain = await core.engine.submit((parcel_request("fp-c2", mode="all"),), name="P")
    plain_record = (await core.repository.requests(plain.id))[0]
    result = await core.repository.commit_selected_manifest(plain_record, executable(*FILES), now=core.clock())
    assert len(result) == 3 and result.selection_id is None


# --------------------------------------------------------------------------- #
# 8.13  Provenance / lifecycle isolation between generations
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_fresh_generation_inherits_no_attempts_provenance_selection_or_execution(core):
    a, _view = await _subset_transfer(core, "fp-iso", {"e2"})
    await core.engine.tick()
    a_attempts, a_prov = await attempts(a.id), await provenance(a.id)
    a_gen = (await generations(a.id))[0]["id"]
    a_executions = await rows("SELECT id FROM execution_attempts WHERE transfer_id=?", (a.id,))
    assert a_attempts and a_prov and a_executions

    await core.engine.delete(a.id, remote=True)
    core.provider.responses.append(core.provider.parcel("box", state=ResourceState.AVAILABLE, files=FILES))
    b = await core.engine.submit((parcel_request("fp-iso", mode="interactive"),), name="B")
    await core.engine.resolve_pending()

    assert await attempts(b.id) == 1 and await provenance(b.id) == 1     # its own, not A's
    assert {g["id"] for g in await generations(b.id)}.isdisjoint({a_gen})
    assert await rows("SELECT id FROM execution_attempts WHERE transfer_id=?", (b.id,)) == []
    assert (await rows("SELECT materialized_selection_id FROM transfer_requests WHERE transfer_id=? "
                       "AND parent_id IS NULL", (b.id,)))[0]["materialized_selection_id"] is None
    assert await attempts(a.id) == a_attempts and await provenance(a.id) == a_prov     # A's history untouched
    assert (await generations(a.id))[0]["decision"] == "explicit"


# --------------------------------------------------------------------------- #
# Crash/restart during the lease: bounded, then automatic
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_crashed_owner_is_recovered_by_lease_expiry_after_a_restart_too(core):
    a = await resolved_parcel(core)
    await orphan_cleanup_claim(core, a.id)
    b = await core.engine.submit((parcel_request(),), name="B")

    restarted = build_engine(core.tmp_path, core.provider)
    restarted.clock.set(core.clock())
    await restarted.engine.initialize()
    assert await restarted.repository.predecessor_cleanup_barrier(b.id) is True    # lease still current -> still fenced
    await restarted.engine.resolve_pending()
    assert await attempts(b.id) == 0

    restarted.clock.advance(LEASE + 1)
    await restarted.engine.resolve_pending()
    assert (await binding(a.id))["cleanup_authority"] is None
    assert await attempts(b.id) == 1
