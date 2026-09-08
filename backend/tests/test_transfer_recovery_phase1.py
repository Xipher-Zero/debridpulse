"""Phase 1 transfer failure semantics and recovery-ownership contract."""
from pathlib import Path
import ssl

import pytest

from executors.aria2.translation import exception_failure, native_failure
from transfers.errors import (
    Category, Confidence, Domain, EvidenceBasis, NormalizedError, Origin,
    Permanence, Recovery, Retryability, Stage,
)
from transfers.policy import compatibility_error


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


def test_core_compatibility_owns_legacy_action_selection():
    transport = native_failure("1", "connection reset by peer")
    translated = compatibility_error(transport)
    assert transport.recovery == Recovery.NONE
    assert translated.recovery == Recovery.TRY_ALTERNATE_CANDIDATE
    assert not translated.operator_action_required

    unknown = native_failure("1", "unrelated future executor problem")
    translated_unknown = compatibility_error(unknown)
    assert unknown.recovery == Recovery.NONE
    assert translated_unknown.recovery == Recovery.REQUIRE_OPERATOR
    assert translated_unknown.operator_action_required

    disk = native_failure("9", "disk full")
    assert compatibility_error(disk).recovery == Recovery.REQUIRE_OPERATOR


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
