"""Login/global DebridPulse brand presentation contract."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AUTH_ROUTES = ROOT / "backend" / "api" / "auth_routes.py"
THEME_BOOTSTRAP = ROOT / "frontend" / "static" / "ui-theme-bootstrap.js"
STATIC_LOGO = ROOT / "frontend" / "static" / "logo-128.png"
FAVICON_32 = ROOT / "frontend" / "static" / "favicon-32.png"


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
    assert "data:image/png;base64," in source
    assert "default-src 'none'" in source
    assert "img-src data:" in source
    assert "style-src 'unsafe-inline'" in source

    encoded = re.search(r'data:image/png;base64,([^\"]+)', source)
    assert encoded is not None
    assert base64.b64decode(encoded.group(1), validate=True) == STATIC_LOGO.read_bytes()


def test_first_paint_shell_and_browser_tab_use_reviewed_brand_assets() -> None:
    bootstrap = read(THEME_BOOTSTRAP)

    assert "/logo-128.png?v=5" in bootstrap
    assert "/favicon-32.png?v=5" in bootstrap
    assert "/apple-touch-icon.png?v=5" in bootstrap
    assert "installReviewedBrandAssets" in bootstrap
    assert "#sidebar .logo-icon" in bootstrap

    # These hashes pin the exact raster derivatives generated from the supplied
    # Batch 2 logo rather than a hand-redrawn or compatibility mark.
    assert sha256(STATIC_LOGO) == "e2141f5a2354ec7b24ca5a564896cc08aa2b7071521c43083c670e2da34ca63e"
    assert sha256(FAVICON_32) == "8934c983ca926bcb746d6c6f23a9181542d3397b62dda6a50c00d0719ca3f72b"
