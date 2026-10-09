"""The transfer core's event-journal vocabulary.

Each function builds the ONE journal event for one canonical occurrence; the
owner that commits the occurrence records it (``db.event_journal.record``)
inside that same transaction. Nothing here reads state or decides anything:
every fact comes from the owner that established it. Messages are short and
neutral; integration ids are context, never part of the type.
"""
from __future__ import annotations

from db.event_journal import JournalEvent
from transfers.models import TransferState

_STATE_LABELS = {
    TransferState.ACCEPTED: ("accepted", "Transfer accepted"),
    TransferState.RESOLVING: ("resolving", "Transfer resolving"),
    TransferState.INPUT_REQUIRED: ("input_required", "Transfer waiting for input"),
    TransferState.READY: ("ready", "Transfer ready"),
    TransferState.QUEUED: ("queued", "Transfer queued"),
    TransferState.TRANSFERRING: ("transferring", "Transfer downloading"),
    TransferState.PAUSED: ("paused", "Transfer paused"),
    TransferState.VERIFYING: ("verifying", "Transfer verifying"),
    TransferState.POST_PROCESSING: ("post_processing", "Transfer post-processing"),
    TransferState.COMPLETED: ("completed", "Transfer completed"),
    TransferState.CONSOLIDATED: ("consolidated", "Transfer consolidated"),
    TransferState.FAILED: ("failed", "Transfer failed"),
    TransferState.CANCELLED: ("cancelled", "Transfer cancelled"),
    TransferState.DELETED: ("deleted", "Transfer deleted"),
}
_STATE_OUTCOMES = {
    TransferState.COMPLETED: "succeeded", TransferState.FAILED: "failed", TransferState.CANCELLED: "cancelled",
}


def _with_error(message: str, error) -> str:
    return f"{message}: {error.message}" if error is not None else message


def _transfer(transfer_id: int, event_type: str, severity: str, message: str, **fields) -> JournalEvent:
    return JournalEvent("transfer", event_type, severity, message, "transfer", subject_id=transfer_id,
                        transfer_id=transfer_id, provenance=f"torrents:{transfer_id}", **fields)


# ── transfer lifecycle ──────────────────────────────────────────────────────

def accepted(transfer_id: int) -> JournalEvent:
    return _transfer(transfer_id, "transfer.accepted", "info", "Transfer accepted",
                     occurrence_key=f"transfer:{transfer_id}:accepted")


def lifecycle(transfer_id: int, previous: str, target, error=None) -> JournalEvent:
    """A committed transfer state (or failure classification) change."""
    target = TransferState(target)
    label, message = _STATE_LABELS[target]
    severity = "error" if target == TransferState.FAILED else "warning" if error is not None else "info"
    try:
        before = _STATE_LABELS[TransferState(previous)][0].replace("_", " ")
    except ValueError:
        before = None
    return _transfer(transfer_id, f"transfer.{label}", severity, _with_error(message, error),
                     outcome=_STATE_OUTCOMES.get(target), error=error,
                     detail=f"Previously {before}" if before and previous != target else None)


def deleted(transfer_id: int, *, remote: bool) -> JournalEvent:
    return _transfer(transfer_id, "transfer.deleted", "info", "Transfer deleted",
                     detail="Provider resources are removed too" if remote else None)


# ── routing ─────────────────────────────────────────────────────────────────

_ROUTE_ENDS = {
    "resolved": ("routing.route_resolved", "info", "Route resolved", None),
    "failed": ("routing.route_failed", "warning", "Route failed", "failed"),
    "declined": ("routing.route_declined", "info", "Provider declined the route", "declined"),
    "exhausted": ("routing.route_exhausted", "warning", "Route exhausted on this provider", "exhausted"),
}


def _route(event_type, severity, message, *, transfer_id, request_id, attempt_id, provider_id, **fields):
    return JournalEvent("routing", event_type, severity, message, "route_attempt", subject_id=attempt_id,
                        transfer_id=transfer_id, integration_id=provider_id,
                        provenance=f"route_attempt_provenance:{attempt_id}", **fields)


def route_started(*, transfer_id, request_id, attempt_id, provider_id, operation, transition_kind,
                  transition_reason, previous_provider_id) -> JournalEvent:
    if operation == "refresh":
        event_type, message = "routing.candidate_refresh_started", "Candidate refresh started"
    elif transition_kind == "provider_change":
        event_type, message = "routing.provider_changed", "Route moved to another provider"
    elif transition_kind == "resolution_retry":
        event_type, message = "routing.route_retried", "Route retried"
    else:
        event_type, message = "routing.route_started", "Route started"
    detail = f"Provider {provider_id}"
    if previous_provider_id:
        detail += f"; previously {previous_provider_id}"
    if transition_reason:
        detail += f" ({str(transition_reason).replace('_', ' ')})"
    return _route(event_type, "info", message, transfer_id=transfer_id, request_id=request_id,
                  attempt_id=attempt_id, provider_id=provider_id, detail=detail,
                  occurrence_key=f"route:{attempt_id}:started")


def route_ended(outcome: str, *, transfer_id, request_id, attempt_id, provider_id, error=None) -> JournalEvent:
    event_type, severity, message, result = _ROUTE_ENDS[outcome]
    return _route(event_type, severity, _with_error(message, error), transfer_id=transfer_id,
                  request_id=request_id, attempt_id=attempt_id, provider_id=provider_id, outcome=result,
                  error=error, occurrence_key=f"route:{attempt_id}:{outcome}")


def request_failed(*, transfer_id, request_id, error, retrying: bool) -> JournalEvent:
    """A source request's failure as its failure owner decided it: retried
    later (warning) or terminal for that request (error). Never the
    transfer's own outcome, which its lifecycle records."""
    return JournalEvent(
        "routing", "routing.request_failed", "warning" if retrying else "error",
        _with_error("Source request failed" + (", retry scheduled" if retrying else ""), error),
        "request", subject_id=request_id, transfer_id=transfer_id, outcome="retrying" if retrying else "failed",
        error=error, provenance=f"transfer_requests:{request_id}")


# ── provider resources ──────────────────────────────────────────────────────

_RESOURCE_STATES = {
    "preparing": ("info", "Provider resource preparing"),
    "available": ("info", "Provider resource available"),
    "unavailable": ("warning", "Provider resource unavailable"),
    "absent": ("info", "Provider resource no longer exists"),
    "expired": ("warning", "Provider resource expired"),
    "unknown": ("warning", "Provider resource state unknown"),
}


def _resource(event_type, severity, message, *, transfer_id, binding_id, provider_id, **fields):
    return JournalEvent("resource", event_type, severity, message, "provider_resource", subject_id=binding_id,
                        transfer_id=transfer_id, integration_id=provider_id,
                        provenance=f"provider_resources:{binding_id}", **fields)


def resource_state(*, transfer_id, binding_id, provider_id, previous, state) -> JournalEvent:
    state = str(state)
    severity, message = _RESOURCE_STATES.get(state, ("info", "Provider resource changed"))
    if previous is None:
        return _resource("resource.bound", "info", "Provider resource bound", transfer_id=transfer_id,
                         binding_id=binding_id, provider_id=provider_id, detail=f"Initial state {state}")
    return _resource(f"resource.{state}" if state in _RESOURCE_STATES else "resource.changed", severity, message,
                     transfer_id=transfer_id, binding_id=binding_id, provider_id=provider_id,
                     detail=f"Previously {previous}")


def resource_cleanup_completed(*, transfer_id, binding_id, provider_id, token, absent) -> JournalEvent:
    return _resource("resource.cleanup_completed", "info",
                     "Provider resource removed" if absent else "Provider resource cleanup finished",
                     transfer_id=transfer_id, binding_id=binding_id, provider_id=provider_id, outcome="succeeded",
                     occurrence_key=f"resource:{binding_id}:cleanup:{token}")


def resource_cleanup_abandoned(*, transfer_id, binding_id, provider_id, error, token) -> JournalEvent:
    return _resource("resource.cleanup_abandoned", "error",
                     _with_error("Provider resource cleanup abandoned", error), transfer_id=transfer_id,
                     binding_id=binding_id, provider_id=provider_id, outcome="abandoned", error=error,
                     occurrence_key=f"resource:{binding_id}:cleanup:{token}")


def standby(event: str, *, transfer_id, standby_id, provider_id, error=None) -> JournalEvent:
    severity, message = {
        "bound": ("info", "Standby provider resource prepared"),
        "failed": ("warning", "Standby provider resource could not be prepared"),
        "promoted": ("info", "Standby provider resource promoted"),
    }[event]
    return JournalEvent("resource", f"resource.standby_{event}", severity, _with_error(message, error),
                        "standby_resource", subject_id=standby_id, transfer_id=transfer_id,
                        integration_id=provider_id, error=error, provenance=f"standby_resources:{standby_id}",
                        occurrence_key=f"standby:{standby_id}:{event}" if event == "promoted" else None)


# ── file selection ──────────────────────────────────────────────────────────

def selection(event: str, *, transfer_id, selection_id, provider_id=None, detail=None) -> JournalEvent:
    severity, message = {
        "offered": ("info", "File selection available"),
        "confirmed": ("info", "File selection confirmed"),
        "dismissed": ("info", "File selection closed; all files selected"),
        "decided": ("info", "File selection decided automatically"),
        "held": ("warning", "File selection held: continuity could not be proven"),
    }[event]
    return JournalEvent("selection", f"selection.{event}", severity, message, "file_selection",
                        subject_id=selection_id, transfer_id=transfer_id, integration_id=provider_id, detail=detail,
                        provenance=f"transfer_file_selections:{selection_id}")


# ── consolidation ───────────────────────────────────────────────────────────

def consolidation(event: str, *, transfer_id, related_transfer_id=None, detail=None) -> JournalEvent:
    message = {
        "consolidated": "Transfer consolidated into canonical artifacts",
        "reopened": "Consolidated transfer reopened",
        "ownership_converged": "Collection member ownership converged",
    }[event]
    return JournalEvent("consolidation", f"consolidation.{event}", "info", message, "transfer",
                        subject_id=transfer_id, transfer_id=transfer_id, related_transfer_id=related_transfer_id,
                        detail=detail, provenance=f"torrents:{transfer_id}")


# ── execution ───────────────────────────────────────────────────────────────

def _artifact(event_type, severity, message, *, transfer_id, artifact_id, **fields):
    return JournalEvent("execution", event_type, severity, message, "artifact", subject_id=artifact_id,
                        transfer_id=transfer_id, provenance=f"download_files:{artifact_id}", **fields)


def execution_started(*, transfer_id, artifact_id, attempt_id, executor_id, provider_id) -> JournalEvent:
    return JournalEvent("execution", "execution.started", "info", "Download started", "execution_attempt",
                        subject_id=attempt_id, transfer_id=transfer_id, integration_id=executor_id,
                        detail=f"Artifact {artifact_id}" + (f"; source provider {provider_id}" if provider_id else ""),
                        provenance=f"execution_attempts:{attempt_id}",
                        occurrence_key=f"execution:{attempt_id}:started")


def execution_ended(state: str, *, transfer_id, artifact_id, attempt_id, executor_id, error=None) -> JournalEvent:
    event_type, severity, message, outcome = {
        "failed": ("execution.attempt_failed", "warning", "Download attempt failed", "failed"),
        "absent": ("execution.attempt_lost", "warning", "Download attempt no longer exists", "failed"),
        "cancelled": ("execution.attempt_cancelled", "info", "Download attempt stopped", "cancelled"),
    }[state]
    return JournalEvent("execution", event_type, severity, _with_error(message, error), "execution_attempt",
                        subject_id=attempt_id, transfer_id=transfer_id, integration_id=executor_id,
                        outcome=outcome, error=error, detail=None if error is not None else f"Artifact {artifact_id}",
                        provenance=f"execution_attempts:{attempt_id}",
                        occurrence_key=f"execution:{attempt_id}:{state}")


def artifact_completed(*, transfer_id, artifact_id, filename) -> JournalEvent:
    return _artifact("execution.file_completed", "info", "File completed", transfer_id=transfer_id,
                     artifact_id=artifact_id, outcome="succeeded", detail=filename)


def artifact_failed(*, transfer_id, artifact_id, filename, error) -> JournalEvent:
    return _artifact("execution.file_failed", "error", _with_error("File failed", error), transfer_id=transfer_id,
                     artifact_id=artifact_id, outcome="failed", error=error, detail=filename)


def size_refined(*, transfer_id, artifact_id, previous, total) -> JournalEvent:
    return _artifact("execution.size_refined", "warning",
                     "Final verified materialization refined execution-observed artifact size",
                     transfer_id=transfer_id, artifact_id=artifact_id, detail=f"{previous} -> {total} bytes")


# ── recovery and continuation ───────────────────────────────────────────────

_RECOVERY_ACTIONS = {
    "reconcile": ("info", "Recovery: reconciling"),
    "retry_same_candidate": ("info", "Recovery: retrying the same source"),
    "backoff": ("info", "Recovery: backing off before retrying"),
    "refresh_candidate": ("info", "Recovery: refreshing the source"),
    "try_alternate_candidate": ("info", "Recovery: switching to another source"),
    "wait_for_resource": ("info", "Recovery: waiting for the provider resource"),
    "wait_for_provider": ("info", "Recovery: waiting for the provider"),
    "wait_for_operator": ("warning", "Recovery: operator action required"),
    "fail_permanently": ("error", "Recovery: giving up"),
}


def recovery_decided(*, transfer_id, artifact_id, decision_id, action, reason, error=None) -> JournalEvent:
    severity, message = _RECOVERY_ACTIONS.get(str(action), ("info", "Recovery decision"))
    return JournalEvent("recovery", "recovery.decided", severity, message, "artifact", subject_id=artifact_id,
                        transfer_id=transfer_id, outcome=str(action), error=error,
                        detail=str(reason).replace("_", " ") if reason else None,
                        provenance=f"artifact_recovery_state:{artifact_id}",
                        occurrence_key=f"recovery:{decision_id}")


def recovery_quiesced(*, transfer_id, artifact_id, reason) -> JournalEvent:
    exhausted = reason == "recovery_exhausted"
    return JournalEvent("recovery", "recovery.exhausted" if exhausted else "recovery.waiting",
                        "error" if exhausted else "warning",
                        "Recovery exhausted" if exhausted else "Recovery waiting",
                        "artifact", subject_id=artifact_id, transfer_id=transfer_id,
                        detail=str(reason).replace("_", " ") if reason else None,
                        provenance=f"artifact_recovery_state:{artifact_id}")


_CONTINUATION = {
    "writer_parked": ("info", "Partial download parked for continuation"),
    "writer_retired": ("info", "Previous download writer retired"),
    "native_retarget": ("warning", "Download could not be moved in place; continuing another way"),
}


def continuation(event: str, *, transfer_id, artifact_id, detail=None) -> JournalEvent:
    severity, message = _CONTINUATION[event]
    return JournalEvent("recovery", f"recovery.{event}", severity, message, "artifact", subject_id=artifact_id,
                        transfer_id=transfer_id, detail=detail, provenance=f"artifact_material_state:{artifact_id}")


def material_invalidated(*, transfer_id, artifact_id, reason) -> JournalEvent:
    return JournalEvent("storage", "storage.material_discarded", "warning", "Partial download data discarded",
                        "artifact", subject_id=artifact_id, transfer_id=transfer_id,
                        detail=str(reason).replace("_", " ") if reason else None,
                        provenance=f"artifact_material_state:{artifact_id}")


# ── input and post-processing ───────────────────────────────────────────────

def input_requested(*, transfer_id, challenge_id, generation, integration_id, message) -> JournalEvent:
    return JournalEvent("input", "input.requested", "warning", message, "input_challenge",
                        subject_id=challenge_id, transfer_id=transfer_id, integration_id=integration_id,
                        provenance=f"transfer_input_challenges:{challenge_id}",
                        occurrence_key=f"input:{challenge_id}:{generation}:requested")


def input_accepted(*, transfer_id, challenge_id, generation, integration_id) -> JournalEvent:
    return JournalEvent("input", "input.accepted", "info", "Requested input accepted", "input_challenge",
                        subject_id=challenge_id, transfer_id=transfer_id, integration_id=integration_id,
                        provenance=f"transfer_input_challenges:{challenge_id}",
                        occurrence_key=f"input:{challenge_id}:{generation}:accepted")


def extraction(event: str, *, transfer_id, processor_id, outcome=None) -> JournalEvent:
    if event == "started":
        return JournalEvent("extraction", "extraction.started", "info", "Post-processing started", "postprocess",
                            subject_id=processor_id, transfer_id=transfer_id, integration_id=processor_id,
                            provenance=f"postprocess_attempts:{transfer_id}:{processor_id}")
    error = getattr(outcome, "error", None)
    kind = str(getattr(outcome, "kind", ""))
    if error is not None:
        event_type, severity, message, result = "extraction.failed", "error", _with_error("Post-processing failed", error), "failed"
    elif kind == "skipped":
        event_type, severity, message, result = "extraction.skipped", "info", "Post-processing skipped", "skipped"
    else:
        event_type, severity, message, result = "extraction.completed", "info", "Post-processing completed", "succeeded"
    return JournalEvent("extraction", event_type, severity, message, "postprocess", subject_id=processor_id,
                        transfer_id=transfer_id, integration_id=processor_id, outcome=result, error=error,
                        detail=getattr(outcome, "detail", None) or None,
                        provenance=f"postprocess_attempts:{transfer_id}:{processor_id}")
