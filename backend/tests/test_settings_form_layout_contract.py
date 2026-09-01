"""Final-state Settings form-layout and archive-password contracts."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = STATIC / "ui-settings-downloads-completion.js"
LAYOUT = STATIC / "ui-settings-form-layout.css"
LOADER = STATIC / "ui-presentation-loader.js"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_archive_password_masks_are_presentation_only_and_cannot_enter_model_state() -> None:
    runtime = source(RUNTIME)
    visibility = runtime.split("function setRowVisibility", 1)[1].split("function setRevealAll", 1)[0]
    assert "input.dataset.passwordDisplay = reveal ? 'raw' : 'masked';" in visibility

    commit = runtime.split("function commitPasswordLine", 1)[1].split("function eyeSvg", 1)[0]
    assert "if (input.dataset.passwordDisplay === 'masked') return;" in commit
    assert commit.index("input.dataset.passwordDisplay === 'masked'") < commit.index(
        "extractionPasswords.values[index] = String(input.value || '');"
    )

    activate = runtime.split("function activatePasswordLine", 1)[1].split("function commitPasswordLine", 1)[0]
    assert "commitPasswordLine(editor?.closest('[data-panel=\"extraction\"]'), previous);" in activate
    assert "setRowVisibility(previous, previousIndex, false);" in activate
    assert activate.index("commitPasswordLine") < activate.index("setRowVisibility(previous, previousIndex, false);")

    render = runtime.split("function renderPasswordRows", 1)[1].split("async function loadExtractionPasswords", 1)[0]
    assert "setRowVisibility(" in render
    assert "input.value = extractionPasswords.revealAll || index === extractionPasswords.activeIndex" not in render


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
