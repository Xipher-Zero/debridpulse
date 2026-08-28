"""Regression contract for the reviewed v1.0.11 Login surface."""

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

    # Final review narrows the desktop card by ~26% while preserving full-width
    # fields. The password Sign In action alone is 30% narrower and centered;
    # OIDC keeps the full card width, and very small screens restore full width.
    assert 'width:min(560px,calc(100vw - 48px))' in source
    assert '.auth-action.primary[type="submit"] { width:70%;margin-left:auto;margin-right:auto; }' in source
    assert '@media (max-width:460px)' in source
    assert '.auth-action.primary[type="submit"] { width:100%; }' in source
    assert 'class="auth-backdrop"' in source
    assert 'class="brand-pulse"' in source
    assert 'class="version"' in source
    assert "read_version()" in source
    assert 'placeholder="Enter your username"' in source
    assert 'placeholder="Enter your password"' in source
    assert 'data-password-reveal' in source
    assert 'or continue with user name and password' in source
    assert 'Sign in with {provider_name}' in source
    assert 'authentik-oidc.svg' in source
    assert 'Password-only LAN deployments may operate over HTTP.' in source
    assert 'OpenID Connect requires a canonical <span class="https">HTTPS</span> external URL.' in source

    # The auth surface keeps application geometry while intentionally using a
    # more translucent glass material and stronger purple/blue depth in both
    # themes, matching the reviewed login mockup direction.
    assert '--card:rgba(10,16,33,.55);--card2:rgba(7,12,27,.40);' in source
    assert '--field:rgba(7,13,29,.52);--border:rgba(125,145,195,.24);--border-strong:rgba(151,172,226,.38);' in source
    assert '--card:rgba(255,255,255,.55);--card2:rgba(249,251,255,.35);' in source
    assert '--field:rgba(255,255,255,.50);--border:rgba(111,132,183,.24);--border-strong:rgba(103,132,203,.38);' in source
    assert '--glass-top:rgba(255,255,255,.13);--glass-line:rgba(255,255,255,.075);' in source
    assert '--glass-purple:rgba(180,92,255,.18);--glass-blue:rgba(42,148,255,.15);' in source
    assert '--glass-top:rgba(255,255,255,.88);--glass-line:rgba(255,255,255,.66);' in source
    assert '--glass-purple:rgba(161,91,255,.13);--glass-blue:rgba(66,150,255,.14);' in source
    assert 'border-radius:12px' in source
    assert '-webkit-backdrop-filter:blur(30px) saturate(165%)' in source
    assert 'backdrop-filter:blur(30px) saturate(165%)' in source
    assert '--icon-accent:#b0a3ff;--icon-accent-strong:#8bbcff;' in source
    assert '--icon-accent:#7868e4;--icon-accent-strong:#3f88f5;' in source
    assert 'body::before {' in source
    assert 'body::after {' in source
    assert source.count('class="wave micro"') >= 4
    assert source.count('class="wave strong"') >= 4
    assert source.count('class="wave filament"') >= 6
    assert 'class="particle twinkle"' in source
    assert source.count('<circle ') >= 60

    # The brand sparkline is deliberately dramatic rather than a small squiggle:
    # tall ECG-like excursions approach the logo from both sides and a broad
    # ghost stroke provides the luminous mockup-style halo.
    assert '_BRAND_PULSE_LEFT = "M8 60 H118' in source
    assert '_BRAND_PULSE_RIGHT = "M308 60' in source
    assert 'class="pulse-left pulse-ghost"' in source
    assert 'class="pulse-right pulse-ghost"' in source
    assert 'stroke-width:2.6' in source
    assert 'stroke-width:7' in source

    # The security note is a centered card unit rather than a left-gutter row.
    assert 'flex-direction:column;align-items:center;justify-content:center' in source
    assert 'text-align:center' in source
    assert '.foot-icon { flex:0 0 21px' in source

    # Hybrid mode deliberately prioritizes OIDC in the assembled controls.
    # Local credentials are appended only after the explanatory separator.
    oidc_append = source.index('controls.append(oidc_control)')
    divider_append = source.index(
        'controls.append(\'<div class="divider"><span>or continue with user name and password</span></div>\')'
    )
    password_append = source.index('controls.append(password_control)')
    assert oidc_append < divider_append < password_append


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

    # Password reveal is momentary: closed eye at rest, open eye only while the
    # user is actively holding mouse/touch/pen or Space/Enter. It always fails
    # back to hidden on release/cancel/leave/focus loss and never click-toggles.
    assert 'localStorage.getItem("theme")' in script
    assert 'data-password-reveal' in script
    assert 'pointerdown' in script
    assert 'pointerup' in script
    assert 'pointercancel' in script
    assert 'pointerleave' in script
    assert 'setPointerCapture' in script
    assert 'keydown' in script
    assert 'keyup' in script
    assert 'window.addEventListener("blur"' in script
    assert 'visibilitychange' in script
    assert 'p.type="text"' in script
    assert 'p.type="password"' in script
    assert 'addEventListener("click"' not in script
    assert 'class="eye" viewBox' in source
    assert 'class="eye-off" viewBox' in source
    assert '.password-reveal .eye { display:none; }.password-reveal .eye-off { display:block; }' in source
    assert '.password-reveal[aria-pressed="true"] .eye { display:block; }.password-reveal[aria-pressed="true"] .eye-off { display:none; }' in source
    assert 'aria-label="Hold to show password"' in source

    # The only state it reads is the existing local theme preference. It must
    # never become an authentication/network helper merely for password reveal.
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
    source = read(AUTH_ROUTES)
    assert OIDC_MARK.is_file()
    assert hashlib.sha256(OIDC_MARK.read_bytes()).hexdigest() == (
        "1adfd82439678a210b8011b18b3451bf032d2f39bdb00a54029e8c021757ff59"
    )
    assert '_static_asset("logo-128.png")' in source
    assert '_static_asset("favicon.svg")' in source
    assert VERSION.read_text(encoding="utf-8").strip() == "1.0.10"
