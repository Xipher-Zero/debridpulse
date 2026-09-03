from transfers.applicability import (
    ApplicabilityClass,
    ProviderApplicability,
    ProviderApplicabilityInput,
    classify_provider_applicability,
)
from transfers.models import TransferRequest


def test_missing_applicability_cannot_be_inferred_from_url_request_types() -> None:
    undeclared = ProviderApplicabilityInput(
        "undeclared-http", frozenset({"http", "https"}), True, None
    )
    request = TransferRequest("https", "https://example.test/file")
    assert classify_provider_applicability(request, (undeclared,)) == ()


def test_missing_applicability_cannot_fall_back_to_opaque_static_routing() -> None:
    undeclared = ProviderApplicabilityInput(
        "undeclared-opaque", frozenset({"parcel"}), True, None
    )
    declared = ProviderApplicabilityInput(
        "declared-opaque", frozenset({"parcel"}), True, ProviderApplicability()
    )
    request = TransferRequest("parcel", "opaque-payload")
    assert classify_provider_applicability(request, (undeclared,)) == ()
    matches = classify_provider_applicability(request, (declared, undeclared))
    assert [(match.provider_id, match.classification) for match in matches] == [
        ("declared-opaque", ApplicabilityClass.STATIC)
    ]


def test_static_magnet_routing_remains_independent_of_url_applicability_facts() -> None:
    static = ProviderApplicabilityInput(
        "static", frozenset({"magnet"}), True, None
    )
    request = TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40)
    matches = classify_provider_applicability(request, (static,))
    assert [(match.provider_id, match.classification) for match in matches] == [
        ("static", ApplicabilityClass.STATIC)
    ]
