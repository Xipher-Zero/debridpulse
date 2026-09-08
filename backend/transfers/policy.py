"""Universal lifecycle and retry decisions; no integration-native semantics.

Phase-1 compatibility keeps the legacy ``NormalizedError.recovery`` field alive
only at this core-owned boundary. Integrations emit factual classifications; the
core derives any legacy action required by the existing lifecycle machinery.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage
from transfers.models import TransferState


_TERMINAL = {TransferState.COMPLETED, TransferState.CONSOLIDATED, TransferState.DELETED, TransferState.CANCELLED}

# Phase-1 compatibility is expressed only in canonical semantic facts. The
# universal layer must never inspect an integration identity, native code, or
# diagnostic to select a lifecycle/recovery action.
_EXECUTION_ALTERNATE_CATEGORIES = frozenset({
    Category.READ_TIMEOUT,
    Category.SOURCE_NOT_FOUND,
    Category.TRANSFER_STALLED,
    Category.CONNECTION_FAILED,
    Category.REMOTE_READ_FAILED,
    Category.DNS_FAILURE,
    Category.CANDIDATE_EXPIRED,
    Category.SOURCE_TEMPORARILY_UNAVAILABLE,
    Category.CHECKSUM_MISMATCH,
    Category.TLS_FAILURE,
    Category.REMOTE_RESET,
})
_EXECUTION_RECONCILE_CATEGORIES = frozenset({
    Category.TRANSFER_INTERRUPTED,
    Category.RESOURCE_STATE_CONFLICT,
})


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
    """Derive the legacy Phase-1 action from normalized factual evidence.

    Explicit core-owned actions are preserved. Integration output is expected to
    carry ``Recovery.NONE``; only this universal boundary translates canonical
    facts for lifecycle code that still consumes the compatibility field.
    """
    if error.recovery != Recovery.NONE:
        return error.recovery
    if error.domain == Domain.SECURITY:
        return Recovery.FAIL
    if error.retryability == Retryability.UNKNOWN:
        return Recovery.REQUIRE_OPERATOR

    # Executor availability is an observation-authority problem regardless of
    # where it is noticed; reconcile the existing execution instead of creating
    # source-selection pressure.
    if error.category in {Category.EXECUTOR_UNAVAILABLE, Category.RECONCILIATION_FAILED}:
        return Recovery.RECONCILE

    # Existing execution failures that identify a remote/candidate problem keep
    # the established alternate-candidate compatibility behavior. Stage is a
    # canonical fact and prevents provider-resolution failures with the same
    # category from being silently reinterpreted as executor recovery.
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
    return Recovery.REQUIRE_OPERATOR


def compatibility_error(error: NormalizedError) -> NormalizedError:
    """Populate legacy policy fields at the core boundary, never in an adapter."""
    action = compatibility_recovery(error)
    operator = action in {Recovery.REQUIRE_OPERATOR, Recovery.REAUTHENTICATE} or error.domain == Domain.SECURITY
    if error.recovery == action and error.operator_action_required == operator:
        return error
    return replace(error, recovery=action, operator_action_required=operator)


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

    @staticmethod
    def compatibility(error: NormalizedError) -> NormalizedError:
        return compatibility_error(error)

    def retry_resolution(self, error, attempts, now):
        policy = replace(self,
            max_attempts=self.max_attempts if self.resolution_max_attempts is None else self.resolution_max_attempts,
            retry_delay=self.retry_delay if self.resolution_retry_delay is None else self.resolution_retry_delay)
        return policy.retry(error, attempts, now, can_refresh=True)

    def retry(self, error: NormalizedError, attempts: int, now: float, *, can_refresh=False, has_alternate=False) -> RetryDecision:
        error = compatibility_error(error)
        # Universal-core semantics remain unchanged unless the application
        # explicitly installs an environmental local-resource admission hook.
        # The hook is provider-neutral and may prove that a LOCAL_RESOURCE
        # failure is an application-level storage condition. Only a recognized
        # condition is deferred nonterminally; unknown local failures retain the
        # established retry/terminal policy below.
        if (
            error.domain == Domain.LOCAL_RESOURCE
            and self.local_resource_failure_handler is not None
            and self.local_resource_failure_handler(error)
        ):
            return RetryDecision(Recovery.RETRY, now)
        if error.domain == Domain.SECURITY or error.retryability in {Retryability.NEVER, Retryability.UNKNOWN}:
            return RetryDecision()
        if error.recovery in {Recovery.REQUIRE_OPERATOR, Recovery.FAIL, Recovery.NONE}:
            return RetryDecision()
        if attempts >= max(1, self.max_attempts):
            if (has_alternate and error.recovery in {
                    Recovery.RETRY,
                    Recovery.BACKOFF,
                    Recovery.RERESOLVE,
                    Recovery.TRY_ALTERNATE_CANDIDATE,
            }):
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
            delay = 0 if error.retryability == Retryability.IMMEDIATE else min(self.max_retry_delay, self.retry_delay * 2 ** max(0, attempts - 1))
            if error.retry_after_seconds is not None:
                delay = max(delay, error.retry_after_seconds)
            return RetryDecision(Recovery.RETRY, now + delay)
        return RetryDecision()
