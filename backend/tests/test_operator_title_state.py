from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def test_operator_title_extension_loads_after_core_app():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    core = '<script src="/app.js?v=15" defer></script>'
    operator = '<script src="/operator-title.js?v=23" defer></script>'

    assert core in html
    assert operator in html
    assert html.index(core) < html.index(operator)


def test_operator_title_uses_authoritative_logical_download_phase():
    source = (STATIC / "operator-title.js").read_text(encoding="utf-8")

    assert "byStatus.downloading" in source
    assert "byStatus.queued" in source
    assert "byStatus.paused" not in source
    assert "stats && stats.paused" in source
    assert "stats && stats.operator_active_downloads" in source


def test_operator_title_has_cancelable_idle_confirmation():
    source = (STATIC / "operator-title.js").read_text(encoding="utf-8")

    assert "const IDLE_CONFIRM_MS = 1500;" in source
    assert "clearTimeout(idleTimer)" in source
    assert "idleTimer = setTimeout" in source
    assert "latestLogicalActive === 0" in source


def test_operator_title_retains_last_progress_when_handoff_has_no_progress_sample():
    source = (STATIC / "operator-title.js").read_text(encoding="utf-8")

    assert "rawProgress == null ? NaN : Number(rawProgress)" in source
    assert "if (Number.isFinite(progress))" in source


def test_custom_speed_cap_handler_is_unchanged():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert (
        '<button type="button" class="btn btn-primary btn-sm" '
        'onclick="applyAria2TopbarCustomSpeedCap()">Apply</button>'
    ) in html
