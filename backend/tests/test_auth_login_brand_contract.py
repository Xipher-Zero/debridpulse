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
    assert "data:image/png;base64," in source
    assert "default-src 'none'" in source
    assert "img-src data:" in source
    assert "style-src 'unsafe-inline'" in source

    # Decode the self-contained HTML data URI and compare its bytes directly to
    # the reviewed large-format raster. This keeps the contract independent of
    # the Python source-string delimiter while still requiring exact image data.
    match = re.search(r'src="data:image/png;base64,([A-Za-z0-9+/=]+)"', source)
    assert match is not None
    assert base64.b64decode(match.group(1), validate=True) == STATIC_LOGO.read_bytes()


def test_first_paint_keeps_reviewed_large_branding_and_restores_compact_tab_mark() -> None:
    bootstrap = read(THEME_BOOTSTRAP)
    favicon = read(FAVICON)

    assert "/logo-128.png?v=5" in bootstrap
    assert "/apple-touch-icon.png?v=5" in bootstrap
    assert "vectorIcon.href = '/favicon.svg?v=6'" in bootstrap
    assert "icon32.remove()" in bootstrap
    assert "installReviewedBrandAssets" in bootstrap
    assert "#sidebar .logo-icon" in bootstrap

    # Large-format branding remains pinned to the reviewed Batch 2 raster while
    # the browser tab intentionally uses the restored compact original vector.
    assert sha256(STATIC_LOGO) == "e2141f5a2354ec7b24ca5a564896cc08aa2b7071521c43083c670e2da34ca63e"
    assert 'viewBox="0 0 64 64"' in favicon
    assert 'transform="scale(.125)"' in favicon
