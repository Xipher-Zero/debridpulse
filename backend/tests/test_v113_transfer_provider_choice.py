"""B1 (1.0.13 release correction): the provider badge of a transfer of several
roots opens ONE best-effort, transfer-level provider choice.

The picker (``transfer_route_status``) reports, for each provider, every
participating root's status exactly as that root's own picker would, and how
many roots it may take now. A choice (``switch_transfer_provider``) switches
each root the provider may take through the one-root switch -- its own
admission, routing-basis check and writer fence -- naming the provider the
operator saw on it. It is not atomic: a refused root stays as it is and never
undoes a switched one, and every root's outcome is reported. Decomposed roots
and roots whose source another transfer holds stay excluded, exactly as WS T
excludes them from the one-root switch.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fake_integrations import MemoryExecutor
from test_v113_collection_route_generic_closure import Clock
from test_v113_multi_root_provider_switch import (
    FICHIER, PD1, PD2, RG1, RG2, file_of, lab, listed, roots_by_link, rows, settle,
)
from test_v113_root_provider_switch import FILES, application_of, magnet_provider, offer

from application.manual_route_switch import switch_transfer_provider, transfer_route_providers
from db import database
from db.database import get_db
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, TransferError
from transfers.manual_route_switch import transfer_route_status
from transfers.models import ExecutionState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

RG3 = "https://rapidgator.net/f/part03.rar"


def by_provider(status):
    return {item["provider_id"]: item for item in status["providers"]}


def statuses(item, roots):
    names = {root_id: link for link, root_id in roots.items()}
    return {names[row["root_id"]]: (row["status"], row["reason"]) for row in item["roots"]}


def expected_routes(item):
    """What the picker showed: each root the provider may take, with the
    provider the operator saw on it."""
    return {row["root_id"]: row["current_provider_id"] for row in item["roots"] if row["status"] == "eligible"}


async def test_the_badge_opens_the_transfer_choice_with_each_roots_truthful_participation(tmp_path, monkeypatch):
    """1063's shape: part01 selects its rapidgator root (prov-ad), part02 its
    pixeldrain root (prov-dl), extra.bin its 1fichier root (prov-ad). Roots
    supplying only alternates take no part. Each provider's count is the
    roots it may take now; a root already on it, or whose link it does not
    claim, is reported as such and never counted as switchable."""
    lab_ = await lab(tmp_path, monkeypatch)
    roots = await roots_by_link(lab_.repository, lab_.transfer.id)
    row = await listed(lab_)
    assert (row["route_switch_available"], row["route_switch_scope"]) == (True, "transfer")

    status = await transfer_route_status(lab_.engine, lab_.transfer.id)
    assert status["participants"] == 3 and status["switchable"]
    providers = by_provider(status)
    assert {provider: (item["eligible"], item["already_selected"], item["participants"], item["selectable"])
            for provider, item in providers.items()} == {
        "prov-ad": (0, 2, 3, False), "prov-dl": (1, 1, 3, True), "prov-tb": (1, 0, 3, True)}
    assert statuses(providers["prov-dl"], roots) == {
        RG1: ("eligible", None), PD2: ("already_selected", None), FICHIER: ("unsupported", None)}
    assert statuses(providers["prov-tb"], roots) == {
        RG1: ("unsupported", None), PD2: ("eligible", None), FICHIER: ("unsupported", None)}
    assert {RG2, PD1}.isdisjoint(statuses(providers["prov-dl"], roots))          # alternates only

    # The application read is the same answer; a transfer of one root has none.
    assert await transfer_route_providers(application_of(lab_.engine), lab_.transfer.id) == status
    single = await lab_.engine.submit((TransferRequest("https", RG1 + "?single"),), name="one", deduplicate=False)
    await settle(lab_.engine, 4)
    assert await transfer_route_status(lab_.engine, single.id) is None


async def test_a_choice_switches_only_the_eligible_roots_and_reports_every_root(tmp_path, monkeypatch):
    """Choosing prov-dl switches part01's root through the one-root switch;
    part02's root, already on prov-dl, and extra.bin's root, which prov-dl
    cannot claim, are reported and untouched: their files keep their writers
    and selections. The result is partial, never a clean whole transfer."""
    lab_ = await lab(tmp_path, monkeypatch)
    repository, engine, executor, transfer = lab_.repository, lab_.engine, lab_.executor, lab_.transfer
    roots = await roots_by_link(repository, transfer.id)
    before = {name: await file_of(repository, transfer.id, name) for name in ("part02.rar", "extra.bin")}
    item = by_provider(await transfer_route_status(engine, transfer.id))["prov-dl"]

    result = await switch_transfer_provider(application_of(engine), transfer.id, "prov-dl",
                                            expected_routes=expected_routes(item))
    assert (result["result"], result["switched"], result["already_selected"], result["ineligible"],
            result["refused"], result["participants"]) == ("partial", 1, 1, 1, 0, 3)
    assert {row["root_id"]: (row["outcome"], row["reason"]) for row in result["roots"]} == {
        roots[RG1]: ("switched", None), roots[PD2]: ("already_selected", None),
        roots[FICHIER]: ("ineligible", "unsupported")}
    assert (await repository.latest_root_route(roots[RG1]))["provider_id"] == "prov-dl"
    assert (await repository.latest_root_route(roots[PD2]))["provider_id"] == "prov-dl"
    assert (await repository.latest_root_route(roots[FICHIER]))["provider_id"] == "prov-ad"
    for name, old in before.items():                                    # no needless switch, no writer touched
        now = await file_of(repository, transfer.id, name)
        assert (now.selected, now.execution.attempt_id) == (old.selected, old.execution.attempt_id)
        assert executor.jobs[now.execution.attempt_id].state == ExecutionState.RUNNING

    await settle(engine)
    part01 = await file_of(repository, transfer.id, "part01.rar")
    assert part01.candidates[part01.selected].provider_id == "prov-dl"
    # Nothing is left to switch to prov-dl; re-reading reports it truthfully.
    again = by_provider(await transfer_route_status(engine, transfer.id))["prov-dl"]
    assert (again["eligible"], again["already_selected"]) == (0, 2)


async def test_a_refused_root_stays_unchanged_and_never_undoes_a_switched_one(tmp_path, monkeypatch):
    """Two roots prov-dl may take. The operator's view of part03's root is
    stale (its route moved after the picker read), so the one-root switch
    refuses it at its own admission and it stays as it is; part01's root,
    switched first, stays switched. A root that became switchable after the
    picker read is never switched by a choice that did not see it."""
    lab_ = await lab(tmp_path, monkeypatch, links=(RG1, PD1, RG2, PD2, FICHIER, RG3))
    repository, engine, transfer = lab_.repository, lab_.engine, lab_.transfer
    roots = await roots_by_link(repository, transfer.id)
    item = by_provider(await transfer_route_status(engine, transfer.id))["prov-dl"]
    seen = expected_routes(item)
    assert set(seen) == {roots[RG1], roots[RG3]}
    stale = {**seen, roots[RG3]: "prov-tb"}                    # what the operator saw is no longer the route

    result = await switch_transfer_provider(application_of(engine), transfer.id, "prov-dl", expected_routes=stale)
    outcomes = {row["root_id"]: (row["outcome"], row["reason"]) for row in result["roots"]}
    assert outcomes[roots[RG1]] == ("switched", None)
    assert outcomes[roots[RG3]] == ("refused", Category.RESOURCE_STATE_CONFLICT.value)
    assert (result["result"], result["switched"], result["refused"]) == ("partial", 1, 1)
    assert (await repository.latest_root_route(roots[RG1]))["provider_id"] == "prov-dl"
    assert (await repository.latest_root_route(roots[RG3]))["provider_id"] == "prov-ad"

    # A root the operator did not see as switchable is refused as changed.
    item = by_provider(await transfer_route_status(engine, transfer.id))["prov-dl"]
    assert [row["root_id"] for row in item["roots"] if row["status"] == "eligible"] == [roots[RG3]]
    result = await switch_transfer_provider(application_of(engine), transfer.id, "prov-dl", expected_routes={})
    outcomes = {row["root_id"]: (row["outcome"], row["reason"]) for row in result["roots"]}
    assert outcomes[roots[RG3]] == ("refused", "changed") and result["result"] == "unchanged"
    assert (await repository.latest_root_route(roots[RG3]))["provider_id"] == "prov-ad"

    # A provider that claims no participating root is refused outright.
    with pytest.raises(TransferError) as refused:
        await switch_transfer_provider(application_of(engine), transfer.id, "prov-unknown", expected_routes={})
    assert refused.value.error.category == Category.PROVIDER_UNAVAILABLE


async def test_capacity_refused_at_one_roots_admission_leaves_it_and_keeps_the_others(tmp_path, monkeypatch):
    """The target has no primary room for part03's root at the moment its own
    switch is admitted: that root is refused with the capacity reason and is
    unchanged; part01's root, admitted first, switched."""
    lab_ = await lab(tmp_path, monkeypatch, links=(RG1, PD1, RG2, PD2, FICHIER, RG3))
    repository, engine, transfer = lab_.repository, lab_.engine, lab_.transfer
    roots = await roots_by_link(repository, transfer.id)
    item = by_provider(await transfer_route_status(engine, transfer.id))["prov-dl"]
    admit = engine._make_primary_room

    async def full_for_part03(root, target):
        return False if root.id == roots[RG3] else await admit(root, target)

    monkeypatch.setattr(engine, "_make_primary_room", full_for_part03)
    result = await switch_transfer_provider(application_of(engine), transfer.id, "prov-dl",
                                            expected_routes=expected_routes(item))
    outcomes = {row["root_id"]: (row["outcome"], row["reason"]) for row in result["roots"]}
    assert outcomes[roots[RG1]] == ("switched", None)
    assert outcomes[roots[RG3]] == ("refused", Category.CONCURRENCY_LIMITED.value)
    assert (await repository.latest_root_route(roots[RG3]))["provider_id"] == "prov-ad"
    assert (await repository.latest_root_route(roots[RG1]))["provider_id"] == "prov-dl"


async def test_a_failure_after_a_switch_committed_reports_it_and_a_failure_before_raises(tmp_path, monkeypatch):
    """Something other than a refusal stops the operation (here the transfer
    disappears under the second root's switch). Once a root has switched, the
    report is returned -- that root switched, the stopping root ``gone``,
    every later root ``not_attempted`` -- so a committed switch is never
    reported as nothing; before any root switched, the failure is raised."""
    import application.manual_route_switch as command
    lab_ = await lab(tmp_path, monkeypatch, links=(RG1, PD1, RG2, PD2, FICHIER, RG3))
    repository, engine, transfer = lab_.repository, lab_.engine, lab_.transfer
    roots = await roots_by_link(repository, transfer.id)
    item = by_provider(await transfer_route_status(engine, transfer.id))["prov-dl"]
    real, calls = command.switch_route_provider, []

    async def second_call_finds_the_transfer_gone(*args, **kwargs):
        calls.append(kwargs["expected_root_id"])
        if len(calls) > 1:
            raise KeyError(transfer.id)
        return await real(*args, **kwargs)

    monkeypatch.setattr(command, "switch_route_provider", second_call_finds_the_transfer_gone)
    result = await switch_transfer_provider(application_of(engine), transfer.id, "prov-dl",
                                            expected_routes=expected_routes(item))
    outcomes = {row["root_id"]: (row["outcome"], row["reason"]) for row in result["roots"]}
    assert outcomes[roots[RG1]] == ("switched", None) and outcomes[roots[RG3]] == ("refused", "gone")
    assert (result["result"], result["switched"]) == ("partial", 1)
    assert (await repository.latest_root_route(roots[RG1]))["provider_id"] == "prov-dl"

    async def gone_at_once(*args, **kwargs):
        raise KeyError(transfer.id)

    monkeypatch.setattr(command, "switch_route_provider", gone_at_once)
    item = by_provider(await transfer_route_status(engine, transfer.id))["prov-dl"]
    with pytest.raises(KeyError):
        await switch_transfer_provider(application_of(engine), transfer.id, "prov-dl",
                                       expected_routes=expected_routes(item))


async def test_decomposed_roots_of_several_are_reported_and_never_switched(tmp_path, monkeypatch):
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
    transfer = await engine.submit((TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40, name="Show"),
                                    TransferRequest("magnet", "magnet:?xt=urn:btih:" + "b" * 40, name="Other")),
                                   name="Pair", deduplicate=False)
    await settle(engine)
    status = await transfer_route_status(engine, transfer.id)
    assert status["participants"] == 2 and not status["switchable"]
    assert {row["status"] for item in status["providers"] for row in item["roots"]} == {"decomposed"}
    routes = await rows("SELECT id,request_id,provider_id,state FROM resolution_attempts ORDER BY rowid")
    result = await switch_transfer_provider(application_of(engine), transfer.id, "parcel-a", expected_routes={})
    assert result["result"] == "unchanged" and result["ineligible"] == 2
    assert {row["reason"] for row in result["roots"]} == {"decomposed"}
    assert await rows("SELECT id,request_id,provider_id,state FROM resolution_attempts ORDER BY rowid") == routes
    assert "route_switch_scope" not in await listed(SimpleNamespace(engine=engine, repository=repository,
                                                                    transfer=transfer))


async def test_a_root_whose_source_another_transfer_holds_is_reported_shared_and_untouched(tmp_path, monkeypatch):
    """Ownership boundary: part01's rapidgator root issued a candidate another
    transfer's file holds. The transfer choice reports that root as shared for
    every provider and switches nothing of it; the foreign file keeps its
    bindings, selection, state and running writer."""
    lab_ = await lab(tmp_path, monkeypatch)
    repository, engine, executor, transfer = lab_.repository, lab_.engine, lab_.executor, lab_.transfer
    roots = await roots_by_link(repository, transfer.id)
    part01 = await file_of(repository, transfer.id, "part01.rar")
    other = await engine.submit((TransferRequest("https", "https://1fichier.com/x/other.bin"),), name="other",
                                deduplicate=False)
    await settle(engine)
    foreign = await file_of(repository, other.id, "other.bin")
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

    async def foreign_state():
        current = await file_of(repository, other.id, "other.bin")
        return (current.state, current.selected, current.execution.attempt_id if current.execution else None,
                [dict(row) for row in await rows(
                    "SELECT * FROM canonical_candidate_bindings WHERE canonical_artifact_id=? ORDER BY id",
                    (foreign.id,))])

    foreign_before = await foreign_state()
    status = await transfer_route_status(engine, transfer.id)
    assert {row["status"] for item in status["providers"] for row in item["roots"]
            if row["root_id"] == roots[RG1]} == {"shared"}
    item = by_provider(status)["prov-dl"]
    assert expected_routes(item) == {}                     # prov-dl's only other root (part02) is already on it
    result = await switch_transfer_provider(application_of(engine), transfer.id, "prov-dl",
                                            expected_routes={roots[RG1]: "prov-ad"})
    assert {row["root_id"]: (row["outcome"], row["reason"]) for row in result["roots"]}[roots[RG1]] == (
        "ineligible", "shared")
    assert result["switched"] == 0
    assert (await repository.latest_root_route(roots[RG1]))["provider_id"] == "prov-ad"
    assert await foreign_state() == foreign_before
    assert executor.jobs[foreign.execution.attempt_id].state == ExecutionState.RUNNING
    held = await file_of(repository, transfer.id, "part01.rar")
    assert held.execution is not None and held.execution.attempt_id == part01.execution.attempt_id


def test_the_common_source_group_control_is_untouched_by_the_transfer_choice():
    """A1: the ``⑂ N`` chip keeps counting common source hosts and switching
    files one after another in the browser; the transfer choice is a separate
    owner and endpoint."""
    from pathlib import Path
    static = Path(__file__).resolve().parents[2] / "frontend" / "static"
    group = (static / "ui-group-candidates.js").read_text()
    owner = (static / "ui-root-provider.js").read_text()
    assert "/route/transfer" not in group and "artifacts/' + artifactId + '/candidate'" in group
    assert "Non-transactional: whatever already switched stays switched." in group
    assert "/route/transfer" in owner and "ui-group-candidates" not in owner
