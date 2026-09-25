"""The ONE neutral Executor Work projection and action surface.

Executor Work exists to answer a single operational question: what is an
executor actually doing with the work DebridPulse gave it, and what may the
operator legally do about it right now. It is a diagnostic surface for work
that is not converging -- Downloads remains the authoritative transfer record.

One canonical flow, for every executor there is or will be:

    durable DP execution attempts
        -> grouped by the registered executor that holds them
        -> executor.observe_many(handles), through the engine's one batched
           observation call
        -> neutral ExecutionObservation
        -> the neutral rows below
        -> one frontend renderer

There is deliberately no aria2 surface and no SABnzbd surface here, and no
per-executor branch anywhere in this module. Nothing native crosses this
boundary: no GID, no NZO id, no native status string, no native action URL, no
correlation and no secret. A browser that learned any of them would be
normalizing an executor's schema, which is exactly what this owner exists to
make unnecessary.

OWNERSHIP. Only work DebridPulse can establish as its OWN durable execution
work appears. The rows start from the durable execution attempts of live
transfers and every one of them is re-checked against the canonical
authorization fence before it is shown or acted on, so an arbitrary daemon job
-- something the operator started by hand, something left over from another
application -- can never appear and can never be controlled from here. There is
no "show all jobs" and no way to ask for one.

ACTIONS. The public identity is the DURABLE DP attempt id; current ownership is
resolved server-side, the observation is re-taken, and the action is verified
legal against that fresh observation before anything is dispatched. Dispatch
itself is always the existing canonical command -- DebridPulse pause, resume
and artifact cancellation -- never a native remove or delete. A stale or
not-owned attempt is refused.
"""
from fastapi import APIRouter, Depends, HTTPException

from application.dependencies import get_application
from application.service import ApplicationService
from transfers.models import ExecutionControl, ExecutionState

router = APIRouter()


# The generic operator actions. Each one names an existing canonical DebridPulse
# command; none of them names an executor or a native operation.
PAUSE, RESUME, CANCEL = "pause", "resume", "cancel"
ACTIONS = frozenset({PAUSE, RESUME, CANCEL})

# Neutral execution states the operator-facing filters group. The mapping is
# from ``ExecutionState`` only -- never from a native status string -- so an
# executor that reports a state DebridPulse already understands is filterable
# the day it is registered.
FILTER_GROUPS = {
    ExecutionState.QUEUED: "waiting",
    ExecutionState.RUNNING: "active",
    ExecutionState.PAUSED: "paused",
    ExecutionState.SUCCEEDED: "stopped",
    ExecutionState.FAILED: "stopped",
    ExecutionState.CANCELLED: "stopped",
    ExecutionState.ABSENT: "stopped",
    ExecutionState.UNKNOWN: "waiting",
}


async def _owned_attempts(application: ApplicationService):
    """Every durable execution attempt this application still owns.

    ``live_executions`` is the durable set; the canonical authorization fence
    is what makes each one OURS -- the attempt exists, is authorized, names the
    executor its handle names, and its stored handle is byte-identical to the
    one being presented.
    """
    owned = []
    for attempt in await application.repository.live_executions():
        if await application.repository.authorize_execution(attempt.handle, "observe"):
            owned.append(attempt)
    return owned


async def _display_names(application: ApplicationService, attempts) -> dict[int, str]:
    """The DebridPulse-owned name of each attempt's artifact.

    Read once per transfer, from canonical state. Never a native job name.
    """
    names: dict[int, str] = {}
    for transfer_id in {attempt.transfer_id for attempt in attempts}:
        for artifact in await application.repository.artifacts(transfer_id):
            names[artifact.id] = str(artifact.name or "")
    return names


def _controls(observation, *, cancellable: bool) -> list[str]:
    """The controls that are legal for this execution RIGHT NOW.

    Pause and Resume are the executor's own neutral answer about its current
    state (``ExecutionObservation.controls``), never an inference from a status
    name. Cancel is offered only where DebridPulse owns a canonical legal
    cancellation path for this exact attempt, which the caller has already
    asked the authorization fence.
    """
    controls = []
    if ExecutionControl.PAUSE in observation.controls:
        controls.append(PAUSE)
    if ExecutionControl.RESUME in observation.controls:
        controls.append(RESUME)
    if cancellable and not observation.stopped:
        controls.append(CANCEL)
    return controls


def _row(attempt, observation, executor, name: str, *, cancellable: bool) -> dict:
    """One neutral row. Everything in it is a DebridPulse fact."""
    progress = observation.progress
    total = int(progress.total_bytes or 0)
    completed = max(0, int(progress.completed_bytes or 0))
    # Size is not always knowable, and an unknown total is NOT zero: without one
    # there is no remaining figure to state, and inventing one would be a lie
    # about how much work is left.
    remaining = max(0, total - completed) if total > 0 else None
    # An executor that measures throughput only for ITSELF publishes no
    # per-execution rate, so this row simply has none -- rather than core
    # splitting one aggregate figure across jobs or, worse, reporting it both
    # here and as the aggregate and counting the same bytes twice.
    aggregate_only = bool(getattr(executor.capabilities, "aggregate_throughput", False))
    return {
        # The durable DebridPulse identity this row is addressed by.
        "attempt_id": attempt.handle.attempt_id,
        "transfer_id": attempt.transfer_id,
        "artifact_id": attempt.artifact_id,
        "name": name,
        "executor_id": executor.descriptor.id,
        "executor_name": executor.descriptor.name,
        "state": str(observation.state),
        "filter_group": FILTER_GROUPS.get(observation.state, "waiting"),
        "progress": round(progress.percentage, 2),
        "completed_bytes": completed,
        "total_bytes": total if total > 0 else None,
        "remaining_bytes": remaining,
        "bytes_per_second": None if aggregate_only else max(0, int(progress.bytes_per_second or 0)),
        "speed_measured_per_execution": not aggregate_only,
        "error": observation.error.as_dict() if observation.error else None,
        "controls": _controls(observation, cancellable=cancellable),
    }


def _unobservable_row(attempt, name: str) -> dict:
    """A DebridPulse-owned attempt whose executor is not currently registered.

    It is still our work, so it is still reported -- as unknown, with no
    controls. Hiding it would claim the work does not exist, and offering
    controls would claim a path to it that does not currently exist either.
    """
    return {
        "attempt_id": attempt.handle.attempt_id,
        "transfer_id": attempt.transfer_id,
        "artifact_id": attempt.artifact_id,
        "name": name,
        "executor_id": attempt.handle.executor_id,
        "executor_name": attempt.handle.executor_id,
        "state": str(ExecutionState.UNKNOWN),
        "filter_group": FILTER_GROUPS[ExecutionState.UNKNOWN],
        "progress": 0.0,
        "completed_bytes": 0,
        "total_bytes": None,
        "remaining_bytes": None,
        "bytes_per_second": None,
        "speed_measured_per_execution": False,
        "error": None,
        "controls": [],
    }


@router.get("/executor-work")
async def list_executor_work(application: ApplicationService = Depends(get_application)):
    """Every execution DebridPulse currently owns, as neutral rows."""
    engine = application.engine
    attempts = await _owned_attempts(application)
    names = await _display_names(application, attempts)

    grouped: dict[str, list] = {}
    for attempt in attempts:
        grouped.setdefault(attempt.handle.executor_id, []).append(attempt)

    rows: list[dict] = []
    for executor_id, owned in grouped.items():
        executor = engine.registry.executor_for_handle(owned[0].handle)
        if executor is None:
            rows.extend(_unobservable_row(attempt, names.get(attempt.artifact_id, "")) for attempt in owned)
            continue
        # One batch per executor. A batch that fails answers UNKNOWN for its own
        # handles and nothing else, so one executor being unreachable can never
        # make another executor's work look absent.
        snapshot = await engine.observe_existing(executor, tuple(item.handle for item in owned))
        for attempt, observation in zip(owned, snapshot.observations):
            cancellable = await application.repository.authorize_execution(attempt.handle, "cancel")
            rows.append(_row(attempt, observation, executor,
                             names.get(attempt.artifact_id, ""), cancellable=cancellable))

    # Aggregate throughput has one owner: the core runtime telemetry meter,
    # which already counts each executor exactly once (an executor that measures
    # only itself contributes that one figure; any other contributes the sum of
    # its own executions). Nothing is re-derived from the rows above, so the
    # same bytes can never be counted twice.
    speed = int(engine.throughput.current())
    # A total is stated only when EVERY row knows its own. A partial sum would
    # read as complete truth while being smaller than reality.
    known = [row["remaining_bytes"] for row in rows]
    remaining = sum(known) if rows and all(value is not None for value in known) else None
    return {
        "ok": True,
        "items": rows,
        "summary": {
            "total": len(rows),
            "download_speed": speed,
            "remaining_bytes": remaining,
            "counts": {group: sum(1 for row in rows if row["filter_group"] == group)
                       for group in ("active", "waiting", "paused", "stopped")},
        },
    }


@router.post("/executor-work/{attempt_id}/{action}")
async def control_executor_work(attempt_id: str, action: str,
                                application: ApplicationService = Depends(get_application)):
    """Perform one generic action on one durable DebridPulse attempt.

    The attempt id is resolved to CURRENT ownership here, the observation is
    re-taken here, and legality is decided here -- from the same neutral facts
    the projection reports. Only then is the existing canonical command
    dispatched. Nothing native is called and nothing is assumed from what the
    browser last saw.
    """
    if action not in ACTIONS:
        raise HTTPException(400, "Unsupported executor action")

    attempt = next((item for item in await _owned_attempts(application)
                    if item.handle.attempt_id == attempt_id), None)
    if attempt is None:
        # Either it was never ours or it is no longer current. Both are the same
        # answer: this request is about work this application does not own now.
        raise HTTPException(409, "That execution is no longer owned by this application")

    executor = application.engine.registry.executor_for_handle(attempt.handle)
    if executor is None:
        raise HTTPException(503, "That execution's integration is not available")

    snapshot = await application.engine.observe_existing(executor, (attempt.handle,))
    observation = snapshot.observations[0]
    cancellable = await application.repository.authorize_execution(attempt.handle, "cancel")
    if action not in _controls(observation, cancellable=cancellable):
        raise HTTPException(409, "That action is not currently available for this execution")

    if action == PAUSE:
        return {"ok": True, "attempt_id": attempt_id, "action": action,
                **await application.pause(attempt.transfer_id)}
    if action == RESUME:
        return {"ok": True, "attempt_id": attempt_id, "action": action,
                **await application.resume(attempt.transfer_id)}
    # Termination goes through the canonical artifact cancellation command, which
    # owns the whole act: the native writer is cancelled, OBSERVED stop truth is
    # required before the attempt is released, the cancellation outcome is
    # recorded against that attempt and the parent is re-aggregated.
    return {"ok": True, "attempt_id": attempt_id, "action": action,
            **await application.cancel_artifact(attempt.transfer_id, attempt.artifact_id)}
