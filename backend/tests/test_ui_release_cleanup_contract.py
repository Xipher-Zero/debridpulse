"""Release-state frontend cleanup contracts for DebridPulse v1.0.11."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
DOCS = ROOT / "docs"


def test_retired_duplicate_legacy_stylesheet_is_absent() -> None:
    """The canonical compatibility stylesheet is style.css; no duplicate copy remains."""
    assert (STATIC / "style.css").is_file()
    assert not (STATIC / "style-legacy.css").exists()


def test_release_architecture_documents_the_effective_presentation_bootstrap() -> None:
    architecture = (DOCS / "UI_FRONTEND_ARCHITECTURE.md").read_text(encoding="utf-8")
    bootstrap = (STATIC / "ui-theme-bootstrap.js").read_text(encoding="utf-8")
    loader = (STATIC / "ui-presentation-loader.js").read_text(encoding="utf-8")

    assert "ui-presentation-loader.js" in bootstrap
    assert "ui-presentation-loader.js" in architecture
    assert "presentation loader" in architecture.lower()
    assert "const STYLES" in loader
    assert "const RUNTIMES" in loader


def test_release_docs_record_browser_validation_without_calling_live_layers_dead() -> None:
    architecture = (DOCS / "UI_FRONTEND_ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "dd6984c940ee9dcffd20d8566f568d6eec9cbd3d" in architecture
    assert "browser-validated" in architecture.lower()
    assert "live calibration" in architecture.lower()
