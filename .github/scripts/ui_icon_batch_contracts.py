from pathlib import Path


def replace_exact(path: str, old: str, new: str, expected: int = 1) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != expected:
        raise SystemExit(f"{path}: expected {expected} occurrence(s) of {old!r}, found {count}")
    p.write_text(text.replace(old, new), encoding="utf-8")


# Static bootstrap generations advanced by this batch.
replace_exact(
    "backend/tests/test_dashboard_startup_surface.py",
    '<script src="/operator-title.js?v=20" defer></script>',
    '<script src="/operator-title.js?v=21" defer></script>',
)
replace_exact(
    "backend/tests/test_operator_title_state.py",
    'operator = \'<script src="/operator-title.js?v=20" defer></script>\'',
    'operator = \'<script src="/operator-title.js?v=21" defer></script>\'',
)

# Recover All intentionally moved from the bespoke retry asset to the same
# refresh utility SVG used by Activity Log. Exact parity is asserted by the new
# feature-icon contract, so retry-borderless is no longer a Dashboard asset.
replace_exact(
    "backend/tests/test_ui_dashboard_contract.py",
    '        "retry-borderless.svg",\n',
    "",
)

# Cache generations for reviewed presentation layers.
replace_exact(
    "backend/tests/test_ui_dashboard_polish_contract.py",
    'control = "/ui-dashboard-control-polish.css?v=21"',
    'control = "/ui-dashboard-control-polish.css?v=22"',
)
replace_exact(
    "backend/tests/test_ui_progress_weight_contract.py",
    'control = "/ui-dashboard-control-polish.css?v=21"',
    'control = "/ui-dashboard-control-polish.css?v=22"',
)

# Provider Status changed from nominal 36px grid-box centering to flex centering
# of the crown's visible painted width plus the subscription copy.
replace_exact(
    "backend/tests/test_ui_desktop_downloads_batch_contract.py",
    '    assert "grid-template-columns: 36px max-content" in css\n',
    '    assert "display: flex !important" in css\n',
)
replace_exact(
    "backend/tests/test_ui_desktop_downloads_batch_contract.py",
    '    assert "column-gap: 3px !important" in css\n',
    '    assert "gap: 5px !important" in css\n'
    '    assert "flex: 0 0 20px !important" in css\n'
    '    assert "background-size: 36px 36px !important" in css\n',
)
replace_exact(
    "backend/tests/test_ui_desktop_downloads_batch_contract.py",
    'provider_v2 = style.index("ui-shell-provider-status-v2.css?v=27")',
    'provider_v2 = style.index("ui-shell-provider-status-v2.css?v=28")',
)

# The earlier mutation script advances literal import URLs; update the mapping
# assertions too, which intentionally use path/version pairs instead of URLs.
for path in (
    "backend/tests/test_ui_frontend_deep_audit_contract.py",
    "backend/tests/test_ui_shell_contract.py",
):
    replace_exact(
        path,
        '            "/ui-shell-provider-status-v2.css": "27",',
        '            "/ui-shell-provider-status-v2.css": "28",',
    )
    replace_exact(
        path,
        '            "/ui-dashboard-control-polish.css": "21",',
        '            "/ui-dashboard-control-polish.css": "22",',
    )
