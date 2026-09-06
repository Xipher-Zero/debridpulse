from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def test_batch1_final_and_p4_repair_runtimes_are_loaded_in_order():
    card_icons = (STATIC / "ui-settings-card-icons.js").read_text(encoding="utf-8")
    final = (STATIC / "ui-correction-batch1-final.js").read_text(encoding="utf-8")
    repair = (STATIC / "ui-correction-p4-repair.js").read_text(encoding="utf-8")

    assert "ui-correction-batch1-final.js?v=1" in card_icons
    assert "DPUICorrectionBatch1Final" in card_icons
    assert "DPUICorrectionBatch1Final" in final
    assert "ui-correction-p4-repair.js?v=1" in card_icons
    assert "finalScript.addEventListener('load', loadRepair" in card_icons
    assert "DPUICorrectionP4Repair" in repair


def test_archive_password_final_contract_is_click_toggle_line_aware_and_append_ready():
    final = (STATIC / "ui-correction-batch1-final.js").read_text(encoding="utf-8")
    repair = (STATIC / "ui-correction-p4-repair.js").read_text(encoding="utf-8")

    assert "Show all passwords" in final
    assert "Hide all passwords" in final
    assert "aria-pressed" in final
    assert "dpLatchedReveal" in final
    assert "Hold to reveal all archive passwords" not in final
    assert "pointerdown" not in final
    assert "pointerup" not in final
    assert "pointercancel" not in final
    assert "pointerleave" not in final
    assert "Select a row to edit it; use the eye to show or hide all passwords." in final
    assert "if (!rows.length || rows[rows.length - 1].value !== '')" in final
    assert ".filter(Boolean)" in final
    assert "event.key === 'Escape'" in final
    assert "event.key === 'Enter'" in final
    assert "event.key === 'Backspace'" in final
    assert "event.altKey" in final
    assert "clipboardData" in final

    # The correction must render the designed ghost button, not merely expose an
    # accessible name on an icon-only control.
    assert "dp-settings-password-eye--ghost" in repair
    assert "dp-settings-password-eye-label" in repair
    assert "visible = revealed ? 'Hide all' : 'Show all'" in repair
    assert "stroke', 'currentColor'" in repair
    assert "button.querySelector('img')" in repair
    assert "background: transparent !important" in repair
    assert "border: 1px solid" in repair
    assert "box-shadow: var(--dp-focus-ring)" in repair
    assert "padding: 8px 11px 50px !important" in repair
    assert "max-height: none !important" in repair
    assert "overflow: visible !important" in repair


def test_activity_log_final_contract_matches_reviewed_controls_and_server_filtering():
    final = (STATIC / "ui-correction-batch1-final.js").read_text(encoding="utf-8")
    repair = (STATIC / "ui-correction-p4-repair.js").read_text(encoding="utf-8")

    for label in (
        "Last hour", "Last 12 hours", "Last day", "Last 3 days",
        "Last week", "Last 30 days", "Available history",
    ):
        assert label in repair
    for value in ("['1h'", "['12h'", "['24h'", "['72h'", "['7d'", "['30d'", "['all'"):
        assert value in repair

    assert "['', 'All']" in repair
    assert "['info', 'Info']" in repair
    assert "['warning', 'Warning']" in repair
    assert "['error', 'Error']" in repair
    assert "first.textContent = 'Time'" in repair
    assert "second.textContent = 'Window'" in repair
    assert "label.textContent = 'Severity'" in repair
    assert "select._dpDropdownShell" in repair
    assert "field.appendChild(shell)" in repair
    assert "display: flex !important" in repair
    assert "align-items: center !important" in repair

    # Existing functional behavior remains owned by the final runtime.
    assert "Reset Filters" in final
    assert "reset.hidden = !activityFiltersActive" in final
    assert "EVENT_LIMIT = 500" in final
    assert "SEARCH_DEBOUNCE_MS = 250" in final
    assert "generation !== eventGeneration" in final
    assert "params.set('include_meta', 'true')" in final
    assert "params.set('timeframe'" in final
    assert "params.set('level'" in final
    assert "params.set('search'" in final
    assert "payload?.truncated === true" in final
    assert "Showing the latest ${EVENT_LIMIT} matching events" in final
    assert "year: 'numeric'" in final
    assert "No events yet." in final
    assert "No events match your filters." in final
    assert "pagination" not in final.lower()
