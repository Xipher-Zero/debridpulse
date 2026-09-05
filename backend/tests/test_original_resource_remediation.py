"""Post-MP Original Resource presentation remediation contracts."""
from api.routes import _public_transfer_presentation, _safe_original_resource
from core.logging_utils import sanitize_log_value
from integrations.catalog import definitions


def _request(kind, payload, name="fixture"):
    return {"kind": kind, "payload": payload, "name": name, "fingerprint": "", "preferred_provider": None}


def test_long_http_original_resource_keeps_source_identity_without_log_redaction():
    path = "/".join(["very-long-release-segment"] * 10 + ["GF010926-DD2-RN.part1.rar.html"])
    raw = f"https://rapidgator.example/{path}?token=SECRET&download=1#fragment"

    presented = _safe_original_resource(_request("https", raw, "GF010926-DD2-RN.part1.rar"))

    assert presented is not None
    assert presented != "<url>"
    assert presented.startswith("https://rapidgator.example/")
    assert presented.endswith("GF010926-DD2-RN.part1.rar.html?…")
    assert len(presented) <= 180
    assert "SECRET" not in presented
    assert "download=1" not in presented
    assert "fragment" not in presented

    # Browser presentation is intentionally separate from conservative log redaction.
    assert sanitize_log_value(raw, max_length=180) == "<url>"


def test_original_resource_credentials_and_query_values_are_not_exposed():
    value = _safe_original_resource(_request(
        "https",
        "https://user:password@example.test:8443/path/file.rar?api_key=SECRET&x=1#fragment",
        "file.rar",
    ))
    assert value == "https://example.test:8443/path/file.rar?…"
    assert "user" not in value
    assert "password" not in value
    assert "SECRET" not in value
    assert "x=1" not in value
    assert "fragment" not in value


def test_provider_capability_cannot_replace_durable_original_resource():
    capability = "https://provider.example/unlocked/file.rar?token=CAPABILITY_SECRET&signature=SIGNED"
    raw = {
        "id": 81,
        "status": "downloading",
        "request": _request("https", "https://source.example/original/file.rar?user_query=safe-to-hide", "file.rar"),
        "candidate": {"endpoints": [{"address": capability}]},
        "candidates": [{"endpoints": [{"address": capability}]}],
        "route_attempts": [{"provider_id": "alldebrid", "candidate": capability}],
        "execution_attempts": [{"provider_id": "alldebrid", "candidate_source": capability}],
    }

    result = _public_transfer_presentation(raw, definitions)

    assert result["original_resource"] == "https://source.example/original/file.rar?…"
    assert "CAPABILITY_SECRET" not in str(result)
    assert "SIGNED" not in str(result)
    assert "unlocked" not in result["original_resource"]
