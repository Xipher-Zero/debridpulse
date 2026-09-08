"""Universal lifecycle and recovery policy; no integration-native semantics.

Providers and executors emit factual normalized evidence. This universal owner
combines those facts with durable transfer context and selects recovery behavior.
The legacy ``NormalizedError.recovery`` field is populated only as a core-owned
compatibility projection for older lifecycle paths.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage
from transfers.models import TransferState


_TERMINAL = {TransferState.COMPLETED, TransferState.CONSOLIDATED, TransferState.DELETED, TransferState.CANCELLED}
_EXECUTION_ALTERNATE_CATEGORIES = frozenset({
    Category.READ_TIMEOUT, Category.SOURCE_NOT_FOUND, Category.TRANSFER_STALLED,
    Category.CONNECTION_FAILED, Category.REMOTE_READ_FAILED, Category.DNS_FAILURE,
    Category.CANDIDATE_EXPIRED, Category.SOURCE_TEMPORARILY_UNAVAILABLE,
    Category.CHECKSUM_MISMATCH, Category.TLS_FAILURE, Category.REMOTE_RESET,
})
_EXECUTION_RECONCILE_CATEGORIES = frozenset({Category.TRANSFER_INTERRUPTED, Category.RESOURCE_STATE_CONFLICT})
_EXPIRY_CATEGORIES = frozenset({Category.CANDIDATE_EXPIRED, Category.SOURCE_EXPIRED, Category.RESOURCE_EXPIRED})
_TRANSIENT_CATEGORIES = frozenset({
    Category.READ_TIMEOUT, Category.CONNECTION_TIMEOUT, Category.CONNECTION_FAILED,
    Category.REMOTE_RESET, Category.REMOTE_READ_FAILED, Category.DNS_FAILURE,
    Category.TLS_FAILURE, Category.SOURCE_TEMPORARILY_UNAVAILABLE,
    Category.SOURCE_UNAVAILABLE, Category.PROVIDER_UNAVAILABLE,
    Category.PROVIDER_DEGRADED, Category.PROVIDER_MAINTENANCE,
    Category.RESOLUTION_TEMPORARILY_FAILED, Category.TRANSFER_STALLED,
    Category.RATE_LIMITED, Category.CONCURRENCY_LIMITED,
})
_RECONCILE_CATEGORIES = frozenset({
    Category.EXECUTOR_UNAVAILABLE,
    Category.RECONCILIATION_FAILED,
    Category.TRANSFER_INTERRUPTED,
    Category.RESOURCE_STATE_CONFLICT,
})

MEANINGFUL_PROGRESS_FLOOR_BYTES = 64 * 1024
MEANINGFUL_PROGRESS_CEILING_BYTES = 1024 * 1024
MEANINGFUL_PROGRESS_DIVISOR = 100


def meaningful_progress_threshold(expected_bytes: int | None) -> int:
    """Deterministic byte threshold; known small files can still reach it."""
    value = int(expected_bytes or 0)
    if value <= 0:
        return MEANINGFUL_PROGRESS_CEILING_BYTES
    one_percent = max(1, (value + MEANINGFUL_PROGRESS_DIVISOR - 1) // MEANINGFUL_PROGRESS_DIVISOR)
    bounded = min(MEANINGFUL_PROGRESS_CEILING_BYTES,
                  max(MEANINGFUL_PROGRESS_FLOOR_BYTES, one_percent))
    return min(value, bounded)


def failure_signature(error: NormalizedError) -> str:
    """Stable signature built only from canonical normalized facts."""
    return "|".join((error.stage.value, error.domain.value, error.category.value, error.retryability.value))


def transition_allowed(current: TransferState, target: TransferState, *, operator=False, verified=False) -> bool:
    if current == target:
        return True
    if current == TransferState.DELETED:
        return operator and target == TransferState.ACCEPTED
    if current == TransferState.CONSOLIDATED:
        return target == TransferState.DELETED
    if current == TransferState.COMPLETED:
        return target == TransferState.DELETED or (operator and target in {TransferState.ACCEPTED, TransferState.QUEUED})
    if current == TransferState.CANCELLED:
        return target == TransferState.DELETED or (operator and target in {TransferState.ACCEPTED, TransferState.QUEUED})
    if target == TransferState.DELETED:
        return True
    if current == TransferState.FAILED:
        return operator or (verified and target in {TransferState.COMPLETED, TransferState.POST_PROCESSING}) or target in {TransferState.RESOLVING, TransferState.QUEUED, TransferState.PAUSED}
    return target in set(TransferState) - {TransferState.ACCEPTED, TransferState.CONSOLIDATED}


def compatibility_recovery(error: NormalizedError) -> Recovery:
    """Project only context-free facts into the transitional compatibility field."""
    if error.recovery != Recovery.NONE:
        return error.recovery
    if error.domain == Domain.SECURITY:
        return Recovery.FAIL
    if error.retryability == Retryability.UNKNOWN:
        return Recovery.NONE
    if error.category in {Category.EXECUTOR_UNAVAILABLE, Category.RECONCILIATION_FAILED}:
        return Recovery.RECONCILE
    if error.stage == Stage.EXECUTION:
        if error.category in _EXECUTION_ALTERNATE_CATEGORIES:
            return Recovery.TRY_ALTERNATE_CANDIDATE
        if error.category in _EXECUTION_RECONCILE_CATEGORIES:
            return Recovery.RECONCILE
        if error.domain == Domain.LIFECYCLE and error.category == Category.LOCAL_PATH_CONFLICT:
            return Recovery.RECONCILE
        if error.category == Category.INVALID_CONFIGURATION:
            return Recovery.REQUIRE_OPERATOR
    if error.retryability == Retryability.NEVER:
        return Recovery.FAIL
    if error.retryability == Retryability.IMMEDIATE:
        return Recovery.RETRY
    if error.retryability == Retryability.BACKOFF:
        return Recovery.BACKOFF
    if error.retryability == Retryability.AFTER_REAUTH:
        return Recovery.REAUTHENTICATE
    if error.retryability == Retryability.AFTER_RERESOLUTION:
        return Recovery.RERESOLVE
    if error.retryability == Retryability.AFTER_RESOURCE_CHANGE:
        return Recovery.REQUIRE_OPERATOR
    return Recovery.NONE


def compatibility_error(error: NormalizedError) -> NormalizedError:
    action = compatibility_recovery(error)
    operator = action in {Recovery.REQUIRE_OPERATOR, Recovery.REAUTHENTICATE} or error.domain == Domain.SECURITY
    if error.recovery == action and error.operator_action_required == operator:
        return error
    return replace(error, recovery=action, operator_action_required=operator)


class RecoveryAction(StrEnum):
    RECONCILE = "reconcile"
    RETRY_SAME_CANDIDATE = "retry_same_candidate"
    BACKOFF = "backoff"
    REFRESH_CANDIDATE = "refresh_candidate"
    TRY_ALTERNATE_CANDIDATE = "try_alternate_candidate"
    WAIT_FOR_RESOURCE = "wait_for_resource"
    WAIT_FOR_PROVIDER = "wait_for_provider"
    WAIT_FOR_OPERATOR = "wait_for_operator"
    FAIL_PERMANENTLY = "fail_permanently"


@dataclass(frozen=True)
class RecoveryContext:
    execution_attempts: int = 0
    consecutive_no_progress_failures: int = 0
    failures_since_meaningful_progress: int = 0
    same_signature_failures: int = 0
    candidate_refreshes: int = 0
    candidate_switches: int = 0
    recovery_epoch: int = 0
    can_refresh: bool = False
    has_alternate: bool = False
    provider_ready: bool = True
    executor_ready: bool = True
    storage_ready: bool = True
    input_required: bool = False


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    reason: str
    retry_at: float | None = None
    quiescence_reason: str | None = None
    wake_condition: str | None = None

    @property
    def automatic(self) -> bool:
        return self.action not in {RecoveryAction.WAIT_FOR_OPERATOR, RecoveryAction.FAIL_PERMANENTLY}


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
    local_resource_failure_handler: Callable[[NormalizedError], bool] | None = None
    same_candidate_no_progress_limit: int = 2
    refreshes_per_recovery_epoch: int = 1

    @staticmethod
    def compatibility(error: NormalizedError) -> NormalizedError:
        return compatibility_error(error)

    def retry_resolution(self, error, attempts, now):
        policy = replace(self,
            max_attempts=self.max_attempts if self.resolution_max_attempts is None else self.resolution_max_attempts,
            retry_delay=self.retry_delay if self.resolution_retry_delay is None else self.resolution_retry_delay)
        return policy.retry(error, attempts, now, can_refresh=True)

    def _delay(self, error: NormalizedError, failures: int) -> float:
        delay = 0.0 if error.retryability == Retryability.IMMEDIATE else min(
            self.max_retry_delay, self.retry_delay * 2 ** max(0, int(failures) - 1))
        if error.retry_after_seconds is not None:
            delay = max(delay, float(error.retry_after_seconds))
        return max(0.0, delay)

    def recover(self, error: NormalizedError, context: RecoveryContext, now: float) -> RecoveryDecision:
        """Choose from normalized evidence, durable accounting and readiness."""
        if error.domain == Domain.SECURITY:
            return RecoveryDecision(RecoveryAction.FAIL_PERMANENTLY, "security_failure")
        if error.domain == Domain.INTEGRITY:
            return RecoveryDecision(RecoveryAction.FAIL_PERMANENTLY, "integrity_failure")
        if error.retryability == Retryability.NEVER:
            return RecoveryDecision(RecoveryAction.FAIL_PERMANENTLY, "nonretryable_failure")
        if (error.domain == Domain.LOCAL_RESOURCE
                and self.local_resource_failure_handler is not None
                and self.local_resource_failure_handler(error)):
            context = replace(context, storage_ready=False)
        if context.input_required:
            return RecoveryDecision(
                RecoveryAction.WAIT_FOR_OPERATOR, "input_required",
                quiescence_reason="input_required", wake_condition="operator_input",
            )
        if error.domain == Domain.LOCAL_RESOURCE and not context.storage_ready:
            return RecoveryDecision(
                RecoveryAction.WAIT_FOR_RESOURCE, "storage_unavailable",
                quiescence_reason="storage_unavailable",
                wake_condition=f"storage_healthy:{error.domain.value}",
            )
        if not context.provider_ready:
            return RecoveryDecision(
                RecoveryAction.WAIT_FOR_PROVIDER, "provider_unavailable",
                quiescence_reason="provider_disabled", wake_condition="provider_enabled",
            )
        if not context.executor_ready:
            return RecoveryDecision(
                RecoveryAction.WAIT_FOR_RESOURCE, "executor_unavailable",
                quiescence_reason="executor_unavailable", wake_condition="executor_available",
            )

        can_refresh = context.can_refresh and context.candidate_refreshes < max(1, self.refreshes_per_recovery_epoch)
        if error.category in _EXPIRY_CATEGORIES:
            if can_refresh:
                return RecoveryDecision(
                    RecoveryAction.REFRESH_CANDIDATE, "candidate_expired_refresh", retry_at=now,
                )
            if context.has_alternate:
                return RecoveryDecision(
                    RecoveryAction.TRY_ALTERNATE_CANDIDATE, "candidate_expired_alternate", retry_at=now,
                )
            return RecoveryDecision(
                RecoveryAction.WAIT_FOR_OPERATOR, "candidate_expired_exhausted",
                quiescence_reason="recovery_exhausted", wake_condition="operator_retry",
            )

        no_progress = max(context.consecutive_no_progress_failures, context.same_signature_failures)
        if error.category in _RECONCILE_CATEGORIES:
            if no_progress < max(1, self.same_candidate_no_progress_limit):
                retry_at = now + self._delay(error, max(1, no_progress))
                return RecoveryDecision(
                    RecoveryAction.RECONCILE, "reconciliation_backoff",
                    retry_at=retry_at, quiescence_reason="retry_backoff",
                    wake_condition=f"retry_at:{retry_at}",
                )
            if error.category == Category.TRANSFER_INTERRUPTED and context.has_alternate:
                return RecoveryDecision(
                    RecoveryAction.TRY_ALTERNATE_CANDIDATE,
                    "reconciliation_exhausted_alternate", retry_at=now,
                )
            return RecoveryDecision(
                RecoveryAction.WAIT_FOR_OPERATOR, "reconciliation_exhausted",
                quiescence_reason="recovery_exhausted", wake_condition="operator_retry",
            )

        if no_progress < max(1, self.same_candidate_no_progress_limit):
            if error.category == Category.RATE_LIMITED:
                action = RecoveryAction.BACKOFF
                reason = "rate_limited_backoff"
            elif error.retryability == Retryability.BACKOFF:
                action = RecoveryAction.BACKOFF
                reason = "transient_backoff"
            else:
                action = RecoveryAction.RETRY_SAME_CANDIDATE
                reason = "bounded_same_candidate_retry"
            retry_at = now + self._delay(error, max(1, no_progress))
            return RecoveryDecision(
                action, reason, retry_at=retry_at,
                quiescence_reason="retry_backoff" if retry_at > now else None,
                wake_condition=f"retry_at:{retry_at}" if retry_at > now else None,
            )

        if can_refresh and (error.retryability in {Retryability.UNKNOWN, Retryability.BACKOFF,
                                                   Retryability.AFTER_RERESOLUTION}
                            or error.category in _TRANSIENT_CATEGORIES):
            return RecoveryDecision(
                RecoveryAction.REFRESH_CANDIDATE, "no_progress_refresh", retry_at=now,
            )
        if context.has_alternate:
            return RecoveryDecision(
                RecoveryAction.TRY_ALTERNATE_CANDIDATE, "no_progress_alternate", retry_at=now,
            )
        if error.retryability == Retryability.AFTER_RESOURCE_CHANGE:
            if error.domain == Domain.LOCAL_RESOURCE:
                return RecoveryDecision(
                    RecoveryAction.WAIT_FOR_RESOURCE, "resource_change_required",
                    quiescence_reason="storage_unavailable",
                    wake_condition=f"storage_healthy:{error.domain.value}",
                )
            return RecoveryDecision(
                RecoveryAction.WAIT_FOR_OPERATOR, "resource_change_operator",
                quiescence_reason="recovery_exhausted", wake_condition="operator_retry",
            )
        return RecoveryDecision(
            RecoveryAction.WAIT_FOR_OPERATOR, "recovery_budget_exhausted",
            quiescence_reason="recovery_exhausted", wake_condition="operator_retry",
        )

    def retry(self, error: NormalizedError, attempts: int, now: float, *, can_refresh=False, has_alternate=False) -> RetryDecision:
        """Compatibility retry path for request-resolution callers."""
        error = compatibility_error(error)
        if (error.domain == Domain.LOCAL_RESOURCE and self.local_resource_failure_handler is not None
                and self.local_resource_failure_handler(error)):
            return RetryDecision(Recovery.RETRY, now)
        if error.domain == Domain.SECURITY or error.retryability == Retryability.NEVER:
            return RetryDecision()
        if error.retryability == Retryability.UNKNOWN:
            if attempts >= max(1, self.max_attempts):
                if can_refresh:
                    return RetryDecision(Recovery.RERESOLVE, now)
                if has_alternate:
                    return RetryDecision(Recovery.TRY_ALTERNATE_CANDIDATE, now)
                return RetryDecision()
            return RetryDecision(Recovery.RETRY, now + self._delay(error, max(1, attempts)))
        if error.recovery in {Recovery.REQUIRE_OPERATOR, Recovery.FAIL}:
            return RetryDecision()
        if attempts >= max(1, self.max_attempts):
            if (has_alternate and error.recovery in {Recovery.RETRY, Recovery.BACKOFF,
                                                     Recovery.RERESOLVE, Recovery.TRY_ALTERNATE_CANDIDATE}):
                return RetryDecision(Recovery.TRY_ALTERNATE_CANDIDATE, now)
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
            return RetryDecision(Recovery.RETRY, now + self._delay(error, max(1, attempts)))
        return RetryDecision()
