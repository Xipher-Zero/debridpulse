"""Final-state contracts for shared visual accents."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_visual_accents_load_after_shared_transfer_semantics() -> None:
    entry = read("style-v11.css")
    assert entry.index("/ui-transfer-contract.css?v=31") < entry.index("/ui-visual-accents.css?v=21")


def test_status_badges_are_lucide_only_and_keep_semantic_glow() -> None:
    css = read("ui-visual-accents.css")
    assert ".badge[data-dp-status]::before" in css
    assert "content: none !important" in css
    assert "var(--dp-badge-color) 72%" in css
    assert "var(--dp-badge-color) 32%" in css


def test_terminal_error_progress_keeps_truthful_geometry_and_salience() -> None:
    css = read("ui-visual-accents.css")
    assert ".prog.dp-terminal-error-rail" in css
    assert "border-radius: 999px !important" in css
    assert "width: 16px" in css
    assert "height: 9px" in css
    assert "transform: translateY(-50%)" in css
    assert ".prog-fill.dp-terminal-error-progress" in css
    assert "var(--dp-state-error) 88%" in css


def test_details_scrollbar_suppresses_native_increment_buttons() -> None:
    css = read("ui-visual-accents.css")
    assert "@supports selector(::-webkit-scrollbar)" in css
    assert "scrollbar-color: auto" in css
    assert "::-webkit-scrollbar-button:vertical:decrement" in css
    assert "::-webkit-scrollbar-button:vertical:increment" in css
    assert "display: none !important" in css


def test_activity_and_details_event_points_share_semantic_glow() -> None:
    css = read("ui-visual-accents.css")
    for fragment in (
        ".dp-activity-level.info",
        "#modal .dp-detail-events-list .elevel.info",
        ".dp-activity-level.warn",
        "#modal .dp-detail-events-list .elevel.warn",
        ".dp-activity-level.error",
        "#modal .dp-detail-events-list .elevel.error",
        "var(--dp-state-active)",
        "var(--dp-state-caution)",
        "var(--dp-state-error)",
    ):
        assert fragment in css
    assert css.count("var(--dp-event-point-color) 92%") >= 2
    assert css.count("var(--dp-event-point-color) 88%") >= 2


def test_theme_elevation_keeps_dark_and_light_provider_parity() -> None:
    css = read("ui-visual-accents.css")
    assert "--dp-dark-surface-shadow:" in css
    assert "--dp-panel-shadow:" in css
    assert "rgba(84, 38, 131, .18)" in css
    assert "rgba(167, 139, 250, .06)" in css
    assert "body.dp-v11-structural:not(.light) #sidebar .sidebar-footer" in css
    assert "body.light.dp-v11-structural #sidebar .sidebar-footer" in css
    assert "var(--dp-shadow-raised)" in css
