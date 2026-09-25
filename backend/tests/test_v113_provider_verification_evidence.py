"""DP 1.0.13 Services final corrective pass -- Defect 3.

``Verified`` is DURABLE CANONICAL TRUTH about the CURRENT SAVED configuration,
not a frontend memory of a Test having once succeeded.

The one durable owner is the existing canonical integration namespace
(``IntegrationSettings``): there is no second configuration file, no browser
authority, no per-provider store and no new database schema. The evidence is a
neutral mapping of *verification subject* -> *fingerprint of the
verification-relevant configuration that was actually tested*, so current truth
is DERIVED (does the evidence still describe what is saved?) rather than
manually synchronized.

What is "verification-relevant" is owned by the integration whose Test knows
what it exercises -- never by generic configuration code:

    AllDebrid   the account credential the connection Test authenticates with
                (and the application identity it is exercised under). Local
                request rate limiting is not part of it.
    Usenet      per news server, exactly the connection material the server
                Test sends: host / port / ssl / username / password /
                connections. Display name, priority and acquisition tuning are
                not part of a connection proof.

A browser may never assert that something is verified. It may only carry an
opaque proof the SERVER minted for the draft the SERVER tested, and the server
accepts it only after re-deriving the fingerprint from the configuration it has
just saved.
"""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api import routes
from core.config import AppSettings
from executors.aria2.definition import definition as aria2_definition
from integrations.configuration import (
    accept_verification, normalize_settings, public_integrations,
    record_verification_outcome,
)
from integrations.definition import (
    IntegrationSettings, verification_fingerprint, verification_proof,
    verification_proves,
)
from integrations.usenet.definition import definition as usenet_definition
from providers.alldebrid.definition import definition as alldebrid_definition
from providers.general_ftp.definition import definition as general_ftp_definition
from providers.general_http.definition import definition as general_http_definition

DEFINITIONS = (alldebrid_definition, general_http_definition, general_ftp_definition,
               usenet_definition, aria2_definition)

SERVER = {"id": "srv-1", "host": "news.example.com", "port": 563, "ssl": True,
          "username": "u", "password": "p", "connections": 8, "priority": 0,
          "articles_per_request": 2, "timeout_seconds": 60,
          "enabled": True, "display_name": ""}


def _settings(**integrations):
    base = {
        "alldebrid": IntegrationSettings(enabled=True, options={"api_key": "secret-key"}),
        "general_http": IntegrationSettings(enabled=True, options={}),
        "general_ftp": IntegrationSettings(enabled=True, options={}),
        "usenet": IntegrationSettings(enabled=True, options={"servers": [dict(SERVER)]}),
    }
    base.update(integrations)
    return AppSettings(integrations=base)


def _fingerprints(definition, options):
    return {subject: value for subject, (value, _required)
            in definition.verification_fingerprints(options).items()}


# ── the durable owner ────────────────────────────────────────────────────────

def test_the_durable_verification_owner_is_the_canonical_integration_namespace():
    """One generic durable owner: no second store, no browser authority."""
    assert "verification" in IntegrationSettings.model_fields
    entry = IntegrationSettings()
    assert entry.verification == {}
    # It survives a round trip through the persisted representation, which is
    # the whole point of "a reload must not lose truth".
    revived = IntegrationSettings(**IntegrationSettings(verification={"a": "b"}).model_dump())
    assert revived.verification == {"a": "b"}


def test_verification_evidence_is_never_exposed_publicly():
    """The UI learns the derived fact and nothing else: a fingerprint is
    internal, and publishing one would leak a credential-derived value."""
    settings = _settings(alldebrid=IntegrationSettings(
        enabled=True, options={"api_key": "secret-key"},
        verification={"credential": "deadbeef"}))
    public = public_integrations(settings, DEFINITIONS)["alldebrid"]
    assert "verified" in public
    assert "verification" not in public
    assert "deadbeef" not in repr(public)
    assert "secret-key" not in repr(public)


def test_a_fingerprint_is_deterministic_and_never_the_credential():
    material = {"api_key": "secret-key", "agent": "DebridPulse"}
    first = verification_fingerprint(material)
    assert first == verification_fingerprint(dict(reversed(list(material.items()))))
    assert first != verification_fingerprint({**material, "api_key": "other"})
    assert "secret-key" not in first


# ── what each integration's Test actually proves ─────────────────────────────

def test_alldebrid_verification_covers_the_tested_credential_only():
    base = {"api_key": "secret-key", "agent": "DebridPulse", "rate_limit_per_minute": 60}
    original = _fingerprints(alldebrid_definition, base)
    assert len(original) == 1
    # Local rate limiting is not what the connection Test proves.
    assert _fingerprints(alldebrid_definition, {**base, "rate_limit_per_minute": 5}) == original
    # The credential and the identity it is exercised under are.
    assert _fingerprints(alldebrid_definition, {**base, "api_key": "other"}) != original
    assert _fingerprints(alldebrid_definition, {**base, "agent": "Other"}) != original
    # Nothing to verify without a credential.
    assert _fingerprints(alldebrid_definition, {**base, "api_key": ""}) == {}


def test_usenet_verification_is_per_server_and_covers_the_tested_connection_only():
    options = {"servers": [dict(SERVER)]}
    original = _fingerprints(usenet_definition, options)
    assert list(original) == ["srv-1"]
    for irrelevant, value in (("display_name", "Renamed"), ("priority", 7),
                              ("articles_per_request", 5), ("timeout_seconds", 120)):
        changed = {"servers": [{**SERVER, irrelevant: value}]}
        assert _fingerprints(usenet_definition, changed) == original, irrelevant
    for relevant in ("host", "port", "ssl", "username", "password", "connections"):
        value = {"host": "news2.example.com", "port": 119, "ssl": False,
                 "username": "other", "password": "other", "connections": 9}[relevant]
        changed = {"servers": [{**SERVER, relevant: value}]}
        assert _fingerprints(usenet_definition, changed) != original, relevant


def test_usenet_is_verified_only_when_every_participating_server_is_covered():
    second = {**SERVER, "id": "srv-2", "host": "news2.example.com"}
    options = {"servers": [dict(SERVER), dict(second)]}
    evidence = _fingerprints(usenet_definition, options)
    assert usenet_definition.verified(options, evidence) is True
    # A second enabled usable server blocks the aggregate until it is covered.
    assert usenet_definition.verified(options, {"srv-1": evidence["srv-1"]}) is False
    # A DISABLED server does not participate, so it cannot block it.
    parked = {"servers": [dict(SERVER), {**second, "enabled": False}]}
    assert usenet_definition.verified(parked, {"srv-1": evidence["srv-1"]}) is True
    # No usable enabled server at all is Unconfigured, never Verified.
    empty = {"servers": [{**SERVER, "enabled": False}]}
    assert usenet_definition.verified(empty, evidence) is False
    assert usenet_definition.configured(empty) is False


def test_enable_state_does_not_decide_verification():
    settings = _settings()
    evidence = _fingerprints(alldebrid_definition, settings.integrations["alldebrid"].options)
    settings.integrations["alldebrid"] = IntegrationSettings(
        enabled=False, options={"api_key": "secret-key"}, verification=evidence)
    public = public_integrations(settings, DEFINITIONS)["alldebrid"]
    assert public["enabled"] is False
    assert public["configured"] is True
    assert public["verified"] is True


# ── preservation and retirement, in the ONE canonical merge owner ────────────

def test_the_canonical_merge_owner_preserves_evidence_a_scoped_route_rebuilt():
    """Every scoped mutation reconstructs ``IntegrationSettings`` without the
    evidence; the one normalization owner carries it forward, so evidence is
    not copied route by route."""
    previous = _settings(alldebrid=IntegrationSettings(
        enabled=True, options={"api_key": "secret-key"},
        verification=_fingerprints(alldebrid_definition, {"api_key": "secret-key"})))
    current = _settings(alldebrid=IntegrationSettings(
        enabled=False, options={"api_key": "secret-key", "rate_limit_per_minute": 5}))
    clean = normalize_settings(current, DEFINITIONS, previous=previous)
    assert clean.integrations["alldebrid"].verification == previous.integrations["alldebrid"].verification
    assert public_integrations(clean, DEFINITIONS)["alldebrid"]["verified"] is True


def test_a_verification_relevant_change_retires_the_evidence():
    previous = _settings(alldebrid=IntegrationSettings(
        enabled=True, options={"api_key": "secret-key"},
        verification=_fingerprints(alldebrid_definition, {"api_key": "secret-key"})))
    current = _settings(alldebrid=IntegrationSettings(enabled=True, options={"api_key": "rotated"}))
    clean = normalize_settings(current, DEFINITIONS, previous=previous)
    assert clean.integrations["alldebrid"].verification == {}
    public = public_integrations(clean, DEFINITIONS)["alldebrid"]
    assert public["configured"] is True and public["verified"] is False


def test_evidence_of_a_removed_usenet_server_does_not_survive_it():
    previous = _settings(usenet=IntegrationSettings(
        enabled=True, options={"servers": [dict(SERVER)]},
        verification=_fingerprints(usenet_definition, {"servers": [dict(SERVER)]})))
    current = _settings(usenet=IntegrationSettings(enabled=True, options={"servers": []}))
    clean = normalize_settings(current, DEFINITIONS, previous=previous)
    assert clean.integrations["usenet"].verification == {}


def test_a_persisted_document_keeps_its_own_evidence_when_it_is_reloaded():
    """The load path normalizes with no ``previous``; the file IS the truth."""
    evidence = _fingerprints(alldebrid_definition, {"api_key": "secret-key"})
    loaded = _settings(alldebrid=IntegrationSettings(
        enabled=True, options={"api_key": "secret-key"}, verification=evidence))
    clean = normalize_settings(loaded, DEFINITIONS)
    assert clean.integrations["alldebrid"].verification == evidence


# ── acceptance: the browser proves nothing by asserting it ───────────────────

def test_a_forged_or_asserted_proof_establishes_nothing():
    settings = _settings()
    for forged in ("true", "1", verification_fingerprint({"api_key": "secret-key"}), "x" * 64):
        accepted = accept_verification(settings, alldebrid_definition, [forged])
        assert accepted.integrations["alldebrid"].verification == {}


def test_a_server_minted_proof_of_the_saved_configuration_is_accepted():
    settings = _settings()
    options = settings.integrations["alldebrid"].options
    proof = verification_proof(_fingerprints(alldebrid_definition, options)["credential"])
    accepted = accept_verification(settings, alldebrid_definition, [proof])
    assert public_integrations(accepted, DEFINITIONS)["alldebrid"]["verified"] is True


def test_a_proof_of_a_different_draft_never_verifies_the_saved_configuration():
    """Test draft A -> change to B -> save B is Configured, never Verified."""
    settings = _settings()
    proof_for_a = verification_proof(verification_fingerprint(
        {"api_key": "draft-a", "agent": alldebrid_definition.options_model().agent}))
    accepted = accept_verification(settings, alldebrid_definition, [proof_for_a])
    assert accepted.integrations["alldebrid"].verification == {}


# ── Test outcomes against the CURRENT SAVED configuration ────────────────────

def test_a_successful_test_of_the_saved_configuration_verifies_it_immediately():
    settings = _settings()
    current = _fingerprints(alldebrid_definition, settings.integrations["alldebrid"].options)
    updated = record_verification_outcome(settings, alldebrid_definition, current["credential"], True)
    assert public_integrations(updated, DEFINITIONS)["alldebrid"]["verified"] is True


def test_a_failed_test_of_the_saved_configuration_retires_its_verification():
    options = {"api_key": "secret-key"}
    evidence = _fingerprints(alldebrid_definition, options)
    settings = _settings(alldebrid=IntegrationSettings(
        enabled=True, options=options, verification=evidence))
    updated = record_verification_outcome(
        settings, alldebrid_definition, evidence["credential"], False)
    assert updated is not None
    public = public_integrations(updated, DEFINITIONS)["alldebrid"]
    assert public["configured"] is True and public["verified"] is False


def test_a_failed_test_of_an_unsaved_draft_revokes_nothing():
    options = {"api_key": "secret-key"}
    settings = _settings(alldebrid=IntegrationSettings(
        enabled=True, options=options,
        verification=_fingerprints(alldebrid_definition, options)))
    other = verification_fingerprint({"api_key": "some-other-draft", "agent": "DebridPulse"})
    assert record_verification_outcome(settings, alldebrid_definition, other, False) is None
    assert public_integrations(settings, DEFINITIONS)["alldebrid"]["verified"] is True


def test_a_failed_usenet_test_retires_only_that_server():
    second = {**SERVER, "id": "srv-2", "host": "news2.example.com"}
    options = {"servers": [dict(SERVER), dict(second)]}
    evidence = _fingerprints(usenet_definition, options)
    settings = _settings(usenet=IntegrationSettings(
        enabled=True, options=options, verification=dict(evidence)))
    updated = record_verification_outcome(
        settings, usenet_definition, evidence["srv-2"], False)
    assert updated.integrations["usenet"].verification == {"srv-1": evidence["srv-1"]}


# ── the scoped save route carries proofs, never a claim ──────────────────────

@asynccontextmanager
async def _noop():
    yield


def _application():
    return SimpleNamespace(
        definitions=DEFINITIONS,
        application_operation=lambda: _noop(),
        configure=lambda: None,
        integration_admin=lambda _identity: SimpleNamespace(apply_memory_tuning=AsyncMock()),
        apply_integration_configuration=AsyncMock(return_value=None),
        validate_configuration=AsyncMock(),
        notify_applicability_changed=lambda _identity: None,
    )


async def _patch_alldebrid(**body):
    previous, current = _settings(), _settings()
    saved = {}
    with patch("api.routes.get_settings", return_value=previous), \
         patch("api.routes.load_settings", return_value=current), \
         patch("api.routes.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings"):
        result = await routes.patch_integration_configuration(
            "alldebrid", routes.IntegrationConfigurationUpdate(**body),
            application=_application())
    return result, saved.get("cfg")


@pytest.mark.asyncio
async def test_saving_a_tested_draft_verifies_exactly_that_draft():
    agent = alldebrid_definition.options_model().agent
    proof = verification_proof(verification_fingerprint({"api_key": "tested-key", "agent": agent}))
    result, saved = await _patch_alldebrid(options={"api_key": "tested-key"}, verification=[proof])
    assert result["verified"] is True
    assert saved.integrations["alldebrid"].verification != {}


@pytest.mark.asyncio
async def test_saving_a_different_draft_than_the_one_tested_is_configured_not_verified():
    agent = alldebrid_definition.options_model().agent
    proof = verification_proof(verification_fingerprint({"api_key": "draft-a", "agent": agent}))
    result, saved = await _patch_alldebrid(options={"api_key": "draft-b"}, verification=[proof])
    assert result["configured"] is True
    assert result["verified"] is False
    assert saved.integrations["alldebrid"].verification == {}


@pytest.mark.asyncio
async def test_the_route_accepts_no_asserted_verification_boolean():
    result, _ = await _patch_alldebrid(options={"api_key": "tested-key"}, verification=["true"])
    assert result["verified"] is False


@pytest.mark.asyncio
async def test_an_unrelated_option_write_keeps_the_verification_it_found():
    def _stored():
        return _settings(alldebrid=IntegrationSettings(
            enabled=True, options={"api_key": "secret-key"},
            verification=_fingerprints(alldebrid_definition, {"api_key": "secret-key"})))
    saved = {}
    with patch("api.routes.get_settings", return_value=_stored()), \
         patch("api.routes.load_settings", return_value=_stored()), \
         patch("api.routes.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings"):
        result = await routes.patch_integration_configuration(
            "alldebrid",
            routes.IntegrationConfigurationUpdate(options={"rate_limit_per_minute": 5}),
            application=_application())
    assert result["verified"] is True


# ── Test is never a configuration writer ─────────────────────────────────────

@pytest.mark.asyncio
async def test_a_test_of_a_draft_persists_no_candidate_configuration():
    """A Test may establish or retire EVIDENCE about the saved configuration.
    It may never promote the draft it tested into canonical configuration."""
    from api import settings_validation_routes as validation

    saved = {}
    user = {"user": {"username": "someone", "isPremium": True, "premiumUntil": 0}}
    with patch("api.settings_validation_routes.get_settings", side_effect=_settings), \
         patch("core.config.load_settings", side_effect=_settings), \
         patch("core.config.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("core.config.apply_settings"), \
         patch("api.settings_validation_routes.AllDebridService") as service:
        service.return_value.get_user = AsyncMock(return_value=user)
        result = await validation.validate_alldebrid(
            validation.AllDebridValidationRequest(api_key="an-unsaved-draft"),
            application=_application())
    assert result["ok"] is True
    # The tested draft is not configuration, so nothing about it was stored.
    stored = saved.get("cfg")
    if stored is not None:
        assert stored.integrations["alldebrid"].options["api_key"] == "secret-key"
        assert stored.integrations["alldebrid"].verification == {}
    # What it hands back is an opaque proof of exactly what was tested.
    assert result["verification"] == verification_proof(verification_fingerprint(
        {"api_key": "an-unsaved-draft",
         "agent": alldebrid_definition.options_model().agent}))


@pytest.mark.asyncio
async def test_a_successful_test_of_the_saved_credential_is_durable_at_once():
    saved = {}
    user = {"user": {"username": "someone", "isPremium": True, "premiumUntil": 0}}
    with patch("api.settings_validation_routes.get_settings", side_effect=_settings), \
         patch("core.config.load_settings", side_effect=_settings), \
         patch("core.config.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("core.config.apply_settings"), \
         patch("api.settings_validation_routes.AllDebridService") as service:
        service.return_value.get_user = AsyncMock(return_value=user)
        await validation_validate()
    stored = saved.get("cfg")
    assert stored is not None, "a Test of the saved configuration established nothing durable"
    assert public_integrations(stored, DEFINITIONS)["alldebrid"]["verified"] is True


async def validation_validate():
    """Test the SAVED credential: a blank draft means 'use the stored one'."""
    from api import settings_validation_routes as validation
    return await validation.validate_alldebrid(
        validation.AllDebridValidationRequest(), application=_application())


# ── the Usenet server mutation preserves evidence and publishes acceptance ───

async def _mutate_usenet(values, *, verification=(), stored=None):
    stored = stored if stored is not None else _settings
    saved = {}
    with patch("core.config.get_settings", return_value=stored()), \
         patch("core.config.load_settings", return_value=stored()), \
         patch("core.config.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("core.config.apply_settings"):
        result = await routes.update_usenet_server(
            "srv-1", routes.UsenetServerUpdate(verification=list(verification), **values),
            application=_application())
    return result, saved.get("cfg")


@pytest.mark.asyncio
async def test_a_server_write_preserves_verification_it_did_not_change():
    options = {"servers": [dict(SERVER)]}
    stored = lambda: _settings(usenet=IntegrationSettings(
        enabled=True, options=options,
        verification=_fingerprints(usenet_definition, options)))
    result, saved = await _mutate_usenet({"priority": 5}, stored=stored)
    assert saved.integrations["usenet"].verification != {}
    assert result["integration"]["verified"] is True


@pytest.mark.asyncio
async def test_a_server_write_that_changes_the_connection_retires_its_verification():
    options = {"servers": [dict(SERVER)]}
    stored = lambda: _settings(usenet=IntegrationSettings(
        enabled=True, options=options,
        verification=_fingerprints(usenet_definition, options)))
    result, saved = await _mutate_usenet({"host": "news3.example.com"}, stored=stored)
    assert saved.integrations["usenet"].verification == {}
    assert result["integration"]["configured"] is True
    assert result["integration"]["verified"] is False


@pytest.mark.asyncio
async def test_saving_a_tested_server_draft_verifies_exactly_that_server():
    proof = verification_proof(verification_fingerprint(
        {"host": "news4.example.com", "port": 563, "ssl": True,
         "username": "u", "password": "p", "connections": 8}))
    result, _ = await _mutate_usenet({"host": "news4.example.com"}, verification=[proof])
    assert result["integration"]["verified"] is True


@pytest.mark.asyncio
async def test_every_server_mutation_publishes_the_canonical_public_projection():
    """The neutral acceptance seam: one response carries the integration's own
    identity and public projection, so a cross-module owner converges without
    a second cache, a compensating GET or a poll."""
    result, _ = await _mutate_usenet({"priority": 1})
    assert result["integration_id"] == "usenet"
    assert set(("enabled", "configured", "verified")) <= set(result["integration"])
    assert "verification" not in result["integration"]


# ── a Test publishes what it accepted, and a write carries only its own ──────

@pytest.mark.asyncio
async def test_a_test_that_settles_verification_publishes_the_accepted_projection():
    """The header must not keep reporting ``Configured`` about a configuration
    the operator's Test has just proved, so the response carries the accepted
    canonical projection for the one neutral acceptance seam."""
    from api import settings_validation_routes as validation

    saved = {}
    user = {"user": {"username": "someone", "isPremium": True, "premiumUntil": 0}}
    with patch("api.settings_validation_routes.get_settings", side_effect=_settings), \
         patch("core.config.load_settings", side_effect=_settings), \
         patch("core.config.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("core.config.apply_settings"), \
         patch("api.settings_validation_routes.AllDebridService") as service:
        service.return_value.get_user = AsyncMock(return_value=user)
        result = await validation.validate_alldebrid(
            validation.AllDebridValidationRequest(), application=_application())
    assert result["integration_id"] == "alldebrid"
    assert result["integration"]["verified"] is True
    assert "verification" not in result["integration"]


@pytest.mark.asyncio
async def test_a_test_of_an_unsaved_draft_publishes_nothing():
    """It accepted nothing, so there is nothing for any owner to converge on."""
    from api import settings_validation_routes as validation

    user = {"user": {"username": "someone", "isPremium": True, "premiumUntil": 0}}
    with patch("api.settings_validation_routes.get_settings", side_effect=_settings), \
         patch("core.config.load_settings", side_effect=_settings), \
         patch("core.config.save_settings"), patch("core.config.apply_settings"), \
         patch("api.settings_validation_routes.AllDebridService") as service:
        service.return_value.get_user = AsyncMock(return_value=user)
        result = await validation.validate_alldebrid(
            validation.AllDebridValidationRequest(api_key="a-different-draft"),
            application=_application())
    assert "integration" not in result and "integration_id" not in result


def test_a_record_write_carries_verification_only_when_a_test_produced_one():
    """An ordinary field commit is byte-for-byte the request it always was."""
    from pathlib import Path
    static = Path(__file__).resolve().parents[2] / "frontend" / "static"
    for name, owner in (("ui-settings-usenet-servers.js", "withTestedDrafts(card, values)"),
                        ("ui-settings-page.js", "withTestedDrafts(identity, body)")):
        source = (static / name).read_text(encoding="utf-8")
        assert f"function {owner}" in source
        helper = source[source.index(f"function {owner}"):]
        helper = helper[:helper.index("\n  }") + 4]
        assert "proofs.length ?" in helper, f"{name} always sends a verification key"
        # And no request assembles the key for itself.
        assert "verification: testedDraftProofs(" not in source


# ── proof lifecycle: a failure supersedes every earlier successful proof ─────
#
# A transient Test->Save proof is evidence about MATERIAL, and the newest truth
# about that material wins. Without this, the proof minted by an earlier
# successful Test stays presentable forever:
#
#     successful Test A  -> proof P
#     failed Test A      -> durable evidence retired
#     Save A carrying P  -> P still validates -> Verified restored
#
# which would let a configuration the server has just proven broken advertise
# itself as verified. Superseding is done at the generic proof owner, never by
# trusting the browser to forget P.


def _alldebrid_material(api_key):
    return {"api_key": api_key, "agent": alldebrid_definition.options_model().agent}


def test_a_failed_test_invalidates_every_proof_minted_for_that_material_before_it():
    material = _alldebrid_material("lifecycle-key")
    fingerprint = verification_fingerprint(material)
    stale = verification_proof(fingerprint)
    assert verification_proves(stale, fingerprint) is True

    record_verification_outcome(_settings(), alldebrid_definition, fingerprint, False)
    assert verification_proves(stale, fingerprint) is False, \
        "a proof minted before the failure is still presentable"


def test_a_later_successful_test_mints_a_proof_that_is_valid_again():
    material = _alldebrid_material("recovery-key")
    fingerprint = verification_fingerprint(material)
    record_verification_outcome(_settings(), alldebrid_definition, fingerprint, False)
    fresh = verification_proof(fingerprint)
    assert verification_proves(fresh, fingerprint) is True


def test_a_superseded_proof_cannot_restore_verification_through_a_save():
    """The whole sequence, end to end, against the generic acceptance owner."""
    options = {"api_key": "superseded-key"}
    fingerprint = verification_fingerprint(_alldebrid_material("superseded-key"))
    saved = _settings(alldebrid=IntegrationSettings(enabled=True, options=options))

    # 1. a successful Test of the saved credential establishes verification.
    proof = verification_proof(fingerprint)
    verified = record_verification_outcome(saved, alldebrid_definition, fingerprint, True)
    assert public_integrations(verified, DEFINITIONS)["alldebrid"]["verified"] is True

    # 2. a later failed Test of the SAME saved credential retires it.
    retired = record_verification_outcome(verified, alldebrid_definition, fingerprint, False)
    assert public_integrations(retired, DEFINITIONS)["alldebrid"]["verified"] is False

    # 3. presenting the pre-failure proof to a Save must establish nothing.
    replayed = accept_verification(retired, alldebrid_definition, [proof])
    assert public_integrations(replayed, DEFINITIONS)["alldebrid"]["verified"] is False, \
        "a stale pre-failure proof restored Verified"

    # 4. only a proof from a NEW successful Test may verify it again.
    renewed = accept_verification(retired, alldebrid_definition, [verification_proof(fingerprint)])
    assert public_integrations(renewed, DEFINITIONS)["alldebrid"]["verified"] is True


def test_the_same_supersession_governs_a_usenet_server_proof():
    options = {"servers": [dict(SERVER)]}
    fingerprint = _fingerprints(usenet_definition, options)["srv-1"]
    saved = _settings(usenet=IntegrationSettings(enabled=True, options=options))

    proof = verification_proof(fingerprint)
    verified = record_verification_outcome(saved, usenet_definition, fingerprint, True)
    assert public_integrations(verified, DEFINITIONS)["usenet"]["verified"] is True

    retired = record_verification_outcome(verified, usenet_definition, fingerprint, False)
    assert public_integrations(retired, DEFINITIONS)["usenet"]["verified"] is False

    replayed = accept_verification(retired, usenet_definition, [proof])
    assert public_integrations(replayed, DEFINITIONS)["usenet"]["verified"] is False, \
        "a stale pre-failure server proof restored Verified"

    renewed = accept_verification(retired, usenet_definition, [verification_proof(fingerprint)])
    assert public_integrations(renewed, DEFINITIONS)["usenet"]["verified"] is True


def test_a_failed_test_of_one_material_does_not_invalidate_another_materials_proof():
    """Supersession is per material: it is not a global proof reset."""
    keep = verification_fingerprint(_alldebrid_material("unaffected-key"))
    kept = verification_proof(keep)
    other = verification_fingerprint(_alldebrid_material("failing-key"))
    record_verification_outcome(_settings(), alldebrid_definition, other, False)
    assert verification_proves(kept, keep) is True


def test_a_failed_test_of_an_unsaved_draft_still_supersedes_only_its_own_proof():
    """It revokes no durable evidence (nothing saved matches it) but the draft's
    own older proof must not survive the failure either."""
    options = {"api_key": "saved-key"}
    evidence = _fingerprints(alldebrid_definition, options)
    saved = _settings(alldebrid=IntegrationSettings(
        enabled=True, options=options, verification=evidence))

    draft = verification_fingerprint(_alldebrid_material("unsaved-draft"))
    stale = verification_proof(draft)
    assert record_verification_outcome(saved, alldebrid_definition, draft, False) is None
    assert public_integrations(saved, DEFINITIONS)["alldebrid"]["verified"] is True
    assert verification_proves(stale, draft) is False


def test_the_browser_is_never_the_authority_on_proof_supersession():
    """Frontend stale-proof cleanup is hygiene. The server refuses a superseded
    proof whether or not any browser remembered to drop it."""
    from pathlib import Path
    static = Path(__file__).resolve().parents[2] / "frontend" / "static"
    for name in ("ui-settings-page.js", "ui-settings-usenet-servers.js"):
        source = (static / name).read_text(encoding="utf-8")
        assert "forgetTestedDrafts" in source, f"{name} keeps proofs a failed Test retired"
        assert "hygiene" in source.lower()
    # And the server-side supersession has exactly one owner, called from the
    # one generic outcome recorder -- no provider-specific branch anywhere.
    definition = (Path(__file__).resolve().parents[1] / "integrations" / "definition.py").read_text()
    configuration = (Path(__file__).resolve().parents[1] / "integrations" / "configuration.py").read_text()
    assert definition.count("def supersede_verification_proofs(") == 1
    assert configuration.count("supersede_verification_proofs(fingerprint)") == 1
    for named in ("alldebrid", "usenet", "sabnzbd"):
        assert named not in definition.lower()
