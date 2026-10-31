"""Login/global DebridPulse brand presentation contract."""

from __future__ import annotations

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


def test_login_draws_the_canonical_logo_inline_without_public_static_dependency() -> None:
    source = read(AUTH_ROUTES)

    assert "_AUTH_MARK_HTML" in source
    assert "_AUTH_MARK_SVG" not in source
    assert 'class="brand-lockup"' in source
    assert '_logo_svg("dp-login-logo", class_name="brand-mark")' in source
    assert "Debrid<span>Pulse</span>" in source
    assert "Secure access" in source
    assert '_static_asset("logo.svg")' in source
    assert "default-src 'none'" in source
    assert "img-src data:" in source
    assert "style-src 'unsafe-inline'" in source

    # The authentication HTML remains self-contained, and its mark is the
    # product logo's one geometry source, logo.svg, drawn inline -- not a
    # raster copy that can drift from it and cannot follow the login theme.
    assert "logo-128.png" not in source and not STATIC_LOGO.exists()
    mark = auth_routes._AUTH_MARK_HTML
    assert mark.startswith("<svg ") and 'class="brand-mark"' in mark and "<img" not in mark
    paths = re.findall(r'\sd="([^"]+)"', read(SHELL_LOGO))
    assert len(paths) == 2 and all(f'd="{path}"' in mark for path in paths)
