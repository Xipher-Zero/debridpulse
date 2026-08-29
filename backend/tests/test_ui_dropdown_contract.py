from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_universal_dropdown_contract_is_loaded_before_page_layers():
    style = read("style-v11.css")
    dropdown_import = "@import url('/ui-dropdown-contract.css?v=20');"
    assert dropdown_import in style
    assert style.index(dropdown_import) < style.index("@import url('/ui-settings-page.css?v=2');")
    assert style.index(dropdown_import) < style.index("@import url('/ui-activity-log-page.css?v=28');")


def test_single_selects_are_upgraded_globally_not_per_page():
    runtime = read("ui-accessibility-runtime.js")
    assert "function installUniversalSelectDropdowns()" in runtime
    assert "root.querySelectorAll('select').forEach(enhanceSelect)" in runtime
    assert "new MutationObserver(function (records)" in runtime
    assert ".observe(document.body, {childList: true, subtree: true})" in runtime
    assert "installUniversalSelectDropdowns();" in runtime


def test_native_select_remains_authoritative_event_source():
    runtime = read("ui-accessibility-runtime.js")
    assert "select.selectedIndex = optionIndex;" in runtime
    assert "select.dispatchEvent(new Event('input', {bubbles: true}));" in runtime
    assert "select.dispatchEvent(new Event('change', {bubbles: true}));" in runtime
    assert "select.classList.add('dp-native-select--enhanced');" in runtime


def test_open_menu_uses_shared_top_layer_and_trigger_width():
    runtime = read("ui-accessibility-runtime.js")
    css = read("ui-dropdown-contract.css")
    assert "document.body.appendChild(layer);" in runtime
    assert "position: fixed;" in css
    assert "dpDropdownLayer.style.width = width + 'px';" in runtime
    assert "const rect = trigger.getBoundingClientRect();" in runtime
    assert "window.addEventListener('scroll', positionProjectedMenu" in runtime
    assert "window.addEventListener('resize', positionProjectedMenu" in runtime


def test_dropdown_contract_has_keyboard_and_escape_navigation():
    runtime = read("ui-accessibility-runtime.js")
    for key in ("ArrowDown", "ArrowUp", "Home", "End", "Escape"):
        assert f"event.key === '{key}'" in runtime
    assert "closeProjectedSelect(true);" in runtime


def test_speed_cap_rich_popover_right_aligns_to_trigger():
    css = read("ui-dropdown-contract.css")
    assert "body.dp-v11-structural .aria2-cap-menu" in css
    assert "right: 0 !important;" in css
