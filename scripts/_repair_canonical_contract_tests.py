from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def write(rel: str, text: str) -> None:
    (ROOT / rel).write_text(text.rstrip() + "\n", encoding="utf-8")


def replace_once(rel: str, old: str, new: str) -> None:
    source = read(rel)
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{rel}: expected one replacement target, found {count}")
    write(rel, source.replace(old, new, 1))


def sub_once(rel: str, pattern: str, replacement: str) -> None:
    source = read(rel)
    source, count = re.subn(pattern, replacement, source, count=1, flags=re.MULTILINE | re.DOTALL)
    if count != 1:
        raise RuntimeError(f"{rel}: expected one regex replacement, found {count}")
    write(rel, source)


def migrate_syntax_gate_contracts() -> None:
    gate_assertions = {
        "backend/tests/test_auth_settings_oidc_failure_containment.py": (
            r"def test_auth_resilience_runtime_is_covered_by_frontend_syntax_gate\(\):\n.*?\n(?=\Z|def )",
            '''def test_retired_auth_resilience_runtime_is_not_required_by_dynamic_syntax_gate():
    workflow = TEST_WORKFLOW.read_text(encoding="utf-8")
    assert not RESILIENCE_JS.exists()
    assert "find frontend/static -maxdepth 1 -name '*.js' -print0" in workflow
    assert "xargs -0 -n1 node --check" in workflow
''',
        ),
        "backend/tests/test_settings_architecture_ui.py": (
            r"def test_settings_page_runtime_is_owned_by_frontend_syntax_gate\(\):\n.*?\n(?=def )",
            '''def test_settings_page_runtime_is_owned_by_dynamic_frontend_syntax_gate():
    workflow = source(TESTS_WORKFLOW)
    assert "find frontend/static -maxdepth 1 -name '*.js' -print0" in workflow
    assert "xargs -0 -n1 node --check" in workflow
    assert SETTINGS_PAGE_JS.exists()

''',
        ),
        "backend/tests/test_ui_help_local_legal_overlay_contract.py": (
            r"def test_help_legal_overlay_runtime_is_in_frontend_syntax_gate\(\):\n.*?\n(?=\Z|def )",
            '''def test_help_legal_overlay_runtime_is_in_dynamic_frontend_syntax_gate():
    workflow = read(WORKFLOW)
    assert RUNTIME.exists()
    assert "find frontend/static -maxdepth 1 -name '*.js' -print0" in workflow
    assert "xargs -0 -n1 node --check" in workflow
''',
        ),
    }
    for rel, (pattern, replacement) in gate_assertions.items():
        sub_once(rel, pattern, replacement)

    sub_once(
        "backend/tests/test_ui_frontend_deep_audit_contract.py",
        r"def test_ci_syntax_checks_every_runtime_in_the_effective_load_graph\(\) -> None:\n.*?\n(?=\Z|def )",
        '''def test_ci_syntax_checks_every_runtime_in_the_effective_load_graph() -> None:
    workflow = read(WORKFLOW)
    loaded = {
        normalized_asset(path).removeprefix("/")
        for path in direct_script_assets() + bootstrap_script_assets() + loader_assets("js")
        if normalized_asset(path).endswith(".js")
        and not normalized_asset(path).removeprefix("/").startswith("vendor/")
    }

    assert loaded
    assert all("/" not in path for path in loaded)
    assert "find frontend/static -maxdepth 1 -name '*.js' -print0" in workflow
    assert "xargs -0 -n1 node --check" in workflow
''',
    )

    write(
        "backend/tests/test_settings_oidc_callback_draft_ui.py",
        '''from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
CALLBACK_JS = STATIC / "ui-settings-authentication-callback.js"
POLISH_JS = STATIC / "ui-settings-authentication-polish.js"
LOADER = STATIC / "ui-presentation-loader.js"
SETTINGS = STATIC / "ui-settings-page.js"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_oidc_callback_behavior_is_canonical_and_syntax_gate_is_dynamic():
    assert not CALLBACK_JS.exists()
    assert not POLISH_JS.exists()
    assert not LOADER.exists()
    settings = read(SETTINGS)
    assert "function callbackFromPublicBase(value)" in settings
    assert "function updateOidcCallbackPreview()" in settings
    workflow = read(WORKFLOW)
    assert "find frontend/static -maxdepth 1 -name '*.js' -print0" in workflow
    assert "xargs -0 -n1 node --check" in workflow
''',
    )


def migrate_status_and_title_contracts() -> None:
    replace_once(
        "backend/tests/test_direct_links.py",
        '''        self.assertIn("missing:'❌ Missing file'", js)
        self.assertIn("downloading_with_errors:'⬇ Downloading'", js)
        self.assertIn("completed_with_errors:'⚠ Completed with errors'", js)
''',
        '''        icons = (repo_root / "frontend/static/operator-title.js").read_text()
        self.assertIn("missing: {icon: 'x', label: 'Missing file'", icons)
        self.assertIn("downloading_with_errors: {icon: 'triangleAlert', label: 'Downloading'", icons)
        self.assertIn("completed_with_errors: {icon: 'triangleAlert', label: 'Completed with errors'", icons)
''',
    )

    replace_once(
        "backend/tests/test_extraction_lifecycle.py",
        '''    app_source = (root / "frontend/static/app.js").read_text()
''',
        '''    app_source = (root / "frontend/static/app.js").read_text()
    icon_source = (root / "frontend/static/operator-title.js").read_text()
''',
    )
    replace_once(
        "backend/tests/test_extraction_lifecycle.py",
        '''    assert "extracting:'📦 Extracting'" in app_source
''',
        '''    assert "extracting: {icon: 'packageOpen', label: 'Extracting'" in icon_source
''',
    )

    write(
        "backend/tests/test_operator_title_state.py",
        '''from pathlib import Path


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
''',
    )


def migrate_dashboard_css_contracts() -> None:
    rel = "backend/tests/test_ui_cross_page_consistency_contract.py"
    source = read(rel).replace('read_static("ui-dashboard-final.css")', 'read_static("ui-dashboard.css")')
    if source.count('read_static("ui-dashboard.css")') < 2:
        raise RuntimeError(f"{rel}: dashboard owner migration did not apply twice")
    old = '''    dashboard = overlay.index("/ui-dashboard.css?v=20")
    dashboard_final = overlay.index("/ui-dashboard-final.css?v=23")
    downloads = overlay.index("/ui-downloads-page.css?v=27")
    transfer = overlay.index("/ui-transfer-contract.css?v=31")
    visual = overlay.index("/ui-visual-accents.css?v=21")
    signal = overlay.index("/ui-shell-signal-field.css?v=20")
    assert shared < shell < provider < dashboard < dashboard_final < downloads < transfer < visual < signal
'''
    new = '''    dashboard = overlay.index("/ui-dashboard.css?v=20")
    downloads = overlay.index("/ui-downloads-page.css?v=27")
    transfer = overlay.index("/ui-transfer-contract.css?v=31")
    visual = overlay.index("/ui-visual-accents.css?v=21")
    signal = overlay.index("/ui-shell-signal-field.css?v=20")
    assert shared < shell < provider < dashboard < downloads < transfer < visual < signal
'''
    if source.count(old) != 1:
        raise RuntimeError(f"{rel}: cascade target mismatch")
    write(rel, source.replace(old, new, 1))

    replace_once(
        "backend/tests/test_ui_feature_icon_contract.py",
        '    dashboard = read("ui-dashboard-polish-final.css")\n',
        '    dashboard = read("ui-dashboard.css")\n',
    )

    sub_once(
        "backend/tests/test_ui_progress_weight_contract.py",
        r"def test_shared_transfer_contract_is_final_progress_geometry_owner\(\) -> None:\n.*?\n(?=\Z|def )",
        '''def test_shared_transfer_contract_is_final_progress_geometry_owner() -> None:
    overlay = STYLE.read_text(encoding="utf-8")
    dashboard = "/ui-dashboard.css?v=20"
    downloads = "/ui-downloads-page.css?v=27"
    transfer_path = "/ui-transfer-contract.css?v=31"

    for layer in (dashboard, downloads, transfer_path):
        assert layer in overlay
    assert overlay.index(dashboard) < overlay.index(downloads) < overlay.index(transfer_path)

    css = TRANSFER.read_text(encoding="utf-8")
    assert "body.dp-v11-structural :is(#dash-tbody, #t-tbody) .prog," in css
    assert "body.dp-v11-structural :is(#dash-tbody, #t-tbody) .prog-fill" in css
    assert "height: 7px !important" in css
    assert "border-radius: 999px !important" in css
    assert "height: 3.5px !important" not in css
''',
    )

    rel = "backend/tests/test_ui_shell_contract.py"
    source = read(rel)
    old_stack = '''        "/ui-dashboard.css?v=20",
        "/ui-dashboard-batch5.css?v=20",
        "/ui-dashboard-polish.css?v=20",
        "/ui-dashboard-polish-final.css?v=20",
        "/ui-utility-controls.css?v=23",
        "/ui-dashboard-final.css?v=23",
'''
    new_stack = '''        "/ui-dashboard.css?v=20",
        "/ui-utility-controls.css?v=23",
'''
    if source.count(old_stack) != 1:
        raise RuntimeError(f"{rel}: retained dashboard stack target mismatch")
    source = source.replace(old_stack, new_stack, 1)
    source = source.replace('        "/ui-dashboard-final.css": "23",\n', '', 1)
    retired_anchor = '        "ui-dashboard-control-polish.css",\n'
    retired_add = '''        "ui-dashboard-control-polish.css",
        "ui-dashboard-batch5.css",
        "ui-dashboard-polish.css",
        "ui-dashboard-polish-final.css",
        "ui-dashboard-final.css",
'''
    if source.count(retired_anchor) != 1:
        raise RuntimeError(f"{rel}: retired-list anchor mismatch")
    source = source.replace(retired_anchor, retired_add, 1)
    write(rel, source)

    rel = "backend/tests/test_ui_transfer_contract.py"
    source = read(rel)
    source = source.replace('DASHBOARD = STATIC / "ui-dashboard-final.css"', 'DASHBOARD = STATIC / "ui-dashboard.css"')
    old_func = '''def test_transfer_contract_is_final_shared_layer_after_page_geometry() -> None:
    overlay = STYLE.read_text(encoding="utf-8")
    dashboard_final = "/ui-dashboard-final.css?v=23"
    downloads = "/ui-downloads-page.css?v=27"
    help_page = "/ui-help-page.css?v=22"
    transfer = "/ui-transfer-contract.css?v=31"

    for layer in (dashboard_final, downloads, help_page, transfer):
        assert layer in overlay
    assert "/ui-dashboard-progress-weight.css" not in overlay
    assert (
        overlay.index(dashboard_final)
        < overlay.index(downloads)
        < overlay.index(help_page)
        < overlay.index(transfer)
    )
'''
    new_func = '''def test_transfer_contract_is_final_shared_layer_after_page_geometry() -> None:
    overlay = STYLE.read_text(encoding="utf-8")
    dashboard = "/ui-dashboard.css?v=20"
    downloads = "/ui-downloads-page.css?v=27"
    help_page = "/ui-help-page.css?v=22"
    transfer = "/ui-transfer-contract.css?v=31"

    for layer in (dashboard, downloads, help_page, transfer):
        assert layer in overlay
    assert "/ui-dashboard-progress-weight.css" not in overlay
    assert "/ui-dashboard-final.css" not in overlay
    assert (
        overlay.index(dashboard)
        < overlay.index(downloads)
        < overlay.index(help_page)
        < overlay.index(transfer)
    )
'''
    if source.count(old_func) != 1:
        raise RuntimeError(f"{rel}: transfer cascade target mismatch")
    write(rel, source.replace(old_func, new_func, 1))


def main() -> None:
    migrate_syntax_gate_contracts()
    migrate_status_and_title_contracts()
    migrate_dashboard_css_contracts()


if __name__ == "__main__":
    main()
