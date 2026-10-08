"""TorBox control plane and the canonical NZB boundary.

The token is saved and forgotten only through the canonical integration
mutation, exactly as Real-Debrid's credential is; and an NZB reaches TorBox the
same way whether it was uploaded or arrived through the NZB-link ingress.
"""
from __future__ import annotations

import json
import socket
from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import pytest
from aiohttp import web

import db.database as database
from providers.torbox import admin
from providers.torbox.client import TorBoxAPIError, USENET
from providers.torbox.definition import definition
from transfers.nzb import read as read_nzb
from transfers.staged_input import StagedInputStore

pytestmark = pytest.mark.asyncio

TOKEN = "f00dfeed-0000-4000-8000-acc0un7t0k3n"
ACCOUNT = {"email": "a@e.net", "plan": 2, "plan_name": "Pro", "premium": True,
           "premium_expires_at": "2099-01-01T00:00:00Z"}
NZB = (b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
       b'<file poster="p@e.net" date="1700000000" subject="show [1/1] - &quot;show.mkv&quot; yEnc (1/1)">'
       b"<groups><group>alt.binaries.test</group></groups>"
       b'<segments><segment bytes="1024" number="1">a@e.net</segment></segments></file></nzb>')


class _Stored:
    """One in-memory saved configuration behind every settings read and write."""

    def __init__(self, enabled=False, **options):
        from core.config import AppSettings
        from integrations.definition import IntegrationSettings
        self.cfg = AppSettings(integrations={"torbox": IntegrationSettings(enabled=enabled, options=options)})

    def read(self):
        return self.cfg.model_copy(deep=True)

    def write(self, cfg):
        self.cfg = cfg


def _application():
    @asynccontextmanager
    async def operation():
        yield

    return SimpleNamespace(
        definitions=(definition,), application_operation=operation, configuration_admission=operation,
        configure=lambda: None,
        apply_integration_configuration=AsyncMock(return_value=None), validate_configuration=AsyncMock(),
        notify_applicability_changed=lambda _identity: None,
        refresh_account_entitlement=AsyncMock(return_value=False), option_availability=lambda _definition: {},
        engine=SimpleNamespace(registry=SimpleNamespace(providers={})))


def _settings_owner(stored):
    stack = ExitStack()
    for target in ("api.routes", "core.config", "api.settings_validation_routes"):
        stack.enter_context(patch(f"{target}.get_settings", side_effect=stored.read))
    for target in ("api.routes", "core.config"):
        stack.enter_context(patch(f"{target}.load_settings", side_effect=stored.read))
        stack.enter_context(patch(f"{target}.save_settings", side_effect=stored.write))
        stack.enter_context(patch(f"{target}.apply_settings"))
    return stack


async def test_an_approved_device_is_saved_proven_enabled_and_never_echoed():
    from api import settings_validation_routes as routes
    stored = _Stored()
    with _settings_owner(stored), \
            patch.object(routes.torbox_admin, "poll_authorization", AsyncMock(return_value=admin.Authorized(TOKEN))), \
            patch.object(routes.torbox_admin, "verify", AsyncMock(return_value=ACCOUNT)):
        result = await routes.poll_torbox_authorization(application=_application())
    assert result["state"] == "connected" and result["email"] == "a@e.net"
    projection = result["integration"]
    assert (projection["configured"], projection["verified"], projection["enabled"]) == (True, True, True)
    assert stored.cfg.integrations["torbox"].options["api_token"] == TOKEN
    assert TOKEN not in json.dumps(result)


async def test_an_unproven_or_pending_connection_never_enables():
    from api import settings_validation_routes as routes
    stored = _Stored()
    with _settings_owner(stored), \
            patch.object(routes.torbox_admin, "poll_authorization", AsyncMock(return_value=admin.Authorized(TOKEN))), \
            patch.object(routes.torbox_admin, "verify", AsyncMock(side_effect=TorBoxAPIError("BAD_TOKEN", "", 403))):
        result = await routes.poll_torbox_authorization(application=_application())
    assert (result["integration"]["configured"], result["integration"]["verified"],
            result["integration"]["enabled"]) == (True, False, False)
    pending = _Stored()
    with _settings_owner(pending), \
            patch.object(routes.torbox_admin, "poll_authorization", AsyncMock(return_value={"state": "pending"})):
        assert (await routes.poll_torbox_authorization(application=_application())) == {"state": "pending"}
    assert pending.cfg.integrations["torbox"].options == {}


async def test_the_test_proves_the_saved_token_and_never_enables():
    from api import settings_validation_routes as routes
    stored = _Stored(api_token=TOKEN)
    verify = AsyncMock(return_value=ACCOUNT)
    with _settings_owner(stored), patch.object(routes.torbox_admin, "verify", verify):
        result = await routes.validate_torbox(application=_application())
    assert verify.await_args.args[0].api_token == TOKEN
    assert result["integration"]["verified"] is True and result["integration"]["enabled"] is False
    unconnected = _Stored()
    with _settings_owner(unconnected), pytest.raises(Exception) as missing:
        await routes.validate_torbox(application=_application())
    assert getattr(missing.value, "status_code", None) == 400


async def test_disconnect_forgets_the_token():
    from api import settings_validation_routes as routes
    stored = _Stored(enabled=True, api_token=TOKEN)
    with _settings_owner(stored):
        result = await routes.disconnect_torbox(application=_application())
    assert stored.cfg.integrations["torbox"].options["api_token"] == ""
    assert result["integration"]["configured"] is False


async def test_the_usenet_preference_is_an_ordinary_persisted_option():
    from api.routes import IntegrationConfigurationUpdate, patch_integration_configuration
    stored = _Stored(api_token=TOKEN)
    with _settings_owner(stored):
        await patch_integration_configuration("torbox", IntegrationConfigurationUpdate(
            options={"use_before_usenet": True}), _application())
    options = stored.cfg.integrations["torbox"].options
    assert options["use_before_usenet"] is True and options["api_token"] == TOKEN
    assert "usenet_enabled" not in options


# -- "Use TorBox Before Usenet" follows the account's plan ----------------------------------

PLAN_EXPIRY = "2099-01-01T00:00:00Z"


async def _plan_application(plan, *, before=False):
    """A real application over a TorBox provider whose real account owner
    holds ``plan``'s current account truth; reconfiguring rebuilds nothing."""
    from application.service import ApplicationService
    from integrations.account_entitlement import AccountEntitlementMaintenance
    from integrations.runtime_state import ScopedRuntimeStateStore, credential_scope
    from providers.torbox.account import TorBoxAccountTranslation
    from providers.torbox.provider import TorBoxProvider
    from test_v113_account_entitlement import Store
    from test_v113_torbox_provider import FakeClient
    from transfers.registry import IntegrationRegistry

    client, account = FakeClient(token=TOKEN), {"plan": plan, "premium_expires_at": PLAN_EXPIRY}

    async def user():
        return dict(account)

    client.user = user
    provider = TorBoxProvider(client, use_before_usenet=before)
    provider.account = AccountEntitlementMaintenance(
        provider, TorBoxAccountTranslation(client), ScopedRuntimeStateStore(Store(), credential_scope("torbox", TOKEN)),
        integration_id="torbox")
    provider.lifecycle = provider.account
    await provider.account.refresh_now()
    registry = IntegrationRegistry()
    registry.register_provider(provider)
    application = ApplicationService(SimpleNamespace(registry=registry, repository=None))
    application.definitions = (definition,)
    application.configure = lambda: None
    application.apply_integration_configuration = AsyncMock(return_value=None)
    return application, provider, account


@pytest.mark.parametrize("plan, entitled", [(1, False), (3, False), (2, True)], ids=["essential", "standard", "pro"])
async def test_torbox_before_usenet_can_be_turned_on_only_by_a_plan_with_usenet(plan, entitled):
    """C-T1, C-T2, C-T3: what Settings shows and what the canonical mutation
    accepts are the account's own plan -- a direct API call cannot turn on
    what the plan lacks."""
    from fastapi import HTTPException

    from api.routes import IntegrationConfigurationUpdate, get_settings_ep, patch_integration_configuration
    application, _provider, _account = await _plan_application(plan)
    stored = _Stored(enabled=True, api_token=TOKEN)
    with _settings_owner(stored):
        shown = (await get_settings_ep(application))["integrations"]["torbox"]["option_availability"]
        assert shown == {"use_before_usenet": {"available": entitled,
                                               "requirement": "" if entitled else "Requires TorBox Pro."}}
        change = patch_integration_configuration("torbox", IntegrationConfigurationUpdate(
            options={"use_before_usenet": True}), application)
        if entitled:
            await change
        else:
            with pytest.raises(HTTPException) as refused:
                await change
            assert (refused.value.status_code, refused.value.detail) == (409, "Requires TorBox Pro.")
    options = stored.cfg.integrations["torbox"].options
    assert options.get("use_before_usenet", False) is entitled and "usenet_enabled" not in options


@pytest.mark.parametrize("plan, nzb", [(1, False), (3, False), (2, True)], ids=["essential", "standard", "pro"])
async def test_torbox_always_offers_nzb_and_its_plan_alone_decides_and_degrades(plan, nzb):
    """D: NZB is offered whatever the preference; Essential and Standard are
    not NZB-entitled and not degraded for it, Pro is entitled -- and a plan
    without NZB is out of NZB acquisition by entitlement, not by a toggle."""
    from transfers.models import TransferRequest
    from transfers.registry import IntegrationRegistry
    for before in (False, True):
        _application, provider, _account = await _plan_application(plan, before=before)
        assert "nzb" in provider.descriptor.request_types
        assert ("nzb" in provider.entitlements.request_types, provider.entitlements.degraded) == (nzb, False)
        assert IntegrationRegistry.entitlement_for(provider, TransferRequest("nzb", b"x", "x.nzb")) is nzb


async def test_a_plan_that_loses_usenet_turns_the_saved_preference_off_and_renewal_never_restores_it():
    """C-T4, C-T5: Pro with "Use TorBox Before Usenet" on, then the plan
    becomes Essential. Refreshed account truth proves Usenet is gone, so the
    saved preference converges off through the canonical write; the account
    is never degraded by a preference. Renewing Pro does not turn it back on.
    A refusal TorBox gives a plan that should include NZBs stays truthful
    degradation (C5)."""
    application, provider, account = await _plan_application(2, before=True)
    stored = _Stored(enabled=True, api_token=TOKEN, use_before_usenet=True)
    with _settings_owner(stored):
        await application.converge_entitled_options()                 # Pro: nothing to converge
        assert stored.cfg.integrations["torbox"].options["use_before_usenet"] is True
        assert provider.entitlements.degraded is False

        account["plan"] = 1                                           # downgraded on TorBox
        await application.refresh_account_entitlement("torbox")
        assert provider.entitlements.degraded is False                # what Essential includes, it can do
        await application.converge_entitled_options()
        assert stored.cfg.integrations["torbox"].options["use_before_usenet"] is False
        application.apply_integration_configuration.assert_awaited_once_with("torbox")

        account["plan"] = 2                                           # renewed on TorBox
        await application.refresh_account_entitlement("torbox")
        await application.converge_entitled_options()
    assert stored.cfg.integrations["torbox"].options["use_before_usenet"] is False   # not silently restored

    rebuilt, offered_now, _account = await _plan_application(1)       # the reconfigured provider
    public = offered_now.entitlements.public()
    assert (public["functional"], public["service_class"], public["entitlement"]) == ("usable", "premium", "ready")
    assert rebuilt.option_availability(definition)["use_before_usenet"]["available"] is False

    _pro, drifted, _account = await _plan_application(2, before=True)
    await drifted.account.contract(frozenset({"nzb"}))               # Pro refused NZBs: real drift
    assert drifted.entitlements.degraded is True
    assert _pro.option_availability(definition)["use_before_usenet"]["available"] is True   # never forced off


# -- one NZB path, whatever the ingress -------------------------------------------------

async def test_an_uploaded_and_a_linked_nzb_reach_the_same_torbox_path(tmp_path, monkeypatch):
    import services.network_safety as safety
    from application.service import ApplicationService
    from providers.torbox.host_runtime import TorBoxHostMaintenance
    from providers.torbox.provider import TorBoxProvider
    from test_v113_torbox_provider import FakeClient, MemoryStore
    from transfers.convergence_engine import TransferEngine
    from transfers.policy import TransferPolicy
    from transfers.recovery_repository import TransferRepository
    from transfers.registry import IntegrationRegistry

    async def handler(request):
        return web.Response(body=NZB)

    app = web.Application()
    app.router.add_route("GET", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    real = safety.validate_resolved_public_destination

    async def allow_fixture(uri, **kwargs):
        parsed = urlsplit(uri)
        return uri if (parsed.hostname, parsed.port) == ("indexer.example", port) else await real(uri, **kwargs)

    async def local_resolve(self, host, port=0, family=socket.AF_UNSPEC):
        return [{"hostname": host, "host": "127.0.0.1", "port": port, "family": socket.AF_INET,
                 "proto": socket.IPPROTO_TCP, "flags": socket.AI_NUMERICHOST}]

    monkeypatch.setattr(safety, "validate_resolved_public_destination", allow_fixture)
    monkeypatch.setattr(safety.PublicDestinationResolver, "resolve", local_resolve)
    try:
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
        await database.init_db()
        staged = StagedInputStore(str(tmp_path / "staged"))
        registry = IntegrationRegistry()
        torbox = TorBoxProvider(FakeClient(token=TOKEN), staged_input=staged)
        TorBoxHostMaintenance(torbox, MemoryStore())
        registry.register_provider(torbox)
        engine = TransferEngine(TransferRepository(), registry, download_root=str(tmp_path / "downloads"),
                                policy=TransferPolicy(), clock=lambda: 1000.0)
        await engine.initialize()
        service = ApplicationService(engine, staged_input=staged, nzb_reader=read_nzb)

        async def chunks():
            yield NZB

        await service.submit_nzb(chunks(), "show.nzb")
        await service.submit_nzb_link(f"http://indexer.example:{port}/get/show.nzb?apikey=K3Y")
        await engine.resolve_pending()
    finally:
        await runner.cleanup()

    uploads = [call for call in torbox.client.calls if call[0] == "create_usenet"]
    assert uploads == [("create_usenet", NZB, "show.nzb")] * 2
    assert all("K3Y" not in json.dumps(call, default=str) for call in torbox.client.calls)
    assert not [call for call in torbox.client.calls if call[0] == "create_webdl"]
    assert len(torbox.client.objects[USENET]) == 2
