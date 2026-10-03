"""Account-entitlement freshness: passive cadence versus explicit refresh.

Two modes, never conflated: the background owner uses last-known-good truth
and refreshes it every five minutes; an explicit operator act -- a successful
Test of the SAVED account, or re-enabling the integration -- fetches account
truth now, whatever passive freshness or retry backoff say, without changing
enablement or credentials. A Test of an unsaved draft touches no live truth.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from application.service import ApplicationService
from integrations import account_entitlement
from integrations.account_entitlement import ACCOUNT_REFRESH_SECONDS, AccountEntitlementMaintenance
from integrations.runtime_state import ScopedRuntimeStateStore, credential_scope
from providers.torbox.account import TorBoxAccountTranslation
from providers.torbox.provider import TorBoxProvider
from test_v113_account_entitlement import ALL, NOW, Lab, Store, owner
from test_v113_torbox_provider import FakeClient
from transfers.entitlement import AccountServiceClass, EntitlementReadiness
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio


# -- the neutral owner -------------------------------------------------------------

async def test_the_background_freshness_is_five_minutes():
    assert ACCOUNT_REFRESH_SECONDS == 5 * 60
    store = Store()
    _provider, value, _translation, _now = owner({"premium": True, "until": NOW + 86400}, store)
    await value.maintain()
    [record] = store.records.values()
    assert record.stale_after == NOW + 300


async def test_passive_refresh_waits_for_freshness_and_runs_when_due():
    _provider, value, translation, now = owner({"premium": True, "until": NOW + 86400}, Store())
    await value.maintain()
    assert translation.fetches == 1
    now[0] = NOW + 299
    await value.maintain()
    assert translation.fetches == 1
    now[0] = NOW + 300
    await value.maintain()
    assert translation.fetches == 2


async def test_refresh_now_bypasses_freshness_and_retry_backoff_and_persists_and_announces():
    store, woken, statuses = Store(), [], []
    _provider, value, translation, now = owner({"premium": False}, store, woken=woken, statuses=statuses)
    await value.maintain()
    generation = next(iter(store.records.values())).generation
    woken.clear(), statuses.clear()

    # Still fresh: passive maintenance would not ask; the operator does.
    translation.answer = {"premium": True, "until": NOW + 86400}
    now[0] = NOW + 30
    current = await value.refresh_now()
    assert translation.fetches == 2
    assert current.service_class == AccountServiceClass.PREMIUM and current.request_types == ALL
    assert next(iter(store.records.values())).generation == generation + 1
    assert woken == ["alpha"] and statuses == [True]

    # A failed background refresh arms the retry delay...
    translation.answer = RuntimeError("upstream down")
    now[0] = NOW + 400
    await value.maintain()
    fetches = translation.fetches
    now[0] = NOW + 401
    await value.maintain()
    assert translation.fetches == fetches              # backoff holds the background
    # ...which an explicit refresh does not obey,
    translation.answer = {"premium": False}
    await value.refresh_now()
    assert translation.fetches == fetches + 1
    assert value.entitlements.service_class == AccountServiceClass.STANDARD


async def test_a_failed_explicit_refresh_keeps_last_known_good_and_never_loops():
    _provider, value, translation, now = owner({"premium": True, "until": NOW + 86400}, Store())
    await value.maintain()
    translation.answer = RuntimeError("upstream down")
    now[0] = NOW + 10
    current = await value.refresh_now()
    assert current.service_class == AccountServiceClass.PREMIUM and current.request_types == ALL
    fetches = translation.fetches
    for step in range(1, 4):                         # the ordinary background retry, not a loop
        now[0] = NOW + 10 + step
        await value.maintain()
    assert translation.fetches == fetches


async def test_a_known_expiry_needs_no_poll_even_when_an_explicit_refresh_fails():
    _provider, value, translation, now = owner({"premium": True, "until": NOW + 60}, Store())
    await value.maintain()
    translation.answer = RuntimeError("upstream down")
    now[0] = NOW + 61
    current = await value.refresh_now()
    assert current.service_class == AccountServiceClass.STANDARD and current.degraded


async def test_an_explicit_refresh_works_while_disabled_and_passive_maintenance_does_not():
    store = Store()
    provider, value, translation, _now = owner({"premium": True, "until": NOW + 86400}, store)
    provider.descriptor = provider.descriptor.__class__(**{**provider.descriptor.__dict__, "enabled": False})
    await value.maintain()
    assert translation.fetches == 0
    current = await value.refresh_now()
    assert translation.fetches == 1 and current.readiness == EntitlementReadiness.READY
    assert provider.descriptor.enabled is False            # proof, not participation


async def test_explicit_observation_and_contraction_serialize_through_the_one_owner_lock():
    store = Store()
    _provider, value, translation, _now = owner({"premium": True, "until": NOW + 86400}, store)
    await value.maintain()
    await value.contract({"magnet", "torrent"})
    started, release = asyncio.Event(), asyncio.Event()
    same_facts = {"premium": True, "until": NOW + 86400}

    async def slow_fetch():
        started.set()
        await release.wait()
        return same_facts

    translation.fetch = slow_fetch
    forced = asyncio.create_task(value.refresh_now())
    await started.wait()
    passive = asyncio.create_task(value.maintain())
    observed = asyncio.create_task(value.observe(same_facts))
    await asyncio.sleep(0)
    assert not passive.done() and not observed.done()        # held behind the owner's lock
    release.set()
    await asyncio.gather(forced, passive, observed)
    # Unchanged account truth kept the proven refusal through every path.
    assert value.entitlements.request_types == {"http", "https"}
    generations = [record.generation for record in store.records.values()]
    assert generations == [max(generations)]
    # A real change converges to authoritative truth and lifts it.
    translation.fetch = lambda: asyncio.sleep(0, {"premium": True, "until": NOW + 2 * 86400})
    await asyncio.gather(value.refresh_now(), value.observe({"premium": True, "until": NOW + 2 * 86400}))
    assert value.entitlements.request_types == ALL


async def test_a_new_credential_scope_never_receives_the_old_account_s_facts():
    store = Store()
    _provider, old, _translation, _now = owner({"premium": True, "until": NOW + 86400}, store, scope="key-one")
    await old.maintain()
    _provider, new, translation, _now = owner(RuntimeError("down"), store, scope="key-two")
    await asyncio.gather(old.refresh_now(), new.refresh_now())
    assert new.entitlements.readiness == EntitlementReadiness.UNRESOLVED
    assert len(store.records) == 1                             # only the old scope holds facts


# -- the neutral application seam ------------------------------------------------------

class Hosts:
    async def start(self): ...
    async def stop(self): ...
    async def maintain(self): ...


def application_with(*providers):
    registry = IntegrationRegistry()
    for provider in providers:
        registry.register_provider(provider)
    return ApplicationService(SimpleNamespace(registry=registry, repository=None))


async def test_the_application_seam_refreshes_every_account_component_and_is_neutral():
    provider, value, translation, _now = owner({"premium": True, "until": NOW + 86400}, Store())
    provider.lifecycle = (Hosts(), value)
    plain = Lab("beta")
    plain.lifecycle = Hosts()
    application = application_with(provider, plain)
    assert await application.refresh_account_entitlement("alpha") is True
    assert translation.fetches == 1
    assert await application.refresh_account_entitlement("beta") is False      # nothing to refresh
    assert await application.refresh_account_entitlement("absent") is False


async def test_an_upstream_failure_never_escapes_the_seam():
    provider, value, translation, _now = owner(RuntimeError("down"), Store())
    provider.lifecycle = value
    value.refresh_now = AsyncMock(side_effect=RuntimeError("unexpected"))
    assert await application_with(provider).refresh_account_entitlement("alpha") is False


# -- 11.1: the observed TorBox sequence ----------------------------------------------------

async def test_a_torbox_plan_upgrade_is_seen_on_explicit_refresh_without_reconnecting():
    client, plan = FakeClient(), {"plan": 0, "premium_expires_at": None}

    async def user():
        return dict(plan)

    client.user = user
    provider = TorBoxProvider(client)
    store = Store()
    provider.account = AccountEntitlementMaintenance(
        provider, TorBoxAccountTranslation(client),
        ScopedRuntimeStateStore(store, credential_scope("torbox", client.token)),
        integration_id="torbox", clock=lambda: NOW)
    provider.lifecycle = provider.account
    application = application_with(provider)
    await provider.account.maintain()
    assert provider.entitlements.request_types == frozenset() and provider.entitlements.degraded

    plan.update({"plan": 1, "premium_expires_at": "2099-01-01T00:00:00Z"})    # upgraded on TorBox
    await provider.account.maintain()
    assert provider.entitlements.request_types == frozenset()               # still fresh: passive waits
    await application.refresh_account_entitlement("torbox")                  # Test / re-enable
    assert provider.entitlements.service_class == AccountServiceClass.PREMIUM
    assert {"magnet", "torrent", "http", "https"} <= provider.entitlements.request_types
    assert len(store.records) == 1                                            # same scope, same token


# -- 7: Test routes -----------------------------------------------------------------------

def _routes_application(refresh):
    from test_v113_torbox_routes import _application
    application = _application()
    application.refresh_account_entitlement = refresh
    return application


@pytest.mark.parametrize("enabled", [True, False])
async def test_a_torbox_test_of_the_saved_account_refreshes_live_truth_and_never_enables(enabled):
    from api import settings_validation_routes as routes
    from test_v113_torbox_routes import ACCOUNT, TOKEN, _settings_owner, _Stored
    stored, refresh = _Stored(enabled=enabled, api_token=TOKEN), AsyncMock(return_value=True)
    with _settings_owner(stored), patch.object(routes.torbox_admin, "verify", AsyncMock(return_value=ACCOUNT)):
        result = await routes.validate_torbox(application=_routes_application(refresh))
    refresh.assert_awaited_once_with("torbox")
    assert result["ok"] is True and stored.cfg.integrations["torbox"].enabled is enabled


async def test_a_failed_test_refreshes_nothing():
    from api import settings_validation_routes as routes
    from providers.torbox.client import TorBoxAPIError
    from test_v113_torbox_routes import TOKEN, _settings_owner, _Stored
    stored, refresh = _Stored(enabled=True, api_token=TOKEN), AsyncMock(return_value=True)
    with _settings_owner(stored), patch.object(
            routes.torbox_admin, "verify", AsyncMock(side_effect=TorBoxAPIError("DATABASE_ERROR", "", 500))), \
            pytest.raises(Exception):
        await routes.validate_torbox(application=_routes_application(refresh))
    refresh.assert_not_awaited()


async def test_a_realdebrid_test_of_the_saved_account_refreshes_live_truth():
    from api import settings_validation_routes as routes
    from core.config import AppSettings
    from integrations.definition import IntegrationSettings
    stored = SimpleNamespace(cfg=AppSettings(integrations={"realdebrid": IntegrationSettings(
        enabled=False, options={"client_id": "c", "client_secret": "s", "refresh_token": "r"})}))
    refresh = AsyncMock(return_value=True)
    application = _routes_application(refresh)
    from providers.realdebrid.definition import definition as realdebrid_definition
    application.definitions = (realdebrid_definition,)
    with patch("api.settings_validation_routes.get_settings", side_effect=lambda: stored.cfg.model_copy(deep=True)), \
            patch("core.config.load_settings", side_effect=lambda: stored.cfg.model_copy(deep=True)), \
            patch("core.config.save_settings", side_effect=lambda cfg: setattr(stored, "cfg", cfg)), \
            patch("core.config.apply_settings"), \
            patch.object(routes.realdebrid_admin, "verify", AsyncMock(return_value={"username": "a"})):
        await routes.validate_realdebrid(application=application)
    refresh.assert_awaited_once_with("realdebrid")
    assert stored.cfg.integrations["realdebrid"].enabled is False


@pytest.mark.parametrize("draft, refreshed", [("", True), ("an-unsaved-draft", False)])
async def test_an_alldebrid_test_refreshes_only_for_the_saved_key(draft, refreshed):
    from api import settings_validation_routes as validation
    from core.config import AppSettings
    from integrations.definition import IntegrationSettings
    from providers.alldebrid.definition import definition as alldebrid_definition
    stored = SimpleNamespace(cfg=AppSettings(integrations={"alldebrid": IntegrationSettings(
        enabled=True, options={"api_key": "saved-key"})}))
    refresh = AsyncMock(return_value=True)
    application = _routes_application(refresh)
    application.definitions = (alldebrid_definition,)
    user = {"user": {"username": "someone", "isPremium": True, "premiumUntil": 0}}
    with patch("api.settings_validation_routes.get_settings", side_effect=lambda: stored.cfg.model_copy(deep=True)), \
            patch("core.config.load_settings", side_effect=lambda: stored.cfg.model_copy(deep=True)), \
            patch("core.config.save_settings", side_effect=lambda cfg: setattr(stored, "cfg", cfg)), \
            patch("core.config.apply_settings"), \
            patch("api.settings_validation_routes.AllDebridService") as service:
        service.return_value.get_user = AsyncMock(return_value=user)
        result = await validation.validate_alldebrid(
            validation.AllDebridValidationRequest(api_key=draft), application=application)
    assert result["ok"] is True
    assert refresh.await_count == (1 if refreshed else 0)
    assert stored.cfg.integrations["alldebrid"].options["api_key"] == "saved-key"


# -- 8: the enable transition -------------------------------------------------------------------

@pytest.mark.parametrize("before, after, refreshes", [
    (False, True, 1),     # re-enable: one explicit refresh, whatever the TTL says
    (True, False, 0),     # disable fetches nothing
    (True, True, 0),      # no transition
    (False, False, 0),
])
async def test_only_a_false_to_true_enable_refreshes_account_truth(before, after, refreshes):
    from api import routes
    from test_v113_torbox_routes import TOKEN, _settings_owner, _Stored
    stored, refresh = _Stored(enabled=before, api_token=TOKEN), AsyncMock(return_value=False)
    with _settings_owner(stored):
        result = await routes.patch_integration_configuration(
            "torbox", routes.IntegrationConfigurationUpdate(enabled=after), application=_routes_application(refresh))
    assert refresh.await_count == refreshes
    if refreshes:
        refresh.assert_awaited_with("torbox")
    # A failed or impossible account check never rewrites the operator's choice.
    assert result["enabled"] is after and stored.cfg.integrations["torbox"].enabled is after


async def test_the_account_owner_module_names_no_integration():
    import inspect
    text = inspect.getsource(account_entitlement).casefold()
    for name in ("torbox", "alldebrid", "realdebrid"):
        assert name not in text
