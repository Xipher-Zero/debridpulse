"""Canonical frontend runtime ownership contracts for v1.0.12."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"

_STATS_DETAIL_IO_PATTERNS = (
    re.compile(r"""\bapi\(\s*['\"]GET['\"]\s*,\s*['\"]/stats/detail"""),
    re.compile(r"""\bfetch\(\s*['\"]/stats/detail"""),
    re.compile(r"""\brequest\(\s*['\"]GET['\"]\s*,\s*['\"]/stats/detail"""),
)


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def all_js_files() -> list[Path]:
    return sorted(STATIC.glob("*.js"))


def statistics_detail_io_owners() -> list[str]:
    owners = []
    for path in all_js_files():
        source = path.read_text(encoding="utf-8")
        if any(pattern.search(source) for pattern in _STATS_DETAIL_IO_PATTERNS):
            owners.append(path.name)
    return owners


def normalized(path: str) -> str:
    return urlsplit(path).path


def test_first_paint_bootstrap_does_not_own_application_io_or_page_state() -> None:
    bootstrap = read("ui-theme-bootstrap.js")
    assert "localStorage.getItem('theme')" in bootstrap
    for forbidden in (
        "fetch(", "/api/", "XMLHttpRequest", "EventSource", "MutationObserver",
        "loadDetailedStats", "loadSettings",
    ):
        assert forbidden not in bootstrap


def test_statistics_detail_endpoint_has_one_frontend_io_owner() -> None:
    owners = statistics_detail_io_owners()
    assert len(owners) == 1, f"Statistics detail I/O has multiple owners: {owners}"


def test_settings_page_is_authoritative_clean_room_owner() -> None:
    settings = read("ui-settings-page.js")
    picker = read("ui-settings-directory-picker.js")
    assert "window.DPSettingsPage = Object.freeze({load});" in settings
    assert "window.DPSettingsModal = Object.freeze({confirm: confirmAction});" in settings
    assert "const modalApi = window.DPSettingsModal;" in picker
    assert "window.loadSettings = load;" in settings
    assert "view.innerHTML =" in settings
    assert "request('GET', '/settings'" in settings
    assert "request('GET', '/auth/config'" in settings
    for forbidden in (
        "window.renderSettings =", "window.getFormSettings =",
        "window.switchSettingsTab =", "settingsObserver",
        "observeSettingsForm", "new MutationObserver",
    ):
        assert forbidden not in settings


def test_loaded_runtime_markers_are_unique_when_a_presentation_loader_exists() -> None:
    loader_path = STATIC / "ui-presentation-loader.js"
    if not loader_path.exists():
        return
    loader = loader_path.read_text(encoding="utf-8")
    runtime_paths = re.findall(r"""src:\s*['\"]([^'\"]+\.js(?:\?[^'\"]*)?)['\"]""", loader)
    style_paths = re.findall(r"""href:\s*['\"]([^'\"]+\.css(?:\?[^'\"]*)?)['\"]""", loader)
    markers = re.findall(r"""marker:\s*['\"]([^'\"]+)['\"]""", loader)
    normalized_runtimes = [normalized(path) for path in runtime_paths]
    normalized_styles = [normalized(path) for path in style_paths]
    assert len(normalized_runtimes) == len(set(normalized_runtimes))
    assert len(normalized_styles) == len(set(normalized_styles))
    assert len(markers) == len(set(markers))


def test_error_semantics_does_not_busy_poll_for_core_helpers() -> None:
    error = read("ui-error-semantics.js")
    assert "window.DPFailureSemantics = Object.freeze" in error
    assert "setTimeout" not in error
    assert "addEventListener" not in error


def test_shell_and_downloads_have_direct_canonical_owners() -> None:
    index = read("index.html")
    app = read("app.js")
    # DP 1.0.12 canonical flattening: ui-downloads.js is the sole Downloads
    # controller/renderer owner -- app.js no longer carries this content.
    downloads = read("ui-downloads.js")
    icons = read("operator-title.js")
    for retired in ("ui-runtime.js", "ui-downloads-runtime.js", "ui-downloads-presentation.js"):
        assert not (STATIC / retired).exists()
        assert retired not in index
        assert retired not in icons
    for forbidden in (
        "new MutationObserver", "createElement('script')", "bindThemeToggle",
        "decorateNavigation", "ensureRuntime",
    ):
        assert forbidden not in icons
    assert "function renderTorrentPagination(" in downloads
    assert "function setFilter(" in downloads
    assert "function updateDownloadsTrackedCopy(" in downloads
    assert "dp-downloads-detail-row" in downloads
    assert 'draggable="true"' not in downloads
    assert 'data-dp-ui="v1.0.12-canonical"' in index


def test_canonical_icon_insertions_use_one_lucide_geometry_owner() -> None:
    index = read("index.html")
    app = read("app.js")
    icons = read("operator-title.js")
    for icon in ("refresh", "arrowRight", "pause", "play", "trash2", "x"):
        assert f'data-dp-lucide="{icon}"' in index
        assert f"{icon}:" in icons
    assert "window.DPIcons.svg" in app
    assert "const LUCIDE" in icons


def test_archived_runtime_layers_do_not_reappear() -> None:
    index = read("index.html")
    joined = "\n".join(path.read_text(encoding="utf-8") for path in all_js_files())
    for retired in (
        "sidebar-v2.js", "hamburger-v2.js", "provider-ui.js",
        "ui-runtime.js", "ui-downloads-runtime.js",
    ):
        assert retired not in index
        assert retired not in joined


# ---------------------------------------------------------------------------
# DP 1.0.12 final audit, Workstream B: no frontend monkeypatching. Ownership is
# never taken by replacing another owner's global, a native browser API, or a
# function another module defined. Each gate below matches the retired *shape*
# and deliberately allows an owner to publish its own exported namespace.
# ---------------------------------------------------------------------------

def _source_without_comments(name: str) -> str:
    text = read(name)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def test_no_frontend_module_replaces_a_native_or_foreign_global() -> None:
    forbidden = (
        r"window\.EventSource\s*=(?!=)",
        r"window\.fetch\s*=(?!=)",
        r"window\.XMLHttpRequest\s*=(?!=)",
        r"window\.loadEvents\s*=(?!=)",
        r"window\.filterEvents\s*=(?!=)",
        r"window\.updateAria2TopbarBadge\s*=(?!=)",
        r"(?m)^\s*loadEvents\s*=(?!=)",
        r"(?m)^\s*filterEvents\s*=(?!=)",
        r"const\s+originalUpdate\s*=\s*window\.updateAria2TopbarBadge",
        r"Object\.defineProperty\(\s*window\s*,\s*['\"](?:EventSource|fetch)['\"]",
    )
    for path in all_js_files():
        text = _source_without_comments(path.name)
        for pattern in forbidden:
            assert not re.search(pattern, text), f"{path.name} matches retired monkeypatch shape {pattern!r}"


def test_app_js_is_the_one_event_source_owner_and_registers_the_consolidation_event() -> None:
    owners = [p.name for p in all_js_files() if re.search(r"new\s+EventSource\(", _source_without_comments(p.name))]
    assert owners == ["app.js"]
    app = _source_without_comments("app.js")
    assert app.count("new EventSource(") == 1
    assert "'/api/events/stream'" in app
    assert "'duplicate_consolidated'" in app
    assert "window.DPIcons.consolidationToastCopy(" in app
    assert "window.DPIcons.toast(copy, 'success')" in app
    assert "EventSource" not in read("operator-title.js")
    # operator-title.js keeps only the copy and the toast presentation.
    operator = read("operator-title.js")
    assert "consolidationToastCopy: consolidationToastCopy" in operator
    assert "installConsolidationEventConsumer" not in operator


def test_activity_log_has_exactly_one_behavior_owner_and_no_compatibility_globals() -> None:
    app = _source_without_comments("app.js")
    for retired in ("_allEvents", "function loadEvents", "function filterEvents", "window.loadEvents",
                    "window.filterEvents"):
        assert retired not in app
    assert "window.DPActivityLog.load()" in app
    owner = _source_without_comments("ui-activity-log-runtime.js")
    assert "window.DPActivityLog=Object.freeze({load,formatTimestamp});" in owner
    # The owner assigns nothing but its own namespace to window.
    assert re.findall(r"window\.([A-Za-z0-9_$]+)\s*=(?!=)", owner) == ["DPActivityLog"]
    for other in all_js_files():
        if other.name != "ui-activity-log-runtime.js":
            assert "DPActivityLog" not in _source_without_comments(other.name) or other.name == "app.js"


def test_topbar_concurrency_is_rendered_by_the_canonical_owner_with_no_wrapper_runtime() -> None:
    assert not (STATIC / "ui-topbar-concurrency.js").exists()
    assert "ui-topbar-concurrency" not in read("index.html")
    for path in all_js_files():
        text = path.read_text(encoding="utf-8")
        assert "ui-topbar-concurrency" not in text and "DPTopbarConcurrency" not in text, path.name
        assert "syncConfiguredConcurrency" not in text, path.name
    app = _source_without_comments("app.js")
    assert app.count("function updateAria2TopbarBadge(") == 1
    assert "window.DPProcessingPresentation.configuredMaxConcurrency()" in app
    processing = _source_without_comments("ui-processing-presentation.js")
    assert "transfer_policy?.max_concurrent_executions" in processing
    assert "max_concurrent_downloads" not in processing and "aria2_max_active_downloads" not in processing


def test_frontend_reads_only_canonical_settings_fields() -> None:
    flat_reads = (
        r"settingsData\.aria2_[A-Za-z_]+", r"settingsData\.max_concurrent_downloads",
        r"settingsData\.poll_interval_seconds", r"settingsData\.upload_fail_retry_[a-z_]+",
        r"settingsData\.stuck_download_timeout_hours", r"settingsData\.alldebrid_[A-Za-z_]+",
        r"cfg\.aria2_[A-Za-z_]+",
    )
    for path in all_js_files():
        text = _source_without_comments(path.name)
        for pattern in flat_reads:
            assert not re.search(pattern, text), f"{path.name} reads flat alias {pattern!r}"
    app = _source_without_comments("app.js")
    assert "settingsData.integrations && settingsData.integrations.aria2" in app
    assert "settingsData.execution_runtime_limits = Object.assign({}, settingsData.execution_runtime_limits" in app
    live = _source_without_comments("ui-settings-aria2-live.js")
    assert "aria2Mode()" in live and "settingsData.aria2_mode" not in live
