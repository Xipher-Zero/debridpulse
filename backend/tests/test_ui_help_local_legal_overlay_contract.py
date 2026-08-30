from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
LOADER = STATIC / "ui-presentation-loader.js"
RUNTIME = STATIC / "ui-help-license-documents.js"
CSS = STATIC / "ui-help-license-documents.css"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_help_local_document_overlay_loads_after_help_chrome():
    loader = read(LOADER)

    assert "/ui-help-license-documents.css?v=1" in loader
    assert "/ui-help-license-documents.js?v=1" in loader
    assert loader.index("/ui-help-chrome.js?v=1") < loader.index(
        "/ui-help-license-documents.js?v=1"
    )


def test_license_actions_are_converted_from_external_navigation_to_local_buttons():
    runtime = read(RUNTIME)

    for document_id in ("gpl", "notice", "upstream-mit", "source-offer", "third-party"):
        assert f"'{document_id}'" in runtime

    assert ".dp-help-license-actions a[href]" in runtime
    assert "button.type = 'button';" in runtime
    assert "button.dataset.legalDocument = documentId;" in runtime
    assert "anchor.replaceWith(button);" in runtime
    assert "fetch('/api/legal-documents/' + encodeURIComponent(documentId)" in runtime
    assert "iframe" not in runtime.lower()


def test_overlay_uses_canonical_dialog_semantics_and_normal_close_paths():
    runtime = read(RUNTIME)
    css = read(CSS)

    assert "dp-backdrop dp-help-legal-backdrop" in runtime
    assert "dp-dialog dp-dialog--lg dp-help-legal-dialog" in runtime
    assert "dialog.setAttribute('role', 'dialog');" in runtime
    assert "dialog.setAttribute('aria-modal', 'true');" in runtime
    assert "close.setAttribute('aria-label', 'Close document');" in runtime
    assert "if (event.target === backdrop) closeModal();" in runtime
    assert "event.key === 'Escape'" in runtime
    assert "event.key !== 'Tab'" in runtime
    assert "opener.focus();" in runtime

    assert "overflow-y: auto" in css
    assert "max-height: min(86vh, 900px)" in css
    assert "overscroll-behavior: contain" in css


def test_overlay_identifies_bundled_snapshot_and_exposes_only_explicit_latest_link():
    runtime = read(RUNTIME)

    assert "This is the copy bundled with DebridPulse " in runtime
    assert "View the latest version on GitHub." in runtime
    assert "link.target = '_blank';" in runtime
    assert "link.rel = 'noopener';" in runtime
    assert "payload.latest_url" in runtime
    assert "modal.documentBody.textContent = String(payload.content || '');" in runtime


def test_help_legal_overlay_runtime_is_in_frontend_syntax_gate():
    workflow = read(WORKFLOW)
    assert "node --check frontend/static/ui-help-license-documents.js" in workflow
