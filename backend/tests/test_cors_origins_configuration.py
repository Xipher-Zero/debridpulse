"""``CORS_ORIGINS`` names explicit, credential-trusted browser origins; a
wildcard is refused rather than becoming "reflect any origin" (XR-S02)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from main import configured_cors_origins


@pytest.mark.parametrize("raw", ["*", " * ", "https://a.example,*", "*,https://a.example"])
def test_a_wildcard_origin_is_refused_explicitly(raw):
    with pytest.raises(RuntimeError, match=r"CORS_ORIGINS=\* is refused"):
        configured_cors_origins(raw)


def test_explicit_origins_and_the_unset_default_are_kept():
    assert configured_cors_origins("") == []
    assert configured_cors_origins(" https://a.example , http://b.example:8080 ,") == [
        "https://a.example", "http://b.example:8080"]


def _cors_app(origins):
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True,
                       allow_methods=["GET"], allow_headers=["Authorization"])
    app.get("/probe")(lambda: {"ok": True})
    return TestClient(app)


def test_an_explicit_origin_is_honoured_and_any_other_is_not():
    """The accepted configuration keeps its meaning: only a listed origin is
    granted credentialed access."""
    client = _cors_app(configured_cors_origins("https://a.example"))
    granted = client.get("/probe", headers={"Origin": "https://a.example"})
    assert granted.headers.get("access-control-allow-origin") == "https://a.example"
    assert granted.headers.get("access-control-allow-credentials") == "true"
    other = client.get("/probe", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers


def test_why_the_wildcard_is_refused_starlette_reflects_any_origin_with_credentials():
    """The behaviour the refusal prevents, shown on the same middleware."""
    client = _cors_app(["*"])
    reflected = client.get("/probe", headers={"Origin": "https://evil.example", "Cookie": "s=1"})
    assert reflected.headers.get("access-control-allow-origin") == "https://evil.example"
