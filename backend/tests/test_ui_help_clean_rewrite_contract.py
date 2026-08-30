from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_clean_help_runtime_is_wired_into_presentation_loader():
    loader = _read(STATIC / "ui-presentation-loader.js")
    runtime = _read(STATIC / "ui-help-page.js")

    assert "{src: '/ui-help-page.js?v=1', marker: 'data-dp-help-page'}" in loader
    assert "view.classList.add('dp-help-clean-view')" in runtime
    assert "switchHelpTab" not in runtime
    assert "api(" not in runtime
    assert "style=" not in runtime


def test_help_rewrite_uses_master_card_and_semantic_tabs():
    runtime = _read(STATIC / "ui-help-page.js")
    css = _read(STATIC / "ui-help-page.css")

    assert 'class="dp-card dp-help-master-card"' in runtime
    assert 'class="dp-tabs dp-help-tabs"' in runtime
    assert 'class="dp-tab dp-help-tab' in runtime
    assert 'role="tablist"' in runtime
    assert 'role="tab"' in runtime
    assert 'role="tabpanel"' in runtime
    assert 'aria-selected=' in runtime
    assert 'aria-controls=' in runtime
    assert "ArrowLeft" in runtime
    assert "ArrowRight" in runtime
    assert "Home" in runtime
    assert "End" in runtime

    assert "#content:has(#view-help.active)" in css
    assert ".dp-help-master-body" in css
    assert ".dp-help-scroll" in css
    assert "overflow-y: auto" in css


def test_help_rewrite_preserves_the_seven_section_boundaries_during_content_overhaul():
    runtime = _read(STATIC / "ui-help-page.js")

    sections = (
        ("quickstart", "Quick Start", "quickStartPanel"),
        ("howitworks", "How it works", "howItWorksPanel"),
        ("aria2", "aria2", "aria2Panel"),
        ("integrations", "Integrations", "integrationsPanel"),
        ("settings", "Settings", "settingsPanel"),
        ("trouble", "Troubleshooting", "troubleshootingPanel"),
        ("license", "License", "licensePanel"),
    )

    for tab_id, label, panel_function in sections:
        assert f"['{tab_id}', '{label}']" in runtime
        assert f"function {panel_function}()" in runtime
        assert f"panel('{tab_id}', {panel_function}())" in runtime

    assert "Getting started with DebridPulse" in runtime


def test_help_page_uses_canonical_components_without_repainting_them():
    css = _read(STATIC / "ui-help-page.css")

    assert ".dp-card {" not in css
    assert ".dp-tab {" not in css
    assert ".dp-btn {" not in css
    assert "linear-gradient(160deg, var(--dp-surface-2), var(--dp-surface-1))" not in css
