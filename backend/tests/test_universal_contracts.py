"""Failures and routing are semantic; no concrete provider defines these contracts."""
import json
from dataclasses import replace

import pytest

from transfers.errors import (
    Category, Domain, NormalizedError, Recovery, Retryability, Stage,
    TransferError, safe_context, safe_diagnostic,
)
from transfers.models import (
    Capability, Endpoint, IntegrationDescriptor, ResolutionResult,
    ResourceState, TransferCandidate, TransferRequest,
)
from transfers.registry import IntegrationRegistry


@pytest.mark.parametrize("category", [Category.DESTINATION_BLOCKED, Category.UNSAFE_REDIRECT,
                                     Category.TLS_IDENTITY_FAILURE, Category.HOST_KEY_FAILURE,
                                     Category.SECURITY_POLICY_REJECTED])
def test_security_cannot_be_downgraded_by_an_adapter(category):
    error = NormalizedError(Domain.EXECUTOR, category, Stage.EXECUTION,
                            Retryability.BACKOFF, Recovery.RETRY, operator_action_required=False)
    assert error.domain == Domain.SECURITY
    assert error.retryability == Retryability.NEVER
    assert error.recovery == Recovery.FAIL
    assert error.operator_action_required


@pytest.mark.parametrize("category", [Category.UNMAPPED_PROVIDER_ERROR, Category.UNMAPPED_EXECUTOR_ERROR,
                                     Category.INVALID_ADAPTER_RESPONSE, Category.PROVIDER_PROTOCOL_VIOLATION])
def test_unknown_failures_preserve_uncertainty(category):
    error = NormalizedError(Domain.PROVIDER, category, Stage.RESOLUTION,
                            Retryability.IMMEDIATE, Recovery.RETRY,
                            native_code="FUTURE_ERROR", diagnostic="safe native explanation")
    assert error.retryability == Retryability.UNKNOWN
    assert error.recovery == Recovery.REQUIRE_OPERATOR
    assert error.native_code == "FUTURE_ERROR"
    assert "safe native explanation" in json.dumps(error.as_dict(diagnostics=True))
    assert "safe native explanation" not in json.dumps(error.as_dict())


def test_diagnostics_remove_short_signed_urls_headers_keys_and_known_secrets():
    raw = ("https://example.org/x?s=short ssh://user:pw@host/x "
           "Bearer tokenvalue password=word apikey=keyvalue\n"
           "Cookie: session=private\n"
           "-----BEGIN OPENSSH PRIVATE KEY-----\nsecretdata\n-----END OPENSSH PRIVATE KEY-----\n"
           "opaque-account-secret")
    safe = safe_diagnostic(raw, secrets=("opaque-account-secret",))
    for secret in ("example.org", "user:pw", "tokenvalue", "word", "keyvalue", "session=private",
                   "secretdata", "opaque-account-secret"):
        assert secret not in safe
    assert "secretvalue" not in json.dumps(safe_context({"token": "secretvalue", "attempt": 3}))


def test_error_roundtrip_is_sanitized_and_cannot_mutate_diagnostics_after_validation():
    error = NormalizedError(Domain.NETWORK, Category.CONNECTION_TIMEOUT, Stage.EXECUTION,
                            Retryability.BACKOFF, Recovery.BACKOFF,
                            context={"attempt": 3, "authorization": "private"})
    restored = NormalizedError.from_dict(json.loads(json.dumps(error.as_dict(diagnostics=True))))
    assert restored == error
    assert restored.context == {"attempt": 3}
    with pytest.raises(TypeError):
        restored.context["secret"] = "injected"


class MinimalProvider:
    def __init__(self, name, kinds=("synthetic",), priority=0):
        self.descriptor = IntegrationDescriptor(name, name, frozenset({Capability.RESOLVE}),
                                                frozenset(kinds), priority=priority)

    async def resolve(self, request):
        return ResolutionResult(ResourceState.AVAILABLE)


def test_routing_requires_enabled_healthy_capability_and_request_support():
    registry = IntegrationRegistry()
    low, high = MinimalProvider("low"), MinimalProvider("high", priority=10)
    wrong = MinimalProvider("wrong", kinds=("different",), priority=100)
    for provider in (low, high, wrong):
        registry.register_provider(provider)
    request = TransferRequest("synthetic", "payload")
    assert registry.provider_for(request) is high
    registry.mark_health("high", healthy=False)
    assert registry.provider_for(request) is low
    assert not registry.eligible_providers(request, capability=Capability.CLEANUP)
    low.descriptor = replace(low.descriptor, enabled=False)
    with pytest.raises(TransferError) as failure:
        registry.provider_for(request)
    assert failure.value.error.category == Category.UNSUPPORTED_REQUEST


def test_declaring_a_capability_does_not_substitute_for_implementing_it():
    provider = MinimalProvider("incomplete")
    provider.descriptor = replace(provider.descriptor, capabilities=frozenset({Capability.RESOLVE, Capability.CLEANUP}))
    with pytest.raises(TypeError):
        IntegrationRegistry().register_provider(provider)


def test_candidate_secrets_do_not_appear_in_repr():
    candidate = TransferCandidate("payload", (Endpoint("https", "https://host/signed-secret"),),
                                  context={"token": "context-secret"},
                                  refresh_request=TransferRequest("https", "source-secret"))
    assert "secret" not in repr(candidate)
