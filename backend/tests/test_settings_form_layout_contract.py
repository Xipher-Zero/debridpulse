"""Final-state Settings form-layout and archive-password contracts."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = STATIC / "ui-settings-page.js"
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


def test_this_late_layer_is_not_a_second_owner_of_two_bounded_geometries() -> None:
    """DP 1.0.13 consolidation.

    This layer used to restate the configured AllDebrid key row's column
    template AND, with a `gap` shorthand, silently replace the row gap its one
    owner had set -- which is what made the credential block expand ~32px
    taller than any other Settings field. It also restated the Extraction
    behaviour row's geometry. Both belong to their own owners now, and the
    point of this case is that they stay there.
    """
    css = source(LAYOUT)
    assert ".dp-settings-alldebrid-key-row.is-configured {" not in css
    assert ".dp-settings-extraction-controls-row" not in css
    assert ".dp-settings-extraction-behavior" not in css

    page = source(STATIC / "ui-settings-page.css")
    # The credential row states no vertical rhythm of its own at all now: it is
    # the shared inline field, where the informational stack is one unit and the
    # control is centred against it. The excessive band this case was written
    # about cannot come back, because there is no stacked row left to gap.
    grammar = page.split("#view-settings .dp-settings-inline-field {", 1)[1].split("}", 1)[0]
    assert "align-items: center;" in grammar
    row = page.split("#view-settings .dp-settings-alldebrid-key-row > .dp-settings-inline-field-control {", 1)[1].split("}", 1)[0]
    assert "flex: 1 1 auto" in row

    extraction = source(STATIC / "ui-settings-downloads-completion.css")
    group = extraction.split("#view-settings .dp-settings-extraction-behavior {", 1)[1].split("}", 1)[0]
    assert "border: 1px solid var(--dp-divider);" in group
    assert "margin-inline: auto;" in group


def test_archive_password_editor_fills_remaining_extraction_card_height() -> None:
    css = source(LAYOUT)
    assert '.dp-settings-scroll:has([data-panel="extraction"]:not([hidden])) .dp-settings-panels' in css
    assert '[data-panel="extraction"]:not([hidden])' in css
    assert ".dp-settings-extraction-card > .card-body" in css

    password_field = css.split(".dp-settings-extraction-password-field {", 1)[1].split("}", 1)[0]
    assert "flex: 1 1 auto;" in password_field
    assert "display: flex;" in password_field
    assert "flex-direction: column;" in password_field

    # The chain ends at the FIELD. What the editor does with the height it is
    # handed belongs to the editor's one geometry owner, so this layer states
    # nothing about it -- and the chain's root is bounded to exactly the scroll
    # viewport, which is what stops the card growing as passwords are added.
    assert ".dp-settings-extraction-password-editor" not in css
    panels = css.split('.dp-settings-scroll:has([data-panel="extraction"]:not([hidden])) .dp-settings-panels', 1)[1].split("}", 1)[0]
    assert "min-height: 100%;" in panels
    assert "max-height: 100%;" in panels

    archive = (STATIC / "ui-settings-archive-passwords.css").read_text(encoding="utf-8")
    editor = archive.split("#view-settings .dp-settings-extraction-password-editor {", 1)[1].split("}", 1)[0]
    assert "max-height: min(58vh, 460px);" in editor
