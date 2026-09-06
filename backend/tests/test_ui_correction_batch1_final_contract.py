from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def test_batch1_final_runtime_is_loaded_after_settings_runtimes():
    card_icons = (STATIC / "ui-settings-card-icons.js").read_text(encoding="utf-8")
    final = (STATIC / "ui-correction-batch1-final.js").read_text(encoding="utf-8")

    assert "ui-correction-batch1-final.js?v=1" in card_icons
    assert "DPUICorrectionBatch1Final" in card_icons
    assert "DPUICorrectionBatch1Final" in final


def test_archive_password_final_contract_is_latched_line_aware_and_append_ready():
    final = (STATIC / "ui-correction-batch1-final.js").read_text(encoding="utf-8")

    assert "Show all passwords" in final
    assert "Hide all passwords" in final
    assert "aria-pressed" in final
    assert "dpLatchedReveal" in final
    assert 'data-dp-lucide="eye"' in final
    assert 'data-dp-lucide="eye-off"' in final
    assert "Hold to reveal all archive passwords" not in final
    assert "pointerdown" not in final
    assert "pointerup" not in final
    assert "pointercancel" not in final
    assert "pointerleave" not in final
    assert "Add one password per line. Passwords stay hidden unless selected. Use the eye to show or hide all passwords." in final
    assert "if (!rows.length || rows[rows.length - 1].value !== '')" in final
    assert ".filter(Boolean)" in final
    assert "event.key === 'Enter'" in final
    assert "event.key === 'Backspace'" in final
    assert "event.altKey" in final
    assert "clipboardData" in final
    assert "type = archiveRevealAll || archiveActiveKey === row.key ? 'text' : 'password'" in final


def test_activity_log_final_contract_has_server_filters_ceiling_and_year_timestamp():
    final = (STATIC / "ui-correction-batch1-final.js").read_text(encoding="utf-8")

    for label in (
        "Last hour", "Last 12 hours", "Last day", "Last 3 days",
        "Last week", "Last 30 days", "Available history",
    ):
        assert label in final
    for value in ("'1h'", "'12h'", "'24h'", "'72h'", "'7d'", "'30d'", "'all'"):
        assert value in final

    assert "Timeframe" in final
    assert "Severity" in final
    assert "[['', 'All'], ['info', 'Info'], ['warn', 'Warning'], ['error', 'Error']]" in final
    assert "EVENT_LIMIT = 500" in final
    assert "SEARCH_DEBOUNCE_MS = 275" in final
    assert "generation !== eventGeneration" in final
    assert "params.set('timeframe'" in final
    assert "params.set('level'" in final
    assert "params.set('search'" in final
    assert "payload?.truncated === true" in final
    assert "Showing the latest ${EVENT_LIMIT} matching events" in final
    assert "year: 'numeric'" in final
    assert "page-size" not in final
    assert "pagination" not in final.lower()
