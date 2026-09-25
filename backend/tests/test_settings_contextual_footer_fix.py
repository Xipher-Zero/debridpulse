from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
SETTINGS_PAGE = STATIC / "ui-settings-page.js"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_send_report_now_is_a_footer_action_of_the_notifications_context():
    page = source(SETTINGS_PAGE)

    # The master Settings tab lifecycle owns contextual action visibility.
    assert "root()?.querySelectorAll('[data-context-action]').forEach(button => {" in page
    assert "button.hidden = button.dataset.contextAction !== name;" in page

    # Send Report Now is emitted by the owner as a notifications-context footer
    # action, so it is shown and hidden by that one lifecycle with no relocation step.
    assert 'data-context-action="notifications" data-action="send-report"' in page
    assert 'data-context-action="notifications" data-action="test-discord"' in page
    assert "reportButton" not in page


def test_downloads_carries_no_footer_action_and_no_deferred_apply_contract():
    """DP 1.0.13: Downloads became a field-boundary persistence surface.

    Every control on it commits at its own boundary, so the footer offers it no
    Apply button and no unsaved hint -- and the Download Engine test is removed
    from the UI entirely rather than relocated or replaced."""
    page = source(SETTINGS_PAGE)

    assert "Test Download Engine" not in page
    assert "testDownloadEngine" not in page
    assert 'data-context-action="downloads"' not in page
    # The footer's deferred controls are hidden on a field-boundary tab; the
    # Apply infrastructure itself remains for the tabs that still need it.
    assert "const FIELD_BOUNDARY_TABS = new Set(['downloads', 'extraction'])" in page
    assert "[data-deferred-apply], .dp-settings-save-hint" in page
    assert 'data-action="save" data-deferred-apply' in page
