"""Roadmap Item 10 neutral provider/source presentation contracts."""
import re
from pathlib import Path

from api.routes import _public_transfer_presentation, _safe_original_resource
from integrations.catalog import definitions


def _request(kind, payload, name="fixture"):
    return {"kind": kind, "payload": payload, "name": name, "fingerprint": "", "preferred_provider": None}


def test_completed_transfer_uses_delivering_provider_and_ordered_route_labels():
    raw = {
        "id": 10, "status": "completed", "provider_provenance_status": "recorded",
        "current_provider_id": "general_http", "delivering_provider_id": "general_http",
        "request": _request("https", "https://downloads.example/file.bin?token=secret&signature=also-secret#fragment", "file.bin"),
        "route_attempts": [
            {"ordinal": 1, "provider_id": "alldebrid", "outcome": "failed"},
            {"ordinal": 2, "provider_id": "general_http", "outcome": "completed"},
        ],
        "execution_attempts": [{"provider_id": "general_http", "executor_id": "aria2", "delivered": True}],
    }
    result = _public_transfer_presentation(raw, definitions)
    assert result["delivering_provider_name"] == "HTTP & HTTPS"
    assert result["current_provider_name"] == "HTTP & HTTPS"
    assert [item["provider_name"] for item in result["route_attempts"]] == ["AllDebrid", "HTTP & HTTPS"]
    assert [item["ordinal"] for item in result["route_attempts"]] == [1, 2]
    assert result["original_resource"] == "https://downloads.example/file.bin?…"
    assert "token=secret" not in str(result)
    assert "signature=also-secret" not in str(result)
    assert "request" not in result


def test_active_transfer_projects_current_durable_provider_without_url_inference():
    raw = {
        "id": 11, "status": "downloading", "provider_provenance_status": "pending",
        "current_provider_id": "general_http", "delivering_provider_id": None,
        "request": _request("https", "https://rapidgator.net/looks-specialized", "active.bin"),
        "route_attempts": [{"ordinal": 1, "provider_id": "general_http", "outcome": "resolved"}],
        "execution_attempts": [],
    }
    result = _public_transfer_presentation(raw, definitions)
    assert result["current_provider_name"] == "HTTP & HTTPS"
    assert result["delivering_provider_name"] is None
    assert result["route_attempts"][0]["provider_name"] == "HTTP & HTTPS"


def test_unknown_historical_provider_keeps_stable_id_without_inventing_label():
    raw = {
        "id": 12, "status": "completed", "provider_provenance_status": "recorded",
        "current_provider_id": "removed_provider", "delivering_provider_id": "removed_provider",
        "request": _request("https", "https://example.test/file", "legacy.bin"),
        "route_attempts": [{"ordinal": 1, "provider_id": "removed_provider", "outcome": "completed"}],
        "execution_attempts": [],
    }
    result = _public_transfer_presentation(raw, definitions)
    assert result["delivering_provider_id"] == "removed_provider"
    assert result["delivering_provider_name"] is None
    assert result["route_attempts"][0]["provider_id"] == "removed_provider"
    assert result["route_attempts"][0]["provider_name"] is None


def test_minimal_projection_does_not_add_null_display_fields():
    result = _public_transfer_presentation({"id": 13, "name": "minimal"}, definitions)
    assert result == {"id": 13, "name": "minimal"}


def test_original_resource_redacts_userinfo_query_fragment_and_magnet_trackers():
    http = _safe_original_resource(_request(
        "https", "https://user:password@example.test/path/file?api_key=secret&x=1#frag", "file"
    ))
    assert http == "https://example.test/path/file?…"
    assert "password" not in http
    assert "secret" not in http
    magnet = _safe_original_resource(_request(
        "magnet", "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&tr=https://tracker.example/secret", "magnet"
    ))
    assert magnet == "<magnet:01234567...>"
    assert "tracker" not in magnet


_STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"


def _render_route_history_source():
    source = (_STATIC / "app.js").read_text(encoding="utf-8")
    start = source.index("function renderRouteHistory(")
    return source[start:source.index("\nfunction ", start + 1)]


def test_public_projection_passes_backend_route_identity_through_unchanged():
    routes = [
        {"ordinal": 1, "provider_id": "alldebrid", "outcome": "resolved", "route_origin": None,
         "route_location": None, "route_identity": "Torrent cache"},
        {"ordinal": 2, "provider_id": "alldebrid", "outcome": "completed", "route_origin": None,
         "route_location": None, "route_identity": "1fichier.com"},
    ]
    result = _public_transfer_presentation({"id": 1, "status": "completed", "route_attempts": [dict(r) for r in routes]}, definitions)
    assert [(r["route_origin"], r["route_location"], r["route_identity"]) for r in result["route_attempts"]] == [
        (None, None, "Torrent cache"), (None, None, "1fichier.com")]
    assert "cache_presence" not in str(result) and "delivery" not in str(result)


def test_route_history_renderer_is_a_thin_projector_of_backend_route_fields():
    """The browser renders backend truth: it may read only the presentation
    fields below, and no provider/domain/cache/request-kind semantics exist in
    the renderer or anywhere else in first-party JS."""
    body = _render_route_history_source()
    assert set(re.findall(r"\battempt\??\.(\w+)", body)) <= {
        "provider_name", "outcome", "route_identity", "route_location", "ordinal"}
    for forbidden in ("alldebrid", "debrid", "cache", "torrent", "magnet", "general_http",
                      "route_origin", "provider_id", "source_identity", "delivery", "hostname",
                      "endsWith", "new URL", "URL("):
        assert forbidden.casefold() not in body.casefold(), forbidden
    assert "attempt.route_identity || '—'" in body
    assert "attempt.route_location || attempt.route_identity" in body
    for script in _STATIC.glob("*.js"):
        text = script.read_text(encoding="utf-8")
        assert "Torrent cache" not in text and "BitTorrent" not in text, script.name
        assert "cache_presence" not in text, script.name
