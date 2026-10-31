"""Final-state contract for the shell version datum and signal field."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
OVERLAY = STATIC / "style.css"
SIGNAL = STATIC / "ui-shell-signal-field.css"
WAVE = STATIC / "icons" / "dp" / "sidebar-wave-accent.svg"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_shell_signal_field_loads_after_cross_page_visual_accents() -> None:
    overlay = read(OVERLAY)
    visual = "@import url('/ui-visual-accents.css?v=22');"
    signal = "@import url('/ui-shell-signal-field.css?v=21');"
    assert visual in overlay
    assert signal in overlay
    assert overlay.index(visual) < overlay.index(signal)


def test_global_version_datum_is_text_only_without_chip_surface() -> None:
    css = read(SIGNAL)
    selector = "#content > #sidebar-version.dp-app-version"
    assert selector in css
    segment = css[css.index(selector):].split("}", 1)[0]
    for declaration in (
        "padding: 0 !important",
        "border: 0 !important",
        "border-radius: 0 !important",
        "outline: 0 !important",
        "background: transparent !important",
        "box-shadow: none !important",
    ):
        assert declaration in segment
    assert "body:not(.light) #content > #sidebar-version.dp-app-version" in css
    assert "body.light #content > #sidebar-version.dp-app-version" in css


def test_the_one_version_datum_follows_the_active_master_surface_never_the_window() -> None:
    # 1.0.14: the one label sits in #content after the views, as wide as a
    # view and right-aligned with it, just below the active page's master
    # surface; it takes no height and no part of the content's overflow.
    index = read(STATIC / "index.html")
    assert index.count('id="sidebar-version"') == 1
    content = index[index.index('<div id="content"'):index.index('<!-- Modal -->')]
    assert '<div class="logo-ver dp-app-version" id="sidebar-version" aria-label="DebridPulse version">v…</div>' in content
    brand = read(STATIC / "ui-shell-brand.css")
    rule = brand[brand.index("#content > #sidebar-version.dp-app-version {"):].split("}", 1)[0]
    for declaration in ("display: block;", "width: min(100%, var(--dp-content-max-width));", "height: 0;",
                        "margin: 0 auto !important;", "contain: layout;", "transform: translateY(8px);",
                        "text-align: right;", "pointer-events: none;"):
        assert declaration in rule, declaration
    for retired in ("body > #sidebar-version", "position: fixed", "bottom: 7px", "z-index: 520"):
        assert retired not in brand, retired


def test_sidebar_signal_field_keeps_accepted_vector_geometry() -> None:
    css = read(SIGNAL)
    wave = read(WAVE)
    # 1.0.14: the file is drawn inline (<dp-signal-field>, one in the sidebar
    # and one in the dock), so the palette in force colours it; no stylesheet
    # draws it as an image, and the former pseudo-elements are gone.
    index = read(STATIC / "index.html")
    assert index.count('<dp-signal-field class="dp-signal-field" aria-hidden="true"></dp-signal-field>') == 2
    assert '<aside id="sidebar">\n  <dp-signal-field' in index
    assert '<nav id="shell-dock" class="dp-shell-dock" aria-label="Pages"><dp-signal-field' in index
    for name in ("ui-shell-signal-field.css", "ui-shell-structural.css", "ui-shell-mobile.css"):
        sheet = read(STATIC / name)
        assert "url('/icons/dp/sidebar-wave-accent.svg" not in sheet and "#sidebar::before" not in sheet and "dock::before" not in sheet, name
    assert "#sidebar > .dp-signal-field {" in css and "background: none !important" in css
    assert "height: 300px !important" in css
    assert "opacity: .72 !important" in css
    assert "body.light #sidebar > .dp-signal-field" in css
    assert "opacity: .34 !important" in css
    # Every colour of the artwork is a tone the stylesheet colours, and nothing else.
    tones = set(re.findall(r'class="(dp-signal-[a-z-]+)"', wave))
    assert tones == {"dp-signal-purple", "dp-signal-purple-bright", "dp-signal-purple-deep",
                     "dp-signal-blue", "dp-signal-blue-bright", "dp-signal-blue-deep"}
    assert tones == set(re.findall(r"^\.(dp-signal-[a-z-]+) \{", css, re.M))
    assert "mask-image:" in css
    assert wave.count("<path ") >= 7
    assert wave.count("<circle ") >= 32
    assert 'linearGradient id="purple"' in wave
    assert 'linearGradient id="blue"' in wave
    assert 'filter id="nodeGlow"' in wave
