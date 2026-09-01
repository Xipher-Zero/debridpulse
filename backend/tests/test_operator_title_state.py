from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def test_operator_title_uses_authoritative_logical_download_phase():
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    shell = (STATIC / "operator-title.js").read_text(encoding="utf-8")

    assert "byStatus.downloading" in source
    assert "byStatus.queued" in source
    assert "byStatus.paused" not in source
    assert "stats && stats.paused" in source
    assert "stats && stats.operator_active_downloads" in source
    assert "window.updateOperatorTitle =" not in shell


def test_operator_title_has_cancelable_idle_confirmation():
    source = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "updateOperatorTitle._idleTimer != null" in source
    assert "clearTimeout(updateOperatorTitle._idleTimer)" in source
    assert "updateOperatorTitle._idleTimer = setTimeout" in source
    assert "updateOperatorTitle._latestLogicalActive === 0" in source
    assert "}, 1500);" in source


def test_operator_title_retains_last_progress_when_handoff_has_no_progress_sample():
    source = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "rawProgress == null ? NaN : Number(rawProgress)" in source
    assert "if (Number.isFinite(value))" in source


def test_custom_speed_cap_handler_is_unchanged():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert (
        '<button type="button" class="btn btn-primary btn-sm" '
        'onclick="applyAria2TopbarCustomSpeedCap()">Apply</button>'
    ) in html
