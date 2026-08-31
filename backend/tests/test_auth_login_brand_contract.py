"""Login/global DebridPulse brand presentation contract."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from api import auth_routes


ROOT = Path(__file__).resolve().parents[2]
AUTH_ROUTES = ROOT / "backend" / "api" / "auth_routes.py"
THEME_BOOTSTRAP = ROOT / "frontend" / "static" / "ui-theme-bootstrap.js"
PRESENTATION_LOADER = ROOT / "frontend" / "static" / "ui-presentation-loader.js"
SHELL_RUNTIME = ROOT / "frontend" / "static" / "ui-shell-runtime.js"
STATIC_LOGO = ROOT / "frontend" / "static" / "logo-128.png"
SHELL_LOGO = ROOT / "frontend" / "static" / "logo.svg"
FAVICON = ROOT / "frontend" / "static" / "favicon.svg"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_login_embeds_the_exact_reviewed_logo_without_public_static_dependency() -> None:
    source = read(AUTH_ROUTES)

    assert "_AUTH_MARK_HTML" in source
    assert "_AUTH_MARK_SVG" not in source
    assert 'class="brand-lockup"' in source
    assert 'class="brand-mark"' in source
    assert "Debrid<span>Pulse</span>" in source
    assert "Secure access" in source
    assert "base64.b64encode" in source
    assert '_static_asset("logo-128.png").read_bytes()' in source
    assert "default-src 'none'" in source
    assert "img-src data:" in source
    assert "style-src 'unsafe-inline'" in source

    # The authentication HTML remains self-contained, but its embedded mark is
    # generated from the exact reviewed large-format asset rather than carrying
    # a second manually copied raster payload that can drift from that asset.
    match = re.search(
        r'src="data:image/png;base64,([A-Za-z0-9+/=]+)"',
        auth_routes._AUTH_MARK_HTML,
    )
    assert match is not None
    assert base64.b64decode(match.group(1), validate=True) == STATIC_LOGO.read_bytes()
