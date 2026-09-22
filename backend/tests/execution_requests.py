"""Canonical execution requests for executor-boundary tests."""
from pathlib import Path

from transfers.models import (
    ExecutionRequest, ExecutionSubject, ExecutionWork, MaterializationKind, MaterializationPlan,
)


def file_request(candidate, target, attempt_id, *, root=None, paused=False) -> ExecutionRequest:
    """A FILE work item whose core plan authorizes exactly ``target`` inside
    ``root`` (default: the target's directory)."""
    base = Path(root) if root is not None else Path(target).parent
    plan = MaterializationPlan(MaterializationKind.FILE, str(base.resolve()), str(target))
    return ExecutionRequest(ExecutionWork(ExecutionSubject.of(candidate), plan), attempt_id, paused)
