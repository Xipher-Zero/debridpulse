from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read_static(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_downloads_outer_view_does_not_clip_universal_card_paint():
    css = read_static("ui-downloads-page.css")

    assert "body.dp-v11-structural #view-torrents.active" in css
    assert "overflow: visible;" in css
    assert "body.dp-v11-structural #view-torrents .dp-downloads-table-wrap" in css
    assert "overflow: auto !important;" in css

    desktop_rule = css.split("body.dp-v11-structural #view-torrents.active", 1)[1].split("}", 1)[0]
    assert "overflow: hidden" not in desktop_rule


def test_clean_settings_uses_structural_root_and_one_internal_scroll_boundary():
    css = read_static("ui-settings-page.css")
    runtime = read_static("ui-settings-page.js")

    selector = "body.dp-v11-structural #view-settings.dp-settings-clean-view.active"
    assert selector in css
    rule = css.split(selector, 1)[1].split("}", 1)[0]
    assert "overflow: hidden;" in rule

    scroll = css.split(".dp-settings-scroll", 1)[1].split("}", 1)[0]
    assert "overflow-y: auto;" in scroll
    assert "overscroll-behavior: contain;" in scroll

    assert '<section class="card dp-settings-header-card"' in runtime
    assert '<section class="card dp-settings-footer"' in runtime
    assert 'class="card dp-settings-card' in runtime
    assert "dp-settings-master" not in runtime


def test_inherited_settings_shell_state_is_rejected_by_clean_runtime():
    shell = read_static("ui-shell-structural.css")
    legacy = read_static("style.css")
    runtime = read_static("ui-settings-page.js")
    settings = read_static("ui-settings-page.css")

    # app.js still emits the inherited class until the monolith cleanup pass.
    # The clean Settings entry removes it synchronously before rendering any
    # Settings DOM, so the legacy zero-padding viewport never owns the page.
    assert "#content.settings-active" in legacy
    assert "body.dp-v11-structural #content.settings-active" in shell
    assert "classList.remove('settings-active')" in runtime
    assert "#content.settings-active" not in settings
    assert "#content" not in settings


def test_help_moves_scroll_boundary_inside_the_card_body():
    css = read_static("ui-help-page.css")

    assert "#view-help .help-panels-wrap" in css
    assert "overflow: visible;" in css
    assert ".help-panel.active > .card" in css
    assert ".help-panel.active > .card > .card-body" in css
    assert "overflow-y: auto;" in css


def test_help_downloads_and_settings_corrections_do_not_redefine_card_material():
    combined = (
        read_static("ui-downloads-page.css")
        + read_static("ui-help-page.css")
        + read_static("ui-settings-page.css")
    )

    forbidden = (
        "--dp-panel-surface:",
        "--dp-panel-frame:",
        "--dp-panel-shadow:",
        "box-shadow: var(--dp-panel-shadow)",
        "background: var(--dp-panel-surface)",
    )
    for token in forbidden:
        assert token not in combined


def test_card_paint_boundary_page_layers_are_loaded_after_universal_language():
    overlay = read_static("style-v11.css")

    universal = overlay.index("/ui-universal-language.css?v=20")
    stats = overlay.index("/ui-statistics-page.css?v=21")
    activity = overlay.index("/ui-activity-log-page.css?v=28")
    downloads = overlay.index("/ui-downloads-page.css?v=27")
    settings = overlay.index("/ui-settings-page.css?v=1")
    help_page = overlay.index("/ui-help-page.css?v=22")

    assert universal < stats < activity < downloads < settings < help_page
