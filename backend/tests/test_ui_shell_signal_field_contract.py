"""Final-state contract for the shell version datum and signal field."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
OVERLAY = STATIC / "style-v11.css"
SIGNAL = STATIC / "ui-shell-signal-field.css"
WAVE = STATIC / "icons" / "dp" / "sidebar-wave-accent.svg"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_shell_signal_field_loads_after_cross_page_visual_accents() -> None:
    overlay = read(OVERLAY)
    visual = "@import url('/ui-visual-accents.css?v=21');"
    signal = "@import url('/ui-shell-signal-field.css?v=20');"
    assert visual in overlay
    assert signal in overlay
    assert overlay.index(visual) < overlay.index(signal)


def test_global_version_datum_is_text_only_without_chip_surface() -> None:
    css = read(SIGNAL)
    selector = "body.dp-v11-structural > #sidebar-version.dp-app-version"
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
    assert "body.dp-v11-structural:not(.light) > #sidebar-version.dp-app-version" in css
    assert "body.light.dp-v11-structural > #sidebar-version.dp-app-version" in css


def test_sidebar_signal_field_keeps_accepted_vector_geometry() -> None:
    css = read(SIGNAL)
    wave = read(WAVE)
    assert "url('/icons/dp/sidebar-wave-accent.svg?v=3')" in css
    assert "height: 300px !important" in css
    assert "opacity: .72 !important" in css
    assert "body.light.dp-v11-structural #sidebar::before" in css
    assert "opacity: .34 !important" in css
    assert "mask-image:" in css
    assert wave.count("<path ") >= 7
    assert wave.count("<circle ") >= 32
    assert 'linearGradient id="purple"' in wave
    assert 'linearGradient id="blue"' in wave
    assert 'filter id="nodeGlow"' in wave
