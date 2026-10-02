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
        definitions=(definition,), application_operation=operation, configure=lambda: None,
        apply_integration_configuration=AsyncMock(return_value=None), validate_configuration=AsyncMock(),
        notify_applicability_changed=lambda _identity: None,
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


async def test_the_usenet_participation_is_an_ordinary_persisted_option():
    from api.routes import IntegrationConfigurationUpdate, patch_integration_configuration
    stored = _Stored(api_token=TOKEN)
    with _settings_owner(stored):
        await patch_integration_configuration("torbox", IntegrationConfigurationUpdate(
            options={"usenet_enabled": True}), _application())
    options = stored.cfg.integrations["torbox"].options
    assert options["usenet_enabled"] is True and options["api_token"] == TOKEN


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
        torbox = TorBoxProvider(FakeClient(token=TOKEN), usenet=True, staged_input=staged)
        TorBoxHostMaintenance(torbox, MemoryStore())
        registry.register_provider(torbox)
        engine = TransferEngine(TransferRepository(), registry, download_root=str(tmp_path / "downloads"),
                                policy=TransferPolicy(), clock=lambda: 1000.0)
        await engine.initialize()
        service = ApplicationService(engine, staged_input=staged)

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
