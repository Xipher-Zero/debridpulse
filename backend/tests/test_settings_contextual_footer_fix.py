from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
SETTINGS_PAGE = STATIC / "ui-settings-page.js"
SETTINGS_CSS = STATIC / "ui-settings-page.css"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_footer_routes_no_page_specific_action_at_all():
    """DP 1.0.13 Notifications migration: the contextual footer region is gone.

    Test Discord and Send Report Now were the last two page-specific footer
    actions. Each has been relocated into the header rail of the card it is
    about, where exactly one control owns it -- so the footer's
    show-this-page's-buttons lifecycle has nothing left to show and was removed
    rather than left behind empty.
    """
    page = source(SETTINGS_PAGE)
    css = source(SETTINGS_CSS)

    for retired in (
        'data-context-action',
        'dp-settings-context-actions',
        "button.hidden = button.dataset.contextAction !== name;",
        "Test Discord",
        "Send Report Now",
        "function sendReport(",
    ):
        assert retired not in page, retired
    assert "dp-settings-context-actions" not in css

    # The relocated actions have exactly one owner each, in their own card's rail.
    assert page.count("providerTestAction('test-discord')") == 1
    assert page.count("providerTestAction('send-report')") == 1
    assert page.count("action === 'test-discord'") == 1
    assert page.count("action === 'send-report'") == 1


def test_the_generic_apply_footer_itself_is_untouched():
    """Footer removal is a later workstream. This one only establishes that
    nothing page-specific routes through the footer any more."""
    page = source(SETTINGS_PAGE)

    assert 'data-action="save" data-deferred-apply' in page
    assert "Apply Settings" in page
    assert "Changes remain unsaved until Apply Settings is selected." in page
    assert "dp-settings-master-footer" in page
    assert "async function saveCurrent(button)" in page
    assert "await persistNonAuth();" in page
    assert "[data-deferred-apply], .dp-settings-save-hint" in page


def test_every_migrated_tab_hides_the_deferred_contract_it_no_longer_has():
    """DP 1.0.13: Downloads, Extraction, Notifications and Authentication all
    commit at their own field boundaries, so the footer must neither offer them
    an Apply nor claim that anything on them is unsaved. Data & Maintenance is
    the one tab that still has a deferred contract."""
    page = source(SETTINGS_PAGE)

    declared = page.split("FIELD_BOUNDARY_TABS = new Set(", 1)[1].split(")", 1)[0]
    for tab in ("'downloads'", "'extraction'", "'notifications'", "'authentication'"):
        assert tab in declared, tab
    assert "'maintenance'" not in declared

    assert "Test Download Engine" not in page
    assert "testDownloadEngine" not in page
