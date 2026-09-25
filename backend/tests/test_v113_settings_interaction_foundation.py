"""DP 1.0.13 -- Settings interaction foundation + Services taxonomy cleanup.

Each bounded item is proved at its canonical owner:

1. Provider Status taxonomy: ``premium_service`` holds the debrid providers and
   then Usenet, which is its reserved LAST row; ``general_family`` -- now shown
   as Standard Services -- holds the one aggregate Network Sources family. Both
   the tier order and the order within a tier fall out of ``display_order``,
   whose reserved bands are declared by the integrations themselves.
2. The Usenet server-card collection centres on its own viewport and expands
   outward from that centre, including when it wraps.
3. Usenet owns no notification system: action RESULTS are the canonical toast
   owner's. Inline field validation is a different thing and survives as its
   own, narrower surface.
4/5. The AllDebrid card's ``Additional Settings`` disclosure and its action
   group share ONE row. The localized Save is gone: Test is the only action.
6b. A Usenet server card is NOT a credential transaction. Each control on it is
   classified by ITS OWN semantics and risk: ordinary scalars AND the password
   are changed-blur, SSL is an immediate reversible toggle, and erasing a
   stored credential is an explicit confirmed Clear. A card with no canonical
   id yet is the one deliberate exception -- record CREATION -- and it is the
   canonical persistence owner that asks the record's scope to perform it.
6/9. One canonical Settings field-persistence owner
   (``ui-settings-persistence.js``) provides baseline, dirty comparison, scoped
   dispatch, stale-response protection, success convergence, failure rollback
   and the ONE optional, integration-neutral record-materialization hook.
   Controls DECLARE their commit class; no page reimplements it.
10. The global ``Apply Settings`` control remains, but can no longer replay a
   migrated Services value over newer locally persisted state.

CREDENTIAL CONTRACT (DP 1.0.13 Services cleanup). Entry and replacement of a
credential is an ordinary value change and commits on changed blur through the
integration's existing scoped mutation; destructive removal is an explicit
confirmed Clear; Test tests and never saves. No secret is ever retained by the
browser as an accepted baseline.

The rendered geometry and the live persistence behaviour are proved against the
real application in ``frontend/browser/settings-providers-layout.spec.js``,
``settings-providers-persistence.spec.js`` and ``usenet-server-cards.spec.js``
(the one owner of the shared news-server collection); this module owns the
source/metadata contracts and the absence audits.
"""
import re
from pathlib import Path

import pytest

from integrations.catalog import definitions
from integrations.definition import IntegrationPresentation
from integrations.usenet.definition import definition as usenet_definition
from providers.alldebrid.definition import definition as alldebrid_definition
from providers.general_ftp.definition import definition as general_ftp_definition
from providers.general_http.definition import definition as general_http_definition

REPO = Path(__file__).resolve().parents[2]
STATIC = REPO / "frontend" / "static"
BACKEND = REPO / "backend"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


SETTINGS_JS = read("ui-settings-page.js")
USENET_JS = read("ui-settings-usenet-servers.js")
USENET_CSS = read("ui-settings-usenet-servers.css")
SETTINGS_CSS = read("ui-settings-page.css")
LANGUAGE_CSS = read("ui-universal-language.css")
INDEX_HTML = read("index.html")
PERSISTENCE_PATH = STATIC / "ui-settings-persistence.js"

MAINTAINED_JS = sorted(STATIC.glob("*.js"))
MAINTAINED_CSS = sorted(STATIC.glob("*.css"))

# The one declaration boundary used by every source assertion below: a slice
# from a named declaration up to the next top-level one in the same module.
_NEXT_DECLARATION = re.compile(r"(?m)^  (?:async function |function |const |window\.)")


def block(source: str, start: str) -> str:
    assert start in source, f"missing declaration: {start}"
    begin = source.index(start)
    tail = source[begin + len(start):]
    match = _NEXT_DECLARATION.search(tail)
    return source[begin:begin + len(start) + (match.start() if match else len(tail))]


def control(markup: str, marker: str) -> str:
    """The ONE element whose opening tag carries ``marker``.

    Deliberately the element and not a character window: server controls sit
    side by side, so a window around one of them reads the next one's
    attributes and would let a misclassified control pass.
    """
    assert marker in markup, f"missing control: {marker}"
    at = markup.index(marker)
    return markup[markup.rindex("<", 0, at):markup.index(">", at) + 1]


def rule(css: str, selector: str) -> str:
    assert selector in css, f"missing rule: {selector}"
    begin = css.index(selector)
    return css[begin:css.index("}", begin) + 1]


def enclosing_rule(css: str, needle: str) -> str:
    """The WHOLE rule -- selector list and body -- that contains ``needle``."""
    assert needle in css, f"missing declaration: {needle}"
    at = css.index(needle)
    return css[css.rfind("}", 0, at) + 1:css.index("}", at) + 1]


# --- Item 1: Services taxonomy and the reserved display_order bands -------

PREMIUM_SERVICE, GENERAL_FAMILY = "premium_service", "general_family"
RETIRED_TIER = "premium_family"
DEFAULT_ORDER = IntegrationPresentation().display_order


def test_usenet_is_a_premium_service():
    presentation = usenet_definition.presentation
    assert presentation.status_tier == PREMIUM_SERVICE
    assert presentation.status_tier_label == "Premium Services"
    assert presentation.premium is True


def test_no_integration_declares_the_retired_standalone_premium_tier():
    assert [d.id for d in definitions if d.presentation.status_tier == RETIRED_TIER] == []


def test_alldebrid_is_unchanged_by_the_taxonomy_move():
    presentation = alldebrid_definition.presentation
    assert presentation.status_tier == PREMIUM_SERVICE
    assert presentation.status_tier_label == "Premium Services"
    assert presentation.display_order == 10
    assert presentation.premium is True


def test_the_lower_tier_keeps_its_identity_and_changes_only_its_label():
    """``general_family`` is the internal tier identity and does not move; only
    what the operator reads changed."""
    members = [d for d in definitions if d.presentation.status_tier == GENERAL_FAMILY]
    assert {d.id for d in members} == {"general_http", "general_ftp"}
    for definition in members:
        assert definition.presentation.status_tier_label == "Standard Services", definition.id


def test_the_premium_tier_reserves_its_last_position_for_usenet():
    """Reserved BANDS, not merely today's values.

    Ordinary premium entries sort below the presentation model's DEFAULT order,
    so a future debrid integration that declares no order at all still lands
    before Usenet -- and Usenet still lands before the Standard band, which is
    what keeps Premium Services rendered before Standard Services even when
    Usenet is the only premium row.
    """
    premium = {d.id: d.presentation.display_order for d in definitions
               if d.presentation.status_tier == PREMIUM_SERVICE}
    assert set(premium) == {"alldebrid", "usenet"}
    ordinary = [order for identity, order in premium.items() if identity != "usenet"]
    assert max(ordinary) < DEFAULT_ORDER
    assert DEFAULT_ORDER < premium["usenet"], \
        "a future default-order premium entry would sort AFTER the reserved Usenet tail"
    standard = [d.presentation.display_order for d in definitions
                if d.presentation.status_tier == GENERAL_FAMILY]
    assert premium["usenet"] < min(standard)


def test_a_future_default_order_premium_entry_lands_before_usenet():
    """The renderer names neither integration: a tier takes the position of its
    first entry and entries arrive sorted by display_order, so this is decided
    entirely by the band an integration declares for itself."""
    future = IntegrationPresentation(status_name="Future Debrid", premium=True,
                                     status_tier=PREMIUM_SERVICE,
                                     status_tier_label="Premium Services")
    rows = sorted([(alldebrid_definition.presentation.display_order, "alldebrid"),
                   (usenet_definition.presentation.display_order, "usenet"),
                   (future.display_order, "future_debrid")])
    assert [identity for _, identity in rows] == ["alldebrid", "future_debrid", "usenet"]


def test_the_aggregate_family_is_now_network_sources():
    for definition in (general_http_definition, general_ftp_definition):
        assert definition.presentation.status_group == "direct_sources"
        assert definition.presentation.status_group_label == "Network Sources"


def test_the_network_source_members_carry_the_new_operator_facing_names():
    """One owner, two fields of the same definition: ``name`` is what the
    transfer-list badge reads and ``status_name`` is what Provider Status
    reads. Nothing durable is renamed."""
    assert general_http_definition.name == "HTTP(S)"
    assert general_http_definition.presentation.status_name == "HTTP(S)"
    assert general_ftp_definition.name == "(S)FTP"
    assert general_ftp_definition.presentation.status_name == "(S)FTP"
    assert general_http_definition.id == "general_http"
    assert general_ftp_definition.id == "general_ftp"


def test_the_transfer_list_badge_label_has_exactly_one_owner():
    """Downloads and Dashboard Recent read the name the backend stamped from
    the integration definition; neither renderer owns a protocol label map."""
    routes = (BACKEND / "api" / "routes.py").read_text(encoding="utf-8")
    assert "def _provider_display_name(" in routes
    assert "definition.name for definition in definitions" in routes
    app_js = read("app.js")
    presentation = app_js[app_js.index("function transferProviderPresentation("):]
    presentation = presentation[:presentation.index("\nfunction ", 1)]
    assert "current_provider_name" in presentation
    assert "delivering_provider_name" in presentation
    # The chip renders that label and nothing it derived itself.
    chip = app_js[app_js.index("function providerChip("):]
    chip = chip[:chip.index("\nfunction ", 1)]
    assert "transferProviderPresentation(t)" in chip
    assert "dp-provider-chip" in chip
    # Neither the label owner nor the row renderers hold a protocol label map.
    # (app.js is audited at its two owning functions: its submission copy names
    # protocols to the operator, which is neither a map nor a badge.)
    for named in ("HTTP(S)", "(S)FTP", "HTTP & HTTPS", "FTP & SFTP",
                  "general_http", "general_ftp"):
        assert named not in presentation, f"the label owner names a protocol: {named}"
        assert named not in chip, f"the chip names a protocol: {named}"
        for renderer in ("ui-downloads.js", "ui-dashboard-transfer-presentation.js"):
            assert named not in read(renderer), \
                f"{renderer} declares its own protocol label: {named}"


def test_no_source_file_still_names_the_retired_tier():
    sources = [p for p in BACKEND.rglob("*.py") if "/tests/" not in p.as_posix()]
    sources += list(STATIC.glob("*.js")) + list(STATIC.glob("*.css"))
    offenders = [p.relative_to(REPO).as_posix() for p in sources
                 if RETIRED_TIER in p.read_text(encoding="utf-8")]
    assert offenders == [], offenders
    # Positive control: the same search DOES find the surviving tier ids, so an
    # empty result above is evidence of absence rather than a broken search.
    survivors = [p for p in sources if GENERAL_FAMILY in p.read_text(encoding="utf-8")]
    assert survivors, "absence audit searched nothing"


def test_the_status_renderer_still_names_no_integration_and_no_tier():
    status_js = read("ui-provider-status.js")
    for named in ("alldebrid", "usenet", "general_http", "general_ftp",
                  PREMIUM_SERVICE, GENERAL_FAMILY, "Premium Services",
                  "Standard Services", "Network Sources"):
        assert named not in status_js, named


# --- Item 1 (presentation): the Services page taxonomy --------------------

def test_the_settings_tab_is_presented_as_services_and_keeps_its_key():
    tabs = block(SETTINGS_JS, "const TABS")
    assert "['sources', 'Services'" in tabs
    assert "Sources & Providers" not in tabs


def test_the_premium_group_card_is_premium_services():
    panel = block(SETTINGS_JS, "function sourcesPanel(")
    assert "groupCard('Premium Services'" in panel
    assert "External Providers" not in panel


def test_the_aggregate_group_label_falls_back_to_network_sources():
    panel = block(SETTINGS_JS, "function sourcesPanel(")
    assert "'Network Sources'" in panel
    assert "'General Sources'" not in panel


def test_the_downloads_executor_tuning_family_matches_the_services_name():
    panel = block(SETTINGS_JS, "function downloadsPanel(")
    assert "executorTuningCard('direct', 'Network Sources'" in panel
    assert "'General Sources'" not in panel


def test_the_services_page_renders_the_new_network_source_names():
    """The operator-facing names are the ones the INTEGRATIONS publish.

    The final pass removed the two hard-coded child cards: members are derived
    from the group they declare and labelled from their own
    ``presentation.status_name``, so HTTP(S) and (S)FTP reach the page without
    this renderer naming either of them, and neither name can drift from the
    Provider Status panel's."""
    panel = block(SETTINGS_JS, "function sourcesPanel(")
    assert "providerCard('general_http'" not in panel
    assert "providerCard('general_ftp'" not in panel
    assert "entry.presentation?.status_name || id" in panel, \
        "the box label is not taken from the integration's own presentation"
    assert "entry?.presentation?.status_group === groupId" in panel, \
        "membership is not derived from the metadata the members publish"
    assert "HTTP & HTTPS" not in panel
    assert "FTP & SFTP" not in panel
    # The durable identities remain the only hard-coded reference, and only as
    # the copy table's keys.
    copy = block(SETTINGS_JS, "const SOURCE_BOX_COPY")
    assert "general_http:" in copy and "general_ftp:" in copy


def test_usenet_stays_first_in_settings_premium_services():
    """Settings order is intentionally INDEPENDENT of Provider Status order:
    the operator configures Usenet first, and the panel reports it last."""
    panel = block(SETTINGS_JS, "function sourcesPanel(")
    assert "usenetCard + " in panel
    assert panel.index("usenetCard +") < panel.index("provider,")


def test_an_inset_separator_groups_usenet_apart_from_the_debrid_providers():
    panel = block(SETTINGS_JS, "function sourcesPanel(")
    assert "usenetCard + PREMIUM_SEPARATOR + provider" in panel
    separator = block(SETTINGS_JS, "const PREMIUM_SEPARATOR")
    assert "dp-settings-group-separator" in separator
    rule_text = rule(SETTINGS_CSS, "#view-settings .dp-settings-group-separator {")
    # Inset and partial width -- within the content bounds, never full bleed.
    assert "width:" in rule_text
    assert "100%" not in rule_text
    for banned in ("position: absolute", "margin-left: -", "margin-inline-start: -"):
        assert banned not in rule_text, banned
    # It is a rule, not a second section/heading.
    for banned in ("<h1", "<h2", "<h3", "card-title", "card-header", "groupCard"):
        assert banned not in separator, banned


# --- Item 2: Usenet collection geometry ------------------------------------

def test_the_server_collection_centres_on_its_own_viewport():
    collection = rule(USENET_CSS, ".dp-usenet-servers {")
    assert "flex-wrap: wrap" in collection
    assert "justify-content: center" in collection
    # Centring is the layout's own behaviour, never a measured offset.
    for banned in ("margin-left", "margin-inline-start", "position: absolute", "transform"):
        assert banned not in collection, banned


# --- Item 3: Usenet owns no notification system ----------------------------

def test_usenet_owns_no_operation_result_notification_system():
    # The private state carrier, its tone vocabulary and its success/progress
    # rendering are gone -- not merely bypassed by an added toast call.
    for retired in ("data-usenet-status", "dp-usenet-server-status", "data-tone",
                    "dataset.tone", "function status(", "'Saving\u2026'", "'Testing\u2026'"):
        assert retired not in USENET_JS, retired
    # And the markup owner renders no such surface either.
    for retired in ("data-usenet-status", "dp-usenet-server-status"):
        assert retired not in SETTINGS_JS, retired
    assert "dp-usenet-server-status" not in USENET_CSS
    assert 'data-tone="ok"' not in USENET_CSS
    # Positive control: the files really were read.
    assert "data-usenet-collection" in USENET_JS
    assert "dp-usenet-servers" in USENET_CSS


def test_every_usenet_action_result_goes_to_the_canonical_toast_owner():
    for action in ("async function createServer(", "async function test(",
                   "async function removeCard(", "async function clearPassword("):
        body = block(USENET_JS, action)
        assert "toast(" in body, action


def test_inline_field_validation_survives_as_its_own_narrower_surface():
    # Validation is NOT the removal target: it explains a malformed field, it
    # never reports the result of an operation.
    assert "data-usenet-validation" in USENET_JS
    assert "data-usenet-validation" in SETTINGS_JS
    assert "A server host is required." in USENET_JS
    validation = block(USENET_JS, "function validation(")
    assert "toast" not in validation


# --- Item 6b: a server card is classified per CONTROL, never as a record ---

ORDINARY_SERVER_FIELDS = ("host", "port", "username", "connections", "priority",
                          "articles_per_request", "timeout_seconds")

# The two markup owners of one server card: the rendered card and the blank one
# added by Add Server. Sliced to the card itself so a selector string elsewhere
# in either file can never be mistaken for a control.
SERVER_CARDS = (
    ("rendered card", block(SETTINGS_JS, "function usenetServerCard(")),
    ("blank card", block(USENET_JS, "function blankCard(")),
)


def test_no_owner_classifies_a_whole_server_card_as_a_credential_transaction():
    """The retired claim: "every server card is a credential-bearing RECORD,
    so its commit class is gated-save". A control's commit boundary is decided
    by what THAT control is, never by what shares its card."""
    for retired in ("credential-bearing RECORD", "its commit class is gated-save",
                    "no field of it persists on blur"):
        assert retired not in USENET_JS, retired
    # Positive control: the file really was read.
    assert "data-usenet-collection" in USENET_JS


def test_every_ordinary_server_field_is_changed_blur_through_the_canonical_owner():
    for label, markup in SERVER_CARDS:
        for field in ORDINARY_SERVER_FIELDS:
            element = control(markup, f'data-usenet-field="{field}"')
            assert 'data-commit="changed-blur"' in element, field
            assert f'data-commit-key="{field}"' in element, field
            assert 'data-commit-scope="usenet-server"' in element, field


def test_ssl_is_an_immediate_reversible_toggle():
    for label, markup in SERVER_CARDS:
        element = control(markup, 'data-usenet-field="ssl"')
        assert 'data-commit="immediate"' in element
        assert 'data-commit="changed-blur"' not in element
    # It has its own committed write, exactly like every other immediate control.
    handler = block(USENET_JS, "async function sslChanged(")
    assert "writeServer(" in handler
    # Switching transport carries the conventional port it just followed:
    # one operator action, one write.
    assert "followSslPort(card)" in handler


def test_the_password_is_an_ordinary_changed_blur_replacement():
    """DP 1.0.13: entering or replacing a credential is an ordinary value
    change. It commits on changed blur, through the SAME per-record scope every
    other field of the card uses -- there is no second credential writer and no
    localized Save."""
    for label, markup in SERVER_CARDS:
        password = control(markup, 'data-usenet-field="password"')
        assert 'data-commit="changed-blur"' in password, label
        assert 'data-commit-key="password"' in password, label
        assert 'data-commit-scope="usenet-server"' in password, label
        assert 'data-commit="gated-save"' not in password, label


def test_no_gated_save_class_survives_anywhere_in_settings():
    """The class is retired for credentials, so no control may still declare
    it -- a dormant declaration is a second contract."""
    for source in (SETTINGS_JS, USENET_JS):
        assert 'data-commit="gated-save"' not in source


def test_no_owner_renders_a_save_action_for_a_usenet_server():
    for label, markup in SERVER_CARDS:
        assert 'data-usenet-action="save"' not in markup, label
    assert 'data-usenet-action="save"' not in USENET_JS
    assert "function gatedIntent(" not in USENET_JS
    assert "function refreshGate(" not in USENET_JS
    assert "async function save(" not in USENET_JS


def test_erasing_a_stored_credential_is_an_explicit_confirmed_clear():
    """Both renderings of the card expose exactly one destructive control, and
    the card itself carries NO confirmation representation: the one canonical
    Settings confirmation asks the question when the operator acts."""
    for label, rendered in SERVER_CARDS:
        action = control(rendered, 'data-usenet-action="clear-password"')
        assert "btn-danger" in action, f"{label}: the canonical destructive treatment is not used"
        # It is NOT pre-disabled: the action is available whenever there is
        # something stored to clear, and the dialog is the gate.
        assert "disabled" not in action, f"{label}: the action still carries a local gate"
        group = rendered[rendered.index("dp-usenet-clear-password"):]
        group = group[:group.index("</div>")]
        assert "Clear Password" in group, label
        assert 'type="checkbox"' not in group, label
        assert "<label" not in group, label
    # The retired gated checkbox and every trace of its copy are gone.
    assert "<span>Clear the stored password for this server</span>" not in SETTINGS_JS
    assert "dp-settings-inline-check dp-usenet-clear-password" not in SETTINGS_JS
    assert "data-usenet-clear-password" not in SETTINGS_JS
    assert "data-usenet-clear-password" not in USENET_JS
    assert "Confirm removal of the stored password for this server" not in SETTINGS_JS
    assert "Confirm removal of the stored password for this server" not in USENET_JS


def test_the_clear_action_writes_only_the_removal_and_is_confirmation_gated():
    body = block(USENET_JS, "async function clearPassword(")
    assert "{clear_password: true}" in body, "Clear must carry only the removal"
    assert "password:" not in body.replace("clear_password", ""), \
        "Clear must never also save a replacement credential"
    # The ONE canonical confirmation is the gate, and declining it returns
    # BEFORE anything is settled, written or converged.
    assert "window.DPSettingsModal.confirm(" in body
    assert "tone: 'danger'" in body
    assert "confirmLabel: 'Clear Password'" in body
    gate = body.index("if (!confirmed) return;")
    assert gate < body.index("writeServer("), "the write is not behind the confirmation"
    assert gate < body.index("settle("), "a commit is settled before the operator has agreed"
    # Identity: the name the operator chose, else the host, else neither.
    assert "confirmSubject(card)" in body
    failure = body[body.index("} catch (error) {"):]
    assert "toast(" in failure, "a failed clear must report itself"
    assert "converge(" not in failure, "a failed clear must not converge the card"


def test_a_blank_password_is_never_a_clear():
    """The persistence owner never commits a draft equal to its baseline, and a
    rendered credential field is always blank -- so leaving one blank writes
    nothing at all. The backend states the same rule for the value it does
    receive."""
    persistence = PERSISTENCE_PATH.read_text(encoding="utf-8")
    commit = block(persistence, "function commit(")
    assert "draft === baselines.get(key)" in commit
    servers = (BACKEND / "integrations" / "usenet" / "servers.py").read_text(encoding="utf-8")
    assert 'means "keep the stored one for THIS server"' in servers


def test_a_credential_is_never_retained_as_an_accepted_baseline():
    """A scope returns the ACCEPTED value, which the owner records as the
    baseline. For a secret that accepted value is blank, so the browser holds
    no credential and the field returns to its blank/configured presentation."""
    scope = block(USENET_JS, "function registerServerScope(")
    assert "SECRET_FIELDS" in scope
    assert "return '';" in scope
    assert "SECRET_FIELDS = new Set(['password'])" in USENET_JS


def test_a_new_card_cannot_field_commit_before_its_record_exists():
    # The record container carries the canonical instance; a blank card has none.
    assert 'data-commit-instance=""' in USENET_JS
    assert "data-commit-instance" in SETTINGS_JS
    # Once the backend mints the id the card joins the universal model.
    created = block(USENET_JS, "function adoptServerId(")
    assert "data-commit-instance" in created or "commitInstance" in created
    assert "DPSettingsPersistence.adopt" in USENET_JS


def test_the_usenet_scope_writes_exactly_one_field_of_exactly_one_record():
    scope = block(USENET_JS, "function registerServerScope(")
    assert "defineScope(SERVER_SCOPE" in scope
    assert "{[key]: committedValue(key, draft)}" in scope, \
        "the scope must carry only the field that changed"
    # One record has exactly one writer, addressed by its canonical id.
    writer = block(USENET_JS, "function requestServerWrite(")
    assert "/usenet/servers/" in writer and "serverId(card)" in writer
    assert USENET_JS.count("api('PUT'") == 1, "a record must have exactly one writer"


def test_an_action_converges_only_the_controls_it_wrote():
    """Settling before dispatch orders what existed BEFORE the request; it says
    nothing about an edit made while that request is in flight. An action
    therefore projects the accepted record over ONLY the controls it actually
    wrote, and only while they still hold what it sent."""
    assert "function adoptServer(" not in USENET_JS, \
        "a whole returned record is projected over controls the action never wrote"
    body = block(USENET_JS, "function converge(")
    # Scoped to what this action sent, never the whole record.
    assert "sent" in body
    assert "card.querySelector(`[data-usenet-field=" in body
    # A newer draft is kept, and made dirty against the accepted baseline so it
    # commits on its own blur rather than being silently swallowed.
    assert "DPSettingsPersistence.accept(" in body
    # A credential is never projected back into the browser -- but its ACCEPTED
    # presentation, which for a secret is blank, still becomes the baseline.
    assert "SECRET_FIELDS.has(key)" in body
    assert "DPSettingsPersistence.accept(node, '')" in body


def test_every_record_write_shares_that_record_lane():
    """Immediate, destructive and dialog-committed writes mutate the SAME record
    as a field commit, so they cannot be allowed to overlap it or each other."""
    writer = block(USENET_JS, "function writeServer(")
    assert "DPSettingsPersistence.perform(" in writer
    assert "SERVER_SCOPE" in writer and "serverId(card)" in writer
    # The field-commit scope is already ON that lane, so it issues the request
    # directly -- queueing it behind itself would deadlock the record.
    scope = block(USENET_JS, "function registerServerScope(")
    assert "requestServerWrite(" in scope
    assert "perform(" not in scope
    # And there is still exactly one place a record is written.
    assert USENET_JS.count("api('PUT'") == 1


def test_the_persistence_owner_owns_record_ordering_and_acceptance():
    source = PERSISTENCE_PATH.read_text(encoding="utf-8")
    # One lane per record, shared by field commits and owner-driven writes.
    perform = block(source, "function perform(")
    assert "chains" in perform and "instance" in perform
    assert "outstanding" in perform, "an owner action must be settled like any commit"
    # An owner that converged a control records what the server accepted.
    accept = block(source, "function accept(")
    assert "baselines.set(" in accept
    assert "window.DPSettingsPersistence" in source
    for name in ("perform", "accept"):
        assert name in source[source.index("window.DPSettingsPersistence"):], name


def test_record_creation_consumes_only_the_credential_it_dispatched():
    """A credential typed while the record was being minted is NEWER intent: it
    stays on screen and commits on its own blur. Only what creation actually
    carried is cleared."""
    consume = block(USENET_JS, "function consumeCarriedCredential(")
    assert "dispatched" in consume
    assert "field.value === dispatched" in consume
    assert "consumeCarriedCredential(card, server.password || '')" in USENET_JS
    # And nothing clears the credential unconditionally any more.
    assert "if (field) field.value = '';" not in USENET_JS


def test_the_alldebrid_credential_row_never_replaces_the_control_it_converges():
    """The row has to change -- whether a key is present decides whether the
    Clear action exists at all -- but the INPUT is never replaced. Destroying a
    control the operator may still be editing would remove focus from it, which
    IS its commit boundary, so this owner's own re-render would persist a draft
    they had not finished."""
    render = block(SETTINGS_JS, "function renderAllDebridCredential(")
    assert "dispatched" in render
    # Only the draft this write carried is consumed.
    assert "String(field.value ?? '') === String(dispatched ?? '')" in render
    # The variable parts are rebuilt from the SAME declarations the row is
    # rendered from, so the row keeps one markup owner in either direction.
    for fragment in ("ALLDEBRID_KEY_PLACEHOLDER(", "ALLDEBRID_KEY_META(", "ALLDEBRID_KEY_CLEAR"):
        assert fragment in render, fragment
        assert fragment.rstrip("(") in block(SETTINGS_JS, "function allDebridApiKeyField(")
    # Nothing replaces or re-parents the control.
    for banned in ("outerHTML", "replaceWith", "replaceChild", "cloneNode"):
        assert banned not in render, banned


def test_every_usenet_action_settles_pending_field_commits_first():
    for action in ("async function clearPassword(", "async function test(",
                   "async function sslChanged(", "async function rename("):
        body = block(USENET_JS, action)
        assert "settle(" in body, action


def test_the_canonical_toast_owner_is_the_only_notification_owner_on_this_page():
    assert "window.toast" in read("ui-toast-contract.js")


# --- Items 4/5/13: the provider card's operational header rail -------------

def test_the_header_rail_reads_state_then_action_then_participation():
    """A provider-level action belongs beside the state it proves and the
    participation control it is about -- not in the body, where its position
    would depend on the body's geometry."""
    card = block(SETTINGS_JS, "function providerCard(")
    controls = card[card.index("dp-settings-card-header-controls"):]
    assert controls.index("dp-settings-provider-config-status") \
        < controls.index("dp-settings-header-action") \
        < controls.index("${enable}"), "the rail is not state -> action -> Enable"


def test_the_header_action_slot_is_neutral_and_optional():
    """The slot is the CARD's grammar, not any one provider's: it is named for
    what it is, it is rendered only when the card has such an action, and it is
    applied to no card that does not ask for one."""
    card = block(SETTINGS_JS, "function providerCard(")
    assert "headerAction = ''" in card, "the slot is not optional"
    assert "headerAction ? " in card, "an empty slot still emits a wrapper"
    assert "alldebrid" not in card.lower(), "the generic card names a provider"
    panel = block(SETTINGS_JS, "function sourcesPanel(")
    # Exactly the cards that HAVE a provider-level action ask for the slot:
    # AllDebrid and Usenet. The Network Sources group card does not.
    assert panel.count("headerAction:") == 2, \
        "the rail was applied to a card that did not ask for it"


def test_the_test_action_lives_in_the_header_and_not_in_the_body():
    """It is not in the credential row (it saves nothing) and not in the
    optional-tuning disclosure (its position must not depend on that being
    open). There is no provider action footer left at all."""
    panel = block(SETTINGS_JS, "function sourcesPanel(")
    assert panel.count("providerTestAction('test-alldebrid')") == 1
    assert "headerAction: providerTest" in panel
    body = panel[panel.index("dp-settings-copy"):panel.index("`, allDebrid, {")]
    assert "test-alldebrid" not in body, "Test is still rendered in the card body"
    # The retired footer machinery is gone, not merely unused.
    assert "dp-settings-provider-actions" not in SETTINGS_JS
    assert "dp-settings-provider-advanced" not in SETTINGS_JS
    assert "dp-settings-provider-actions" not in SETTINGS_CSS
    assert "dp-settings-provider-advanced" not in SETTINGS_CSS


def test_the_header_rail_geometry_has_one_owner_and_no_positioning_hack():
    owners = [p.name for p in MAINTAINED_CSS
              if "dp-settings-header-action" in p.read_text(encoding="utf-8")]
    assert owners == ["ui-settings-page.css"], owners
    rail = rule(SETTINGS_CSS,
                "#view-settings .dp-settings-card-header > .dp-settings-card-header-controls {")
    assert "display: flex" in rail
    # Narrow layouts reflow; they do not overflow, and wrapping preserves the
    # rail's order because the order is the source order.
    assert "flex-wrap: wrap" in rail
    assert "align-items: center" in rail
    slot = rule(SETTINGS_CSS, "#view-settings .dp-settings-header-action {")
    for banned in ("position: absolute", "margin", "top:", "transform"):
        assert banned not in slot, banned


def test_no_localized_save_action_survives_for_the_alldebrid_credential():
    """Item 13: the localized Save is gone entirely, not merely hidden."""
    assert 'data-action="save-alldebrid"' not in SETTINGS_JS
    for retired in ("function saveAllDebridCredentials(", "function allDebridGatedIntent(",
                    "function refreshGatedSave(", "function scopedClears("):
        assert retired not in SETTINGS_JS, retired
    panel = block(SETTINGS_JS, "function sourcesPanel(")
    for banned in (">Save", "Save Settings", 'data-action="save', "btn-save"):
        assert banned not in panel, banned


def test_the_alldebrid_additional_settings_are_five_tuning_cells():
    """Tuning-only behind its disclosure: the five existing controls, each still
    bound to the same canonical setting, in the reusable tuning grid. The
    disclosure row carries no Test, no Save and no action footer."""
    panel = block(SETTINGS_JS, "function sourcesPanel(")
    additional = panel[panel.index("dp-settings-additional"):panel.index("`, allDebrid, {")]
    assert "tuningCells(" in additional
    for field in ("alldebrid_rate_limit_per_minute", "poll_interval_seconds",
                  "full_sync_interval_minutes", "upload_fail_retry_count",
                  "upload_fail_retry_delay_minutes"):
        assert field in additional, field
    assert additional.count("input(") == 5, "the collection does not hold exactly five cells"
    for banned in ("test-alldebrid", "Save", "dp-settings-provider-actions"):
        assert banned not in additional, banned


def test_the_tuning_grid_is_a_neutral_bounded_reusable_primitive():
    """Compact cells the row FITS rather than fills, wrapping responsively with
    no horizontal scroll, and centring each cell's label/control/help as
    ELEMENTS while the value inside the control keeps canonical alignment.

    It is a centred wrapping FLEX line, deliberately: a flex line centres every
    row including a partial one, which a grid does not -- a grid's partial last
    row sits in its own columns."""
    owners = [p.name for p in MAINTAINED_CSS
              if "dp-settings-tuning-grid" in p.read_text(encoding="utf-8")]
    assert owners == ["ui-settings-page.css"], owners
    grid = rule(SETTINGS_CSS, "#view-settings .dp-settings-tuning-grid {")
    assert "display: flex" in grid
    assert "flex-wrap: wrap" in grid
    assert "justify-content: center" in grid
    # A cell is a cell wherever it sits -- directly on the line, or inside a
    # relationship group -- so the rule is a descendant one.
    cell = rule(SETTINGS_CSS, "#view-settings .dp-settings-tuning-grid .dp-settings-field {")
    assert "flex: 0 1 170px" in cell, \
        "the basis is not bounded, so a cell can stretch to fill the row"
    assert "max-width: 190px" in cell
    assert "justify-items: center" in cell
    assert "text-align: center" in cell
    control = rule(SETTINGS_CSS,
                   "#view-settings .dp-settings-tuning-grid .dp-settings-field > .input,")
    assert "text-align: left" in control, \
        "the cell's centring leaked into the value inside the control"
    assert "overflow-x" not in SETTINGS_CSS.split("dp-settings-tuning-grid")[1][:400]
    # The primitive names no provider.
    assert "alldebrid" not in grid.lower() and "alldebrid" not in cell.lower()


def test_a_relationship_group_never_draws_a_broken_outline():
    """DP 1.0.13: adjacent cells may carry a light shared outline, and ONLY at
    a width where their whole span demonstrably fits one row. Below that the
    group is not a layout box at all, so its cells wrap as ordinary cells and
    the relationship simply is not drawn -- never split across two rows.

    The decision is a container query on the collection's own inline size.
    Nothing measures geometry in JavaScript and nothing is re-parented."""
    group = rule(SETTINGS_CSS, "#view-settings .dp-settings-tuning-group {")
    assert "display: contents" in group, "the group is a layout box by default"
    grid = rule(SETTINGS_CSS, "#view-settings .dp-settings-tuning-grid {")
    assert "container-type: inline-size" in grid
    assert "container-name: dp-tuning" in grid
    # Each span has its own threshold, and the outline takes no space.
    for span in ("2", "3"):
        marker = f'.dp-settings-tuning-group[data-tuning-span="{span}"]'
        assert marker in SETTINGS_CSS, span
        rule_body = SETTINGS_CSS.split(marker + " {", 1)[1].split("}", 1)[0]
        assert "flex-wrap: nowrap" in rule_body, span
        assert "outline:" in rule_body and "outline-offset:" in rule_body, span
        assert "border:" not in rule_body, "an outline that takes layout space is a border"
    assert SETTINGS_CSS.count("@container dp-tuning (min-width:") == 2
    # No owner measures a cell, a row or a group in JavaScript.
    page = SETTINGS_JS
    for measurement in ("getBoundingClientRect", "offsetWidth", "clientWidth",
                        "getComputedStyle", "ResizeObserver"):
        assert measurement not in block(page, "function tuningCells("), measurement
        assert measurement not in block(page, "function tuningGroup("), measurement


# --- Items 6/9: one canonical persistence owner ----------------------------

def test_one_canonical_settings_persistence_owner_exists():
    assert PERSISTENCE_PATH.exists(), "ui-settings-persistence.js is missing"
    source = PERSISTENCE_PATH.read_text(encoding="utf-8")
    assert "window.DPSettingsPersistence" in source
    for capability in ("function adopt(", "function dirty(", "function commit(",
                       "function settle(", "function defineScope("):
        assert capability in source, capability
    # stale-response protection and failure rollback are the owner's, by name.
    assert "tokens" in source
    assert "baselines" in source


def test_the_persistence_owner_is_instance_scoped():
    """The same logical field exists once per RECORD (one Usenet server card
    per server), so baseline, staleness and serialization are keyed by
    scope + key + instance -- never by the field name alone."""
    source = PERSISTENCE_PATH.read_text(encoding="utf-8")
    assert "data-commit-instance" in source
    assert "function identity(" in source
    body = block(source, "function identity(")
    for part in ("scope", "key", "instance"):
        assert part in body, part
    # A control that belongs to no record yet cannot be committed.
    commit = block(source, "function commit(")
    assert "instance" in commit
    # Writes serialize per RECORD, so two servers never block each other.
    assert "chains" in commit


def test_the_persistence_owner_offers_one_optional_neutral_materialization_hook():
    """A record with no canonical identity cannot be field-committed, and only
    its own scope knows how one is created. The generic owner therefore ASKS --
    once per record -- and owns nothing about the creation itself."""
    source = PERSISTENCE_PATH.read_text(encoding="utf-8")
    assert "function materialize(" in source
    body = block(source, "function materialize(")
    # Optional: a scope that does not declare it is untouched.
    assert "typeof scope.materialize !== 'function'" in body
    # Exactly one creation per draft record.
    assert "materializing.has(owner)" in body
    assert "materializing.set(owner" in body
    # It is counted as outstanding work, so `settle` waits for it.
    settle = block(source, "async function settle(")
    assert "materializations" in settle
    # The commit boundary is what asks, and only for an uncommittable record
    # whose draft actually differs from the accepted baseline.
    commit = block(source, "function commit(")
    guard = commit[:commit.index("const scopeId")]
    assert "deferred.set(control, draft)" in guard
    assert "materialize(control)" in guard
    assert guard.index("draft === baselines.get(key)") < guard.index("materialize(control)")


def test_the_materialization_hook_names_no_integration_or_page():
    source = PERSISTENCE_PATH.read_text(encoding="utf-8")
    for named in ("usenet", "alldebrid", "general_http", "general_ftp",
                  "Usenet", "AllDebrid", "/api/", "data-setting"):
        assert named not in source, named


def test_the_persistence_owner_keys_off_its_own_attribute_vocabulary():
    source = PERSISTENCE_PATH.read_text(encoding="utf-8")
    # The owner reads data-commit-key, not another owner's data-setting.
    assert 'data-commit-key' in source
    assert 'data-setting' not in source


def test_supersession_suppresses_presentation_but_never_canonical_knowledge():
    """A response that arrives after a newer edit still says what the server
    now holds. If it only suppressed the repaint but also dropped the value, a
    later write's rollback would land on state the server abandoned."""
    source = PERSISTENCE_PATH.read_text(encoding="utf-8")
    body = block(source, "function commit(")
    accepted = body.index("baselines.set(key, String(value));")
    # The baseline is advanced BEFORE any supersession check on the success
    # path -- unconditionally.
    success = body[body.index("const result = await scope.commit("):accepted]
    assert "tokens.get(key) !== token" not in success, \
        "a superseded success drops what the server accepted"
    # Only the repaint is conditional.
    repaint = body[accepted:body.index("} catch (error) {")]
    assert "tokens.get(key) === token" in repaint and "present(control, value)" in repaint


def test_failure_rolls_back_to_the_baseline_as_it_stands_at_failure_time():
    """Earlier writes in the same lane may have been accepted since this one
    was queued, so a queue-time snapshot can be two values out of date."""
    source = PERSISTENCE_PATH.read_text(encoding="utf-8")
    body = block(source, "function commit(")
    failure = body[body.index("} catch (error) {"):]
    assert "baselines.get(key)" in failure, \
        "rollback uses a stale queue-time snapshot instead of current truth"
    assert "present(control, accepted)" not in failure


def test_failure_repaints_only_while_the_control_still_shows_what_it_sent():
    """A commit token protects a draft that has CROSSED its commit boundary.
    An operator still typing while a write is in flight has no token: the
    visible value is the only evidence that newer intent exists, and a failed
    write -- which accepted nothing -- must not paint over it."""
    source = PERSISTENCE_PATH.read_text(encoding="utf-8")
    body = block(source, "function commit(")
    failure = body[body.index("} catch (error) {"):]
    guard = failure[:failure.index("present(control,")]
    assert "signature(control) === draft" in guard, \
        "a failure repaints over a newer draft the operator is still typing"
    # The failure path still changes no canonical state: nothing was accepted.
    assert "baselines.set(" not in failure


# --- the record CREATION boundary ----------------------------------------
#
# A card being created has no canonical identity, so nothing on it can be
# field-committed -- yet it stays interactive, so the operator can express
# intent after the creation write is dispatched and before the id arrives.
# That intent must reach the minted record, and removal must never abandon it.
#
# DP 1.0.13: there is no Save to take that boundary, so the canonical
# persistence owner asks the record's own scope to materialize it.


def test_a_commit_boundary_crossed_before_the_record_exists_is_remembered():
    """Dropping it would strand the edit: the operator already left the field,
    so nothing would ever commit it again."""
    source = PERSISTENCE_PATH.read_text(encoding="utf-8")
    body = block(source, "function commit(")
    guard = body[:body.index("const scopeId")]
    assert "deferred.set(control, draft)" in guard, \
        "a boundary crossed before the record exists is silently dropped"
    # Remembered only where there is something to write.
    assert guard.index("draft === baselines.get(key)") < guard.index("deferred.set(control, draft)")
    assert "resume," in source, "the handoff out of pending creation is not exposed"
    replay = block(source, "function resume(")
    assert "deferred.delete(control)" in replay


def test_what_is_deferred_is_the_draft_that_crossed_the_boundary():
    """A commit boundary belongs to a DRAFT, not to a control that happened to
    cross one earlier. Replaying the control's CURRENT value would promote a
    draft the operator typed afterwards and never left."""
    source = PERSISTENCE_PATH.read_text(encoding="utf-8")
    assert "const deferred = new WeakMap();" in source, \
        "membership alone cannot say WHICH draft crossed the boundary"
    body = block(source, "function commit(")
    # The draft under commit is the replayed one when there is one.
    assert "replayed === undefined ? signature(control) : String(replayed)" in body
    replay = block(source, "function resume(")
    assert "deferred.get(control)" in replay and "commit(control, draft)" in replay, \
        "resume replays the control's current value instead of the deferred draft"


def test_the_usenet_scope_is_what_creates_the_record():
    """The generic owner asks; the scope creates and adopts the identity. There
    is no second creation owner and no Usenet focusout listener."""
    scope = block(USENET_JS, "function registerServerScope(")
    assert "materialize:" in scope
    assert "createServer(" in scope
    creation = block(USENET_JS, "async function createServer(")
    assert "'POST'" in creation and "readCard(" in creation
    assert "adoptServerId(" in creation
    # Exactly one creation site.
    assert USENET_JS.count("api(\n") + USENET_JS.count("api('POST'") == 1 or \
        USENET_JS.count("'/usenet/servers'") == 1


def test_a_blank_or_invalid_host_creates_nothing_and_keeps_inline_validation():
    creation = block(USENET_JS, "async function createServer(")
    assert "if (!server.host)" in creation
    assert "validation(card, 'A server host is required.')" in creation
    assert creation.index("if (!server.host)") < creation.index("'POST'")


def test_record_creation_hands_post_dispatch_intent_onto_the_new_record():
    """Ordinary fields, the immediate SSL act and a chosen display name are
    each carried in their own semantic order -- after the card has acquired its
    canonical identity and its accepted baselines."""
    body = block(USENET_JS, "async function createServer(")
    assert body.index("adoptServerId(") < body.index("converge(card, result, server)")
    assert body.index("converge(card, result, server)") < body.index("resumeCreation(")
    # The creation response may no longer project the display name itself: a
    # name chosen while the record was being minted is newer.
    assert "convergeName(card, result)" not in body
    handoff = block(USENET_JS, "async function resumeCreation(")
    assert "DPSettingsPersistence.resume(card)" in handoff
    assert "resumeSsl(" in handoff and "resumeName(" in handoff
    # In the order the operator performed them: a port boundary crossed AFTER
    # the immediate act must win over the port that act carried.
    assert handoff.index("resumeSsl(") < handoff.index("DPSettingsPersistence.resume(card)")


def test_an_immediate_act_before_identity_is_remembered_as_an_act():
    """Reconstructing it from the card's current values when the record
    finally exists would fold in a later draft that crossed no boundary --
    exactly the defect the deferred DRAFT fixed for changed-blur fields."""
    deferral = block(USENET_JS, "function deferSsl(")
    assert "pendingSsl.set(card, sent)" in deferral, \
        "the act is not captured as a payload"
    assert "sent.port = committedValue('port', node.value)" in deferral
    # The port this act moved is part of the act, so it supersedes any boundary
    # that port crossed before it.
    assert "DPSettingsPersistence.supersede(node, node.value)" in deferral
    replay = block(USENET_JS, "async function resumeSsl(")
    assert "pendingSsl.get(card)" in replay, \
        "the act is reconstructed from mutable form state instead of replayed"
    assert "control.checked" not in replay.split("catch (error)")[0], \
        "the replayed payload is derived from the live control"


def test_removal_is_serialized_behind_an_in_flight_creation():
    """An outstanding creation that completes after the card is gone would
    otherwise leave a backend record with no card to govern it."""
    body = block(USENET_JS, "async function removeCard(")
    assert "creations.get(card)" in body, "removal can act while creation is in flight"
    assert body.index("await minting") < body.index("const id = serverId(card)"), \
        "the id is read before the creation that mints it has returned"


def test_only_the_persistence_owner_listens_for_the_commit_boundary():
    offenders = [p.name for p in MAINTAINED_JS
                 if p.name != "ui-settings-persistence.js" and "focusout" in p.read_text(encoding="utf-8")]
    assert offenders == [], offenders


def test_the_persistence_owner_loads_before_the_settings_page():
    assert "ui-settings-persistence.js" in INDEX_HTML
    assert INDEX_HTML.index("ui-settings-persistence.js") < INDEX_HTML.index("ui-settings-page.js")


def test_every_migrated_providers_field_declares_exactly_one_scope():
    table = block(SETTINGS_JS, "const COMMIT_FIELDS")
    for key in ("alldebrid_rate_limit_per_minute", "poll_interval_seconds",
                "full_sync_interval_minutes", "upload_fail_retry_count",
                "upload_fail_retry_delay_minutes",
                # DP 1.0.13: credential replacement is an ordinary value change.
                "alldebrid_api_key"):
        assert key in table, key
    # Archive passwords keep their own, unchanged hydration contract.
    assert "extraction_password" not in table


def test_the_page_declares_its_commit_class_rather_than_reimplementing_it():
    field = block(SETTINGS_JS, "function input(")
    assert "commitAttributes(key)" in field
    # The class itself is emitted in exactly ONE place, for every kind of
    # control, from the page's own declaration table.
    attributes = block(SETTINGS_JS, "function commitAttributes(")
    assert 'data-commit="${html(declared.commit || \'changed-blur\')}"' in attributes
    assert "data-commit-key=" in attributes and "data-commit-scope=" in attributes
    # The page dispatches scoped mutations; it owns no baseline/stale machinery.
    assert "window.DPSettingsPersistence" in SETTINGS_JS
    assert "defineScope(" in SETTINGS_JS
    for machinery in ("function dirty(", "baselines", "staleToken"):
        assert machinery not in SETTINGS_JS, machinery


# --- Items 11-13: the AllDebrid credential contract ------------------------

def test_the_api_key_is_an_ordinary_changed_blur_replacement():
    key_field = block(SETTINGS_JS, "function allDebridApiKeyField(")
    element = control(key_field, 'data-commit-scope="integration:alldebrid"')
    assert "ALLDEBRID_KEY_PLACEHOLDER(configured)" in key_field
    assert 'data-commit="changed-blur"' in element
    assert 'data-setting="${key}"' in element
    assert 'data-commit-key="${key}"' in element
    assert 'data-commit="gated-save"' not in element
    assert "const key = 'alldebrid_api_key';" in key_field
    # The retired Save-oriented clear checkbox and its copy are gone.
    assert "data-clear-secret=" not in key_field
    assert "data-clear-secret=\"alldebrid_api_key\"" not in SETTINGS_JS
    assert "Remove the saved API key when you choose Save." not in SETTINGS_JS
    assert "then choose Save" not in key_field


def test_the_credential_scope_writes_the_existing_integration_mutation():
    # One generic integration scope serves every integration namespace; the
    # identity is the only thing that differs.
    scope = block(SETTINGS_JS, "function registerIntegrationScope(")
    assert "`/integrations/${identity}/configuration`" in scope
    assert "INTEGRATION_SECRET_CONTROLS" in scope, \
        "the scope must know which of its controls is a secret"
    # A secret's accepted presentation is blank: no credential becomes a baseline.
    assert "return '';" in scope
    # The row that has to change because the ACCEPTED state changed is named by
    # the secret control's own declaration, not by the generic scope.
    assert "secret.converge(draft)" in scope
    assert "renderAllDebridCredential(" in block(SETTINGS_JS, "const INTEGRATION_SECRET_CONTROLS")
    assert "registerIntegrationScope(persistence, identity)" in \
        block(SETTINGS_JS, "function registerCommitScopes(")


def test_the_explicit_clear_is_the_only_destructive_credential_writer():
    body = block(SETTINGS_JS, "async function clearAllDebridKey(")
    assert "clear_secrets: ['api_key']" in body
    assert "options: {}" in body, "Clear must never also save a replacement key"
    assert "/integrations/alldebrid/configuration" in body
    # The ONE canonical confirmation gates it, with the required wording.
    assert "window.DPSettingsModal.confirm(" in body
    assert "tone: 'danger'" in body
    assert "title: 'Clear AllDebrid API key?'" in body
    assert "confirmLabel: 'Clear AllDebrid API Key'" in body
    # Declining performs no mutation: nothing is settled or written first.
    gate = body.index("if (!confirmed) return;")
    assert gate < body.index("settle("), "a commit is settled before the operator agrees"
    assert gate < body.index("clear_secrets"), "the clear is not behind the confirmation"
    # Settled first, so the clear is the LAST write on this namespace.
    assert "settle(" in body
    # Exactly one canonical clear, and a failure converges nothing.
    assert body.count("request('PATCH'") == 1
    success = body[:body.index("} catch (error) {")]
    failure = body[body.index("} catch (error) {"):]
    assert "renderAllDebridCredential('')" in success
    assert "renderAllDebridCredential(" not in failure, \
        "a failed clear must not render a false cleared state"
    assert "notify(" in failure


def test_the_clear_group_is_one_explicit_destructive_action():
    """The card-local confirmation checkbox is gone: the group is one red button
    naming exactly what it erases, and whether the operator means it is the one
    canonical Settings confirmation's question."""
    group = block(SETTINGS_JS, "const ALLDEBRID_KEY_CLEAR")
    action = control(group, 'data-action="clear-alldebrid-key"')
    assert "btn-danger" in action
    assert "disabled" not in action, "the action still carries a local gate"
    assert "Clear Stored API Key" in group
    assert 'type="checkbox"' not in group
    assert "<label" not in group
    assert "data-alldebrid-clear-confirm" not in SETTINGS_JS
    assert "Confirm removal of the stored API key" not in SETTINGS_JS
    assert "function refreshAllDebridClearGate(" not in SETTINGS_JS
    # The row renders that one declaration, and nothing else does.
    assert "ALLDEBRID_KEY_CLEAR" in block(SETTINGS_JS, "function allDebridApiKeyField(")
    assert SETTINGS_JS.count("dp-settings-alldebrid-key-clear\"") == 1


def test_the_clear_group_is_centred_against_the_api_key_input_itself():
    """Structural: the clear group occupies the INPUT's own grid row, so it is
    centred against the control rather than the label+hint stack, and it takes
    horizontal room from the field instead of adding an action band. The retired
    confirmation's own rule is gone with it."""
    assert "dp-settings-alldebrid-key-confirm" not in SETTINGS_CSS
    row = rule(SETTINGS_CSS, "#view-settings .dp-settings-alldebrid-key-row {")
    assert "display: grid" in row
    clear = rule(SETTINGS_CSS, "#view-settings .dp-settings-alldebrid-key-clear {")
    assert "align-self: center" in clear
    assert "grid-row: 2" in clear
    control_rule = rule(SETTINGS_CSS, "#view-settings .dp-settings-alldebrid-key-input {")
    assert "grid-row: 2" in control_rule
    for banned in ("position: absolute", "transform", "margin-top: -"):
        assert banned not in clear, banned


def test_verification_invalidation_is_derived_and_never_a_second_check():
    """A credential change stops matching the fingerprint the stored evidence
    describes, so ``verified`` falls to False by derivation. No owner asserts
    it, clears it, or re-checks it.

    The BEHAVIOURAL proof against real evidence is the existing owner's:
    ``test_v113_provider_verification_evidence.py``
    ``test_a_verification_relevant_change_retires_the_evidence``. What is
    proved here is that this batch introduced no second check anywhere."""
    configuration = (BACKEND / "integrations" / "configuration.py").read_text(encoding="utf-8")
    assert "definition.verified(entry.options, entry.verification)" in configuration
    definition_py = (BACKEND / "integrations" / "definition.py").read_text(encoding="utf-8")
    assert "def verified(self, options: dict, evidence) -> bool:" in definition_py
    assert "stored.get(subject) == fingerprint" in definition_py
    # No credential owner asserts, clears or re-checks verification: the
    # AllDebrid scope and its Clear carry the mutation and nothing else.
    scope = block(SETTINGS_JS, "function registerCommitScopes(")
    clear = block(SETTINGS_JS, "async function clearAllDebridKey(")
    for body in (scope, clear):
        assert "verified" not in body
        assert "invalidate" not in body


def test_every_providers_page_action_settles_pending_commits_first():
    for action in ("async function testConnection(", "async function clearAllDebridKey(",
                   "async function saveCurrent("):
        body = block(SETTINGS_JS, action)
        assert "settle(" in body, action


def test_test_is_never_a_credential_save_action():
    payload = block(SETTINGS_JS, "function connectionTestPayload(")
    assert "valueOf('alldebrid_api_key')" in payload
    # Testing never writes, and it carries no removal intent of its own: a
    # removal is an explicit action, never something pending at Test time.
    assert "PATCH" not in payload and "PUT" not in payload
    assert "clear_api_key" not in payload
    tester = block(SETTINGS_JS, "async function testConnection(")
    assert "PATCH" not in tester and "PUT" not in tester


# --- Item 10 (glow): one master-card outer-glow owner ----------------------

def test_the_master_card_outer_glow_has_exactly_one_owner():
    """The Network Sources master chip had no external glow at all. Rather
    than a second copy of the existing master treatment, both masters derive
    the same two-stop omnidirectional glow from ONE per-instance colour datum,
    declared once and covering both selectors."""
    icons = read("ui-settings-card-icons.css")
    chrome = read("ui-settings-chrome.css")
    owner = enclosing_rule(
        icons, "drop-shadow(0 0 4px color-mix(in srgb, var(--dp-settings-master-glow) 78%")
    assert ".dp-settings-debrid-services > .card-header > .card-title::before" in owner
    assert "[data-integration-group] > .card-header .dp-settings-protocol-chip" in owner
    # The previously hard-coded debrid-services filter is gone from the other
    # stylesheet: it now supplies only its colour.
    debrid = enclosing_rule(chrome, "url('/icons/dp/debrid-services.svg")
    assert "drop-shadow" not in debrid, "a second outer-glow owner survives"
    assert "--dp-settings-master-glow: #b866f5;" in debrid
    assert "rgba(184,102,245,.78)" not in chrome
    # Exactly two stylesheets mention the datum: the one owner, and the one
    # master that is not a protocol chip stating its colour.
    owners = [p.name for p in MAINTAINED_CSS
              if "--dp-settings-master-glow" in p.read_text(encoding="utf-8")]
    assert sorted(owners) == ["ui-settings-card-icons.css", "ui-settings-chrome.css"], owners


def test_the_network_sources_master_glow_is_selected_structurally():
    """The master chip is addressed through the group attribute groupCard()
    already emits -- no integration is named, and the Downloads executor-tuning
    cards are deliberately not matched."""
    icons = re.sub(r"/\*.*?\*/", "", read("ui-settings-card-icons.css"), flags=re.S)
    owner = enclosing_rule(
        icons, "drop-shadow(0 0 4px color-mix(in srgb, var(--dp-settings-master-glow) 78%")
    selectors = owner[:owner.index("{")]
    assert "[data-integration-group]" in selectors
    # The glow names no integration and no family: it is selected by the group
    # attribute groupCard() already emits.
    for named in ("direct_sources", "direct-sources", "general-sources",
                  "network-sources", "general_http", "general_ftp", "usenet",
                  "data-executor-tuning"):
        assert named not in selectors, named


def test_the_network_sources_chip_keeps_its_colour_geometry_and_inner_glow():
    icons = read("ui-settings-card-icons.css")
    datum = rule(icons, "#view-settings .dp-settings-protocol-chip[data-protocol='general_http'] {")
    assert "#3B82F6" in datum
    chip = rule(icons, "#view-settings .dp-settings-protocol-chip {")
    for kept in ("width: 38px", "height: 38px", "border-radius: 9px", "inset 0 1px 0"):
        assert kept in chip, kept
    glyph = rule(icons, "#view-settings .dp-settings-protocol-chip img {")
    assert "drop-shadow" in glyph, "the internal glyph glow was removed"


# --- Items 17-19: Usenet server-card geometry ------------------------------

def test_one_test_and_one_remove_per_card_rendered_once():
    for label, markup in SERVER_CARDS:
        assert markup.count('data-usenet-action="test"') == 1, label
        assert markup.count('data-usenet-action="remove"') == 1, label
    # And the behaviour owner never clones or relocates them.
    for banned in ("cloneNode", "insertBefore(actions", "appendChild(actions",
                   "getBoundingClientRect"):
        assert banned not in USENET_JS, banned


def test_the_action_pair_lives_inside_the_disclosure_grid():
    """One markup set, placed by the grid in both states -- never moved by JS."""
    for label, markup in SERVER_CARDS:
        advanced = markup[markup.index("dp-usenet-advanced"):]
        assert "dp-usenet-actions" in advanced, label
        # DOM order is toggle -> body -> actions, so tab order follows the
        # expanded visual order.
        assert advanced.index("dp-usenet-advanced-toggle") < advanced.index("dp-usenet-advanced-body")
        assert advanced.index("dp-usenet-advanced-body") < advanced.index("dp-usenet-actions")


def test_the_disclosure_grid_centres_the_pair_on_the_whole_card():
    grid = rule(USENET_CSS, ".dp-usenet-advanced {")
    assert "display: grid" in grid
    # Symmetric rails: the pair is centred on the CARD, not on leftover space.
    assert "minmax(0, 1fr) auto minmax(0, 1fr)" in grid
    for banned in ("position: absolute", "margin-left", "transform"):
        assert banned not in grid, banned
    collapsed = rule(USENET_CSS, ".dp-usenet-actions {")
    assert "grid-row: 1" in collapsed and "grid-column: 2" in collapsed
    # Expanded: its own final row beneath every advanced field, still centred,
    # driven by the disclosure's own open state.
    expanded = rule(USENET_CSS, ".dp-usenet-advanced:has(> .dp-usenet-advanced-body:not([hidden])) .dp-usenet-actions {")
    assert "grid-row: 3" in expanded
    assert "grid-column: 1 / -1" in expanded


def test_the_priority_hint_belongs_to_the_priority_field():
    for label, markup in SERVER_CARDS:
        field = markup[markup.index("dp-usenet-field--priority"):]
        field = field[:field.index("</div>")]
        assert 'data-usenet-field="priority"' in field, label
        assert "Lower values have priority." in field, label
        assert field.index('data-usenet-field="priority"') < field.index("Lower values have priority."), label
    hint = rule(USENET_CSS, ".dp-usenet-priority-hint {")
    assert "text-align: center" not in hint, "the hint is still a free-floating centred paragraph"
    # The tuning row keeps both inputs on one band even though one column is taller.
    tuning = rule(USENET_CSS, ".dp-usenet-row--tuning {")
    assert "align-items: flex-start" in tuning


def test_ssl_is_centred_against_the_host_and_port_input_boxes():
    """Structural: the host row becomes a two-row grid -- labels, then controls
    -- and SSL is placed in the CONTROLS row. Nothing is nudged."""
    row = rule(USENET_CSS, ".dp-usenet-row--host {")
    assert "display: grid" in row
    ssl = rule(USENET_CSS, ".dp-usenet-ssl {")
    assert "grid-row: 2" in ssl
    assert "align-self: center" in ssl
    for banned in ("position: absolute", "transform", "margin-top: -", "margin-bottom: -"):
        assert banned not in ssl, banned
    # The toggle itself is untouched.
    for label, markup in SERVER_CARDS:
        element = control(markup, 'data-usenet-field="ssl"')
        assert 'type="checkbox"' in element, label
        assert "toggle-row" in markup


def test_no_usenet_geometry_is_faked_at_runtime():
    for banned in ("position: absolute", "position:absolute"):
        assert banned not in USENET_CSS, banned
    toggle = block(USENET_JS, "function toggleAdvanced(")
    assert "hidden" in toggle and "aria-expanded" in toggle
    for banned in ("style.", "classList.add", "appendChild", "insertBefore"):
        assert banned not in toggle, banned


# --- Item 10: the footer cannot replay migrated Providers-page state -------

def test_footer_apply_remains_present():
    assert 'data-action="save"' in SETTINGS_JS
    assert "Apply Settings" in SETTINGS_JS


def test_footer_apply_no_longer_reads_migrated_providers_page_controls():
    # There is no deferred transfer-policy or integration payload left at all.
    for retired in ("function transferPolicyPayload(", "function aria2ConfigurationPayload(",
                    "function usenetConfigurationPayload("):
        assert retired not in SETTINGS_JS, retired

    document = block(SETTINGS_JS, "function nonAuthPayload(")
    assert "intOf('full_sync_interval_minutes'" not in document
    assert "current.full_sync_interval_minutes" in document
    # Every Downloads-owned settings-document value is carried forward from
    # canonical truth, never re-read from the page.
    for migrated in ("download_folder", "min_free_disk_gb",
                     "disk_guard_resume_hysteresis_gb"):
        assert f"valueOf('{migrated}'" not in document, migrated
        assert f"floatOf('{migrated}'" not in document, migrated
        assert f"{migrated}:" not in document, migrated
    # Positive control: an unmigrated top-level field is still read from the form.
    assert "boolOf('extract_enabled')" in document


def test_footer_apply_writes_no_integration_namespace_at_all():
    persist = block(SETTINGS_JS, "async function persistNonAuth(")
    assert "/integrations/" not in persist
    assert "/transfer-policy" not in persist
    assert "allDebridConfigurationPayload" not in SETTINGS_JS
    # Positive control: the whole-settings document write is still the footer's.
    assert "request('PUT', '/settings', nonAuthPayload()" in persist


def test_immediate_toggles_are_still_immediate_and_never_deferred():
    enable = block(SETTINGS_JS, "async function providerEnableChanged(")
    assert "/integrations/" in enable and "PATCH" in enable
    persist = block(SETTINGS_JS, "async function persistNonAuth(")
    assert "data-integration-enabled" not in persist
    assert "enabled:" not in persist


# --- Archive Passwords is explicitly excluded from this batch --------------

def test_archive_passwords_is_not_migrated_by_this_batch():
    table = block(SETTINGS_JS, "const COMMIT_FIELDS")
    assert "extraction_password" not in table
    field = block(SETTINGS_JS, "function archivePasswordField(")
    assert "data-commit" not in field
    # Its owner keeps its own, unchanged hydration contract.
    assert "DPArchivePasswords" in SETTINGS_JS
