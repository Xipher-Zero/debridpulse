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
async def test_alldebrid_realdebrid_and_debridlink_are_on_by_default_torrent_only_and_off_when_chosen(identity):
    # Each offers backup torrents of its own (TASK3d-2). Ledger (1.0.13,
    # route-resource retention): on by default; the operator's off is kept.
    provider = built(identity)
    assert isinstance(provider, SpeculativePreparation)
    assert [allowed(provider, request) for request in EVERY_KIND] == [True, True, False, False]
    chosen_off = built(identity, prepare_backup_torrents=False)
    assert [allowed(chosen_off, request) for request in EVERY_KIND] == [False] * len(EVERY_KIND)


# -- T3.1 / T3.4 / T3.5: TorBox, default off, torrents and magnets only ----------------------

async def test_t3_1_prepare_backup_torrents_is_off_by_default():
    assert TorBoxOptions().prepare_backup_torrents is False
    provider = built("torbox", api_token=TOKEN)
    assert [allowed(provider, request) for request in EVERY_KIND] == [False] * len(EVERY_KIND)


async def test_t3_5_on_allows_torrents_and_magnets_and_nothing_else():
    provider = built("torbox", api_token=TOKEN, prepare_backup_torrents=True, use_before_usenet=True)
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
        "api_token": "", "use_before_usenet": False, "rate_limit_per_minute": 240,
        "request_timeout_seconds": 30, "upload_timeout_seconds": 120, "host_refresh_interval_hours": 24}
    stored = {"api_token": TOKEN, "use_before_usenet": True, "rate_limit_per_minute": 100,
              "request_timeout_seconds": 60, "upload_timeout_seconds": 300, "host_refresh_interval_hours": 12}
    loaded = canonical_options(SimpleNamespace(integrations={"torbox": SimpleNamespace(options=dict(stored))}))
    assert {key: getattr(loaded, key) for key in stored} == stored    # a pre-existing save reads back unchanged
    assert loaded.prepare_backup_torrents is False
    # A stated on is never rewritten either (1.0.13).
    stated = canonical_options(SimpleNamespace(integrations={"torbox": SimpleNamespace(
        options={**stored, "prepare_backup_torrents": True})}))
    assert stated.prepare_backup_torrents is True
    provider = built("torbox", **stored)
    assert provider.descriptor.priority_for("nzb") == 1                # the Usenet preference still means what it meant
    assert (provider.client.request_timeout.total, provider.client.upload_timeout.total) == (60, 300)


# -- T3.3: neutral core never names the provider or its option ---------------------------------

async def test_t3_3_core_sees_only_the_neutral_fact():
    for path in (ROOT / "backend" / "transfers").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "prepare_backup_torrents" not in text, path.name
    registry = (ROOT / "backend" / "transfers" / "registry.py").read_text(encoding="utf-8")
    assert "torbox" not in registry.casefold()


# -- 1.0.13 mixed defaults: on for AllDebrid, Debrid-Link, Premiumize and Real-Debrid, off for TorBox ----------

DEFAULTS = {"alldebrid": True, "debridlink": True, "premiumize": True, "realdebrid": True, "torbox": False}


@pytest.mark.parametrize("identity", sorted(DEFAULTS))
async def test_each_provider_has_its_own_default_and_a_stated_choice_wins(identity):
    """Prepare Backup Torrents defaults on, except for TorBox, whose create
    limits and active slots are restrictive. The default lives in each
    provider's own options model; core sees only the neutral answer."""
    options_model = importlib.import_module(f"providers.{identity}.definition").definition.options_model
    assert options_model().prepare_backup_torrents is DEFAULTS[identity]
    extra = {"api_token": TOKEN} if identity == "torbox" else {}
    default = [True, True, False, False] if DEFAULTS[identity] else [False] * len(EVERY_KIND)
    assert [allowed(built(identity, **extra), request) for request in EVERY_KIND] == default
    for stated, expected in ((True, [True, True, False, False]), (False, [False] * len(EVERY_KIND))):
        provider = built(identity, prepare_backup_torrents=stated, **extra)
        assert [allowed(provider, request) for request in EVERY_KIND] == expected


async def test_a_stated_choice_survives_load_and_save_and_an_unstated_option_takes_its_default(
        tmp_path, monkeypatch):
    """The persisted document keeps every stated value -- an operator's saved
    off on a provider that defaults on, and a saved on for TorBox -- through
    the real load, normalization and save. A provider namespace that never
    stated the option (saved before it existed) takes that provider's
    default, and is saved with it."""
    import json

    import core.config as config

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"integrations": {
        "alldebrid": {"enabled": True, "options": {"api_key": "k", "prepare_backup_torrents": False}},
        "realdebrid": {"enabled": True, "options": {"api_token": "t"}},
        "premiumize": {"enabled": True, "options": {"api_key": "p"}},
        "torbox": {"enabled": True, "options": {"api_token": TOKEN}},
        "debridlink": {"enabled": True, "options": {"api_key": "d", "prepare_backup_torrents": True}},
    }}))
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    expected = {"alldebrid": False, "realdebrid": True, "premiumize": True, "torbox": False, "debridlink": True}
    loaded = config.load_settings()
    assert {identity: loaded.integrations[identity].options["prepare_backup_torrents"]
            for identity in expected} == expected
    config.save_settings(loaded)
    stored = json.loads(path.read_text())["integrations"]
    assert {identity: stored[identity]["options"]["prepare_backup_torrents"] for identity in expected} == expected
    again = config.load_settings()
    assert {identity: again.integrations[identity].options["prepare_backup_torrents"]
            for identity in expected} == expected
