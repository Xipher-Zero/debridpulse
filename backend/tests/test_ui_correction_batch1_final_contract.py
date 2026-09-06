from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def test_batch1_final_runtime_is_loaded_after_settings_runtimes():
    card_icons = (STATIC / "ui-settings-card-icons.js").read_text(encoding="utf-8")
    final = (STATIC / "ui-correction-batch1-final.js").read_text(encoding="utf-8")

    assert "ui-correction-batch1-final.js?v=1" in card_icons
    assert "DPUICorrectionBatch1Final" in card_icons
    assert "DPUICorrectionBatch1Final" in final


def test_archive_password_final_contract_is_click_toggle_line_aware_and_append_ready():
    final = (STATIC / "ui-correction-batch1-final.js").read_text(encoding="utf-8")

    assert "Show all passwords" in final
    assert "Hide all passwords" in final
    assert "aria-pressed" in final
    assert "dpLatchedReveal" in final
    assert "/icons/lucide/${icon}.svg" in final
    assert (STATIC / "icons" / "lucide" / "eye.svg").is_file()
    assert (STATIC / "icons" / "lucide" / "eye-off.svg").is_file()
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
    assert "max-height: none !important" in final
    assert "overflow: visible !important" in final


def test_activity_log_final_contract_matches_reviewed_controls_and_server_filtering():
    final = (STATIC / "ui-correction-batch1-final.js").read_text(encoding="utf-8")

    for label in ("All time", "Last hour", "Last 24 hours", "Last 7 days", "Last 30 days"):
        assert label in final
    for value in ("['all'", "['1h'", "['24h'", "['7d'", "['30d'"):
        assert value in final
    assert "Last 12 hours" not in final
    assert "Last 3 days" not in final
    assert "Available history" not in final

    assert "Time window" in final
    assert "Severity" in final
    assert "All levels" in final
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
