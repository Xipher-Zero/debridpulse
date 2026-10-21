"""Transfer 1081 (live acceptance of ac0943f7): a link switched to a provider
that resolves links through a cloud copy (Premiumize's cloud and immediate
paths, TorBox web downloads) never downloaded after the provider finished.

On that provider the switched link root does not resolve to candidates
itself: its resource is PREPARING, then AVAILABLE, then decomposes into ONE
member request that resolves to the file. The landing that moves a held file
onto its switched root's replacement ran only for the root's own resolution,
so the member's candidate attached beside the file and the file stayed held
on the replaced candidate -- queued, with no writer, for ever. Meanwhile the
badge named the switched-away provider, the status said Queued, and once the
root had a member it was no longer switchable at all.

Every provider here is a neutral fixture; no provider identity decides
anything.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fake_integrations import MemoryExecutor
from test_v113_collection_route_generic_closure import Clock
from test_v113_multi_root_provider_switch import (
    RG1 as LAB_RG1, HostProvider, file_of, lab, listed, roots_by_link, rows, settle,
)

from db import database
from transfers.applicability import ApplicabilityReadiness, HostClaim, HostClaimScope, ProviderApplicability
from transfers.convergence_engine import TransferEngine
from transfers.errors import TransferError
from transfers.manual_route_switch import root_switches, switch_root_provider, transfer_route_status
from transfers.models import (
    Capability, Endpoint, ExecutionState, FileManifest, FileManifestEntry, IntegrationDescriptor, Ownership,
    ProviderObservation, ProviderResource,
    ResolutionResult, ResourceState, SourceEntry, TransferCandidate, TransferRequest,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

RG1 = "https://rapidgator.net/f/part01.rar"
FICHIER1 = "https://1fichier.com/x/part01.rar"


class CloudProvider:
    """A neutral provider that copies a link into its own cloud first: the
    root resolves to a PREPARING resource, becomes AVAILABLE when ``ready``,
    and its manifest is ONE member (the file) that resolves to a candidate."""

    def __init__(self, identity, hosts, files=None, prefix=""):
        # FILE_MANIFEST as the real cloud providers declare it: each binding
        # of a root opens its own decomposition generation, and the members it
        # fans out are stamped with that generation.
        self.descriptor = IntegrationDescriptor(
            identity, identity, frozenset({Capability.RESOLVE, Capability.RESOURCE_LOOKUP, Capability.METADATA,
                                           Capability.FILE_MANIFEST}),
            request_types=frozenset({"https", "cloud-member"}), priority=1)
        self.prefix = prefix
        self.applicability = ProviderApplicability(
            specialized_hosts=tuple(HostClaim(host, HostClaimScope.DOMAIN, frozenset({"https"})) for host in hosts),
            specialized=True, readiness=ApplicabilityReadiness.READY)
        self.ready = False
        self.files = files
        self.member_failure = None
        self.resources: dict[str, ProviderObservation] = {}
        self.calls: list = []

    def _names(self, request):
        return self.files or (request.name,)

    def _observation(self, resource, request):
        state = ResourceState.AVAILABLE if self.ready else ResourceState.PREPARING
        tree = FileManifest(tuple(FileManifestEntry(name, self.prefix + name, 4) for name in self._names(request)))
        return ProviderObservation(resource, state, request.name or "", request=request,
                                   file_manifest=tree if self.ready else None)

    async def resolve(self, request):
        self.calls.append(("resolve", str(request.payload)))
        if request.kind == "cloud-member":
            if self.member_failure is not None:
                raise TransferError(self.member_failure)
            name = str(request.payload).rsplit("/", 1)[-1]
            return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
                name, (Endpoint("memory", f"memory:{name}"),), expected_bytes=4, provider_id=self.descriptor.id),))
        name = str(request.payload).rsplit("/", 1)[-1]
        resource = ProviderResource(self.descriptor.id, {"cloud": name}, Ownership.CREATED,
                                    id=f"{self.descriptor.id}:{name}")
        observed = self._observation(resource, replace(request, name=name))
        self.resources[resource.id] = observed
        return ResolutionResult(observed.state, observation=observed)

    async def observe(self, resource):
        self.calls.append(("observe", resource.id))
        known = self.resources.get(resource.id)
        if known is None:
            return ProviderObservation(resource, ResourceState.ABSENT)
        return self._observation(resource, known.request)

    async def manifest(self, resource):
        self.calls.append(("manifest", resource.id))
        names = self._names(self.resources[resource.id].request)
        return tuple(SourceEntry(name, 4, self.prefix + name,
                                 TransferRequest("cloud-member", f"cloud://{self.descriptor.id}/{name}", name=name,
                                                 preferred_provider=self.descriptor.id))
                     for name in names)


async def cloud_lab(tmp_path, monkeypatch, *, second_cloud=False, second_prefix=""):
    """part01 offered by two links: rapidgator (selected, via prov-ad) and
    1fichier (an alternate, via prov-ad). prov-cloud also claims rapidgator."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "cloud-switch.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    ad = HostProvider("prov-ad")
    cloud = CloudProvider("prov-cloud", ("rapidgator.net",))
    registry.register_provider(ad)
    registry.register_provider(cloud)
    # A second cloud provider; ``second_prefix`` makes it name the file at
    # another coordinate than the first copy did.
    cloud2 = CloudProvider("prov-cloud2", ("rapidgator.net",), prefix=second_prefix) if second_cloud else None
    if cloud2 is not None:
        registry.register_provider(cloud2)
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3, resolution_concurrency=1),
                            clock=Clock())
    await engine.initialize()
    transfer = await engine.submit((TransferRequest("https", RG1), TransferRequest("https", FICHIER1)),
                                   name="1081", deduplicate=False)
    await settle(engine)
    return SimpleNamespace(repository=repository, registry=registry, engine=engine, executor=executor,
                           ad=ad, cloud=cloud, cloud2=cloud2, transfer=transfer)



async def switched_to_cloud(tmp_path, monkeypatch):
    lab_ = await cloud_lab(tmp_path, monkeypatch)
    roots = await roots_by_link(lab_.repository, lab_.transfer.id)
    part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    first = part.execution.attempt_id
    await switch_root_provider(lab_.engine, lab_.transfer.id, "prov-cloud", expected_provider_id="prov-ad",
                               artifact_id=part.id, expected_root_id=roots[RG1])
    await settle(lab_.engine, 4)
    return lab_, roots, first


async def detail_of(lab_):
    from api.routes import get_torrent
    application = SimpleNamespace(engine=lab_.engine, repository=lab_.repository, definitions={})
    return await get_torrent(lab_.transfer.id, application=application, request=None)


def running(lab_):
    """The attempt ids of every running writer."""
    return [attempt for attempt, job in lab_.executor.jobs.items() if job.state == ExecutionState.RUNNING]


async def test_while_the_new_provider_prepares_its_copy_the_badge_names_it_and_the_file_waits_for_it(
        tmp_path, monkeypatch):
    """B and C: the switch committed, the new provider is still preparing,
    nothing downloads. The list and Details name the committed provider,
    and the file and the transfer say Waiting for provider, not Queued."""
    lab_, _roots, first = await switched_to_cloud(tmp_path, monkeypatch)
    assert lab_.executor.jobs[first].state == ExecutionState.CANCELLED and not running(lab_)
    row = await listed(lab_)
    assert row["active_provider_id"] == "prov-cloud"
    assert (row["presentation_status"], row["presentation_label"]) == ("waiting_for_provider", "Waiting for provider")
    detail = await detail_of(lab_)
    assert detail["active_provider_id"] == "prov-cloud"
    assert detail["presentation_status"] == "waiting_for_provider"
    part = next(item for item in detail["file_presentations"] if item.get("filename") == "part01.rar")
    assert part["presentation_status"] == "waiting_for_provider"


async def test_a_refused_switch_leaves_the_badge_and_status_where_they_were(tmp_path, monkeypatch):
    """Control: a switch refused at its own admission changes neither the
    named provider nor the file's work."""
    lab_ = await cloud_lab(tmp_path, monkeypatch)
    roots = await roots_by_link(lab_.repository, lab_.transfer.id)
    part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    with pytest.raises(TransferError):
        await switch_root_provider(lab_.engine, lab_.transfer.id, "prov-cloud", expected_provider_id="prov-cloud",
                                   artifact_id=part.id, expected_root_id=roots[RG1])
    row = await listed(lab_)
    assert row["active_provider_id"] == "prov-ad" and row["presentation_status"] == "downloading"
    assert running(lab_) == [part.execution.attempt_id]


async def test_when_the_copy_is_ready_the_held_file_lands_on_it_and_downloads_with_one_writer(tmp_path, monkeypatch):
    """D (transfer 1081): the new provider finishes its copy and describes it
    as one member. The member's resolution lands in the switched root's
    replaced place: the file selects the new provider's candidate and starts
    on its own -- one writer, no Resume -- and no stray member row appears."""
    lab_, roots, first = await switched_to_cloud(tmp_path, monkeypatch)
    lab_.cloud.ready = True
    await settle(lab_.engine, 10)
    part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    assert part.state == "downloading" and part.candidates[part.selected].provider_id == "prov-cloud"
    assert running(lab_) == [part.execution.attempt_id] != [first]
    assert lab_.executor.jobs[first].state == ExecutionState.CANCELLED
    files = await rows("SELECT request_id FROM download_files")
    assert {row["request_id"] for row in files} == set(roots.values())            # the member has no row of its own
    row = await listed(lab_)
    assert (row["active_provider_id"], row["presentation_status"]) == ("prov-cloud", "downloading")


async def test_a_landed_link_stays_switchable_and_switches_back_with_one_writer(tmp_path, monkeypatch):
    """A: the link resolved through one member is still one link -- the badge
    keeps its picker -- and A -> B -> A holds the file, retires the cloud
    writer and lands on the first provider again."""
    lab_, roots, _first = await switched_to_cloud(tmp_path, monkeypatch)
    lab_.cloud.ready = True
    await settle(lab_.engine, 10)
    part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    cloud_writer = part.execution.attempt_id
    facts = await lab_.repository.root_route_facts_by_root(lab_.transfer.id)
    assert (facts[roots[RG1]]["decomposed"], facts[roots[RG1]]["members"]) == (True, 1)
    assert root_switches(lab_.engine.registry, facts)[roots[RG1]]
    assert (await listed(lab_))["route_switch_scope"] == "transfer"
    status = await transfer_route_status(lab_.engine, lab_.transfer.id)
    item = next(entry for entry in status["providers"] if entry["provider_id"] == "prov-ad")
    assert [(row["status"]) for row in item["roots"] if row["root_id"] == roots[RG1]] == ["eligible"]

    await switch_root_provider(lab_.engine, lab_.transfer.id, "prov-ad", expected_provider_id="prov-cloud",
                               artifact_id=part.id, expected_root_id=roots[RG1])
    held = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    assert held.execution is None
    assert (await lab_.repository.materialization_authorization(held)).kind.value == "hold"
    assert lab_.executor.jobs[cloud_writer].state == ExecutionState.CANCELLED
    await settle(lab_.engine, 8)
    part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    assert part.state == "downloading" and part.candidates[part.selected].provider_id == "prov-ad"
    assert running(lab_) == [part.execution.attempt_id]
    assert (await listed(lab_))["active_provider_id"] == "prov-ad"


async def test_a_switch_back_while_the_copy_prepares_fences_it_and_its_late_readiness_lands_nothing(
        tmp_path, monkeypatch):
    """A second switch during preparation: the cloud route is released and the
    file lands on the first provider. The cloud copy becoming ready later is
    never observed into this transfer: no member, no second writer."""
    lab_, roots, _first = await switched_to_cloud(tmp_path, monkeypatch)
    part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    await switch_root_provider(lab_.engine, lab_.transfer.id, "prov-ad", expected_provider_id="prov-cloud",
                               artifact_id=part.id, expected_root_id=roots[RG1])
    lab_.cloud.ready = True
    await settle(lab_.engine, 10)
    part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    assert part.state == "downloading" and part.candidates[part.selected].provider_id == "prov-ad"
    assert len(running(lab_)) == 1
    assert ("manifest", "prov-cloud:part01.rar") not in lab_.cloud.calls
    assert not await rows("SELECT id FROM transfer_requests WHERE parent_id IS NOT NULL")


async def test_a_restart_while_the_copy_prepares_still_lands_it(tmp_path, monkeypatch):
    """The hold and the landing are derived from stored facts: a new engine
    over the same database observes the copy, lands it and downloads."""
    lab_, _roots, _first = await switched_to_cloud(tmp_path, monkeypatch)
    repository = TransferRepository()
    executor = MemoryExecutor(repository.authorize_execution)
    lab_.registry.executors.clear()
    lab_.registry.register_executor(executor)
    engine = TransferEngine(repository, lab_.registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3, resolution_concurrency=1),
                            clock=lab_.engine.clock)
    await engine.initialize()
    lab_.cloud.ready = True
    await settle(engine, 10)
    part = await file_of(repository, lab_.transfer.id, "part01.rar")
    assert part.state == "downloading" and part.candidates[part.selected].provider_id == "prov-cloud"
    assert len([job for job in executor.jobs.values() if job.state == ExecutionState.RUNNING]) == 1


async def test_a_link_that_expanded_into_several_files_stays_unswitchable(tmp_path, monkeypatch):
    """Control: a link the provider describes as SEVERAL members expanded into
    several files of a multi-link transfer -- still never a switch subject."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "expanded.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    cloud = CloudProvider("prov-cloud", ("cloudonly.example",), files=("a.bin", "b.bin"))
    cloud.ready = True
    registry.register_provider(cloud)
    registry.register_provider(HostProvider("prov-ad"))
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3, resolution_concurrency=1),
                            clock=Clock())
    await engine.initialize()
    transfer = await engine.submit((TransferRequest("https", "https://cloudonly.example/f/folder"),
                                    TransferRequest("https", FICHIER1)), name="folder", deduplicate=False)
    await settle(engine, 10)
    facts = await repository.root_route_facts_by_root(transfer.id)
    folder = next(item for item in facts.values() if item["members"])
    assert folder["members"] == 2 and folder["relevant"]
    assert not root_switches(registry, facts).get(folder["root_id"])


async def test_superseded_candidates_are_history_not_choices(tmp_path, monkeypatch):
    """The ``⑂ N`` chooser (transfer 1081): a switch's landing keeps the
    replaced candidate as history in the non-current band. It is never offered
    as a source again, nor counted -- the operator saw the same host twice."""
    lab_ = await lab(tmp_path, monkeypatch)
    repository, engine, transfer = lab_.repository, lab_.engine, lab_.transfer
    roots = await roots_by_link(repository, transfer.id)
    part = await file_of(repository, transfer.id, "part01.rar")
    await switch_root_provider(engine, transfer.id, "prov-dl", expected_provider_id="prov-ad",
                               artifact_id=part.id, expected_root_id=roots[LAB_RG1])
    await settle(engine)
    history = await rows("SELECT candidate_id FROM canonical_candidate_bindings WHERE canonical_artifact_id=? "
                         "AND candidate_order>100000", (part.id,))
    assert history                                                                  # history is kept
    detail = await detail_of(lab_)
    offered = next(item for item in detail["files"] if item["id"] == part.id)["acquisition_candidates"]
    assert {item["candidate_id"] for item in offered}.isdisjoint({row["candidate_id"] for row in history})
    current = await rows("SELECT candidate_id FROM canonical_candidate_bindings WHERE canonical_artifact_id=? "
                         "AND candidate_order<=100000", (part.id,))
    assert sorted(item["candidate_id"] for item in offered) == sorted(row["candidate_id"] for row in current)
    assert len(offered) == len(current) == 2                                       # rapidgator (dl), pixeldrain



async def test_a_copy_whose_member_fails_for_good_puts_the_held_file_in_error(tmp_path, monkeypatch):
    """Review MAJOR-2: the new provider's copy is ready but its one member
    fails for good. Nothing can land, so the held file takes that error --
    visible, recoverable by Retry -- instead of waiting for the provider for
    ever."""
    from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage
    lab_, _roots, _first = await switched_to_cloud(tmp_path, monkeypatch)
    lab_.cloud.member_failure = NormalizedError(Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION,
                                                Retryability.NEVER, origin=Origin.PROVIDER,
                                                integration_id="prov-cloud")
    lab_.cloud.ready = True
    await settle(lab_.engine, 20)
    part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    assert part.state == "error" and part.error is not None
    assert part.error.category == Category.PROVIDER_UNAVAILABLE
    assert (await listed(lab_))["presentation_status"] != "waiting_for_provider"
    assert not running(lab_)


async def test_a_single_member_that_owns_its_own_file_stays_unswitchable(tmp_path, monkeypatch):
    """Review MAJOR-3: one member that owns a canonical file of its own (a
    directory's only file, a media member) is a decomposition the member's
    generation governs: the root stays excluded, exactly as before. Only a
    member serving the root's own file is a link switched as one."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "owned.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    cloud = CloudProvider("prov-cloud", ("cloudonly.example",))
    cloud.ready = True
    registry.register_provider(cloud)
    registry.register_provider(HostProvider("prov-ad"))
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3, resolution_concurrency=1),
                            clock=Clock())
    await engine.initialize()
    transfer = await engine.submit((TransferRequest("https", "https://cloudonly.example/f/only.bin"),
                                    TransferRequest("https", FICHIER1)), name="owned", deduplicate=False)
    await settle(engine, 10)
    facts = await repository.root_route_facts_by_root(transfer.id)
    owned = next(item for item in facts.values() if item["members"])
    assert (owned["members"], owned["member_owned"], owned["relevant"]) == (1, True, True)
    assert not root_switches(registry, facts).get(owned["root_id"])


async def through_two_clouds(tmp_path, monkeypatch, *, second_ready=True, second_prefix=""):
    """prov-ad -> prov-cloud (ready, landed) -> prov-cloud2: one file copied
    by two cloud providers. At the same coordinate, the second copy's
    generation rebuilds the same member request in place (strict
    decomposition continuity); at another, that generation is HELD."""
    lab_ = await cloud_lab(tmp_path, monkeypatch, second_cloud=True, second_prefix=second_prefix)
    roots = await roots_by_link(lab_.repository, lab_.transfer.id)
    part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    await switch_root_provider(lab_.engine, lab_.transfer.id, "prov-cloud", expected_provider_id="prov-ad",
                               artifact_id=part.id, expected_root_id=roots[RG1])
    lab_.cloud.ready = True
    await settle(lab_.engine, 10)
    part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    assert part.candidates[part.selected].provider_id == "prov-cloud"
    await switch_root_provider(lab_.engine, lab_.transfer.id, "prov-cloud2", expected_provider_id="prov-cloud",
                               artifact_id=part.id, expected_root_id=roots[RG1])
    lab_.cloud2.ready = second_ready
    await settle(lab_.engine, 10)
    return lab_, roots


async def historical_member(lab_, root_id):
    """A member an earlier generation of the root left behind at another
    coordinate (the generational supersede of a decomposition keeps it):
    stamped with the superseded generation, resolved, recorded for good."""
    from transfers import codec
    from transfers._repository_base import manifest_child_identity
    generations = await rows("SELECT id FROM transfer_file_selections WHERE request_id=? ORDER BY created_at",
                             (root_id,))
    member_id = manifest_child_identity(root_id, "Earlier/part01.rar")
    from db.database import get_db
    async with get_db() as db:
        await db.execute(
            "INSERT INTO transfer_requests(id,transfer_id,parent_id,ordinal,payload,state,materialized_selection_id) "
            "VALUES(?,?,?,?,?,'resolved',?)",
            (member_id, lab_.transfer.id, root_id, 99,
             codec.dump(TransferRequest("cloud-member", "cloud://earlier/part01.rar", name="part01.rar")),
             generations[0]["id"]))
        await db.commit()
    return member_id


async def test_a_file_copied_by_two_clouds_stays_one_link_and_switches_back(tmp_path, monkeypatch):
    """A -> cloud B -> cloud C at the same coordinate: the member request is
    rebuilt in place and C's copy lands. With an earlier generation's member
    also recorded, only the CURRENT generation's member is the root's file:
    the link stays switchable, participation stays truthful, the history
    stays recorded, and switching back to A lands with one writer."""
    lab_, roots = await through_two_clouds(tmp_path, monkeypatch)
    part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    assert part.state == "downloading" and part.candidates[part.selected].provider_id == "prov-cloud2"
    assert len(await rows("SELECT id FROM transfer_requests WHERE parent_id=?", (roots[RG1],))) == 1
    earlier = await historical_member(lab_, roots[RG1])
    facts = await lab_.repository.root_route_facts_by_root(lab_.transfer.id)
    assert (facts[roots[RG1]]["decomposed"], facts[roots[RG1]]["members"]) == (True, 1)
    assert root_switches(lab_.engine.registry, facts)[roots[RG1]]
    assert (await listed(lab_))["route_switch_scope"] == "transfer"
    status = await transfer_route_status(lab_.engine, lab_.transfer.id)
    by = {item["provider_id"]: item for item in status["providers"]}
    assert [row["status"] for row in by["prov-ad"]["roots"] if row["root_id"] == roots[RG1]] == ["eligible"]
    assert [row["status"] for row in by["prov-cloud2"]["roots"] if row["root_id"] == roots[RG1]] == [
        "already_selected"]
    detail = await detail_of(lab_)
    offered = next(item for item in detail["files"] if item["id"] == part.id)["acquisition_candidates"]
    current = await rows("SELECT candidate_id FROM canonical_candidate_bindings WHERE canonical_artifact_id=? "
                         "AND candidate_order<=100000", (part.id,))
    assert sorted(item["candidate_id"] for item in offered) == sorted(row["candidate_id"] for row in current)

    await switch_root_provider(lab_.engine, lab_.transfer.id, "prov-ad", expected_provider_id="prov-cloud2",
                               artifact_id=part.id, expected_root_id=roots[RG1])
    await settle(lab_.engine, 10)
    part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    assert part.state == "downloading" and part.candidates[part.selected].provider_id == "prov-ad"
    assert running(lab_) == [part.execution.attempt_id]
    assert earlier in {row["id"] for row in await rows("SELECT id FROM transfer_requests WHERE parent_id=?",
                                                        (roots[RG1],))}


async def test_a_second_copy_at_another_coordinate_is_held_and_the_link_still_switches_back(tmp_path, monkeypatch):
    """Cloud C names the file at another coordinate: strict decomposition
    continuity HOLDS its generation (nothing fans out, nothing lands). The
    link is not lost: it stays switchable, and switching back to A lands
    with one writer."""
    lab_, roots = await through_two_clouds(tmp_path, monkeypatch, second_prefix="Dir/")
    held = await rows("SELECT continuity,continuity_reason FROM transfer_file_selections WHERE request_id=? "
                      "AND manifest_committed_at IS NULL", (roots[RG1],))
    assert [(row["continuity"], row["continuity_reason"]) for row in held] == [("held", "established_member_missing")]
    part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    assert part.execution is None and not running(lab_)
    facts = await lab_.repository.root_route_facts_by_root(lab_.transfer.id)
    assert root_switches(lab_.engine.registry, facts)[roots[RG1]]
    await switch_root_provider(lab_.engine, lab_.transfer.id, "prov-ad", expected_provider_id="prov-cloud2",
                               artifact_id=part.id, expected_root_id=roots[RG1])
    await settle(lab_.engine, 10)
    part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    assert part.state == "downloading" and part.candidates[part.selected].provider_id == "prov-ad"
    assert running(lab_) == [part.execution.attempt_id]


async def test_a_switch_back_while_the_second_cloud_prepares_lands_nothing_from_it_later(tmp_path, monkeypatch):
    """Switching away while cloud C is still preparing: C's later readiness is
    never observed into the transfer -- no landing, one writer on A."""
    lab_, roots = await through_two_clouds(tmp_path, monkeypatch, second_ready=False)
    part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    assert part.execution is None
    facts = await lab_.repository.root_route_facts_by_root(lab_.transfer.id)
    assert facts[roots[RG1]]["members"] == 0 and root_switches(lab_.engine.registry, facts)[roots[RG1]]
    await switch_root_provider(lab_.engine, lab_.transfer.id, "prov-ad", expected_provider_id="prov-cloud2",
                               artifact_id=part.id, expected_root_id=roots[RG1])
    lab_.cloud2.ready = True
    await settle(lab_.engine, 10)
    part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    assert part.state == "downloading" and part.candidates[part.selected].provider_id == "prov-ad"
    assert running(lab_) == [part.execution.attempt_id]
    assert not [call for call in lab_.cloud2.calls if call[0] == "manifest"]


async def test_a_second_clouds_failed_member_errors_the_file_despite_an_earlier_generations_member(
        tmp_path, monkeypatch):
    """Cloud C's member failing for good fails the held file even though an
    earlier generation's member is recorded as resolved: the historical
    member keeps nothing waiting."""
    from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage
    lab_ = await cloud_lab(tmp_path, monkeypatch, second_cloud=True)
    roots = await roots_by_link(lab_.repository, lab_.transfer.id)
    part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    await switch_root_provider(lab_.engine, lab_.transfer.id, "prov-cloud", expected_provider_id="prov-ad",
                               artifact_id=part.id, expected_root_id=roots[RG1])
    lab_.cloud.ready = True
    await settle(lab_.engine, 10)
    lab_.cloud2.member_failure = NormalizedError(Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION,
                                                 Retryability.NEVER, origin=Origin.PROVIDER,
                                                 integration_id="prov-cloud2")
    await switch_root_provider(lab_.engine, lab_.transfer.id, "prov-cloud2", expected_provider_id="prov-cloud",
                               artifact_id=part.id, expected_root_id=roots[RG1])
    await historical_member(lab_, roots[RG1])                  # prov-cloud's generation is superseded now
    lab_.cloud2.ready = True
    await settle(lab_.engine, 20)
    part = await file_of(lab_.repository, lab_.transfer.id, "part01.rar")
    assert part.state == "error" and part.error.category == Category.PROVIDER_UNAVAILABLE
    assert not running(lab_)


async def test_a_folder_being_rebound_stays_expanded_and_unswitchable(tmp_path, monkeypatch):
    """Bounded check of the correction: a two-file folder whose root is being
    rebound to a new copy (its new generation opened but HELD, its members
    still stamped with the old one) has no CURRENT member, yet its members
    still own its files: it stays expanded and is never offered -- even with
    another provider that claims its host."""
    import json
    import time
    from db.database import get_db
    from transfers.manual_route_switch import expanded
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "rebind.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    cloud = CloudProvider("prov-cloud", ("cloudonly.example",), files=("a.bin", "b.bin"))
    cloud.ready = True
    registry.register_provider(cloud)
    registry.register_provider(CloudProvider("prov-cloud2", ("cloudonly.example",)))
    registry.register_provider(HostProvider("prov-ad"))
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3, resolution_concurrency=1),
                            clock=Clock())
    await engine.initialize()
    transfer = await engine.submit((TransferRequest("https", "https://cloudonly.example/f/folder"),
                                    TransferRequest("https", FICHIER1)), name="folder", deduplicate=False)
    await settle(engine, 10)
    folder = next(item for item in (await repository.root_route_facts_by_root(transfer.id)).values()
                  if item["members"])
    root_id = folder["root_id"]
    assert folder["members"] == 2 and expanded(folder)
    async with get_db() as db:
        root = await db.fetchone("SELECT resource FROM transfer_requests WHERE id=?", (root_id,))
        resource = json.loads(root["resource"])
        resource["id"] = "new-copy"
        old = await db.fetchone("SELECT state FROM provider_resources WHERE transfer_id=? AND provider_id='prov-cloud'",
                                (transfer.id,))
        await db.execute("INSERT INTO provider_resources(id,transfer_id,provider_id,payload,state,resource_key) "
                         "VALUES(?,?,?,?,?,?)", ("pr-new", transfer.id, "prov-cloud", json.dumps(resource),
                                                 old["state"], "new-copy"))
        await db.execute("UPDATE transfer_requests SET resource=? WHERE id=?", (json.dumps(resource), root_id))
        now = time.time()
        await db.execute(
            """INSERT INTO transfer_file_selections(id,request_id,transfer_id,provider_resource_id,provider_id,
               manifest_wait_until,created_at,updated_at,continuity,continuity_reason) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("sel-new", root_id, transfer.id, "pr-new", "prov-cloud", 0.0, now, now, "held",
             "established_member_missing"))
        await db.commit()
    facts = await repository.root_route_facts_by_root(transfer.id)
    rebound = facts[root_id]
    assert (rebound["members"], rebound["member_owned"], rebound["relevant"]) == (0, True, True)
    assert expanded(rebound) and not root_switches(registry, facts).get(root_id)
