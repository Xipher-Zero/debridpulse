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
from types import SimpleNamespace
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
    SourceEntry,
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


# -- a replacement that reports the same files without their directories ------------------------------------------
#
# Transfer 531: the established route kept each season's folder, Debrid-Link
# reported the same files flat, and an inherited explicit selection could not
# be proven by exact path. Proven the same source (equal fingerprints) and the
# same complete member set (a unique basename+size bijection), it is carried:
# the established logical paths stay, the replacement supplies the material.

SEASONS = [("A.mkv", "S1/A.mkv", 100), ("B.mkv", "S2/B.mkv", 200), ("C.mkv", "S3/C.mkv", 300)]
FLAT = [("A.mkv", "A.mkv", 100), ("B.mkv", "B.mkv", 200), ("C.mkv", "C.mkv", 300)]
SOURCE = "a" * 40


def offer_source(provider, files, fingerprint):
    """``offer``, with the source fingerprint the provider reports for it."""
    resource = offer(provider, files)
    observed = replace(provider.resources[resource.id], fingerprint=fingerprint)
    provider.resources[resource.id] = observed
    provider.responses[-1] = replace(provider.responses[-1], observation=observed)
    return resource


async def chosen_then_switched(tmp_path, monkeypatch, *, target=FLAT, before=SOURCE, after=SOURCE,
                               chosen=("S1/A.mkv", "S3/C.mkv"), conflict=False):
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
    offer_source(providers["parcel-a"], SEASONS, before)
    transfer = await engine.submit((TransferRequest("magnet", MAGNET, name="Show", selection_mode="interactive"),),
                                   name="Show", deduplicate=False)
    for _ in range(3):
        await engine.tick()
    view = await repository.file_selection_presentation(transfer.id, now=engine.clock())
    await repository.confirm_file_selection(
        transfer.id, view["manifest_id"], [entry["entry_id"] for entry in view["entries"]
                                           if entry["relative_path"] in chosen], now=engine.clock())
    await settle(engine, 4)
    offer_source(providers["parcel-b"], target, after)
    await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    error = await first_conflict(repository, engine, transfer.id) if conflict else await settle(engine)
    return repository, engine, transfer, error


async def first_conflict(repository, engine, transfer_id, ticks=8):
    """The first state conflict the root records after the switch."""
    for _ in range(ticks):
        engine.clock.now += 30
        await engine.tick()
        root = await root_of(repository, transfer_id)
        if root.error is not None and root.error.category == Category.RESOURCE_STATE_CONFLICT:
            return root.error
    return None


async def members_of(repository, transfer_id):
    return sorted((item.entry.relative_path, item.request.payload)
                  for item in await repository.requests(transfer_id) if item.parent_id)


async def test_a_replacement_reporting_the_files_without_their_directories_carries_the_selection(
        tmp_path, monkeypatch):
    """M-C2 / M-C13 / M-C15 (transfer 531's shape)."""
    from transfers import file_selection as fs
    repository, _engine, transfer, _ = await chosen_then_switched(tmp_path, monkeypatch)
    root = await root_of(repository, transfer.id)
    assert root.resource.provider_id == "parcel-b"
    assert root.error is None or root.error.category != Category.RESOURCE_STATE_CONFLICT
    (generation,) = await rows("SELECT * FROM transfer_file_selections WHERE transfer_id=? AND provider_id='parcel-b'",
                               (transfer.id,))
    assert (generation["decision"], generation["decision_reason"], generation["continuity"]) == (
        "explicit", "inherited", "proven")
    assert generation["manifest_committed_at"] is not None
    # Established logical coordinates, the replacement's material, B never added.
    assert await members_of(repository, transfer.id) == [("S1/A.mkv", "x:A.mkv"), ("S3/C.mkv", "x:C.mkv")]
    # Provenance is the replacement manifest's own entries.
    binding = await repository.resource_binding_id(transfer.id, root.resource.id)
    recorded = {row["entry_id"] for row in await rows(
        "SELECT entry_id FROM transfer_file_selection_entries WHERE selection_id=?", (generation["id"],))}
    assert recorded == {fs.entry_identity(binding, "A.mkv"), fs.entry_identity(binding, "C.mkv")}


@pytest.mark.parametrize("before, after, target, reason", [
    ("", SOURCE, FLAT, "fallback_missing_fingerprint"),                       # M-C16
    (SOURCE, "", FLAT, "fallback_missing_fingerprint"),                       # M-C17
    (SOURCE, "b" * 40, FLAT, "fallback_fingerprint_mismatch"),                # M-C18
    (SOURCE, SOURCE, [("A.mkv", "A.mkv", 100), ("B.mkv", "x/A.mkv", 100), ("C.mkv", "C.mkv", 300)],
     "fallback_duplicate_identity"),                                          # M-C14
    (SOURCE, SOURCE, [("A.mkv", "A.mkv", 100), ("B.mkv", "B.mkv", 200), ("D.mkv", "D.mkv", 300)],
     "fallback_member_set_mismatch"),                                         # M-C14
])
async def test_an_unprovable_replacement_carries_nothing_and_says_why(tmp_path, monkeypatch, before, after, target,
                                                                       reason):
    repository, _engine, transfer, error = await chosen_then_switched(
        tmp_path, monkeypatch, target=target, before=before, after=after, conflict=True)
    assert error is not None and error.stage.value == "reconciliation" and error.diagnostic == reason
    assert "A.mkv" not in error.diagnostic and "S1" not in error.diagnostic
    assert not await rows("SELECT 1 FROM transfer_file_selections WHERE transfer_id=? AND provider_id='parcel-b' "
                          "AND manifest_committed_at IS NOT NULL", (transfer.id,))
    assert await members_of(repository, transfer.id) == [("S1/A.mkv", "x:S1/A.mkv"), ("S3/C.mkv", "x:S3/C.mkv")]


async def test_a_selection_of_the_current_generation_never_crosses_coordinates(tmp_path, monkeypatch):
    """M-C10 / M-C14: no inherited predecessor -- an executable manifest that
    moved a selected member is the existing conflict, now naming its reason."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "switch.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    provider = magnet_provider("parcel-a")
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3), clock=Clock())
    await engine.initialize()
    resource = offer_source(provider, SEASONS, SOURCE)
    provider.members[resource.id] = tuple(                 # executable list flattened, same source
        SourceEntry(name, size, path, TransferRequest("parcel-member", f"x:{path}", name=name))
        for name, path, size in FLAT)
    transfer = await engine.submit((TransferRequest("magnet", MAGNET, name="Show", selection_mode="interactive"),),
                                   name="Show", deduplicate=False)
    for _ in range(3):
        await engine.tick()
    view = await repository.file_selection_presentation(transfer.id, now=engine.clock())
    await repository.confirm_file_selection(
        transfer.id, view["manifest_id"], [entry["entry_id"] for entry in view["entries"]
                                           if entry["relative_path"] in {"S1/A.mkv", "S3/C.mkv"}],
        now=engine.clock())
    error = await first_conflict(repository, engine, transfer.id)
    assert error is not None and error.diagnostic == "selected_path_missing"
    assert await members_of(repository, transfer.id) == []


# -- the established paths survive every later replacement (transfer 533) -----------------------------------------
#
# A committed flat generation (an earlier migration) must not redefine where
# the members live: each later generation recovers the established logical
# paths by the members' identity. A generation that fails before it commits
# never becomes the predecessor.

def rejecting_provider(identity, *, priority=0):
    """A manifest provider that binds, then refuses the source at candidate
    preparation -- the provider's own permanent rejection (as Real-Debrid
    answers ``infringing_file``)."""
    from transfers.errors import Domain, NormalizedError, Origin, Permanence, Retryability, Stage

    class Rejecting(ParcelProvider):
        async def manifest(self, resource):
            self.calls.append(("manifest", resource.id))
            raise TransferError(NormalizedError(
                Domain.PROVIDER, Category.CANDIDATE_REJECTED, Stage.CANDIDATE_PREPARATION, Retryability.NEVER,
                origin=Origin.PROVIDER, permanence=Permanence.PERMANENT, integration_id=identity, native_code="35"))

    provider = Rejecting(identity, file_manifest=True)
    provider.descriptor = replace(provider.descriptor, request_types=frozenset({"magnet", "parcel-member"}),
                                  priority=priority)
    return provider


async def lineage_lab(tmp_path, monkeypatch, *, members=None, chosen=("S1/A.mkv", "S3/C.mkv"), deduplicate=False):
    """parcel-a (hierarchical, preferred), parcel-b (flat), parcel-c
    (rejects), parcel-d (hierarchical); the root starts on parcel-a with an
    explicit selection. ``members(provider_id, files)`` may supply each
    provider's executable members."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "switch.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    providers = {"parcel-a": magnet_provider("parcel-a"), "parcel-b": magnet_provider("parcel-b"),
                 "parcel-c": rejecting_provider("parcel-c"), "parcel-d": magnet_provider("parcel-d")}
    providers["parcel-a"].descriptor = replace(providers["parcel-a"].descriptor, priority=40)
    for provider in providers.values():
        registry.register_provider(provider)
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3), clock=Clock())
    await engine.initialize()
    lab = SimpleNamespace(repository=repository, engine=engine, providers=providers, members=members)
    lab.offer = lambda identity, files, native="x": supplied_offer(lab, identity, files, native)
    lab.offer("parcel-a", SEASONS)
    transfer = await engine.submit((TransferRequest("magnet", MAGNET, name="Show", selection_mode="interactive"),),
                                   name="Show", deduplicate=deduplicate)
    for _ in range(3):
        await engine.tick()
    view = await repository.file_selection_presentation(transfer.id, now=engine.clock())
    if chosen is None:                                      # Close/X on the live offer: ALL
        await repository.dismiss_file_selection(transfer.id, view["manifest_id"], now=engine.clock())
    else:
        await repository.confirm_file_selection(
            transfer.id, view["manifest_id"], [entry["entry_id"] for entry in view["entries"]
                                               if entry["relative_path"] in chosen], now=engine.clock())
    await settle(engine, 4)
    lab.transfer = transfer
    return lab


def supplied_offer(lab, identity, files, native):
    provider = lab.providers[identity]
    resource = offer_source(provider, files, SOURCE) if native == "x" else None
    if resource is None:
        result = provider.parcel(native, state=ResourceState.AVAILABLE, files=files)
        observed = replace(result.observation, request=TransferRequest("magnet", MAGNET), fingerprint=SOURCE)
        provider.resources[observed.resource.id] = observed
        provider.responses.append(replace(result, observation=observed))
        resource = observed.resource
    if lab.members is not None:
        provider.members[resource.id] = lab.members(identity, files)
    return resource


async def switched(lab, target, files, *, expected, native="x"):
    lab.offer(target, files, native)
    await switch_root_provider(lab.engine, lab.transfer.id, target, expected_provider_id=expected)
    await settle(lab.engine)


async def generation_of(transfer_id, provider_id):
    return (await rows("SELECT * FROM transfer_file_selections WHERE transfer_id=? AND provider_id=? "
                       "ORDER BY created_at DESC, id DESC LIMIT 1", (transfer_id, provider_id)))[0]


def committed_proven(generation):
    return (generation["manifest_committed_at"] is not None, generation["continuity"]) == (True, "proven")


async def test_hierarchy_then_flat_then_hierarchy_keeps_the_established_paths(tmp_path, monkeypatch):
    """FB-2 / T-C4 / T-C19."""
    lab = await lineage_lab(tmp_path, monkeypatch)
    await switched(lab, "parcel-b", FLAT, expected="parcel-a")
    assert committed_proven(await generation_of(lab.transfer.id, "parcel-b"))          # FB-1/T-C2: first hop
    await switched(lab, "parcel-d", SEASONS, expected="parcel-b")
    generation = await generation_of(lab.transfer.id, "parcel-d")
    assert committed_proven(generation), (generation["continuity"], generation["continuity_reason"])
    assert await members_of(lab.repository, lab.transfer.id) == [("S1/A.mkv", "x:S1/A.mkv"),
                                                                 ("S3/C.mkv", "x:S3/C.mkv")]


async def test_hierarchy_then_flat_then_flat_keeps_the_established_paths(tmp_path, monkeypatch):
    """T-C5: the second flat replacement proves by exact path against the
    flat predecessor; the logical paths are still the established ones."""
    from transfers import file_selection as fs
    lab = await lineage_lab(tmp_path, monkeypatch)
    await switched(lab, "parcel-b", FLAT, expected="parcel-a")
    await switched(lab, "parcel-d", FLAT, expected="parcel-b")
    generation = await generation_of(lab.transfer.id, "parcel-d")
    assert committed_proven(generation), (generation["continuity"], generation["continuity_reason"])
    assert await members_of(lab.repository, lab.transfer.id) == [("S1/A.mkv", "x:A.mkv"), ("S3/C.mkv", "x:C.mkv")]
    root = await root_of(lab.repository, lab.transfer.id)
    binding = await lab.repository.resource_binding_id(lab.transfer.id, root.resource.id)
    recorded = {row["entry_id"] for row in await rows(
        "SELECT entry_id FROM transfer_file_selection_entries WHERE selection_id=?", (generation["id"],))}
    assert recorded == {fs.entry_identity(binding, "A.mkv"), fs.entry_identity(binding, "C.mkv")}   # T-C18


async def test_a_rejected_intermediate_never_becomes_the_predecessor_and_the_next_switch_commits(
        tmp_path, monkeypatch):
    """FB-3 / T-C6 (transfer 533): hierarchy, flat committed, a provider that
    binds and then rejects the source, then an operator switch onward."""
    lab = await lineage_lab(tmp_path, monkeypatch)
    await switched(lab, "parcel-b", FLAT, expected="parcel-a")
    flat = await generation_of(lab.transfer.id, "parcel-b")
    lab.offer("parcel-a", SEASONS, native="x2")                       # where the core's reselection lands
    await switched(lab, "parcel-c", SEASONS, expected="parcel-b")
    rejected = await generation_of(lab.transfer.id, "parcel-c")
    assert rejected["manifest_committed_at"] is None                                   # never authority
    root = await root_of(lab.repository, lab.transfer.id)
    await switched(lab, "parcel-d", SEASONS, expected=root.resource.provider_id, native="y")
    generation = await generation_of(lab.transfer.id, "parcel-d")
    assert generation["predecessor_id"] != rejected["id"]
    assert committed_proven(generation), (generation["continuity"], generation["continuity_reason"])
    assert await members_of(lab.repository, lab.transfer.id) == [("S1/A.mkv", "y:S1/A.mkv"),
                                                                 ("S3/C.mkv", "y:S3/C.mkv")]
    assert flat["manifest_committed_at"] is not None


async def test_automatic_failover_after_a_rejected_intermediate_commits_and_so_does_a_later_switch(
        tmp_path, monkeypatch):
    """FB-4 / T-C7 / T-C8: after the rejection the core's own reselection
    (the preferred hierarchical provider) commits the inherited selection;
    a later operator switch commits too."""
    lab = await lineage_lab(tmp_path, monkeypatch)
    await switched(lab, "parcel-b", FLAT, expected="parcel-a")
    lab.offer("parcel-a", SEASONS, native="x2")                       # what parcel-a answers when reselected
    await switched(lab, "parcel-c", SEASONS, expected="parcel-b")
    automatic = await generation_of(lab.transfer.id, "parcel-a")
    root = await root_of(lab.repository, lab.transfer.id)
    assert root.resource.provider_id == "parcel-a" and root.resource.id.endswith("x2")
    assert committed_proven(automatic), (automatic["continuity"], automatic["continuity_reason"])
    await switched(lab, "parcel-d", SEASONS, expected="parcel-a", native="y")
    later = await generation_of(lab.transfer.id, "parcel-d")
    assert committed_proven(later), (later["continuity"], later["continuity_reason"])
    assert await members_of(lab.repository, lab.transfer.id) == [("S1/A.mkv", "y:S1/A.mkv"),
                                                                 ("S3/C.mkv", "y:S3/C.mkv")]


# -- credentials follow the proven coordinate, never a path match --------------------------------------------------

def credentialed(provider_id, files):
    """Members whose requests carry USER_SUPPLIED userinfo: a primary and an
    alternate per member, each its own scope."""
    return tuple(
        SourceEntry(name, size, path,
                    TransferRequest("parcel-member", f"sftp://user-{provider_id}:secret@{provider_id}.example/{path}",
                                    name=name),
                    alternates=(TransferRequest("parcel-member",
                                                f"sftp://alt-{provider_id}:secret@mirror-{provider_id}.example/{path}",
                                                name=name),))
        for name, path, size in files)


def admitted(lab):
    """``{(child request id, scope host)}`` holding admitted material."""
    return {(key[1], key[2].host) for key, context in lab.engine.inputs._contexts.items()
            if key[0] == lab.transfer.id and context.materials}


def child(lab, root_id, path, alternate=0):
    from transfers._repository_base import manifest_child_identity
    return manifest_child_identity(root_id, path, alternate)


async def test_credentials_are_admitted_to_the_established_member_across_coordinates(tmp_path, monkeypatch):
    """C-C1 (exact path), C-C2 / C-C6 (flat replacement), C-C3 (an unselected
    member's credentials stay unadmitted), C-C4 (alternate ordinals), C-C5
    (hierarchy, flat, hierarchy: always the same logical child)."""
    lab = await lineage_lab(tmp_path, monkeypatch, members=credentialed)
    root = await root_of(lab.repository, lab.transfer.id)
    expected = {(child(lab, root.id, path, alternate), f"{prefix}{provider}.example")
                for path in ("S1/A.mkv", "S3/C.mkv") for alternate, prefix in ((0, ""), (1, "mirror-"))
                for provider in ("parcel-a",)}
    assert admitted(lab) == expected                                                  # C-C1
    await switched(lab, "parcel-b", FLAT, expected="parcel-a")
    expected |= {(child(lab, root.id, path, alternate), f"{prefix}parcel-b.example")
                 for path in ("S1/A.mkv", "S3/C.mkv") for alternate, prefix in ((0, ""), (1, "mirror-"))}
    assert admitted(lab) == expected                                                  # C-C2..C-C4
    await switched(lab, "parcel-d", SEASONS, expected="parcel-b")
    expected |= {(child(lab, root.id, path, alternate), f"{prefix}parcel-d.example")
                 for path in ("S1/A.mkv", "S3/C.mkv") for alternate, prefix in ((0, ""), (1, "mirror-"))}
    assert admitted(lab) == expected                                                  # C-C5
    flat_children = {child(lab, root.id, path, alternate) for path in ("A.mkv", "B.mkv", "C.mkv", "S2/B.mkv")
                     for alternate in (0, 1)}
    assert not {request for request, _host in admitted(lab)} & flat_children


async def test_a_credential_without_one_proven_coordinate_is_withheld(tmp_path, monkeypatch):
    """C-C7: a translation that names no member, or names one logical member
    for two coordinates, admits nothing it cannot place -- never at the
    provider's own path, never to another member."""
    from transfers.repository import ManifestCommitResult
    lab = await lineage_lab(tmp_path, monkeypatch, members=credentialed)
    root = await root_of(lab.repository, lab.transfer.id)
    before = admitted(lab)
    commit = lab.repository.commit_selected_manifest

    async def ambiguous(record, entries, *, now):
        result = await commit(record, entries, now=now)
        return ManifestCommitResult(tuple(result), first_commitment=result.first_commitment,
                                    selection_id=result.selection_id, held=result.held,
                                    coordinates={"A.mkv": "S1/A.mkv", "B.mkv": "S1/A.mkv"})

    monkeypatch.setattr(lab.repository, "commit_selected_manifest", ambiguous)
    await switched(lab, "parcel-b", FLAT, expected="parcel-a")
    new = admitted(lab) - before
    assert not {host for _request, host in new} & {"parcel-b.example", "mirror-parcel-b.example"}


def test_the_engine_places_credentials_by_the_proven_coordinate_only():
    """C-C6: the engine consumes the selection owner's translation; it
    derives no member identity of its own."""
    import inspect
    from transfers.engine import TransferEngine as Engine
    source = inspect.getsource(Engine._observe_resource)
    assert "coordinates" in source
    for forbidden in ("migrate_inherited_subset", "established_logical_paths", "PurePosixPath", ".name,",
                      "fingerprint", "basename"):
        assert forbidden not in source, forbidden


# -- the transfer owns its file selection: providers prove it, never ask again --------------------------------------
#
# Once the operator settles a usable multi-file choice (Confirm, Close/X, or
# the decision timeout), that concrete member set is the live transfer's
# intent. Every later provider generation silently proves and records it; an
# operator's later deselect or reselect revises it; deleting the transfer ends
# it.

async def active_members(repository, transfer_id):
    return sorted((item.entry.relative_path, item.request.payload)
                  for item in await repository.requests(transfer_id) if item.parent_id and item.state != "skipped")


async def intent_of(lab):
    root = await root_of(lab.repository, lab.transfer.id)
    if not await rows("SELECT 1 FROM transfer_file_selection_intents WHERE request_id=?", (root.id,)):
        return None
    return sorted(row["relative_path"] for row in await rows(
        "SELECT relative_path FROM transfer_file_selection_intent_entries WHERE request_id=?", (root.id,)))


def never_offered(generation):
    return (generation["decision"], generation["hold_until"], generation["auto_offer_queued_at"]) == (
        "explicit", None, None)


async def deselect(lab, path, selected=False):
    member = next(item for item in await lab.repository.requests(lab.transfer.id)
                  if item.parent_id and item.entry.relative_path == path and item.id == child(lab, item.parent_id, path))
    (artifact,) = await rows("SELECT id FROM download_files WHERE request_id=?", (member.id,))
    await lab.repository.select_artifact(lab.transfer.id, artifact["id"], selected)


async def test_a_successor_after_a_settled_all_never_offers_selection_again(tmp_path, monkeypatch):
    """FB-1 / T2: Close/X settled ALL on a usable three-file manifest; the
    next provider never reopens the selector and commits exactly those
    three."""
    lab = await lineage_lab(tmp_path, monkeypatch, chosen=None)
    await switched(lab, "parcel-d", SEASONS, expected="parcel-a", native="y")
    generation = await generation_of(lab.transfer.id, "parcel-d")
    assert never_offered(generation), dict(generation)
    assert committed_proven(generation)
    assert await active_members(lab.repository, lab.transfer.id) == [
        ("S1/A.mkv", "y:S1/A.mkv"), ("S2/B.mkv", "y:S2/B.mkv"), ("S3/C.mkv", "y:S3/C.mkv")]


async def test_an_explicit_selection_crosses_providers_without_ever_being_offered_again(tmp_path, monkeypatch):
    """FB-3 / T1: no successor is ever pending or offerable; each commits
    exactly the confirmed members, across a path-changing provider too."""
    lab = await lineage_lab(tmp_path, monkeypatch)
    offers = len(await rows("SELECT 1 FROM application_events WHERE kind='file_selection_available'"))
    await switched(lab, "parcel-b", FLAT, expected="parcel-a")
    await switched(lab, "parcel-d", SEASONS, expected="parcel-b", native="y")
    for provider in ("parcel-b", "parcel-d"):
        generation = await generation_of(lab.transfer.id, provider)
        assert never_offered(generation), (provider, dict(generation))
        assert committed_proven(generation)
    assert len(await rows("SELECT 1 FROM application_events WHERE kind='file_selection_available'")) == offers
    assert await active_members(lab.repository, lab.transfer.id) == [("S1/A.mkv", "y:S1/A.mkv"),
                                                                     ("S3/C.mkv", "y:S3/C.mkv")]


async def test_a_deselect_after_commitment_is_what_every_later_provider_commits(tmp_path, monkeypatch):
    """FB-2 / T4: confirmed A+C; the operator deselects C (a switch detaches
    the writers, so it has none); the flat provider and then a hierarchical
    one each commit A alone, and the generation that committed A+C keeps
    its own record."""
    lab = await lineage_lab(tmp_path, monkeypatch)
    first = await generation_of(lab.transfer.id, "parcel-a")
    lab.offer("parcel-b", FLAT)
    await switch_root_provider(lab.engine, lab.transfer.id, "parcel-b", expected_provider_id="parcel-a")
    await deselect(lab, "S3/C.mkv")
    await settle(lab.engine)
    flat = await generation_of(lab.transfer.id, "parcel-b")
    assert committed_proven(flat), (flat["continuity"], flat["continuity_reason"])
    assert await active_members(lab.repository, lab.transfer.id) == [("S1/A.mkv", "x:A.mkv")]
    await switched(lab, "parcel-d", SEASONS, expected="parcel-b", native="y")
    later = await generation_of(lab.transfer.id, "parcel-d")
    assert committed_proven(later), (later["continuity"], later["continuity_reason"])
    assert await active_members(lab.repository, lab.transfer.id) == [("S1/A.mkv", "y:S1/A.mkv")]
    assert len(await rows("SELECT 1 FROM transfer_file_selection_entries WHERE selection_id=?", (first["id"],))) == 2
    assert await intent_of(lab) == ["S1/A.mkv"]


async def test_a_reselect_before_the_successor_commits_restores_the_member(tmp_path, monkeypatch):
    """T5."""
    lab = await lineage_lab(tmp_path, monkeypatch)
    lab.offer("parcel-b", FLAT)
    await switch_root_provider(lab.engine, lab.transfer.id, "parcel-b", expected_provider_id="parcel-a")
    await deselect(lab, "S3/C.mkv")
    await deselect(lab, "S3/C.mkv", selected=True)
    await settle(lab.engine)
    assert committed_proven(await generation_of(lab.transfer.id, "parcel-b"))
    assert await active_members(lab.repository, lab.transfer.id) == [("S1/A.mkv", "x:A.mkv"), ("S3/C.mkv", "x:C.mkv")]
    assert await intent_of(lab) == ["S1/A.mkv", "S3/C.mkv"]


async def test_a_reselect_after_a_newer_generation_committed_without_the_member_is_refused(tmp_path, monkeypatch):
    """The late re-add boundary: the member is no longer part of the current
    committed decomposition; the reselect is refused and changes nothing."""
    lab = await lineage_lab(tmp_path, monkeypatch)
    lab.offer("parcel-b", FLAT)
    await switch_root_provider(lab.engine, lab.transfer.id, "parcel-b", expected_provider_id="parcel-a")
    await deselect(lab, "S3/C.mkv")
    await settle(lab.engine)
    with pytest.raises(TransferError) as caught:
        await deselect(lab, "S3/C.mkv", selected=True)
    assert caught.value.error.category == Category.RESOURCE_STATE_CONFLICT
    assert await intent_of(lab) == ["S1/A.mkv"]


async def test_a_rejected_successor_never_changes_the_intent(tmp_path, monkeypatch):
    """T6."""
    lab = await lineage_lab(tmp_path, monkeypatch)
    lab.offer("parcel-a", SEASONS, native="x2")
    await switched(lab, "parcel-c", SEASONS, expected="parcel-a")
    assert (await generation_of(lab.transfer.id, "parcel-c"))["manifest_committed_at"] is None
    automatic = await generation_of(lab.transfer.id, "parcel-a")
    assert never_offered(automatic) and committed_proven(automatic)
    assert await intent_of(lab) == ["S1/A.mkv", "S3/C.mkv"]


def executable_only(path):
    """parcel-d executes ``path`` alone of its three described files (as
    Real-Debrid may link fewer files than it describes); every other
    provider executes all of them."""
    def members(identity, files):
        native = "y" if identity == "parcel-d" else "x"
        return tuple(SourceEntry(name, size, member, TransferRequest("parcel-member", f"{native}:{member}", name=name))
                     for name, member, size in files if identity != "parcel-d" or member == path)
    return members


async def test_an_inherited_selection_commits_against_a_successor_executing_only_that_member(tmp_path, monkeypatch):
    """T10 (transfer 538): described three, executes the chosen one."""
    lab = await lineage_lab(tmp_path, monkeypatch, chosen=("S1/A.mkv",), members=executable_only("S1/A.mkv"))
    offers = len(await rows("SELECT 1 FROM application_events WHERE kind='file_selection_available'"))
    await switched(lab, "parcel-d", SEASONS, expected="parcel-a", native="y")
    generation = await generation_of(lab.transfer.id, "parcel-d")
    assert never_offered(generation) and committed_proven(generation), dict(generation)
    assert await active_members(lab.repository, lab.transfer.id) == [("S1/A.mkv", "y:S1/A.mkv")]
    assert len(await rows("SELECT 1 FROM application_events WHERE kind='file_selection_available'")) == offers
    assert await intent_of(lab) == ["S1/A.mkv"]


async def test_an_inherited_selection_refuses_a_successor_executing_only_another_member(tmp_path, monkeypatch):
    """T11: the successor's one executable member is not the chosen one."""
    lab = await lineage_lab(tmp_path, monkeypatch, chosen=("S1/A.mkv",), members=executable_only("S2/B.mkv"))
    offers = len(await rows("SELECT 1 FROM application_events WHERE kind='file_selection_available'"))
    lab.offer("parcel-d", SEASONS, "y")
    await switch_root_provider(lab.engine, lab.transfer.id, "parcel-d", expected_provider_id="parcel-a")
    error = await first_conflict(lab.repository, lab.engine, lab.transfer.id)
    assert error is not None and error.diagnostic == "fallback_executable_path_missing"
    generation = await generation_of(lab.transfer.id, "parcel-d")
    assert never_offered(generation) and generation["manifest_committed_at"] is None, dict(generation)
    assert "S2/B.mkv" not in [path for path, _payload in await members_of(lab.repository, lab.transfer.id)]
    assert len(await rows("SELECT 1 FROM application_events WHERE kind='file_selection_available'")) == offers
    assert await intent_of(lab) == ["S1/A.mkv"]


async def test_an_empty_intent_is_kept_and_never_broadens(tmp_path, monkeypatch):
    """T10: every member deselected -- the intent row stays, empty, and
    nothing is materialized as ALL."""
    lab = await lineage_lab(tmp_path, monkeypatch)
    lab.offer("parcel-b", FLAT)
    await switch_root_provider(lab.engine, lab.transfer.id, "parcel-b", expected_provider_id="parcel-a")
    await deselect(lab, "S1/A.mkv")
    await deselect(lab, "S3/C.mkv")
    await settle(lab.engine)
    assert await intent_of(lab) == []
    assert await active_members(lab.repository, lab.transfer.id) == []
    flat = await generation_of(lab.transfer.id, "parcel-b")
    assert not await rows("SELECT 1 FROM transfer_file_selection_entries WHERE selection_id=?", (flat["id"],))


async def test_lifecycle_retirement_never_edits_the_intent(tmp_path, monkeypatch):
    """T9: generations superseding members and provider switches change no
    intent; only the operator's member control does."""
    lab = await lineage_lab(tmp_path, monkeypatch)
    root = await root_of(lab.repository, lab.transfer.id)
    (before,) = await rows("SELECT updated_at FROM transfer_file_selection_intents WHERE request_id=?", (root.id,))
    await switched(lab, "parcel-b", FLAT, expected="parcel-a")
    await switched(lab, "parcel-d", SEASONS, expected="parcel-b", native="y")
    (after,) = await rows("SELECT updated_at FROM transfer_file_selection_intents WHERE request_id=?", (root.id,))
    assert after["updated_at"] == before["updated_at"]
    assert await intent_of(lab) == ["S1/A.mkv", "S3/C.mkv"]


async def test_a_deleted_transfer_takes_its_intent_and_a_re_add_chooses_afresh(tmp_path, monkeypatch):
    """T7 / T13 (delete): the same source added again is a new transfer with
    no intent -- its source fingerprint equal, its lineage not -- and its
    first usable manifest is offered."""
    lab = await lineage_lab(tmp_path, monkeypatch, deduplicate=True)
    old_root = await root_of(lab.repository, lab.transfer.id)
    await lab.engine.delete(lab.transfer.id, remote=False)
    assert not await rows("SELECT 1 FROM transfer_file_selection_intents WHERE request_id=?", (old_root.id,))
    assert not await rows("SELECT 1 FROM transfer_file_selection_intent_entries WHERE request_id=?", (old_root.id,))
    assert await rows("SELECT 1 FROM transfer_file_selections WHERE request_id=?", (old_root.id,))   # history kept
    lab.offer("parcel-a", SEASONS, native="z")
    again = await lab.engine.submit((TransferRequest("magnet", MAGNET, name="Show", selection_mode="interactive"),),
                                    name="Show", deduplicate=True)
    assert again.id != lab.transfer.id
    fingerprints = await rows("SELECT source_fingerprint FROM torrents WHERE id IN (?,?)", (lab.transfer.id, again.id))
    assert len({row["source_fingerprint"] for row in fingerprints}) == 1
    for _ in range(3):
        await lab.engine.tick()
    lab.transfer = again
    assert await intent_of(lab) is None
    (fresh,) = await rows("SELECT * FROM transfer_file_selections WHERE transfer_id=?", (again.id,))
    assert (fresh["decision"], fresh["predecessor_id"]) == ("pending", None) and fresh["auto_offer_queued_at"]


async def test_a_re_add_after_delete_is_a_new_transfer_offered_afresh(tmp_path, monkeypatch):
    """FB-4 (preservation): a deleted transfer's selection never reaches an
    independent re-add of the same source."""
    lab = await lineage_lab(tmp_path, monkeypatch, deduplicate=True)
    await lab.engine.delete(lab.transfer.id, remote=False)
    lab.offer("parcel-a", SEASONS, native="z")
    again = await lab.engine.submit((TransferRequest("magnet", MAGNET, name="Show", selection_mode="interactive"),),
                                    name="Show", deduplicate=True)
    assert again.id != lab.transfer.id
    for _ in range(3):
        await lab.engine.tick()
    (fresh,) = await rows("SELECT * FROM transfer_file_selections WHERE transfer_id=?", (again.id,))
    assert (fresh["decision"], fresh["predecessor_id"]) == ("pending", None) and fresh["auto_offer_queued_at"]


async def test_a_deduplicated_submission_keeps_the_live_transfers_intent(tmp_path, monkeypatch):
    """T8."""
    lab = await lineage_lab(tmp_path, monkeypatch, deduplicate=True)
    same = await lab.engine.submit((TransferRequest("magnet", MAGNET, name="Show", selection_mode="interactive"),),
                                   name="Show", deduplicate=True)
    assert same.id == lab.transfer.id
    assert await intent_of(lab) == ["S1/A.mkv", "S3/C.mkv"]


async def test_no_usable_manifest_settles_all_without_intent_and_a_later_manifest_is_offered(tmp_path, monkeypatch):
    """T3 / T14: a provider that never publishes a complete manifest (its
    observation carries none) settles ALL by the manifest grace; that is no
    selection, so the next provider's first usable manifest is offered."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "switch.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    providers = {name: magnet_provider(name) for name in ("parcel-a", "parcel-b")}
    for provider in providers.values():
        registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3), clock=Clock())
    await engine.initialize()
    resource = offer_source(providers["parcel-a"], SEASONS, SOURCE)
    providers["parcel-a"].resources[resource.id] = replace(providers["parcel-a"].resources[resource.id],
                                                           file_manifest=None)
    providers["parcel-a"].responses[-1] = replace(
        providers["parcel-a"].responses[-1],
        observation=replace(providers["parcel-a"].responses[-1].observation, file_manifest=None))
    transfer = await engine.submit((TransferRequest("magnet", MAGNET, name="Show", selection_mode="interactive"),),
                                   name="Show", deduplicate=False)
    await settle(engine, 6)
    (first,) = await rows("SELECT * FROM transfer_file_selections WHERE transfer_id=?", (transfer.id,))
    assert (first["decision"], first["decision_reason"], first["hold_until"]) == ("all", "manifest_timeout", None)
    lab = SimpleNamespace(repository=repository, transfer=transfer)
    assert await intent_of(lab) is None
    offer_source(providers["parcel-b"], SEASONS, SOURCE)
    await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    for _ in range(3):
        await engine.tick()
    second = await generation_of(transfer.id, "parcel-b")
    assert second["decision"] == "pending" and second["auto_offer_queued_at"] is not None


async def test_a_transfer_without_intent_gains_it_only_from_an_operator_member_change(tmp_path, monkeypatch):
    """T11 (operator action): a transfer whose selection predates the intent
    (no intent row -- nothing is backfilled) keeps the pre-change behavior
    until the operator changes a member; that change establishes the intent
    from the committed decomposition, and the next provider commits it."""
    lab = await lineage_lab(tmp_path, monkeypatch)
    root = await root_of(lab.repository, lab.transfer.id)
    async with get_db() as db:
        await db.execute("DELETE FROM transfer_file_selection_intent_entries WHERE request_id=?", (root.id,))
        await db.execute("DELETE FROM transfer_file_selection_intents WHERE request_id=?", (root.id,))
        await db.commit()
    assert await intent_of(lab) is None
    lab.offer("parcel-b", FLAT)
    await switch_root_provider(lab.engine, lab.transfer.id, "parcel-b", expected_provider_id="parcel-a")
    await deselect(lab, "S3/C.mkv")
    assert await intent_of(lab) == ["S1/A.mkv"]
    await settle(lab.engine)
    assert committed_proven(await generation_of(lab.transfer.id, "parcel-b"))
    assert await active_members(lab.repository, lab.transfer.id) == [("S1/A.mkv", "x:A.mkv")]


async def test_an_intent_established_while_a_successor_is_open_governs_that_successor(tmp_path, monkeypatch):
    """A committed ALL that is no intent (the manifest grace ran out), then a
    provider whose usable manifest is offered. The operator deselects one
    established member: that establishes the intent, and the open successor
    carries it at once -- no longer offered, never ALL past its old deadline,
    committing exactly the reduced intent; a stale Confirm or Close from the
    earlier offer conflicts and replaces nothing."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "switch.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    providers = {name: magnet_provider(name) for name in ("parcel-a", "parcel-b")}
    for provider in providers.values():
        registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3), clock=Clock())
    await engine.initialize()
    resource = offer_source(providers["parcel-a"], SEASONS, SOURCE)
    providers["parcel-a"].resources[resource.id] = replace(providers["parcel-a"].resources[resource.id],
                                                           file_manifest=None)
    providers["parcel-a"].responses[-1] = replace(
        providers["parcel-a"].responses[-1],
        observation=replace(providers["parcel-a"].responses[-1].observation, file_manifest=None))
    transfer = await engine.submit((TransferRequest("magnet", MAGNET, name="Show", selection_mode="interactive"),),
                                   name="Show", deduplicate=False)
    await settle(engine, 6)
    lab = SimpleNamespace(repository=repository, engine=engine, transfer=transfer)
    assert (await generation_of(transfer.id, "parcel-a"))["decision_reason"] == "manifest_timeout"
    assert await intent_of(lab) is None

    offer_source(providers["parcel-b"], SEASONS, SOURCE)
    await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    for _ in range(2):
        await engine.tick()
    opened = await generation_of(transfer.id, "parcel-b")
    assert opened["decision"] == "pending" and opened["auto_offer_queued_at"] is not None
    assert [offer["selection_id"] for offer in await repository.active_file_selection_offers(now=engine.clock())] \
        == [opened["id"]]

    await deselect(lab, "S3/C.mkv")
    assert await intent_of(lab) == ["S1/A.mkv", "S2/B.mkv"]
    carried = await generation_of(transfer.id, "parcel-b")
    assert (carried["decision"], carried["decision_reason"]) == ("explicit", "inherited")
    assert await repository.active_file_selection_offers(now=engine.clock()) == []

    stale_confirm = await repository.confirm_file_selection(
        transfer.id, carried["manifest_id"], [fs_entry(carried, "S3/C.mkv")], now=engine.clock())
    stale_close = await repository.dismiss_file_selection(transfer.id, carried["manifest_id"], now=engine.clock())
    assert (stale_confirm.outcome, stale_close.outcome) == ("conflict", "conflict")
    assert await intent_of(lab) == ["S1/A.mkv", "S2/B.mkv"]

    engine.clock.now = float(opened["hold_until"]) + 60                # past the old decision deadline
    await settle(engine)
    committed = await generation_of(transfer.id, "parcel-b")
    assert (committed["decision"], committed["continuity"]) == ("explicit", "proven")
    assert committed["manifest_committed_at"] is not None
    assert await active_members(repository, transfer.id) == [("S1/A.mkv", "x:S1/A.mkv"),
                                                             ("S2/B.mkv", "x:S2/B.mkv")]
    recorded = await rows("SELECT entry_id FROM transfer_file_selection_entries WHERE selection_id=?",
                          (committed["id"],))
    assert {row["entry_id"] for row in recorded} == {fs_entry(committed, "S1/A.mkv"), fs_entry(committed, "S2/B.mkv")}


def fs_entry(generation, path):
    from transfers import file_selection as fs
    return fs.entry_identity(generation["provider_resource_id"], path)
