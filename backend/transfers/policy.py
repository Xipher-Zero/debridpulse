"""Universal lifecycle and retry decisions; no integration-native semantics."""
from __future__ import annotations

from dataclasses import dataclass

from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability
from transfers.models import TransferState


_TERMINAL = {TransferState.COMPLETED, TransferState.DELETED, TransferState.CANCELLED}
_STORAGE_RESOURCE_FAILURES = frozenset({
    Category.DISK_FULL,
    Category.LOCAL_RESOURCE_EXHAUSTED,
    Category.QUOTA_EXCEEDED,
    Category.PERMISSION_DENIED,
    Category.LOCAL_IO_FAILURE,
    Category.PATH_UNAVAILABLE,
    Category.DOWNLOAD_STORAGE_FULL,
    Category.DOWNLOAD_STORAGE_READ_ONLY,
    Category.DOWNLOAD_STORAGE_UNAVAILABLE,
})


def transition_allowed(current: TransferState, target: TransferState, *, operator=False, verified=False) -> bool:
    if current == target:
        return True
    if current == TransferState.DELETED:
        return operator and target == TransferState.ACCEPTED
    if current == TransferState.COMPLETED:
        return target == TransferState.DELETED or (operator and target in {TransferState.ACCEPTED, TransferState.QUEUED})
    if current == TransferState.CANCELLED:
        return target == TransferState.DELETED or (operator and target in {TransferState.ACCEPTED, TransferState.QUEUED})
    if target == TransferState.DELETED:
        return True
    if current == TransferState.FAILED:
        return operator or (verified and target in {TransferState.COMPLETED, TransferState.POST_PROCESSING}) or target in {TransferState.RESOLVING, TransferState.QUEUED, TransferState.PAUSED}
    return target in set(TransferState) - {TransferState.ACCEPTED}


@dataclass(frozen=True)
class RetryDecision:
    action: Recovery = Recovery.REQUIRE_OPERATOR
    retry_at: float | None = None

    @property
    def automatic(self) -> bool:
        return self.retry_at is not None


@dataclass(frozen=True)
class TransferPolicy:
    max_attempts: int = 3
    retry_delay: float = 5.0
    max_retry_delay: float = 300.0
    max_active_executions: int = 5
    resolution_concurrency: int = 3
    adoption_stability_seconds: float = 3.25
    cleanup_after_completion: bool = False
    resource_poll_interval: float = 30.0
    resolution_max_attempts: int | None = None
    resolution_retry_delay: float | None = None
    stalled_after_seconds: float = 0

    def retry_resolution(self, error, attempts, now):
        from dataclasses import replace
        policy = replace(self,
            max_attempts=self.max_attempts if self.resolution_max_attempts is None else self.resolution_max_attempts,
            retry_delay=self.retry_delay if self.resolution_retry_delay is None else self.resolution_retry_delay)
        return policy.retry(error, attempts, now, can_refresh=True)

    def retry(self, error: NormalizedError, attempts: int, now: float, *, can_refresh=False, has_alternate=False) -> RetryDecision:
        # Download-storage failures are environmental admission conditions, not
        # logical transfer failures.  They must remain retryable regardless of
        # the normal execution-attempt budget so recovery of the same storage
        # domain can continue the same logical transfer.  ApplicationService
        # feeds these neutral LOCAL_RESOURCE semantics into the canonical
        # DiskCapacity owner, which closes dispatch until a recovery probe.
        if error.domain == Domain.LOCAL_RESOURCE and error.category in _STORAGE_RESOURCE_FAILURES:
            return RetryDecision(Recovery.RETRY, now)
        if error.domain == Domain.SECURITY or error.retryability in {Retryability.NEVER, Retryability.UNKNOWN}:
            return RetryDecision()
        if error.recovery in {Recovery.REQUIRE_OPERATOR, Recovery.FAIL, Recovery.NONE}:
            return RetryDecision()
        if attempts >= max(1, self.max_attempts):
            return RetryDecision()
        if error.retryability in {Retryability.AFTER_REAUTH, Retryability.AFTER_RESOURCE_CHANGE}:
            return RetryDecision(error.recovery)
        if error.recovery == Recovery.TRY_ALTERNATE_CANDIDATE and has_alternate:
            return RetryDecision(Recovery.TRY_ALTERNATE_CANDIDATE, now)
        if error.recovery == Recovery.RECONCILE:
            return RetryDecision(Recovery.RECONCILE, now + self.retry_delay)
        if error.retryability == Retryability.AFTER_RERESOLUTION or error.recovery == Recovery.RERESOLVE:
            return RetryDecision(Recovery.RERESOLVE, now + self.retry_delay) if can_refresh else RetryDecision()
        if error.retryability in {Retryability.IMMEDIATE, Retryability.BACKOFF}:
            delay = 0 if error.retryability == Retryability.IMMEDIATE else min(self.max_retry_delay, self.retry_delay * 2 ** max(0, attempts - 1))
            if error.retry_after_seconds is not None:
                delay = max(delay, error.retry_after_seconds)
            return RetryDecision(Recovery.RETRY, now + delay)
        return RetryDecision()
