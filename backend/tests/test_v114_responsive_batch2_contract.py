"""DP 1.0.14 responsive Settings batch 2: the owners each fix lives in.

Behaviour is proved in the browser (settings-responsive-batch-2.spec.js, with a
real User session) and at the auth writer (test_v114_local_credential_apply.py);
these pin where it lives, so a later edit cannot quietly reopen one.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
PAGE = (STATIC / "ui-settings-page.js").read_text(encoding="utf-8")
PAGE_CSS = (STATIC / "ui-settings-page.css").read_text(encoding="utf-8")
SECTION_CSS = {name: (STATIC / name).read_text(encoding="utf-8") for name in (
    "ui-settings-authentication.css", "ui-settings-notifications.css", "ui-settings-maintenance-wipe.css",
    "ui-settings-chrome.css")}


def _function(name: str) -> str:
    return PAGE.split(f"function {name}(", 1)[1].split("\n  }\n", 1)[0]


def test_the_stacked_island_keeps_each_setting_at_its_own_width():
    # Item 1 (and the session island, Item 3): centred, never stretched apart.
    stacked = PAGE_CSS.split("#view-settings .dp-settings-island[data-island='stacked'] {", 1)[1].split("}", 1)[0]
    assert "align-items: center;" in stacked and "stretch" not in stacked
    assert "justify-content: space-between" not in PAGE_CSS.split("data-island='stacked'", 1)[1].split("@media", 1)[0]
    assert 'dp-settings-auth-session-row dp-settings-island' in PAGE
    session = _function("authStatusCard")
    assert session.index("'Browser Session Lifetime'") < session.index("'Active Browser Sessions'")


def test_the_local_credential_is_applied_only_by_apply():
    # Item 5: neither field is enrolled in field-boundary persistence.
    fields = PAGE.split("const COMMIT_FIELDS", 1)[1].split("});", 1)[0]
    assert not re.search(r"^    auth_(username|password): ", fields, re.M)
    panel = _function("authenticationPanel")
    for key in ("auth_username", "auth_password"):
        call = panel.split(f"input('{key}'", 1)[1].split("});", 1)[0]
        assert "commit: false," in call, key
    apply = _function("applyCredentials")
    assert "credentialApply = writeAuthentication(payload);" in apply
    assert apply.count("writeAuthentication(") == 1 and "request(" not in apply
    draft = _function("credentialDraft")
    assert "'Enter a new password to change the username.'" in draft
    # Item 4: Clear inside the password field, Apply in its action slot.
    password = panel.split("input('auth_password'", 1)[1].split("});", 1)[0]
    assert 'embedAction: `<button class="btn btn-danger btn-sm" type="button" data-action="clear-password"' in password
    assert "action: '<button class=\"btn btn-blue btn-sm\" type=\"button\" data-action=\"apply-credentials\" disabled>Apply</button>'" in password


def test_the_credential_status_never_says_verified():
    # Item 7.
    status = _function("credentialStatus")
    assert "Verified" not in status and "password_configured" in status
    assert "headerStatus: credentialStatus(a)," in _function("authenticationPanel")


def test_every_governing_card_of_the_four_sections_is_hinted_and_their_private_grids_are_retired():
    # Items 2 and 6: one grammar, one fitter.
    for card in ("Authentication Status", "Username & Password", "OpenID Connect", "User Accounts", "Integration Accounts",
                 "Integration Delivery Profiles", "Discord Notifications", "Statistics Reporting", "Event Logging",
                 "Backups & Retention", "Database Reset Controls"):
        call = PAGE.split(f"card('{card}'", 1)[1].split("\n    });", 1)[0]
        assert "hinted: true" in call, card
    for name, css in SECTION_CSS.items():
        assert not re.search(r"-card > \.card-header \{[^}]*grid-template-columns", css), name
        assert "dp-settings-card-title-hint" not in css, name
    assert "titleHint" not in PAGE
    # Both late render paths hand their new headers to the one fitter.
    assert "fitHeaderHints();" in _function("paintUsers")
    assert "fitHeaderHints();" in _function("renderOwnNotifications")
    assert PAGE.count("new ResizeObserver(() => headerHintFit())") == 1
