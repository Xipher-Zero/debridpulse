"""An operator's pending provider choice (transfer 1082, Option A).

The provider selector names a best-effort PRIMARY provider. For a file whose
own link that provider cannot take, but which has a proven-equivalent source on
a link it may take, the choice is a durable INTENT
(``transfers.provider_choice``): that link is switched to the provider, and its
copy is selected for the file only when it lands. Until then the file keeps
its committed source and writer, and its own recovery stays authoritative.

Neutral fixtures: part01 downloads from ddownload via prov-rd; pixeldrain is an
equivalent source of it, resolved by prov-tb and also claimed by prov-y (and,
where present, 1fichier by prov-ad and prov-z).
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest
from test_v113_collection_route_generic_closure import Clock
from test_v113_multi_root_provider_switch import file_of, listed, roots_by_link, rows, settle
from test_v113_root_provider_switch import application_of
from test_v113_switch_member_continuation import CloudProvider
from test_v113_transient_bound_candidate import (
    DDL1, FICHIER01, PIX1, ClaimingProvider, RecordingExecutor, by_provider, primary_lab, running,
)

from application.manual_route_switch import switch_transfer_provider
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage
from transfers.manual_failover import manual_candidate_failover
from transfers.manual_route_switch import transfer_route_status
from transfers.models import ExecutionState
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

UNAVAILABLE_FOR_GOOD = NormalizedError(Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION,
                                       Retryability.NEVER, origin=Origin.PROVIDER)


async def lab(tmp_path, monkeypatch, *, y=None, links=(DDL1, PIX1), extra=()):
    y = y or ClaimingProvider("prov-y", ("pixeldrain.com",), priority=0)
    lab_ = await primary_lab(tmp_path, monkeypatch, links, extra=(y, *extra))
    lab_.roots = await roots_by_link(lab_.repository, lab_.transfer.id)
    lab_.part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    assert lab_.part.state == "downloading" and lab_.part.candidates[lab_.part.selected].provider_id == "prov-rd"
    return lab_


async def choose(lab_, provider_id):
    """What the picker showed for ``provider_id``, then the operator's choice."""
    item = by_provider(await transfer_route_status(lab_.engine, lab_.transfer.id))[provider_id]
    expected = {row["root_id"]: row["current_provider_id"] for row in item["roots"] if row["status"] == "eligible"}
    return await switch_transfer_provider(application_of(lab_.engine), lab_.transfer.id, provider_id,
                                          expected_routes=expected)


async def choices(lab_):
    return [dict(row) for row in await rows("SELECT * FROM pending_provider_choices ORDER BY created_at,id", ())]


async def activations(lab_) -> list[tuple[str, str]]:
    return [(detail.get("authority"), detail.get("new_provider_id")) for detail in (
        json.loads(row["detail"]) for row in await rows(
            "SELECT detail FROM application_events WHERE kind='candidate_activation' ORDER BY id", ()))]


async def part(lab_):
    return await file_of(lab_.repository, lab_.transfer.id, "part01.rar")


def selected_provider(artifact):
    return artifact.candidates[artifact.selected].provider_id


async def held(lab_) -> bool:
    from db.database import get_db
    async with get_db() as db:
        return lab_.part.id in await lab_.repository._replacement_held_artifacts(db, (lab_.transfer.id,))


async def test_a_provider_that_must_first_acquire_an_equivalent_source_waits_and_then_becomes_primary(
        tmp_path, monkeypatch):
    """Core path: the choice is accepted as PENDING; the file keeps prov-rd and
    its writer (never held, never on prov-tb); prov-y prepares pixeldrain; when
    its copy lands the canonical activation selects it, retires prov-rd's
    writer and one prov-y writer runs."""
    lab_ = await lab(tmp_path, monkeypatch)
    first = lab_.part.execution.attempt_id
    (row,) = by_provider(await transfer_route_status(lab_.engine, lab_.transfer.id))["prov-y"]["roots"]
    assert (row["status"], row["via"]) == ("eligible", "alternate")

    result = await choose(lab_, "prov-y")
    assert (result["result"], result["pending"], result["switched"]) == ("pending", 1, 0)
    (root,) = result["roots"]
    assert root["outcome"] == "pending" and [item["outcome"] for item in root["files"]] == ["pending"]
    (choice,) = await choices(lab_)
    assert (choice["state"], choice["provider_id"], choice["source_root_id"], choice["artifact_id"]) == (
        "pending", "prov-y", lab_.roots[PIX1], lab_.part.id)
    # Nothing about the file changed: same selection, same writer, not held.
    now = await part(lab_)
    assert (now.selected, now.execution.attempt_id) == (lab_.part.selected, first) and running(lab_) == [first]
    assert not await held(lab_)
    item = await listed(lab_)
    assert (item["active_provider_id"], item["pending_provider_ids"]) == ("prov-rd", ["prov-y"])
    assert (await lab_.repository.latest_root_route(lab_.roots[PIX1]))["provider_id"] == "prov-y"

    await settle(lab_.engine, 10)
    (choice,) = await choices(lab_)
    assert (choice["state"], choice["reason"]) == ("committed", "landed")
    now = await part(lab_)
    assert now.state == "downloading" and selected_provider(now) == "prov-y"
    assert str(now.candidates[now.selected].id) == choice["committed_candidate_id"]
    assert running(lab_) == [now.execution.attempt_id] and now.execution.attempt_id != first
    assert lab_.issuer.refreshed == []                                # prov-tb never issued material
    provenance = await rows("SELECT provider_id FROM execution_attempt_provenance WHERE artifact_id=? "
                            "ORDER BY ordinal", (now.id,))
    assert [item["provider_id"] for item in provenance] == ["prov-rd", "prov-y"]
    assert await activations(lab_) == [("user_route_switch", "prov-y")]
    item = await listed(lab_)
    assert item["active_provider_id"] == "prov-y" and "pending_provider_ids" not in item
    # Repeatable: prov-rd is a choice again from here.
    assert by_provider(await transfer_route_status(lab_.engine, lab_.transfer.id))["prov-rd"]["selectable"]


async def test_a_requested_provider_that_fails_for_good_ends_the_choice_and_leaves_the_working_route(
        tmp_path, monkeypatch):
    """prov-y fails for good; routing falls back on the pixeldrain link. The
    fallback never fulfils the choice: it ends ``failed``, and the file keeps
    downloading from prov-rd with the same writer -- never held, never moved
    to prov-tb."""
    y = ClaimingProvider("prov-y", ("pixeldrain.com",), priority=0)
    y.failures = [UNAVAILABLE_FOR_GOOD]
    lab_ = await lab(tmp_path, monkeypatch, y=y)
    first = lab_.part.execution.attempt_id
    assert (await choose(lab_, "prov-y"))["result"] == "pending"
    await settle(lab_.engine, 10)
    (choice,) = await choices(lab_)
    assert choice["state"] == "failed" and choice["reason"] in {"provider_fell_back", "provider_failed"}
    now = await part(lab_)
    assert (selected_provider(now), now.state, now.execution.attempt_id) == ("prov-rd", "downloading", first)
    assert running(lab_) == [first] and not await held(lab_)
    assert await activations(lab_) == []
    item = await listed(lab_)
    assert item["active_provider_id"] == "prov-rd" and "pending_provider_ids" not in item


def fail_writer(lab_, attempt, retryability):
    """The file's writer fails as an executor reports it."""
    error = NormalizedError(Domain.EXECUTOR, Category.CONNECTION_TIMEOUT, Stage.EXECUTION, retryability,
                            origin=Origin.EXECUTOR, integration_id="memory")
    lab_.executor.jobs[attempt] = replace(lab_.executor.jobs[attempt], state=ExecutionState.FAILED, error=error)


@pytest.mark.parametrize("retryability", [Retryability.BACKOFF, Retryability.NEVER], ids=["retryable", "permanent"])
async def test_the_files_own_recovery_is_never_blocked_and_the_choice_can_still_land(
        tmp_path, monkeypatch, retryability):
    """prov-y prepares a cloud copy (PREPARING); the choice stays pending, owned
    by that link's own resolution. Meanwhile the file's prov-rd writer fails
    and ORDINARY recovery decides it exactly as without a choice: a retryable
    failure resumes prov-rd (the choice neither vetoes nor replaces that), a
    permanent one ends in its explicit error. When prov-y's copy lands, the
    still-current choice selects it and one prov-y writer runs."""
    cloud = CloudProvider("prov-y", ("pixeldrain.com",))
    lab_ = await lab(tmp_path, monkeypatch, y=cloud)
    assert (await choose(lab_, "prov-y"))["result"] == "pending"
    await settle(lab_.engine, 4)
    (choice,) = await choices(lab_)
    assert choice["state"] == "pending" and not await held(lab_)
    fail_writer(lab_, (await part(lab_)).execution.attempt_id, retryability)
    await settle(lab_.engine, 6)
    recovered = await part(lab_)
    assert selected_provider(recovered) == "prov-rd"
    if retryability == Retryability.BACKOFF:
        assert recovered.state == "downloading" and running(lab_) == [recovered.execution.attempt_id]
    else:
        assert recovered.state == "error" and recovered.error.category == Category.CONNECTION_TIMEOUT
        assert not running(lab_)
    (choice,) = await choices(lab_)
    assert choice["state"] == "pending"

    cloud.ready = True
    await settle(lab_.engine, 10)
    (choice,) = await choices(lab_)
    after = await part(lab_)
    assert choice["state"] == "committed" and selected_provider(after) == "prov-y"
    assert after.state == "downloading" and running(lab_) == [after.execution.attempt_id]


async def test_the_operators_host_switch_supersedes_a_pending_choice(tmp_path, monkeypatch):
    """The independent host selector moves the file to its 1fichier source
    while prov-y prepares: that selection wins, the choice is superseded in the
    same commit, and prov-y's later copy never undoes it."""
    cloud = CloudProvider("prov-y", ("pixeldrain.com",))
    lab_ = await lab(tmp_path, monkeypatch, y=cloud, links=(DDL1, PIX1, FICHIER01))
    assert (await choose(lab_, "prov-y"))["result"] == "pending"
    fichier = next(item for item in lab_.part.candidates if item.provider_id == "prov-ad")
    await manual_candidate_failover(lab_.engine, lab_.transfer.id, lab_.part.id, str(fichier.id))
    (choice,) = await choices(lab_)
    assert (choice["state"], choice["reason"]) == ("superseded", "selection_changed")
    cloud.ready = True
    await settle(lab_.engine, 12)
    now = await part(lab_)
    assert selected_provider(now) == "prov-ad" and running(lab_) == [now.execution.attempt_id]


async def test_a_later_provider_choice_supersedes_the_earlier_one(tmp_path, monkeypatch):
    """prov-y (pixeldrain), then prov-z (1fichier), before either is ready:
    only prov-z's choice is current; prov-y's copy landing never selects it."""
    cloud = CloudProvider("prov-y", ("pixeldrain.com",))
    z = ClaimingProvider("prov-z", ("1fichier.com",), priority=0)
    lab_ = await lab(tmp_path, monkeypatch, y=cloud, links=(DDL1, PIX1, FICHIER01), extra=(z,))
    assert (await choose(lab_, "prov-y"))["result"] == "pending"
    assert (await choose(lab_, "prov-z"))["result"] == "pending"
    states = {row["provider_id"]: (row["state"], row["reason"]) for row in await choices(lab_)}
    assert states == {"prov-y": ("superseded", "new_choice"), "prov-z": ("pending", None)}
    cloud.ready = True
    await settle(lab_.engine, 12)
    now = await part(lab_)
    assert selected_provider(now) == "prov-z" and running(lab_) == [now.execution.attempt_id]
    states = {row["provider_id"]: row["state"] for row in await choices(lab_)}
    assert states == {"prov-y": "superseded", "prov-z": "committed"}
    assert [item for item in await activations(lab_) if item[1] == "prov-y"] == []


async def test_a_restart_while_the_provider_prepares_keeps_the_choice_and_lands_it_once(tmp_path, monkeypatch):
    """The choice is durable: a new engine on the same state finds it pending,
    keeps the file's working route meanwhile, and lands prov-y's copy with
    exactly one writer -- no new operator request."""
    lab_ = await lab(tmp_path, monkeypatch)
    assert (await choose(lab_, "prov-y"))["result"] == "pending"
    registry = IntegrationRegistry()
    for identity, provider in lab_.providers.items():
        registry.register_provider(provider)
    executor = RecordingExecutor(lab_.repository.authorize_execution)
    executor.jobs.update(lab_.executor.jobs)
    registry.register_executor(executor)
    engine = TransferEngine(lab_.repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3, resolution_concurrency=1),
                            clock=Clock())
    engine.clock.now = lab_.engine.clock.now
    await engine.initialize()
    (choice,) = await choices(lab_)
    assert choice["state"] == "pending"
    await settle(engine, 10)
    (choice,) = await choices(lab_)
    now = await part(lab_)
    assert choice["state"] == "committed" and selected_provider(now) == "prov-y"
    live = [attempt for attempt, job in executor.jobs.items() if job.state == ExecutionState.RUNNING]
    assert live == [now.execution.attempt_id]


async def test_a_choice_that_would_discard_downloaded_data_is_refused_before_anything_changes(
        tmp_path, monkeypatch):
    """Moving the file's writing to the equivalent source would discard data
    the operator never agreed to discard: refused up front -- no choice, the
    link's route unchanged, the file untouched -- and reported unchanged."""
    from types import SimpleNamespace
    lab_ = await lab(tmp_path, monkeypatch)
    first = lab_.part.execution.attempt_id

    async def discarding(_artifact, _candidate):
        return SimpleNamespace(discarded_bytes=4, retained_bytes=0, material_generation=1, unusable_bytes=4)
    monkeypatch.setattr(lab_.engine, "preview_continuation", discarding)
    result = await choose(lab_, "prov-y")
    (root,) = result["roots"]
    assert (result["result"], root["outcome"], root["reason"]) == ("unchanged", "refused",
                                                                   "discard_confirmation_required")
    assert await choices(lab_) == []
    assert (await lab_.repository.latest_root_route(lab_.roots[PIX1]))["provider_id"] == "prov-tb"
    assert (await part(lab_)).execution.attempt_id == first and running(lab_) == [first]


async def test_switching_the_files_own_link_supersedes_its_pending_choice_at_once(tmp_path, monkeypatch):
    """While prov-y prepares, the operator switches the file's OWN ddownload
    link to prov-rd2 (a newer, different decision about this file). That switch
    supersedes the pending choice in its own commit, so prov-y's copy landing
    first can never override it."""
    from transfers.manual_route_switch import switch_root_provider
    cloud = CloudProvider("prov-y", ("pixeldrain.com",))
    rd2 = ClaimingProvider("prov-rd2", ("ddownload.com",), priority=0)
    lab_ = await lab(tmp_path, monkeypatch, y=cloud, extra=(rd2,))
    assert (await choose(lab_, "prov-y"))["result"] == "pending"
    rd2.failures = [replace(UNAVAILABLE_FOR_GOOD, retryability=Retryability.BACKOFF)] * 3   # rd2 slower than prov-y
    await switch_root_provider(lab_.engine, lab_.transfer.id, "prov-rd2", expected_provider_id="prov-rd",
                               artifact_id=lab_.part.id, expected_root_id=lab_.roots[DDL1])
    (choice,) = await choices(lab_)
    assert (choice["state"], choice["reason"]) == ("superseded", "owning_root_switched")
    cloud.ready = True
    await settle(lab_.engine, 12)
    assert ("user_route_switch", "prov-y") not in await activations(lab_)
    assert selected_provider(await part(lab_)) != "prov-y"


# -- fulfilment identity at commit (Gate 9 rejection of 932070bf) --------------------------------------------
# The landing names prov-y's copy; between that read and the activation's claim
# another commit may rewrite the file's candidate list. The injection runs at
# exactly that point: just before the file's claim is taken.

def before_claim(monkeypatch, lab_, mutate):
    """Run ``mutate`` once, right before the pending choice's activation takes
    the file's claim (``activate_candidate_command``)."""
    from transfers.recovery_execution import RecoveryTrigger
    real = lab_.repository.claim_recovery
    fired = []

    async def claim_recovery(artifact_id, trigger, *args, **kwargs):
        if trigger == RecoveryTrigger.USER_ROUTE_SWITCH and artifact_id == lab_.part.id and not fired:
            fired.append(True)
            await mutate()
        return await real(artifact_id, trigger, *args, **kwargs)
    monkeypatch.setattr(lab_.repository, "claim_recovery", claim_recovery)
    return fired


async def rewrite_candidates(artifact_id, change):
    """Rewrite the file's durable candidate list (``change(list) -> list``),
    keeping its selection on the same candidate."""
    from db.database import get_db
    from transfers import codec
    async with get_db() as db:
        row = await db.fetchone("SELECT candidates,selected_candidate FROM download_files WHERE id=?", (artifact_id,))
        stored = codec.load(row["candidates"], [])
        selected_id = str(codec.candidate(stored[int(row["selected_candidate"] or 0)]).id)
        stored = change(stored)
        index = next(i for i, item in enumerate(stored) if str(codec.candidate(item).id) == selected_id)
        await db.execute("UPDATE download_files SET candidates=?,selected_candidate=? WHERE id=?",
                         (codec.dump(stored), index, artifact_id))
        await db.commit()


async def landed_lab(tmp_path, monkeypatch):
    """ddownload (prov-rd, selected), 1fichier (prov-ad), pixeldrain (prov-tb;
    prov-y claims it, preparing). The choice for prov-y is pending and prov-y's
    copy is ready to land at the next resolution."""
    cloud = CloudProvider("prov-y", ("pixeldrain.com",))
    lab_ = await lab(tmp_path, monkeypatch, y=cloud, links=(DDL1, FICHIER01, PIX1))
    assert (await choose(lab_, "prov-y"))["result"] == "pending"
    await settle(lab_.engine, 3)
    cloud.ready = True
    return lab_


def ids(stored):
    from transfers import codec
    return [codec.candidate(item) for item in stored]


async def test_a_moved_target_index_cannot_redirect_the_choice_to_another_source(tmp_path, monkeypatch):
    """Between the landing's read and the claim, prov-ad's 1fichier candidate and
    prov-y's copy trade places: the index computed first now names prov-ad. The
    choice still lands prov-y's copy -- found by identity -- and only it."""
    lab_ = await landed_lab(tmp_path, monkeypatch)

    async def swap():
        def change(stored):
            items = ids(stored)
            y_at = next(i for i, item in enumerate(items) if item.provider_id == "prov-y")
            ad_at = next(i for i, item in enumerate(items) if item.provider_id == "prov-ad")
            stored = list(stored)
            stored[y_at], stored[ad_at] = stored[ad_at], stored[y_at]
            return stored
        await rewrite_candidates(lab_.part.id, change)
    fired = before_claim(monkeypatch, lab_, swap)
    await settle(lab_.engine, 6)
    assert fired
    now = await part(lab_)
    (choice,) = await choices(lab_)
    assert choice["state"] == "committed" and selected_provider(now) == "prov-y"
    assert str(now.candidates[now.selected].id) == choice["committed_candidate_id"]
    assert running(lab_) == [now.execution.attempt_id]
    assert "prov-ad" not in [provider for authority, provider in await activations(lab_)]


async def test_a_target_that_is_no_longer_the_authorized_copy_is_refused_before_anything_changes(
        tmp_path, monkeypatch):
    """Between the read and the claim, prov-y's copy is replaced by an unrelated
    candidate: the activation finds no authorized target and refuses before
    retiring anything. The working prov-rd writer keeps running, nothing
    commits, and the choice stays pending for the next decision."""
    lab_ = await landed_lab(tmp_path, monkeypatch)
    first = (await part(lab_)).execution.attempt_id

    async def replace_target():
        def change(stored):
            out = []
            for item in stored:
                if ids([item])[0].provider_id == "prov-y":
                    item = dict(item, id="unrelated-copy")
                out.append(item)
            return out
        await rewrite_candidates(lab_.part.id, change)
    fired = before_claim(monkeypatch, lab_, replace_target)
    await settle(lab_.engine, 6)
    assert fired
    now = await part(lab_)
    (choice,) = await choices(lab_)
    # Never fulfilled by the unrelated candidate: pending, or ended because no
    # authorized copy of the source remains in the file (``not_landed``).
    assert choice["state"] in {"pending", "failed"} and choice["committed_candidate_id"] is None
    assert selected_provider(now) == "prov-rd" and now.execution.attempt_id == first and running(lab_) == [first]
    assert (await rows("SELECT status FROM torrents", ()))[0]["status"] == "downloading"


async def test_the_commit_itself_refuses_a_target_that_is_not_the_authorized_copy(tmp_path, monkeypatch):
    """The last line: even if the target passed the activation's check, the
    commit re-verifies it. Here the target's binding stops being prov-y's issue
    of the pixeldrain link after that check; the selection does not commit, the
    choice is not fulfilled, and the file's own route carries on (prov-rd)."""
    lab_ = await landed_lab(tmp_path, monkeypatch)
    from db.database import get_db
    real_check = lab_.repository.provider_choice_target_current

    async def check_then_break(choice_id, candidate_id):
        verdict = await real_check(choice_id, candidate_id)
        async with get_db() as db:
            await db.execute("UPDATE canonical_candidate_bindings SET provider_id='prov-q' WHERE candidate_id=?",
                             (str(candidate_id),))
            await db.commit()
        return verdict
    monkeypatch.setattr(lab_.repository, "provider_choice_target_current", check_then_break)
    await settle(lab_.engine, 6)
    (choice,) = await choices(lab_)
    assert choice["state"] != "committed" and choice["committed_candidate_id"] is None
    now = await part(lab_)
    # Failed closed: the file kept its own source and is downloading again on it
    # (the retired writer is not left authorized), one writer; the transfer goes on.
    assert selected_provider(now) == "prov-rd" and now.state == "downloading"
    assert running(lab_) == [now.execution.attempt_id]
    assert (await rows("SELECT status FROM torrents", ()))[0]["status"] == "downloading"


async def test_full_details_report_pending_choices_and_outcomes_on_its_own_connection(tmp_path, monkeypatch):
    """Full Details returns the pending choice -- and, once it lands, its
    committed outcome -- read on the Details read's own connection: the
    SQLite acquisitions of a full Details read are the same with a pending
    choice as without one. The bounded (non-Details) read carries neither."""
    from db import database as db_module
    lab_ = await lab(tmp_path, monkeypatch)

    async def acquires(details):
        before = db_module.db_runtime_metrics()["sqlite_acquires"]
        result = await lab_.repository.presentation(lab_.transfer.id, details=details)
        return db_module.db_runtime_metrics()["sqlite_acquires"] - before, result

    without, plain = await acquires(True)
    assert plain["pending_provider_choices"] == [] and plain["provider_choices"] == []
    assert (await choose(lab_, "prov-y"))["result"] == "pending"
    with_choice, detail = await acquires(True)
    assert with_choice == without
    ((pending,),) = (detail["pending_provider_choices"],)
    assert (pending["provider_id"], pending["artifact_id"], pending["source_root_id"]) == (
        "prov-y", lab_.part.id, lab_.roots[PIX1])
    assert [(item["provider_id"], item["state"]) for item in detail["provider_choices"]] == [("prov-y", "pending")]
    _count, bounded = await acquires(False)
    assert "pending_provider_choices" not in bounded and "provider_choices" not in bounded

    await settle(lab_.engine, 10)
    _count, detail = await acquires(True)
    assert detail["pending_provider_choices"] == []
    assert [(item["provider_id"], item["state"], item["reason"]) for item in detail["provider_choices"]] == [
        ("prov-y", "committed", "landed")]
