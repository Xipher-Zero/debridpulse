from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = STATIC / "ui-settings-notifications.js"
STYLE = STATIC / "ui-settings-notifications.css"
LOADER = STATIC / "ui-presentation-loader.js"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_discord_notifications_header_and_field_copy_are_locked():
    js = source(RUNTIME)

    assert "Configure notification identity, delivery destinations, and event alerts." in js
    expected = [
        ("Display Name", "Name shown as the sender of Discord notifications."),
        ("Avatar URL", "Image shown with Discord notifications. Paste a direct image URL or upload one."),
        ("Discord Webhook", "Primary Discord destination for enabled notifications."),
        (
            "Download Added Webhook",
            "Optional destination for new-download notifications. Leave blank to use the primary webhook.",
        ),
        (
            "Update Check Interval (Hours Between Checks)",
            "How often DebridPulse checks for a newer release.",
        ),
    ]
    for title, flavor in expected:
        assert title in js
        assert flavor in js


def test_discord_notification_event_copy_and_row_grouping_are_locked():
    js = source(RUNTIME)

    expected = [
        ("Download Added", "Send a notification when a new download is accepted."),
        ("Download Completed", "Send a notification when a download finishes successfully."),
        ("Download Error", "Send a notification when a download fails."),
        ("Extraction Result", "Send a notification when archive extraction completes or fails."),
        ("Update Available", "Send a notification when a newer DebridPulse release is detected."),
    ]
    for title, flavor in expected:
        assert title in js
        assert flavor in js

    assert "primaryToggleRow.append(addedToggle, completedToggle, errorToggle);" in js
    assert "secondaryToggleRow.append(extractToggle, updateToggle);" in js


def test_discord_notifications_reuse_existing_controls_and_secret_semantics():
    js = source(RUNTIME)

    for key in [
        "discord_username",
        "discord_avatar_url",
        "discord_webhook_url",
        "discord_webhook_added",
        "update_check_interval_hours",
        "discord_notify_added",
        "discord_notify_finished",
        "discord_notify_error",
        "discord_notify_extract",
        "discord_notify_update",
    ]:
        assert key in js

    assert 'data-clear-secret="${key}"' in js
    assert "Clear Stored Webhook" in js
    assert "Clear Stored Download Added Webhook" in js
    assert "dpNotificationsPolished" in js
    assert "MutationObserver" in js
    assert "mutation.type === 'childList'" in js
    assert "attributes: true" not in js


def test_discord_notifications_layout_uses_field_datum_and_inverted_pyramid():
    css = source(STYLE)

    assert '[data-panel="notifications"] .dp-settings-discord-card .dp-settings-field > .form-label' in css
    assert '[data-panel="notifications"] .dp-settings-discord-card .dp-settings-field > .form-hint' in css
    assert "inset-inline-start: 3px;" in css

    assert ".dp-settings-notifications-identity-row" in css
    assert "grid-template-columns: minmax(260px, .9fr) minmax(420px, 1.35fr) auto;" in css
    assert ".dp-settings-avatar-actions" in css
    assert "flex-direction: column;" in css
    assert "justify-self: end;" in css

    assert ".dp-settings-notifications-delivery-row" in css
    assert "width: min(92%, 1500px);" in css
    assert "grid-template-columns: minmax(320px, 1.45fr) minmax(320px, 1.45fr) minmax(190px, .55fr);" in css

    assert ".dp-settings-notifications-toggle-row--primary" in css
    assert "width: min(82%, 1360px);" in css
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in css
    assert ".dp-settings-notifications-toggle-row--secondary" in css
    assert "width: min(58%, 980px);" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css


def test_discord_notification_toggles_use_stacked_copy_with_centered_adjacent_toggle():
    css = source(STYLE)

    assert ".dp-settings-notifications-toggle-row > .dp-settings-toggle" in css
    assert "grid-template-columns: minmax(0, 1fr) auto;" in css
    assert "gap: 14px;" in css
    assert ".dp-settings-notifications-toggle-row .toggle-info" in css
    assert "flex-direction: column;" in css
    assert "align-items: flex-start;" in css
    assert ".dp-settings-notifications-toggle-row .toggle" in css
    assert "align-self: center;" in css
    assert "justify-self: end;" in css


def test_notifications_presentation_assets_load_after_settings_page():
    loader = source(LOADER)

    css_entry = "'/ui-settings-notifications.css?v=1'"
    js_entry = "'/ui-settings-notifications.js?v=1'"
    settings_entry = "'/ui-settings-page.js?v=4'"
    assert css_entry in loader
    assert js_entry in loader
    assert settings_entry in loader
    assert loader.index(js_entry) > loader.index(settings_entry)
