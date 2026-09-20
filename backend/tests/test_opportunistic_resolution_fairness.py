"""DP 1.0.12 opportunistic resolution fairness.

The resolution scheduler (``TransferEngine.resolve_pending``) grants provider
resolution opportunities one fair unit at a time, re-reading current truth at
every admission boundary: a transfer that already owns a viable
executable/materialization path keeps enriching, but never stands an entire
sibling list ahead of an independent transfer that has no path at all.

A cycle boundary is never latency policy (reactive resolution closeout): a
request the cycle's own work created (a manifest child) and a request an
operator Retry / Resume / Resume All made runnable are admitted by the SAME
running cycle through the same bounded fair admission -- while a request the
cycle already admitted is never admitted twice merely because current truth
is re-read.

Every scenario is driven by an explicit fake provider whose ``resolve()`` calls
are individually gated with ``asyncio.Event`` -- no network, no production
monkeypatching, no sleep used as an assertion. ``_until`` only waits for a
durable fact that has no event of its own and fails the test on timeout.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage
from transfers.input_required import auth_required, username_password
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
        # Payloads whose failure is permanent: the request is durably
        # ``failed`` and only an operator Retry requeues it.
        self.terminal: set[str] = set()
        # Root payloads that resolve to an AVAILABLE resource whose manifest
        # fans out into child requests (``_members``) instead of a candidate.
        self.parcels: dict[str, tuple] = {}
        # Root payloads that first demand provider input. Their continuation
        # is gated and recorded like a resolve() call, under ``input:<payload>``;
        # a submitted password of ``rejected`` is challenged again.
        self.auth: set[str] = set()
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
        if request.payload in self.auth:
            self.entered.append(request.payload)
            self.finished.append(request.payload)
            return ResolutionResult(ResourceState.UNKNOWN, input_required=auth_required(username_password()))
        return await self._call(request, request.payload)

    async def resolve_with_input(self, request, submitted):
        if submitted.value("password") == "rejected":
            self.entered.append(f"input:{request.payload}")
            self.finished.append(f"input:{request.payload}")
            return ResolutionResult(ResourceState.UNKNOWN, input_required=auth_required(username_password()))
        return await self._call(request, f"input:{request.payload}")

    async def _call(self, request, key: str):
        payload = request.payload
        self.entered.append(key)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        async with self._entry:
            self._entry.notify_all()
        try:
            gate = self.gates.get(key)
            if gate is not None:
                await gate.wait()
        except asyncio.CancelledError:
            self.cancelled.append(key)
            raise
        finally:
            self.active -= 1
        self.finished.append(key)
        if payload in self.terminal:
            return ResolutionResult(ResourceState.UNKNOWN, error=NormalizedError(
                Domain.PROVIDER, Category.SOURCE_NOT_FOUND, Stage.RESOLUTION,
                retryability=Retryability.NEVER, origin=Origin.REMOTE_SOURCE,
                integration_id=self.descriptor.id,
            ))
        if payload in self.failing:
            return ResolutionResult(ResourceState.UNKNOWN, error=NormalizedError(
                Domain.NETWORK, Category.CONNECTION_FAILED, Stage.RESOLUTION,
                retryability=Retryability.BACKOFF, origin=Origin.REMOTE_SOURCE,
                integration_id=self.descriptor.id,
            ))
        if payload in self.parcels:
            return self.parcel(payload, state=ResourceState.AVAILABLE, files=self.parcels[payload])
        return ResolutionResult(
            ResourceState.AVAILABLE, (self.candidate(request.name or payload, payload=payload),),
        )


class GatedExecutor(MemoryExecutor):
    """Memory executor whose native resume() the test can hold open."""

    def __init__(self, authorize):
        super().__init__(authorize)
        self.resume_gate: asyncio.Event | None = None
        self.resuming = asyncio.Event()

    async def resume(self, handle):
        if self.resume_gate is not None:
            self.resuming.set()
            await self.resume_gate.wait()
        return await super().resume(handle)


def _members(payload: str, count: int) -> tuple:
    """Manifest of a parcel root: ``count`` files, one child request each."""
    return tuple((f"{payload}-m{index}.bin", f"{payload}/m{index}.bin", 4) for index in range(count))


def _member(payload: str, index: int) -> str:
    """Provider payload of the ``index``-th child of parcel root ``payload``."""
    return f"{payload}:{payload}/m{index}.bin"


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
        artifact, a root whose manifest fan-out is committed, or a request
        failure parked for a later retry."""
        artifacts, parked = set(), set()
        for transfer in await self.repository.active():
            artifacts |= await self.artifact_payloads(transfer.id)
            requests = await self.repository.requests(transfer.id)
            artifacts |= {item.request.payload for item in requests
                          if item.state == "resolved" and any(child.parent_id == item.id for child in requests)}
            parked |= {item.request.payload for item in requests
                       if item.state == "pending" and item.error is not None and item.retry_at > self.now[0]}
        return all(item.removeprefix("input:") in artifacts or item in parked for item in payloads)

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
    executor = GatedExecutor(repository.authorize_execution)
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


# ---------------------------------------------------------------------------
# Reactive resolution: work that becomes runnable DURING a running cycle.
# ---------------------------------------------------------------------------


async def _fan_out_is_durable(runtime: Runtime, transfer_id: int, root: str) -> None:
    """The root's manifest fan-out is committed: its children are durable."""
    async def committed():
        records = await runtime.repository.requests(transfer_id)
        return any(item.request.payload == root and item.state == "resolved" for item in records) and any(
            item.parent_id for item in records)
    await _until(committed)


@pytest.mark.asyncio
async def test_manifest_child_is_admitted_by_the_cycle_that_created_it(build):
    """A root resolves inside a running cycle and its manifest fan-out makes a
    child durably runnable while ``x-0`` keeps that one cycle alive with a
    provider slot idle. On BASE the child is "the next cycle's work" and never
    reaches the provider before this cycle returns; the root is never
    re-admitted by re-reading current truth."""
    runtime = await build(concurrency=2)
    provider = runtime.provider
    provider.parcels["a-0"] = _members("a-0", 1)
    child = _member("a-0", 0)
    provider.hold("x-0", child)
    await runtime.submit("x", 1)
    a = await runtime.submit("a", 1)
    cycle = _cycle(build, runtime)

    await provider.wait_entered("x-0")
    await _fan_out_is_durable(runtime, a.id, "a-0")
    await provider.wait_entered(child)
    assert not cycle.done(), "the child was admitted by the cycle that created it"
    assert "x-0" not in provider.finished and provider.cancelled == []
    assert provider.entered.count("a-0") == 1

    provider.open()
    await asyncio.wait_for(cycle, GUARD_SECONDS)
    assert provider.entered.count("a-0") == 1 and provider.entered.count(child) == 1
    assert await runtime.artifact_payloads(a.id) == {child}
    assert set((await runtime.request_states(a.id)).values()) == {"resolved"}


@pytest.mark.asyncio
async def test_manifest_children_join_bounded_admission(build):
    """A root that fans out into 40 children parks nothing: the children are
    admitted through the same bounded machinery -- ``resolution_concurrency``
    units in the provider, never one coroutine per child -- and a new
    independent transfer still receives the next opportunity."""
    runtime = await build(concurrency=3)
    provider = runtime.provider
    provider.parcels["a-0"] = _members("a-0", 40)
    children = [_member("a-0", index) for index in range(40)]
    provider.hold(*children)
    a = await runtime.submit("a", 1)
    baseline = len(asyncio.all_tasks())
    cycle = _cycle(build, runtime)

    await provider.wait_active(3)
    assert provider.active == 3 and len(provider.entered) == 1 + 3
    states = await runtime.request_states(a.id)
    assert sum(states[child] == "resolving" for child in children) == 3
    assert sum(states[child] == "pending" for child in children) == 37
    # The cycle itself plus one admitted unit per configured slot.
    assert len(asyncio.all_tasks()) - baseline <= 1 + 3

    b = await runtime.submit("b", 1)
    provider.open(provider.entered[1])
    await provider.wait_entered("b-0")
    assert len(provider.entered) == 1 + 3 + 1
    assert len(asyncio.all_tasks()) - baseline <= 1 + 3 + 1

    provider.open()
    await asyncio.wait_for(cycle, 60)
    assert provider.max_active == 3 and provider.cancelled == []
    assert sorted(provider.entered) == sorted(["a-0", "b-0", *children])
    assert await runtime.artifact_payloads(a.id) == set(children)
    assert await runtime.artifact_payloads(b.id) == {"b-0"}


@pytest.mark.asyncio
async def test_manifest_children_take_no_fast_lane(build):
    """Children are ordinary work of their transfer. A's root used A's turn of
    the bootstrap round, so its first child waits for pathless B and for the
    one enrichment turn owed to productive P; once that child gives A a path
    the second child is enrichment and again follows pathless B and P."""
    runtime = await build(concurrency=1)
    provider = runtime.provider
    productive = await runtime.submit("p", 3)
    provider.failing = {"p-1", "p-2"}
    await runtime.engine.resolve_pending()
    assert await runtime.artifact_payloads(productive.id) == {"p-0"}

    provider.parcels["a-0"] = _members("a-0", 2)
    first, second = _member("a-0", 0), _member("a-0", 1)
    a = await runtime.submit("a", 1)
    await runtime.submit("b", 2)
    provider.failing = {"b-0", "b-1"}
    provider.hold("p-1", "p-2", "a-0", first, second, "b-0", "b-1")
    provider.entered.clear()
    runtime.now[0] += 60

    await _drive(runtime, _cycle(build, runtime), ["a-0", "b-0", "p-1", first, "b-1", "p-2", second])
    assert await runtime.artifact_payloads(a.id) == {first, second}
    assert await runtime.artifact_payloads(productive.id) == {"p-0", "p-1", "p-2"}


@pytest.mark.parametrize("command", ["delete", "cancel"])
@pytest.mark.asyncio
async def test_child_of_a_transfer_retired_before_its_admission_never_executes(build, command):
    """Section 23. A's children become durable while B owns the only slot; A
    is then deleted/cancelled. Same-cycle discovery never resurrects them."""
    runtime = await build(concurrency=1)
    provider = runtime.provider
    provider.parcels["a-0"] = _members("a-0", 3)
    provider.hold("a-0", "b-0", *(_member("a-0", index) for index in range(3)))
    a = await runtime.submit("a", 1)
    b = await runtime.submit("b", 1)
    cycle = _cycle(build, runtime)

    await provider.wait_entered("a-0")
    provider.open("a-0")
    await provider.wait_entered("b-0")
    await _fan_out_is_durable(runtime, a.id, "a-0")
    if command == "delete":
        await runtime.engine.delete(a.id, remote=False)
    else:
        await runtime.engine.cancel(a.id)

    provider.open("b-0")
    await asyncio.wait_for(cycle, GUARD_SECONDS)
    assert provider.entered == ["a-0", "b-0"]
    assert provider.cancelled == []
    assert await runtime.artifact_payloads(b.id) == {"b-0"}


@pytest.mark.asyncio
async def test_paused_child_is_not_admitted_until_resume_wakes_the_same_cycle(build):
    """Section 23 + 8.1. A is paused after its child became durable: the child
    makes no provider contact while paused, and Resume hands it to the SAME
    running cycle -- exactly once."""
    runtime = await build(concurrency=1)
    provider = runtime.provider
    provider.parcels["a-0"] = _members("a-0", 1)
    child = _member("a-0", 0)
    provider.hold("a-0", child, "b-0", "b-1")
    a = await runtime.submit("a", 1)
    await runtime.submit("b", 2)
    cycle = _cycle(build, runtime)

    await provider.wait_entered("a-0")
    provider.open("a-0")
    await provider.wait_entered("b-0")
    await _fan_out_is_durable(runtime, a.id, "a-0")
    await runtime.engine.pause(a.id)
    provider.open("b-0")
    await provider.wait_entered("b-1")
    assert child not in provider.entered

    assert await runtime.engine.resume(a.id) == ()
    provider.open("b-1")
    await provider.wait_entered(child)
    assert not cycle.done()

    provider.open()
    await asyncio.wait_for(cycle, GUARD_SECONDS)
    assert provider.entered == ["a-0", "b-0", "b-1", child]
    assert await runtime.artifact_payloads(a.id) == {child}


async def _terminally_failed(runtime: Runtime, prefix: str, count: int, *, terminal, failing=()):
    """A transfer whose named requests failed in an EARLIER cycle: ``terminal``
    ones are durably ``failed`` (only an operator Retry requeues them)."""
    provider = runtime.provider
    transfer = await runtime.submit(prefix, count)
    provider.terminal, provider.failing = set(terminal), set(failing)
    await runtime.engine.resolve_pending()
    states = await runtime.request_states(transfer.id)
    assert all(states[payload] == "failed" for payload in terminal)
    provider.terminal = set()
    provider.entered.clear()
    runtime.now[0] += 60
    return transfer


@pytest.mark.asyncio
async def test_operator_retry_wakes_the_running_cycle(build):
    """Section 7. B's request failed permanently before this cycle, so the
    cycle passed B over. Operator Retry durably requeues it while A still owns
    the cycle and a slot is idle; on BASE the requeued request waits for the
    cycle to end."""
    runtime = await build(concurrency=2)
    provider = runtime.provider
    b = await _terminally_failed(runtime, "b", 1, terminal={"b-0"})
    provider.hold("a-0", "b-0")
    await runtime.submit("a", 1)
    cycle = _cycle(build, runtime)
    await provider.wait_entered("a-0")
    assert provider.entered == ["a-0"]

    assert await runtime.engine.retry(b.id) is True
    assert (await runtime.request_states(b.id))["b-0"] in {"pending", "resolving"}
    await provider.wait_entered("b-0")
    assert not cycle.done(), "Retry was served by the running cycle"
    assert "a-0" not in provider.finished and provider.cancelled == []

    provider.open()
    await asyncio.wait_for(cycle, GUARD_SECONDS)
    assert provider.entered == ["a-0", "b-0"]
    assert await runtime.artifact_payloads(b.id) == {"b-0"}


@pytest.mark.asyncio
async def test_retry_racing_in_flight_work_requeues_without_duplicating_it(build):
    """Section 7.3. ``b-1`` is inside the provider when Retry arrives. Retry
    serializes behind that in-flight unit, only the permanently failed ``b-0``
    is a new incarnation, and ``b-1`` is never admitted a second time."""
    runtime = await build(concurrency=3)
    provider = runtime.provider
    b = await _terminally_failed(runtime, "b", 2, terminal={"b-0"}, failing={"b-1"})
    provider.hold("a-0", "b-0", "b-1")
    await runtime.submit("a", 1)
    cycle = _cycle(build, runtime)
    await provider.wait_entered("a-0", "b-1")

    retry = asyncio.create_task(runtime.engine.retry(b.id))
    build.cycles.append(retry)
    await asyncio.sleep(0)
    assert not retry.done()
    assert (await runtime.request_states(b.id)) == {"b-0": "failed", "b-1": "resolving"}

    provider.open("b-1")
    assert await asyncio.wait_for(retry, GUARD_SECONDS) is True
    await provider.wait_entered("b-0")
    assert not cycle.done() and "a-0" not in provider.finished

    provider.open()
    await asyncio.wait_for(cycle, GUARD_SECONDS)
    assert sorted(provider.entered) == ["a-0", "b-0", "b-1"]
    assert provider.cancelled == []
    assert await runtime.artifact_payloads(b.id) == {"b-0"}


@pytest.mark.asyncio
async def test_reconsidering_a_transfer_never_readmits_its_in_flight_requests(build):
    """Sections 5.3, 9.C. Resume names B while both of its requests are inside
    the provider and durably ``resolving`` -- a state that looks schedulable
    when current truth is re-read. With a slot idle neither is admitted again;
    ``c-0``, submitted afterwards, proves the scheduler did reassess."""
    runtime = await build(concurrency=4)
    provider = runtime.provider
    provider.hold("a-0", "b-0", "b-1")
    await runtime.submit("a", 1)
    b = await runtime.submit("b", 2)
    cycle = _cycle(build, runtime)
    await provider.wait_entered("a-0", "b-0", "b-1")

    await runtime.engine.pause(b.id)
    assert await runtime.engine.resume(b.id) == ()
    await runtime.submit("c", 1)
    await provider.wait_entered("c-0")
    assert sorted(provider.entered) == ["a-0", "b-0", "b-1", "c-0"]
    assert await runtime.request_states(b.id) == {"b-0": "resolving", "b-1": "resolving"}

    provider.open()
    await asyncio.wait_for(cycle, GUARD_SECONDS)
    assert sorted(provider.entered) == ["a-0", "b-0", "b-1", "c-0"]
    assert await runtime.artifact_payloads(b.id) == {"b-0", "b-1"}


@pytest.mark.asyncio
async def test_resume_wakes_the_running_cycle_and_leaves_paused_siblings_paused(build):
    """Sections 8.1, 18.3. B and S are paused before the cycle, which passes
    both over. Resume(B) makes B admissible while A still owns the cycle; on
    BASE B waits for the cycle to end. S stays paused and untouched."""
    runtime = await build(concurrency=2)
    provider = runtime.provider
    provider.hold("a-0", "b-0")
    await runtime.submit("a", 1)
    b = await runtime.submit("b", 1)
    s = await runtime.submit("s", 1)
    await runtime.engine.pause(b.id)
    await runtime.engine.pause(s.id)
    cycle = _cycle(build, runtime)
    await provider.wait_entered("a-0")

    assert await runtime.engine.resume(b.id) == ()
    await provider.wait_entered("b-0")
    assert not cycle.done(), "Resume was served by the running cycle"
    assert "a-0" not in provider.finished and provider.cancelled == []
    assert (await runtime.repository.get(s.id)).paused

    provider.open()
    await asyncio.wait_for(cycle, GUARD_SECONDS)
    assert provider.entered == ["a-0", "b-0"]
    assert await runtime.artifact_payloads(b.id) == {"b-0"}
    assert (await runtime.repository.get(s.id)).paused


@pytest.mark.asyncio
async def test_resume_one_under_global_pause_admits_only_that_transfer(build):
    """Section 8.3. Resume of one transfer while globally paused keeps the
    existing contract -- every sibling becomes individually paused, the global
    pause ends -- and only the resumed transfer reaches the provider."""
    runtime = await build(concurrency=2)
    provider = runtime.provider
    provider.hold("a-0", "b-0")
    a = await runtime.submit("a", 1)
    b = await runtime.submit("b", 1)
    s = await runtime.submit("s", 1)
    await runtime.engine.pause(b.id)
    await runtime.engine.pause(s.id)
    cycle = _cycle(build, runtime)
    await provider.wait_entered("a-0")
    await runtime.engine.pause_all()
    assert provider.entered == ["a-0"]

    assert await runtime.engine.resume(b.id) == ()
    await provider.wait_entered("b-0")
    assert not cycle.done()
    assert not await runtime.repository.globally_paused()
    assert (await runtime.repository.get(a.id)).paused and (await runtime.repository.get(s.id)).paused

    provider.open()
    await asyncio.wait_for(cycle, GUARD_SECONDS)
    assert provider.entered == ["a-0", "b-0"]


@pytest.mark.asyncio
async def test_resume_reaches_the_scheduler_before_artifact_recovery_returns(build):
    """Section 21.3 boundary. ``resume()`` holds no transfer lock, and its
    durable unpause is complete before it starts artifact recovery, so that is
    where the scheduler is told: B's parked request reaches the provider while
    ``resume()`` is still blocked inside the executor's native resume."""
    runtime = await build(concurrency=2)
    provider, executor = runtime.provider, runtime.executor
    b = await runtime.submit("b", 2)
    provider.failing = {"b-1"}
    await runtime.engine.resolve_pending()
    await runtime.engine.reconcile_executions()
    assert await runtime.artifact_payloads(b.id) == {"b-0"}
    assert [call[0] for call in executor.calls].count("start") == 1
    await runtime.engine.pause(b.id)

    provider.failing = set()
    provider.entered.clear()
    runtime.now[0] += 60
    provider.hold("a-0", "b-1")
    await runtime.submit("a", 1)
    cycle = _cycle(build, runtime)
    await provider.wait_entered("a-0")

    executor.resume_gate = asyncio.Event()
    resume = asyncio.create_task(runtime.engine.resume(b.id))
    build.cycles.append(resume)
    await asyncio.wait_for(executor.resuming.wait(), GUARD_SECONDS)
    await provider.wait_entered("b-1")
    assert not resume.done() and not cycle.done()

    executor.resume_gate.set()
    assert await asyncio.wait_for(resume, GUARD_SECONDS) == ()
    provider.open()
    await asyncio.wait_for(cycle, GUARD_SECONDS)
    assert provider.entered == ["a-0", "b-1"]
    assert await runtime.artifact_payloads(b.id) == {"b-0", "b-1"}


@pytest.mark.asyncio
async def test_resume_all_wakes_the_running_cycle_for_every_resumed_transfer(build):
    """Sections 8.2, 18.2. B and C are paused before the cycle. Resume All
    makes both admissible; the running cycle admits them through ordinary fair
    bounded admission -- one free slot, one unit -- with no cycle restart."""
    runtime = await build(concurrency=2)
    provider = runtime.provider
    provider.hold("a-0", "b-0", "c-0")
    a = await runtime.submit("a", 1)
    b = await runtime.submit("b", 1)
    c = await runtime.submit("c", 1)
    await runtime.engine.pause(b.id)
    await runtime.engine.pause(c.id)
    cycle = _cycle(build, runtime)
    await provider.wait_entered("a-0")

    results = await runtime.engine.resume_all()
    assert results == {a.id: (), b.id: (), c.id: ()}
    assert not (await runtime.repository.get(b.id)).paused and not (await runtime.repository.get(c.id)).paused
    await provider.wait_entered("b-0")
    assert provider.entered == ["a-0", "b-0"]

    provider.open("b-0")
    await provider.wait_entered("c-0")
    assert not cycle.done(), "Resume All was served by the running cycle"
    assert "a-0" not in provider.finished and provider.cancelled == []

    provider.open()
    await asyncio.wait_for(cycle, GUARD_SECONDS)
    assert provider.entered == ["a-0", "b-0", "c-0"]
    assert await runtime.artifact_payloads(b.id) == {"b-0"}
    assert await runtime.artifact_payloads(c.id) == {"c-0"}


def test_operator_triggers_signal_the_one_scheduler_wake_and_nothing_else():
    """Sections 2.3, 21. Retry / Resume / Resume All only tell the existing
    scheduler owner that work may be runnable: one wake call each (Resume All
    once per batch, outside its per-transfer loop), and no scheduling policy."""
    for command in (TransferEngine.retry, TransferEngine.resume, TransferEngine.resume_all):
        source = inspect.getsource(command)
        assert source.count("self._resolution_opportunity(") == 1, command.__name__
        for policy in ("_ResolutionCycle", "_resolution_cycle", "_admit_resolution_unit", "_resolution_work",
                       "_fair_resolution_choice", "create_task", "asyncio.Event"):
            assert policy not in source, (command.__name__, policy)
    batch = inspect.getsource(TransferEngine.resume_all)
    wake = next(line for line in batch.splitlines() if "self._resolution_opportunity(" in line)
    loop = next(line for line in batch.splitlines() if line.lstrip().startswith("for transfer in"))
    assert len(wake) - len(wake.lstrip()) <= len(loop) - len(loop.lstrip())


async def _challenged(runtime: Runtime, prefix: str, *, password: str = "accepted", members: int = 1):
    """Transfer whose root ``<prefix>-0`` demanded provider input in an EARLIER
    cycle; the operator's submission is waiting for the next continuation,
    which -- when accepted -- fans out into ``members`` child requests."""
    provider = runtime.provider
    root = f"{prefix}-0"
    provider.auth.add(root)
    provider.parcels[root] = _members(root, members)
    transfer = await runtime.submit(prefix, 1)
    await runtime.engine.resolve_pending()
    challenge = await runtime.engine.challenges.current(transfer.id)
    assert challenge is not None and provider.entered == [root]
    await runtime.engine.submit_input(transfer.id, challenge.id, "username_password",
                                      {"username": "operator", "password": password})
    provider.entered.clear()
    return transfer, challenge


@pytest.mark.asyncio
async def test_provider_input_continuation_children_are_admitted_by_the_same_cycle(build):
    """A provider-input continuation is admitted by a running cycle, satisfies
    the challenge and fans out a durable child while ``x-0`` keeps that cycle
    alive with a slot idle. The continuation is applied exactly once, the root
    is never resolved again, and the child is this cycle's work."""
    runtime = await build(concurrency=2)
    provider = runtime.provider
    a, challenge = await _challenged(runtime, "a")
    child = _member("a-0", 0)
    provider.hold("x-0", child)
    await runtime.submit("x", 1)
    cycle = _cycle(build, runtime)

    await provider.wait_entered("x-0", "input:a-0")
    await _fan_out_is_durable(runtime, a.id, "a-0")
    assert await runtime.engine.challenges.current(a.id) is None
    await provider.wait_entered(child)
    assert not cycle.done(), "the continuation's child was admitted by the same cycle"
    assert "x-0" not in provider.finished and provider.cancelled == []

    provider.open()
    await asyncio.wait_for(cycle, GUARD_SECONDS)
    assert sorted(provider.entered) == sorted(["x-0", "input:a-0", child])
    assert await runtime.artifact_payloads(a.id) == {child}
    assert await runtime.engine.challenges.current(a.id) is None


@pytest.mark.asyncio
async def test_provider_input_challenge_that_persists_still_blocks_the_transfer(build):
    """The continuation is rejected and the provider challenges again. Current
    challenge truth stays authoritative: A's other request is pending and
    ready, yet it makes no provider contact, and neither the old nor the
    replacement challenge is continued a second time."""
    runtime = await build(concurrency=2)
    provider = runtime.provider
    provider.auth.add("a-0")
    a = await runtime.submit("a", 2)
    provider.failing = {"a-1"}
    await runtime.engine.resolve_pending()
    first = await runtime.engine.challenges.current(a.id)
    assert first is not None and sorted(provider.entered) == ["a-0", "a-1"]
    await runtime.engine.submit_input(a.id, first.id, "username_password",
                                      {"username": "operator", "password": "rejected"})
    provider.failing = set()
    provider.entered.clear()
    runtime.now[0] += 60
    assert (await runtime.request_states(a.id))["a-1"] == "pending"

    provider.hold("x-0")
    await runtime.submit("x", 1)
    cycle = _cycle(build, runtime)
    await provider.wait_entered("x-0", "input:a-0")

    async def replaced():
        current = await runtime.engine.challenges.current(a.id)
        return current is not None and current.id != first.id
    await _until(replaced)
    # ``y-0`` is submitted afterwards: once it has been admitted the scheduler
    # has reassessed A under the replacement challenge as well.
    await runtime.submit("y", 1)
    await provider.wait_entered("y-0")
    assert not cycle.done()
    assert sorted(provider.entered) == ["input:a-0", "x-0", "y-0"]

    provider.open()
    await asyncio.wait_for(cycle, GUARD_SECONDS)
    assert sorted(provider.entered) == ["input:a-0", "x-0", "y-0"]
    assert (await runtime.request_states(a.id))["a-1"] == "pending"
    assert await runtime.artifact_payloads(a.id) == set()


@pytest.mark.asyncio
async def test_provider_input_children_take_no_fast_lane(build):
    """A child a continuation created is ordinary work of its transfer: the
    continuation used A's turn of the bootstrap round, so the child waits for
    pathless B and for the one enrichment turn owed to productive P."""
    runtime = await build(concurrency=1)
    provider = runtime.provider
    productive = await runtime.submit("p", 3)
    provider.failing = {"p-1", "p-2"}
    await runtime.engine.resolve_pending()
    assert await runtime.artifact_payloads(productive.id) == {"p-0"}
    provider.entered.clear()

    a, _challenge = await _challenged(runtime, "a", members=2)
    first, second = _member("a-0", 0), _member("a-0", 1)
    await runtime.submit("b", 2)
    provider.failing = {"b-0", "b-1"}
    provider.hold("p-1", "p-2", "input:a-0", first, second, "b-0", "b-1")
    runtime.now[0] += 60

    await _drive(runtime, _cycle(build, runtime), ["input:a-0", "b-0", "p-1", first, "b-1", "p-2", second])
    assert await runtime.artifact_payloads(a.id) == {first, second}


@pytest.mark.asyncio
async def test_provider_input_children_join_bounded_admission(build):
    """A continuation that fans out into 30 children parks nothing: they join
    the same bounded admission, never one coroutine per child."""
    runtime = await build(concurrency=3)
    provider = runtime.provider
    a, _challenge = await _challenged(runtime, "a", members=30)
    children = [_member("a-0", index) for index in range(30)]
    provider.hold(*children)
    baseline = len(asyncio.all_tasks())
    cycle = _cycle(build, runtime)

    await provider.wait_active(3)
    assert provider.active == 3 and len(provider.entered) == 1 + 3
    states = await runtime.request_states(a.id)
    assert sum(states[child] == "resolving" for child in children) == 3
    assert sum(states[child] == "pending" for child in children) == 27
    assert len(asyncio.all_tasks()) - baseline <= 1 + 3

    provider.open()
    await asyncio.wait_for(cycle, 60)
    assert provider.max_active == 3 and provider.cancelled == []
    assert sorted(provider.entered) == sorted(["input:a-0", *children])
    assert await runtime.artifact_payloads(a.id) == set(children)
