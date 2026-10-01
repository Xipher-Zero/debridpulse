"""The bounded ``GET /api/torrents`` read model carries the canonical challenge.

The INPUT_REQUIRED modal (``frontend/static/ui-auth-required.js``) discovers
work through ``GET /api/torrents?status=input_required&limit=5000`` and acts
only on an item whose ``input_required`` is the public challenge. Production
transfer 399 reached a real, durable executor challenge that the bounded list
presented as input-required but did not carry, so the modal never opened.

Everything here is real: the engine raises the challenge through the ordinary
lifecycle, and both the bounded list and the single-transfer API are called
against the same durable row. The bounded list must stay bounded -- it never
reaches the comprehensive presentation path.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import api.operational_downloads as downloads
import api.routes as routes
from test_v113_transfer_auth_context import CountingVault, _current, lab  # noqa: F401
from transfers.models import TransferRequest, TransferState

pytestmark = pytest.mark.asyncio


class _NoComprehensivePresentation:
    async def presentation(self, *_args, **_kwargs):
        raise AssertionError("the bounded list must never call comprehensive presentation")


async def _challenged(lab):
    repository, registry, engine, *_rest, now = lab
    registry.register_executor(CountingVault(repository.authorize_execution,
                                             objects={"locked.example/solo.bin": b"four"},
                                             locks={"locked.example": ("user", "pass")}))
    transfer = await engine.submit((TransferRequest("vault", "vault://locked.example/solo.bin"),),
                                   deduplicate=False)
    for _ in range(10):
        now[0] += 5
        await engine.tick()
        if await _current(engine, transfer.id) is not None:
            break
    challenge = await _current(engine, transfer.id)
    assert challenge is not None and challenge.origin.value == "executor"
    assert (await repository.get(transfer.id)).state == TransferState.INPUT_REQUIRED
    return repository, engine, transfer, challenge


async def test_the_modal_query_returns_the_canonical_public_challenge(lab):
    repository, engine, transfer, challenge = await _challenged(lab)
    listed = await downloads.list_operational_torrents(
        status="input_required", search=None, limit=5000, offset=0,
        application=SimpleNamespace(repository=_NoComprehensivePresentation(), definitions=[], engine=engine))
    item = next(entry for entry in listed["items"] if entry["id"] == transfer.id)
    detail = await routes.get_torrent(transfer.id, application=SimpleNamespace(
        repository=repository, definitions=[], engine=engine))

    assert item["input_required"] is not None, "the modal cannot act on a list item without its challenge"
    assert item["input_required"] == detail["input_required"]
    assert item["input_required"]["id"] == challenge.id
    assert item["input_required"]["reason"] == "auth_required"
    assert item["input_required"]["origin"] == "executor"
    assert item["input_required"]["methods"] == [{"method": "username_password", "fields": [
        {"name": "username", "required": True}, {"name": "password", "required": True}]}]


async def test_a_transfer_without_a_current_challenge_carries_none(lab):
    repository, registry, engine, *_rest, now = lab
    registry.register_executor(CountingVault(repository.authorize_execution, objects={"open.example/f.bin": b"four"}))
    transfer = await engine.submit((TransferRequest("vault", "vault://open.example/f.bin"),), deduplicate=False)
    listed = await downloads.list_operational_torrents(
        status=None, search=None, limit=0, offset=0,
        application=SimpleNamespace(repository=_NoComprehensivePresentation(), definitions=[], engine=engine))
    item = next(entry for entry in listed["items"] if entry["id"] == transfer.id)
    assert item["input_required"] is None


async def test_the_listed_challenge_carries_descriptors_and_facts_only(lab):
    _repository, engine, transfer, _challenge = await _challenged(lab)
    await engine.submit_input(transfer.id, _challenge.id, "username_password",
                              {"username": "listed-user-sentinel", "password": "listed-secret-sentinel"})
    listed = await downloads.list_operational_torrents(
        status=None, search=None, limit=0, offset=0,
        application=SimpleNamespace(repository=_NoComprehensivePresentation(), definitions=[], engine=engine))
    encoded = json.dumps(listed, default=str)
    assert "listed-secret-sentinel" not in encoded and "listed-user-sentinel" not in encoded
    item = next(entry for entry in listed["items"] if entry["id"] == transfer.id)
    assert set(item["input_required"]) == {"id", "generation", "subject", "reason", "origin", "methods", "facts",
                                           "authority"}
    assert item["input_required"]["authority"] == ""  # the source's own authority asked
