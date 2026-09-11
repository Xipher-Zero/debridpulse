"""DP 1.0.12 Section 17 measurement: batch link-admission DB/query boundedness.

``application/service.py submit_links()`` now calls ``self.submit((request,), ...)``
once per URL instead of one ``self.submit(requests, ...)`` call for all URLs. This
instruments the real ``db.database`` connection layer to measure, rather than
assert from reading the code, that:

  * the marginal SQL-statement cost per additional URL is constant (linear
    total cost, not N^2 / multiplicative);
  * ``self.submit()``'s own ``_publish()`` call (``repository.presentation()``
    plus two ``event_bus.publish()`` calls) is accounted for in that measured
    cost, not waved away as "only durable admission";
  * no resolution, materialization, or execution work happens synchronously
    inside the batch-submission request handler -- the provider and executor
    are never touched until the caller explicitly drives
    ``resolve_pending()``/``reconcile_executions()``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

import db.database as database
from test_application_runtime import runtime  # noqa: F401  (shared fixture, established repo convention)
from transfers.models import TransferRequest


@dataclass
class _QueryCounter:
    execute: int = 0
    executemany: int = 0
    fetchall: int = 0
    fetchone: int = 0
    execute_returning_id: int = 0
    get_db_acquisitions: int = 0
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


def _acquisitions():
    return int(database.db_runtime_metrics()["sqlite_acquires"])


@pytest.mark.asyncio
async def test_batch_link_admission_marginal_query_cost_is_constant(runtime, query_counter):
    """Submit growing batches (1, 2, 4, 8 URLs) and measure the exact
    SQL-statement delta per batch. If the corrected per-URL submit() loop
    caused multiplicative/N^2 work, the marginal cost per additional URL
    would grow with N; measurement below proves it does not."""
    _application, _provider, _executor, client = runtime
    batch_sizes = [1, 2, 4, 8]
    measurements = []
    cursor = 0

    for size in batch_sizes:
        links = [f"https://fake.example/query-bounded-{cursor + i}" for i in range(size)]
        cursor += size
        start_statements = query_counter.total_statements
        start_acquisitions = _acquisitions()

        response = await client.post("/api/links/add", json={"links": links})

        assert response.status_code == 200, response.text
        assert response.json()["accepted"] == size
        statements = query_counter.total_statements - start_statements
        acquisitions = _acquisitions() - start_acquisitions
        measurements.append((size, statements, acquisitions))

    # --- Report exact measured counts (surfaced in the pytest failure/log
    # output and cited verbatim in the DP 1.0.12 checkpoint) ---
    report = "\n".join(
        f"  N={size:>2}: {statements} SQL statements ({acquisitions} get_db() acquisitions), "
        f"{statements / size:.2f} statements/URL"
        for size, statements, acquisitions in measurements
    )
    print(f"\nBatch link admission query cost:\n{report}")

    # Marginal cost per additional URL, computed from consecutive batches.
    # A linear/bounded-per-item admission path yields an identical marginal
    # rate regardless of N; a multiplicative/N^2 path would show the
    # marginal rate growing with N.
    marginal_rates = []
    for (size_a, stmts_a, _), (size_b, stmts_b, _) in zip(measurements, measurements[1:]):
        marginal_rates.append((stmts_b - stmts_a) / (size_b - size_a))
    print(f"  marginal statements/URL between consecutive batches: {marginal_rates}")

    assert len(set(marginal_rates)) == 1, (
        f"Marginal per-URL statement cost is not constant across batch sizes -- "
        f"possible multiplicative/N^2 admission cost: {measurements}"
    )

    # Each individual submit() call (admit() + _publish(), including its
    # repository.presentation() query and its two event_bus.publish() SSE
    # fan-outs) is itself several bounded statements, not "one" -- confirm
    # that explicitly rather than assert a specific magic number, so this
    # doesn't silently pass if the shape changes materially.
    per_item = marginal_rates[0]
    assert per_item > 1, "expected multiple bounded statements per URL (admit + presentation), not a single one"
    assert per_item < 30, f"per-URL statement cost ({per_item}) looks unexpectedly large for bounded admission"

    # Section 17: admission must not synchronously resolve/materialize/
    # execute. The fake provider/executor must be untouched by pure
    # submission -- only the caller's own explicit resolve_pending()/
    # reconcile_executions() calls (never made in this test) may touch them.
    assert _provider.calls == []
    assert _executor.calls == []


@pytest.mark.asyncio
async def test_single_submit_call_cost_includes_publish_presentation_and_events(runtime, query_counter):
    """Explicitly attribute one ApplicationService.submit() call's statement
    cost between engine.submit() (repository.admit() + engine.submit()'s own
    extra repository.globally_paused()/repository.get() calls) and
    _publish() (repository.presentation() + two event_bus.publish() SSE
    fan-outs), so the checkpoint's wording is measured rather than assumed --
    the previous version of this test measured repository.admit() alone,
    which undercounts engine.submit()'s real cost (it also calls
    repository.globally_paused() and repository.get() -- see
    transfers/_engine_base.py submit()) and did not reconcile against the
    per-URL total measured in the batch test above; this version calls the
    exact same two steps application.submit() itself calls, in order, so the
    two pieces sum to that measured per-URL total exactly.

    ``api.routes`` calls ``bind_publisher(_sse_broadcast)`` at module import
    time (routes.py line 1539), so importing ``api.routes`` -- which the
    ``runtime`` fixture does via ``from api.routes import router`` -- binds
    the real SSE publisher even in this lightweight test harness.
    ``_sse_broadcast`` (routes.py ~1521-1537) is pure in-memory
    ``asyncio.Queue.put_nowait`` fan-out over connected SSE clients (none, in
    this test) -- zero DB I/O regardless of publisher wiring, confirmed by
    reading the function directly. So the measured _publish() cost below is
    genuinely all repository.presentation(), not SSE dispatch."""
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
    # Matches the per-URL total measured independently in
    # test_batch_link_admission_marginal_query_cost_is_constant (20
    # statements/URL) -- same two steps, called through the same
    # ApplicationService.submit() path, just attributed here.
    assert total == 20
