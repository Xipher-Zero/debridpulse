"""DP 1.0.13 -- Settings interaction foundation + Sources & Providers cleanup.

Six bounded items, each proved at its canonical owner:

1. Provider Status drops the standalone ``premium_family`` tier; Usenet joins
   the existing ``general_family`` tier, whose first two positions are reserved
   -- Usenet, then General Sources -- so a later GENERAL integration that
   declares no explicit order still follows both.
2. The Usenet server-card collection centres on its own viewport and expands
   outward from that centre, including when it wraps.
3. Usenet owns no notification system: Save/Test/Remove RESULTS are the
   canonical toast owner's. Inline field validation is a different thing and
   survives as its own, narrower surface.
4/5. The AllDebrid card's ``Additional Settings`` disclosure and its
   ``[Test][Save]`` action group share ONE row, and Save is the explicit commit
   boundary for gated credential/destructive state.
6b. A Usenet server card is NOT a credential transaction. Each control on it
   is classified by ITS OWN semantics and risk: ordinary scalars are
   changed-blur, SSL is an immediate reversible toggle, and only the password
   and its Clear confirmation are gated behind the card's Save. A card with no
   canonical id yet is the one deliberate exception -- record CREATION -- and
   its Save mints the record.
6/9. One canonical Settings field-persistence owner
   (``ui-settings-persistence.js``) provides baseline, dirty comparison, scoped
   dispatch, stale-response protection, success convergence and failure
   rollback. Controls DECLARE their commit class; no page reimplements it.
10. The global ``Apply Settings`` control remains, but can no longer replay a
   migrated Sources & Providers value over newer locally persisted state.

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


# --- Item 1: GENERAL tier ordering ----------------------------------------

GENERAL_FAMILY = "general_family"
RETIRED_TIER = "premium_family"


def test_usenet_belongs_to_the_general_tier():
    presentation = usenet_definition.presentation
    assert presentation.status_tier == GENERAL_FAMILY
    assert presentation.status_tier_label == "General"


def test_no_integration_declares_the_retired_standalone_premium_tier():
    assert [d.id for d in definitions if d.presentation.status_tier == RETIRED_TIER] == []


def test_premium_services_and_alldebrid_are_unchanged():
    presentation = alldebrid_definition.presentation
    assert presentation.status_tier == "premium_service"
    assert presentation.status_tier_label == "Premium Services"
    assert presentation.display_order == 10
    assert presentation.premium is True


def test_the_general_tier_reserves_usenet_first_and_general_sources_second():
    orders = {d.id: d.presentation.display_order for d in definitions
              if d.presentation.status_tier == GENERAL_FAMILY}
    assert set(orders) == {"usenet", "general_http", "general_ftp"}
    assert orders["usenet"] < orders["general_http"] <= orders["general_ftp"]


def test_a_future_general_tier_entry_naturally_follows_both_reserved_positions():
    """The invariant is future-proof, not merely true of today's two entries.

    An integration that declares no explicit order takes the presentation
    model's default, so it sorts after BOTH reserved positions without the
    renderer, or this batch, knowing anything about it.
    """
    reserved = [d.presentation.display_order for d in definitions
                if d.presentation.status_tier == GENERAL_FAMILY]
    assert max(reserved) < IntegrationPresentation().display_order


def test_the_general_sources_aggregate_group_survives_unchanged():
    for definition in (general_http_definition, general_ftp_definition):
        assert definition.presentation.status_group == "direct_sources"
        assert definition.presentation.status_group_label == "General Sources"
        assert definition.presentation.status_tier_label == "General"


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
                  "premium_service", GENERAL_FAMILY, "Premium Services", "General Sources"):
        assert named not in status_js, named


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
    for action in ("async function save(", "async function test(", "async function removeCard("):
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


def test_only_the_credential_and_its_confirmation_are_gated():
    for label, markup in SERVER_CARDS:
        password = control(markup, 'data-usenet-field="password"')
        assert 'data-commit="gated-save"' in password
        assert 'data-commit="changed-blur"' not in password
    # A brand-new card has no stored credential, so only the rendered card
    # carries the Clear confirmation at all.
    clear = control(SERVER_CARDS[0][1], "data-usenet-clear-password")
    assert 'data-commit="gated-save"' in clear
    assert "data-usenet-clear-password" not in block(USENET_JS, "function blankCard(")


def test_save_on_an_existing_record_commits_only_the_gated_credential():
    body = block(USENET_JS, "async function save(")
    # The gated payload is the credential and its confirmation, nothing else.
    assert "password" in body
    assert "clear_password" in body
    for ordinary in ORDINARY_SERVER_FIELDS:
        assert f"'{ordinary}'" not in body, ordinary
    # Creation is the one deliberate record boundary, taken only when the card
    # has no canonical id yet.
    assert "if (!serverId(card)) return createServer(" in body
    creation = block(USENET_JS, "async function createServer(")
    assert "'POST'" in creation and "readCard(" in creation
    assert "adoptServerId(" in creation


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
    # A credential is never projected back into the browser.
    assert "password" in body


def test_every_record_write_shares_that_record_lane():
    """Immediate, gated and dialog-committed writes mutate the SAME record as a
    field commit, so they cannot be allowed to overlap it or each other."""
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


def test_a_gated_mutation_consumes_only_the_intent_it_dispatched():
    """The rule scoped convergence applies to ordinary controls applies to the
    GATED ones too: a completed write may reset the credential and its Clear
    confirmation only while they still represent what it sent. Intent created
    after dispatch is newer and must survive."""
    consume = block(USENET_JS, "function consumeGatedIntent(")
    assert "dispatched.password" in consume and "dispatched.clear" in consume
    assert "field.value === dispatched.password" in consume
    assert "gate.checked === dispatched.clear" in consume
    # Both gated writers use it: the existing record's Save and record creation.
    assert "consumeGatedIntent(card, {password: secret, clear})" in USENET_JS
    assert "consumeGatedIntent(card, {password: server.password || ''" in USENET_JS
    # And nothing clears the credential unconditionally any more.
    for retired in ("if (field) field.value = '';", "if (gate) gate.checked = false;"):
        assert retired not in USENET_JS, retired


def test_the_alldebrid_gated_save_carries_newer_intent_across_its_rerender():
    """The credential row is re-rendered because the accepted state changes
    what it must show; anything entered after dispatch is carried across it."""
    consume = block(SETTINGS_JS, "function consumeAllDebridIntent(")
    assert "dispatched.apiKey" in consume and "dispatched.clear" in consume
    assert "pendingKey" in consume and "pendingClear" in consume
    assert "allDebridApiKeyField(" in consume
    saver = block(SETTINGS_JS, "async function saveAllDebridCredentials(")
    assert "consumeAllDebridIntent(card, {apiKey, clear: clears.length > 0})" in saver
    # The unconditional row replacement is gone from the save path.
    assert "row.outerHTML" not in saver


def test_every_usenet_action_settles_pending_field_commits_first():
    for action in ("async function save(", "async function test(",
                   "async function sslChanged(", "async function rename("):
        body = block(USENET_JS, action)
        assert "settle(" in body, action


def test_the_canonical_toast_owner_is_the_only_notification_owner_on_this_page():
    assert "window.toast" in read("ui-toast-contract.js")


# --- Items 4/5: the AllDebrid action row -----------------------------------

def test_the_disclosure_and_the_action_group_share_one_row():
    panel = block(SETTINGS_JS, "function sourcesPanel(")
    assert "dp-settings-provider-advanced" in panel
    group = panel.index("dp-settings-provider-advanced")
    assert panel.index("dp-settings-additional", group) < panel.index("dp-settings-provider-actions", group)


def test_the_action_row_geometry_has_one_owner_and_no_positioning_hack():
    owners = [p.name for p in MAINTAINED_CSS
              if "dp-settings-provider-advanced" in p.read_text(encoding="utf-8")]
    assert owners == ["ui-settings-page.css"], owners
    group = rule(SETTINGS_CSS, "#view-settings .dp-settings-provider-advanced {")
    assert "display: grid" in group
    for banned in ("position: absolute", "margin-left", "margin-inline-start", "transform"):
        assert banned not in group, banned
    # The action group's placement is the grid's, never a nudge of its own.
    actions = rule(SETTINGS_CSS, "#view-settings .dp-settings-provider-actions {")
    for banned in ("position: absolute", "margin", "top:", "transform"):
        assert banned not in actions, banned


def test_the_expanded_state_bottom_aligns_the_action_group():
    # Declarative, driven by the disclosure's own open state -- never a
    # measured offset and never a second stylesheet.
    assert ":has(> details[open])" in SETTINGS_CSS


def test_save_sits_immediately_right_of_test_in_one_action_group():
    actions = block(SETTINGS_JS, "function sourcesPanel(")
    actions = actions[actions.index("dp-settings-provider-actions"):]
    assert actions.index('data-action="test-alldebrid"') < actions.index('data-action="save-alldebrid"')


def test_save_uses_the_canonical_success_button_variant():
    assert "btn-success" in SETTINGS_JS
    assert ":is(.btn-success" in LANGUAGE_CSS
    variant = rule(LANGUAGE_CSS, ":is(.btn-success")
    assert "--dp-state-success" in variant
    # The success variant belongs to the one button-language owner.
    offenders = [p.name for p in MAINTAINED_CSS
                 if p.name != "ui-universal-language.css" and ".btn-success" in p.read_text(encoding="utf-8")]
    assert offenders == [], offenders


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
    table = block(SETTINGS_JS, "const CHANGED_BLUR_FIELDS")
    for key in ("alldebrid_rate_limit_per_minute", "poll_interval_seconds",
                "full_sync_interval_minutes", "upload_fail_retry_count",
                "upload_fail_retry_delay_minutes"):
        assert key in table, key
    # Gated and secret controls are never changed-blur.
    assert "alldebrid_api_key" not in table
    assert "extraction_password" not in table


def test_the_page_declares_its_commit_class_rather_than_reimplementing_it():
    field = block(SETTINGS_JS, "function input(")
    assert "CHANGED_BLUR_FIELDS" in field
    assert 'data-commit=' in field
    # The page dispatches scoped mutations; it owns no baseline/stale machinery.
    assert "window.DPSettingsPersistence" in SETTINGS_JS
    assert "defineScope(" in SETTINGS_JS
    for machinery in ("function dirty(", "baselines", "staleToken"):
        assert machinery not in SETTINGS_JS, machinery


# --- Item 6.3 / 7 / 8: gated save, draft Test, deterministic ordering ------

def test_the_gated_save_is_the_only_writer_of_alldebrid_credentials():
    saver = block(SETTINGS_JS, "async function saveAllDebridCredentials(")
    assert "/integrations/alldebrid/configuration" in saver
    assert "scopedClears('alldebrid')" in saver
    assert "alldebrid_api_key" in saver


def test_no_gated_control_is_persisted_by_blur():
    key_field = block(SETTINGS_JS, "function allDebridApiKeyField(")
    assert 'data-commit="changed-blur"' not in key_field
    assert 'data-commit="gated-save"' in key_field
    # The confirmation checkbox expresses pending intent only.
    assert "data-clear-secret" in key_field


def test_every_providers_page_action_settles_pending_commits_first():
    for action in ("async function testConnection(", "async function saveAllDebridCredentials(",
                   "async function saveCurrent("):
        body = block(SETTINGS_JS, action)
        assert "settle(" in body, action


def test_test_operates_on_the_current_draft_including_an_unsaved_secret():
    payload = block(SETTINGS_JS, "function connectionTestPayload(")
    assert "valueOf('alldebrid_api_key')" in payload
    assert "clearSecrets()" in payload
    # Testing a draft never writes it.
    assert "PATCH" not in payload and "PUT" not in payload


# --- Item 10: the footer cannot replay migrated Providers-page state -------

def test_footer_apply_remains_present():
    assert 'data-action="save"' in SETTINGS_JS
    assert "Apply Settings" in SETTINGS_JS


def test_footer_apply_no_longer_reads_migrated_providers_page_controls():
    policy = block(SETTINGS_JS, "function transferPolicyPayload(")
    for migrated in ("poll_interval_seconds", "upload_fail_retry_count",
                     "upload_fail_retry_delay_minutes"):
        assert f"intOf('{migrated}'" not in policy, migrated
    # Positive control: the Downloads-page policy fields are still written here.
    assert "intOf('aria2_max_active_downloads'" in policy

    document = block(SETTINGS_JS, "function nonAuthPayload(")
    assert "intOf('full_sync_interval_minutes'" not in document
    assert "current.full_sync_interval_minutes" in document
    # Positive control: an unmigrated top-level field is still read from the form.
    assert "boolOf('extract_enabled')" in document


def test_footer_apply_writes_no_alldebrid_namespace_at_all():
    persist = block(SETTINGS_JS, "async function persistNonAuth(")
    assert "/integrations/alldebrid/configuration" not in persist
    assert "allDebridConfigurationPayload" not in SETTINGS_JS
    # Positive control: the namespaces the footer still owns are untouched.
    assert "/integrations/aria2/configuration" in persist


def test_immediate_toggles_are_still_immediate_and_never_deferred():
    enable = block(SETTINGS_JS, "async function providerEnableChanged(")
    assert "/integrations/" in enable and "PATCH" in enable
    persist = block(SETTINGS_JS, "async function persistNonAuth(")
    assert "data-integration-enabled" not in persist
    assert "enabled:" not in persist


# --- Archive Passwords is explicitly excluded from this batch --------------

def test_archive_passwords_is_not_migrated_by_this_batch():
    table = block(SETTINGS_JS, "const CHANGED_BLUR_FIELDS")
    assert "extraction_password" not in table
    field = block(SETTINGS_JS, "function archivePasswordField(")
    assert "data-commit" not in field
    # Its owner keeps its own, unchanged hydration contract.
    assert "DPArchivePasswords" in SETTINGS_JS
