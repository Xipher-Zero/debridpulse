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


def test_downloads_context_action_uses_download_engine_language():
    page = source(SETTINGS_PAGE)

    # Keep the existing aria2 validation action/endpoint contract while presenting
    # the user-facing abstraction used by the redesigned Downloads tab.
    assert 'data-context-action="downloads" data-action="test-aria2">Test Download Engine</button>' in page
    assert "testDownloadEngine" not in page
