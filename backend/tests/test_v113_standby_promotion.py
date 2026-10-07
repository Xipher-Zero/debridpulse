"""Sequential backup promotion and bounded selection inheritance.

A prepared backup stays a subordinate resource until the ordinary router
makes its provider the root's route (TASK1/TASK2 decide; promotion never
does). Then that route takes over the prepared resource -- the exact
``provider_resources`` binding, read with the provider's own ``observe`` --
instead of resolving again, and the rest is the ordinary sequential
lifecycle: manifest, selection, member fan-out under ``uuid5(root, path)``,
candidates. A root whose operator explicitly chose files on its earlier
binding keeps that subset on the promoted binding when the one existing
matcher proves it completely, and fails closed otherwise.
"""
from __future__ import annotations

import math
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_v113_collection_route_generic_closure import Clock
from test_v113_standby_preparation import (
    FakeClient,
    Primary,
    creates,
    lab,
    magnet,
    root_of,
    submitted,
)
from test_v113_standby_preparation import torbox as unpublished_torbox
from test_v113_torbox_provider import torrent

from application.service import ApplicationService
from providers.torbox.client import TORRENT
from providers.torbox.translation import resource as torbox_resource
from transfers import file_selection as fs
from transfers._repository_base import manifest_child_identity
from transfers.errors import (
    Category,
    Domain,
    NormalizedError,
    Origin,
    Permanence,
    Retryability,
    Stage,
    TransferError,
)
from transfers.models import (
    FileManifest,
    FileManifestEntry,
    Ownership,
    ProviderObservation,
    ProviderResource,
    ResourceState,
    SourceEntry,
    TransferRequest,
)
from transfers.policy import TransferPolicy

pytestmark = pytest.mark.asyncio


def torbox(client=None, *, on=True):
    """TorBox as its host maintenance publishes it: it always claims its own
    delivery address, so the members its torrents decompose into route to it."""
    from providers.torbox.host_runtime import _OWN_CLAIM
    from transfers.applicability import ProviderApplicability
    provider = unpublished_torbox(client, on=on)
    provider.applicability = ProviderApplicability(specialized_hosts=(_OWN_CLAIM,), specialized=True)
    return provider

PROVIDER_FINAL = NormalizedError(Domain.PROVIDER, Category.ACCOUNT_LIMITED, Stage.RESOLUTION, Retryability.NEVER,
                                 origin=Origin.PROVIDER, permanence=Permanence.PERMANENT)


class FailingPrimary(Primary):
    """The primary keeps preparing until told its account can no longer serve
    the root -- a provider-final failure the router answers by moving on."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failed = False

    async def observe(self, resource):
        self.observed += 1
        if self.failed:
            return ProviderObservation(resource, ResourceState.UNAVAILABLE, "Show",
                                       error=replace(PROVIDER_FINAL, integration_id=self.descriptor.id))
        return ProviderObservation(resource, ResourceState.PREPARING, "Show")


async def prepared(tmp_path, monkeypatch, client=None, *, clock=None):
    """A root on the primary with a bound TorBox backup."""
    client = client or FakeClient()
    primary = FailingPrimary()
    repository, registry, engine = await lab(tmp_path, monkeypatch, primary, torbox(client), clock=clock)
    # The test executor also takes the HTTPS delivery links TorBox members
    # resolve to (aria2's role in the product).
    for executor in registry.executors.values():
        executor.claim_schemes = frozenset({*executor.claim_schemes, "https"})
    transfer = await submitted(engine)
    await engine.resolve_pending()
    (standby,) = await repository.standbys(transfer.id)
    assert standby["state"] == "bound" and len(creates(client)) == 1
    return client, primary, repository, registry, engine, transfer, standby


async def fail_over(engine, primary, passes=4):
    primary.failed = True
    for _ in range(passes):
        await engine.resolve_pending()


def torbox_rows(resources):
    return [resource for resource, _state, _pending in resources if resource.provider_id == "torbox"]


# -- 12 / 2 / 3 / 4: the chosen route takes the prepared resource over ---------------------------------

async def test_the_route_moving_to_the_backup_provider_takes_over_its_prepared_resource(tmp_path, monkeypatch):
    client, primary, repository, _registry, engine, transfer, standby = await prepared(tmp_path, monkeypatch)

    await fail_over(engine, primary)

    root = await root_of(repository, transfer)
    assert await repository.bound_route_provider(root.id) == "torbox"           # the router chose it
    assert len(creates(client)) == 1                                           # never created again
    assert root.resource.id == standby["resource"].id                          # the exact prepared resource
    assert len(torbox_rows(await repository.resources(transfer.id))) == 1      # one binding, one obligation
    assert root.resource.ownership == Ownership.CREATED                        # not relabelled
    (after,) = await repository.standbys(transfer.id)
    assert after["promoted_at"] is not None and after["state"] == "bound"      # provenance, no longer a backup


@pytest.mark.parametrize("ownership", [Ownership.CREATED, Ownership.ADOPTED, Ownership.OBSERVED])
async def test_promotion_preserves_the_resources_real_ownership(tmp_path, monkeypatch, ownership):
    client = FakeClient()
    primary = FailingPrimary()
    repository, _registry, engine = await lab(tmp_path, monkeypatch, primary, torbox(client, on=False))
    transfer = await submitted(engine)
    await engine.resolve_pending()
    root = await root_of(repository, transfer)
    client.objects[TORRENT]["900"] = torrent(native_id=900)
    held = replace(torbox_resource(TORRENT, "900"), ownership=ownership)
    standby_id, _attempts = await repository.begin_standby(transfer.id, root.id, "torbox", engine.clock())
    await repository.bind_standby(standby_id, transfer.id, held, ResourceState.PREPARING, engine.clock())

    await fail_over(engine, primary)

    after = await root_of(repository, transfer)
    assert after.resource.id == held.id and after.resource.ownership == ownership
    assert creates(client) == []


async def test_one_cleanup_obligation_follows_the_promoted_resource(tmp_path, monkeypatch):
    client, primary, _repository, _registry, engine, transfer, _standby = await prepared(tmp_path, monkeypatch)
    native = str(client.next_id)
    await fail_over(engine, primary)

    await engine.delete(transfer.id, remote=True)

    assert [call for call in client.calls if call[:2] == ("delete", TORRENT)] == [("delete", TORRENT, native)]


# -- 5: PREPARING versus AVAILABLE, and nothing executable before the ordinary lifecycle -----------------

async def test_a_preparing_promotion_waits_and_an_available_one_fans_out_under_ordinary_member_identity(
        tmp_path, monkeypatch):
    clock = Clock()
    client, primary, repository, _registry, engine, transfer, _standby = await prepared(
        tmp_path, monkeypatch, clock=clock)
    native = str(client.next_id)

    await fail_over(engine, primary)

    root = await root_of(repository, transfer)
    assert root.state == "waiting"                                             # PREPARING: it simply waits
    assert [item for item in await repository.requests(transfer.id) if item.parent_id] == []
    assert await repository.artifacts(transfer.id) == ()

    client.objects[TORRENT][native].update(download_state="cached", download_present=True, progress=1.0)
    clock.now += 3_600
    for _ in range(4):
        await engine.tick()

    members = [item for item in await repository.requests(transfer.id) if item.parent_id == root.id]
    assert len(members) == 2
    # Ordinary sequential member identity: uuid5(root, the member's own path).
    assert all(item.id == manifest_child_identity(root.id, item.entry.relative_path) for item in members)
    artifacts = await repository.artifacts(transfer.id)
    assert len(artifacts) == 2
    for artifact in artifacts:
        assert {candidate.provider_id for candidate in artifact.candidates} == {"torbox"}
        for candidate in artifact.candidates:
            # No signed delivery link is ever durable: the provider issues one
            # on use through the candidate's refresh request.
            assert all("token=" not in endpoint.address and "tb-cdn" not in endpoint.address
                       for endpoint in candidate.endpoints)
            assert candidate.delivery.value == "provider_issued" and candidate.refresh_request is not None
    assert len(creates(client)) == 1


async def test_an_available_backup_that_is_not_promoted_is_never_executable(tmp_path, monkeypatch):
    clock = Clock()
    client, _primary, repository, _registry, engine, transfer, _standby = await prepared(
        tmp_path, monkeypatch, clock=clock)
    client.objects[TORRENT][str(client.next_id)].update(download_state="cached", download_present=True)
    clock.now += 3_600
    for _ in range(3):
        await engine.tick()
    (standby,) = await repository.standbys(transfer.id)
    assert standby["resource_state"] == ResourceState.AVAILABLE.value and standby["promoted_at"] is None
    assert [item for item in await repository.requests(transfer.id) if item.parent_id] == []
    assert await repository.artifacts(transfer.id) == ()


# -- only a live backup is taken over -------------------------------------------------------------------------

async def test_a_backup_the_provider_reports_gone_is_never_promoted(tmp_path, monkeypatch):
    client, primary, repository, _registry, engine, transfer, standby = await prepared(tmp_path, monkeypatch)
    client.objects[TORRENT].clear()                                            # TorBox no longer has it

    await fail_over(engine, primary)

    root = await root_of(repository, transfer)
    assert len(creates(client)) == 2                                           # ordinary resolution again
    assert root.resource.id != standby["resource"].id
    (after,) = await repository.standbys(transfer.id)
    # Never promoted; the claim on the gone resource is settled (released,
    # holding nothing) rather than left bound to it.
    assert after["promoted_at"] is None
    assert (after["state"], after["binding_id"], after["resource_state"]) == ("deferred", None, None)


@pytest.mark.parametrize("refusal", ["ACTIVE_LIMIT", "INVALID_MAGNET"])
async def test_a_deferred_or_failed_backup_is_never_promoted(tmp_path, monkeypatch, refusal):
    from providers.torbox.client import TorBoxAPIError
    client = FakeClient()
    primary = FailingPrimary()
    repository, _registry, engine = await lab(tmp_path, monkeypatch, primary, torbox(client))
    transfer = await submitted(engine)
    client.refusal = TorBoxAPIError(refusal, "no", 403)
    await engine.resolve_pending()
    (standby,) = await repository.standbys(transfer.id)
    assert standby["state"] in {"deferred", "failed"}
    client.refusal = None

    await fail_over(engine, primary)

    root = await root_of(repository, transfer)
    assert await repository.bound_route_provider(root.id) == "torbox"
    assert root.resource is not None and root.resource.provider_id == "torbox"
    assert (await repository.standbys(transfer.id))[0]["promoted_at"] is None


async def test_without_a_backup_the_chosen_route_resolves_exactly_as_before(tmp_path, monkeypatch):
    client = FakeClient()
    primary = FailingPrimary()
    repository, _registry, engine = await lab(tmp_path, monkeypatch, primary, torbox(client, on=False))
    transfer = await submitted(engine)
    await engine.resolve_pending()
    assert creates(client) == []
    await fail_over(engine, primary)
    assert len(creates(client)) == 1
    assert (await root_of(repository, transfer)).resource.provider_id == "torbox"


async def test_a_backup_never_chooses_the_route(tmp_path, monkeypatch):
    _client, _primary, repository, _registry, engine, transfer, _standby = await prepared(tmp_path, monkeypatch)
    for _ in range(4):
        await engine.resolve_pending()                                         # the primary stays healthy
    root = await root_of(repository, transfer)
    assert await repository.bound_route_provider(root.id) == "provider-a"
    assert root.resource.provider_id == "provider-a"
    assert (await repository.standbys(transfer.id))[0]["promoted_at"] is None


# -- preview: a backup in reserve is not the transfer's file set ---------------------------------------------

async def test_the_provider_preview_never_lists_a_backup_held_in_reserve(tmp_path, monkeypatch):
    client, primary, repository, _registry, engine, transfer, _standby = await prepared(tmp_path, monkeypatch)
    client.objects[TORRENT][str(client.next_id)].update(download_state="cached", download_present=True)

    async def require(_transfer_id):
        return None

    service = SimpleNamespace(require=require, repository=repository, engine=engine)
    assert await ApplicationService.preview(service, transfer.id) == {"source": "provider", "files": []}
    await fail_over(engine, primary)
    assert (await root_of(repository, transfer)).resource.provider_id == "torbox"
    if not (await repository.presentation(transfer.id, details=True))["files"]:
        promoted = await ApplicationService.preview(service, transfer.id)
        assert len(promoted["files"]) == 2                                    # now it IS the root's file set


# -- 7 / 8 / 9: bounded selection inheritance (the selection owner, directly) --------------------------------

def entries(*items):
    return FileManifest(tuple(FileManifestEntry(path.rsplit("/", 1)[-1], path, size) for path, size in items))


def executable(*items):
    return tuple(SourceEntry(path.rsplit("/", 1)[-1], size, path, TransferRequest("https", f"https://x/{path}"))
                 for path, size in items)


EARLIER = (("Show/a.mkv", 10), ("Show/b.mkv", 20), ("Show/c.mkv", 30), ("Show/d.mkv", 40))


async def explicit_earlier_generation(tmp_path, monkeypatch, *, promoted=True, decide="explicit"):
    """A root whose operator explicitly chose b and d on provider A's binding,
    then a TorBox binding for the same root (promoted or not)."""
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), torbox(FakeClient(), on=False))
    transfer = await engine.submit((replace(magnet(), selection_mode="interactive"),), name="Show",
                                   deduplicate=False)
    root = await root_of(repository, transfer)
    now = engine.clock()
    first = ProviderResource("provider-a", {"n": 1}, Ownership.CREATED, id="provider-a:1")
    await repository.resource_observation(transfer.id, first, ResourceState.AVAILABLE)
    await repository.ensure_selection_generation(replace(root, resource=first), "provider-a", first,
                                                 available=True, file_manifest=entries(*EARLIER), now=now)
    binding = await repository.resource_binding_id(transfer.id, first.id)
    async with __import__("db.database", fromlist=["get_db"]).get_db() as db:
        generation = await db.fetchone("SELECT * FROM transfer_file_selections WHERE provider_resource_id=?",
                                       (binding,))
    manifest_id = generation["manifest_id"]
    if decide == "explicit":
        chosen = [fs.entry_identity(binding, path) for path in ("Show/b.mkv", "Show/d.mkv")]
        result = await repository.confirm_file_selection(transfer.id, manifest_id, chosen, now=now)
        assert result.outcome == fs.SelectionOutcome.CONFIRMED
    await repository.commit_selected_manifest(replace(root, resource=first), executable(*EARLIER), now=now)
    second = replace(torbox_resource(TORRENT, "900"), ownership=Ownership.CREATED)
    standby_id, _attempts = await repository.begin_standby(transfer.id, root.id, "torbox", now)
    await repository.bind_standby(standby_id, transfer.id, second, ResourceState.AVAILABLE, now)
    if promoted:
        await repository.promote_standby(standby_id, now)
    return repository, engine, replace(root, resource=second), now


async def generation_of(repository, record):
    from db.database import get_db
    binding = await repository.resource_binding_id(record.transfer_id, record.resource.id)
    async with get_db() as db:
        return await db.fetchone("SELECT * FROM transfer_file_selections WHERE request_id=? AND "
                                 "provider_resource_id=?", (record.id, binding))


async def test_a_promoted_binding_carries_the_explicit_subset_matched_by_path_not_position(tmp_path, monkeypatch):
    repository, _engine, record, now = await explicit_earlier_generation(tmp_path, monkeypatch)
    reordered = (("Show/d.mkv", 40), ("Show/c.mkv", 30), ("Show/a.mkv", 10), ("Show/b.mkv", 20))
    await repository.ensure_selection_generation(record, "torbox", record.resource, available=True,
                                                 file_manifest=entries(*reordered), now=now)
    generation = await generation_of(repository, record)
    assert (generation["decision"], generation["decision_reason"]) == ("explicit", "inherited")

    authorized = await repository.commit_selected_manifest(record, executable(*reordered), now=now)

    assert [entry.relative_path for entry in authorized] == ["Show/b.mkv", "Show/d.mkv"]   # never broadened
    presentation = await repository.file_selection_presentation(record.transfer_id, now=now)
    assert presentation is not None


@pytest.mark.parametrize("promoted_manifest, reason", [
    ((("Show/a.mkv", 10), ("Show/b.mkv", 20), ("Show/c.mkv", 30)), "missing"),          # d is gone
    ((("Show/b.mkv", 20), ("Show/d.mkv", 41)), "size_conflict"),                         # d is another file
    ((("Show/b.mkv", 20), ("Show/b.mkv", 20), ("Show/d.mkv", 40)), "ambiguous"),          # b is not unique
])
async def test_a_subset_that_cannot_be_completely_proven_fails_closed(tmp_path, monkeypatch, promoted_manifest,
                                                                      reason):
    repository, _engine, record, now = await explicit_earlier_generation(tmp_path, monkeypatch)
    await repository.ensure_selection_generation(record, "torbox", record.resource, available=True,
                                                 file_manifest=None, now=now)
    with pytest.raises(TransferError) as refused:
        await repository.commit_selected_manifest(record, executable(*promoted_manifest), now=now)
    assert refused.value.error.category == Category.RESOURCE_STATE_CONFLICT        # never dropped, never ALL
    assert (await generation_of(repository, record))["manifest_committed_at"] is None


async def test_any_rebind_carries_the_immediate_explicit_subset_promoted_or_not(tmp_path, monkeypatch):
    """TASK3d-3a0 D2: the trigger is any resource rebind that opens a new
    decomposition generation, not only a promoted backup -- the subset is
    carried exactly as a promoted one, through the same proof."""
    repository, _engine, record, now = await explicit_earlier_generation(tmp_path, monkeypatch, promoted=False)
    await repository.ensure_selection_generation(record, "torbox", record.resource, available=True,
                                                 file_manifest=entries(*EARLIER), now=now)
    generation = await generation_of(repository, record)
    assert (generation["decision"], generation["decision_reason"]) == ("explicit", "inherited")
    authorized = await repository.commit_selected_manifest(record, executable(*EARLIER), now=now)
    assert [entry.relative_path for entry in authorized] == ["Show/b.mkv", "Show/d.mkv"]


async def test_without_an_earlier_explicit_subset_nothing_is_inherited(tmp_path, monkeypatch):
    """An ALL predecessor is not carried (TASK3d-3a0 B): the new generation
    keeps its own fresh selection window, exactly as TASK3c left it."""
    repository, _engine, record, now = await explicit_earlier_generation(tmp_path, monkeypatch, decide="all")
    await repository.ensure_selection_generation(record, "torbox", record.resource, available=True,
                                                 file_manifest=entries(*EARLIER), now=now)
    assert (await generation_of(repository, record))["decision"] == "pending"


# -- the predecessor is the latest committed generation, whatever it decided ----------------------------

async def committed_generation(repository, transfer, root, resource, provider_id, chosen, now):
    """One committed generation of ``root`` on ``resource``: explicit over
    ``chosen`` paths, or ALL when ``chosen`` is None -- decided as stated
    whatever it would have carried (an earlier-decided or legacy generation)."""
    from db.database import get_db
    await repository.resource_observation(transfer.id, resource, ResourceState.AVAILABLE)
    record = replace(root, resource=resource)
    await repository.ensure_selection_generation(record, provider_id, resource, available=True,
                                                 file_manifest=entries(*EARLIER), now=now)
    binding = await repository.resource_binding_id(transfer.id, resource.id)
    async with get_db() as db:
        generation = await db.fetchone("SELECT id,manifest_id FROM transfer_file_selections WHERE "
                                       "provider_resource_id=?", (binding,))
        await db.execute("DELETE FROM transfer_file_selection_entries WHERE selection_id=?", (generation["id"],))
        await db.execute("UPDATE transfer_file_selections SET decision='pending',decision_reason=NULL,decision_at=NULL "
                         "WHERE id=?", (generation["id"],))
        await db.commit()
    if chosen is not None:
        result = await repository.confirm_file_selection(
            transfer.id, generation["manifest_id"], [fs.entry_identity(binding, path) for path in chosen], now=now)
        assert result.outcome == fs.SelectionOutcome.CONFIRMED
    return await repository.commit_selected_manifest(record, executable(*EARLIER), now=now)


@pytest.mark.parametrize("newer, expected", [
    (None, None),                                            # B: newer ALL -> nothing to carry
    (("Show/a.mkv", "Show/c.mkv"), ["Show/a.mkv", "Show/c.mkv"]),   # B: newer explicit -> B's choice
])
async def test_only_the_immediate_predecessors_choice_is_ever_carried(tmp_path, monkeypatch, newer, expected):
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), torbox(FakeClient(), on=False))
    transfer = await engine.submit((replace(magnet(), selection_mode="interactive"),), name="Show",
                                   deduplicate=False)
    root = await root_of(repository, transfer)
    t = engine.clock()
    first = ProviderResource("provider-a", {"n": 1}, Ownership.CREATED, id="provider-a:1")
    second = ProviderResource("provider-b", {"n": 2}, Ownership.CREATED, id="provider-b:2")
    await committed_generation(repository, transfer, root, first, "provider-a", ("Show/b.mkv", "Show/d.mkv"), t)
    # These generations are decided as stated, as a transfer from before the
    # transfer-owned selection intent was: it holds none, so the immediate
    # predecessor's own choice is what is carried (or not).
    from db.database import get_db
    async with get_db() as db:
        await db.execute("DELETE FROM transfer_file_selection_intent_entries WHERE request_id=?", (root.id,))
        await db.execute("DELETE FROM transfer_file_selection_intents WHERE request_id=?", (root.id,))
        await db.commit()
    later = await committed_generation(repository, transfer, root, second, "provider-b", newer, t + 10)
    assert [entry.relative_path for entry in later] == (list(newer) if newer else [path for path, _ in EARLIER])

    third = replace(torbox_resource(TORRENT, "900"), ownership=Ownership.CREATED)
    standby_id, _attempts = await repository.begin_standby(transfer.id, root.id, "torbox", t + 20)
    await repository.bind_standby(standby_id, transfer.id, third, ResourceState.AVAILABLE, t + 20)
    await repository.promote_standby(standby_id, t + 20)
    promoted = replace(root, resource=third)
    await repository.ensure_selection_generation(promoted, "torbox", third, available=True,
                                                 file_manifest=entries(*EARLIER), now=t + 30)

    generation = await generation_of(repository, promoted)
    if expected is None:
        # A's {b, d} is never resurrected past B's newer ALL: the ordinary
        # fresh generation (its own window) is what C gets.
        assert (generation["decision"], generation["decision_reason"]) == ("pending", None)
    else:
        assert (generation["decision"], generation["decision_reason"]) == ("explicit", "inherited")
        authorized = await repository.commit_selected_manifest(promoted, executable(*EARLIER), now=t + 30)
        assert [entry.relative_path for entry in authorized] == expected       # B's choice, never A's


# -- transfer 536: a route whose AVAILABLE resource cannot be made executable hands over to the backup ----------

class UnmanifestablePrimary(Primary):
    """The primary's resource becomes AVAILABLE, then its provider cannot
    produce the executable manifest -- transfer 536's normalized facts, with
    no provider's text."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.available = False
        self.manifests = 0

    async def observe(self, resource):
        self.observed += 1
        return ProviderObservation(resource, ResourceState.AVAILABLE if self.available else ResourceState.PREPARING,
                                   "Show")

    async def manifest(self, resource):
        self.manifests += 1
        raise TransferError(NormalizedError(
            Domain.PROVIDER, Category.PROVIDER_PROTOCOL_VIOLATION, Stage.CANDIDATE_PREPARATION, Retryability.UNKNOWN,
            origin=Origin.PROVIDER, permanence=Permanence.UNKNOWN, integration_id=self.descriptor.id))


async def test_an_available_route_whose_manifest_fails_at_its_provider_promotes_the_prepared_backup(
        tmp_path, monkeypatch):
    clock, client, primary = Clock(), FakeClient(), UnmanifestablePrimary()
    repository, registry, engine = await lab(
        tmp_path, monkeypatch, primary, torbox(client), clock=clock,
        policy=TransferPolicy(retry_delay=60.0, resolution_retry_delay=300.0, max_attempts=4))
    for executor in registry.executors.values():
        executor.claim_schemes = frozenset({*executor.claim_schemes, "https"})
    transfer = await submitted(engine)
    await engine.resolve_pending()
    (standby,) = await repository.standbys(transfer.id)
    assert standby["state"] == "bound" and len(creates(client)) == 1
    client.objects[TORRENT][str(client.next_id)].update(download_state="cached", download_present=True, progress=1.0)
    root = await root_of(repository, transfer)

    primary.available = True
    clock.now += 31
    await engine.tick()                                                        # AVAILABLE, then its manifest fails
    failed_at = clock.now
    for _ in range(3):
        await engine.tick()

    assert primary.manifests == 1
    assert "provider-a" in await repository.exhausted_route_providers(root.id, failed_at)
    assert "provider-a" not in await repository.exhausted_route_providers(root.id, math.inf)   # re-enters later
    root = await root_of(repository, transfer)
    assert await repository.bound_route_provider(root.id) == "torbox"          # the router chose it, at once
    assert root.resource.id == standby["resource"].id                          # the prepared resource, taken over
    assert len(creates(client)) == 1                                           # never created again
    (after,) = await repository.standbys(transfer.id)
    assert after["promoted_at"] is not None
