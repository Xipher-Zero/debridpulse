"""Transfer failure semantics and universal recovery-ownership contracts."""
from pathlib import Path
import ssl

import pytest

from executors.aria2.translation import exception_failure, native_failure
from transfers.errors import (
    Category, Confidence, Domain, EvidenceBasis, NormalizedError, Origin,
    Permanence, Recovery, Retryability, Stage,
)
from transfers.policy import (
    RecoveryAction, RecoveryContext, TransferPolicy, compatibility_error,
    failure_signature, meaningful_progress_threshold,
)


def assert_factual(error):
    assert error.recovery == Recovery.NONE
    assert not error.operator_action_required


@pytest.mark.parametrize("diagnostic,category", [
    ("TLS/SSL receive failure: record decode error", Category.TLS_FAILURE),
    ("connection reset by peer", Category.REMOTE_RESET),
    ("remote stream ended with premature EOF", Category.REMOTE_READ_FAILED),
])
def test_generic_code1_bounded_transport_classification(diagnostic, category):
    error = native_failure("1", diagnostic)
    assert error.domain == Domain.NETWORK
    assert error.category == category
    assert error.origin == Origin.REMOTE_SOURCE
    assert error.retryability == Retryability.BACKOFF
    assert error.permanence == Permanence.TEMPORARY
    assert error.confidence == Confidence.MEDIUM
    assert error.evidence_basis == EvidenceBasis.DIAGNOSTIC
    assert_factual(error)


def test_generic_code1_unknown_and_near_match_remain_unknown():
    for diagnostic in ("unrelated future executor problem", "SSL handshake error", "network failed"):
        error = native_failure("1", diagnostic)
        assert error.domain == Domain.EXECUTOR
        assert error.category == Category.UNMAPPED_EXECUTOR_ERROR
        assert error.origin == Origin.EXECUTOR
        assert error.retryability == Retryability.UNKNOWN
        assert error.permanence == Permanence.UNKNOWN
        assert error.confidence == Confidence.UNKNOWN
        assert error.evidence_basis == EvidenceBasis.UNKNOWN
        assert_factual(error)


def test_specific_native_code_outranks_diagnostic_language():
    error = native_failure("19", "connection reset by peer")
    assert error.category == Category.DNS_FAILURE
    assert error.domain == Domain.NETWORK
    assert error.evidence_basis == EvidenceBasis.NATIVE_CODE
    assert error.confidence == Confidence.HIGH
    assert_factual(error)


@pytest.mark.parametrize("code,domain,category,origin,retryability,permanence", [
    ("2", Domain.NETWORK, Category.READ_TIMEOUT, Origin.REMOTE_SOURCE, Retryability.BACKOFF, Permanence.TEMPORARY),
    ("19", Domain.NETWORK, Category.DNS_FAILURE, Origin.REMOTE_SOURCE, Retryability.BACKOFF, Permanence.TEMPORARY),
    ("24", Domain.RESOLUTION, Category.CANDIDATE_EXPIRED, Origin.REMOTE_SOURCE, Retryability.AFTER_RERESOLUTION, Permanence.PERMANENT),
    ("9", Domain.LOCAL_RESOURCE, Category.DISK_FULL, Origin.LOCAL_SYSTEM, Retryability.AFTER_RESOURCE_CHANGE, Permanence.PERMANENT),
    ("13", Domain.LOCAL_RESOURCE, Category.LOCAL_PATH_CONFLICT, Origin.LOCAL_SYSTEM, Retryability.AFTER_RESOURCE_CHANGE, Permanence.PERMANENT),
    ("28", Domain.EXECUTOR, Category.INVALID_CONFIGURATION, Origin.EXECUTOR, Retryability.NEVER, Permanence.PERMANENT),
    ("32", Domain.INTEGRITY, Category.CHECKSUM_MISMATCH, Origin.REMOTE_SOURCE, Retryability.AFTER_RERESOLUTION, Permanence.PERMANENT),
])
def test_representative_native_semantics(code, domain, category, origin, retryability, permanence):
    error = native_failure(code, "native diagnostic")
    assert (error.domain, error.category, error.origin) == (domain, category, origin)
    assert error.retryability == retryability
    assert error.permanence == permanence
    assert error.confidence == Confidence.HIGH
    assert error.evidence_basis == EvidenceBasis.NATIVE_CODE
    assert_factual(error)


def test_certificate_identity_failure_is_strict_and_factual():
    error = exception_failure(ssl.SSLCertVerificationError("certificate verify failed"))
    assert error.domain == Domain.SECURITY
    assert error.category == Category.TLS_IDENTITY_FAILURE
    assert error.origin == Origin.SECURITY_POLICY
    assert error.retryability == Retryability.NEVER
    assert error.permanence == Permanence.PERMANENT
    assert error.evidence_basis == EvidenceBasis.TYPED_EXCEPTION
    assert_factual(error)


def test_core_compatibility_owns_only_context_free_legacy_projection():
    transport = native_failure("1", "connection reset by peer")
    translated = compatibility_error(transport)
    assert transport.recovery == Recovery.NONE
    assert translated.recovery == Recovery.TRY_ALTERNATE_CANDIDATE
    assert not translated.operator_action_required

    unknown = native_failure("1", "unrelated future executor problem")
    translated_unknown = compatibility_error(unknown)
    assert unknown.recovery == Recovery.NONE
    assert translated_unknown.recovery == Recovery.NONE
    assert not translated_unknown.operator_action_required

    disk = native_failure("9", "disk full")
    translated_disk = compatibility_error(disk)
    assert translated_disk.recovery == Recovery.FAIL
    assert not translated_disk.operator_action_required


def test_unknown_recovery_is_context_sensitive_and_bounded():
    error = NormalizedError(
        Domain.EXECUTOR, Category.UNMAPPED_EXECUTOR_ERROR, Stage.EXECUTION,
        retryability=Retryability.UNKNOWN, origin=Origin.EXECUTOR,
    )
    policy = TransferPolicy(retry_delay=2)
    first = policy.recover(error, RecoveryContext(
        consecutive_no_progress_failures=1, same_signature_failures=1,
    ), 100.0)
    assert first.action == RecoveryAction.RETRY_SAME_CANDIDATE
    assert first.retry_at == 102.0

    refresh = policy.recover(error, RecoveryContext(
        consecutive_no_progress_failures=2, same_signature_failures=2,
        can_refresh=True,
    ), 100.0)
    assert refresh.action == RecoveryAction.REFRESH_CANDIDATE

    alternate = policy.recover(error, RecoveryContext(
        consecutive_no_progress_failures=2, same_signature_failures=2,
        has_alternate=True,
    ), 100.0)
    assert alternate.action == RecoveryAction.TRY_ALTERNATE_CANDIDATE

    exhausted = policy.recover(error, RecoveryContext(
        consecutive_no_progress_failures=2, same_signature_failures=2,
    ), 100.0)
    assert exhausted.action == RecoveryAction.WAIT_FOR_OPERATOR
    assert exhausted.quiescence_reason == "recovery_exhausted"
    assert exhausted.wake_condition == "operator_retry"


def test_structured_rate_limit_delay_is_honored_and_persistable():
    error = NormalizedError(
        Domain.PROVIDER, Category.RATE_LIMITED, Stage.EXECUTION,
        retryability=Retryability.BACKOFF, retry_after_seconds=45,
    )
    decision = TransferPolicy(retry_delay=1).recover(
        error, RecoveryContext(consecutive_no_progress_failures=1), 10.0,
    )
    assert decision.action == RecoveryAction.BACKOFF
    assert decision.retry_at == 55.0
    assert decision.quiescence_reason == "retry_backoff"
    assert decision.wake_condition == "retry_at:55.0"


def test_expiry_refreshes_immediately_and_security_never_loops():
    expired = NormalizedError(
        Domain.RESOLUTION, Category.CANDIDATE_EXPIRED, Stage.EXECUTION,
        retryability=Retryability.AFTER_RERESOLUTION,
    )
    policy = TransferPolicy()
    assert policy.recover(expired, RecoveryContext(can_refresh=True), 50).action == RecoveryAction.REFRESH_CANDIDATE

    security = NormalizedError(
        Domain.SECURITY, Category.TLS_IDENTITY_FAILURE, Stage.EXECUTION,
        retryability=Retryability.NEVER,
    )
    assert policy.recover(security, RecoveryContext(can_refresh=True, has_alternate=True), 50).action == RecoveryAction.FAIL_PERMANENTLY


def test_failure_signature_uses_only_canonical_facts():
    left = NormalizedError(
        Domain.NETWORK, Category.READ_TIMEOUT, Stage.EXECUTION,
        retryability=Retryability.BACKOFF, integration_id="one",
        native_code="17", diagnostic="first native message",
    )
    right = NormalizedError(
        Domain.NETWORK, Category.READ_TIMEOUT, Stage.EXECUTION,
        retryability=Retryability.BACKOFF, integration_id="two",
        native_code="999", diagnostic="different diagnostic",
    )
    assert failure_signature(left) == failure_signature(right)


def test_meaningful_progress_threshold_is_centralized_and_byte_based():
    assert meaningful_progress_threshold(None) == 1024 * 1024
    assert meaningful_progress_threshold(100 * 1024 * 1024) == 1024 * 1024
    assert meaningful_progress_threshold(4) >= 1


def test_normalized_error_roundtrip_preserves_evidence_without_policy_injection():
    error = native_failure("1", "TLS/SSL receive failure: record decode error")
    restored = NormalizedError.from_dict(error.as_dict(diagnostics=True))
    assert restored == error
    assert restored.recovery == Recovery.NONE
    assert restored.evidence_basis == EvidenceBasis.DIAGNOSTIC


def test_provider_executor_modules_do_not_select_recovery_actions():
    backend = Path(__file__).resolve().parents[1]
    roots = (backend / "providers", backend / "executors")
    offenders = []
    for root in roots:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "Recovery." in text or "recovery=Recovery" in text:
                offenders.append(str(path.relative_to(backend)))
    assert offenders == []
