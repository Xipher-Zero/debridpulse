"""Speculative-preparation eligibility: the question, never the work.

Whether a provider allows a request to be prepared speculatively -- added to
its account as a backup while another provider delivers it -- is the
provider's own pure answer (``SpeculativePreparation``), read through one
fail-closed reader (``IntegrationRegistry.speculative_preparation_allowed``).
A provider without the contract is never eligible. AllDebrid, Real-Debrid,
Debrid-Link and TorBox each offer it for torrents and magnets only, and only
while the operator turns on that provider's own "Prepare Backup Torrents"
(default off); their active-slot and quota limits constrain capacity rather
than excluding them. Nothing here prepares anything.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_v113_torbox_routes import TOKEN, _application, _settings_owner, _Stored

from integrations.definition import IntegrationSettings
from providers.torbox.definition import TorBoxOptions, canonical_options
from transfers.contracts import SpeculativePreparation
from transfers.models import Capability, IntegrationDescriptor, TransferRequest
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

ROOT = Path(__file__).resolve().parents[2]
MAGNET = TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40, fingerprint="a" * 40)
TORRENT = TransferRequest("torrent", b"metainfo", name="t.torrent", fingerprint="b" * 40)
HOSTER = TransferRequest("https", "https://hoster.example/f/1")
NZB = TransferRequest("nzb", b"<nzb/>", name="p.nzb")
EVERY_KIND = (MAGNET, TORRENT, HOSTER, NZB)
allowed = IntegrationRegistry.speculative_preparation_allowed


def built(identity, **options):
    definition = importlib.import_module(f"providers.{identity}.definition").definition
    return definition.build(IntegrationSettings(enabled=True, options=options), SimpleNamespace())


class Answering:
    def __init__(self, answer):
        self.descriptor = IntegrationDescriptor("answering", "answering", frozenset({Capability.RESOLVE}),
                                                request_types=frozenset({"magnet"}))
        self.answer = answer

    def resolve(self, request):
        raise AssertionError("eligibility never resolves")

    def speculative_preparation_allowed(self, request):
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class Silent:
    descriptor = IntegrationDescriptor("silent", "silent", frozenset({Capability.RESOLVE}),
                                       request_types=frozenset({"magnet"}))

    def resolve(self, request):
        raise AssertionError("eligibility never resolves")


# -- the neutral contract, fail-closed ------------------------------------------------------

async def test_only_the_providers_own_true_allows_it():
    assert allowed(Answering(True), MAGNET) is True
    for answer in (False, None, 1, "yes", object(), RuntimeError("broken")):
        assert allowed(Answering(answer), MAGNET) is False


async def test_a_provider_without_the_contract_is_never_eligible():
    assert not isinstance(Silent(), SpeculativePreparation)
    assert allowed(Silent(), MAGNET) is False


@pytest.mark.parametrize("identity", ["alldebrid", "realdebrid", "debridlink"])
async def test_alldebrid_realdebrid_and_debridlink_are_off_by_default_and_torrent_only_when_on(identity):
    # Each offers backup torrents of its own (TASK3d-2), only by the operator's choice.
    provider = built(identity)
    assert isinstance(provider, SpeculativePreparation)
    assert [allowed(provider, request) for request in EVERY_KIND] == [False] * len(EVERY_KIND)
    enabled = built(identity, prepare_backup_torrents=True)
    assert [allowed(enabled, request) for request in EVERY_KIND] == [True, True, False, False]


# -- T3.1 / T3.4 / T3.5: TorBox, default off, torrents and magnets only ----------------------

async def test_t3_1_prepare_backup_torrents_is_off_by_default():
    assert TorBoxOptions().prepare_backup_torrents is False
    provider = built("torbox", api_token=TOKEN)
    assert [allowed(provider, request) for request in EVERY_KIND] == [False] * len(EVERY_KIND)


async def test_t3_5_on_allows_torrents_and_magnets_and_nothing_else():
    provider = built("torbox", api_token=TOKEN, prepare_backup_torrents=True, usenet_enabled=True)
    assert [allowed(provider, request) for request in EVERY_KIND] == [True, True, False, False]
    # A member TorBox already decomposed is not a torrent root either.
    assert allowed(provider, TransferRequest("https", "torbox://torrent/5/0")) is False


async def test_t3_4_reading_eligibility_performs_no_provider_io():
    provider = built("torbox", api_token=TOKEN, prepare_backup_torrents=True)

    async def refused(*_args, **_kwargs):
        raise AssertionError("eligibility performs no I/O")

    provider.client._send = refused
    assert allowed(provider, MAGNET) is True


# -- T3.2 / T3.6: persisted through the existing TorBox options path, live on rebuild ---------

async def test_t3_2_t3_6_the_setting_persists_immediately_and_each_rebuild_reads_it():
    from api.routes import (
        IntegrationConfigurationUpdate,
        patch_integration_configuration,
    )
    stored = _Stored(enabled=True, api_token=TOKEN)
    for value in (True, False):
        with _settings_owner(stored):
            await patch_integration_configuration("torbox", IntegrationConfigurationUpdate(
                options={"prepare_backup_torrents": value}), _application())
        options = stored.cfg.integrations["torbox"].options
        assert options["prepare_backup_torrents"] is value and options["api_token"] == TOKEN
        # ``application.configure()`` rebuilds the provider from saved options.
        rebuilt = built("torbox", **options)
        assert allowed(rebuilt, MAGNET) is value
        assert rebuilt.descriptor.enabled is True                # turning it on or off changes nothing else


# -- T3.7: every existing TorBox option keeps its meaning, default and storage -----------------

async def test_t3_7_existing_torbox_options_are_unchanged():
    defaults = TorBoxOptions().model_dump()
    assert {key: value for key, value in defaults.items()
            if key not in {"prepare_backup_torrents", "max_active_torrents"}} == {
        "api_token": "", "usenet_enabled": False, "rate_limit_per_minute": 240,
        "request_timeout_seconds": 30, "upload_timeout_seconds": 120, "host_refresh_interval_hours": 24}
    stored = {"api_token": TOKEN, "usenet_enabled": True, "rate_limit_per_minute": 100,
              "request_timeout_seconds": 60, "upload_timeout_seconds": 300, "host_refresh_interval_hours": 12}
    loaded = canonical_options(SimpleNamespace(integrations={"torbox": SimpleNamespace(options=dict(stored))}))
    assert {key: getattr(loaded, key) for key in stored} == stored    # a pre-existing save reads back unchanged
    assert loaded.prepare_backup_torrents is False
    provider = built("torbox", **stored)
    assert "nzb" in provider.descriptor.request_types                 # usenet_enabled still means what it meant
    assert (provider.client.request_timeout.total, provider.client.upload_timeout.total) == (60, 300)


# -- T3.3: neutral core never names the provider or its option ---------------------------------

async def test_t3_3_core_sees_only_the_neutral_fact():
    for path in (ROOT / "backend" / "transfers").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "prepare_backup_torrents" not in text, path.name
    registry = (ROOT / "backend" / "transfers" / "registry.py").read_text(encoding="utf-8")
    assert "torbox" not in registry.casefold()
