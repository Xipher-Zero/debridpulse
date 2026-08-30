from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
SETTINGS_PAGE = STATIC / "ui-settings-page.js"
NOTIFICATIONS = STATIC / "ui-settings-notifications.js"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_send_report_now_inherits_notifications_context_visibility():
    page = source(SETTINGS_PAGE)
    notifications = source(NOTIFICATIONS)

    # The master Settings tab lifecycle owns contextual action visibility.
    assert "root()?.querySelectorAll('[data-context-action]').forEach(button => {" in page
    assert "button.hidden = button.dataset.contextAction !== name;" in page

    # Send Report Now is moved into the footer after the base lifecycle has run,
    # so it must inherit the already-correct Notifications visibility immediately.
    assert "reportButton.dataset.contextAction = 'notifications';" in notifications
    assert "reportButton.hidden = testDiscord.hidden;" in notifications
    assert "testDiscord.insertAdjacentElement('afterend', reportButton);" in notifications


def test_downloads_context_action_uses_download_engine_language():
    page = source(SETTINGS_PAGE)
    notifications = source(NOTIFICATIONS)

    # Keep the existing aria2 validation action/endpoint contract while presenting
    # the user-facing abstraction used by the redesigned Downloads tab.
    assert 'data-context-action="downloads" data-action="test-aria2"' in page
    assert "button[data-context-action=\"downloads\"][data-action=\"test-aria2\"]" in notifications
    assert "testDownloadEngine.textContent = 'Test Download Engine';" in notifications
