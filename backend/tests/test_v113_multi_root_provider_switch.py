"""WS T (transfer 1063): an operator switches the provider of ONE root of a
transfer that is not a single torrent root.

The trace's shape: a release split into parts, each part submitted as a mirror
pair -- one link on a host two debrid providers both claim, one on a host only
one of them claims -- plus a lone link on a third host. A root is offered for a
switch only when it produced the selected candidate of an unfinished file, its
alternatives are exactly the providers that claim THAT root's own link, and a
switch moves only the files whose selected source that root produced: every
other file keeps its writer, its selection and its provenance. Between the
switch's commit and the root's new resolution the moved files are held by a
rule derived from stored facts, so a restart in between changes nothing. Every
provider is a neutral host-claim fixture; no provider identity decides anything.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fake_integrations import MemoryExecutor
from test_v113_collection_route_generic_closure import Clock
from test_v113_root_provider_switch import FILES, application_of, magnet_provider, offer

from db import database
from db.database import get_db
from transfers.applicability import ApplicabilityReadiness, HostClaim, HostClaimScope, ProviderApplicability
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage, TransferError
from transfers.manual_route_switch import (
    root_providers, root_switches, route_providers, route_status, route_switch_offer, switch_root_provider,
)
from transfers.models import (
    Capability, Endpoint, ExecutionState, IntegrationDescriptor, MaterializationAdmissionKind, ResolutionResult,
    ResourceState, TransferCandidate, TransferRequest, TransferState,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

# Which hosts each neutral provider claims (the trace: two debrid services both
# reach the first host; only one reaches each of the others).
HOSTS = {"prov-ad": ("rapidgator.net", "1fichier.com"), "prov-dl": ("rapidgator.net", "pixeldrain.com"),
         "prov-tb": ("pixeldrain.com",)}
PRIORITY = {"prov-ad": 10, "prov-dl": 5, "prov-tb": 1}
RG1, PD1 = "https://rapidgator.net/f/part01.rar", "https://pixeldrain.com/u/part01.rar"
RG2, PD2 = "https://rapidgator.net/f/part02.rar", "https://pixeldrain.com/u/part02.rar"
FICHIER = "https://1fichier.com/x/extra.bin"
UNAVAILABLE = NormalizedError(Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION,
                              Retryability.BACKOFF, origin=Origin.PROVIDER)


class HostProvider:
    """A neutral URL provider claiming its hosts. Every mirror of one part
    resolves to the same object (one endpoint per file name), so mirrors of a
    part become one canonical file with one candidate per mirror."""

    def __init__(self, identity):
        self.descriptor = IntegrationDescriptor(identity, identity, frozenset({Capability.RESOLVE}),
                                                request_types=frozenset({"https"}), priority=PRIORITY[identity])
        self.applicability = ProviderApplicability(
            specialized_hosts=tuple(HostClaim(host, HostClaimScope.DOMAIN, frozenset({"https"}))
                                    for host in HOSTS[identity]),
            specialized=True, readiness=ApplicabilityReadiness.READY)
        self.resolved: list[str] = []
        self.sizes: dict[str, int] = {}
        self.failing: set[str] = set()

    async def resolve(self, request):
        payload = str(request.payload)
        self.resolved.append(payload)
        if payload in self.failing:
            raise TransferError(replace(UNAVAILABLE, integration_id=self.descriptor.id))
        name = payload.rsplit("/", 1)[-1]
        return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
            name, (Endpoint("memory", f"memory:{name}"),), expected_bytes=self.sizes.get(payload, 4),
            provider_id=self.descriptor.id),))


async def lab(tmp_path, monkeypatch, links=(RG1, PD1, RG2, PD2, FICHIER)):
    """The trace: part01 selects its rapidgator mirror; part02 was moved by
    the operator onto its pixeldrain mirror (an ordinary candidate switch)."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "multi-root.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    providers = {identity: HostProvider(identity) for identity in HOSTS}
    for provider in providers.values():
        registry.register_provider(provider)
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            # One resolution at a time: the links resolve in submission order, so
                            # each part's first mirror (rapidgator) is its canonical, selected source
                            # -- the trace -- rather than whichever concurrent resolution landed first.
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3, resolution_concurrency=1),
                            clock=Clock())
    await engine.initialize()
    transfer = await engine.submit(tuple(TransferRequest("https", link) for link in links), name="1063",
                                   deduplicate=False)
    await settle(engine)
    part02 = next((item for item in await repository.artifacts(transfer.id) if item.name == "part02.rar"), None)
    if part02 is not None:
        mirror = next(index for index, item in enumerate(part02.candidates) if item.provider_id == "prov-dl")
        assert (await engine.activate_candidate_command(transfer.id, part02.id, mirror)).committed
        await settle(engine)
    return SimpleNamespace(repository=repository, registry=registry, engine=engine, providers=providers,
                           executor=executor, transfer=transfer)


async def settle(engine, ticks=8):
    for _ in range(ticks):
        engine.clock.now += 30
        await engine.tick()


async def roots_by_link(repository, transfer_id):
    return {str(item.request.payload): item.id for item in await repository.requests(transfer_id)
            if item.parent_id is None}


async def file_of(repository, transfer_id, name):
    return next(item for item in await repository.artifacts(transfer_id) if item.name == name)


async def rows(sql, params=()):
    async with get_db() as db:
        return await db.fetchall(sql, params)


async def listed(lab_):
    from api.operational_downloads import list_operational_torrents
    application = SimpleNamespace(engine=lab_.engine, repository=lab_.repository, definitions={})
    page = await list_operational_torrents(status=None, search=None, limit=0, offset=0, order=None,
                                           application=application)
    return next(item for item in (page["items"] if isinstance(page, dict) else page)
                if item["id"] == lab_.transfer.id)


async def details(lab_):
    from api.routes import get_torrent
    application = SimpleNamespace(engine=lab_.engine, repository=lab_.repository, definitions={})
    detail = await get_torrent(lab_.transfer.id, application=application, request=None)
    # Mirror standbys are history rows: only a canonical file names a root.
    return {item["filename"]: item["route_root"] for item in detail["files"] if item.get("route_root")}


def offered(status):
    return {item["provider_id"]: (item["status"], item["selectable"]) for item in status["providers"]}


# -- D-T3: alternatives are the providers of the exact root ------------------------------------------------------

async def test_each_root_offers_only_the_providers_that_claim_its_own_link(tmp_path, monkeypatch):
    lab_ = await lab(tmp_path, monkeypatch)
    roots = await roots_by_link(lab_.repository, lab_.transfer.id)
    part01 = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    part02 = await file_of(lab_.repository, lab_.transfer.id, "part02.rar")
    assert (part01.candidates[part01.selected].provider_id, part02.candidates[part02.selected].provider_id) == (
        "prov-ad", "prov-dl")

    # A rapidgator root offers the other rapidgator claimant; a pixeldrain
    # root never offers the provider that cannot reach pixeldrain.
    assert offered(await root_providers(lab_.engine, lab_.transfer.id, roots[RG1])) == {
        "prov-ad": ("current", False), "prov-dl": ("available", True)}
    for link in (PD1, PD2):
        assert "prov-ad" not in offered(await root_providers(lab_.engine, lab_.transfer.id, roots[link]))
    assert offered(await root_providers(lab_.engine, lab_.transfer.id, roots[FICHIER])) == {
        "prov-ad": ("current", False)}

    # Only a root that produced a selected candidate is a switch subject:
    # rapidgator part02 and pixeldrain part01 supply alternates only.
    switchable = {link: (await root_providers(lab_.engine, lab_.transfer.id, roots[link]))["switchable"]
                  for link in roots}
    assert switchable == {RG1: True, PD1: False, PD2: True, RG2: False, FICHIER: False}

    # The list hint opens the transfer's provider picker (several roots, B1);
    # Details names each file's producing root and its provider -- never the
    # selected candidate's.
    row = await listed(lab_)
    assert (row["route_switch_available"], row["route_switch_scope"]) == (True, "transfer")
    assert "route_provider_id" not in row
    files = await details(lab_)
    assert {name: (route["root_id"], route["provider_id"], route["switch_available"])
            for name, route in files.items()} == {
        "part01.rar": (roots[RG1], "prov-ad", True), "part02.rar": (roots[PD2], "prov-dl", True),
        "extra.bin": (roots[FICHIER], "prov-ad", False)}
    # Automatic readiness promotion keeps its torrent-only scope.
    assert await route_providers(lab_.engine, lab_.transfer.id) is None


async def test_the_offer_the_picker_and_the_guard_agree_for_every_root_and_lifecycle(tmp_path, monkeypatch):
    """The list hint, the per-file Details affordance, the picker (``GET
    /route?artifact_id``) and the switch's own guard read one rule; on every
    lifecycle state the guard refuses as ``gone`` exactly where nothing is
    offered, and a busy root is offered but refused without a change."""
    lab_ = await lab(tmp_path, monkeypatch)
    repository, engine, transfer = lab_.repository, lab_.engine, lab_.transfer
    roots = await roots_by_link(repository, transfer.id)
    part01 = await file_of(repository, transfer.id, "part01.rar")
    for state in TransferState:
        async with get_db() as db:
            await db.execute("UPDATE torrents SET status=? WHERE id=?", (state.value, transfer.id))
            await db.commit()
        facts = await repository.root_route_facts_by_root(transfer.id)
        switches = root_switches(engine.registry, facts)
        hint = route_switch_offer(engine.registry, facts)
        assert (hint is not None) is any(switches.values()), state
        for root_id in roots.values():
            assert (await root_providers(engine, transfer.id, root_id))["switchable"] is switches[root_id], state
        status = await route_status(engine, transfer.id, part01.id)
        assert (status["root_id"], status["switchable"]) == (roots[RG1], switches[roots[RG1]])
        latest = await repository.latest_root_route(roots[RG1])
        refusal = await repository.root_route_replacement_refusal(
            roots[RG1], expected_attempt_id=str(latest["id"]), expected_provider_id="prov-ad")
        assert switches[roots[RG1]] is (refusal != "gone"), (state, refusal)
    async with get_db() as db:
        await db.execute("UPDATE torrents SET status='downloading' WHERE id=?", (transfer.id,))
        await db.execute("UPDATE transfer_requests SET state='resolving' WHERE id=?", (roots[RG1],))
        await db.commit()
    assert root_switches(engine.registry, await repository.root_route_facts_by_root(transfer.id))[roots[RG1]]
    with pytest.raises(TransferError) as busy:
        await switch_root_provider(engine, transfer.id, "prov-dl", expected_provider_id="prov-ad",
                                   artifact_id=part01.id, expected_root_id=roots[RG1])
    assert busy.value.error.retryability == Retryability.BACKOFF
    assert (await repository.latest_root_route(roots[RG1]))["provider_id"] == "prov-ad"


async def test_a_root_that_supplies_no_selected_source_and_a_stale_root_change_nothing(tmp_path, monkeypatch):
    lab_ = await lab(tmp_path, monkeypatch)
    repository, engine, transfer = lab_.repository, lab_.engine, lab_.transfer
    roots = await roots_by_link(repository, transfer.id)
    part01 = await file_of(repository, transfer.id, "part01.rar")
    history = {link: await repository.latest_root_route(root_id) for link, root_id in roots.items()}
    with pytest.raises(TransferError) as unnamed:                        # several roots: a file must be named
        await switch_root_provider(engine, transfer.id, "prov-dl", expected_provider_id="prov-ad")
    assert unnamed.value.error.category == Category.UNSUPPORTED_REQUEST
    with pytest.raises(TransferError) as stale:                          # the root the operator saw moved
        await switch_root_provider(engine, transfer.id, "prov-dl", expected_provider_id="prov-ad",
                                   artifact_id=part01.id, expected_root_id=roots[RG2])
    assert stale.value.error.category == Category.RESOURCE_STATE_CONFLICT
    with pytest.raises(TransferError) as unreachable:                    # a provider not claiming this link
        await switch_root_provider(engine, transfer.id, "prov-tb", expected_provider_id="prov-ad",
                                   artifact_id=part01.id, expected_root_id=roots[RG1])
    assert unreachable.value.error.category == Category.PROVIDER_UNAVAILABLE
    assert {link: await repository.latest_root_route(root_id) for link, root_id in roots.items()} == history


# -- D-T4: only the files the switched root selected move --------------------------------------------------------

async def test_switching_one_root_moves_only_the_files_whose_selected_source_it_produced(tmp_path, monkeypatch):
    lab_ = await lab(tmp_path, monkeypatch)
    repository, engine, executor, transfer = lab_.repository, lab_.engine, lab_.executor, lab_.transfer
    roots = await roots_by_link(repository, transfer.id)
    part01 = await file_of(repository, transfer.id, "part01.rar")
    part02 = await file_of(repository, transfer.id, "part02.rar")
    extra = await file_of(repository, transfer.id, "extra.bin")
    mirror = next(item for item in part01.candidates if item.provider_id == "prov-dl")   # pixeldrain part01
    kept = {item.id: (item.execution.attempt_id, str(item.candidates[item.selected].id)) for item in (part02, extra)}
    old = part01.candidates[part01.selected]

    application = application_of(engine)
    from application.manual_route_switch import switch_route_provider
    result = await switch_route_provider(application, transfer.id, "prov-dl", expected_provider_id="prov-ad",
                                         artifact_id=part01.id, expected_root_id=roots[RG1])
    assert (result["root_id"], result["artifact_ids"]) == (roots[RG1], [part01.id])
    held = await file_of(repository, transfer.id, "part01.rar")
    assert held.execution is None and executor.jobs[part01.execution.attempt_id].state == ExecutionState.CANCELLED
    assert (await repository.materialization_authorization(held)).kind == MaterializationAdmissionKind.HOLD
    for item in (part02, extra):                                       # untouched writers keep running
        assert executor.jobs[item.execution.attempt_id].state == ExecutionState.RUNNING

    await settle(engine)
    part01 = await file_of(repository, transfer.id, "part01.rar")
    selected = part01.candidates[part01.selected]
    assert selected.provider_id == "prov-dl" and selected.id not in {old.id, mirror.id}
    assert [item.id for item in part01.candidates] == [selected.id, mirror.id]   # the replaced place, mirror kept
    assert part01.execution is not None and executor.jobs[part01.execution.attempt_id].state == ExecutionState.RUNNING
    for item in (part02, extra):
        current = await file_of(repository, transfer.id, item.name)
        assert (current.execution.attempt_id, str(current.candidates[current.selected].id)) == kept[item.id]
    # The replaced binding stays as non-current history with its origin; the
    # mirror's binding and origin are untouched.
    bindings = {row["candidate_id"]: row for row in await rows(
        "SELECT * FROM canonical_candidate_bindings WHERE canonical_artifact_id=?", (part01.id,))}
    assert bindings[str(old.id)]["candidate_order"] > 100000
    assert (bindings[str(selected.id)]["candidate_order"], bindings[str(mirror.id)]["candidate_order"]) == (1, 2)
    origins = {(row["binding_id"], row["request_id"]) for row in await rows(
        "SELECT binding_id,request_id FROM canonical_candidate_origins")}
    assert {(bindings[str(old.id)]["id"], roots[RG1]), (bindings[str(selected.id)]["id"], roots[RG1]),
            (bindings[str(mirror.id)]["id"], roots[PD1])} <= origins
    # Route History: the replaced route released, the operator's route adopted.
    attempts = [(row["provider_id"], row["state"], row["operation"]) for row in await rows(
        """SELECT a.provider_id,a.state,p.operation FROM route_attempt_provenance p
           JOIN resolution_attempts a ON a.id=p.resolution_attempt_id WHERE a.request_id=? ORDER BY p.ordinal""",
        (roots[RG1],))]
    assert attempts == [("prov-ad", "released", "resolve"), ("prov-dl", "succeeded", "operator_switch")]
    events = {row["event_type"]: row["detail"] for row in await rows(
        "SELECT event_type,detail FROM event_journal WHERE transfer_id=?", (transfer.id,))}
    assert f"#{part01.id}" in events["routing.root_switched"] and roots[RG1] in events["routing.root_switched"]
    assert "prov-dl" in events["routing.root_candidates_replaced"]
    # Only the switched root resolved again.
    assert lab_.providers["prov-dl"].resolved.count(RG1) == 1
    assert {RG2, PD1, PD2, FICHIER}.isdisjoint(lab_.providers["prov-dl"].resolved[-1:])


async def test_the_hold_survives_a_restart_between_the_commit_and_the_new_resolution(tmp_path, monkeypatch):
    """part02 is OWNED by its rapidgator request but selects its pixeldrain
    mirror: only the canonical hold (every origin of the selected candidate
    belongs to a released route) keeps the replaced pixeldrain route's
    candidate from running again, across a restart, until the root lands."""
    lab_ = await lab(tmp_path, monkeypatch)
    repository, engine, transfer = lab_.repository, lab_.engine, lab_.transfer
    roots = await roots_by_link(repository, transfer.id)
    part02 = await file_of(repository, transfer.id, "part02.rar")
    old = part02.candidates[part02.selected]
    assert part02.request_id == roots[RG2] and old.provider_id == "prov-dl"
    lab_.providers["prov-tb"].failing.add(PD2)                       # the new route cannot resolve yet
    await switch_root_provider(engine, transfer.id, "prov-tb", expected_provider_id="prov-dl",
                               artifact_id=part02.id, expected_root_id=roots[PD2])

    # A restart: a new repository and engine over the same durable state.
    restarted_repository = TransferRepository()
    executor = MemoryExecutor(restarted_repository.authorize_execution)
    registry = IntegrationRegistry()
    for provider in lab_.providers.values():
        registry.register_provider(provider)
    registry.register_executor(executor)
    restarted = TransferEngine(restarted_repository, registry, download_root=str(tmp_path / "dl"),
                               policy=TransferPolicy(retry_delay=0.0, max_attempts=3), clock=engine.clock)
    await restarted.initialize()
    held = await file_of(restarted_repository, transfer.id, "part02.rar")
    assert (await restarted_repository.materialization_authorization(held)).kind == MaterializationAdmissionKind.HOLD
    await settle(restarted, 3)
    held = await file_of(restarted_repository, transfer.id, "part02.rar")
    assert held.execution is None and held.candidates[held.selected].id == old.id   # nothing ran the old source
    assert not any(call[0] == "start" and call[1].correlation.get("destination") == held.target
                   for call in executor.calls)

    lab_.providers["prov-tb"].failing.clear()
    await restarted_repository.retry_requests(transfer.id, request_id=roots[PD2])
    await settle(restarted)
    landed = await file_of(restarted_repository, transfer.id, "part02.rar")
    assert [item.provider_id for item in landed.candidates] == ["prov-ad", "prov-tb"]   # rapidgator kept first
    assert landed.candidates[landed.selected].provider_id == "prov-tb" and landed.execution is not None


async def test_a_size_mismatch_at_landing_never_selects_the_mismatched_candidate(tmp_path, monkeypatch):
    lab_ = await lab(tmp_path, monkeypatch)
    repository, engine, transfer = lab_.repository, lab_.engine, lab_.transfer
    roots = await roots_by_link(repository, transfer.id)
    part01 = await file_of(repository, transfer.id, "part01.rar")
    before = [item.id for item in part01.candidates]
    lab_.providers["prov-dl"].sizes[RG1] = 4096                         # contradicts the file's known 4 bytes
    await switch_root_provider(engine, transfer.id, "prov-dl", expected_provider_id="prov-ad",
                               artifact_id=part01.id, expected_root_id=roots[RG1])
    await settle(engine)
    part01 = await file_of(repository, transfer.id, "part01.rar")
    assert [item.id for item in part01.candidates] == before
    assert all(item.provider_id != "prov-dl" or item.id == before[1] for item in part01.candidates)
    assert part01.execution is None
    root = next(item for item in await repository.requests(transfer.id) if item.id == roots[RG1])
    assert root.error is not None and root.error.category == Category.SIZE_MISMATCH
    current = await rows("""SELECT b.candidate_id FROM canonical_candidate_bindings b
        WHERE b.canonical_artifact_id=? AND b.candidate_order<=100000""", (part01.id,))
    assert sorted(row["candidate_id"] for row in current) == sorted(str(item) for item in before)


async def test_a_landing_the_canonical_owner_refuses_as_a_conflict_fails_the_root_and_retry_recovers(
        tmp_path, monkeypatch):
    """``replace_root_candidates`` answers ``"conflict"`` when the
    replacement's issuing attempt has no durable provenance: nothing can be
    bound. That is the root's own failure through the ordinary request
    failure, as a size mismatch is -- never a "decided" landing that leaves the
    root ``materializing`` with nothing landed, which every later cycle would
    meet again (a silent livelock). The refusal is injected at the canonical
    owner's boundary; once it stops, an operator Retry lands the switch."""
    lab_ = await lab(tmp_path, monkeypatch)
    repository, engine, transfer = lab_.repository, lab_.engine, lab_.transfer
    roots = await roots_by_link(repository, transfer.id)
    part01 = await file_of(repository, transfer.id, "part01.rar")
    before = [item.id for item in part01.candidates]
    refused = []

    async def conflict(record, candidates):
        refused.append(record.id)
        return "conflict"

    engine.canonical.replace_root_candidates = conflict
    await switch_root_provider(engine, transfer.id, "prov-dl", expected_provider_id="prov-ad",
                               artifact_id=part01.id, expected_root_id=roots[RG1])
    await settle(engine, 12)
    assert refused and set(refused) == {roots[RG1]}
    root = next(item for item in await repository.requests(transfer.id) if item.id == roots[RG1])
    assert root.state != "materializing"
    assert root.error is not None and root.error.category == Category.RECOVERY_FAILED
    held = await file_of(repository, transfer.id, "part01.rar")
    assert [item.id for item in held.candidates] == before and held.execution is None
    assert held.error is not None                    # the root's failure is visible on the file it supplied

    del engine.canonical.replace_root_candidates
    assert await engine.retry(transfer.id)
    await settle(engine, 10)
    landed = await file_of(repository, transfer.id, "part01.rar")
    selected = landed.candidates[landed.selected]
    assert selected.provider_id == "prov-dl" and selected.id not in before
    assert landed.error is None and landed.execution is not None


async def test_a_held_file_cancelled_before_its_switch_lands_is_never_touched_by_the_landing(tmp_path, monkeypatch):
    """A cancelled artifact is never touched. Cancelling the file a switch
    holds ends the file, not the switch: the root's later resolution still
    takes the landing path (``route_replacement_pending``), where the
    canonical owner leaves the cancelled file exactly as it is and marks the
    root resolved -- never the ordinary rebuild of the root's own artifact,
    which replaced the cancelled file's candidates and pushed the pixeldrain
    mirror's binding (a source still supplied) out of the current band."""
    lab_ = await lab(tmp_path, monkeypatch)
    repository, engine, transfer = lab_.repository, lab_.engine, lab_.transfer
    roots = await roots_by_link(repository, transfer.id)
    part01 = await file_of(repository, transfer.id, "part01.rar")
    lab_.providers["prov-dl"].failing.add(RG1)                       # the new route cannot resolve yet
    await switch_root_provider(engine, transfer.id, "prov-dl", expected_provider_id="prov-ad",
                               artifact_id=part01.id, expected_root_id=roots[RG1])
    await settle(engine, 2)
    await engine.cancel_artifact(transfer.id, part01.id)

    async def facts():
        current = await file_of(repository, transfer.id, "part01.rar")
        return (current.state, current.selected, [str(item.id) for item in current.candidates],
                current.execution, [(row["id"], row["candidate_id"], row["provider_id"], row["candidate_order"])
                                    for row in await rows(
                    """SELECT b.id,b.candidate_id,b.provider_id,b.candidate_order FROM canonical_candidate_bindings b
                       WHERE b.canonical_artifact_id=? ORDER BY b.id""", (part01.id,))])

    cancelled = await facts()
    assert cancelled[0] == "cancelled" and cancelled[3] is None
    mirror = next(str(item.id) for item in part01.candidates if item.provider_id == "prov-dl")
    assert (mirror, "prov-dl", 2) in [row[1:] for row in cancelled[4]]   # PD1 supplies it, current

    lab_.providers["prov-dl"].failing.clear()
    await repository.retry_requests(transfer.id, request_id=roots[RG1])
    await settle(engine)

    assert lab_.providers["prov-dl"].resolved.count(RG1) >= 1          # the root did resolve again
    assert await facts() == cancelled
    root = next(item for item in await repository.requests(transfer.id) if item.id == roots[RG1])
    assert root.state == "resolved" and root.error is None


async def test_reacquiring_a_held_file_cancelled_before_its_switch_landed_acquires_through_the_switched_route(
        tmp_path, monkeypatch):
    """Cancellation is terminal until an explicit revival. The operator's
    Retry of the deleted transfer (reacquisition) revives the cancelled file
    with its pre-switch selection, which the switch holds; its root already
    resolved when the landing left the cancelled file as it was, so
    reacquisition reopens that root: it resolves again on its current route
    (the operator's provider, never the superseded one) and the landing takes
    the replaced candidate's place. The mirror keeps its binding, the finished
    siblings keep their selections and are not acquired again, and no other
    root resolves."""
    lab_ = await lab(tmp_path, monkeypatch)
    repository, engine, executor, transfer = lab_.repository, lab_.engine, lab_.executor, lab_.transfer
    roots = await roots_by_link(repository, transfer.id)
    part01 = await file_of(repository, transfer.id, "part01.rar")
    old = part01.candidates[part01.selected]
    mirror = next(str(item.id) for item in part01.candidates if item.provider_id == "prov-dl")
    lab_.providers["prov-dl"].failing.add(RG1)
    await switch_root_provider(engine, transfer.id, "prov-dl", expected_provider_id="prov-ad",
                               artifact_id=part01.id, expected_root_id=roots[RG1])
    await settle(engine, 2)
    await engine.cancel_artifact(transfer.id, part01.id)
    lab_.providers["prov-dl"].failing.clear()
    await repository.retry_requests(transfer.id, request_id=roots[RG1])
    await settle(engine)
    for item in await repository.artifacts(transfer.id):
        if item.execution is not None and item.state != "cancelled":
            executor.finish(item.execution)
    await settle(engine)
    siblings = {item.name: (item.state, item.execution.attempt_id, str(item.candidates[item.selected].id))
                for item in await repository.artifacts(transfer.id) if item.name != "part01.rar"}
    assert {state for state, _, _ in siblings.values()} == {"completed"}
    assert (await file_of(repository, transfer.id, "part01.rar")).state == "cancelled"   # terminal until revived
    await engine.delete(transfer.id, remote=False)
    resolved = {identity: len(provider.resolved) for identity, provider in lab_.providers.items()}

    async def route_history(request_id):
        return [(row["provider_id"], row["state"], row["operation"]) for row in await rows(
            """SELECT a.provider_id,a.state,p.operation FROM route_attempt_provenance p
               JOIN resolution_attempts a ON a.id=p.resolution_attempt_id WHERE a.request_id=? ORDER BY p.ordinal""",
            (request_id,))]

    history = await route_history(roots[RG1])
    assert history[-1][:2] == ("prov-dl", "succeeded")

    assert await engine.retry(transfer.id, reacquire=True)
    await settle(engine)

    revived = await file_of(repository, transfer.id, "part01.rar")
    selected = revived.candidates[revived.selected]
    assert selected.provider_id == "prov-dl" and str(selected.id) not in {str(old.id), mirror}
    assert [str(item.id) for item in revived.candidates] == [str(selected.id), mirror]
    assert revived.execution is not None and executor.jobs[revived.execution.attempt_id].state == ExecutionState.RUNNING
    assert (await repository.materialization_authorization(revived)).kind == MaterializationAdmissionKind.PROCEED
    # Only the switched root resolved again, and only on its current route.
    assert {identity: provider.resolved[resolved[identity]:] for identity, provider in lab_.providers.items()} == {
        "prov-ad": [], "prov-dl": [RG1], "prov-tb": []}
    # Its bound route (``bound_route_provider``: the latest attempt) is the
    # operator's; reacquisition opened exactly one more attempt on it.
    assert (await route_history(roots[RG1]))[len(history):] == [("prov-dl", "succeeded", "resolve")]
    bindings = {row["candidate_id"]: row["candidate_order"] for row in await rows(
        "SELECT candidate_id,candidate_order FROM canonical_candidate_bindings WHERE canonical_artifact_id=?",
        (part01.id,))}
    assert bindings[str(old.id)] > 100000 and (bindings[str(selected.id)], bindings[mirror]) == (1, 2)
    assert {item.name: (item.state, item.execution.attempt_id, str(item.candidates[item.selected].id))
            for item in await repository.artifacts(transfer.id) if item.name != "part01.rar"} == siblings

    executor.finish(revived.execution)
    await settle(engine)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED


# -- one link that is not a torrent: the picker opens directly --------------------------------------------------

async def test_a_single_link_root_switches_through_its_picker(tmp_path, monkeypatch):
    lab_ = await lab(tmp_path, monkeypatch, links=(RG1,))
    repository, engine, executor, transfer = lab_.repository, lab_.engine, lab_.executor, lab_.transfer
    row = await listed(lab_)
    assert (row["route_switch_available"], row["route_switch_scope"]) == (True, "root")
    assert "route_provider_id" not in row                             # the normal active-provider label stays
    status = await route_status(engine, transfer.id)
    assert offered(status) == {"prov-ad": ("current", False), "prov-dl": ("available", True)} and status["switchable"]
    assert await route_providers(engine, transfer.id) is None          # readiness promotion stays torrent-only
    old = (await repository.artifacts(transfer.id))[0]

    await switch_root_provider(engine, transfer.id, "prov-dl", expected_provider_id="prov-ad",
                               expected_root_id=status["root_id"])
    await settle(engine)
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.id == old.id and artifact.target == old.target
    assert artifact.candidates[artifact.selected].provider_id == "prov-dl"
    assert executor.jobs[artifact.execution.attempt_id].state == ExecutionState.RUNNING
    assert executor.jobs[old.execution.attempt_id].state == ExecutionState.CANCELLED
    assert (await route_status(engine, transfer.id))["current_provider_id"] == "prov-dl"


# -- several decomposed roots: neither offered nor switched in 1.0.13 --------------------------------------------

async def test_decomposed_roots_of_a_transfer_of_several_roots_are_never_switched(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "decomposed.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    providers = {identity: magnet_provider(identity) for identity in ("parcel-a", "parcel-b")}
    for provider in providers.values():
        registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3), clock=Clock())
    await engine.initialize()
    offer(providers["parcel-a"], FILES, native="x")
    offer(providers["parcel-a"], [("three.bin", "Other/three.bin", 4)], native="y")
    second = "magnet:?xt=urn:btih:" + "b" * 40
    transfer = await engine.submit((TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40, name="Show"),
                                    TransferRequest("magnet", second, name="Other")), name="Pair", deduplicate=False)
    await settle(engine)
    roots = await repository.root_route_facts_by_root(transfer.id)
    assert len(roots) == 2 and all(facts["decomposed"] and facts["relevant"] for facts in roots.values())
    assert not any(root_switches(registry, roots).values())
    assert route_switch_offer(registry, roots) is None
    lab_ = SimpleNamespace(engine=engine, repository=repository, transfer=transfer)
    assert "route_switch_scope" not in await listed(lab_)
    member = (await repository.artifacts(transfer.id))[0]
    root_id = next(root_id for root_id, facts in roots.items() if member.id in facts["artifacts"])
    assert not (await route_status(engine, transfer.id, member.id))["switchable"]
    with pytest.raises(TransferError) as refused:
        await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a",
                                   artifact_id=member.id, expected_root_id=root_id)
    assert refused.value.error.category == Category.UNSUPPORTED_REQUEST
    assert (await repository.latest_root_route(root_id))["provider_id"] == "parcel-a"


# -- the bounded list stays bounded; one renderer owns every launcher --------------------------------------------

async def test_the_bounded_list_reads_every_roots_facts_without_per_transfer_connections(tmp_path, monkeypatch):
    lab_ = await lab(tmp_path, monkeypatch)

    async def acquires_for_list():
        before = database.db_runtime_metrics()["sqlite_acquires"]
        await listed(lab_)
        return database.db_runtime_metrics()["sqlite_acquires"] - before

    one = await acquires_for_list()
    for index in range(3):
        await lab_.engine.submit(tuple(TransferRequest("https", f"{link}?copy={index}") for link in (RG1, PD1, FICHIER)),
                                 name=f"more-{index}", deduplicate=False)
    await settle(lab_.engine, 4)
    assert await acquires_for_list() == one


def test_every_root_provider_launcher_has_one_renderer_and_one_route_caller():
    from pathlib import Path
    static = Path(__file__).resolve().parents[2] / "frontend" / "static"
    owner = (static / "ui-root-provider.js").read_text()
    rows = (static / "ui-detail-candidates.js").read_text()
    app = (static / "app.js").read_text()
    # The list chip delegates for a torrent root AND for a per-root switch.
    chip = app.split("function providerChip(t, surface) {", 1)[1].split("\n}\n", 1)[0]
    assert "'route_switch_scope'" in chip and "window.DPRootProvider.badgeMarkup(t, surface, provider)" in chip
    # Details rows place the owner's per-file launcher; they render no provider
    # themselves and never call the route endpoint.
    assert "window.DPRootProvider.artifactBadgeMarkup(detail, file)" in rows
    assert "/route" not in rows and "route_root" not in rows
    assert "artifactBadgeMarkup: artifactBadgeMarkup" in owner
    # The several-roots launcher opens the transfer's provider picker itself
    # (B1 supersedes D-T2a): it never navigates to Details, and its one
    # transfer-wide action is the backend's per-root composite.
    assert "openFiles" not in owner and "showDetail(transferId)" not in owner.split("async function refreshSurfaces", 1)[0]
    assert "(scope === 'transfer' ? '/transfer'" in owner
    assert "'/api/torrents/' + transferId + '/route/transfer'" in owner
    assert "body: JSON.stringify({provider_id: providerId, expected_routes: expectedRoutes})" in owner
    # The picker and the switch name the file and the root the operator saw.
    assert "'?artifact_id=' + encodeURIComponent(artifactId)" in owner
    assert "body.artifact_id = Number(subject.artifactId)" in owner
    assert "body.expected_root_id = String(subject.rootId)" in owner


async def test_a_released_route_that_no_operator_switch_replaced_holds_nothing_and_lands_nothing(tmp_path,
                                                                                                monkeypatch):
    """An operator Retry releases a root's exhausted route too (a new routing
    campaign). Only an ``operator_switch`` is the replacement's durable fact:
    a released later attempt of part01's root, with no switch, neither holds
    part01 nor makes the root's next resolution land in place (the switch
    test above proves a switch does both)."""
    lab_ = await lab(tmp_path, monkeypatch)
    repository, transfer = lab_.repository, lab_.transfer
    roots = await roots_by_link(repository, transfer.id)
    part01 = await file_of(repository, transfer.id, "part01.rar")
    async with get_db() as db:
        (ordinal,) = await db.fetchone("SELECT COALESCE(MAX(ordinal),0)+1 FROM route_attempt_provenance "
                                       "WHERE transfer_id=?", (transfer.id,))
        await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) "
                         "VALUES('retried-ad',?,'prov-ad','released')", (roots[RG1],))
        await db.execute("INSERT INTO route_attempt_provenance(resolution_attempt_id,transfer_id,request_id,ordinal,"
                         "operation) VALUES('retried-ad',?,?,?,'resolve')", (transfer.id, roots[RG1], ordinal))
        await db.commit()
    assert (await repository.materialization_authorization(part01)).kind != MaterializationAdmissionKind.HOLD
    assert not await repository.route_replacement_pending(roots[RG1])


@pytest.mark.parametrize("links", [(RG1, PD1, RG2, PD2, FICHIER), (RG1,)], ids=["multi-root", "single-link"])
async def test_a_switched_root_that_fails_on_its_new_route_puts_its_files_in_error_and_retry_recovers(
        tmp_path, monkeypatch, links):
    """The switched root fails for good on its new route (every claimant
    refuses): nothing can land, so the files it supplied are not left held and
    unseen -- they take the root's own error. An operator Retry then starts a
    new routing campaign whose resolution lands exactly as the switch would
    have, and the file downloads again."""
    lab_ = await lab(tmp_path, monkeypatch, links=links)
    repository, engine, transfer = lab_.repository, lab_.engine, lab_.transfer
    roots = await roots_by_link(repository, transfer.id)
    part01 = await file_of(repository, transfer.id, "part01.rar")
    for provider in ("prov-dl", "prov-ad"):
        lab_.providers[provider].failing.add(RG1)
    await switch_root_provider(engine, transfer.id, "prov-dl", expected_provider_id="prov-ad",
                               artifact_id=part01.id if len(links) > 1 else None, expected_root_id=roots[RG1])
    await settle(engine, 40)
    failed = await file_of(repository, transfer.id, "part01.rar")
    assert failed.state == ("error" if len(links) > 1 else "unresolved") and failed.error is not None
    assert failed.error.category == Category.PROVIDER_UNAVAILABLE
    # A sole source fails the transfer visibly; a multi-source one keeps its
    # other files downloading while this file shows the error.
    assert (await repository.get(transfer.id)).state == (
        TransferState.TRANSFERRING if len(links) > 1 else TransferState.FAILED)
    for provider in ("prov-dl", "prov-ad"):
        lab_.providers[provider].failing.clear()
    assert await engine.retry(transfer.id)
    await settle(engine, 10)
    recovered = await file_of(repository, transfer.id, "part01.rar")
    assert recovered.state in {"downloading", "completed"} and recovered.error is None
    assert recovered.candidates[recovered.selected].provider_id in {"prov-dl", "prov-ad"}


async def test_a_root_whose_source_another_transfer_consolidated_onto_is_never_switched(tmp_path, monkeypatch):
    """Ownership boundary: the switch fences only its own transfer's files. A
    file of ANOTHER transfer holds a current candidate issued by part01's
    rapidgator root (a cross-transfer consolidation onto that source), so that
    root is neither offered nor switched: the refusal comes before any claim,
    writer retirement or route change, and the other transfer's file keeps its
    bindings, selection, state and running writer. Control: without that
    consolidation, the same switch of the same root succeeds."""
    lab_ = await lab(tmp_path, monkeypatch)
    repository, engine, executor, transfer = lab_.repository, lab_.engine, lab_.executor, lab_.transfer
    roots = await roots_by_link(repository, transfer.id)
    part01 = await file_of(repository, transfer.id, "part01.rar")
    other = await engine.submit((TransferRequest("https", "https://1fichier.com/x/other.bin"),), name="other",
                                deduplicate=False)
    await settle(engine)
    foreign = await file_of(repository, other.id, "other.bin")
    assert foreign.execution is not None and executor.jobs[foreign.execution.attempt_id].state == ExecutionState.RUNNING
    (issued,) = await rows("""SELECT a.id FROM resolution_attempts a WHERE a.request_id=? AND a.state='succeeded'
                              ORDER BY a.rowid DESC LIMIT 1""", (roots[RG1],))
    async with get_db() as db:
        await db.execute(
            """INSERT INTO canonical_candidate_bindings(canonical_artifact_id,candidate_id,provider_id,role,
               candidate_order) VALUES(?,?,?,'alternate',2)""", (foreign.id, "consolidated-part01", "prov-ad"))
        binding_id = (await db.fetchone("SELECT id FROM canonical_candidate_bindings WHERE candidate_id=?",
                                        ("consolidated-part01",)))["id"]
        await db.execute(
            """INSERT INTO canonical_candidate_origins(binding_id,contributing_artifact_id,contributing_transfer_id,
               request_id,resolution_attempt_id,discovered_candidate_id) VALUES(?,?,?,?,?,?)""",
            (binding_id, part01.id, transfer.id, roots[RG1], issued["id"], str(part01.candidates[part01.selected].id)))
        await db.commit()

    async def snapshot():
        current = await file_of(repository, other.id, "other.bin")
        return (current.state, current.selected, [str(item.id) for item in current.candidates],
                current.execution.attempt_id if current.execution else None,
                [dict(row) for row in await rows(
                    "SELECT * FROM canonical_candidate_bindings WHERE canonical_artifact_id=? ORDER BY id",
                    (foreign.id,))])

    foreign_before = await snapshot()
    routes_before = await rows("SELECT id,state FROM resolution_attempts WHERE request_id=? ORDER BY rowid",
                               (roots[RG1],))
    offers = root_switches(engine.registry, await repository.root_route_facts_by_root(transfer.id))
    assert not offers.get(roots[RG1])                                          # never offered
    application = application_of(engine)
    from application.manual_route_switch import switch_route_provider
    with pytest.raises(TransferError) as refused:
        await switch_route_provider(application, transfer.id, "prov-dl", expected_provider_id="prov-ad",
                                    artifact_id=part01.id, expected_root_id=roots[RG1])
    assert refused.value.error.category == Category.UNSUPPORTED_REQUEST
    assert await snapshot() == foreign_before                                  # bindings, selection, state, writer
    assert executor.jobs[foreign.execution.attempt_id].state == ExecutionState.RUNNING
    held = await file_of(repository, transfer.id, "part01.rar")                # nothing retired or held either
    assert held.execution is not None and held.execution.attempt_id == part01.execution.attempt_id
    assert executor.jobs[part01.execution.attempt_id].state == ExecutionState.RUNNING
    assert await rows("SELECT id,state FROM resolution_attempts WHERE request_id=? ORDER BY rowid",
                      (roots[RG1],)) == routes_before

    # Control: the same switch of the same root, once no other transfer holds its source.
    async with get_db() as db:
        await db.execute("DELETE FROM canonical_candidate_origins WHERE binding_id=?", (binding_id,))
        await db.execute("DELETE FROM canonical_candidate_bindings WHERE id=?", (binding_id,))
        await db.commit()
    result = await switch_route_provider(application, transfer.id, "prov-dl", expected_provider_id="prov-ad",
                                         artifact_id=part01.id, expected_root_id=roots[RG1])
    assert (result["root_id"], result["artifact_ids"]) == (roots[RG1], [part01.id])
    held = await file_of(repository, transfer.id, "part01.rar")
    assert (await repository.materialization_authorization(held)).kind == MaterializationAdmissionKind.HOLD
    assert executor.jobs[foreign.execution.attempt_id].state == ExecutionState.RUNNING


# -- candidate refresh never re-points a selection by position (B5-C2, probe 13) ---------------------------------

FICHIER01 = "https://1fichier.com/x/part01.rar"


async def _three_mirrors_selecting_the_middle(tmp_path, monkeypatch):
    """part01 offered by three mirrors [rapidgator (ad), pixeldrain (dl),
    1fichier (ad)], the operator having selected the middle one: its writer
    runs."""
    lab_ = await lab(tmp_path, monkeypatch, links=(RG1, PD1, FICHIER01))
    part01 = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    middle = next(i for i, item in enumerate(part01.candidates) if item.provider_id == "prov-dl")
    assert middle == 1
    assert (await lab_.engine.activate_candidate_command(lab_.transfer.id, part01.id, middle)).committed
    await settle(lab_.engine)
    return lab_, await file_of(lab_.repository, lab_.transfer.id, "part01.rar")


async def _refresh(lab_, artifact, old, replacement):
    """The origin request's own provider records the refreshed route, then the
    canonical owner applies it (``CanonicalOwnership.refresh_candidate``)."""
    origin = await lab_.engine.canonical.origin_for(artifact, old)
    attempt = await lab_.repository.begin_refresh(origin.request, origin.provider_id)
    await lab_.repository.resolution(attempt, ResolutionResult(ResourceState.AVAILABLE, (replacement,)))
    return await lab_.engine.canonical.refresh_candidate(artifact, origin, old, [replacement])


async def _selection(lab_, artifact_id):
    current = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    binding = await rows("SELECT candidate_id FROM canonical_candidate_bindings WHERE canonical_artifact_id=? "
                         "AND candidate_order=?", (artifact_id, current.selected + 1))
    return current, str(current.candidates[current.selected].id), [str(row["candidate_id"]) for row in binding]


async def test_a_refresh_that_drops_a_candidate_before_the_selection_never_repoints_a_running_writer(
        tmp_path, monkeypatch):
    """Probe 13: a refreshed route coalesces into another existing binding and
    is dropped from a position BEFORE the selected candidate, while that
    candidate's writer runs. The selection is an identity: it still names
    the writer's candidate (and so does the selected binding), never the
    candidate that slid into its old position."""
    lab_, part01 = await _three_mirrors_selecting_the_middle(tmp_path, monkeypatch)
    writing = str(part01.candidates[part01.selected].id)
    writer = part01.execution.attempt_id
    first, last = part01.candidates[0], part01.candidates[2]
    assert await _refresh(lab_, part01, first, replace(last))          # coalesces into the 1fichier binding
    current, selected, binding = await _selection(lab_, part01.id)
    assert len(current.candidates) == 2
    assert selected == writing and binding == [writing]
    assert current.execution.attempt_id == writer and lab_.executor.jobs[writer].state == ExecutionState.RUNNING


async def test_a_refresh_that_coalesces_the_selected_candidate_selects_what_it_coalesced_into(tmp_path, monkeypatch):
    """The refreshed route is the selected candidate's own (the first,
    rapidgator) and coalesces into an existing binding further on (1fichier):
    the selection follows it there, never to the candidate that slid into the
    selected position (pixeldrain)."""
    lab_ = await lab(tmp_path, monkeypatch, links=(RG1, PD1, FICHIER01))
    part01 = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    assert part01.selected == 0
    first, last = part01.candidates[0], part01.candidates[2]
    assert await _refresh(lab_, part01, first, replace(last))
    current, selected, binding = await _selection(lab_, part01.id)
    assert [str(item.id) for item in current.candidates] == [str(part01.candidates[1].id), str(last.id)]
    assert selected == str(last.id) and binding == [str(last.id)]


async def test_a_selected_binding_whose_candidate_id_drifted_still_keeps_the_selection(tmp_path, monkeypatch):
    """The selected candidate's binding still carries an earlier candidate id
    (a re-resolution reused it by source): the rewrite resolves it by source,
    as every reader does, so the refresh neither drops the running writer's
    candidate nor strands itself on the missing id."""
    lab_, part01 = await _three_mirrors_selecting_the_middle(tmp_path, monkeypatch)
    writing = str(part01.candidates[part01.selected].id)
    writer = part01.execution.attempt_id
    binding_id, scope = (await rows(
        "SELECT id,source_scope FROM canonical_candidate_bindings WHERE canonical_artifact_id=? AND candidate_id=?",
        (part01.id, writing)))[0].values()
    assert scope                                                            # resolvable by its source
    async with database.get_db() as db:
        await db.execute("UPDATE canonical_candidate_bindings SET candidate_id='drifted' WHERE id=?", (binding_id,))
        await db.commit()
    first = part01.candidates[0]
    assert await _refresh(lab_, part01, first, replace(first, id="fresh-" + str(first.id)))
    current = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    assert len(current.candidates) == 3 and str(current.candidates[current.selected].id) == writing
    assert current.execution.attempt_id == writer and lab_.executor.jobs[writer].state == ExecutionState.RUNNING


async def test_an_in_place_refresh_keeps_the_selection(tmp_path, monkeypatch):
    """Negative control: a refreshed route that is new (a fresh candidate)
    takes the refreshed one's place; the selected candidate and its writer
    are untouched."""
    lab_, part01 = await _three_mirrors_selecting_the_middle(tmp_path, monkeypatch)
    writing = str(part01.candidates[part01.selected].id)
    first = part01.candidates[0]
    fresh = replace(first, id="fresh-" + str(first.id))
    assert await _refresh(lab_, part01, first, fresh)
    current, selected, binding = await _selection(lab_, part01.id)
    assert [str(item.id) for item in current.candidates][0] == str(fresh.id) and len(current.candidates) == 3
    assert selected == writing and binding == [writing]


async def test_the_engines_refresh_keeps_the_selection_its_canonical_owner_settled(tmp_path, monkeypatch):
    """The engine's own refresh of a ``refresh_pending`` selected candidate
    (``TransferEngine._refresh``): the provider's refreshed route coalesces
    into another existing binding, so the canonical owner selects that one
    -- and the engine then queues the artifact without writing back the
    selection's old position, which now names a different candidate."""
    lab_ = await lab(tmp_path, monkeypatch, links=(RG1, PD1, FICHIER01))
    part01 = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    assert part01.selected == 0 and part01.candidates[0].provider_id == "prov-ad"
    coalesced = part01.candidates[2]                                        # 1fichier, also prov-ad

    async def refresh(self, candidate):
        return ResolutionResult(ResourceState.AVAILABLE, (replace(coalesced),))

    monkeypatch.setattr(HostProvider, "refresh", refresh, raising=False)
    await lab_.repository.artifact_state(part01.id, "refresh_pending", release=True,
                                         expected_bytes=part01.candidates[0].expected_bytes)
    pending = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    await lab_.engine._refresh(pending)
    current, selected, binding = await _selection(lab_, part01.id)
    assert current.state == "queued" and [item.provider_id for item in current.candidates] == ["prov-dl", "prov-ad"]
    assert selected == str(coalesced.id) and binding == [str(coalesced.id)]
