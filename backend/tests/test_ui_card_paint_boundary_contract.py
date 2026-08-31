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


def test_clean_settings_uses_one_master_card_and_one_internal_scroll_boundary():
    css = read_static("ui-settings-page.css")
    runtime = read_static("ui-settings-page.js")

    selector = "body.dp-v11-structural #view-settings.dp-settings-clean-view.active"
    assert selector in css
    rule = css.split(selector, 1)[1].split("}", 1)[0]
    assert "overflow: visible;" in rule
    assert "overflow: hidden;" not in rule

    master = css.split("#view-settings > .dp-settings-master-card", 1)[1].split("}", 1)[0]
    assert "flex: 1 1 auto;" in master
    assert "min-height: 0;" in master
    assert "margin-bottom: 0 !important;" in master

    body = css.split("#view-settings .dp-settings-master-body", 1)[1].split("}", 1)[0]
    assert "flex: 1 1 auto;" in body
    assert "overflow: hidden;" in body

    scroll = css.split("#view-settings .dp-settings-scroll", 1)[1].split("}", 1)[0]
    assert "overflow-y: auto;" in scroll
    assert "overscroll-behavior: contain;" in scroll

    panels = css.split("#view-settings .dp-settings-panels", 1)[1].split("}", 1)[0]
    assert "padding: 12px 12px 16px;" in panels

    footer = css.split("#view-settings .dp-settings-master-footer", 1)[1].split("}", 1)[0]
    assert "flex: 0 0 auto;" in footer
    assert "border-top: 1px solid var(--dp-divider);" in footer

    assert '<section class="card dp-settings-master-card"' in runtime
    assert '<div class="card-header dp-settings-master-header">' in runtime
    assert '<div class="dp-settings-master-body">' in runtime
    assert '<div class="dp-settings-master-footer"' in runtime
    assert 'class="card dp-settings-card' in runtime
    assert '<section class="card dp-settings-header-card"' not in runtime
    assert '<section class="card dp-settings-footer"' not in runtime


def test_inherited_settings_shell_state_is_rejected_but_normal_content_height_is_reused():
    shell = read_static("ui-shell-structural.css")
    legacy = read_static("style.css")
    runtime = read_static("ui-settings-page.js")
    settings = read_static("ui-settings-page.css")

    # app.js still emits the inherited class until the monolith cleanup pass.
    # The clean Settings entry removes it synchronously before rendering any
    # Settings DOM. The only #content selector Settings owns mirrors Activity:
    # suppress outer scrolling while the page fills the shell-owned content box.
    assert "#content.settings-active" in legacy
    assert "body.dp-v11-structural #content.settings-active" in shell
    assert "classList.remove('settings-active')" in runtime
    assert "#content.settings-active" not in settings
    assert "body.dp-v11-structural #content:has(#view-settings.active)" in settings

    content_rule = settings.split("#content:has(#view-settings.active)", 1)[1].split("}", 1)[0]
    assert "overflow-y: hidden;" in content_rule
    for forbidden in ("#main", "#sidebar", "#topbar"):
        assert forbidden not in settings




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
    activity = overlay.index("/ui-activity-log-page.css?v=30")
    downloads = overlay.index("/ui-downloads-page.css?v=27")
    settings = overlay.index("/ui-settings-page.css?v=2")
    help_page = overlay.index("/ui-help-page.css?v=22")

    assert universal < stats < activity < downloads < settings < help_page
