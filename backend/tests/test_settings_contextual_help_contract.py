"""Settings contextual help and the Sierra copy corrections (1.0.13).

Every Settings subsection card renders exactly one help chip naming a topic
of the one help-content owner (ui-settings-help.js), which presents it
read-only through the canonical dialog shell. The locked hints are exact, the
OIDC and reporting explanations say what the backend does, and the role
comparison agrees with the authorization policy it summarises.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from auth.accounts import Role
from auth.oidc import OidcAuthorizationError, OidcConfiguration, authorize_oidc_claims
from auth.permissions import Capability, ROLE_CAPABILITIES

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
PAGE = (STATIC / "ui-settings-page.js").read_text(encoding="utf-8")
HELP = (STATIC / "ui-settings-help.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")


def _hint_of(key: str) -> str:
    """The source of one field's builder call, from its key to its hint."""
    start = re.search(rf"\b(?:input|selectField|textarea)\('{key}'", PAGE).start()
    return PAGE[start:start + 900]


# ── Items 2-5 and 7: exact locked hints, on the intended fields ─────────────

LOCKED = {
    "rsync_directory_depth": "How many folder levels below an rsync folder that you add where DebridPulse will "
                             "search for files. Files in deeper folders are not included.",
    "webdav_directory_depth": "How many folder levels below a WebDAV folder that you add where DebridPulse will "
                              "search for files. Files in deeper folders are not included.",
    "webdav_max_files": "The maximum number of files DebridPulse will accept from a WebDAV folder. If the folder "
                        "contains more files than this limit, the entire folder is rejected rather than "
                        "partially added.",
    "stats_snapshot_interval_minutes": "How often DebridPulse saves a record of download activity and statistics "
                                       "from the previous 24 hours to its internal database. These snapshots are "
                                       "not sent as reports.",
    "stats_snapshot_keep_days": "How many days DebridPulse keeps its saved statistics snapshots before "
                                "automatically deleting older records. Snapshots are stored internally and are "
                                "not currently viewable in the interface.",
}
SESSION_LIFETIME = ("How long a browser login remains valid before the session expires and you must sign in "
                    "again. Signing in starts a new session. Active downloads continue uninterrupted.")


@pytest.mark.parametrize("key", sorted(LOCKED))
def test_each_locked_hint_is_verbatim_on_its_own_field(key):
    assert PAGE.count(LOCKED[key]) == 1
    assert LOCKED[key] in _hint_of(key), key


def test_the_session_lifetime_hint_is_verbatim_beside_its_control():
    assert PAGE.count(SESSION_LIFETIME) == 1
    lifetime = PAGE.split("inlineField(lifetimeId, 'Browser Session Lifetime',", 1)[1][:400]
    assert SESSION_LIFETIME in lifetime


def test_the_superseded_hints_are_gone_without_a_shadow_copy():
    for retired in (
        "How far below an rsync folder you add",
        "How far below a WebDAV folder you add",
        "The most files one WebDAV folder may hold",
        "before sign-in is required again",
        "Set how often DebridPulse records a statistics snapshot.",
        "Delete statistics snapshots older than the configured number of days.",
        "Space-separated scopes requested during sign-in.",
        "Claim containing group memberships",
        "Authorize matching OIDC subject identifiers",
        "Authorize verified email addresses",
        "Authorize identities belonging to matching OIDC groups",
        "Choose whether any authenticated OIDC identity is accepted",
    ):
        assert retired not in PAGE, retired


# ── Item 6: OIDC explanations, true to authorize_oidc_claims ────────────────

def test_the_oidc_hints_keep_every_technical_term():
    assert "The usual values are openid profile email, separated by spaces." in _hint_of("oidc_scopes")
    assert "Usually groups." in _hint_of("oidc_group_claim")
    assert "Requires email_verified=true." in _hint_of("oidc_allowed_emails")
    subjects = _hint_of("oidc_allowed_subjects")
    assert "issuer|subject-id" in subjects and "not a username" in subjects
    assert "one group name per line" in _hint_of("oidc_allowed_groups")
    toggle = PAGE.split('class="dp-settings-oidc-section-copy">', 1)[1].split("</small>", 1)[0]
    assert toggle.startswith("When enabled, anyone who successfully signs in through your identity provider "
                             "can access DebridPulse.")
    assert "a user must match every list that is filled in" in toggle


def _config(**lists) -> OidcConfiguration:
    return OidcConfiguration(
        issuer="https://id.example", client_id="dp", client_secret="", scopes=("openid",),
        callback_url="https://dp.example/auth/oidc/callback", provider_name="IdP",
        allow_all=lists.pop("allow_all", False), allowed_subjects=lists.pop("subjects", ()),
        allowed_emails=lists.pop("emails", ()), allowed_groups=lists.pop("groups", ()), group_claim="groups")


CLAIMS = {"iss": "https://id.example", "sub": "u-1", "email": "Ann@Example.com", "email_verified": True,
          "groups": ["media"]}


def test_the_explained_allowlist_composition_is_the_enforced_one():
    # Every filled-in list must match (AND); one list matching is not enough.
    authorize_oidc_claims(_config(emails=("ann@example.com",), groups=("media",)), CLAIMS)
    with pytest.raises(OidcAuthorizationError):
        authorize_oidc_claims(_config(emails=("ann@example.com",), groups=("admins",)), CLAIMS)
    # A subject entry is issuer|subject; the bare subject ID never matches.
    authorize_oidc_claims(_config(subjects=("https://id.example|u-1",)), CLAIMS)
    with pytest.raises(OidcAuthorizationError):
        authorize_oidc_claims(_config(subjects=("u-1",)), CLAIMS)
    # Email requires email_verified=true; letter case does not matter.
    with pytest.raises(OidcAuthorizationError):
        authorize_oidc_claims(_config(emails=("ann@example.com",)), {**CLAIMS, "email_verified": "true"})
    # Every list empty admits no one; Allow Any ignores the lists.
    with pytest.raises(OidcAuthorizationError):
        authorize_oidc_claims(_config(), CLAIMS)
    authorize_oidc_claims(_config(allow_all=True, groups=("admins",)), CLAIMS)
    # A missing group claim does not match a group list.
    with pytest.raises(OidcAuthorizationError):
        authorize_oidc_claims(_config(groups=("media",)), {k: v for k, v in CLAIMS.items() if k != "groups"})


# ── Item 1: serial commas, literal syntax untouched ─────────────────────────

def test_serial_commas_in_settings_copy():
    for corrected in (
        "Rarely helps with archives, video, or other already-compressed files.",
        "such as 0, 512K, or 2M.",
        "such as 0, 512K, or 64M.",
        "API tokens, downloads, and files are not deleted.",
    ):
        assert corrected in PAGE, corrected
    for retired in ("video or other already-compressed", "512K or 2M", "512K or 64M", "downloads and files are"):
        assert retired not in PAGE, retired
    backend = ROOT / "backend"
    assert "without a scheme, path, or spaces" in (backend / "core" / "settings_constraints.py").read_text()
    assert "without a scheme, path, or spaces" in (backend / "api" / "routes.py").read_text()
    assert "(PNG, JPG, GIF, or WEBP)" in (backend / "services" / "account_notifications.py").read_text()
    # The literal syntax is unchanged: the size grammar still names K or M,
    # and the scope example is still space-separated tokens.
    assert "with an optional K or M suffix" in PAGE
    assert "placeholder: 'openid profile email'" in PAGE


# ── Item 8: coverage, one owner, read-only presentation ─────────────────────

def _topics() -> set[str]:
    block = HELP.split("const TOPICS = Object.freeze({", 1)[1].split("\n  });\n", 1)[0]
    return set(re.findall(r"^    '?([a-z][a-z-]*)'?: \{$", block, re.M))


def test_every_settings_card_declares_exactly_one_help_topic():
    calls = [m.start() for m in re.finditer(r"\b(?:card|groupCard)\((?:'|groupLabel,)", PAGE)]
    assert len(calls) == 17
    for start in calls:
        title = PAGE[start:start + 80]
        # The options object follows the body; its help names the topic.
        options = PAGE[start:start + 6000].split("help: ", 1)[1].split("\n", 1)[0]
        assert re.match(r"(own \? '[a-z-]+' : '[a-z-]+'|'[a-z-]+')", options), title
    assert "${helpChip('engine-activity', 'Download Engine Activity')}" in PAGE
    # Group members (provider, protocol and transfer-method cards) carry none.
    for builder in ("function providerCard(", "function sourceProtocolBox(", "function executorTuningCard("):
        body = PAGE.split(builder, 1)[1].split("\n  }\n", 1)[0]
        assert "helpChip" not in body and "help:" not in body, builder


def test_every_referenced_topic_exists_and_none_is_orphaned():
    referenced = set(re.findall(r"help: '([a-z-]+)'", PAGE))
    for left, right in re.findall(r"help: own \? '([a-z-]+)' : '([a-z-]+)'", PAGE):
        referenced |= {left, right}
    referenced |= set(re.findall(r"helpChip\('([a-z-]+)'", PAGE))
    assert referenced == _topics()
    assert len(referenced) == 20


def test_the_chip_is_one_icon_only_named_control_in_the_header_rail():
    chip = PAGE.split("function helpChip(topic, subject) {", 1)[1].split("\n  }\n", 1)[0]
    assert "const label = `Help: ${subject}`;" in chip
    assert 'aria-label="${html(label)}"' in chip
    assert "window.DPIcons.svg('help', 'dp-settings-help-glyph')" in chip
    assert 'class="dp-settings-help-chip"' in chip
    card = PAGE.split("function card(title, body, options = {}) {", 1)[1].split("\n  }\n", 1)[0]
    assert "options.action || ''}${help}</div>`" in card
    group = PAGE.split("function groupCard(title, body, options = {}) {", 1)[1].split("\n  }\n", 1)[0]
    assert "${options.action || ''}${help}</div>" in group


def test_the_help_owner_is_read_only_and_uses_the_canonical_dialog():
    assert re.search(r"<script src=\"/ui-settings-help\.js\?v=\d+\" defer></script>", INDEX)
    for forbidden in ("fetch(", "request(", "XMLHttpRequest", "DPSettingsPersistence", "data-setting",
                      "innerHTML", "localStorage", "sessionStorage"):
        assert forbidden not in HELP, forbidden
    assert "window.DPSettingsModal.open({" in HELP
    assert "dismiss: true," in HELP and "closeControl: true," in HELP
    assert "window.DPSettingsHelp = Object.freeze({open, topics:" in HELP
    # The page reaches the owner only through its open().
    assert PAGE.count("window.DPSettingsHelp?.open(") == 2


def test_role_selectors_describe_each_role_and_reuse_the_same_help():
    assert PAGE.count("...ROLE_FIELD_HELP}") == 2
    assert "help: 'user-accounts', helpSubject: 'User Account Roles'" in PAGE
    descriptions = PAGE.split("const ROLE_DESCRIPTIONS = Object.freeze({", 1)[1].split("});", 1)[0]
    assert set(re.findall(r"^    (\w+):", descriptions, re.M)) == {"admin", "user", "readonly"}


# ── The role comparison agrees with the authorization policy ────────────────

def _role_rows() -> dict[str, list[str]]:
    block = HELP.split("rows: [", 1)[1].split("\n    ],\n", 1)[0]
    rows = {}
    for raw in re.findall(r"\[('[^\]]*?)\],", block, re.S):
        cells = [cell.replace("’", "'") for cell in re.findall(r"'([^']*)'|\b(YES|NO)\b", raw)
                 for cell in cell if cell]
        rows[cells[0]] = cells[1:]
    return rows


ROLES = (Role.ADMIN, Role.USER, Role.READONLY, Role.INTEGRATION)


def _derived(capability: Capability) -> list[bool]:
    return [capability in ROLE_CAPABILITIES[role] for role in ROLES]


@pytest.mark.parametrize(("row", "capability"), [
    ("Sign in to this browser interface", Capability.INTERFACE),
    ("View the Dashboard, Downloads, and transfer details", Capability.TRANSFERS_READ),
    ("Add downloads", Capability.TRANSFERS_SUBMIT),
    ("Pause, resume, retry, and reorder transfers", Capability.TRANSFERS_OPERATE),
    ("Delete, cancel, or redirect transfers", Capability.TRANSFERS_DESTROY),
    ("Settings, accounts, maintenance, the Activity Log, and global pause", Capability.ADMINISTER),
])
def test_each_role_row_follows_the_capability_it_summarises(row, capability):
    cells = _role_rows()[row]
    assert len(cells) == 4
    for role, held, cell in zip(ROLES, _derived(capability), cells):
        if held:
            assert cell != "NO", (row, role)
        else:
            # A role without the capability is "No", or (Integration) states
            # its narrower external-client path explicitly.
            assert cell == "NO" or role is Role.INTEGRATION, (row, role, cell)


def test_the_qualified_cells_state_the_policy_exceptions():
    rows = _role_rows()
    # A User's destructive act is fenced by every outstanding demand; Admin is not.
    assert rows["Delete, cancel, or redirect transfers"][:2] == ["YES",
                                                               "Only when no other account still needs the transfer"]
    # Integration: no browser, a summary-only Statistics read, advisory pause.
    assert Capability.STATISTICS_SUMMARY in ROLE_CAPABILITIES[Role.INTEGRATION]
    assert Capability.STATISTICS_READ not in ROLE_CAPABILITIES[Role.INTEGRATION]
    assert rows["View Statistics"][3] == "Summary counts through the API only"
    assert "may decline" in rows["Pause, resume, retry, and reorder transfers"][3]
    # Personal notifications are a User's alone; Admin has the global ones.
    assert _derived(Capability.NOTIFICATIONS_SELF) == [False, True, False, False]
    assert rows["Discord notifications and statistics reports"][1:] == ["Its own, under Notifications", "NO", "NO"]
    assert _derived(Capability.DEMAND_ANY) == [True, False, False, False]
    assert _derived(Capability.DEMAND_OWN) == [True, True, False, True]
    assert rows["Withdraw download requests"] == ["Any account's", "Its own", "NO", "Its own jobs"]


# ── Statistics Reporting: destination, recipients and scope ─────────────────

def test_the_reporting_explanation_names_the_implemented_destination():
    from types import SimpleNamespace

    from services.notification_service import reporting_destination

    both = SimpleNamespace(stats_report_webhook_url="https://r", discord_webhook_url="https://d")
    primary = SimpleNamespace(stats_report_webhook_url="", discord_webhook_url="https://d")
    assert reporting_destination(both) == "https://r"
    assert reporting_destination(primary) == "https://d"
    reporting = HELP.split("'statistics-reporting': {", 1)[1].split("\n    },\n", 1)[0]
    assert "the Reporting Webhook, or the Discord Webhook from Discord Notifications when no Reporting Webhook is set" in reporting
    assert "It is not sent to DebridPulse users." in reporting
    assert "not a Statistics Snapshot" in reporting
    own = HELP.split("'statistics-reporting-own': {", 1)[1].split("\n    },\n", 1)[0]
    assert "An Admin’s destinations are never used." in own
    assert "never includes other accounts’ activity" in own
