from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_concise_failure_taxonomy_uses_only_canonical_categories():
    import json
    import subprocess
    script = read("ui-error-semantics.js")
    checks = [
        ({"error": {"category": "disk_full"}, "error_message": "LINK_DOWN"}, "disk_full"),
        ({"error": {"category": "source_not_found"}}, "source_not_found"),
        ({"error_message": "LINK_HOST_NOT_SUPPORTED", "provider_status_code": 11}, "internal_error"),
        ({"error": {"category": "unknown_future_code"}}, "internal_error"),
        ({"error": {"category": "__proto__"}}, "internal_error"),
    ]
    code = "global.window = {};\n" + script + "\nconst cases = " + json.dumps(checks) + "; cases.forEach(([input,expected]) => { if(window.DPFailureSemantics.classify(input)!==expected) throw new Error(expected); });"
    result = subprocess.run(["node", "-e", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "LINK_HOST_NOT_SUPPORTED" not in script
    assert "fetch(" not in script
    assert "provider_status" not in script


def test_terminal_failure_progress_has_one_renderer_and_truthful_geometry():
    app = read("app.js")
    runtime = read("ui-error-semantics.js")
    for fragment in ("function progress(pct, status)", "const visual = actual;", "dp-terminal-error-progress", "dp-terminal-error-rail", "data-dp-actual-progress", "data-dp-visual-progress", "actual.toFixed(0) + '%'", "background:var(--dp-state-error)!important", "background-image:none!important"):
        assert fragment in app
    assert "paintFailedProgress" not in runtime
    assert "window.progress =" not in runtime
    assert "MutationObserver" not in runtime
    assert "fetch(" not in runtime
    assert "failed && actual === 0 ? 100 : actual" not in app
    css = read("ui-visual-accents.css")
    for fragment in (".prog.dp-terminal-error-rail", "overflow: visible !important", ".prog.dp-terminal-error-rail::before", ".prog.dp-terminal-error-rail::after", ".prog-fill.error", "background: var(--dp-state-error) !important", "background-image: none !important"):
        assert fragment in css


def test_error_semantics_is_available_before_first_render_without_fetch_or_observers():
    runtime = read("ui-error-semantics.js")
    html = read("index.html")
    assert "window.DPFailureSemantics = Object.freeze" in runtime
    assert "addEventListener" not in runtime
    assert "setTimeout" not in runtime
    assert "fetch(" not in runtime
    assert html.index('/ui-error-semantics.js') < html.index('/app.js')


def test_dark_dashboard_cards_receive_subdued_colored_shadow():
    css = read("ui-visual-accents.css")
    assert "rgba(84, 38, 131, .18)" in css
    assert "rgba(167, 139, 250, .06)" in css
    assert "#view-dashboard .dash-hero-stat" in css
    assert "#view-dashboard .dp-dashboard-quick-add" in css
    assert "#view-dashboard .dp-dashboard-activity" in css


def test_details_scrollbar_has_no_increment_decrement_buttons():
    modal_css = read("ui-modal-contract.css")
    review_css = read("ui-visual-accents.css")
    assert ".modal-body::-webkit-scrollbar-button" in modal_css
    assert "@supports selector(::-webkit-scrollbar)" in review_css
    assert "::-webkit-scrollbar-button:vertical:decrement" in review_css
    assert "::-webkit-scrollbar-button:vertical:increment" in review_css
    assert "display: none !important" in review_css
    assert "width: 0 !important" in review_css
    assert "height: 0 !important" in review_css
