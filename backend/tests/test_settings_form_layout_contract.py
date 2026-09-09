"""Final-state Settings form-layout and archive-password contracts."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = STATIC / "ui-settings-downloads-completion.js"
ARCHIVE = STATIC / "ui-settings-archive-passwords.js"
LAYOUT = STATIC / "ui-settings-form-layout.css"
LOADER = STATIC / "ui-presentation-loader.js"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_archive_password_masks_are_presentation_only_and_cannot_enter_model_state() -> None:
    # ui-settings-archive-passwords.js is the sole owner of the editor; the
    # completion runtime must not carry a competing password editor.
    completion = source(RUNTIME)
    for token in (
        "extractionPasswords",
        "renderPasswordRows",
        "loadExtractionPasswords",
        "buildPasswordEditor",
        "syncExtractionPasswordSource",
        "dpExtractionClearCompat",
    ):
        assert token not in completion

    archive = source(ARCHIVE)

    # The mask is rendered by present() and is flagged non-authoritative. The
    # displayed value is only reassigned when it actually differs, so a
    # Playwright/keyboard fill that focuses a masked row is not clobbered.
    present = archive.split("function present(", 1)[1].split("function refreshPresentation", 1)[0]
    assert "input.dataset.passwordDisplay=raw?'raw':'masked'" in present
    assert "const next=raw?String(row.value||''):mask(row.value);if(input.value!==next)input.value=next;" in present

    # A masked field can never write its displayed value back into row state:
    # every commit path is guarded on passwordDisplay==='raw'.
    assert "input.addEventListener('input',()=>{if(input.dataset.passwordDisplay!=='raw')return;" in archive
    assert "input.addEventListener('blur',()=>{if(input.dataset.passwordDisplay==='raw'){row.value=input.value;" in archive
    assert "function mask(value){return'•'.repeat(String(value||'').length);}" in archive


def test_settings_secret_fields_and_extraction_controls_keep_accepted_geometry() -> None:
    css = source(LAYOUT)
    alldebrid = css.split(".dp-settings-alldebrid-key-row.is-configured {", 1)[1].split("}", 1)[0]
    assert "grid-template-columns: minmax(0, 1fr) max-content;" in alldebrid

    external = css.split(".dp-settings-external-connection-row.is-secret-configured {", 1)[1].split("}", 1)[0]
    assert "minmax(300px, .9fr) 320px" in external
    assert "column-gap: 32px;" in external

    controls = css.split(".dp-settings-extraction-controls-row {", 1)[1].split("}", 1)[0]
    assert "width: min(100%, 1040px);" in controls
    assert "margin-inline: auto;" in controls
    assert "minmax(360px, 460px) minmax(0, 520px)" in controls
    assert "justify-content: center;" in controls


def test_archive_password_editor_fills_remaining_extraction_card_height() -> None:
    css = source(LAYOUT)
    assert '.dp-settings-scroll:has([data-panel="extraction"]:not([hidden])) .dp-settings-panels' in css
    assert '[data-panel="extraction"]:not([hidden])' in css
    assert ".dp-settings-extraction-card > .card-body" in css

    password_field = css.split(".dp-settings-extraction-password-field {", 1)[1].split("}", 1)[0]
    assert "flex: 1 1 auto;" in password_field
    assert "display: flex;" in password_field
    assert "flex-direction: column;" in password_field

    editor = css.split(".dp-settings-extraction-password-editor {", 1)[1].split("}", 1)[0]
    assert "flex: 1 1 auto;" in editor
    assert "max-height: none;" in editor
