"""DP 1.0.12 opportunistic resolution fairness.

The resolution scheduler (``TransferEngine.resolve_pending``) grants provider
resolution opportunities one fair unit at a time, re-reading current truth at
every admission boundary: a transfer that already owns a viable
executable/materialization path keeps enriching, but never stands an entire
sibling list ahead of an independent transfer that has no path at all.

Every scenario is driven by an explicit fake provider whose ``resolve()`` calls
are individually gated with ``asyncio.Event`` -- no network, no production
monkeypatching, no sleep used as an assertion. ``_until`` only waits for a
durable fact that has no event of its own and fails the test on timeout.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage
from transfers.models import ResolutionResult, ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

GUARD_SECONDS = 10.0


class GatedProvider(ParcelProvider):
    """Parcel provider whose individual resolve() calls the test can hold open."""

    def __init__(self):
        super().__init__()
        self.entered: list[str] = []
        self.finished: list[str] = []
        self.cancelled: list[str] = []
        self.active = 0
        self.max_active = 0
        self.gates: dict[str, asyncio.Event] = {}
        self.failing: set[str] = set()
        self._entry = asyncio.Condition()

    def hold(self, *payloads: str) -> None:
        for payload in payloads:
            self.gates[payload] = asyncio.Event()

    def open(self, *payloads: str) -> None:
        for payload in payloads or tuple(self.gates):
            self.gates[payload].set()

    async def wait_entered(self, *payloads: str) -> None:
        async with self._entry:
            await asyncio.wait_for(
                self._entry.wait_for(lambda: all(item in self.entered for item in payloads)),
                GUARD_SECONDS,
            )

    async def wait_entries(self, count: int) -> None:
        async with self._entry:
            await asyncio.wait_for(self._entry.wait_for(lambda: len(self.entered) >= count), GUARD_SECONDS)

    async def wait_active(self, count: int) -> None:
        async with self._entry:
            await asyncio.wait_for(self._entry.wait_for(lambda: self.active >= count), GUARD_SECONDS)

    async def resolve(self, request):
        payload = request.payload
        self.entered.append(payload)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        async with self._entry:
            self._entry.notify_all()
        try:
            gate = self.gates.get(payload)
            if gate is not None:
                await gate.wait()
        except asyncio.CancelledError:
            self.cancelled.append(payload)
            raise
        finally:
            self.active -= 1
        self.finished.append(payload)
        if payload in self.failing:
            return ResolutionResult(ResourceState.UNKNOWN, error=NormalizedError(
                Domain.NETWORK, Category.CONNECTION_FAILED, Stage.RESOLUTION,
                retryability=Retryability.BACKOFF, origin=Origin.REMOTE_SOURCE,
                integration_id=self.descriptor.id,
            ))
        return ResolutionResult(
            ResourceState.AVAILABLE, (self.candidate(request.name or payload, payload=payload),),
        )


class Runtime:
    def __init__(self, repository, provider, executor, engine, now):
        self.repository = repository
        self.provider = provider
        self.executor = executor
        self.engine = engine
        self.now = now

    async def submit(self, prefix: str, count: int, *, priority=0):
        return await self.engine.submit(
            tuple(TransferRequest("parcel", f"{prefix}-{index}", name=f"{prefix}-{index}.bin")
                  for index in range(count)),
            name=prefix, priority=priority,
        )

    async def request_states(self, transfer_id: int) -> dict[str, str]:
        return {item.request.payload: item.state for item in await self.repository.requests(transfer_id)}

    async def settled(self, payloads) -> bool:
        """Every named provider call's outcome is durable: a materialized
        artifact, or a request failure parked for a later retry."""
        artifacts, parked = set(), set()
        for transfer in await self.repository.active():
            artifacts |= await self.artifact_payloads(transfer.id)
            parked |= {item.request.payload for item in await self.repository.requests(transfer.id)
                       if item.state == "pending" and item.error is not None and item.retry_at > self.now[0]}
        return all(item in artifacts or item in parked for item in payloads)

    async def artifact_payloads(self, transfer_id: int) -> set[str]:
        requests = {item.id: item.request.payload for item in await self.repository.requests(transfer_id)}
        return {requests[item.request_id] for item in await self.repository.artifacts(transfer_id)}


async def _until(predicate) -> None:
    """Wait for a durable fact; a timeout is a test failure, never a pass."""
    async def poll():
        while not await predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), GUARD_SECONDS)


async def _build(tmp_path, monkeypatch, *, concurrency: int) -> Runtime:
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fairness.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = GatedProvider()
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    now = [9000.0]
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(
            retry_delay=1, adoption_stability_seconds=0, max_active_executions=4,
            resolution_concurrency=concurrency, max_attempts=10,
        ),
        clock=lambda: now[0],
    )
    await engine.initialize()
    return Runtime(repository, provider, executor, engine, now)


@pytest_asyncio.fixture
async def build(tmp_path, monkeypatch):
    cycles: list[asyncio.Task] = []
    runtimes: list[Runtime] = []

    async def factory(*, concurrency: int) -> Runtime:
        runtime = await _build(tmp_path, monkeypatch, concurrency=concurrency)
        runtimes.append(runtime)
        return runtime

    factory.cycles = cycles
    yield factory
    for runtime in runtimes:
        runtime.provider.open()
    for task in cycles:
        if not task.done():
            task.cancel()
    await asyncio.gather(*cycles, return_exceptions=True)


async def _drive(runtime: Runtime, cycle: asyncio.Task, expected: list[str], *, arrivals=None) -> None:
    """Open one gated provider call at a time and require the scheduler's
    decision order to be exactly ``expected``.

    Before a gate is opened every earlier call's post-resolution work is
    durable (an artifact, or a recorded request failure), so each admission
    boundary the scheduler sees is a function of settled truth only.

    ``arrivals[payload]`` is submitted while ``payload`` still owns the only
    provider slot -- i.e. strictly before the admission boundary that opening
    it creates -- and every such transfer's requests are held as well.
    """
    provider = runtime.provider
    for position, payload in enumerate(expected):
        await provider.wait_entries(position + 1)
        assert provider.entered == expected[:position + 1]
        await _until(lambda: runtime.settled(expected[:position]))
        for prefix, count in (arrivals or {}).get(payload, ()):
            provider.hold(*(f"{prefix}-{index}" for index in range(count)))
            await runtime.submit(prefix, count)
        provider.open(payload)
    await asyncio.wait_for(cycle, GUARD_SECONDS)
    assert provider.entered == expected
    assert provider.max_active == 1


def _cycle(build, runtime: Runtime) -> asyncio.Task:
    task = asyncio.create_task(runtime.engine.resolve_pending())
    build.cycles.append(task)
    return task


async def _productive_transfer_with_blocked_enrichment(build, runtime: Runtime, *, requests: int, blocked: int):
    """Transfer A: its first request yields a viable path, ``blocked`` sibling
    requests then sit inside the provider, the rest are not yet admitted."""
    provider = runtime.provider
    transfer = await runtime.submit("a", requests)
    provider.hold(*(f"a-{index}" for index in range(1, requests)))
    cycle = _cycle(build, runtime)
    await provider.wait_entered(*(f"a-{index}" for index in range(1, blocked + 1)))

    async def first_path_exists():
        return "a-0" in await runtime.artifact_payloads(transfer.id)
    await _until(first_path_exists)
    return transfer, cycle


@pytest.mark.asyncio
async def test_later_transfer_resolves_while_earlier_cycle_is_still_blocked(build):
    """Section 13.1. A provider slot is idle and B is runnable, yet on BASE B
    cannot begin provider resolution until A's whole previously-admitted
    sibling set drains. Nothing of A is released before B must have started."""
    runtime = await build(concurrency=3)
    provider = runtime.provider
    a, cycle = await _productive_transfer_with_blocked_enrichment(build, runtime, requests=3, blocked=2)

    b = await runtime.submit("b", 1)
    # The production scheduler answers the submission wakeup with another
    # resolve_pending() call while the first cycle is still running.
    follow_up = _cycle(build, runtime)

    await provider.wait_entered("b-0")
    assert provider.finished.count("a-1") == provider.finished.count("a-2") == 0
    assert not cycle.done()

    async def b_has_path():
        return "b-0" in await runtime.artifact_payloads(b.id)
    await _until(b_has_path)
    assert provider.cancelled == []
    assert (await runtime.request_states(a.id))["a-1"] == "resolving"

    provider.open()
    await asyncio.wait_for(asyncio.gather(cycle, follow_up), GUARD_SECONDS)
    assert await runtime.artifact_payloads(a.id) == {"a-0", "a-1", "a-2"}


@pytest.mark.asyncio
async def test_new_transfer_gets_the_next_opportunity_without_preemption(build):
    """Section 13.2. Every slot is owned by A's in-flight enrichment; exactly
    one opportunity is released and B -- not A's next sibling -- receives it."""
    runtime = await build(concurrency=2)
    provider = runtime.provider
    a, cycle = await _productive_transfer_with_blocked_enrichment(build, runtime, requests=6, blocked=2)
    # Units admitted together may reach the provider in either order; WHICH
    # calls were admitted, and who receives the next opportunity, is exact.
    assert sorted(provider.entered) == ["a-0", "a-1", "a-2"]

    await runtime.submit("b", 1)
    provider.open("a-1")
    await provider.wait_entered("b-0")

    assert provider.entered[3:] == ["b-0"]
    assert provider.cancelled == []
    assert "a-2" not in provider.finished, "in-flight provider work must never be preempted"

    # B has nothing further to ask for, so the capacity returns to A's enrichment.
    await provider.wait_entered("a-3")
    assert provider.max_active <= 2

    provider.open()
    await asyncio.wait_for(cycle, GUARD_SECONDS)
    assert await runtime.artifact_payloads(a.id) == {f"a-{index}" for index in range(6)}


@pytest.mark.asyncio
async def test_single_transfer_uses_every_configured_resolution_slot(build):
    """Section 13.3. Fairness never serializes a lone transfer."""
    runtime = await build(concurrency=3)
    provider = runtime.provider
    a = await runtime.submit("a", 5)
    provider.hold(*(f"a-{index}" for index in range(5)))
    cycle = _cycle(build, runtime)

    await provider.wait_active(3)
    assert provider.active == 3
    assert len(provider.entered) == 3

    provider.open()
    await asyncio.wait_for(cycle, GUARD_SECONDS)
    assert provider.max_active == 3
    assert await runtime.artifact_payloads(a.id) == {f"a-{index}" for index in range(5)}


@pytest.mark.asyncio
async def test_bootstrap_transfers_share_capacity_instead_of_draining_in_arrival_order(build):
    """Section 13.4. A's long sibling list does not occupy every slot while B
    and C -- equally pathless -- wait behind it."""
    runtime = await build(concurrency=3)
    provider = runtime.provider
    a = await runtime.submit("a", 6)
    b = await runtime.submit("b", 2)
    c = await runtime.submit("c", 2)
    provider.hold(*(f"{prefix}-{index}" for prefix, count in (("a", 6), ("b", 2), ("c", 2))
                    for index in range(count)))
    cycle = _cycle(build, runtime)

    await provider.wait_active(3)
    assert sorted(provider.entered) == ["a-0", "b-0", "c-0"]

    provider.open()
    await asyncio.wait_for(cycle, GUARD_SECONDS)
    assert provider.max_active == 3
    for transfer, prefix, count in ((a, "a", 6), (b, "b", 2), (c, "c", 2)):
        assert await runtime.artifact_payloads(transfer.id) == {f"{prefix}-{index}" for index in range(count)}


@pytest.mark.asyncio
async def test_bootstrap_rotation_is_deterministic_round_robin(build):
    """Section 13.4. With one slot the provider-entry order is exactly the
    scheduler's decision order: pathless transfers rotate, none drains first."""
    runtime = await build(concurrency=1)
    provider = runtime.provider
    await runtime.submit("a", 3)
    await runtime.submit("b", 3)
    await runtime.submit("c", 3)
    everything = [f"{prefix}-{index}" for index in range(3) for prefix in "abc"]
    provider.failing = set(everything)
    provider.hold(*everything)

    await _drive(runtime, _cycle(build, runtime),
                 ["a-0", "b-0", "c-0", "a-1", "b-1", "c-1", "a-2", "b-2", "c-2"])


@pytest.mark.asyncio
async def test_pathless_transfer_outranks_enrichment_without_starving_it(build):
    """Sections 4.3, 4.7, 4.8. Three transfers already own a viable path and
    still have enrichment work; B has none and keeps failing. B receives the
    first opportunity of every round, and exactly one enrichment opportunity
    follows each round, so neither class starves the other."""
    runtime = await build(concurrency=1)
    provider = runtime.provider
    productive = [await runtime.submit(prefix, 3) for prefix in ("p", "q", "r")]
    provider.failing = {f"{prefix}-{index}" for prefix in "pqr" for index in (1, 2)}
    await runtime.engine.resolve_pending()
    for transfer, prefix in zip(productive, "pqr"):
        assert await runtime.artifact_payloads(transfer.id) == {f"{prefix}-0"}

    await runtime.submit("b", 3)
    provider.failing = {"b-0", "b-1", "b-2"}
    provider.hold(*(f"{prefix}-{index}" for prefix in "pqr" for index in (1, 2)), "b-0", "b-1", "b-2")
    provider.entered.clear()
    runtime.now[0] += 60

    await _drive(runtime, _cycle(build, runtime), [
        "b-0", "p-1", "b-1", "q-1", "b-2", "r-1",
        "p-2", "q-2", "r-2",
    ])
    for transfer, prefix in zip(productive, "pqr"):
        assert await runtime.artifact_payloads(transfer.id) == {f"{prefix}-{index}" for index in range(3)}


@pytest.mark.asyncio
async def test_classification_follows_current_truth_inside_one_cycle(build):
    """Section 4.7. A enters the cycle pathless and gains a writer path during
    it. From the next admission boundary its siblings are enrichment: B, still
    pathless, outranks them although A was served less recently. A cycle-wide
    classification snapshot would grant ``a-1`` where ``b-1`` is required."""
    runtime = await build(concurrency=1)
    provider = runtime.provider
    productive = await runtime.submit("p", 4)
    provider.failing = {"p-1", "p-2", "p-3"}
    await runtime.engine.resolve_pending()
    assert await runtime.artifact_payloads(productive.id) == {"p-0"}

    a = await runtime.submit("a", 4)
    await runtime.submit("b", 3)
    provider.failing = {"b-0", "b-1", "b-2"}
    provider.hold(*(f"{prefix}-{index}" for prefix, indexes in (("p", (1, 2, 3)), ("a", range(4)), ("b", range(3)))
                    for index in indexes))
    provider.entered.clear()
    runtime.now[0] += 60
    cycle = _cycle(build, runtime)

    await _drive(runtime, cycle,
                 ["a-0", "b-0", "p-1", "b-1", "a-1", "b-2", "p-2", "a-2", "p-3", "a-3"])
    assert await runtime.artifact_payloads(a.id) == {f"a-{index}" for index in range(4)}


@pytest.mark.asyncio
async def test_sustained_pathless_arrivals_never_starve_enrichment(build):
    """Section 4.8. A bootstrap round has bounded membership: the pathless
    transfers runnable when it starts. A is productive with ready enrichment;
    a new pathless transfer is submitted before EVERY admission boundary.

    Arrivals never extend the round in progress -- they join the next one --
    so A is granted exactly one enrichment opportunity after each round however
    long the arrivals continue, every arrival is still served before any
    further enrichment once its round starts, a member that stays pathless
    (``b1``) is served again in the following round, and A's remaining
    enrichment completes. Nothing in flight is preempted and the single slot
    is re-admitted at every boundary, round change or not.
    """
    runtime = await build(concurrency=1)
    provider = runtime.provider
    a = await runtime.submit("a", 4)
    provider.failing = {"a-1", "a-2", "a-3"}
    await runtime.engine.resolve_pending()
    assert await runtime.artifact_payloads(a.id) == {"a-0"}

    b1 = await runtime.submit("b1", 2)
    provider.failing = {"b1-0", "b1-1"}
    provider.hold("a-1", "a-2", "a-3", "b1-0", "b1-1")
    provider.entered.clear()
    runtime.now[0] += 60

    await _drive(runtime, _cycle(build, runtime), [
        "b1-0",                     # round 1 = {b1}
        "a-1",                      # round 1 served -> one enrichment turn (b2 already waiting)
        "b2-0", "b3-0", "b1-1",     # round 2 = {b1, b2, b3}; b4 and b5 arrive during it
        "a-2",                      # round 2 served -> one enrichment turn (b4, b5 already waiting)
        "b4-0", "b5-0",             # round 3 = {b4, b5}
        "a-3",                      # enrichment continues to completion
    ], arrivals={
        "b1-0": [("b2", 1)],
        "a-1": [("b3", 1)],
        "b2-0": [("b4", 1)],
        "b3-0": [("b5", 1)],
    })

    assert provider.cancelled == []
    assert await runtime.artifact_payloads(a.id) == {"a-0", "a-1", "a-2", "a-3"}
    assert await runtime.artifact_payloads(b1.id) == set()
    arrived = {item.name: item for item in await runtime.repository.active()}
    for prefix in ("b2", "b3", "b4", "b5"):
        assert await runtime.artifact_payloads(arrived[prefix].id) == {f"{prefix}-0"}


@pytest.mark.asyncio
async def test_round_changes_never_idle_a_resolution_slot(build):
    """Sections 4.6, 4.8. Two slots, a productive A and one pathless arrival
    before every boundary: exactly one call is opened at a time and the freed
    slot is re-admitted at once across every round change, while A keeps
    receiving its one enrichment turn per bootstrap round."""
    runtime = await build(concurrency=2)
    provider = runtime.provider
    a, cycle = await _productive_transfer_with_blocked_enrichment(build, runtime, requests=5, blocked=2)
    assert sorted(provider.entered) == ["a-0", "a-1", "a-2"]

    steps = [            # (arrival submitted first, call then opened, call admitted into the freed slot)
        ("b1", "a-1", "b1-0"),   # no round populated: the new pathless transfer is next
        ("b2", "a-2", "a-3"),    # round {b1} served -> enrichment turn; b2 joins the next round
        ("b3", "b1-0", "b2-0"),  # round {b2, b3}
        ("b4", "a-3", "b3-0"),   # ... b4 arrived too late for it
        ("b5", "b2-0", "a-4"),   # round served -> enrichment turn
        (None, "b3-0", "b4-0"),  # round {b4, b5}
        (None, "a-4", "b5-0"),
    ]
    for position, (arrival, opened, admitted) in enumerate(steps):
        if arrival:
            provider.hold(f"{arrival}-0")
            await runtime.submit(arrival, 1)
        provider.open(opened)
        await provider.wait_entries(4 + position)
        assert provider.entered[-1] == admitted
        assert provider.entered[3:] == [step[2] for step in steps[:position + 1]]
        await provider.wait_active(2)
        assert provider.active == 2

    provider.open()
    await asyncio.wait_for(cycle, GUARD_SECONDS)
    assert provider.cancelled == [] and provider.max_active == 2
    assert await runtime.artifact_payloads(a.id) == {f"a-{index}" for index in range(5)}
    for transfer in await runtime.repository.active():
        if transfer.id != a.id:
            assert await runtime.artifact_payloads(transfer.id) == {f"{transfer.name}-0"}


@pytest.mark.asyncio
async def test_explicit_priority_outranks_scheduling_class(build):
    """Section 4.3. A user-assigned higher priority is preserved: productive
    high-priority enrichment still precedes a pathless lower-priority transfer."""
    runtime = await build(concurrency=1)
    provider = runtime.provider
    high = await runtime.submit("h", 3, priority=5)
    provider.failing = {"h-1", "h-2"}
    await runtime.engine.resolve_pending()
    assert await runtime.artifact_payloads(high.id) == {"h-0"}

    await runtime.submit("b", 2)
    provider.failing = set()
    provider.hold("h-1", "h-2", "b-0", "b-1")
    provider.entered.clear()
    runtime.now[0] += 60

    await _drive(runtime, _cycle(build, runtime), ["h-1", "h-2", "b-0", "b-1"])


@pytest.mark.asyncio
async def test_enrichment_is_never_abandoned(build):
    """Section 13.5. Every sibling request still resolves after its transfer
    already owns a writer path, across all competing transfers."""
    runtime = await build(concurrency=2)
    transfers = {prefix: await runtime.submit(prefix, 4) for prefix in ("a", "b", "c")}

    await runtime.engine.resolve_pending()

    for prefix, transfer in transfers.items():
        assert await runtime.artifact_payloads(transfer.id) == {f"{prefix}-{index}" for index in range(4)}
        assert set((await runtime.request_states(transfer.id)).values()) == {"resolved"}
    assert sorted(runtime.provider.entered) == sorted(
        f"{prefix}-{index}" for prefix in "abc" for index in range(4))
    assert runtime.provider.max_active <= 2


@pytest.mark.asyncio
async def test_large_multilink_has_bounded_admitted_work(build):
    """Section 13.6. A 100-request transfer holds ``resolution_concurrency``
    admitted units, not one parked coroutine per request."""
    runtime = await build(concurrency=3)
    provider = runtime.provider
    a = await runtime.submit("a", 100)
    provider.hold(*(f"a-{index}" for index in range(100)))
    baseline = len(asyncio.all_tasks())
    cycle = _cycle(build, runtime)

    await provider.wait_active(3)
    states = await runtime.request_states(a.id)
    assert sum(state == "resolving" for state in states.values()) == 3
    assert sum(state == "pending" for state in states.values()) == 97
    # The cycle itself plus one admitted unit per configured slot.
    assert len(asyncio.all_tasks()) - baseline <= 1 + 3

    b = await runtime.submit("b", 1)
    provider.open("a-0")
    await provider.wait_entered("b-0")
    assert len(provider.entered) == 4
    assert len(asyncio.all_tasks()) - baseline <= 1 + 3 + 1

    provider.open()
    await asyncio.wait_for(cycle, 60)
    assert provider.max_active == 3
    assert len(await runtime.artifact_payloads(a.id)) == 100
    assert await runtime.artifact_payloads(b.id) == {"b-0"}


@pytest.mark.asyncio
async def test_paused_and_cancelled_work_is_never_admitted(build):
    """Section 14.6. Fair admission re-checks liveness before provider side
    effects; it never resurrects paused or cancelled transfers."""
    runtime = await build(concurrency=1)
    provider = runtime.provider
    paused = await runtime.submit("p", 2)
    cancelled = await runtime.submit("x", 2)
    await runtime.submit("l", 2)
    await runtime.engine.pause(paused.id)
    await runtime.engine.cancel(cancelled.id)

    await runtime.engine.resolve_pending()

    assert provider.entered == ["l-0", "l-1"]
