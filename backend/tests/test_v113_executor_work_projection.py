"""DP 1.0.13 Executor Work: one neutral projection and action surface.

Two executors that share nothing -- a per-execution copier and an
aggregate-throughput acquisition service -- are rendered by ONE schema, from
ONE observation seam, and controlled through ONE set of generic actions that
dispatch the existing canonical DebridPulse commands. Nothing native reaches
the boundary and nothing the application does not own can be seen or touched.
"""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from api import executor_work
from executor_fakes import LedgerExecutor, LedgerProvider, ledger_capabilities
from fake_integrations import MemoryExecutor, ParcelProvider
from fastapi import HTTPException
from transfers.convergence_engine import TransferEngine
from transfers.models import (
    ExecutionControl, ExecutionObservation, ExecutionState, TransferProgress, TransferRequest,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry


class Commands:
    """The canonical application commands Executor Work is allowed to use.

    Each one records that it was called and does exactly what the real command
    does not need to do here: nothing. The point of the record is that the
    action surface reaches these, and only these -- never an executor.
    """

    def __init__(self, repository, engine):
        self.repository = repository
        self.engine = engine
        self.calls: list[tuple] = []

    async def pause(self, transfer_id):
        self.calls.append(("pause", transfer_id))
        return {"ok": True}

    async def resume(self, transfer_id):
        self.calls.append(("resume", transfer_id))
        return {"ok": True}

    async def cancel_artifact(self, transfer_id, artifact_id):
        self.calls.append(("cancel_artifact", transfer_id, artifact_id))
        return {"ok": True}


@pytest_asyncio.fixture
async def work(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    registry.register_provider(ParcelProvider())
    registry.register_provider(LedgerProvider())
    copier = MemoryExecutor(repository.authorize_execution)
    # The other executor measures throughput for ITSELF and publishes no
    # per-job rate -- the SABnzbd shape.
    service = LedgerExecutor(repository.authorize_execution,
                             capabilities=ledger_capabilities(aggregate_throughput=True))
    registry.register_executor(copier)
    registry.register_executor(service)
    now = [1000.0]
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                                                  max_active_executions=4),
                            clock=lambda: now[0])
    await engine.initialize()
    application = Commands(repository, engine)
    return SimpleNamespace(application=application, repository=repository, engine=engine,
                           registry=registry, copier=copier, service=service, now=now)


async def settle(engine, rounds=4):
    for _ in range(rounds):
        await engine.tick()
        await asyncio.sleep(0)


async def two_executions(work):
    await work.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),))
    # A collection acquisition of unknown size -- the ordinary Usenet shape.
    await work.engine.submit((TransferRequest("ledger", "tome:collection", name="tome"),))
    await settle(work.engine)
    return await executor_work.list_executor_work(work.application)


@pytest.mark.asyncio
async def test_two_unrelated_executors_are_rendered_by_one_neutral_schema(work):
    payload = await two_executions(work)
    rows = payload["items"]
    assert {row["executor_id"] for row in rows} == {"memory-copy", "ledger-copy"}
    # ONE schema: every row of every executor carries exactly the same fields.
    assert len({tuple(sorted(row)) for row in rows}) == 1
    for row in rows:
        assert row["executor_name"], "a row must say which executor holds it"
        assert row["name"], "the display name is DebridPulse's own artifact name"


@pytest.mark.asyncio
async def test_no_native_identity_or_action_crosses_the_boundary(work):
    payload = await two_executions(work)
    serialized = repr(payload)
    for native in ("gid", "nzo", "native", "correlation", "ticket", "copy_ticket",
                   "aria2", "sabnzbd", "apikey", "api_key", "job"):
        assert native not in serialized.lower(), native
    for row in payload["items"]:
        assert set(row["controls"]) <= executor_work.ACTIONS


@pytest.mark.asyncio
async def test_only_work_the_application_owns_is_ever_shown(work):
    payload = await two_executions(work)
    owned = {row["attempt_id"] for row in payload["items"]}
    attempts = {attempt.handle.attempt_id for attempt in await work.repository.live_executions()}
    assert owned == attempts and owned

    # A job the service holds that DebridPulse never created is not our work,
    # so it is not reported -- and there is no way to ask for it.
    before = len(payload["items"])
    work.service.jobs["stranger"] = SimpleNamespace(native_id="stranger")
    assert len((await executor_work.list_executor_work(work.application))["items"]) == before


@pytest.mark.asyncio
async def test_state_filters_are_mapped_from_the_neutral_execution_state(work):
    payload = await two_executions(work)
    groups = {row["state"]: row["filter_group"] for row in payload["items"]}
    assert groups, "no rows to classify"
    for state, group in groups.items():
        assert group == executor_work.FILTER_GROUPS[ExecutionState(state)]
    # Every neutral state the core model defines has a group; nothing falls
    # through to a native status name.
    assert set(executor_work.FILTER_GROUPS) == set(ExecutionState)


@pytest.mark.asyncio
async def test_per_job_speed_is_unavailable_for_an_aggregate_throughput_executor(work):
    payload = await two_executions(work)
    by_executor = {row["executor_id"]: row for row in payload["items"]}
    service_row = by_executor["ledger-copy"]
    assert service_row["bytes_per_second"] is None
    assert service_row["speed_measured_per_execution"] is False
    copier_row = by_executor["memory-copy"]
    assert isinstance(copier_row["bytes_per_second"], int)
    assert copier_row["speed_measured_per_execution"] is True


@pytest.mark.asyncio
async def test_aggregate_speed_comes_from_core_telemetry_and_is_never_double_counted(work):
    work.service.aggregate_throughput = 4096
    payload = await two_executions(work)
    assert payload["summary"]["download_speed"] == work.engine.throughput.current()
    # Not a re-derivation from the rows: the aggregate-throughput executor's
    # own figure is counted once, and its rows contribute nothing.
    per_row = sum(row["bytes_per_second"] or 0 for row in payload["items"])
    assert payload["summary"]["download_speed"] != per_row or per_row == 0


@pytest.mark.asyncio
async def test_unknown_size_never_fabricates_a_remaining_total(work):
    await two_executions(work)
    # An acquisition whose payload size nothing has established yet.
    for job in work.service.jobs.values():
        job.progress = TransferProgress(0, 0, 0)
    payload = await executor_work.list_executor_work(work.application)

    unknown = [row for row in payload["items"] if row["total_bytes"] is None]
    assert unknown, "this fixture must contain at least one execution of unknown size"
    for row in unknown:
        assert row["remaining_bytes"] is None
    # A partial sum is never presented as the whole truth: one unknown total
    # means there is no honest aggregate remaining figure at all.
    assert payload["summary"]["remaining_bytes"] is None
    # The rows whose size IS known still state it.
    known = [row for row in payload["items"] if row["total_bytes"] is not None]
    for row in known:
        assert row["remaining_bytes"] == row["total_bytes"] - row["completed_bytes"]


@pytest.mark.asyncio
async def test_controls_are_the_executors_own_neutral_answer(work):
    payload = await two_executions(work)
    for row in payload["items"]:
        attempt = next(item for item in await work.repository.live_executions()
                       if item.handle.attempt_id == row["attempt_id"])
        executor = work.registry.executor_for_handle(attempt.handle)
        observation = (await work.engine.observe_existing(executor, (attempt.handle,))).observations[0]
        expected = {executor_work.PAUSE} if ExecutionControl.PAUSE in observation.controls else set()
        if ExecutionControl.RESUME in observation.controls:
            expected.add(executor_work.RESUME)
        assert expected <= set(row["controls"])
        # Cancel is offered only while there is live native work to stop.
        assert (executor_work.CANCEL in row["controls"]) is not observation.stopped


@pytest.mark.asyncio
async def test_pause_and_resume_dispatch_the_canonical_application_commands(work):
    payload = await two_executions(work)
    row = next(row for row in payload["items"] if executor_work.PAUSE in row["controls"])
    await executor_work.control_executor_work(row["attempt_id"], "pause", work.application)
    assert ("pause", row["transfer_id"]) in work.application.calls
    # The executor itself was never asked to do anything by this surface.
    assert not any(call[0] == "pause" for call in work.copier.calls)


@pytest.mark.asyncio
async def test_termination_uses_the_canonical_artifact_cancellation_command(work):
    payload = await two_executions(work)
    row = next(row for row in payload["items"] if executor_work.CANCEL in row["controls"])
    await executor_work.control_executor_work(row["attempt_id"], "cancel", work.application)
    assert ("cancel_artifact", row["transfer_id"], row["artifact_id"]) in work.application.calls


@pytest.mark.asyncio
async def test_a_stale_or_unowned_attempt_is_refused(work):
    await two_executions(work)
    with pytest.raises(HTTPException) as refusal:
        await executor_work.control_executor_work("not-an-attempt", "cancel", work.application)
    assert refusal.value.status_code == 409


@pytest.mark.asyncio
async def test_an_action_that_is_not_currently_legal_is_refused(work):
    payload = await two_executions(work)
    row = next(row for row in payload["items"] if executor_work.RESUME not in row["controls"])
    with pytest.raises(HTTPException) as refusal:
        await executor_work.control_executor_work(row["attempt_id"], "resume", work.application)
    assert refusal.value.status_code == 409
    assert not work.application.calls


@pytest.mark.asyncio
async def test_an_unsupported_action_is_rejected_before_anything_is_resolved(work):
    await two_executions(work)
    with pytest.raises(HTTPException) as refusal:
        await executor_work.control_executor_work("anything", "remove", work.application)
    assert refusal.value.status_code == 400


@pytest.mark.asyncio
async def test_one_executor_failing_never_fabricates_absence_for_another(work):
    from transfers.errors import Category, Domain, NormalizedError, Stage

    work.service.observe_failure = NormalizedError(Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE,
                                                  Stage.RECONCILIATION)
    payload = await two_executions(work)
    by_executor = {row["executor_id"]: row for row in payload["items"]}
    assert set(by_executor) == {"memory-copy", "ledger-copy"}
    assert by_executor["ledger-copy"]["state"] == str(ExecutionState.UNKNOWN)
    # An unobservable execution declares no pause/resume of its own: those are
    # the executor's answer, and it did not give one. Cancellation remains
    # available because that path is DebridPulse's, not the executor's -- and
    # it still refuses to release the attempt without observed stop truth.
    assert executor_work.PAUSE not in by_executor["ledger-copy"]["controls"]
    assert executor_work.RESUME not in by_executor["ledger-copy"]["controls"]
    # The healthy executor is untouched by the other's failure.
    assert by_executor["memory-copy"]["state"] != str(ExecutionState.UNKNOWN)
    assert executor_work.PAUSE in by_executor["memory-copy"]["controls"]
