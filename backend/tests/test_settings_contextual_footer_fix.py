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


def test_the_generic_apply_footer_is_gone_entirely():
    """DP 1.0.13 terminal Settings migration: with Data & Maintenance on the
    canonical field-boundary persistence owner, generic Apply had zero semantic
    consumers and the whole footer region was DELETED -- the button, the
    unsaved-changes sentence, the container and the space it reserved. It is
    not hidden, and no tab lifecycle turns it back on."""
    page = source(SETTINGS_PAGE)
    css = source(SETTINGS_CSS)

    for retired in (
        'data-deferred-apply',
        "Apply Settings",
        "Changes remain unsaved",
        "dp-settings-master-footer",
        "dp-settings-save-hint",
        "saveCurrent",
        "persistNonAuth",
        "nonAuthPayload",
        "FIELD_BOUNDARY_TABS",
        'data-action="save"',
    ):
        assert retired not in page, retired

    for retired in ("dp-settings-master-footer", "dp-settings-save-hint"):
        assert retired not in css, retired


def test_no_page_specific_action_can_reacquire_a_footer():
    """The contextual footer region and the generic footer are both gone, so
    there is no shared surface left for a page to route an action through."""
    page = source(SETTINGS_PAGE)

    assert "Test Download Engine" not in page
    assert "testDownloadEngine" not in page
