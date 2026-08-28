"""Runtime architecture invariants for the v1.0.11 migration layer."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_first_paint_does_not_own_page_runtimes():
    bootstrap = read("ui-theme-bootstrap.js")
    assert "/ui-presentation-loader.js?v=1" in bootstrap
    for page_runtime in (
        "ui-visual-behavior-fixes.js",
        "ui-statistics-orchestrator.js",
        "ui-statistics-batch3.js",
        "ui-statistics-batch4.js",
        "ui-statistics-batch5.js",
        "ui-settings-page.js",
        "ui-settings-architecture.js",
        "ui-settings-presentation.js",
        "ui-error-semantics.js",
    ):
        assert page_runtime not in bootstrap


def test_statistics_wrapper_graph_has_exactly_one_writer():
    owners = {
        name: read(name).count("window.loadDetailedStats = wrapped")
        for name in (
            "ui-statistics-orchestrator.js",
            "ui-visual-behavior-fixes.js",
            "ui-statistics-batch3.js",
            "ui-statistics-batch4.js",
            "ui-statistics-batch5.js",
        )
    }
    assert owners == {
        "ui-statistics-orchestrator.js": 1,
        "ui-visual-behavior-fixes.js": 0,
        "ui-statistics-batch3.js": 0,
        "ui-statistics-batch4.js": 0,
        "ui-statistics-batch5.js": 0,
    }


def test_statistics_layers_share_one_post_render_event():
    for name in (
        "ui-visual-behavior-fixes.js",
        "ui-statistics-batch3.js",
        "ui-statistics-batch4.js",
        "ui-statistics-batch5.js",
    ):
        assert "debridpulse:statistics-rendered" in read(name)


def test_settings_page_is_direct_and_not_dom_lifecycle_driven():
    settings = read("ui-settings-page.js")
    assert "window.renderSettings = render" in settings
    assert "window.getFormSettings = serialize" in settings
    assert "view.innerHTML = `" in settings
    assert "settingsObserver" not in settings
    assert "observeSettingsForm" not in settings
    assert "scheduleApply" not in settings
    assert "new MutationObserver" not in settings
    assert "dp-settings-preserved" not in settings


def test_statistics_page_does_not_own_global_shell_branding():
    batch5_js = read("ui-statistics-batch5.js")
    batch5_css = read("ui-statistics-batch5.css")
    shell_js = read("ui-shell-runtime.js")
    shell_css = read("ui-shell-brand.css")

    assert "normalizeShellBranding" not in batch5_js
    assert "sidebar-version" not in batch5_js
    assert "#sidebar-version.dp-app-version" not in batch5_css
    assert "normalizeShellBranding" in shell_js
    assert "#sidebar-version.dp-app-version" in shell_css


def test_error_semantics_does_not_busy_poll_for_core_helpers():
    error = read("ui-error-semantics.js")
    assert "startAfterCore" in error
    assert "setTimeout(startWhenReady" not in error
    assert "window.setTimeout(startWhenReady" not in error
