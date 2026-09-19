from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = STATIC / "ui-settings-page.js"   # the one Settings owner emits this markup
STYLE = STATIC / "ui-settings-notifications.css"
LOADER = STATIC / "ui-presentation-loader.js"
SEND_ICON = STATIC / "icons" / "lucide" / "send.svg"


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
            "Set how often DebridPulse checks for a newer release. Enter 0 to disable update checks.",
        ),
    ]
    for title, flavor in expected:
        assert title in js
        assert flavor in js

    assert "type: 'number', min: 0, max: 168, step: 1," in js.split("update_check_interval_hours", 2)[2][:200]


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

    primary = js.split("dp-settings-notifications-toggle-row--primary", 1)[1].split("</div>", 1)[0]
    secondary = js.split("dp-settings-notifications-toggle-row--secondary", 1)[1].split("</div>", 1)[0]
    assert [k for k in ("discord_notify_added", "discord_notify_finished", "discord_notify_error") if k in primary] == [
        "discord_notify_added", "discord_notify_finished", "discord_notify_error"]
    assert [k for k in ("discord_notify_extract", "discord_notify_update") if k in secondary] == [
        "discord_notify_extract", "discord_notify_update"]




def test_discord_delivery_fields_share_control_datum_and_preserve_inverted_pyramid():
    css = source(STYLE)

    assert '[data-panel="notifications"] .dp-settings-field > .form-label' in css
    assert '[data-panel="notifications"] .dp-settings-field > .form-hint' in css
    assert "inset-inline-start: 3px;" in css

    assert ".dp-settings-notifications-identity-row" in css
    assert "grid-template-columns: minmax(260px, .9fr) minmax(420px, 1.35fr) auto;" in css
    assert ".dp-settings-avatar-actions" in css
    assert "flex-direction: column;" in css
    assert "justify-self: end;" in css

    assert ".dp-settings-notifications-delivery-row" in css
    assert "width: min(92%, 1500px);" in css
    assert "grid-template-columns: minmax(320px, 1.45fr) minmax(320px, 1.45fr) minmax(190px, .55fr);" in css
    assert ".dp-settings-notifications-delivery-row > .dp-settings-field > .form-label" in css
    assert "min-height: 30px;" in css
    assert "align-items: flex-end;" in css

    assert ".dp-settings-notifications-toggle-row--primary" in css
    assert "width: min(82%, 1360px);" in css
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in css
    assert ".dp-settings-notifications-toggle-row--secondary" in css
    assert "width: min(58%, 980px);" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css


def test_discord_notification_toggles_keep_item_spacing_but_tighten_switch_association():
    css = source(STYLE)

    assert ".dp-settings-notifications-toggle-row" in css
    assert "gap: 24px;" in css
    assert ".dp-settings-notifications-toggle-row > .dp-settings-toggle" in css
    assert "display: flex;" in css
    assert "justify-content: center;" in css
    assert "gap: 10px;" in css
    assert ".dp-settings-notifications-toggle-row .toggle-info" in css
    assert "flex-direction: column;" in css
    assert "align-items: flex-start;" in css
    assert "max-width: 330px;" in css
    assert ".dp-settings-notifications-toggle-row .toggle" in css
    assert "align-self: center;" in css
    assert "margin-left: 0;" in css


def test_statistics_reporting_copy_and_single_row_layout_are_locked():
    js = source(RUNTIME)
    css = source(STYLE)

    expected = [
        "Statistics Reporting",
        "Configure where reports are sent, how often they are delivered, and how much activity they summarize.",
        "Reporting Webhook",
        "Optional destination for statistics reports. Leave blank to use the primary Discord webhook.",
        "Automatic Report Interval (Hours Between Reports)",
        "Set how often DebridPulse sends statistics reports. Enter 0 to disable automatic reports.",
        "Report Window",
        "Choose how much recent activity each statistics report includes.",
        "Clear Stored Reporting Webhook",
    ]
    for text in expected:
        assert text in js

    row = js.split("dp-settings-statistics-reporting-row", 1)[1]
    assert row.index("stats_report_webhook_url") < row.index("stats_report_interval_hours") < row.index("stats_report_window_hours")
    assert "type: 'number', min: 0, max: 168, step: 1," in row

    assert ".dp-settings-statistics-reporting-row" in css
    assert "width: min(88%, 1450px);" in css
    assert "grid-template-columns: minmax(430px, 2fr) minmax(260px, .9fr) minmax(220px, .7fr);" in css
    assert ".dp-settings-statistics-reporting-row > .dp-settings-field > .form-label" in css


def test_notifications_footer_actions_are_contextual_and_semantically_distinct():
    js = source(RUNTIME)
    icon = source(SEND_ICON)

    assert "Changes remain unsaved until Apply Settings is selected." in js
    footer = js[js.index('<div class="dp-settings-context-actions">'):js.index('data-action="save"')]
    # Both notification actions are footer buttons of the notifications context,
    # in this order, emitted directly by the Settings owner (nothing relocates them).
    assert footer.index('data-action="test-discord"') < footer.index('data-action="send-report"')
    assert footer.count('data-context-action="notifications"') == 2
    assert "/icons/lucide/flask-conical.svg" in footer and "<span>Test Discord</span>" in footer
    assert "/icons/lucide/send.svg" in footer and "<span>Send Report Now</span>" in footer
    assert "-draft" not in js
    assert "<svg" in icon
    assert "<path" in icon
    assert 'stroke="#B866F5"' in icon
    assert 'stroke="currentColor"' not in icon
    assert "data:image" not in icon


def test_notification_context_actions_use_current_draft_without_persisting_it():
    js = source(RUNTIME)

    assert "'/settings/validate-discord'" in js
    assert "username: valueOf('discord_username')" in js
    assert "avatar_url: valueOf('discord_avatar_url')" in js
    assert "clear_webhook: clears.has('discord_webhook_url')" in js

    assert "'/settings/send-stats-report'" in js
    assert "stats_report_webhook_url: valueOf('stats_report_webhook_url')" in js
    assert "clear_stats_report_webhook: clearSecrets().includes('stats_report_webhook_url')" in js
    assert "discord_webhook_url: valueOf('discord_webhook_url')" in js
    assert "clear_discord_webhook: clearSecrets().includes('discord_webhook_url')" in js
    assert "hours = Math.max(1, intOf('stats_report_window_hours', 24))" in js

    # A contextual send must not rerender the Settings page and discard unsaved draft fields.
    send_function = js[js.index("async function sendReport("):js.index("async function", js.index("async function sendReport(") + 10)]
    assert "render" not in send_function
