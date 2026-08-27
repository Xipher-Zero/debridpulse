"""Login/global DebridPulse brand presentation contract."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AUTH_ROUTES = ROOT / "backend" / "api" / "auth_routes.py"
STATIC_LOGO = ROOT / "frontend" / "static" / "logo.svg"
FAVICON = ROOT / "frontend" / "static" / "favicon.svg"
DOCS_LOGO = ROOT / "docs" / "logo.svg"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_login_uses_native_vector_debridpulse_mark_without_relaxing_csp() -> None:
    source = read(AUTH_ROUTES)

    assert "_AUTH_MARK_SVG" in source
    assert 'class="brand-lockup"' in source
    assert 'class="brand-mark"' in source
    assert "Debrid<span>Pulse</span>" in source
    assert "Secure access" in source
    assert "<image" not in source[source.index("_AUTH_MARK_SVG"):source.index("_AUTH_PAGE_STYLE")]
    assert "data:image" not in source[source.index("_AUTH_MARK_SVG"):source.index("_AUTH_PAGE_STYLE")]
    assert "default-src 'none'" in source
    assert "style-src 'unsafe-inline'" in source


def test_global_logo_surfaces_are_true_vector_art() -> None:
    for path in (STATIC_LOGO, FAVICON, DOCS_LOGO):
        raw = read(path)
        lowered = raw.lower()
        assert "<svg" in lowered
        assert "viewbox=" in lowered
        assert "<path" in lowered
        assert "<image" not in lowered
        assert "data:image" not in lowered
        assert "#a62cff" in lowered
        assert "#208cff" in lowered
