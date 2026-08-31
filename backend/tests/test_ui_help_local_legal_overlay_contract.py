from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
LOADER = STATIC / "ui-presentation-loader.js"
RUNTIME = STATIC / "ui-help-license-documents.js"
CSS = STATIC / "ui-help-license-documents.css"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")






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


def test_overlay_uses_card_material_and_document_text_reflows_to_modal_width():
    runtime = read(RUNTIME)
    css = read(CSS)

    assert "documentBody = document.createElement('div')" in runtime
    assert "renderBundledDocument(modal.documentBody, payload.content);" in runtime
    assert "filter(Boolean).join(' ')" in runtime
    assert "dp-help-legal-document-block" in runtime

    assert "background: linear-gradient(160deg, var(--dp-surface-2), var(--dp-surface-1))" in css
    assert "box-shadow: var(--dp-shadow-card)" in css
    assert "border: 1px solid var(--dp-border-default)" in css
    assert "font-size: var(--dp-type-card-title-size)" in css
    assert "padding: var(--dp-card-padding)" in css

    assert ".dp-help-legal-document {" in css
    assert "max-width: none" in css
    assert ".dp-help-legal-document-block {" in css
    assert "white-space: normal" in css
    assert "overflow-wrap: anywhere" in css


def test_overlay_identifies_bundled_snapshot_and_exposes_only_explicit_latest_link():
    runtime = read(RUNTIME)

    assert "This is the copy bundled with DebridPulse " in runtime
    assert "View the latest version on GitHub." in runtime
    assert "link.target = '_blank';" in runtime
    assert "link.rel = 'noopener';" in runtime
    assert "payload.latest_url" in runtime
    assert "renderBundledDocument(modal.documentBody, payload.content);" in runtime


def test_help_legal_overlay_runtime_is_in_frontend_syntax_gate():
    workflow = read(WORKFLOW)
    assert "node --check frontend/static/ui-help-license-documents.js" in workflow
