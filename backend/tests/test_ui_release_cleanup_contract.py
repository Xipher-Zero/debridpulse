"""Release-state frontend cleanup contracts for DebridPulse v1.0.11."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
DOCS = ROOT / "docs"


def test_retired_duplicate_legacy_stylesheet_is_absent() -> None:
    """The canonical compatibility stylesheet is style.css; no duplicate copy remains."""
    assert (STATIC / "style.css").is_file()
    assert not (STATIC / "style-legacy.css").exists()


def test_release_docs_record_permanent_browser_validation_without_legacy_live_layers() -> None:
    architecture = (DOCS / "UI_FRONTEND_ARCHITECTURE.md").read_text(encoding="utf-8").lower()
    assert "permanent ci" in architecture
    assert "browser runtime" in architecture
    assert "real-browser smoke contract" in architecture
    assert "six canonical navigation surfaces" in architecture
    assert "retired presentation-loader/finalization dependencies" in architecture
    assert "live calibration" not in architecture
    assert "`ui-runtime.js` and `ui-downloads-runtime.js` are physically absent" in architecture
    assert "must not be reintroduced as a corrective mechanism" in architecture
