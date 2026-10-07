"""Transfer 523: provider-local failure never strands the logical root.

A provider's failure is scoped to the provider: whether routing continues is
decided by that scope, never by the lifecycle stage the route had reached.
A route the operator chose and that already published its manifest is
consumed exactly like any other current route: a provider-local permanent
failure exhausts that provider for the root and the one canonical competition
continues at once; a provider-local transient failure backs off only that
provider while any other legitimate provider proceeds now; the root waits only
when nothing can run, and every provider's retries stay bounded. The picker
projects the same durable failure truth.

Every provider here is a neutral fixture; the one native refusal goes through
the Real-Debrid adapter's own translation, unchanged.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import replace

import pytest
from test_v113_collection_route_generic_closure import Clock
from test_v113_live_provider_routing_corrective import FILES, native_refusal, offer, refusing
from test_v113_root_provider_switch import MAGNET, magnet_provider, root_of, rows
from test_v113_root_provider_switch_corrective import ParkingExecutor

from db import database
from providers.realdebrid.translation import translate_error
from transfers import manual_route_switch
from transfers.convergence_engine import TransferEngine
from transfers.errors import (
    Category,
    Domain,
    NormalizedError,
    Origin,
    Permanence,
    Recovery,
    Retryability,
    Stage,
    TransferError,
)
from transfers.manual_route_switch import switch_root_provider
from transfers.models import ResourceState, TransferRequest
from transfers.policy import TransferPolicy, provider_attributable
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

# The live runtime's policy (transfer 523's trace). A provider whose route
# ended on a transient failure re-enters automatic competition after the
# ordinary retry interval (``retry_delay``); a same-route resolution retry
# keeps the resolution retry delay.
LIVE_POLICY = dict(retry_delay=60.0, resolution_retry_delay=300.0, max_attempts=3)
REENTRY = LIVE_POLICY["retry_delay"]
PROTOCOL = NormalizedError(Domain.PROVIDER, Category.PROVIDER_PROTOCOL_VIOLATION, Stage.RESOLUTION,
                           Retryability.UNKNOWN, origin=Origin.PROVIDER, diagnostic="returned invalid JSON")


async def lab(tmp_path, monkeypatch, *providers):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "failover.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    for provider in providers:
        registry.register_provider(provider)
    executor = ParkingExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(**LIVE_POLICY), clock=Clock())
    await engine.initialize()
    return repository, engine, executor


async def submit(engine):
    """An interactive torrent root, as transfer 523 was: each provider's
    complete early manifest is durably recorded and offered before its
    members are prepared."""
    return await engine.submit((TransferRequest("magnet", MAGNET, name="Show", selection_mode="interactive"),),
                               name="Show", deduplicate=False)


HOLD = 130.0                                                       # past the selector's decision hold


async def tick(engine, seconds=1.0, times=1):
    for _ in range(times):
        engine.clock.now += seconds
        await engine.tick()


async def routes(request_id):
    """The root's route history: provider, attempt state, operation,
    transition kind and reason, error."""
    return [(row["provider_id"], row["state"], row["operation"], row["transition_kind"],
             row["transition_reason"], row["error"]) for row in await rows(
        """SELECT a.provider_id,a.state,a.error,p.operation,p.transition_kind,p.transition_reason
           FROM route_attempt_provenance p JOIN resolution_attempts a ON a.id=p.resolution_attempt_id
           WHERE a.request_id=? ORDER BY p.ordinal""", (request_id,))]


async def status(transfer_id):
    return (await rows("SELECT status FROM torrents WHERE id=?", (transfer_id,)))[0]["status"]


async def terminal_count(transfer_id):
    return (await rows("SELECT COUNT(*) AS n FROM application_events WHERE transfer_id=? AND kind='error'",
                       (transfer_id,)))[0]["n"]


async def productive_then_switched(tmp_path, monkeypatch, target, *others, offered=True):
    """Transfer 523's opening: ``parcel-a`` resolves the root, decomposes it
    and downloads; the operator then switches the root to ``target`` (which
    is offered the root unless ``offered`` is false)."""
    first = magnet_provider("parcel-a")
    repository, engine, executor = await lab(tmp_path, monkeypatch, first, target, *others)
    offer(first)
    transfer = await submit(engine)
    await tick(engine, times=2)
    await tick(engine, HOLD, times=2)
    artifacts = await repository.artifacts(transfer.id)
    assert len(artifacts) == len(FILES) and any(artifact.execution for artifact in artifacts)
    if offered:
        offer(target)
    await switch_root_provider(engine, transfer.id, target.descriptor.id, expected_provider_id="parcel-a")
    return repository, engine, executor, first, transfer


def choices(status_):
    return {entry["provider_id"]: (entry["status"], entry["selectable"]) for entry in status_["providers"]}


# -- 15.1: the canonical transfer-523 regression -------------------------------------------------------------------

async def test_an_operator_chosen_route_that_fails_after_its_manifest_continues_automatically(tmp_path, monkeypatch):
    refuser = refusing(magnet_provider("parcel-b"), native_refusal(35, "infringing_file"))
    alternate = magnet_provider("parcel-c")
    repository, engine, _executor, first, transfer = await productive_then_switched(
        tmp_path, monkeypatch, refuser, alternate)
    offer(first, native="again")
    offer(alternate)
    root = await root_of(repository, transfer.id)

    await tick(engine, times=2)                                    # no operator action from here on
    await tick(engine, HOLD, times=2)
    await tick(engine, times=4)

    assert ("manifest", f"parcel-b:x") in refuser.calls            # it reached candidate preparation
    selection = await rows("""SELECT s.manifest_id FROM transfer_file_selections s JOIN provider_resources r
        ON r.id=s.provider_resource_id WHERE s.request_id=? AND r.provider_id='parcel-b'""", (root.id,))
    assert selection and selection[0]["manifest_id"]               # after its complete manifest
    assert "parcel-b" in await repository.exhausted_route_providers(root.id)
    history = await routes(root.id)
    failed = [item for item in history if item[0] == "parcel-b"]
    assert [item[1:3] for item in failed] == [("exhausted", "operator_switch")]
    for fact in ('"category":"candidate_rejected"', '"native_code":"35"', '"diagnostic":"infringing_file"',
                 '"stage":"candidate_preparation"', '"integration_id":"realdebrid"'):
        assert fact in failed[0][5], fact
    successor = history[history.index(failed[0]) + 1]
    assert successor[2:5] == ("resolve", "provider_change", "candidate_rejected")   # automatic, never operator
    assert successor[0] in {"parcel-a", "parcel-c"}
    assert await repository.bound_route_provider(root.id) == successor[0]
    artifacts = await repository.artifacts(transfer.id)
    assert len(artifacts) == len(FILES)                            # one graph, rebuilt in place
    assert {candidate.provider_id for artifact in artifacts for candidate in artifact.candidates} == {successor[0]}
    assert len([artifact for artifact in artifacts if artifact.execution]) == len(
        {artifact.execution.attempt_id for artifact in artifacts if artifact.execution})
    assert await status(transfer.id) != "error" and await terminal_count(transfer.id) == 0


# -- 15.2: a request-global failure after equivalent route work stays terminal and never fails over ---------------

async def test_a_request_global_failure_on_an_operator_chosen_route_never_fails_over(tmp_path, monkeypatch):
    unsafe = TransferError(NormalizedError(Domain.SECURITY, Category.PATH_POLICY_VIOLATION, Stage.CANDIDATE_PREPARATION))
    refuser = refusing(magnet_provider("parcel-b"), unsafe)
    alternate = magnet_provider("parcel-c")
    repository, engine, _executor, first, transfer = await productive_then_switched(
        tmp_path, monkeypatch, refuser, alternate)
    offer(first, native="again")
    offer(alternate)
    root = await root_of(repository, transfer.id)
    await tick(engine, times=2)
    await tick(engine, HOLD, times=2)
    await tick(engine, times=4)

    assert ("manifest", "parcel-b:x") in refuser.calls
    assert ("resolve", MAGNET) not in alternate.calls and first.calls.count(("resolve", MAGNET)) == 1
    assert await repository.exhausted_route_providers(root.id) == frozenset()
    assert (await root_of(repository, transfer.id)).state == "failed"
    failures = await rows("SELECT COUNT(*) AS n FROM transfer_outcomes WHERE transfer_id=? AND payload LIKE ?",
                          (transfer.id, '%path_policy_violation%'))
    assert failures[0]["n"] == 1                                   # one terminal outcome


# -- 15.3: a transient provider failure backs off that provider only --------------------------------------------

async def test_a_transient_failure_on_an_operator_chosen_route_moves_on_now_and_backs_off_only_that_provider(
        tmp_path, monkeypatch):
    flaky = magnet_provider("parcel-d")
    repository, engine, _executor, first, transfer = await productive_then_switched(
        tmp_path, monkeypatch, flaky, offered=False)
    flaky.responses.extend([TransferError(replace(PROTOCOL, integration_id="parcel-d"))] * 3)
    offer(first, native="again")
    root = await root_of(repository, transfer.id)
    failed_at = engine.clock.now + 1

    await tick(engine)                                             # parcel-d fails here
    await tick(engine, times=2)                                    # and parcel-a is routed at once

    assert flaky.calls.count(("resolve", MAGNET)) == 1
    reentry = await rows("""SELECT reentry_at,error FROM resolution_attempts WHERE request_id=? AND provider_id='parcel-d'""",
                         (root.id,))
    assert reentry[0]["reentry_at"] == pytest.approx(failed_at + REENTRY)
    assert '"category":"provider_protocol_violation"' in reentry[0]["error"]
    history = await routes(root.id)
    assert history[-2][:3] == ("parcel-d", "exhausted", "operator_switch")
    assert history[-1][:5] == ("parcel-a", "succeeded", "resolve", "provider_change", "provider_protocol_violation")
    assert engine.clock.now - failed_at < 5                        # never waited for parcel-d
    assert (await root_of(repository, transfer.id)).retry_at < failed_at + REENTRY   # never the provider's re-entry
    assert choices(await manual_route_switch.route_providers(engine, transfer.id))["parcel-d"] == (
        "failed_earlier", True)

    await tick(engine, 10, times=5)                                # before its re-entry: never asked again
    assert flaky.calls.count(("resolve", MAGNET)) == 1


# -- 15.4 / 15.5: the root waits only when nothing can run, and every provider's retries are bounded ---------------

async def test_the_only_provider_cooling_down_is_retried_after_its_interval_within_its_budget(tmp_path, monkeypatch):
    only = magnet_provider("parcel-a")
    repository, engine, _executor = await lab(tmp_path, monkeypatch, only)
    only.responses.extend([TransferError(replace(PROTOCOL, integration_id="parcel-a"))] * 5)
    transfer = await submit(engine)
    root = await root_of(repository, transfer.id)
    calls = lambda: only.calls.count(("resolve", MAGNET))  # noqa: E731

    await tick(engine)
    assert calls() == 1
    first_failure = engine.clock.now
    record = await root_of(repository, transfer.id)
    assert record.state == "pending" and record.retry_at == pytest.approx(first_failure + REENTRY)  # parked
    await tick(engine, 10, times=5)                                # 50 s: no hot loop, no early retry
    assert calls() == 1 and await status(transfer.id) != "error"
    await tick(engine, 11)                                         # at its re-entry: ordinary routing retries
    assert calls() == 2
    await tick(engine, REENTRY + 1)
    assert calls() == 3                                            # the budget (3) is spent
    await tick(engine, REENTRY + 1, times=3)
    assert calls() == 3
    assert await repository.exhausted_route_providers(root.id, engine.clock.now) == frozenset({"parcel-a"})
    assert await status(transfer.id) == "error" and await terminal_count(transfer.id) == 1


async def test_a_permanent_and_a_transient_failure_wait_for_the_transient_provider_then_end_once(
        tmp_path, monkeypatch):
    refuser = refusing(magnet_provider("parcel-a"), native_refusal(35, "infringing_file"))
    flaky = magnet_provider("parcel-b")
    disabled = magnet_provider("parcel-c")
    disabled.descriptor = replace(disabled.descriptor, enabled=False)
    repository, engine, _executor = await lab(tmp_path, monkeypatch, refuser, flaky, disabled)
    offer(refuser)
    flaky.responses.extend([TransferError(replace(PROTOCOL, integration_id="parcel-b"))] * 5)
    transfer = await submit(engine)
    root = await root_of(repository, transfer.id)
    await tick(engine, times=2)
    await tick(engine, HOLD, times=2)
    await tick(engine, times=3)

    assert ("resolve", MAGNET) not in disabled.calls
    permanent = await repository.exhausted_route_providers(root.id, math.inf)
    assert "parcel-a" in permanent and "parcel-b" not in permanent
    record = await root_of(repository, transfer.id)
    assert record.state == "pending" and await status(transfer.id) != "error"   # waits for parcel-b
    await tick(engine, REENTRY + 1, times=4)
    assert flaky.calls.count(("resolve", MAGNET)) == 3
    assert await repository.exhausted_route_providers(root.id, math.inf) == frozenset({"parcel-a", "parcel-b"})
    assert await status(transfer.id) == "error" and await terminal_count(transfer.id) == 1


async def test_transient_failures_on_two_providers_never_alternate_forever(tmp_path, monkeypatch):
    first, second = magnet_provider("parcel-a"), magnet_provider("parcel-b")
    repository, engine, _executor = await lab(tmp_path, monkeypatch, first, second)
    for provider in (first, second):
        provider.responses.extend([TransferError(replace(PROTOCOL, integration_id=provider.descriptor.id))] * 9)
    transfer = await submit(engine)

    await tick(engine, 31, times=20)                               # ten minutes of ordinary cadence

    assert first.calls.count(("resolve", MAGNET)) == 3             # each spends only its own budget
    assert second.calls.count(("resolve", MAGNET)) == 3
    assert await status(transfer.id) == "error" and await terminal_count(transfer.id) == 1


# -- 15.4A: a re-entry never preempts the healthy current route ----------------------------------------------------

async def test_a_provider_s_reentry_never_preempts_the_healthy_current_route(tmp_path, monkeypatch):
    flaky, healthy = magnet_provider("parcel-a"), magnet_provider("parcel-b")
    repository, engine, executor = await lab(tmp_path, monkeypatch, flaky, healthy)
    flaky.responses.append(TransferError(replace(PROTOCOL, integration_id="parcel-a")))
    offer(healthy)
    transfer = await submit(engine)
    root = await root_of(repository, transfer.id)
    await tick(engine, times=2)
    await tick(engine, HOLD, times=2)
    assert await repository.bound_route_provider(root.id) == "parcel-b"
    assert any(artifact.execution for artifact in await repository.artifacts(transfer.id))
    before = (await routes(root.id), sorted(executor.jobs), flaky.calls.count(("resolve", MAGNET)))

    await tick(engine, 31, times=12)                               # parcel-a's re-entry passes

    assert await repository.bound_route_provider(root.id) == "parcel-b"
    assert await routes(root.id) == before[0]                      # no route attempt opened
    assert flaky.calls.count(("resolve", MAGNET)) == before[2]
    assert not [call for call in executor.calls if call[0] == "cancel"]   # no writer retired


# -- 15.6 / 15.7 / 15.8 / 15.9: the picker projects the same durable failure truth --------------------------------

async def test_a_failed_backup_is_failed_earlier_never_available(tmp_path, monkeypatch):
    first, second = magnet_provider("parcel-a"), magnet_provider("parcel-b")
    repository, engine, _executor = await lab(tmp_path, monkeypatch, first, second)
    offer(first)
    transfer = await submit(engine)
    await tick(engine, times=2)
    root = await root_of(repository, transfer.id)
    standby_id, _attempts = await repository.begin_standby(transfer.id, root.id, "parcel-b", engine.clock())
    await repository.fail_standby(standby_id, replace(PROTOCOL, integration_id="parcel-b"), engine.clock())
    assert choices(await manual_route_switch.route_providers(engine, transfer.id))["parcel-b"] == (
        "failed_earlier", True)


async def test_a_promoted_route_that_fails_is_failed_earlier_and_a_cleanly_left_provider_stays_available(
        tmp_path, monkeypatch):
    refuser = refusing(magnet_provider("parcel-b"), native_refusal(35, "infringing_file"))
    alternate = magnet_provider("parcel-c")
    first = magnet_provider("parcel-a")
    repository, engine, _executor = await lab(tmp_path, monkeypatch, first, refuser, alternate)
    offer(first)
    transfer = await submit(engine)
    await tick(engine, times=2)
    await tick(engine, HOLD, times=2)
    root = await root_of(repository, transfer.id)
    prepared = offer(refuser, native="backup")
    refuser.responses.clear()                                      # promotion only: never a cold resolve
    standby_id, _attempts = await repository.begin_standby(transfer.id, root.id, "parcel-b", engine.clock())
    await repository.bind_standby(standby_id, transfer.id, prepared, ResourceState.AVAILABLE, engine.clock())
    offer(alternate)

    await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    await tick(engine, times=2)
    await tick(engine, HOLD, times=2)
    await tick(engine, times=3)

    assert [item["id"] for item in await repository.standbys(transfer.id) if item.get("promoted_at")] == [standby_id]
    assert "parcel-b" in await repository.exhausted_route_providers(root.id)
    current = await repository.bound_route_provider(root.id)
    picked = choices(await manual_route_switch.route_providers(engine, transfer.id))
    assert picked["parcel-b"] == ("failed_earlier", True)          # not Available after losing the route
    assert picked[current][0] == "current"
    if current != "parcel-a":
        assert picked["parcel-a"] == ("available", True)           # left cleanly: never marked failed


async def test_a_cleanly_released_provider_returns_to_available(tmp_path, monkeypatch):
    other = magnet_provider("parcel-c")
    repository, engine, _executor, _first, transfer = await productive_then_switched(tmp_path, monkeypatch, other)
    await tick(engine, times=2)
    await tick(engine, HOLD, times=2)
    picked = choices(await manual_route_switch.route_providers(engine, transfer.id))
    assert picked["parcel-c"][0] == "current" and picked["parcel-a"] == ("available", True)


async def test_an_explicit_retry_of_a_failed_provider_is_one_ordinary_admitted_switch(tmp_path, monkeypatch):
    flaky = magnet_provider("parcel-d")
    repository, engine, _executor, first, transfer = await productive_then_switched(
        tmp_path, monkeypatch, flaky, offered=False)
    flaky.responses.append(TransferError(replace(PROTOCOL, integration_id="parcel-d")))
    offer(first, native="again")
    await tick(engine, times=3)
    root = await root_of(repository, transfer.id)
    assert choices(await manual_route_switch.route_providers(engine, transfer.id))["parcel-d"] == (
        "failed_earlier", True)

    admitted = []
    make_room = engine._make_primary_room

    async def counted(record, provider):
        admitted.append(provider.descriptor.id)
        return await make_room(record, provider)

    monkeypatch.setattr(engine, "_make_primary_room", counted)
    offer(flaky, native="retried")
    await switch_root_provider(engine, transfer.id, "parcel-d", expected_provider_id="parcel-a")   # inside cooldown

    assert admitted == ["parcel-d"]                                # the ordinary preflight and admission
    switches = [item for item in await routes(root.id) if item[0] == "parcel-d"]
    assert [item[1:3] for item in switches] == [("released", "operator_switch"), ("started", "operator_switch")]
    assert await repository.bound_route_provider(root.id) == "parcel-d"


# -- the re-entry interval is the provider's, never the root's resolution retry ---------------------------------------

async def test_a_source_scoped_transient_still_retries_its_route_after_the_resolution_retry_delay(
        tmp_path, monkeypatch):
    only = magnet_provider("parcel-a")
    repository, engine, _executor = await lab(tmp_path, monkeypatch, only)
    source = NormalizedError(Domain.RESOLUTION, Category.SOURCE_TEMPORARILY_UNAVAILABLE, Stage.RESOLUTION,
                             Retryability.BACKOFF, origin=Origin.REMOTE_SOURCE)
    only.responses.append(TransferError(source))
    transfer = await submit(engine)
    await tick(engine)
    record = await root_of(repository, transfer.id)
    assert engine.policy.resolution_retry_delay == 300.0
    assert record.retry_at == pytest.approx(engine.clock.now + 300.0)      # unchanged: the route's own retry
    assert await repository.bound_route_provider(record.id) == "parcel-a"
    assert await repository.provider_reentries(record.id) == {}


async def test_a_provider_mandated_wait_longer_than_the_interval_is_honoured(tmp_path, monkeypatch):
    only = magnet_provider("parcel-a")
    repository, engine, _executor = await lab(tmp_path, monkeypatch, only)
    only.responses.append(TransferError(replace(PROTOCOL, integration_id="parcel-a", retry_after_seconds=120)))
    transfer = await submit(engine)
    await tick(engine)
    record = await root_of(repository, transfer.id)
    assert (await repository.provider_reentries(record.id))["parcel-a"][1] == pytest.approx(engine.clock.now + 120)


# -- a provider's re-entry survives a restart, and only a new campaign resets its budget -----------------------------

async def test_a_provider_s_reentry_and_budget_survive_a_restart(tmp_path, monkeypatch):
    only = magnet_provider("parcel-a")
    repository, engine, _executor = await lab(tmp_path, monkeypatch, only)
    only.responses.extend([TransferError(replace(PROTOCOL, integration_id="parcel-a"))] * 4)
    transfer = await submit(engine)
    calls = lambda: only.calls.count(("resolve", MAGNET))  # noqa: E731
    await tick(engine)
    root = await root_of(repository, transfer.id)
    deadline = engine.clock.now + REENTRY
    assert (await repository.provider_reentries(root.id))["parcel-a"] == (1, pytest.approx(deadline))

    # Restart before the deadline: a fresh repository and engine over the same state.
    restarted_repository = TransferRepository()
    restarted = TransferEngine(restarted_repository, engine.registry, download_root=engine.root,
                               policy=engine.policy, clock=engine.clock)
    await restarted.initialize()
    await tick(restarted, 20, times=2)                             # 40 s after the failure
    assert calls() == 1                                            # still excluded: no premature attempt
    assert await restarted_repository.exhausted_route_providers(root.id, restarted.clock.now) == {"parcel-a"}
    restarted.clock.now = deadline
    await restarted.tick()                                         # at the original deadline: ordinary routing
    assert calls() == 2
    assert (await restarted_repository.provider_reentries(root.id))["parcel-a"][0] == 2   # budget not reset
    await tick(restarted, REENTRY + 1, times=3)
    assert calls() == 3                                            # the third failure spends the budget
    assert await status(transfer.id) == "error" and await terminal_count(transfer.id) == 1

    # The operator's new campaign releases the spent routes and grants a fresh budget.
    only.responses.clear()
    offer(only, native="campaign")
    assert await restarted.retry(transfer.id)
    assert await restarted_repository.provider_reentries(root.id) == {}
    await tick(restarted, times=2)
    assert calls() == 4 and await repository.bound_route_provider(root.id) == "parcel-a"


# -- an exhausted latest route owns nothing, even under a collection route binding ----------------------------------

async def bound_to_collection(repository, transfer_id, provider_id):
    """The earlier single-owner collection route binding, as a database from
    before collection route authority still carries it (no current code
    writes it)."""
    async with database.get_db() as db:
        await db.execute("UPDATE torrents SET collection_route_provider_id=? WHERE id=?", (provider_id, transfer_id))
        await db.commit()


@pytest.mark.parametrize("alternate", [False, True], ids=["only-provider", "alternate-takes-over"])
async def test_a_collection_binding_never_revives_a_route_that_ended_on_a_transient_failure(
        tmp_path, monkeypatch, alternate):
    first = magnet_provider("parcel-a")
    others = (magnet_provider("parcel-b"),) if alternate else ()
    repository, engine, _executor = await lab(tmp_path, monkeypatch, first, *others)
    first.responses.append(TransferError(replace(PROTOCOL, integration_id="parcel-a")))
    for other in others:
        offer(other)
    transfer = await submit(engine)
    await bound_to_collection(repository, transfer.id, "parcel-a")
    root = await root_of(repository, transfer.id)
    calls = lambda: first.calls.count(("resolve", MAGNET))  # noqa: E731

    await tick(engine)                                             # parcel-a owns the route, and fails
    failed_at = engine.clock.now
    assert calls() == 1
    a_routes = [item for item in await routes(root.id) if item[0] == "parcel-a"]
    assert [item[1] for item in a_routes] == ["exhausted"]
    if alternate:
        await tick(engine, times=2)
        assert await repository.bound_route_provider(root.id) == "parcel-b"   # takes over during the cooldown
        assert ("resolve", MAGNET) in others[0].calls
    else:
        assert await repository.bound_route_provider(root.id) is None        # owns nothing; never revived
    await tick(engine, 10, times=5)                                # before the re-entry
    assert calls() == 1                                            # no new parcel-a attempt
    assert len([item for item in await routes(root.id) if item[0] == "parcel-a"]) == 1
    if alternate:
        return

    offer(first, native="reentered")
    engine.clock.now = failed_at + REENTRY
    await engine.tick()                                            # eligible again: ordinary competition
    assert calls() == 2
    a_routes = [item for item in await routes(root.id) if item[0] == "parcel-a"]
    assert [item[1] for item in a_routes][0] == "exhausted" and a_routes[-1][1] != "exhausted"
    assert await repository.bound_route_provider(root.id) == "parcel-a"      # the NEW live attempt owns it


# -- transfer 536: an AVAILABLE resource whose provider cannot produce its executable manifest ---------------------

# Transfer 536's normalized facts, with no provider's text: a provider protocol
# violation met while the provider prepares the executable manifest.
MANIFEST_PROTOCOL = NormalizedError(Domain.PROVIDER, Category.PROVIDER_PROTOCOL_VIOLATION, Stage.CANDIDATE_PREPARATION,
                                    Retryability.UNKNOWN, origin=Origin.PROVIDER, permanence=Permanence.UNKNOWN)


def manifest_timeout() -> TransferError:
    """A provider API read timeout met inside ``manifest()``, normalized by a
    provider adapter's own translation owner, unchanged but for whose it is."""
    return TransferError(replace(translate_error(asyncio.TimeoutError(), stage=Stage.CANDIDATE_PREPARATION),
                                 integration_id="parcel-b"))


async def test_the_536_failure_shape_is_the_provider_s_own_and_transient():
    assert provider_attributable(MANIFEST_PROTOCOL)
    assert (MANIFEST_PROTOCOL.retryability, MANIFEST_PROTOCOL.permanence) == (Retryability.UNKNOWN, Permanence.UNKNOWN)
    decision = TransferPolicy(**LIVE_POLICY).retry_resolution(MANIFEST_PROTOCOL, 1, 0.0)
    assert decision.action == Recovery.RETRY and decision.retry_at is not None   # transient: re-entry, not exclusion
    timeout = manifest_timeout().error
    assert (timeout.domain, timeout.origin, timeout.stage) == (Domain.NETWORK, Origin.PROVIDER,
                                                              Stage.CANDIDATE_PREPARATION)
    assert provider_attributable(timeout)


@pytest.mark.parametrize("failure", [
    lambda: TransferError(replace(MANIFEST_PROTOCOL, integration_id="parcel-b")),
    manifest_timeout,
], ids=["provider-protocol-violation", "provider-transport-timeout"])
async def test_an_available_resource_whose_manifest_fails_at_its_provider_ends_that_route_and_continues(
        tmp_path, monkeypatch, failure):
    refuser = refusing(magnet_provider("parcel-b"), failure())
    alternate = magnet_provider("parcel-c")
    repository, engine, _executor, first, transfer = await productive_then_switched(
        tmp_path, monkeypatch, refuser, alternate)
    offer(first, native="again")
    offer(alternate)
    root = await root_of(repository, transfer.id)

    await tick(engine)                                             # parcel-b: AVAILABLE, then its manifest fails
    failed_at = engine.clock.now
    await tick(engine, times=3)

    assert refuser.calls.count(("manifest", "parcel-b:x")) == 1    # once: no same-resource manifest retry
    assert "parcel-b" in await repository.exhausted_route_providers(root.id)
    assert "parcel-b" not in await repository.exhausted_route_providers(root.id, math.inf)   # never blacklisted
    reentry = await rows("SELECT state,reentry_at FROM resolution_attempts WHERE request_id=? AND provider_id='parcel-b'",
                         (root.id,))
    assert [(row["state"], row["reentry_at"]) for row in reentry] == [("exhausted", pytest.approx(failed_at + REENTRY))]
    history = await routes(root.id)
    failed = [item for item in history if item[0] == "parcel-b"]
    successor = history[history.index(failed[0]) + 1]
    assert successor[2:4] == ("resolve", "provider_change")        # automatic, never operator
    assert successor[0] in {"parcel-a", "parcel-c"}
    assert await repository.bound_route_provider(root.id) == successor[0]
    assert engine.clock.now - failed_at < 5                        # never parked on parcel-b's retry
    assert (await root_of(repository, transfer.id)).resource.provider_id == successor[0]
    assert await status(transfer.id) != "error" and await terminal_count(transfer.id) == 0
