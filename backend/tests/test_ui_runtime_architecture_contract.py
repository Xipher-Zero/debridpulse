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
    assert "window.DPSettingsPage = Object.freeze({load, confirm: confirmAction});" in settings
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
    icons = read("operator-title.js")
    for retired in ("ui-runtime.js", "ui-downloads-runtime.js"):
        assert not (STATIC / retired).exists()
        assert retired not in index
        assert retired not in icons
    for forbidden in (
        "new MutationObserver", "createElement('script')", "bindThemeToggle",
        "decorateNavigation", "ensureRuntime",
    ):
        assert forbidden not in icons
    assert "function renderTorrentPagination(" in app
    assert "function setFilter(" in app
    assert "function updateDownloadsTrackedCopy(" in app
    assert "dp-downloads-detail-row" in app
    assert 'draggable="true"' not in app
    assert 'data-dp-ui="v1.0.12-canonical"' in index


def test_canonical_icon_insertions_use_one_lucide_geometry_owner() -> None:
    index = read("index.html")
    app = read("app.js")
    icons = read("operator-title.js")
    for icon in ("upload", "refresh", "arrowRight", "pause", "play", "trash2", "x"):
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
