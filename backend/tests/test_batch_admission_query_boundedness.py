"""DP 1.0.12 corrective Section A7 measurement: batch link-admission DB/query
boundedness under the restored one-batch/one-transfer admission topology.

``application/service.py submit_links()`` restores the parent architecture:
ONE ``self.submit(requests, ...)`` call admits the entire N-URL batch as one
transfer with N sibling requests, instead of looping ``self.submit((request,), ...)``
once per URL (which produced N independent top-level transfers). This
instruments the real ``db.database`` connection layer, plus the exact
``TransferRepository.admit`` / ``TransferRepository.presentation`` seams
``ApplicationService.submit()`` calls, to measure -- rather than assert from
reading the code -- that:

  * exactly ONE ``repository.admit()`` call and ONE ``_publish()``/
    ``repository.presentation()`` cycle happen per Quick Add batch, regardless
    of how many URLs it contains (Section A7: "no repeated per-URL top-level
    submit()/presentation() cycle");
  * the marginal SQL-statement cost per additional URL *within* that one
    admission is bounded/constant (attributable to N request-row INSERTs
    inside the one ``admit()`` transaction), not the N-transfer-fan-out shape
    the reverted topology produced;
  * no resolution, materialization, or execution work happens synchronously
    inside the batch-submission request handler -- the fake provider/executor
    are untouched until the caller explicitly drives
    ``resolve_pending()``/``reconcile_executions()``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

import db.database as database
import transfers.repository as repository_module
from test_application_runtime import runtime  # noqa: F401  (shared fixture, established repo convention)


@dataclass
class _QueryCounter:
    execute: int = 0
    executemany: int = 0
    fetchall: int = 0
    fetchone: int = 0
    execute_returning_id: int = 0
    calls: list[str] = field(default_factory=list)

    @property
    def total_statements(self) -> int:
        # execute_returning_id and executemany each issue exactly one
        # statement of their own; counting them alongside execute/fetchall/
        # fetchone gives the true total SQL-statement count, not just a
        # per-method breakdown.
        return self.execute + self.executemany + self.fetchall + self.fetchone + self.execute_returning_id


@pytest.fixture
def query_counter(monkeypatch):
    counter = _QueryCounter()
    original_execute = database._DbConnection.execute
    original_executemany = database._DbConnection.executemany
    original_fetchall = database._DbConnection.fetchall
    original_fetchone = database._DbConnection.fetchone
    original_execute_returning_id = database._DbConnection.execute_returning_id

    async def counted_execute(self, sql, params=()):
        counter.execute += 1
        counter.calls.append(sql.strip().split()[0].upper())
        return await original_execute(self, sql, params)

    async def counted_executemany(self, sql, params_list):
        counter.executemany += 1
        counter.calls.append("EXECUTEMANY")
        return await original_executemany(self, sql, params_list)

    async def counted_fetchall(self, sql, params=()):
        counter.fetchall += 1
        counter.calls.append("SELECT*")
        return await original_fetchall(self, sql, params)

    async def counted_fetchone(self, sql, params=()):
        counter.fetchone += 1
        counter.calls.append("SELECT1")
        return await original_fetchone(self, sql, params)

    async def counted_execute_returning_id(self, sql, params=()):
        counter.execute_returning_id += 1
        counter.calls.append("INSERT-RID")
        return await original_execute_returning_id(self, sql, params)

    monkeypatch.setattr(database._DbConnection, "execute", counted_execute)
    monkeypatch.setattr(database._DbConnection, "executemany", counted_executemany)
    monkeypatch.setattr(database._DbConnection, "fetchall", counted_fetchall)
    monkeypatch.setattr(database._DbConnection, "fetchone", counted_fetchone)
    monkeypatch.setattr(database._DbConnection, "execute_returning_id", counted_execute_returning_id)
    return counter


@dataclass
class _CallCounter:
    admit: int = 0
    presentation: int = 0


@pytest.fixture
def call_counter(monkeypatch):
    counter = _CallCounter()
    original_admit = repository_module.TransferRepository.admit
    original_presentation = repository_module.TransferRepository.presentation

    async def counted_admit(self, *args, **kwargs):
        counter.admit += 1
        return await original_admit(self, *args, **kwargs)

    async def counted_presentation(self, *args, **kwargs):
        counter.presentation += 1
        return await original_presentation(self, *args, **kwargs)

    monkeypatch.setattr(repository_module.TransferRepository, "admit", counted_admit)
    monkeypatch.setattr(repository_module.TransferRepository, "presentation", counted_presentation)
    return counter


@pytest.mark.asyncio
async def test_batch_link_admission_uses_exactly_one_admit_and_publish_cycle(runtime, call_counter):
    """Section A7 / A2: one N-URL Quick Add performs one batched application
    admission -- exactly one ``repository.admit()`` call and exactly one
    ``_publish()``/``repository.presentation()`` cycle -- never N of either."""
    _application, provider, executor, client = runtime
    links = [f"https://fake.example/one-admit-{index}" for index in range(10)]

    response = await client.post("/api/links/add", json={"links": links})

    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 10
    assert call_counter.admit == 1, "expected exactly one repository.admit() call for the whole N-URL batch"
    assert call_counter.presentation == 1, "expected exactly one presentation()/publish cycle for the whole batch"

    # Section 8/B4/17: no synchronous provider/executor work during submission.
    assert provider.calls == []
    assert executor.calls == []


@pytest.mark.asyncio
async def test_batch_link_admission_marginal_query_cost_is_bounded_per_request_row(runtime, query_counter):
    """Submit growing single-batch sizes (1, 2, 4, 8, 16 URLs, each its own
    Quick Add call) and measure the exact SQL-statement delta per batch. The
    one-transfer/N-request-row admission model predicts total cost grows as
    a small constant per-batch overhead plus one bounded increment per
    additional request row (linear in N); a reintroduced N-transfer fan-out
    would instead multiply the *entire* per-submission cost (admit + publish)
    by N. The marginal rate below distinguishes the two shapes."""
    _application, provider, executor, client = runtime
    batch_sizes = [1, 2, 4, 8, 16]
    measurements = []
    cursor = 0

    for size in batch_sizes:
        links = [f"https://fake.example/query-bounded-{cursor + i}" for i in range(size)]
        cursor += size
        start_statements = query_counter.total_statements

        response = await client.post("/api/links/add", json={"links": links})

        assert response.status_code == 200, response.text
        assert response.json()["accepted"] == size
        statements = query_counter.total_statements - start_statements
        measurements.append((size, statements))

    report = "\n".join(
        f"  N={size:>2}: {statements} SQL statements, {statements / size:.2f} statements/URL"
        for size, statements in measurements
    )
    print(f"\nBatch link admission query cost (one transfer per batch):\n{report}")

    # Marginal cost per additional request row, computed from consecutive
    # batches. A bounded-per-row admission path (one INSERT per row inside
    # the single admit() transaction) yields an identical marginal rate
    # regardless of N; an N-transfer-fan-out path would instead show the
    # *total* cost scaling by whole per-submission multiples of N, not by a
    # small constant per extra row.
    marginal_rates = []
    for (size_a, stmts_a), (size_b, stmts_b) in zip(measurements, measurements[1:]):
        marginal_rates.append((stmts_b - stmts_a) / (size_b - size_a))
    print(f"  marginal statements/URL between consecutive batches: {marginal_rates}")

    assert len(set(marginal_rates)) == 1, (
        f"Marginal per-request-row statement cost is not constant across batch sizes -- "
        f"possible reintroduced N-transfer-fan-out admission cost: {measurements}"
    )
    per_row = marginal_rates[0]
    # One INSERT per request row inside the existing admit() loop is the
    # expected bounded marginal cost; anything at or above the whole
    # per-submission overhead measured for N=1 would indicate the batch is
    # still being split into independent top-level submissions.
    single_batch_overhead = measurements[0][1]
    assert 0 < per_row < single_batch_overhead, (
        f"expected a small bounded per-row marginal cost ({per_row}) well under the "
        f"whole one-batch overhead ({single_batch_overhead}), not a repeated per-URL submission cost"
    )

    # Section 8/B4/17: admission must not synchronously resolve/materialize/
    # execute regardless of batch size.
    assert provider.calls == []
    assert executor.calls == []


@pytest.mark.asyncio
async def test_single_submit_call_cost_includes_publish_presentation_and_events(runtime, query_counter):
    """Explicitly attribute one ApplicationService.submit() call's statement
    cost between engine.submit() (repository.admit() + engine.submit()'s own
    extra repository.globally_paused()/repository.get() calls) and
    _publish() (repository.presentation() + two event_bus.publish() SSE
    fan-outs), so the checkpoint's wording is measured rather than assumed.

    ``api.routes`` calls ``bind_publisher(_sse_broadcast)`` at module import
    time (routes.py line 1539), so importing ``api.routes`` -- which the
    ``runtime`` fixture does via ``from api.routes import router`` -- binds
    the real SSE publisher even in this lightweight test harness.
    ``_sse_broadcast`` (routes.py ~1521-1537) is pure in-memory
    ``asyncio.Queue.put_nowait`` fan-out over connected SSE clients (none, in
    this test) -- zero DB I/O regardless of publisher wiring, confirmed by
    reading the function directly. So the measured _publish() cost below is
    genuinely all repository.presentation(), not SSE dispatch."""
    from transfers.models import TransferRequest

    application, _provider, _executor, client = runtime

    engine_submit_before = query_counter.total_statements
    transfer = await application.engine.submit(
        (TransferRequest("http", "https://fake.example/attribution-probe"),),
        name="attribution-probe", deduplicate=False,
    )
    engine_submit_statements = query_counter.total_statements - engine_submit_before

    publish_before = query_counter.total_statements
    await application._publish(transfer.id)
    publish_statements = query_counter.total_statements - publish_before

    total = engine_submit_statements + publish_statements
    print(
        f"\nengine.submit() (repository.admit() + globally_paused() + get()): "
        f"{engine_submit_statements} statements; "
        f"ApplicationService._publish() (repository.presentation() + 2x event_bus.publish()): "
        f"{publish_statements} statements; total: {total}"
    )
    assert engine_submit_statements > 0
    assert publish_statements > 0  # _publish()/presentation() is real, measured cost, not zero.
