"""The locked Notifications Settings contract.

Both cards are ordinary operational Settings cards built from the canonical
primitives: the shared header rail, the shared plain-text status vocabulary,
the shared Test treatment, the shared header Enable, the standard Title + hint
+ field grammar, the shared two-state destructive clear, the one canonical
disclosure and the one compact tuning-cell collection.

These cases hold that -- and, just as importantly, that none of it was
re-invented locally. Where a primitive is shared, the assertion is that the
Notifications surface RENDERS it rather than that it describes a lookalike.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = STATIC / "ui-settings-page.js"       # the one Settings markup owner
STYLE = STATIC / "ui-settings-notifications.css"
SHARED = STATIC / "ui-settings-page.css"
LANGUAGE = STATIC / "ui-universal-language.css"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def block(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end, text.index(start))]


def panel() -> str:
    return block(source(RUNTIME), "function notificationsPanel(s) {",
                 "/* Converge the Notifications surface")


def discord_card() -> str:
    return block(panel(), "const discord = card(", "const reports = card(")


def reports_card() -> str:
    return block(panel(), "const reports = card(", "return discord + reports;")


# ── Header rails ─────────────────────────────────────────────────────────────

def test_both_cards_render_the_shared_rail_in_the_locked_order():
    """Status -> Test -> Enable, from the card primitive's own rail slots."""
    for card in (discord_card(), reports_card()):
        assert card.index("headerStatus:") < card.index("headerAction:") < card.index("action:")
        assert "notificationStatus(" in card
        assert "providerTestAction(" in card
        assert "headerEnableToggle(" in card


def test_each_relocated_action_and_enable_control_has_exactly_one_owner():
    runtime = source(RUNTIME)
    assert runtime.count("providerTestAction('test-discord')") == 1
    assert runtime.count("providerTestAction('send-report')") == 1
    assert runtime.count("headerEnableToggle('discord_notifications_enabled'") == 1
    assert runtime.count("headerEnableToggle('stats_reporting_enabled'") == 1
    # The rail slot, the status node and the Enable control are the shared
    # ones; this page declares no variant of any of them.
    for invented in ("dp-settings-notifications-status", "dp-settings-notifications-test",
                     "dp-settings-notifications-enable", "dp-settings-notifications-header-spacer"):
        assert invented not in runtime, invented
        assert invented not in source(STYLE), invented


def test_the_status_vocabulary_is_the_shared_three_state_one():
    status = block(source(RUNTIME), "function notificationStatus(", "/* The three stored webhooks")
    assert "{text: 'Unconfigured', tone: 'error'}" in status
    assert "{text: 'Configured', tone: 'warning'}" in status
    assert "{text: 'Verified', tone: 'success'}" in status
    # Participation is a separate fact, so the report is never blanked by it.
    assert "enabled" not in status
    # It is a projection of canonical state; nothing else may feed it.
    for card, prefix in ((discord_card(), "discord_notifications"),
                         (reports_card(), "stats_reporting")):
        assert f"notificationStatus(s.{prefix}_configured, s.{prefix}_verified)" in card


# ── Discord identity ─────────────────────────────────────────────────────────

def test_display_name_and_avatar_url_use_the_standard_field_grammar():
    card = discord_card()
    assert "input('discord_username', 'Display Name'" in card
    assert "Name shown as the sender of Discord notifications." in card
    assert "input('discord_avatar_url', 'Avatar URL'" in card
    assert "Image shown with Discord notifications. Paste a direct image URL or upload one." in card
    # No bespoke identity layout: both are the one field composer.
    assert "<label class=\"form-label\"" not in card


def test_upload_avatar_is_inside_the_avatar_field_and_clear_avatar_is_beside_it():
    runtime = source(RUNTIME)
    card = discord_card()
    # Inside the field, through the ONE embedded-action primitive.
    assert "embedAction: AVATAR_UPLOAD" in card
    assert "fieldRow(" in card
    assert "AVATAR_CLEAR(" in card
    # One markup owner each; the only other mentions are the action router and
    # the convergence read, neither of which renders a control.
    assert runtime.count(">Upload Avatar<") == 1
    assert runtime.count(">Clear Avatar<") == 1
    # Keyboard reachable: a real button opening the hidden file input, not a
    # <label> wrapper that no tab stop can reach.
    upload = block(runtime, "const AVATAR_UPLOAD", "const AVATAR_CLEAR")
    assert 'type="button"' in upload and 'data-action="upload-avatar"' in upload
    assert "dp-settings-file-button" not in runtime
    # Room for the text is structural: the control and the action are siblings
    # inside one compound field, so entered text cannot run beneath the button.
    assert ".dp-action-field {" in source(LANGUAGE)
    assert "flex: 1 1 auto" in block(source(LANGUAGE), ".dp-action-field > :is(.input, .dp-field) {", "}")


def test_clear_avatar_is_the_shared_two_state_destructive_action():
    clear = block(source(RUNTIME), "const AVATAR_CLEAR", "const avatarPreview")
    assert "btn btn-danger btn-sm" in clear
    assert "configured ? '' : ' disabled'" in clear
    assert "aria-label=" in clear
    # It never disappears, so the row cannot reflow as a value comes and goes.
    assert "hidden" not in clear


# ── Discord destinations ─────────────────────────────────────────────────────

def test_both_webhooks_use_the_standard_field_grammar_with_one_clear_each():
    runtime = source(RUNTIME)
    card = discord_card()
    assert "webhookField('discord_webhook_url'" in card
    assert "webhookField('discord_webhook_added'" in card

    composer = block(runtime, "function webhookField(key, configured)", "/* Upload Avatar belongs")
    assert "input(key, spec.label, ''" in composer
    assert "hint: spec.hint" in composer
    assert "fieldRow(" in composer and "WEBHOOK_CLEAR(key" in composer

    controls = block(runtime, "const WEBHOOK_CONTROLS = Object.freeze({", "const webhookPlaceholder")
    assert "'Discord Webhook'" in controls
    assert "Primary Discord destination for enabled notifications." in controls
    assert "'Download Added Webhook'" in controls
    assert "Optional destination for new-download notifications. Leave blank to use the primary webhook." in controls
    assert "'Reporting Webhook'" in controls
    assert "Optional destination for statistics reports. Leave blank to use the primary Discord webhook." in controls
    # One declaration, three rows, one clear action shape.
    assert runtime.count("const WEBHOOK_CLEAR") == 1
    assert runtime.count(">Clear Webhook<") == 1
    assert runtime.count("WEBHOOK_CLEAR(key") == 1


def test_the_clear_on_apply_checkbox_semantics_are_gone_entirely():
    runtime = source(RUNTIME)
    for retired in (
        "function secretField(",
        "data-clear-secret",
        "clearSecrets(",
        "dp-settings-clear-secret",
        "Clear Stored Webhook",
        "Clear Stored Reporting Webhook",
        "Clear Stored Download Added Webhook",
        "when Settings are applied",
        "clear_webhook",
        "clear_stats_report_webhook",
        "clear_discord_webhook",
    ):
        assert retired not in runtime, retired
    for sheet in (STYLE, SHARED):
        assert "dp-settings-clear-secret" not in source(sheet)


def test_blanking_a_redacted_webhook_field_is_never_a_clear():
    """A stored secret's accepted presentation is blank, so its baseline is
    blank: blanking it is not a change and commits nothing. Erasing one is an
    explicit action carrying only the removal."""
    runtime = source(RUNTIME)
    document_scope = block(runtime, "persistence.defineScope('settings-document'",
                           "/* The ONE whole-settings write.")
    assert "if (declared.secret) return '';" in document_scope

    clear = block(runtime, "async function clearWebhook(button)", "async function runBackup")
    assert "window.DPSettingsModal.confirm({" in clear
    assert "if (!confirmed) return;" in clear
    assert "await window.DPSettingsPersistence.settle(root());" in clear
    assert "await writeSettingsDocument({}, [key]);" in clear
    assert clear.index("if (!confirmed) return;") < clear.index("writeSettingsDocument")
    # Convergence is the last word: releasing the busy state must not resurrect
    # an action that now has nothing to act on.
    assert clear.index("setBusy(button, false);") < clear.index("paintNotifications();")


# ── The disclosure and its compact cells ─────────────────────────────────────

def test_the_disclosure_is_named_exactly_and_uses_the_one_disclosure_control():
    runtime = source(RUNTIME)
    card = discord_card()
    assert "disclosureSection('Notification Events & Delivery Options', 'notifications-events'" in card
    assert "Additional Options" not in runtime

    composer = block(runtime, "function disclosureSection(title, key, body, expanded = false)",
                     "  // Provider card:")
    assert "settingsDisclosure(bodyId, open, title, persistKey)" in composer
    assert "disclosureOpen(persistKey, expanded)" in composer
    assert 'aria-controls' not in composer          # the shared control owns ARIA
    # Presentation only: the choice is remembered by the disclosure owner and
    # written nowhere.
    assert "request(" not in composer and "commitAttributes" not in composer
    # The destination row above is a separate block, so opening this cannot
    # resize it.
    assert card.index("dp-settings-notifications-delivery-row") < card.index("disclosureSection(")


def test_every_current_event_toggle_moved_into_the_compact_cells():
    """The inventory comes from the source, not from a screenshot: exactly the
    five event settings the delivery code actually reads."""
    card = discord_card()
    cells = block(card, "disclosureSection(", "    `, {")
    expected = [
        ("discord_notify_added", "Download Added", "Send a notification when a new download is accepted."),
        ("discord_notify_finished", "Download Completed", "Send a notification when a download finishes successfully."),
        ("discord_notify_error", "Download Error", "Send a notification when a download fails."),
        ("discord_notify_extract", "Extraction Result", "Send a notification when archive extraction completes or fails."),
        ("discord_notify_update", "Update Available", "Send a notification when a newer DebridPulse release is detected."),
    ]
    assert "tuningCells(" in cells
    for key, title, flavor in expected:
        assert f"tuningToggle('{key}', '{title}'" in cells, key
        assert flavor in cells, key
    assert cells.count("tuningToggle(") == len(expected)
    # Six cells: the five events and the update interval, one collection.
    assert cells.count("tuningToggle(") + cells.count("input(") == 6
    # Nothing invented and nothing dropped.
    assert source(RUNTIME).count("dp-settings-notifications-toggle-row") == 0


def test_the_event_settings_the_delivery_code_reads_are_exactly_those_five():
    """The screenshot is not the inventory; the runtime is. If delivery grows a
    new event setting, this case fails until the disclosure carries it."""
    observability = (ROOT / "backend" / "application" / "observability.py").read_text(encoding="utf-8")
    scheduler = (ROOT / "backend" / "core" / "scheduler.py").read_text(encoding="utf-8")
    notifications = (ROOT / "backend" / "services" / "notifications.py").read_text(encoding="utf-8")
    config = (ROOT / "backend" / "core" / "config.py").read_text(encoding="utf-8")

    declared = {line.split(":")[0].strip() for line in config.splitlines()
                if line.strip().startswith("discord_notify_")}
    assert declared == {"discord_notify_added", "discord_notify_finished", "discord_notify_error",
                        "discord_notify_extract", "discord_notify_update"}
    card = discord_card()
    for setting in declared:
        assert setting in card, setting
    # And each one is actually consulted by a delivery path.
    consulted = observability + scheduler + notifications
    for setting in declared:
        assert setting in consulted, setting


def test_update_check_interval_moved_and_states_its_unit_inside_the_field():
    card = discord_card()
    cells = block(card, "disclosureSection(", "    `, {")
    assert "input('update_check_interval_hours', 'Update Check Interval'" in cells
    assert "Update Check Interval (Hours Between Checks)" not in source(RUNTIME)
    assert "type: 'number', min: 0, max: 168, step: 1," in cells
    assert "Set how often DebridPulse checks for a newer release. Enter 0 to disable update checks." in cells
    assert "embedAction: fieldUnit('hours')" in cells
    # The delivery row no longer holds it.
    delivery = block(card, "dp-settings-notifications-delivery-row", "disclosureSection(")
    assert "update_check_interval_hours" not in delivery


# ── Statistics Reporting ─────────────────────────────────────────────────────

def test_statistics_reporting_is_three_standard_controls_and_no_disclosure():
    card = reports_card()
    assert "Configure where reports are sent, how often they are delivered, and how much activity they summarize." in card
    assert "webhookField('stats_report_webhook_url'" in card
    assert "input('stats_report_interval_hours', 'Automatic Report Interval'" in card
    assert "Automatic Report Interval (Hours Between Reports)" not in source(RUNTIME)
    assert "Set how often DebridPulse sends statistics reports. Enter 0 to disable automatic reports." in card
    assert "embedAction: fieldUnit('hours')" in card
    assert "selectField('stats_report_window_hours', 'Report Window'" in card
    assert "Choose how much recent activity each statistics report includes." in card
    for window in ("[24, '24 hours']", "[168, '7 days']", "[720, '30 days']", "[8760, '1 year']"):
        assert window in card, window
    assert "disclosureSection(" not in card

    order = [card.index(k) for k in ("stats_report_webhook_url", "stats_report_interval_hours",
                                     "stats_report_window_hours")]
    assert order == sorted(order)


# ── Geometry that IS the contract ────────────────────────────────────────────

def test_a_clear_action_shares_its_own_field_control_row():
    """The action is centred against the CONTROL, not against the whole stack,
    and it keeps its track in both states -- so the row does not reflow when a
    stored value appears or disappears."""
    shared = source(SHARED)
    row = block(shared, "#view-settings .dp-settings-field-row {", "}")
    assert "display: grid" in row
    assert "grid-template-columns: minmax(0, 1fr) auto;" in row

    assert "#view-settings .dp-settings-field-row > .dp-settings-field {\n  display: contents;\n}" in shared
    action = block(shared, "#view-settings .dp-settings-field-row > :not(.dp-settings-field) {", "}")
    assert "grid-column: 2" in action and "grid-row: 2" in action
    assert "align-self: center" in action
    for banned in ("position: absolute", "transform", "top:"):
        assert banned not in action, banned


def test_the_in_field_unit_is_a_reading_not_a_control():
    unit = block(source(SHARED), "#view-settings .dp-settings-field-unit {", "}")
    assert "pointer-events: none" in unit
    assert "user-select: none" in unit
    for banned in ("position: absolute", "transform"):
        assert banned not in unit, banned
    # The unit is aria-hidden and untabbable because the field's own label
    # already names the setting.
    assert 'aria-hidden="true"' in block(source(RUNTIME), "function fieldUnit(text)", "const CONFIGURED_SECRET_MASK")


def test_this_page_declares_no_lookalike_of_a_shared_primitive():
    css = source(STYLE)
    # The rail, the status tones, the field material, the compound field, the
    # disclosure chip, the destructive button and the tuning cells all have
    # their own owners. This sheet places rows; it re-materialises nothing.
    for owned in (
        "dp-settings-provider-config-status[data-tone",
        ".dp-settings-disclosure {",
        ".dp-action-field {",
        ".dp-settings-tuning-grid {",
        "#view-settings .dp-settings-field-row {",
        "#view-settings .dp-settings-field-unit {",
        "display: contents",
        "btn-danger",
        "box-shadow",
    ):
        assert owned not in css, owned
    # The rail may be RE-PLACED here when the header stops being three regions
    # wide, but it is never re-materialised.
    rail = [chunk for chunk in css.split("}") if "dp-settings-card-header-controls" in chunk]
    for chunk in rail:
        for material in ("display:", "flex-wrap", "align-items", "gap:"):
            assert material not in chunk, material
    # The datum itself has one owner (ui-settings-form-layout.css).
    assert "inset-inline-start" not in css
