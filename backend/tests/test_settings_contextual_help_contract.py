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
    # A protocol tile carries none.
    body = PAGE.split("function sourceProtocolBox(", 1)[1].split("\n  }\n", 1)[0]
    assert "helpChip" not in body and "help:" not in body
    # Every Transfer Method card names its OWN topic, once, in its own rail.
    tuning = PAGE.split("function executorTuningCard(", 1)[1].split("\n  }\n", 1)[0]
    assert '<div class="dp-settings-card-header-controls">${helpChip(help, `${label} Tuning`)}</div>' in tuning
    assert _tuning_topics() == ["tuning-direct", "tuning-rsync", "tuning-webdav", "tuning-usenet", "tuning-media"]
    assert set(_tuning_topics()) <= _topics()
    # Every premium provider card names its OWN topic -- six distinct ones,
    # never the group's -- and names it once.
    providers = re.findall(r"providerCard\('([a-z]+)'", PAGE)
    assert set(providers) == {"alldebrid", "realdebrid", "torbox", "usenet", "debridlink", "premiumize"}
    for identity in providers:
        start = PAGE.index(f"providerCard('{identity}'")
        options = PAGE[start:PAGE.index("\n    });", start)]
        assert re.findall(r"help: '([a-z-]+)'", options) == [identity], identity
        assert PAGE.count(f"help: '{identity}'") == 1, identity
    assert set(providers) <= _topics()


def _tuning_topics():
    """The topics the Transfer Method cards name: each call's last argument."""
    panel = PAGE.split("function downloadsPanel(", 1)[1].split("\n  }\n", 1)[0]
    return re.findall(r"executorTuningCard\('[a-z]+',[^+]*?'(tuning-[a-z]+)'\)", panel)


def test_every_referenced_topic_exists_and_none_is_orphaned():
    referenced = set(re.findall(r"help: '([a-z-]+)'", PAGE))
    for left, right in re.findall(r"help: own \? '([a-z-]+)' : '([a-z-]+)'", PAGE):
        referenced |= {left, right}
    referenced |= set(re.findall(r"helpChip\('([a-z-]+)'", PAGE))
    referenced |= set(_tuning_topics())
    assert referenced == _topics()
    # 21, one topic for each of the five premium services that had none, and
    # one for each of the five Transfer Method cards.
    assert {"alldebrid", "debridlink", "premiumize", "realdebrid", "torbox"} <= referenced
    assert len(referenced) == 31


def test_the_chip_is_one_icon_only_named_control_in_the_header_rail():
    chip = PAGE.split("function helpChip(topic, subject) {", 1)[1].split("\n  }\n", 1)[0]
    assert "const label = `Help: ${subject}`;" in chip
    assert 'aria-label="${html(label)}"' in chip
    assert "window.DPIcons.svg('help', 'dp-settings-help-glyph')" in chip
    assert 'class="dp-settings-help-chip"' in chip


def test_every_header_rail_reads_status_then_help_then_its_own_controls():
    """Status, then Help, then the card's existing actions and controls in
    their established order -- in every canonical header builder."""
    card = PAGE.split("function card(title, body, options = {}) {", 1)[1].split("\n  }\n", 1)[0]
    rail = card.split("const rail = ", 1)[1].split(";\n", 1)[0]
    order = [rail.index(token) for token in
             ("dp-settings-provider-config-status", "${help}", "options.headerAction ?", "options.action || ''")]
    assert order == sorted(order)
    group = PAGE.split("function groupCard(title, body, options = {}) {", 1)[1].split("\n  }\n", 1)[0]
    assert '<div class="dp-settings-card-header-controls">${help}${options.action || \'\'}</div>' in group
    provider = PAGE.split("function providerCard(", 1)[1].split("\n  }\n", 1)[0]
    controls = provider.split('<div class="dp-settings-card-header-controls">', 1)[1].split("</div>\n", 1)[0]
    order = [controls.index(token) for token in
             ("dp-settings-provider-config-status", "helpChip(help, title)", "headerAction ?", "${enable}")]
    assert order == sorted(order)
    work = PAGE.split("function executorWorkCard() {", 1)[1].split("\n  }\n", 1)[0]
    assert work.index("helpChip('engine-activity'") < work.index("data-dp-executor-work-refresh")


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


# ── Usenet: its own help, true to the provider/executor it explains ────────

def _topic(name: str) -> str:
    return HELP.split(f"    {name}: {{", 1)[1].split("\n    },\n", 1)[0]


def test_premium_services_points_to_the_usenet_help():
    premium = HELP.split("'premium-services': {", 1)[1].split("\n    },\n", 1)[0]
    assert ("Usenet uses your configured news servers rather than a debrid account. Open the Usenet card’s help "
            "for server configuration and download behavior.") in premium
    assert "connection count, and priority" not in premium


def test_the_usenet_help_states_what_the_integration_does():
    from integrations.usenet import definition as usenet
    from providers.premiumize.provider import NZB_AFTER_USENET, NZB_BEFORE_USENET

    text = _topic("usenet")
    assert "title: 'Usenet'," in text
    # Priority: SAB sorts ascending; the field's own help says the same.
    assert usenet.PRIORITY_HELP == "Lower values have priority." and usenet.MIN_PRIORITY == 0
    assert "a lower value is preferred, and 0 is the highest" in text
    assert usenet.MIN_CONNECTIONS == 1 and "from 1 upward" in text
    # Off by default; repair without unpacking; extraction stays DP's.
    assert usenet.definition.default_enabled is False and "It is off until you enable it." in text
    from executors.sabnzbd.client import PP_REPAIR_ONLY
    assert PP_REPAIR_ONLY == 1 and "verifies and repairs the downloaded files, but never unpacks them" in text
    # NZB ordering: native Usenet first by default; the provider toggle puts it ahead.
    assert (NZB_AFTER_USENET, NZB_BEFORE_USENET) == (-1, 1)
    assert "By default an NZB goes to Usenet first" in text
    for label in ("Use TorBox Before Usenet", "Use Premiumize Before Usenet"):
        assert f"tuningToggle('{label.split()[1].lower()}_use_before_usenet', '{label}'" in PAGE
        assert label in text
    # The per-article retry is not the download retry count.
    assert "(not the download retry count)" in text
    # No claim the source does not make: no article-level failover between
    # servers or services, and the bundled service is never named.
    for unproven in ("missing article", "backup server", "fill server", "failover"):
        assert unproven not in text.lower(), unproven


# ── Services: the five premium provider topics and the header hints ─────────

def _provider_options():
    from providers.alldebrid.definition import AllDebridOptions
    from providers.debridlink.definition import DebridLinkOptions
    from providers.premiumize.definition import PremiumizeOptions
    from providers.realdebrid.definition import RealDebridOptions
    from providers.torbox.definition import TorBoxOptions
    return {"alldebrid": AllDebridOptions(), "debridlink": DebridLinkOptions(), "premiumize": PremiumizeOptions(),
            "realdebrid": RealDebridOptions(), "torbox": TorBoxOptions()}


@pytest.mark.parametrize("identity", ["alldebrid", "debridlink", "premiumize", "realdebrid", "torbox"])
def test_each_provider_topic_states_its_own_defaults_from_its_own_options_model(identity):
    """Every default a provider topic names is that provider's own schema
    default -- Prepare Backup Torrents has no one default across providers."""
    options = _provider_options()[identity]
    text = _topic(identity)
    backup = "on" if options.prepare_backup_torrents else "off"
    assert f"Prepare Backup Torrents ({backup} by default" in text
    assert ("on by default" if backup == "off" else "off by default, unlike") not in text.split("Prepare Backup Torrents", 1)[1][:20]
    if hasattr(options, "use_before_usenet"):
        label = {"premiumize": "Use Premiumize Before Usenet", "torbox": "Use TorBox Before Usenet"}[identity]
        assert f"{label} ({'on' if options.use_before_usenet else 'off'} by default)" in text
    if hasattr(options, "rate_limit_per_minute"):
        assert f"API Calls per Minute ({options.rate_limit_per_minute} by default)" in text
    else:
        assert "there is no API Calls per Minute setting" in text
    if hasattr(options, "request_timeout_seconds"):
        assert f"Request Timeout ({options.request_timeout_seconds} seconds by default)" in text
        upload = getattr(options, "upload_timeout_seconds", None) or options.torrent_upload_timeout_seconds
        assert f"Upload Timeout ({upload} seconds)" in text
        assert f"Supported Host Refresh Interval ({options.host_refresh_interval_hours} hours)" in text
    # The topic is about THIS provider, and saves/promises nothing it does not.
    assert f"title: '{ {'alldebrid': 'AllDebrid', 'debridlink': 'Debrid-Link', 'premiumize': 'Premiumize', 'realdebrid': 'Real-Debrid', 'torbox': 'TorBox'}[identity] }'," in text
    assert "$" not in text and "price" not in text.lower() and "guarantee" not in text.lower()


def test_provider_limits_named_in_help_are_the_enforced_ones():
    from providers.alldebrid.account import ACTIVE_MAGNET_MAXIMUM
    from providers.realdebrid.rate_limit import SERVICE_LIMIT_PER_MINUTE as REAL_DEBRID_LIMIT
    from providers.torbox.rate_limit import SERVICE_LIMIT_PER_MINUTE as TORBOX_LIMIT
    assert f"documented maximum of {ACTIVE_MAGNET_MAXIMUM} active magnets" in _topic("alldebrid")
    assert f"Real-Debrid documents a maximum of {REAL_DEBRID_LIMIT}" in _topic("realdebrid")
    assert f"TorBox documents a maximum of {TORBOX_LIMIT}" in _topic("torbox")


def test_plan_dependent_claims_are_qualified_and_torbox_promises_no_usenet():
    torbox, premiumize = _topic("torbox"), _topic("premiumize")
    # Documented-only plan facts say so; NZBs are never promised.
    assert "per TorBox’s documentation that is the Pro plan" in torbox
    assert "NZBs only where your plan includes Usenet" in torbox
    assert "Whether a particular NZB succeeds is TorBox’s decision for your account." in torbox
    assert "TorBox is separate from your Usenet service" in torbox
    for promise in ("supports Usenet", "TorBox Usenet works", "will download NZBs"):
        assert promise not in torbox
    assert "as Premiumize documents them for premium accounts" in premiumize
    assert "What a free or expired account can do through Premiumize’s API is not documented" in premiumize
    assert "Debrid-Link does not document seedbox use for free accounts" in _topic("debridlink")
    assert "Per AllDebrid’s documentation, magnets and .torrent files need a premium account." in _topic("alldebrid")
    assert "Per Real-Debrid’s documentation, torrents need a premium account." in _topic("realdebrid")
    # The Usenet topic qualifies the same claim.
    assert "TorBox and Premiumize can also take NZBs where your plan with them includes it, per each provider’s documentation." in _topic("usenet")
    # Test is described as it is implemented: it saves nothing.
    for identity in ("alldebrid", "debridlink", "premiumize"):
        assert "or the saved key when the field is empty" in _topic(identity) and "It saves nothing" in _topic(identity)


def test_the_eight_services_headers_carry_one_line_hints_and_the_tiles_none():
    hints = {
        "Usenet": "Download NZB content from configured Usenet news servers.",
        "AllDebrid": "Resolve supported links and torrents through your AllDebrid account.",
        "Debrid-Link": "Resolve supported links and torrents through your Debrid-Link account.",
        "Premiumize": "Resolve supported links and torrents through your Premiumize account; NZBs where your plan allows.",
        "Real-Debrid": "Resolve supported links and torrents through your Real-Debrid account.",
        "TorBox": "Resolve supported links and torrents through your TorBox account; NZBs where your plan allows.",
    }
    for text in hints.values():
        assert PAGE.count(f"headerCopy: '{text}'") == 1, text
    assert "links, torrents, and NZBs through your" not in PAGE
    assert "hint: 'Connect your Usenet servers and the debrid services you have accounts with.'," in PAGE
    assert "hint: 'Download directly from network sources; no premium account needed.'," in PAGE
    # Every provider header and both group headers are the hinted grammar; a
    # protocol tile is not a card header and gets none of it.
    card = PAGE.split("function providerCard(", 1)[1].split("\n  }\n", 1)[0]
    assert "dp-settings-card-header${headerCopy ? ' dp-settings-card-header--hinted' : ''}" in card
    group = PAGE.split("function groupCard(", 1)[1].split("\n  }\n", 1)[0]
    assert "${hinted ? ' dp-settings-card-header--hinted' : ''}" in group
    tile = PAGE.split("function sourceProtocolBox(", 1)[1].split("\n  }\n", 1)[0]
    for absent in ("helpChip", "data-tooltip", "hinted", "header-copy", "data-settings-help"):
        assert absent not in tile, absent
    # The hint is measured, not a breakpoint: one fit owner, kept current.
    fit = PAGE.split("function fitHeaderHints() {", 1)[1].split("\n  }\n", 1)[0]
    assert "chip.dataset.tooltip = fits ? chip.getAttribute('aria-label') : hint.textContent.trim();" in fit
    assert "tracks === 3 && (width - hint.getBoundingClientRect().width) / 2 >= side + gap" in fit
    assert "new ResizeObserver(() => headerHintFit())" in fit
    assert "headerHintFit?.();" in PAGE


# --- Downloads: the nine card headers, their Help, and the Network Sources tiles

def _block(source: str, start: str) -> str:
    return source.split(start, 1)[1].split("\n  }\n", 1)[0]


def test_the_nine_downloads_headers_are_the_hinted_header_grammar():
    css = (STATIC / "ui-settings-page.css").read_text(encoding="utf-8")
    work_css = (STATIC / "ui-settings-executor-work.css").read_text(encoding="utf-8")
    panel = _block(PAGE, "function downloadsPanel(s) {")
    # card() takes the grammar only when asked; nothing else changes for it.
    card = _block(PAGE, "function card(title, body, options = {}) {")
    assert "<div class=\"card-header${options.hinted ? ' dp-settings-card-header dp-settings-card-header--hinted' : ''}\">" in card
    assert "${options.hinted ? `<span class=\"dp-settings-card-title-group\">${identity}</span>` : identity}" in card
    # Download Behavior & Limits and Disk Space & Recovery, the two card() headers.
    assert panel.count("hinted: true,") == 2 and panel.count("help: 'disk-recovery', hinted: true,") == 1
    # The two hints this pass adds; the four that were there are unchanged.
    assert "hint: 'Tune how each supported download method transfers files.'," in panel
    assert "Protect free disk space and recover failed or stalled downloads." in panel
    for kept in ("Where DebridPulse saves downloads and how many it runs at once.",
                 "View current download engine jobs and intervene when something is stuck.",
                 "'How DebridPulse handles downloads from direct network sources.'",
                 "'How DebridPulse transfers files using rsync.'", "'Which files DebridPulse collects from WebDAV folders.'",
                 "'Global download behavior shared by all Usenet servers.'",
                 "'Downloads supported web-hosted media in its native form, without transcoding.'"):
        assert kept in PAGE, kept
    tuning = _block(PAGE, "function executorTuningCard(")
    assert '<div class="card-header dp-settings-card-header dp-settings-card-header--hinted">' in tuning
    # Download Engine Activity: the canonical header, its rail Help then Refresh.
    work = _block(PAGE, "function executorWorkCard() {")
    assert '<div class="card-header dp-settings-card-header dp-settings-card-header--hinted">' in work
    rail = work.split('<div class="dp-settings-card-header-controls">', 1)[1].split("</div>", 1)[0]
    assert rail.index("helpChip('engine-activity'") < rail.index("data-dp-executor-work-refresh")
    # No private header grid survives beside the canonical one.
    for retired in (".dp-settings-download-engine-card > .card-header", "minmax(420px, 900px)"):
        assert retired not in css, retired
    for retired in ("dp-executor-work-header-actions", ".dp-executor-work-card > .card-header"):
        assert retired not in work_css and retired not in PAGE, retired
    # One measured fit owner serves every hinted header, on the header's own midpoint.
    fit = _block(PAGE, "function fitHeaderHints() {")
    assert "root()?.querySelectorAll('.dp-settings-card-header--hinted')" in fit
    assert "hint: header.querySelector(':scope > .dp-settings-card-header-center > :only-child')," in fit
    assert PAGE.count("new ResizeObserver(() => headerHintFit())") == 1
    assert "#view-settings .dp-settings-card-header--hinted > .dp-settings-card-header-center > * {\n  white-space: nowrap;" in css


def test_the_network_sources_tiles_are_centred_rows_not_a_capacity_grid():
    css = (STATIC / "ui-settings-page.css").read_text(encoding="utf-8")
    grid = css.split("#view-settings .dp-settings-source-box-grid {", 1)[1].split("}", 1)[0]
    for declaration in ("display: flex;", "flex-wrap: wrap;", "justify-content: center;", "gap: 12px;"):
        assert declaration in grid, declaration
    for retired in ("grid-template-columns", "auto-fill", "auto-fit"):
        assert retired not in grid, retired
    box = css.split("#view-settings .card.dp-settings-source-box {", 1)[1].split("}", 1)[0]
    assert "flex: 0 1 210px;" in box and "width: 210px;" in box and "max-width: 100%;" in box
    assert "margin-inline: auto" not in box


# The Media Downloads Help describes exactly the controls its card renders:
# label as rendered -> setting key, each in mediaTuning() / youtubePlayerClientsField().
MEDIA_CONTROLS = {
    "Target Resolution": "media_target_resolution",
    "Video Quality": "media_video_quality",
    "Preferred Video Codec": "media_video_codec",
    "Preferred Subtitle Language": "preferred_subtitle_language",
    "Concurrent Fragment Downloads": "media_concurrent_fragment_downloads",
    "YouTube Player Client": "media_youtube_player_clients",
}


def test_the_media_downloads_help_names_only_the_rendered_controls_and_no_conversion():
    renderer = _block(PAGE, "function mediaTuning(s) {") + _block(PAGE, "function youtubePlayerClientsField(options) {")
    rendered = dict((label, key) for key, label in re.findall(r"(?:selectField|input)\('([a-z_]+)', '([^']+)'", renderer))
    assert rendered == MEDIA_CONTROLS
    assert "const VIDEO_CODECS = Object.freeze([\n    ['auto', 'Auto'],\n    ['av1', 'AV1'],\n    ['hevc', 'HEVC (H.265)'],\n    ['h264', 'H.264'],\n  ]);" in PAGE
    topic = _topic("'tuning-media'")
    for label in MEDIA_CONTROLS:
        assert f"'{label} " in topic or f"'{label}," in topic, label
    assert "never re-encodes or transcodes media" in topic
    assert "There is no setting for the output container." in topic
    assert "It is a preference among what the site offers, never a conversion." in topic
    assert "among the bitrates the site offers" in topic
    for invented in ("Output Container", "Container Format", "convert to", "transcode to", "guarantee that"):
        assert invented not in topic, invented
    # The no-conversion statement is the executor's own: stream copy only.
    worker = (ROOT / "backend" / "executors" / "media" / "worker.py").read_text(encoding="utf-8")
    assert 'argv += ["-c", "copy"]' in worker


def test_each_transfer_method_topic_names_its_cards_controls_and_their_backend_defaults():
    topics = {name: _topic(f"'{name}'") for name in ("tuning-direct", "tuning-rsync", "tuning-webdav", "tuning-usenet")}
    expected = {
        "tuning-direct": ("Connections per Server", "from 1 to 16 (default 16)", "Segments per File", "from 1 to 64 (default 16)",
                          "Minimum Split Size", "from 1M to 1024M (default 10M)", "Continue Partial Downloads",
                          "Lowest Speed Limit", "Disk Cache", "(default 64M; 0 turns it off)", "File Allocation", "Fallocate (the default)"),
        "tuning-rsync": ("Partial Transfers (on by default)", "Compression (off by default)", "Preserve Modification Time (on by default)",
                         "5 to 300 seconds (default 30)", "30 to 3600 seconds (default 300)", "all subfolders (the default)"),
        "tuning-webdav": ("the current folder only (the default)", "from 1 to 10000 (default 10000)", "rejected whole",
                          "empty for no limit (the default), or 10 to 3600 seconds"),
        "tuning-usenet": ("from 0 to 4096 MB (default 1024)", "from 2 to 25 (default 3)", "from 5 to 300 seconds (default 30)",
                          "Direct Write (on by default)", "under Services, Usenet"),
    }
    for name, phrases in expected.items():
        for phrase in phrases:
            assert phrase in topics[name], (name, phrase)
    # The numbers are the backend's own option models.
    aria2 = (ROOT / "backend" / "executors" / "aria2" / "definition.py").read_text(encoding="utf-8")
    for source in ('split: int = Field(default=16, ge=1, le=64)', 'min_split_size: str = "10M"',
                   'max_connection_per_server: int = Field(default=16, ge=1, le=16)', 'disk_cache: str = "64M"',
                   '= "falloc"', 'lowest_speed_limit: str = "0"', 'continue_downloads: bool = True'):
        assert source in aria2, source
    rsync = (ROOT / "backend" / "executors" / "rsync" / "definition.py").read_text(encoding="utf-8")
    for source in ("partial_transfers: bool = True", "compression: bool = False", "preserve_modification_time: bool = True",
                   "connection_timeout_seconds: int = Field(default=30, ge=5, le=300)",
                   "transfer_timeout_seconds: int = Field(default=300, ge=30, le=3600)"):
        assert source in rsync, source
    assert 'directory_depth: DirectoryDepthSetting = "all"' in (ROOT / "backend" / "providers" / "general_rsync" / "definition.py").read_text(encoding="utf-8")
    webdav = (ROOT / "backend" / "providers" / "general_webdav" / "definition.py").read_text(encoding="utf-8")
    for source in ('directory_depth: DirectoryDepthSetting = "current"', "max_files: int = Field(default=10_000, ge=1, le=10_000)",
                   "collection_scan_timeout_seconds: int = Field(default=0, ge=0, le=3600)", "if 0 < value < 10:"):
        assert source in webdav, source
    usenet = (ROOT / "backend" / "integrations" / "usenet" / "definition.py").read_text(encoding="utf-8")
    for source in ("MIN_ARTICLE_CACHE_MB, MAX_ARTICLE_CACHE_MB = 0, 4096", "MIN_ACQUISITION_RETRIES, MAX_ACQUISITION_RETRIES = 2, 25",
                   "operation_timeout_seconds: int = Field(default=30, ge=5, le=300)", "direct_write: bool = True",
                   "max_acquisition_retries: int = Field(default=3,", "article_cache_megabytes: int = Field(default=1024,"):
        assert source in usenet, source
