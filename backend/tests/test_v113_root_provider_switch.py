"""TASK3d-3: torrent root provider status and the operator's root route switch.

A torrent root's provider is its committed ROOT route. An operator switch
replaces that route (never child URLs): old writers are fenced through the
existing pause and writer retirement, the replacement commits atomically, the
replaced resource gets the ordinary owned cleanup, and the ordinary machinery
-- prepared-backup promotion or a cold resolve, a new decomposition generation,
members rebuilt in place -- produces the new state. Neutral manifest providers
drive every case; no provider identity decides anything.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fake_integrations import MemoryExecutor, ParcelProvider
from test_v113_collection_route_generic_closure import Clock

from db import database
from db.database import get_db
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, TransferError
from transfers.manual_route_switch import route_providers, switch_root_provider
from transfers.models import (
    ActiveCapacity,
    ExecutionState,
    ProviderResource,
    ResourceState,
    TransferRequest,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

FILES = [("one.bin", "Show/one.bin", 4), ("two.bin", "Show/two.bin", 4)]
MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


def magnet_provider(identity):
    """A neutral manifest provider that takes magnet roots."""
    provider = ParcelProvider(identity, file_manifest=True)
    provider.descriptor = replace(provider.descriptor, request_types=frozenset({"magnet", "parcel-member"}))
    return provider


def offer(provider, files=FILES, *, native="x"):
    """Queue the provider's resource for the root (an AVAILABLE torrent)."""
    result = provider.parcel(native, state=ResourceState.AVAILABLE, files=files)
    observed = replace(result.observation, request=TransferRequest("magnet", MAGNET))
    provider.resources[observed.resource.id] = observed
    provider.responses.append(replace(result, observation=observed))
    return observed.resource


async def lab(tmp_path, monkeypatch, *identities, selection_mode="all"):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "switch.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    providers = {identity: magnet_provider(identity) for identity in identities}
    for provider in providers.values():
        registry.register_provider(provider)
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    clock = Clock()
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3), clock=clock)
    await engine.initialize()
    offer(providers[identities[0]])
    transfer = await engine.submit((TransferRequest("magnet", MAGNET, name="Show", selection_mode=selection_mode),),
                                   name="Show", deduplicate=False)
    for _ in range(6):
        await engine.tick()
    return repository, engine, providers, executor, transfer


async def settle(engine, ticks=8):
    for _ in range(ticks):
        engine.clock.now += 30
        await engine.tick()


async def root_of(repository, transfer_id):
    return next(item for item in await repository.requests(transfer_id) if item.parent_id is None)


async def rows(sql, params=()):
    async with get_db() as db:
        return await db.fetchall(sql, params)


def running(executor):
    return {attempt for attempt, job in executor.jobs.items() if job.state == ExecutionState.RUNNING}


async def route_attempts(request_id):
    return [(r["provider_id"], r["state"], r["operation"], r["outcome"]) for r in await rows(
        """SELECT a.provider_id,a.state,p.operation,p.outcome FROM route_attempt_provenance p
           JOIN resolution_attempts a ON a.id=p.resolution_attempt_id WHERE a.request_id=? ORDER BY p.ordinal""",
        (request_id,))]


async def surfaces(repository, engine, transfer_id, *, recent=False):
    """The provider every surface shows: Details and the bounded list, read as
    Downloads reads it or (``recent``) as Dashboard Recent Activity does."""
    from types import SimpleNamespace

    from api.operational_downloads import list_operational_torrents
    detail = await repository.presentation(transfer_id, details=True)
    application = SimpleNamespace(engine=engine, repository=repository, definitions={})
    listed = await list_operational_torrents(status=None, search=None, limit=6 if recent else 0, offset=0,
                                             order="activity" if recent else None, application=application)
    row = next(item for item in (listed["items"] if isinstance(listed, dict) else listed) if item["id"] == transfer_id)
    return detail.get("route_provider_id"), row.get("route_provider_id"), row.get("route_switch_available")


# -- 29.1 / 29.2 / 29.16: the provider shown is the committed root route ------------------------------------

async def test_every_surface_shows_the_committed_root_route_never_a_child_provider(tmp_path, monkeypatch):
    repository, engine, providers, _executor, transfer = await lab(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    assert await surfaces(repository, engine, transfer.id) == ("parcel-a", "parcel-a", True)

    offer(providers["parcel-b"])
    await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    # Before the new generation fans out, every child candidate still names
    # parcel-a: the surfaces follow the committed root route regardless.
    details, listed, _hint = await surfaces(repository, engine, transfer.id)
    assert (details, listed) == ("parcel-b", "parcel-b")


# -- 29.3: a prepared provider is taken over through the one promotion seam ---------------------------------

async def test_a_prepared_target_is_reused_exactly_with_no_second_create(tmp_path, monkeypatch):
    repository, engine, providers, _executor, transfer = await lab(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    root = await root_of(repository, transfer.id)
    prepared = offer(providers["parcel-b"], native="prepared")
    providers["parcel-b"].responses.clear()                          # no resolve answer: reuse or nothing
    standby_id, _attempts = await repository.begin_standby(transfer.id, root.id, "parcel-b", engine.clock())
    await repository.bind_standby(standby_id, transfer.id, prepared, ResourceState.AVAILABLE, engine.clock())
    assert {item["provider_id"]: item["status"] for item in
            (await route_providers(engine, transfer.id))["providers"]} == {"parcel-a": "current",
                                                                           "parcel-b": "prepared"}

    await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    await settle(engine)

    root = await root_of(repository, transfer.id)
    assert root.resource.id == prepared.id                            # the exact prepared resource
    assert [call for call in providers["parcel-b"].calls if call[0] == "resolve" and call[1] == MAGNET] == []
    assert (await route_attempts(root.id))[-1][:3] == ("parcel-b", "succeeded", "operator_switch")
    artifacts = await repository.artifacts(transfer.id)
    assert {candidate.provider_id for artifact in artifacts for candidate in artifact.candidates} == {"parcel-b"}


# -- 29.4 / 29.12: a cold legitimate provider switches with no backup required -------------------------------

async def test_a_cold_target_resolves_once_through_ordinary_resolution(tmp_path, monkeypatch):
    repository, engine, providers, _executor, transfer = await lab(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    status = await route_providers(engine, transfer.id)
    assert {item["provider_id"]: (item["status"], item["selectable"]) for item in status["providers"]} == {
        "parcel-a": ("current", False), "parcel-b": ("available", True)}

    offer(providers["parcel-b"])
    await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    await settle(engine)

    root = await root_of(repository, transfer.id)
    assert root.resource.provider_id == "parcel-b"
    assert len([call for call in providers["parcel-b"].calls if call == ("resolve", MAGNET)]) == 1
    attempts = await route_attempts(root.id)
    assert attempts[0][:2] == ("parcel-a", "released") and attempts[0][3] == "superseded"   # never "failed"
    assert attempts[-1][:3] == ("parcel-b", "succeeded", "operator_switch") and len(attempts) == 2   # pin adopted


# -- 29.5 / 29.6 / 29.18: active writers are fenced, material kept, one writer, old resource owned ---------------

async def test_an_active_download_switch_fences_the_old_writers_and_keeps_the_material(tmp_path, monkeypatch):
    repository, engine, providers, executor, transfer = await lab(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    old_writers = running(executor)
    assert len(old_writers) == 2
    one, two = await repository.artifacts(transfer.id)
    executor.finish(one.execution)                                   # one member delivered
    await engine.tick()
    Path(two.target).parent.mkdir(parents=True, exist_ok=True)
    Path(two.target).write_bytes(b"pa")                              # the other partially written
    old_resource = (await root_of(repository, transfer.id)).resource

    offer(providers["parcel-b"])
    await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    assert not (running(executor) & old_writers), "an old writer survived the route commit"
    await settle(engine)

    assert not (running(executor) & old_writers)
    assert Path(one.target).read_bytes() == b"done" and Path(two.target).exists()
    artifacts = {artifact.id: artifact for artifact in await repository.artifacts(transfer.id)}
    assert set(artifacts) == {one.id, two.id}                        # the same logical targets
    assert artifacts[one.id].state == "completed" and artifacts[one.id].target == one.target
    assert artifacts[two.id].target == two.target
    live = [artifact for artifact in artifacts.values() if artifact.execution and artifact.state != "completed"]
    assert len(live) == 1 and running(executor) == {live[0].execution.attempt_id}      # exactly one writer
    assert {candidate.provider_id for candidate in live[0].candidates} == {"parcel-b"}
    # The replaced resource was owned and cleaned through the ordinary cadence.
    cleaned = next(call for call in providers["parcel-a"].calls if call[0] == "cleanup")
    assert cleaned[1].resource.id == old_resource.id
    states = {resource.id: state for resource, state, _pending in await repository.resources(transfer.id)}
    assert states[old_resource.id] == ResourceState.ABSENT


# -- 29.7: an explicit choice retries a provider automatic routing exhausted, for that root only ------------------

async def test_an_exhausted_provider_is_shown_as_failed_earlier_and_retried_only_for_this_root(tmp_path, monkeypatch):
    repository, engine, providers, _executor, transfer = await lab(tmp_path, monkeypatch,
                                                                  "parcel-a", "parcel-b", "parcel-c")
    root = await root_of(repository, transfer.id)
    async with get_db() as db:                                       # automatic routing exhausted b and c earlier
        for provider_id in ("parcel-b", "parcel-c"):
            await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) "
                             "VALUES(?,?,?,'exhausted')", (f"old-{provider_id}", root.id, provider_id))
        await db.commit()
    statuses = {item["provider_id"]: (item["status"], item["selectable"])
                for item in (await route_providers(engine, transfer.id))["providers"]}
    assert statuses["parcel-b"] == ("failed_earlier", True) and statuses["parcel-c"] == ("failed_earlier", True)

    offer(providers["parcel-b"])
    await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    await settle(engine)

    assert (await root_of(repository, transfer.id)).resource.provider_id == "parcel-b"
    exhausted = await rows("SELECT provider_id,state FROM resolution_attempts WHERE id LIKE 'old-%' ORDER BY id")
    assert [tuple(row.values()) for row in exhausted] == [("parcel-b", "released"), ("parcel-c", "exhausted")]


# -- 29.8: the target is primary work under TASK3d-1 admission ----------------------------------------------------

async def test_a_full_target_is_refused_with_a_capacity_reason_and_nothing_changes(tmp_path, monkeypatch):
    repository, engine, providers, executor, transfer = await lab(tmp_path, monkeypatch, "parcel-a", "parcel-b")

    async def full(request):
        return ActiveCapacity(1, 1)

    providers["parcel-b"].active_capacity = full
    before_root = await root_of(repository, transfer.id)
    before_writers = running(executor)
    with pytest.raises(TransferError) as refused:
        await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    assert refused.value.error.category == Category.CONCURRENCY_LIMITED
    assert (await root_of(repository, transfer.id)).resource == before_root.resource
    assert running(executor) == before_writers                       # nothing was fenced
    assert not (await repository.get(transfer.id)).paused
    assert (await surfaces(repository, engine, transfer.id))[:2] == ("parcel-a", "parcel-a")


# -- 29.9 / 29.13 / 29.19: stale, unavailable and racing actions change nothing ----------------------------------

async def test_a_stale_or_unavailable_switch_changes_nothing(tmp_path, monkeypatch):
    repository, engine, providers, executor, transfer = await lab(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    root = await root_of(repository, transfer.id)
    writers = running(executor)
    with pytest.raises(TransferError) as stale:
        await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-x")
    assert stale.value.error.category == Category.RESOURCE_STATE_CONFLICT

    providers["parcel-b"].descriptor = replace(providers["parcel-b"].descriptor, enabled=False)
    status = await route_providers(engine, transfer.id)
    assert {item["provider_id"]: (item["status"], item["selectable"], item["reason"])
            for item in status["providers"]}["parcel-b"] == ("unavailable", False, "disabled")
    with pytest.raises(TransferError) as disabled:
        await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    assert disabled.value.error.category == Category.PROVIDER_UNAVAILABLE
    assert (await root_of(repository, transfer.id)).resource == root.resource and running(executor) == writers

    # A newer committed route wins: a replacement still naming the old route
    # attempt changes nothing.
    latest = await repository.latest_root_route(root.id)
    providers["parcel-b"].descriptor = replace(providers["parcel-b"].descriptor, enabled=True)
    outcome = await repository.replace_root_route(root.id, expected_attempt_id="not-" + latest["id"],
                                                  expected_provider_id="parcel-a", target_provider_id="parcel-b")
    assert outcome == "stale" and (await repository.latest_root_route(root.id))["id"] == latest["id"]


# -- 29.11: a second movement follows the route that committed ------------------------------------------------

async def test_a_second_switch_follows_the_route_that_committed(tmp_path, monkeypatch):
    repository, engine, providers, _executor, transfer = await lab(tmp_path, monkeypatch,
                                                                  "parcel-a", "parcel-b", "parcel-c")
    offer(providers["parcel-b"])
    await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    await settle(engine)
    offer(providers["parcel-c"])
    await switch_root_provider(engine, transfer.id, "parcel-c", expected_provider_id="parcel-b")
    await settle(engine)
    root = await root_of(repository, transfer.id)
    assert root.resource.provider_id == "parcel-c"
    assert (await surfaces(repository, engine, transfer.id))[:2] == ("parcel-c", "parcel-c")
    assert [attempt[:3] for attempt in await route_attempts(root.id)] == [
        ("parcel-a", "released", "resolve"), ("parcel-b", "released", "operator_switch"),
        ("parcel-c", "succeeded", "operator_switch")]


# -- 29.15: one legitimate provider is informational ------------------------------------------------------------

async def test_a_root_with_one_legitimate_provider_offers_no_switch(tmp_path, monkeypatch):
    repository, engine, _providers, _executor, transfer = await lab(tmp_path, monkeypatch, "parcel-a")
    status = await route_providers(engine, transfer.id)
    assert status["switchable"] is False and [item["status"] for item in status["providers"]] == ["current"]
    assert (await surfaces(repository, engine, transfer.id))[2] is False


# -- 29.17: an explicit selection is carried and proven (TASK3c/D2), never broadened -----------------------------

async def test_an_explicit_selection_crosses_a_switch_through_the_existing_proof(tmp_path, monkeypatch):
    three = [*FILES, ("three.bin", "Show/three.bin", 4)]
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "switch.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    providers = {identity: magnet_provider(identity) for identity in ("parcel-a", "parcel-b")}
    for provider in providers.values():
        registry.register_provider(provider)
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3), clock=Clock())
    await engine.initialize()
    offer(providers["parcel-a"], three)
    transfer = await engine.submit((TransferRequest("magnet", MAGNET, name="Show", selection_mode="interactive"),),
                                   name="Show", deduplicate=False)
    for _ in range(3):
        await engine.tick()
    view = await repository.file_selection_presentation(transfer.id, now=engine.clock())
    chosen = [entry["entry_id"] for entry in view["entries"] if entry["relative_path"] != "Show/three.bin"]
    await repository.confirm_file_selection(transfer.id, view["manifest_id"], chosen, now=engine.clock())
    await settle(engine, 4)

    offer(providers["parcel-b"], three)
    await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    await settle(engine)

    members = sorted(item.entry.relative_path for item in await repository.requests(transfer.id) if item.parent_id)
    assert members == ["Show/one.bin", "Show/two.bin"]                 # never broadened to three
    generation = (await rows("SELECT decision,decision_reason,continuity FROM transfer_file_selections "
                             "WHERE transfer_id=? ORDER BY created_at DESC, id DESC LIMIT 1", (transfer.id,)))[0]
    assert tuple(generation.values()) == ("explicit", "inherited", "proven")


# -- D1 on a switch: a target whose decomposition is not the established one holds --------------------------------

async def test_a_target_presenting_a_different_decomposition_holds_and_changes_nothing(tmp_path, monkeypatch):
    _repository, engine, providers, executor, transfer = await lab(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    before = await rows("SELECT id,request_id,local_path FROM download_files WHERE torrent_id=? ORDER BY id",
                        (transfer.id,))
    offer(providers["parcel-b"], [("one.bin", "Other/one.bin", 4), ("two.bin", "Other/two.bin", 4)])
    await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    await settle(engine)

    held = (await rows("SELECT continuity,continuity_reason FROM transfer_file_selections WHERE transfer_id=? "
                       "ORDER BY created_at DESC, id DESC LIMIT 1", (transfer.id,)))[0]
    assert tuple(held.values()) == ("held", "established_member_missing")
    assert await rows("SELECT id,request_id,local_path FROM download_files WHERE torrent_id=? ORDER BY id",
                      (transfer.id,)) == before
    assert not running(executor)


# -- members follow the route that decomposed them ------------------------------------------------------------

async def test_members_follow_the_roots_current_provider(tmp_path, monkeypatch):
    repository, engine, providers, _executor, transfer = await lab(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    offer(providers["parcel-b"])
    await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    await settle(engine)
    for member in [item for item in await repository.requests(transfer.id) if item.parent_id]:
        assert (await route_attempts(member.id))[-1][0] == "parcel-b"


async def test_the_read_model_never_reports_a_prepared_backup_from_cache_readiness(tmp_path, monkeypatch):
    """29.14: TASK2 readiness is a secondary fact; only a TASK3 backup is Prepared."""
    repository, engine, _providers, _executor, transfer = await lab(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    root = await root_of(repository, transfer.id)
    async with get_db() as db:
        decision = json.dumps({"v": 1, "outcome": "selected", "providers": [
            {"provider_id": "parcel-a", "disposition": "selected"},
            {"provider_id": "parcel-b", "disposition": "applicable_not_selected", "availability": "ready"}]})
        await db.execute("UPDATE transfer_requests SET routing_decision=? WHERE id=?", (decision, root.id))
        await db.commit()
    statuses = {item["provider_id"]: item["status"] for item in (await route_providers(engine, transfer.id))["providers"]}
    assert statuses["parcel-b"] == "available"
    _ = ProviderResource


# -- 29.21: every surface uses the one action -----------------------------------------------------------------

def test_every_surface_reaches_the_one_route_action_through_its_one_owner():
    static = Path(__file__).resolve().parents[2] / "frontend" / "static"
    owners = [path.name for path in static.glob("*.js") if "/route'" in path.read_text() or '/route"' in path.read_text()]
    assert owners == ["ui-root-provider.js"]                          # the only caller of the route endpoint
    app = (static / "app.js").read_text()
    assert "window.DPRootProvider.badgeMarkup(t, surface)" in app       # Recent and Downloads chips delegate
    assert "data-dp-root-provider-mount" in app                         # the Details Files-header slot
    assert "${providerChip(t,'dashboard_recent')}" in (static / "ui-dashboard-transfer-presentation.js").read_text()
    assert "${providerChip(t, 'downloads')}" in (static / "ui-downloads.js").read_text()
    index = (static / "index.html").read_text()
    assert index.count('/ui-root-provider.js?v=3') == 1
    assert "@import url('/ui-root-provider.css?v=2');" in (static / "style.css").read_text()


# -- the switch's temporary fence never erases a newer operator Pause / Pause All ------------------------------

def application_of(engine):
    from application.service import ApplicationService
    return ApplicationService(engine)


async def switch_blocked_at_commit(monkeypatch, engine, *, outcome=None):
    """Hold a switch after its fence, right before its route commit; ``outcome``
    replaces the commit's answer (a refusal after fencing)."""
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()
    commit = engine.repository.replace_root_route

    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        return outcome if outcome is not None else await commit(*args, **kwargs)

    monkeypatch.setattr(engine.repository, "replace_root_route", held)
    return entered, release


async def race(application, transfer_id, control, entered, release):
    """Run the switch, start ``control`` while the switch is fenced, and let the
    switch finish only once the operator control has either COMPLETED (no
    serialization: it landed inside the fence) or BLOCKED on the operator
    pause-control boundary -- decided by events, never by elapsed time."""
    import asyncio

    from application.manual_route_switch import switch_route_provider
    contended = asyncio.Event()

    class Probe(asyncio.Lock):
        async def acquire(self):
            if self.locked():
                contended.set()
            return await super().acquire()

    application.operator_controls = Probe()
    switching = asyncio.create_task(switch_route_provider(application, transfer_id, "parcel-b",
                                                          expected_provider_id="parcel-a"))
    await asyncio.wait_for(entered.wait(), 5)
    operator = asyncio.create_task(control())
    waiting = asyncio.create_task(contended.wait())
    await asyncio.wait({operator, waiting}, return_when=asyncio.FIRST_COMPLETED)
    waiting.cancel()
    release.set()
    switched, controlled = await asyncio.gather(switching, operator, return_exceptions=True)
    return switched, controlled, contended.is_set()


async def test_an_operator_pause_landing_during_a_switch_stays_authoritative(tmp_path, monkeypatch):
    repository, engine, providers, executor, transfer = await lab(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    application = application_of(engine)
    offer(providers["parcel-b"])
    entered, release = await switch_blocked_at_commit(monkeypatch, engine)
    switched, paused, serialized = await race(application, transfer.id, lambda: application.pause(transfer.id), entered, release)
    assert not isinstance(switched, Exception) and not isinstance(paused, Exception)
    assert (await repository.get(transfer.id)).paused, "the switch's cleanup cleared the operator's newer Pause"
    assert serialized, "the operator Pause ran inside the switch's fence"
    await settle(engine)
    assert not running(executor), "a paused transfer was readmitted"
    assert await repository.bound_route_provider((await root_of(repository, transfer.id)).id) == "parcel-b"


async def test_a_pause_all_landing_during_a_switch_stays_authoritative(tmp_path, monkeypatch):
    repository, engine, providers, executor, transfer = await lab(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    application = application_of(engine)
    offer(providers["parcel-b"])
    entered, release = await switch_blocked_at_commit(monkeypatch, engine)
    switched, paused, serialized = await race(application, transfer.id, application.pause_all, entered, release)
    assert not isinstance(switched, Exception) and not isinstance(paused, Exception)
    assert await repository.globally_paused(), "the switch's cleanup cleared Pause All"
    assert serialized, "Pause All ran inside the switch's fence"
    await settle(engine)
    assert not running(executor)


async def test_a_switch_never_clears_a_pause_that_was_already_there(tmp_path, monkeypatch):
    repository, engine, providers, _executor, transfer = await lab(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    application = application_of(engine)
    await application.pause(transfer.id)
    offer(providers["parcel-b"])
    from application.manual_route_switch import switch_route_provider
    await switch_route_provider(application, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    assert (await repository.get(transfer.id)).paused
    await application.pause_all()
    await application.resume(transfer.id)                              # (resume converts Pause All per transfer)
    await application.pause_all()
    offer(providers["parcel-a"])
    await switch_route_provider(application, transfer.id, "parcel-a", expected_provider_id="parcel-b")
    assert await repository.globally_paused(), "a switch under Pause All cleared it"


async def test_a_refusal_after_fencing_keeps_the_old_route_and_a_newer_pause(tmp_path, monkeypatch):
    repository, engine, providers, _executor, transfer = await lab(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    application = application_of(engine)
    before = (await root_of(repository, transfer.id)).resource
    files = await rows("SELECT id,local_path FROM download_files WHERE torrent_id=? ORDER BY id", (transfer.id,))
    offer(providers["parcel-b"])
    entered, release = await switch_blocked_at_commit(monkeypatch, engine, outcome="stale")
    switched, paused, serialized = await race(application, transfer.id, lambda: application.pause(transfer.id), entered, release)
    assert isinstance(switched, TransferError) and switched.error.category == Category.RESOURCE_STATE_CONFLICT
    assert not isinstance(paused, Exception)
    assert (await root_of(repository, transfer.id)).resource == before          # the old route stays current
    assert await repository.bound_route_provider((await root_of(repository, transfer.id)).id) == "parcel-a"
    assert await rows("SELECT id,local_path FROM download_files WHERE torrent_id=? ORDER BY id",
                      (transfer.id,)) == files
    assert (await repository.get(transfer.id)).paused, "the refused switch's cleanup cleared the operator's Pause"
    assert serialized


# -- no fake choice: the launcher hint is the picker's own selectability ---------------------------------------

async def test_enabled_but_illegitimate_providers_never_create_a_switch_affordance(tmp_path, monkeypatch):
    repository, engine, providers, _executor, transfer = await lab(tmp_path, monkeypatch,
                                                                  "parcel-a", "parcel-b", "parcel-c")
    providers["parcel-b"].entitlement_for = lambda request: False               # enabled, not entitled
    providers["parcel-c"].descriptor = replace(providers["parcel-c"].descriptor,
                                               request_types=frozenset({"parcel-member"}))   # enabled, not applicable
    status = await route_providers(engine, transfer.id)
    assert status["switchable"] is False
    assert {item["provider_id"]: (item["status"], item["reason"]) for item in status["providers"]} == {
        "parcel-a": ("current", None), "parcel-b": ("unavailable", "not_entitled")}
    assert (await surfaces(repository, engine, transfer.id))[2] is False
    assert await surfaces(repository, engine, transfer.id, recent=True) == ("parcel-a", "parcel-a", False)
    with pytest.raises(TransferError) as refused:
        await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    assert refused.value.error.category == Category.ACCOUNT_LIMITED

    del providers["parcel-b"].entitlement_for                                  # now legitimately selectable
    assert (await route_providers(engine, transfer.id))["switchable"] is True
    assert (await surfaces(repository, engine, transfer.id))[2] is True


# -- one committed-root-route authority on every surface ------------------------------------------------------

async def test_every_surface_and_the_canonical_owner_agree_across_route_movements(tmp_path, monkeypatch):
    from test_v113_standby_promotion import PROVIDER_FINAL

    from transfers.models import ProviderObservation
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "switch.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    providers = {identity: magnet_provider(identity) for identity in ("parcel-a", "parcel-b", "parcel-c")}
    for provider in providers.values():
        registry.register_provider(provider)
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3), clock=Clock())
    await engine.initialize()
    preparing = providers["parcel-a"].parcel("x", state=ResourceState.PREPARING, files=FILES)
    observed = replace(preparing.observation, request=TransferRequest("magnet", MAGNET))
    providers["parcel-a"].resources[observed.resource.id] = observed
    providers["parcel-a"].responses.append(replace(preparing, observation=observed))
    transfer = await engine.submit((TransferRequest("magnet", MAGNET, name="Show"),), name="Show", deduplicate=False)
    await settle(engine, 3)

    async def agree(expected):
        root = await root_of(repository, transfer.id)
        canonical = await repository.bound_route_provider(root.id)
        details, downloads, _hint = await surfaces(repository, engine, transfer.id)
        _details, recent, _hint = await surfaces(repository, engine, transfer.id, recent=True)
        status = (await route_providers(engine, transfer.id))["current_provider_id"]
        assert (canonical, details, downloads, recent, status) == (expected,) * 5

    await agree("parcel-a")                                                    # 1. initial route
    providers["parcel-a"].resources[observed.resource.id] = ProviderObservation(
        observed.resource, ResourceState.UNAVAILABLE, "Show", error=replace(PROVIDER_FINAL, integration_id="parcel-a"))
    offer(providers["parcel-b"])
    await settle(engine)
    await agree("parcel-b")                                                    # 2. automatic movement

    offer(providers["parcel-c"])
    await switch_root_provider(engine, transfer.id, "parcel-c", expected_provider_id="parcel-b")
    # Child-provider trap: the members' candidates still name parcel-b.
    assert {c.provider_id for a in await repository.artifacts(transfer.id) for c in a.candidates} == {"parcel-b"}
    await agree("parcel-c")                                                    # 3. manual switch
    await settle(engine)
    await agree("parcel-c")

    offer(providers["parcel-a"], native="again")
    await switch_root_provider(engine, transfer.id, "parcel-a", expected_provider_id="parcel-c")
    await settle(engine)
    await agree("parcel-a")                                                    # 4. second movement (retried a)


async def test_the_bounded_list_reads_route_facts_without_per_transfer_connections(tmp_path, monkeypatch):
    repository, engine, providers, _executor, transfer = await lab(tmp_path, monkeypatch, "parcel-a", "parcel-b")

    async def acquires_for_list():
        before = database.db_runtime_metrics()["sqlite_acquires"]
        await surfaces(repository, engine, transfer.id)
        return database.db_runtime_metrics()["sqlite_acquires"] - before

    one = await acquires_for_list()
    for index in range(4):
        offer(providers["parcel-a"], native=f"more-{index}")
        await engine.submit((TransferRequest("magnet", MAGNET.replace("a" * 40, str(index) * 40), name=f"S{index}"),),
                            name=f"S{index}", deduplicate=False)
    await settle(engine, 4)
    assert await acquires_for_list() == one


def test_every_operator_pause_control_and_the_switch_enter_the_one_boundary():
    """Every non-test call of an engine pause control (and of the switch, which
    sets and lifts its own fence) sits inside ``async with ...operator_controls``,
    and nothing that runs inside that boundary re-enters it. The switch itself
    calls no operator pause control: its fence is the claim-scoped pause intent,
    lifted without a Resume."""
    import ast
    controls = {"pause", "resume", "pause_all", "resume_all", "record_pause_intent", "restore_pause_intent"}
    root = Path(__file__).resolve().parents[1]

    def guarded(stack):
        return any(isinstance(node, ast.AsyncWith) and any("operator_controls" in ast.unparse(item.context_expr)
                                                           for item in node.items) for node in stack)

    found, unguarded = [], []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(("tests/", ".venv/")) or "/site-packages/" in relative:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def visit(node, stack, relative=relative):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                target = ast.unparse(node.func.value)
                engine_control = node.func.attr in controls and target.endswith("engine")
                if engine_control or ast.unparse(node.func).endswith("switch_root_provider"):
                    found.append((relative, node.func.attr))
                    if not guarded(stack) and relative != "transfers/manual_route_switch.py":
                        unguarded.append((relative, node.lineno, ast.unparse(node.func)))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "switch_root_provider":
                found.append((relative, "switch_root_provider"))
                if not guarded(stack):
                    unguarded.append((relative, node.lineno, "switch_root_provider"))
            for child in ast.iter_child_nodes(node):
                visit(child, [*stack, node])

        visit(tree, [])
    assert unguarded == []
    assert {relative for relative, _name in found} == {"application/service.py", "application/manual_route_switch.py"}
    switch = (root / "transfers" / "manual_route_switch.py").read_text(encoding="utf-8")
    assert "engine.pause(" not in switch and "engine.resume(" not in switch
    assert switch.count("set_pause_and_fence(int(transfer_id), True, claimed_only=True)") == 1
    assert switch.count("set_pause_and_fence(int(transfer_id), False, claimed_only=True)") == 1
    service = (root / "application" / "service.py").read_text(encoding="utf-8")
    inside = [block for block in service.split("async with self.operator_controls")[1:]]
    assert inside and not any("operator_controls" in block.split("\n    async def ")[0] for block in inside)
