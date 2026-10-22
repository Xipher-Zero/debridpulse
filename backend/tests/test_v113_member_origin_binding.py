"""A provider that serves a link through a member of its own keeps its
binding when the link returns to it (transfer 1085).

A link first resolved DIRECTLY (by a provider issuing its candidate from the
link's own request, as AllDebrid does) keeps a file row keyed by that root
request. When the link is later switched to a provider serving it through a
MEMBER (a copy of its own: Premiumize, TorBox), that provider's candidate is
issued by the member, which no file row is keyed by. On a return to such a
provider, its candidate lands as an origin on the provider's existing binding
-- and the binding must be found through that origin (the binding whose own
origin discovered exactly this candidate, ``bound_candidate_origin``), or it
stays history naming its earlier candidate: the file's selected candidate
then belongs to no link (no provider picker), its links vanish from both
selectors, a pending choice of that provider ends ``not_landed``, and
activation, refresh and failover refuse the candidate.

Neutral fixtures: part01 is reachable from ddownload (prov-rd, direct) and
1fichier (prov-ad, direct; prov-y and prov-w through members). Every
candidate names its upstream host, as every real provider does.
"""
from __future__ import annotations

import pytest
import test_v113_transient_bound_candidate as transient
from test_v113_multi_root_provider_switch import file_of, roots_by_link, rows, settle
from test_v113_pending_provider_choice import choices, choose
from test_v113_switching_context_integrity import HostProvider, bindings, discovered, restart
from test_v113_transient_bound_candidate import DDL1, FICHIER01, IssuingProvider, primary_lab, running

from db.database import get_db
from transfers.manual_failover import _refresh_exact, manual_candidate_failover
from transfers.manual_route_switch import switch_root_provider, transfer_route_status

pytestmark = pytest.mark.asyncio

CLAIMANTS = {"prov-rd", "prov-ad", "prov-y", "prov-w"}


async def lab(tmp_path, monkeypatch):
    monkeypatch.setattr(transient, "ClaimingProvider", HostProvider)
    lab_ = await primary_lab(tmp_path, monkeypatch, (DDL1, FICHIER01), extra=(
        IssuingProvider("prov-y", ("1fichier.com",)), IssuingProvider("prov-w", ("1fichier.com",))))
    lab_.roots = await roots_by_link(lab_.repository, lab_.transfer.id)
    return lab_


async def part(lab_):
    return await file_of(lab_.repository, lab_.transfer.id, "part01.rar")


def on(artifact, provider_id):
    return next(item for item in artifact.candidates if item.provider_id == provider_id)


def host_of(artifact):
    return artifact.candidates[artifact.selected].source_identity.key


async def onto(lab_, host):
    """The operator moves the file to ``host`` by the identity the host selector offers."""
    now = await part(lab_)
    if host_of(now) == host:
        return now
    projected = (await lab_.repository._candidate_presentation(lab_.transfer.id))[now.id]
    target = next(item for item in projected["source_candidates"] if item["source_host"] == host)
    assert target["switch_eligible"]
    await manual_candidate_failover(lab_.engine, lab_.transfer.id, now.id, target["candidate_id"])
    await settle(lab_.engine, 6)
    moved = await part(lab_)
    assert host_of(moved) == host and running(lab_) == [moved.execution.attempt_id]
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


async def returned(lab_):
    """Direct prov-ad -> member prov-y -> member prov-w -> back to prov-y."""
    await onto(lab_, "1fichier.com")
    first = str(on(await relink(lab_, "prov-y", "prov-ad"), "prov-y").id)
    await relink(lab_, "prov-w", "prov-y")
    now = await relink(lab_, "prov-y", "prov-w")
    return first, str(on(now, "prov-y").id)


async def assert_context_intact(lab_):
    """Every current binding names a candidate the file has, at its position;
    every candidate is route-owned; the picker is switchable and offers every
    claimant; both hosts are offered; exactly the selected one is selected."""
    now = await part(lab_)
    current = [str(item.id) for item in now.candidates]
    named = {item["candidate_order"]: item["candidate_id"] for item in await bindings(now.id)
             if item["candidate_order"] <= 100000}
    assert named == {position: candidate for position, candidate in enumerate(current, start=1)}
    for item in now.candidates:
        origin = await lab_.engine.canonical.origin_for(now, item)
        assert origin is not None and origin.provider_id == item.provider_id
    status = await transfer_route_status(lab_.engine, lab_.transfer.id)
    assert status["switchable"] and {item["provider_id"] for item in status["providers"]} == CLAIMANTS
    projected = (await lab_.repository._candidate_presentation(lab_.transfer.id))[now.id]
    assert {item["source_host"] for item in projected["source_candidates"]} == {"ddownload.com", "1fichier.com"}
    assert [item["candidate_id"] for item in projected["acquisition_candidates"] if item["is_selected"]] == [
        current[now.selected]]
    return now


async def test_r1_a_member_providers_returned_candidate_takes_its_binding(tmp_path, monkeypatch):
    lab_ = await lab(tmp_path, monkeypatch)
    first, second = await returned(lab_)
    assert first != second
    now = await part(lab_)
    (binding,) = [item for item in await bindings(now.id) if item["provider_id"] == "prov-y"]
    assert binding["candidate_id"] == second and binding["candidate_order"] == now.selected + 1
    history = await discovered(binding["id"])
    assert history.index(first) < history.index(second)                  # its history stays as it was
    origin = await lab_.engine.canonical.origin_for(now, on(now, "prov-y"))
    assert origin is not None and origin.request.parent_id == lab_.roots[FICHIER01]   # issued by the member
    await assert_context_intact(lab_)


async def test_r2_r3_repeated_switching_keeps_every_provider_and_host_actionable(tmp_path, monkeypatch):
    lab_ = await lab(tmp_path, monkeypatch)
    await onto(lab_, "1fichier.com")
    current = "prov-ad"
    for provider_id in ("prov-y", "prov-w", "prov-y", "prov-ad", "prov-w", "prov-y"):
        await onto(lab_, "1fichier.com")
        await relink(lab_, provider_id, current)
        current = provider_id
        await assert_context_intact(lab_)
        await onto(lab_, "ddownload.com")
        await assert_context_intact(lab_)


async def test_r4_a_pending_choice_of_a_returning_member_provider_lands_exactly(tmp_path, monkeypatch):
    """From ddownload, the operator chooses prov-y, prov-w, then prov-y again
    (a return): each choice commits on exactly its provider's copy."""
    lab_ = await lab(tmp_path, monkeypatch)
    for provider_id in ("prov-y", "prov-w", "prov-y"):
        await onto(lab_, "ddownload.com")
        assert (await choose(lab_, provider_id))["result"] == "pending"
        await settle(lab_.engine, 12)
        choice = (await choices(lab_))[-1]
        now = await part(lab_)
        selected = now.candidates[now.selected]
        assert (choice["provider_id"], choice["state"], choice["reason"]) == (provider_id, "committed", "landed")
        assert selected.provider_id == provider_id and str(selected.id) == choice["committed_candidate_id"]
        assert choice["source_root_id"] == lab_.roots[FICHIER01]
        assert running(lab_) == [now.execution.attempt_id]
        await assert_context_intact(lab_)


async def test_r5_activation_refresh_and_failover_recognize_the_returned_candidate(tmp_path, monkeypatch):
    lab_ = await lab(tmp_path, monkeypatch)
    _first, second = await returned(lab_)
    now = await onto(lab_, "ddownload.com")

    async def no_history(artifact_id):
        return {"candidate_attempt_history": []}

    recorded = lab_.repository.recovery_context
    monkeypatch.setattr(lab_.repository, "recovery_context", no_history)
    assert await lab_.engine._next_alternate_index(now) == next(
        index for index, item in enumerate(now.candidates) if str(item.id) == second)
    monkeypatch.setattr(lab_.repository, "recovery_context", recorded)

    index = next(i for i, item in enumerate(now.candidates) if str(item.id) == second)
    refreshed, index = await _refresh_exact(lab_.engine, now, index)
    assert refreshed.candidates[index].provider_id == "prov-y"
    assert lab_.providers["prov-y"].refreshed[-1].startswith("issuer://member/")   # through its own member
    await assert_context_intact(lab_)
    moved = await onto(lab_, "1fichier.com")
    assert moved.candidates[moved.selected].provider_id == "prov-y"


async def stale(lab_, first):
    """The state the earlier version persisted (transfer 1085): the returned
    provider's binding in the non-current band, still naming its earlier
    candidate, the file's candidates unchanged."""
    now = await part(lab_)
    (binding,) = [item for item in await bindings(now.id) if item["provider_id"] == "prov-y"]
    async with get_db() as db:
        top = await db.fetchone("SELECT MAX(candidate_order) AS n FROM canonical_candidate_bindings "
                                "WHERE canonical_artifact_id=?", (now.id,))
        await db.execute("UPDATE canonical_candidate_bindings SET candidate_id=?,candidate_order=? WHERE id=?",
                         (first, max(100000, int(top["n"])) + 1, binding["id"]))
        await db.commit()
    assert await lab_.engine.canonical.origin_for(now, on(now, "prov-y")) is None
    assert not (await transfer_route_status(lab_.engine, lab_.transfer.id))["switchable"]
    return now, binding


async def test_r6_startup_repairs_exactly_the_disconnected_binding_and_is_idempotent(tmp_path, monkeypatch):
    lab_ = await lab(tmp_path, monkeypatch)
    first, second = await returned(lab_)
    now, binding = await stale(lab_, first)
    others = [item for item in await bindings(now.id) if item["id"] != binding["id"]]
    origins = await rows("SELECT * FROM canonical_candidate_origins ORDER BY id", ())
    await restart(lab_)
    (repaired,) = [item for item in await bindings(now.id) if item["id"] == binding["id"]]
    assert (repaired["candidate_id"], repaired["candidate_order"]) == (second, now.selected + 1)
    assert [item for item in await bindings(now.id) if item["id"] != binding["id"]] == others
    assert await rows("SELECT * FROM canonical_candidate_origins ORDER BY id", ()) == origins
    await assert_context_intact(lab_)
    before = await bindings(now.id)
    await restart(lab_)
    assert await bindings(now.id) == before


async def test_r7_a_candidate_two_bindings_claim_to_have_discovered_stays_unbound(tmp_path, monkeypatch):
    """Ambiguous provenance: another binding's origin also claims to have
    discovered the candidate. Nothing is guessed: no binding is renamed or
    moved and the candidate gains no route ownership."""
    lab_ = await lab(tmp_path, monkeypatch)
    first, second = await returned(lab_)
    now, binding = await stale(lab_, first)
    (other,) = [item for item in await bindings(now.id) if item["provider_id"] == "prov-w"]
    async with get_db() as db:
        origin = await db.fetchone("SELECT * FROM canonical_candidate_origins WHERE binding_id=? LIMIT 1",
                                   (other["id"],))
        await db.execute(
            """INSERT INTO canonical_candidate_origins(binding_id,contributing_artifact_id,contributing_transfer_id,
               request_id,resolution_attempt_id,discovered_candidate_id) VALUES(?,?,?,?,?,?)""",
            (other["id"], origin["contributing_artifact_id"], origin["contributing_transfer_id"],
             origin["request_id"], origin["resolution_attempt_id"], second))
        await db.commit()
    before = await bindings(now.id)
    await restart(lab_)
    assert await bindings(now.id) == before
    now = await part(lab_)
    assert await lab_.engine.canonical.origin_for(now, on(now, "prov-y")) is None
    assert await lab_.engine.canonical.bound_origin(now, on(now, "prov-y")) is None


async def test_r5b_a_refresh_is_only_ever_the_issuing_requests(tmp_path, monkeypatch):
    """``refresh_candidate`` refreshes a candidate only through an origin of
    its own binding that issued exactly it. Handed another link's origin
    (ddownload's, whose own provenance does carry the replacement), it
    refuses and changes nothing; through its own origin it lands."""
    lab_ = await lab(tmp_path, monkeypatch)
    await returned(lab_)
    now = await part(lab_)
    candidate, other = on(now, "prov-y"), on(now, "prov-rd")
    foreign = await lab_.engine.canonical.origin_for(now, other)
    before = (await bindings(now.id), [str(item.id) for item in now.candidates])
    assert not await lab_.engine.canonical.refresh_candidate(now, foreign, candidate, (other,))
    assert (await bindings(now.id), [str(item.id) for item in (await part(lab_)).candidates]) == before
    own = await lab_.engine.canonical.origin_for(now, candidate)
    index = next(i for i, item in enumerate(now.candidates) if item.id == candidate.id)
    refreshed, index = await _refresh_exact(lab_.engine, now, index)
    assert refreshed.candidates[index].provider_id == "prov-y" and own.request.parent_id == lab_.roots[FICHIER01]
    await assert_context_intact(lab_)
