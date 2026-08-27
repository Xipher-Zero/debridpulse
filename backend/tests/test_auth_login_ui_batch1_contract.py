"""Regression contract for the reviewed v1.0.11 Login Batch 1 surface."""

from __future__ import annotations

import hashlib
from pathlib import Path

from api import auth_routes


ROOT = Path(__file__).resolve().parents[2]
AUTH_ROUTES = ROOT / "backend" / "api" / "auth_routes.py"
OIDC_MARK = ROOT / "frontend" / "static" / "authentik-oidc.svg"
VERSION = ROOT / "VERSION"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_login_batch1_preserves_auth_order_and_reviewed_visual_contract() -> None:
    source = read(AUTH_ROUTES)

    assert 'width:min(760px,calc(100vw - 48px))' in source
    assert 'class="auth-backdrop"' in source
    assert 'class="brand-pulse"' in source
    assert 'class="version"' in source
    assert "read_version()" in source
    assert 'placeholder="Enter your username"' in source
    assert 'placeholder="Enter your password"' in source
    assert 'data-password-toggle' in source
    assert 'or continue with single sign-on' in source
    assert 'authentik-oidc.svg' in source
    assert 'Password-only LAN deployments may operate over HTTP.' in source
    assert 'OpenID Connect requires a canonical <span class="https">HTTPS</span> external URL.' in source

    # Hybrid mode is deliberately local credentials first, SSO second.
    assert source.index('<form method="post" action="/login"') < source.index(
        'or continue with single sign-on'
    ) < source.index('/auth/oidc/start?next=')


def test_login_interaction_script_is_exact_hash_pinned_and_narrow() -> None:
    source = read(AUTH_ROUTES)
    script = auth_routes._AUTH_PAGE_SCRIPT
    csp = auth_routes._auth_csp(allow_form=True)

    assert "hashlib.sha256(_AUTH_PAGE_SCRIPT.encode" in source
    assert f"script-src 'sha256-{auth_routes._AUTH_PAGE_SCRIPT_HASH}'" in csp
    assert "script-src 'self'" not in csp
    assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]
    assert "default-src 'none'" in csp
    assert "img-src data:" in csp
    assert "form-action 'self'" in csp

    # The only state it reads is the existing local theme preference. It must
    # never become an authentication/network helper merely for an eye toggle.
    assert 'localStorage.getItem("theme")' in script
    assert 'data-password-toggle' in script
    for forbidden in (
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "document.cookie",
        "sessionStorage",
        "Authorization",
        "csrf",
        "token",
    ):
        assert forbidden not in script


def test_login_uses_exact_reviewed_oidc_artwork_and_keeps_version_frozen() -> None:
    assert OIDC_MARK.is_file()
    assert hashlib.sha256(OIDC_MARK.read_bytes()).hexdigest() == (
        "1adfd82439678a210b8011b18b3451bf032d2f39bdb00a54029e8c021757ff59"
    )
    assert VERSION.read_text(encoding="utf-8").strip() == "1.0.10"
