"""1.0.13 WS6a FUNC-001: a profile catalog write and its retirement never split across the two stores.

``PUT /api/external/profiles`` wrote and applied the new catalog file before
the database commit that retires the profile's dependent jobs. A commit that
failed left the catalog durably changed with every dependent job still
active under a profile that no longer existed; the retried write then saw no
profile to take away and maintenance had nothing to retire. The database
commit is now the write's one commit point: the catalog file follows it, and
the in-memory catalog is withdrawn whenever the write does not complete.

Real application, middleware, external service, repository and engine; the
faults are injected at exactly the route's ``db.commit()`` and its catalog
file write.
"""
from __future__ import annotations

import sqlite3

import pytest

import api.external_routes as routes
import db.database as database
from core.config import apply_settings, get_settings, load_settings
from external.service import ExternalClients
from test_external_client_foundation import body, http, transfer_of, world  # noqa: F401
from test_external_profile_guard import catalog, without
from test_v113_profile_retirement import (OTHER, PROFILE_RETIRED, _fresh_settlement_walk,  # noqa: F401
                                          demand_of, dependents, retire, state_of)
from transfers.models import TransferState

pytestmark = pytest.mark.asyncio


def _profiles(settings) -> set[str]:
    return {profile.id for profile in settings.external_delivery.profiles}


async def _running_tv_job(world):  # noqa: F811
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    for _ in range(2):
        await world.engine.tick()
    return job, transfer_id


async def test_a_failed_commit_changes_neither_store_and_the_same_write_then_retires(http, world, monkeypatch):  # noqa: F811
    job, transfer_id = await _running_tv_job(world)
    unrelated, _ = await world.external.submit(world.clients.r, body("k-r", category="movies",
                                                                     source={"kind": "magnet", "magnet": OTHER}))
    document = await catalog(http)
    real_retire, real_commit = ExternalClients.retire_dependents_in, database._DbConnection.commit
    armed = {"on": False, "fired": 0}

    async def retire_then_arm(db, jobs):
        retired = await real_retire(db, jobs)
        armed["on"] = True                                   # the very next commit is the route's
        return retired

    async def commit(self):
        if armed["on"]:
            armed["on"], armed["fired"] = False, armed["fired"] + 1
            raise sqlite3.OperationalError("disk I/O error (injected at the route's commit)")
        return await real_commit(self)

    monkeypatch.setattr(ExternalClients, "retire_dependents_in", staticmethod(retire_then_arm))
    monkeypatch.setattr(database._DbConnection, "commit", commit)
    failed = await retire(http, document, without(document, "tv"), "tv")
    monkeypatch.setattr(database._DbConnection, "commit", real_commit)
    monkeypatch.setattr(ExternalClients, "retire_dependents_in", real_retire)

    assert armed["fired"] == 1 and failed.status_code >= 500
    # Neither store moved: the file, the live catalog and the job agree.
    assert "tv" in _profiles(load_settings()) and "tv" in _profiles(get_settings())
    assert load_settings().external_delivery.revision == get_settings().external_delivery.revision == document["revision"]
    assert (await demand_of(job["id"]))["withdrawn_at"] is None
    assert await dependents(http, "tv") == 1
    apply_settings(load_settings())                          # a restart reads the same point
    assert "tv" in _profiles(get_settings()) and await dependents(http, "tv") == 1
    # The failed write advanced nothing: a write based on its revision is stale.
    stale = await http.client.put("/api/external/profiles", headers=http.admin_headers,
                                  json={"expected_revision": document["revision"] + 1,
                                        "profiles": without(document, "tv"), "retire_dependents": ["tv"]})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "stale_revision"

    retried = await retire(http, await catalog(http), without(document, "tv"), "tv")
    assert retried.status_code == 200, retried.text
    assert retried.json()["retired_jobs"] == 1 and retried.json()["revision"] == document["revision"] + 1
    assert (await demand_of(job["id"]))["withdrawn_by"] == PROFILE_RETIRED
    assert await state_of(world, transfer_id) == TransferState.DELETED
    assert "tv" not in _profiles(load_settings()) and "tv" not in _profiles(get_settings())
    assert (await demand_of(unrelated["id"]))["withdrawn_at"] is None          # another profile's job: untouched


async def test_a_failed_catalog_write_after_the_commit_leaves_a_coherent_point_that_converges(  # noqa: F811
        http, world, monkeypatch):  # noqa: F811
    job, transfer_id = await _running_tv_job(world)
    document = await catalog(http)
    real_save = routes.save_settings

    def full_disk(settings):
        raise OSError(28, "No space left on device (injected at the route's catalog write)")

    monkeypatch.setattr(routes, "save_settings", full_disk)
    failed = await retire(http, document, without(document, "tv"), "tv")
    monkeypatch.setattr(routes, "save_settings", real_save)

    assert failed.status_code >= 500
    # The catalog is unchanged, in the file and live; the retirement the
    # Admin authorized is durable and never stranded under a missing profile.
    assert "tv" in _profiles(load_settings()) and "tv" in _profiles(get_settings())
    assert (await catalog(http))["revision"] == load_settings().external_delivery.revision == document["revision"]
    assert (await demand_of(job["id"]))["withdrawn_by"] == PROFILE_RETIRED
    assert await dependents(http, "tv") == 0

    apply_settings(load_settings())                          # a restart: maintenance finishes the retirement
    assert (await ExternalClients(world.application).maintain())["profile_retirements"] == 1
    assert await state_of(world, transfer_id) == TransferState.DELETED

    retried = await retire(http, await catalog(http), without(document, "tv"), "tv")
    assert retried.status_code == 200, retried.text
    assert "retired_jobs" not in retried.json()              # nothing left to retire, truthfully
    assert "tv" not in _profiles(load_settings()) and "tv" not in _profiles(get_settings())
    assert load_settings().external_delivery.revision == get_settings().external_delivery.revision \
        == document["revision"] + 1
