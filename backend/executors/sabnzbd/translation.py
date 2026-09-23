"""SAB-native state -> neutral execution facts. Nothing native crosses out.

Every mapping below is taken from the state set SABnzbd 5.1.3 actually
declares (``sabnzbd/constants.py`` class ``Status``), characterized at Gate 1.
"""
from __future__ import annotations

from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
from transfers.models import ExecutionActivity, ExecutionState, TransferProgress

EXECUTOR_ID = "sabnzbd"

# Queue-side states that mean network acquisition is happening or pending.
_ACQUIRING = {"downloading", "fetching", "grabbing"}
# Queue-side states that are live but not yet acquiring.
_WAITING = {"queued", "checking", "propagating", "idle"}
# Post-processing: local reconstruction / PAR2 work. Never network acquisition.
# (``extracting`` cannot occur for DP jobs -- they are submitted pp=repair-only.)
_POST_PROCESSING = {"quickcheck", "verifying", "repairing", "moving", "running", "extracting"}

_TERMINAL = {"completed": ExecutionState.SUCCEEDED, "failed": ExecutionState.FAILED}


def native_state(status: str, *, in_history: bool) -> ExecutionState:
    """The neutral lifecycle state for one SAB job status."""
    value = str(status or "").strip().lower().replace(" ", "")
    if value in _TERMINAL:
        return _TERMINAL[value]
    if value == "deleted":
        return ExecutionState.CANCELLED
    if value == "paused":
        return ExecutionState.PAUSED
    if value in _ACQUIRING or value in _POST_PROCESSING:
        return ExecutionState.RUNNING
    if value in _WAITING:
        return ExecutionState.QUEUED
    if in_history:
        # An unmapped terminal-side status is still terminal truth we cannot
        # classify; never guess success.
        return ExecutionState.UNKNOWN
    return ExecutionState.UNKNOWN


def native_activity(status: str) -> ExecutionActivity:
    """Activity facts, independent of lifecycle state."""
    value = str(status or "").strip().lower().replace(" ", "")
    acquiring = value in _ACQUIRING
    post = value in _POST_PROCESSING
    return ExecutionActivity(
        network_active=acquiring,
        # A live native job may resume acquisition without another core
        # admission, so it holds its reservation while queued or acquiring.
        bandwidth_reservation_required=acquiring or value in _WAITING,
        progress_expected=acquiring or post,
    )


def _megabytes(value) -> float:
    try:
        return max(0.0, float(str(value or "0").strip() or 0.0))
    except (TypeError, ValueError):
        return 0.0


def native_progress(slot: dict, *, exact_bytes: int | None = None) -> TransferProgress:
    """Progress from a queue slot, or exact history bytes when available.

    SAB's queue reports MB with limited precision; history reports an exact
    byte count, which is the completion authority.
    """
    if exact_bytes is not None and exact_bytes > 0:
        return TransferProgress(exact_bytes, exact_bytes, 0)
    megabyte = 1024 * 1024
    total = int(_megabytes(slot.get("mb")) * megabyte)
    left = int(_megabytes(slot.get("mbleft")) * megabyte)
    completed = max(0, total - left)
    return TransferProgress(total, completed, 0)


def failure(category: Category, *, stage=Stage.EXECUTION, domain=Domain.EXECUTOR,
            retryability=Retryability.NEVER, diagnostic: str = "") -> NormalizedError:
    return NormalizedError(domain, category, stage, retryability=retryability,
                           integration_id=EXECUTOR_ID, diagnostic=diagnostic)


def unreachable(diagnostic: str = "") -> NormalizedError:
    """SAB could not be observed. Uncertainty, never absence or failure."""
    return failure(Category.EXECUTOR_UNAVAILABLE, stage=Stage.RECONCILIATION,
                   retryability=Retryability.BACKOFF, diagnostic=diagnostic)


def native_failure(message: str, secrets: tuple[str, ...] = ()) -> NormalizedError:
    """A job SAB positively reports as failed."""
    return failure(Category.TRANSFER_FAILED, retryability=Retryability.BACKOFF,
                   diagnostic=sanitize(message, secrets))


def sanitize(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Remove exact secret values from a native diagnostic."""
    value = str(text or "")
    for secret in secrets:
        if secret:
            value = value.replace(str(secret), "***")
    return value[:512]
