"""Switching context integrity (transfer 1084).

A canonical binding is one source's participation through one provider; the
candidate it names is that participation's current, provider-issued
realization. When a link's provider is switched back to one that served it
before (A -> B -> A), the provider issues the source a NEW candidate, which
lands on that provider's existing binding as an origin -- and the binding must
then name it (``CanonicalOwnership._name_current_candidates``), or every reader
that asks a binding which candidate it is (Details and both selectors, the
host switch, ``origin_for`` and so activation, refresh and automatic failover)
is handed a candidate the file no longer has.

Every fixture provider here names its upstream host (``SourceIdentity("host",
...)``), as every real provider does: a provider that names none keys its
binding by candidate id, which never meets this path.

Neutral fixtures: part01 is reachable from ddownload (prov-rd) and 1fichier
(prov-ad, and also claimed by prov-z and prov-w).
"""
from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlsplit

import pytest
import test_v113_transient_bound_candidate as transient
from test_v113_multi_root_provider_switch import file_of, roots_by_link, rows, settle
from test_v113_root_provider_switch import application_of
from test_v113_transient_bound_candidate import DDL1, FICHIER01, primary_lab, running

from application.manual_route_switch import switch_transfer_provider
from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage, TransferError
from transfers.manual_failover import manual_candidate_failover, preview_candidate_switch
from transfers.manual_route_switch import switch_root_provider, transfer_route_status
from transfers.models import ExecutionState, SourceIdentity

pytestmark = pytest.mark.asyncio


class HostProvider(transient.ClaimingProvider):
    """A neutral direct-link provider whose candidates name their upstream
    host and can be refreshed; with ``lifetime`` set, its links expire."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.clock, self.lifetime = None, None

    async def resolve(self, request):
        result = await super().resolve(request)
        host = urlsplit(str(request.payload)).hostname
        expires = None if self.lifetime is None else self.clock() + self.lifetime
        return replace(result, candidates=tuple(
            replace(item, source_identity=SourceIdentity("host", host), refresh_request=request, expires_at=expires)
            for item in result.candidates))

    async def refresh(self, candidate):
        return await self.resolve(candidate.refresh_request)


async def lab(tmp_path, monkeypatch):
    monkeypatch.setattr(transient, "ClaimingProvider", HostProvider)
    lab_ = await primary_lab(tmp_path, monkeypatch, (DDL1, FICHIER01), extra=(
        HostProvider("prov-z", ("1fichier.com",), priority=0), HostProvider("prov-w", ("1fichier.com",), priority=0)))
    lab_.roots = await roots_by_link(lab_.repository, lab_.transfer.id)
    now = await part(lab_)
    assert now.candidates[now.selected].provider_id == "prov-rd" and now.state == "downloading"
    return lab_


async def part(lab_):
    return await file_of(lab_.repository, lab_.transfer.id, "part01.rar")


def on(artifact, provider_id):
    return next(item for item in artifact.candidates if item.provider_id == provider_id)


def host_of(artifact):
    return artifact.candidates[artifact.selected].source_identity.key


async def bindings(artifact_id):
    return [dict(row) for row in await rows(
        "SELECT id,candidate_id,provider_id,source_key,candidate_order FROM canonical_candidate_bindings "
        "WHERE canonical_artifact_id=? ORDER BY candidate_order,id", (artifact_id,))]


async def discovered(binding_id):
    return [row["discovered_candidate_id"] for row in await rows(
        "SELECT discovered_candidate_id FROM canonical_candidate_origins WHERE binding_id=? ORDER BY id",
        (binding_id,))]


async def offered(lab_):
    now = await part(lab_)
    return now, (await lab_.repository._candidate_presentation(lab_.transfer.id))[now.id]


async def onto(lab_, host):
    """The operator moves the file to ``host`` by the identity the host selector offers."""
    now, projected = await offered(lab_)
    if host_of(now) == host:
        return now
    target = next(item for item in projected["source_candidates"] if item["source_host"] == host)
    assert target["switch_eligible"]
    await manual_candidate_failover(lab_.engine, lab_.transfer.id, now.id, target["candidate_id"])
    await settle(lab_.engine, 6)
    moved = await part(lab_)
    assert host_of(moved) == host and moved.state == "downloading"
    assert running(lab_) == [moved.execution.attempt_id]
    return moved


async def relink(lab_, provider_id, expected):
    """The operator switches the provider of the 1fichier link the file downloads from."""
    now = await part(lab_)
    assert host_of(now) == "1fichier.com"
    await switch_root_provider(lab_.engine, lab_.transfer.id, provider_id, expected_provider_id=expected,
                               artifact_id=now.id, expected_root_id=lab_.roots[FICHIER01])
    await settle(lab_.engine, 12)
    moved = await part(lab_)
    assert moved.candidates[moved.selected].provider_id == provider_id and moved.state == "downloading"
    assert running(lab_) == [moved.execution.attempt_id]
    return moved


async def assert_context_intact(lab_):
    """Every current binding names a candidate the file has; every candidate
    is route-owned; Details offers exactly the current candidates, one of them
    selected; both hosts and every provider of the 1fichier link stay offered."""
    now = await part(lab_)
    current = {str(item.id) for item in now.candidates}
    named = [item for item in await bindings(now.id) if item["candidate_order"] <= 100000]
    assert {item["candidate_id"] for item in named} == current
    assert sorted(item["candidate_order"] for item in named) == list(range(1, len(current) + 1))
    for item in now.candidates:
        origin = await lab_.engine.canonical.origin_for(now, item)
        assert origin is not None and origin.provider_id == item.provider_id
    _, projected = await offered(lab_)
    assert {item["candidate_id"] for item in projected["acquisition_candidates"]} == current
    assert [item["candidate_id"] for item in projected["acquisition_candidates"] if item["is_selected"]] == [
        str(now.candidates[now.selected].id)]
    assert {item["source_host"] for item in projected["source_candidates"]} == {"ddownload.com", "1fichier.com"}
    status = {item["provider_id"] for item in (await transfer_route_status(lab_.engine, lab_.transfer.id))["providers"]}
    assert {"prov-ad", "prov-z", "prov-w"} <= status
    return now


async def switched_back(lab_):
    """A -> B -> A on the 1fichier link: A1, then A2."""
    first = str(on(await onto(lab_, "1fichier.com"), "prov-ad").id)
    await relink(lab_, "prov-z", "prov-ad")
    now = await relink(lab_, "prov-ad", "prov-z")
    return first, str(on(now, "prov-ad").id)


async def test_a_link_switched_back_to_its_provider_names_the_new_candidate(tmp_path, monkeypatch):
    """prov-ad's binding names its new candidate A2 -- A1 stays its history --
    and the source stays selectable from either host, any number of times."""
    lab_ = await lab(tmp_path, monkeypatch)
    first, second = await switched_back(lab_)
    assert second != first
    (binding,) = [item for item in await bindings((await part(lab_)).id) if item["provider_id"] == "prov-ad"]
    assert binding["candidate_id"] == second and binding["candidate_order"] <= 100000
    history = await discovered(binding["id"])
    assert first in history and second in history and history.index(first) < history.index(second)
    await assert_context_intact(lab_)
    for host in ("ddownload.com", "1fichier.com", "ddownload.com", "1fichier.com"):
        await onto(lab_, host)
        await assert_context_intact(lab_)


async def test_automatic_failover_and_refresh_recognize_the_returned_candidate(tmp_path, monkeypatch):
    """A2 is route-owned: the failover gate admits it (here with no attempt
    history, the one other filter), and when its link has expired the switch
    refreshes it through its issuing link and lands on the refreshed copy."""
    lab_ = await lab(tmp_path, monkeypatch)
    _first, second = await switched_back(lab_)
    now = await onto(lab_, "ddownload.com")

    async def no_history(artifact_id):
        return {"candidate_attempt_history": []}

    recorded = lab_.repository.recovery_context
    monkeypatch.setattr(lab_.repository, "recovery_context", no_history)
    assert await lab_.engine._next_alternate_index(now) == next(
        index for index, item in enumerate(now.candidates) if str(item.id) == second)
    monkeypatch.setattr(lab_.repository, "recovery_context", recorded)

    # The link expires; switching to it refreshes it through the request that issued it.
    ad = lab_.providers["prov-ad"]
    ad.clock, ad.lifetime = lab_.engine.clock, 10_000.0
    from db.database import get_db
    async with get_db() as db:
        candidates = [dict(item) for item in __import__("json").loads(
            (await db.fetchone("SELECT candidates FROM download_files WHERE id=?", (now.id,)))["candidates"])]
        for item in candidates:
            if item["id"] == second:
                item["expires_at"] = lab_.engine.clock() - 1
        await db.execute("UPDATE download_files SET candidates=? WHERE id=?",
                         (__import__("json").dumps(candidates), now.id))
        await db.commit()
    resolved_before = list(ad.resolved)
    moved = await onto(lab_, "1fichier.com")
    assert ad.resolved[len(resolved_before):] == [FICHIER01]                # refreshed through its own link
    third = str(moved.candidates[moved.selected].id)
    assert moved.candidates[moved.selected].provider_id == "prov-ad" and third != second
    (binding,) = [item for item in await bindings(moved.id) if item["provider_id"] == "prov-ad"]
    assert binding["candidate_id"] == third
    await assert_context_intact(lab_)


async def test_the_provider_picker_moves_the_file_onto_a_provider_it_was_switched_back_to(tmp_path, monkeypatch):
    lab_ = await lab(tmp_path, monkeypatch)
    _first, second = await switched_back(lab_)
    await onto(lab_, "ddownload.com")
    item = {entry["provider_id"]: entry for entry in
            (await transfer_route_status(lab_.engine, lab_.transfer.id))["providers"]}["prov-ad"]
    expected = {row["root_id"]: row["current_provider_id"] for row in item["roots"] if row["status"] == "eligible"}
    assert expected, item
    result = await switch_transfer_provider(application_of(lab_.engine), lab_.transfer.id, "prov-ad",
                                            expected_routes=expected)
    assert result["result"] != "unchanged", result
    await settle(lab_.engine, 8)
    now = await part(lab_)
    assert str(now.candidates[now.selected].id) == second and running(lab_) == [now.execution.attempt_id]


async def test_repeated_switching_across_three_providers_and_both_hosts_never_erodes_the_source_set(
        tmp_path, monkeypatch):
    lab_ = await lab(tmp_path, monkeypatch)
    current = "prov-ad"
    for provider_id in ("prov-z", "prov-w", "prov-ad", "prov-z", "prov-ad", "prov-w", "prov-ad"):
        await onto(lab_, "1fichier.com")
        await relink(lab_, provider_id, current)
        current = provider_id
        now = await assert_context_intact(lab_)
        assert {item.provider_id for item in now.candidates} == {"prov-rd", current}
        await onto(lab_, "ddownload.com")
        now = await assert_context_intact(lab_)
        history = [item for item in await bindings(now.id) if item["candidate_order"] > 100000]
        assert not {item["candidate_id"] for item in history} & {str(item.id) for item in now.candidates}


async def restart(lab_):
    lab_.engine.canonical._initialized = False
    await lab_.engine.canonical.initialize()


async def stale(lab_, first):
    """The state an earlier version persisted: prov-ad's current binding names A1."""
    now = await part(lab_)
    (binding,) = [item for item in await bindings(now.id) if item["provider_id"] == "prov-ad"]
    from db.database import get_db
    async with get_db() as db:
        await db.execute("UPDATE canonical_candidate_bindings SET candidate_id=? WHERE id=?", (first, binding["id"]))
        await db.commit()
    return now, binding


async def test_startup_repairs_a_binding_left_naming_a_replaced_candidate(tmp_path, monkeypatch):
    """The canonical owner's startup formalization names A2 -- only on the
    binding's own origin's proof, idempotently, keeping every origin -- and
    a later startup never undoes a newer switch."""
    lab_ = await lab(tmp_path, monkeypatch)
    first, second = await switched_back(lab_)
    await onto(lab_, "ddownload.com")
    now, binding = await stale(lab_, first)
    origins = await rows("SELECT * FROM canonical_candidate_origins ORDER BY id", ())
    _, projected = await offered(lab_)
    assert first in {item["candidate_id"] for item in projected["acquisition_candidates"]}   # what was offered
    assert await lab_.engine.canonical.origin_for(now, on(now, "prov-ad")) is None

    await restart(lab_)
    (repaired,) = [item for item in await bindings(now.id) if item["provider_id"] == "prov-ad"]
    assert repaired["candidate_id"] == second and repaired["candidate_order"] == binding["candidate_order"]
    assert await rows("SELECT * FROM canonical_candidate_origins ORDER BY id", ()) == origins
    await assert_context_intact(lab_)
    before = await bindings(now.id)
    await restart(lab_)
    assert await bindings(now.id) == before                               # idempotent

    await onto(lab_, "1fichier.com")
    after = await relink(lab_, "prov-w", "prov-ad")
    await restart(lab_)
    assert {item["candidate_id"] for item in await bindings(after.id) if item["candidate_order"] <= 100000} == {
        str(item.id) for item in after.candidates}
    await assert_context_intact(lab_)


async def test_startup_leaves_a_binding_alone_without_its_own_origins_proof(tmp_path, monkeypatch):
    lab_ = await lab(tmp_path, monkeypatch)
    first, second = await switched_back(lab_)
    await onto(lab_, "ddownload.com")
    now, binding = await stale(lab_, first)
    from db.database import get_db
    async with get_db() as db:
        await db.execute("DELETE FROM canonical_candidate_origins WHERE binding_id=? AND discovered_candidate_id=?",
                         (binding["id"], second))
        await db.commit()
    await restart(lab_)
    (left,) = [item for item in await bindings(now.id) if item["provider_id"] == "prov-ad"]
    assert left["candidate_id"] == first


def fail_with_retry_after(lab_, attempt, seconds=60):
    """The writer fails transiently, its source asking to be retried later
    (an HTTP 503 with Retry-After)."""
    error = NormalizedError(Domain.EXECUTOR, Category.SOURCE_TEMPORARILY_UNAVAILABLE, Stage.EXECUTION,
                            Retryability.BACKOFF, origin=Origin.EXECUTOR, integration_id="memory",
                            retry_after_seconds=seconds)
    lab_.executor.jobs[attempt] = replace(lab_.executor.jobs[attempt], state=ExecutionState.FAILED, error=error)


async def still(engine, ticks):
    """Scheduler passes with the clock standing still."""
    for _ in range(ticks):
        await engine.tick()


async def backoff(lab_, seconds=60):
    """The file's prov-rd writer fails transiently: it waits out a timed retry of that route."""
    fail_with_retry_after(lab_, (await part(lab_)).execution.attempt_id, seconds)
    await still(lab_.engine, 4)
    now = await part(lab_)
    context = await lab_.repository.recovery_context(now.id)
    assert (now.state, context["quiescence_reason"]) == ("recovery_wait", "retry_backoff") and not running(lab_)
    assert now.retry_at > lab_.engine.clock()
    return now


async def test_another_route_may_be_chosen_while_the_failed_route_waits_out_its_retry(tmp_path, monkeypatch):
    lab_ = await lab(tmp_path, monkeypatch)
    now = await backoff(lab_)
    retry_at = now.retry_at
    _, projected = await offered(lab_)
    target = next(item for item in projected["acquisition_candidates"] if item["provider_id"] == "prov-ad")
    assert target["switch_eligible"]
    assert all(item["switch_eligible"] for item in projected["source_candidates"] if not item["is_selected"])
    assert (await preview_candidate_switch(lab_.engine, lab_.transfer.id, now.id, target["candidate_id"]))[
        "candidate_id"] == target["candidate_id"]
    await manual_candidate_failover(lab_.engine, lab_.transfer.id, now.id, target["candidate_id"])
    await still(lab_.engine, 4)                                           # the clock never moves
    moved = await part(lab_)
    assert lab_.engine.clock() < retry_at
    assert moved.state == "downloading" and moved.candidates[moved.selected].provider_id == "prov-ad"
    assert running(lab_) == [moved.execution.attempt_id]
    assert (await lab_.repository.recovery_context(moved.id))["quiescence_reason"] is None
    # The failed route's timer expiring never preempts the healthy route chosen meanwhile.
    lab_.engine.clock.now = retry_at + 1
    await settle(lab_.engine, 4)
    after = await part(lab_)
    assert after.execution.attempt_id == moved.execution.attempt_id and running(lab_) == [moved.execution.attempt_id]


async def test_without_a_choice_the_retry_waits_for_its_timer_and_retries_the_same_route(tmp_path, monkeypatch):
    lab_ = await lab(tmp_path, monkeypatch)
    now = await backoff(lab_)
    await still(lab_.engine, 4)
    assert (await part(lab_)).state == "recovery_wait" and not running(lab_)
    lab_.engine.clock.now = now.retry_at + 1
    await settle(lab_.engine, 6)
    retried = await part(lab_)
    assert retried.state == "downloading" and retried.candidates[retried.selected].provider_id == "prov-rd"
    assert running(lab_) == [retried.execution.attempt_id]


@pytest.mark.parametrize("reason", ["storage_unavailable", "executor_unavailable", "provider_disabled"])
async def test_a_wait_that_is_not_a_route_retry_stays_unswitchable(tmp_path, monkeypatch, reason):
    lab_ = await lab(tmp_path, monkeypatch)
    now = await backoff(lab_)
    assert await lab_.repository.transition_recovery(now.id, "recovery_wait", retry_at=now.retry_at,
                                                     quiescence_reason=reason, wake_condition="probe")
    _, projected = await offered(lab_)
    target = next(item for item in projected["acquisition_candidates"] if item["provider_id"] == "prov-ad")
    assert not target["switch_eligible"]
    with pytest.raises(TransferError) as refused:
        await manual_candidate_failover(lab_.engine, lab_.transfer.id, now.id, target["candidate_id"])
    assert refused.value.error.category == Category.RESOURCE_STATE_CONFLICT
    assert (await part(lab_)).candidates[now.selected].provider_id == "prov-rd" and not running(lab_)


async def test_a_provider_chosen_during_the_retry_that_must_first_prepare_is_a_pending_choice(tmp_path, monkeypatch):
    """The picker's provider can take only the file's equivalent pixeldrain
    source and must first prepare it: chosen while the failed route waits
    out its retry, the choice is pending -- the file keeps its route and its
    timer -- and when the copy lands the file moves onto it, once."""
    from test_v113_pending_provider_choice import choices, choose, lab as choice_lab
    from test_v113_switch_member_continuation import CloudProvider

    cloud = CloudProvider("prov-y", ("pixeldrain.com",))
    lab_ = await choice_lab(tmp_path, monkeypatch, y=cloud)
    now = await backoff(lab_, 3600)
    result = await choose(lab_, "prov-y")
    assert (result["result"], result["pending"]) == ("pending", 1)
    await still(lab_.engine, 3)
    waiting = await part(lab_)
    assert (waiting.state, waiting.selected, waiting.retry_at) == ("recovery_wait", now.selected, now.retry_at)
    (choice,) = await choices(lab_)
    assert choice["state"] == "pending"
    cloud.ready = True
    await settle(lab_.engine, 8)                                          # 4 minutes: inside the retry's hour
    (choice,) = await choices(lab_)
    landed = await part(lab_)
    assert lab_.engine.clock() < now.retry_at
    assert choice["state"] == "committed" and landed.candidates[landed.selected].provider_id == "prov-y"
    assert landed.state == "downloading" and running(lab_) == [landed.execution.attempt_id]


async def test_refreshes_after_switching_never_collide_with_or_inflate_the_history_band(tmp_path, monkeypatch):
    """A file whose switches left history bindings refreshes its link through
    the real refresh path as often as it must: each refresh lands, history
    rows keep their orders, and orders never grow beyond what leaves."""
    from transfers.manual_failover import _refresh_exact

    lab_ = await lab(tmp_path, monkeypatch)
    await switched_back(lab_)
    now = await onto(lab_, "ddownload.com")
    history = {item["id"]: item["candidate_order"] for item in await bindings(now.id)
               if item["candidate_order"] > 100000}
    assert history
    ceiling = max(history.values())
    for _ in range(60):
        now = await part(lab_)
        index = next(i for i, item in enumerate(now.candidates) if item.provider_id == "prov-ad")
        refreshed, _index = await _refresh_exact(lab_.engine, now, index)
        assert str(refreshed.candidates[_index].id) != str(now.candidates[index].id)
    after = await bindings(now.id)
    assert {item["id"]: item["candidate_order"] for item in after if item["id"] in history} == history
    assert max(item["candidate_order"] for item in after) == ceiling
    await assert_context_intact(lab_)
