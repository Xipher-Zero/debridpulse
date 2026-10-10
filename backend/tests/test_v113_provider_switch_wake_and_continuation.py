"""Transfers 590 and 591: an operator provider switch converges at once, and
the continuation proof a switch's inherited selection crosses is pinned.

* 591: every switch waited for the scheduler's next provider poll (30 s)
  before the target's prepared backup was promoted -- the switch woke only the
  execution cadence, and the engine's own opportunity reaches only a cycle
  already running.
* 590: a replacement reporting the same files under an extra top-level folder
  and no source fingerprint cannot prove an inherited explicit selection
  (``fallback_missing_fingerprint``); the same replacement in the established
  collection-root-relative coordinates proves it by exact path, never through
  the fingerprint fallback, and executes.
"""
from __future__ import annotations

import asyncio

import pytest

from application.service import ApplicationService
from core import scheduler
from test_v113_root_provider_switch import SEASONS, chosen_then_switched, members_of, root_of, route_attempts, rows
from test_v113_root_provider_switch_corrective import big_lab, prepare_backup, running
from application.manual_route_switch import switch_route_provider
from transfers.errors import Category

pytestmark = pytest.mark.asyncio

SOURCE = "a" * 40


# -- 591: the switch wakes resolution; promotion needs no provider poll --------------------------------------------

async def test_an_operator_switch_wakes_idle_resolution_and_promotes_the_prepared_backup_without_a_poll(
        tmp_path, monkeypatch):
    repository, engine, providers, executor, transfer = await big_lab(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    old_writers = running(executor)
    assert old_writers
    standby_id, prepared = await prepare_backup(repository, engine, providers["parcel-b"], transfer.id)
    application = ApplicationService(engine)

    async def publish(_kind, _payload):
        return None

    monkeypatch.setattr("application.service.publish", publish)
    # The scheduler is idle between cycles: no resolution cycle is running.
    assert engine._resolution_cycle is None
    application.resolution_wakeup.clear()
    idle = asyncio.create_task(scheduler._wait_for_work(application.resolution_wakeup, 3600))

    await switch_route_provider(application, transfer.id, "parcel-b", expected_provider_id="parcel-a")

    # The idle scheduler is released by the switch itself, not by its poll interval.
    await asyncio.wait_for(idle, timeout=5)
    assert application.resolution_wakeup.is_set()
    # The old writers are retired before the new route exists.
    assert not (running(executor) & old_writers)

    # One resolution pass -- the clock unmoved, so no deadline or poll interval
    # can have elapsed -- takes over the prepared backup.
    clock_before = engine.clock()
    await application.resolve_pending()
    assert engine.clock() == clock_before
    root = await root_of(repository, transfer.id)
    assert root.resource is not None and root.resource.id == prepared.id
    promoted = [item["id"] for item in await repository.standbys(transfer.id) if item.get("promoted_at")]
    assert promoted == [standby_id]
    assert [call for call in providers["parcel-b"].calls if call[0] == "resolve"] == []    # never created again
    attempts = await route_attempts(root.id)
    assert attempts[0][:2] == ("parcel-a", "released")
    assert attempts[-1][:3] == ("parcel-b", "succeeded", "operator_switch")
    # The replaced resource was given back through the one cleanup owner.
    old = await rows("SELECT state,cleanup_authority FROM provider_resources WHERE transfer_id=? AND provider_id=?",
                     (transfer.id, "parcel-a"))
    assert old and all(row["state"] == "absent" or row["cleanup_authority"] for row in old)


# -- 590: the inherited selection a switch carries, in and out of the established coordinates --------------------

async def test_a_fingerprintless_replacement_in_root_relative_coordinates_carries_the_selection_and_executes(
        tmp_path, monkeypatch):
    """A replacement that reports no fingerprint but the established
    collection-root-relative paths: proven by exact path, never by the
    identity fallback, and its members run on the replacement."""
    repository, engine, transfer, _ = await chosen_then_switched(tmp_path, monkeypatch, target=SEASONS, after="")
    root = await root_of(repository, transfer.id)
    assert root.resource.provider_id == "parcel-b"
    assert root.error is None or root.error.category != Category.RESOURCE_STATE_CONFLICT
    (generation,) = await rows("SELECT * FROM transfer_file_selections WHERE transfer_id=? AND provider_id='parcel-b'",
                               (transfer.id,))
    assert (generation["decision"], generation["decision_reason"], generation["continuity"]) == (
        "explicit", "inherited", "proven")
    assert generation["manifest_committed_at"] is not None
    assert await members_of(repository, transfer.id) == [("S1/A.mkv", "x:S1/A.mkv"), ("S3/C.mkv", "x:S3/C.mkv")]
    artifacts = await repository.artifacts(transfer.id)
    assert {artifact.state for artifact in artifacts} <= {"queued", "downloading", "completed"}
    assert all(candidate.provider_id == "parcel-b" for artifact in artifacts for candidate in artifact.candidates)
    assert any(artifact.execution is not None or artifact.state == "completed" for artifact in artifacts)


async def test_the_wrapped_shape_without_a_fingerprint_is_the_590_refusal(tmp_path, monkeypatch):
    """Premiumize's shape in transfer 590: the same files under the torrent's
    own folder and no fingerprint -- unprovable, refused truthfully, nothing
    carried."""
    wrapped = [(name, f"Show/{path}", size) for name, path, size in SEASONS]
    repository, _engine, transfer, error = await chosen_then_switched(
        tmp_path, monkeypatch, target=wrapped, before=SOURCE, after="", conflict=True)
    assert error is not None and error.diagnostic == "fallback_missing_fingerprint"
    assert await members_of(repository, transfer.id) == [("S1/A.mkv", "x:S1/A.mkv"), ("S3/C.mkv", "x:S3/C.mkv")]
