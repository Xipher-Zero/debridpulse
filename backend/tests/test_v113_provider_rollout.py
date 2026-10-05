"""Backup torrents across every torrent provider (TASK3d-2).

AllDebrid, Real-Debrid and Debrid-Link now offer what TorBox offered first:
each prepares a magnet or torrent as a backup only while the operator turns on
that provider's own "Prepare Backup Torrents" (default off), and each states
its own active capacity on the shared contract -- AllDebrid its documented 30
active magnets and the account's processing magnets, Real-Debrid its own
``activeCount`` (count and limit), Debrid-Link nothing it can state (only its
``maxTransfer`` refusal says the seedbox is full). Limits constrain capacity;
they never remove a provider. Hoster links keep cross-provider routing and
failover with no backup warmup.
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from test_v113_collection_route_generic_closure import Clock
from test_v113_debridlink_provider import ok, refused
from test_v113_debridlink_provider import service as dl_service
from test_v113_debridlink_provider import torrent as dl_torrent
from test_v113_realdebrid_provider import FakeClient as RDFakeClient
from test_v113_realdebrid_provider import info as rd_info
from test_v113_standby_preparation import Primary, lab, magnet, root_of, submitted
from test_v113_standby_promotion import FailingPrimary
from test_v113_torbox_provider import FakeClient as TBFakeClient

from integrations.definition import IntegrationSettings
from providers.alldebrid.account import ACTIVE_MAGNET_MAXIMUM
from providers.alldebrid.client import AllDebridAPIError, AllDebridService
from providers.alldebrid.provider import AllDebridProvider
from providers.debridlink.definition import DebridLinkOptions
from providers.debridlink.provider import DebridLinkProvider
from providers.realdebrid.client import RealDebridAPIError
from providers.realdebrid.provider import RealDebridProvider
from providers.torbox.provider import TorBoxProvider
from transfers.applicability import HostClaim, HostClaimScope, ProviderApplicability
from transfers.contracts import speculative_preparation
from transfers.errors import TransferError
from transfers.models import ActiveCapacity, Ownership, TransferRequest
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

ROLLED_OUT = ("alldebrid", "realdebrid", "debridlink")
MAGNET = TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40, fingerprint="a" * 40)
TORRENT = TransferRequest("torrent", b"metainfo", name="t.torrent", fingerprint="b" * 40)
HOSTER = TransferRequest("https", "https://hoster.example/f/1")
NZB = TransferRequest("nzb", b"<nzb/>", name="p.nzb")
allowed = IntegrationRegistry.speculative_preparation_allowed


def built(identity, **options):
    definition = importlib.import_module(f"providers.{identity}.definition").definition
    return definition.build(IntegrationSettings(enabled=True, options=options), SimpleNamespace())


# -- neutral provider stubs over the real adapters ---------------------------------------------------------

class ADClient:
    """An AllDebrid account: magnets by id, processing until marked ready."""

    def __init__(self, *, active=0):
        self.magnets = {f"x{index}": {"id": f"x{index}", "statusCode": 1} for index in range(active)}
        self.calls, self.next_id = [], 100

    async def upload_magnet(self, magnet):
        self.calls.append(("upload_magnet", magnet))
        self.next_id += 1
        native = {"id": str(self.next_id), "name": "Show", "statusCode": 1, "size": 10, "downloaded": 1}
        self.magnets[native["id"]] = native
        return dict(native)

    async def get_magnet_status(self, magnet_id=None):
        self.calls.append(("get_magnet_status", magnet_id))
        if magnet_id is None:
            return [dict(item) for item in self.magnets.values()]
        return [dict(self.magnets[magnet_id])] if magnet_id in self.magnets else []

    async def account_magnet_status_codes(self):
        self.calls.append(("account_magnet_status_codes",))
        return [item["statusCode"] for item in self.magnets.values()]

    async def delete_magnet(self, magnet_id):
        self.calls.append(("delete_magnet", magnet_id))
        self.magnets.pop(magnet_id, None)
        return True


class RDClient(RDFakeClient):
    def __init__(self, *, nb=0, limit=25, **responses):
        super().__init__(**responses)
        self.count = {"nb": nb, "limit": limit}

    async def active_count(self):
        self.calls.append(("active_count",))
        return dict(self.count)


def rd_client(**counts):
    return RDClient(add_magnet={"id": "T1", "uri": "https://api.real-debrid.com/rest/1.0/torrents/info/T1"},
                    select_files=202, torrent_info=lambda _id: rd_info("downloading"),
                    delete_torrent=None, **counts)


class Seedbox:
    """A Debrid-Link seedbox over the real client: ``add`` takes the next
    scripted answer, ``list`` answers exactly the torrents asked for, and
    ``remove`` removes."""

    def __init__(self, *adds):
        self.adds, self.torrents, self.calls = list(adds), {}, []

    async def __call__(self, method, url, *, headers=None, params=None, data=None, timeout=None):
        import json

        from providers.debridlink.client import API, RawResponse
        path = url.removeprefix(API + "/")
        self.calls.append({"method": method, "url": url, "params": dict(params or {})})
        if path == "seedbox/add":
            status, payload, _headers = self.adds.pop(0)
            if payload.get("success"):
                self.torrents[payload["value"]["id"]] = payload["value"]
        elif path == "seedbox/list":
            ids = str((params or {}).get("ids") or "")
            found = [item for key, item in self.torrents.items() if not ids or key in ids.split(",")]
            status, payload = 200, {"success": True, "value": found, "pagination": {"next": -1}}
        elif path.endswith("/remove"):
            torrent_id = path.split("/")[1]
            self.torrents.pop(torrent_id, None)
            status, payload = 200, {"success": True, "value": [torrent_id]}
        else:
            raise AssertionError(path)
        return RawResponse(status, {}, json.dumps(payload).encode())


def seedbox(*adds):
    from test_v113_debridlink_provider import KEY

    from providers.debridlink.client import DebridLinkService
    transport = Seedbox(*adds)
    return DebridLinkService(KEY, transport=transport), transport


def tb_provider(client=None, *, on=True):
    provider = TorBoxProvider(client or TBFakeClient(), prepare_backup_torrents=on)
    provider.account = SimpleNamespace(contract=AsyncMock(), entitlements=None)
    return provider


# -- consent: off by default, per provider, torrents only ------------------------------------------------------

@pytest.mark.parametrize("identity", ROLLED_OUT)
async def test_each_provider_is_off_until_its_own_operator_choice(identity):
    assert [allowed(built(identity), request) for request in (MAGNET, TORRENT, HOSTER, NZB)] == [False] * 4
    assert [allowed(built(identity, prepare_backup_torrents=True), request)
            for request in (MAGNET, TORRENT, HOSTER, NZB)] == [True, True, False, False]
    for other in set(ROLLED_OUT) - {identity}:                # enabling one never enables another
        assert allowed(built(other), MAGNET) is False


# -- active capacity, as each provider states it ----------------------------------------------------------------

async def test_alldebrid_states_thirty_and_counts_the_accounts_processing_magnets():
    client = ADClient()
    client.magnets = {str(code): {"id": str(code), "statusCode": code} for code in (0, 1, 2, 3, 4, 5, 11)}
    assert await AllDebridProvider(client=client).active_capacity(MAGNET) == ActiveCapacity(ACTIVE_MAGNET_MAXIMUM, 4)
    assert (await AllDebridProvider(client=client, max_active_torrents=10).active_capacity(MAGNET)).maximum == 10
    assert await AllDebridProvider(client=client).active_capacity(HOSTER) is None

    async def disabled():
        raise AllDebridAPIError("MAGNET_INVALID_ID", "list all is disabled")

    client.account_magnet_status_codes = disabled             # an unreadable list: occupancy unknown
    assert await AllDebridProvider(client=client).active_capacity(MAGNET) == ActiveCapacity(30, None)


def ad_listing(data):
    """The real AllDebrid client over one ``magnet/status`` answer's ``data``."""
    client = AllDebridService("key")

    async def post(base, endpoint, payload=None):
        assert (endpoint, payload) == ("magnet/status", None)
        return data

    client._post = post
    return client


@pytest.mark.parametrize("data, occupancy", [
    ({"magnets": []}, 0),                                      # a valid empty list proves zero
    ({"magnets": [{"id": 1, "statusCode": 1}, {"id": 2, "statusCode": 4}, {"id": 3, "statusCode": 0}]}, 2),
    ({}, None),                                                # magnets missing
    ({"magnets": None}, None),                                 # magnets null
    ({"magnets": "unexpected"}, None),                         # magnets of the wrong type
    ({"magnets": {"id": 1, "statusCode": 1}}, None),           # one object is not the account's list
    ([], None),                                                # data itself malformed
    ({"magnets": [{"id": 1, "statusCode": 1}, {"id": 2}]}, None),                    # a statusCode missing
    ({"magnets": [{"id": 1, "statusCode": 1}, {"id": 2, "statusCode": "1"}]}, None),  # a string statusCode
    ({"magnets": [{"id": 1, "statusCode": 1}, {"id": 2, "statusCode": True}]}, None),  # a bool statusCode
    ({"magnets": [{"id": 1, "statusCode": 1}, {"id": 2, "statusCode": -1}]}, None),   # a negative statusCode
    ({"magnets": [{"id": 1, "statusCode": 1}, "x"]}, None),                          # a record not an object
])
async def test_alldebrid_occupancy_is_unknown_never_zero_when_the_listing_is_malformed(data, occupancy):
    provider = AllDebridProvider(client=ad_listing(data), max_active_torrents=10)
    assert await provider.active_capacity(MAGNET) == ActiveCapacity(10, occupancy)


@pytest.mark.parametrize("count, override, expected", [
    ({"nb": 3, "limit": 25}, None, ActiveCapacity(25, 3)),     # the account's own limit and count
    ({"nb": 3, "limit": 25}, 10, ActiveCapacity(10, 3)),       # a lower ceiling wins
    ({"nb": 3, "limit": 25}, 40, ActiveCapacity(25, 3)),       # a ceiling never raises it
    ({"nb": 3, "limit": 8}, None, ActiveCapacity(8, 3)),       # the limit follows the account
    ({"nb": "x", "limit": 0}, None, ActiveCapacity(None, None)),   # nothing sane: unknown
])
async def test_realdebrid_states_its_own_active_count_and_limit(count, override, expected):
    provider = RealDebridProvider(RDClient(**count), max_active_torrents=override)
    assert await provider.active_capacity(MAGNET) == expected
    assert await provider.active_capacity(HOSTER) is None


async def test_debridlink_states_a_torrent_capacity_it_cannot_measure_and_offers_no_ceiling():
    client, _transport = dl_service({})
    provider = DebridLinkProvider(client)
    assert await provider.active_capacity(MAGNET) == ActiveCapacity(None, None)
    assert await provider.active_capacity(HOSTER) is None
    assert "max_active_torrents" not in DebridLinkOptions.model_fields


# -- a speculative refusal never contracts the account ------------------------------------------------------------

async def test_a_backups_plan_refusal_never_contracts_alldebrid_or_realdebrid():
    ad_client = AsyncMock()
    ad_client.upload_magnet.side_effect = AllDebridAPIError("MAGNET_MUST_BE_PREMIUM", "premium only")
    rd = RDClient(add_magnet=RealDebridAPIError(9, "permission_denied", 403))
    for provider in (AllDebridProvider(client=ad_client), RealDebridProvider(rd)):
        provider.account = SimpleNamespace(contract=AsyncMock())
        with speculative_preparation(), pytest.raises(TransferError):
            await provider.resolve(MAGNET)
        provider.account.contract.assert_not_awaited()
        rd.responses["add_magnet"] = RealDebridAPIError(9, "permission_denied", 403)
        with pytest.raises(TransferError):                     # the same refusal of a primary still does
            await provider.resolve(MAGNET)
        provider.account.contract.assert_awaited_once()


# -- Settings: options persist only when set ----------------------------------------------------------------------

def _stored(identity, **options):
    from core.config import AppSettings
    return SimpleNamespace(cfg=AppSettings(integrations={identity: IntegrationSettings(enabled=True, options=options)}))


@pytest.mark.parametrize("identity, ceiling", [("alldebrid", 31), ("realdebrid", 0), ("debridlink", None)])
async def test_each_providers_options_persist_through_the_existing_path(identity, ceiling):
    from fastapi import HTTPException
    from test_v113_torbox_routes import _application

    from api.routes import (
        IntegrationConfigurationUpdate,
        patch_integration_configuration,
    )
    definition = importlib.import_module(f"providers.{identity}.definition").definition
    stored = _stored(identity)
    application = _application()
    application.definitions = (definition,)

    def read():
        return stored.cfg.model_copy(deep=True)

    def write(cfg):
        stored.cfg = cfg

    async def save(options):
        with patch("api.routes.get_settings", side_effect=read), patch("core.config.get_settings", side_effect=read), \
                patch("api.settings_validation_routes.get_settings", side_effect=read), \
                patch("api.routes.load_settings", side_effect=read), patch("core.config.load_settings", side_effect=read), \
                patch("api.routes.save_settings", side_effect=write), patch("core.config.save_settings", side_effect=write), \
                patch("api.routes.apply_settings"), patch("core.config.apply_settings"):
            await patch_integration_configuration(identity, IntegrationConfigurationUpdate(options=options), application)

    await save({"prepare_backup_torrents": True})
    options = stored.cfg.integrations[identity].options
    assert options["prepare_backup_torrents"] is True
    assert options.get("max_active_torrents") is None                         # no synthetic ceiling
    if ceiling is not None:
        await save({"max_active_torrents": 5})
        assert stored.cfg.integrations[identity].options["max_active_torrents"] == 5
        await save({"max_active_torrents": None})
        assert stored.cfg.integrations[identity].options["max_active_torrents"] is None
        with pytest.raises(HTTPException):
            await save({"max_active_torrents": ceiling})                      # out of range
    else:
        try:                                                                   # Debrid-Link has no such option:
            await save({"max_active_torrents": 5})                            # refused or dropped, never stored
        except HTTPException:
            pass
        assert "max_active_torrents" not in stored.cfg.integrations[identity].options


# -- Web/URL: every specialized claimant participates; no warmup --------------------------------------------------

async def test_a_hoster_link_routes_and_fails_over_across_every_claimant_with_no_backup_warmup():
    registry = IntegrationRegistry()
    claim = ProviderApplicability(specialized_hosts=(HostClaim("hoster.example", HostClaimScope.DOMAIN,
                                                               frozenset({"http", "https"})),), specialized=True)
    dl, _transport = dl_service({})
    providers = [AllDebridProvider(client=ADClient(), prepare_backup_torrents=True),
                 RealDebridProvider(RDClient(), prepare_backup_torrents=True),
                 DebridLinkProvider(dl, prepare_backup_torrents=True), tb_provider()]
    for provider in providers:                     # each one's host maintenance publishes the claim
        provider.applicability = claim
        provider.applicability_for = lambda _request, claim=claim: claim
        registry.register_provider(provider)
    order = [provider.descriptor.id for provider in registry.eligible_providers(HOSTER)]
    assert sorted(order) == ["alldebrid", "debridlink", "realdebrid", "torbox"]
    exhausted = set()
    for expected in order:                         # ordinary exhaustion failover reaches every claimant
        assert registry.provider_route(HOSTER, exhausted=frozenset(exhausted)).provider.descriptor.id == expected
        exhausted.add(expected)
    assert registry.provider_route(HOSTER, exhausted=frozenset(exhausted)).provider is None
    assert [allowed(provider, HOSTER) for provider in providers] == [False] * 4   # never warmed in reserve


# -- engine: every enabled provider holds its own backup under one root -------------------------------------------

async def every_provider(tmp_path, monkeypatch, *, ad=None, primary=None, clock=None):
    ad = ad or ADClient()
    rd = rd_client()
    dl, _transport = seedbox(*[ok(dl_torrent("t1", files=[]))] * 3)
    tb = TBFakeClient()
    providers = (primary or Primary(), AllDebridProvider(client=ad, prepare_backup_torrents=True),
                 RealDebridProvider(rd, prepare_backup_torrents=True),
                 DebridLinkProvider(dl, prepare_backup_torrents=True), tb_provider(tb))
    repository, _registry, engine = await lab(tmp_path, monkeypatch, *providers, clock=clock)
    return repository, engine, providers, ad, rd, tb


async def test_one_root_holds_a_backup_at_every_enabled_provider(tmp_path, monkeypatch):
    repository, engine, _providers, ad, rd, _tb = await every_provider(tmp_path, monkeypatch)
    transfer = await submitted(engine)
    for _ in range(3):
        await engine.resolve_pending()

    standbys = await repository.standbys(transfer.id)
    assert sorted((item["provider_id"], item["state"]) for item in standbys) == [
        ("alldebrid", "bound"), ("debridlink", "bound"), ("realdebrid", "bound"), ("torbox", "bound")]
    root = await root_of(repository, transfer)
    assert root.resource.provider_id == "provider-a" and len(await repository.active()) == 1
    assert len([call for call in ad.calls if call[0] == "upload_magnet"]) == 1
    assert len([call for call in rd.calls if call[0] == "add_magnet"]) == 1
    assert all(candidate.provider_id == "provider-a" for artifact in await repository.artifacts(transfer.id)
               for candidate in artifact.candidates)


async def test_one_full_provider_never_blocks_another_providers_backup(tmp_path, monkeypatch):
    full = ADClient(active=ACTIVE_MAGNET_MAXIMUM)              # AllDebrid's account is at its 30
    repository, engine, _providers, ad, _rd, _tb = await every_provider(tmp_path, monkeypatch, ad=full)
    transfer = await submitted(engine)
    for _ in range(3):
        await engine.resolve_pending()
    states = {item["provider_id"]: item["state"] for item in await repository.standbys(transfer.id)}
    assert states == {"alldebrid": "deferred", "debridlink": "bound", "realdebrid": "bound", "torbox": "bound"}
    assert not any(call[0] == "upload_magnet" for call in ad.calls)            # no productive call into it


async def test_a_later_failover_reuses_the_next_providers_prepared_backup(tmp_path, monkeypatch):
    clock = Clock()
    primary = FailingPrimary()
    repository, engine, _providers, ad, _rd, _tb = await every_provider(tmp_path, monkeypatch, primary=primary,
                                                                        clock=clock)
    transfer = await submitted(engine)
    for _ in range(3):
        await engine.resolve_pending()
    prepared = {item["provider_id"]: item for item in await repository.standbys(transfer.id)}

    primary.failed = True
    for _ in range(6):
        clock.now += 3_600
        await engine.resolve_pending()

    root = await root_of(repository, transfer)
    chosen = root.resource.provider_id
    assert chosen in prepared and root.resource.id == prepared[chosen]["resource"].id   # the exact backup
    assert root.resource.ownership == Ownership.CREATED
    assert len([call for call in ad.calls if call[0] == "upload_magnet"]) == 1        # never created twice


async def test_a_debridlink_transfer_limit_reclaims_one_backup_and_retries_once(tmp_path, monkeypatch):
    dl, transport = seedbox(ok(dl_torrent("t1", files=[])), refused("maxTransfer"), ok(dl_torrent("t2", files=[])))
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(),
                                              DebridLinkProvider(dl, prepare_backup_torrents=True))
    first = await submitted(engine)
    await engine.resolve_pending()
    assert (await repository.standbys(first.id))[0]["state"] == "bound"

    request = magnet("c" * 40)
    second = await engine.submit((TransferRequest(request.kind, request.payload, name=request.name,
                                                  fingerprint=request.fingerprint,
                                                  preferred_provider="debridlink"),), name="Show", deduplicate=False)
    await engine._resolve(await root_of(repository, second))

    removed = [call for call in transport.calls if call["method"] == "DELETE"]
    assert len(removed) == 1 and removed[0]["url"].endswith("seedbox/t1/remove")      # one backup given back
    assert (await root_of(repository, second)).resource.provider_id == "debridlink"   # the retry succeeded



# -- Settings markup: one toggle per provider, a ceiling only where a maximum exists ---------------------------------

def test_each_provider_card_offers_backup_torrents_and_a_ceiling_only_where_a_maximum_is_known():
    from pathlib import Path
    settings = (Path(__file__).resolve().parents[2] / "frontend" / "static" / "ui-settings-page.js").read_text()
    for identity in ROLLED_OUT:
        assert (f"{identity}_prepare_backup_torrents: {{scope: 'integration:{identity}', "
                f"option: 'prepare_backup_torrents', commit: 'immediate'}}") in settings
        assert f"tuningToggle('{identity}_prepare_backup_torrents', 'Prepare Backup Torrents'," in settings
    for identity in ("alldebrid", "realdebrid"):
        assert (f"{identity}_max_active_torrents: {{scope: 'integration:{identity}', "
                f"option: 'max_active_torrents', blank: null}}") in settings
        assert f"input('{identity}_max_active_torrents', 'Maximum Active Torrents'," in settings
    assert "debridlink_max_active_torrents" not in settings          # no maximum to state, no field
    assert "min: 1, max: 30, placeholder: 'AllDebrid maximum (30)'" in settings
    for jargon in ("standby", "speculative", "reclaim"):
        assert jargon not in settings.casefold()
