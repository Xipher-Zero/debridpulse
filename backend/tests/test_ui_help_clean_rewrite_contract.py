from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")








def test_help_page_uses_canonical_components_without_repainting_them():
    css = _read(STATIC / "ui-help-page.css")

    assert ".dp-card {" not in css
    assert ".dp-tab {" not in css
    assert ".dp-btn {" not in css
    assert "linear-gradient(160deg, var(--dp-surface-2), var(--dp-surface-1))" not in css
