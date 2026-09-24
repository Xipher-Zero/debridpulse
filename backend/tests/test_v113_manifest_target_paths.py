"""Manifest member paths are collection-root-relative; core applies the root ONCE.

Proven defect (live transfers 387/388): AllDebrid's native file tree exposes the
torrent's own collection root as a path component, that component crossed the
provider boundary inside ``SourceEntry.relative_path``, and
``transfers._engine_base.TransferEngine._materialize`` then correctly prepended the
canonical transfer root a second time::

    provider member path already contained collection root
  + core child target allocation prepended canonical transfer root
  = /download/<root>/<root>/<member>

The malformed target was durable and was handed to the executor as authoritative
output placement, so it is a target-construction defect, not an executor defect.

The canonical contract these tests freeze:

    ``FileManifestEntry.relative_path``, ``SourceEntry.relative_path`` and the
    resulting child ``TransferCandidate.relative_path`` describe the member path
    INSIDE the collection root. Core applies the durable transfer root exactly once.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor
from file_selection_support import Clock
from providers.alldebrid.client import API_V4
from providers.alldebrid.provider import AllDebridProvider
from providers.alldebrid.translation import (
    file_manifest_from_files_response, native_members, observation_from_native,
)
from transfers.applicability import HostClaim, HostClaimScope, ProviderApplicability
from transfers.convergence_engine import TransferEngine
from transfers.errors import TransferError
from transfers.models import IntegrationDescriptor, ProviderResource, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.recovery_repository import TransferRepository

ROOT = "Album"
HASH = "0123456789abcdef0123456789abcdef01234567"


# --------------------------------------------------------------------------- #
# Native fixtures: the observed live shape (collection root wraps the members)
# --------------------------------------------------------------------------- #

def leaf(name, size, *, link=True):
    node = {"n": name, "s": size}
    if link:
        node["l"] = f"https://alldebrid.example/dl/{name.replace(' ', '_')}?token=secret"
    return node


def wrapped_tree(*, link=True):
    """``Album/`` wrapping one top-level file and two real nested directories."""
    return [{"n": ROOT, "e": [
        leaf("track01.flac", 1024, link=link),
        {"n": "Disc 1", "e": [
            leaf("track02.flac", 2048, link=link),
            {"n": "CD1", "e": [leaf("track03.flac", 4096, link=link)]},
        ]},
    ]}]


MEMBER_PATHS = {"track01.flac", "Disc 1/track02.flac", "Disc 1/CD1/track03.flac"}

_SIZES = {"track01.flac": 1024, "track02.flac": 2048, "track03.flac": 4096, "payload.iso": 4096}


class FakeAllDebridClient:
    """Deterministic stand-in: one magnet, a configurable authoritative name and
    a configurable native tree. ``/magnet/files`` deliberately carries NO name
    fact, exactly like the real endpoint."""

    def __init__(self, *, filename=ROOT, tree=None, name_after=0):
        self.filename = filename
        self.tree = wrapped_tree() if tree is None else tree
        self.name_after = name_after          # polls before the name becomes authoritative
        self.polls = 0
        self.magnets = {}
        self.deleted = []

    def _name(self):
        """``""`` until the torrent's metadata has resolved."""
        return self.filename if self.polls > self.name_after else ""

    def _record(self, magnet_id):
        """Before the torrent's metadata resolves AllDebrid has neither a name
        nor a file tree -- both come from the same ``info`` dictionary -- so an
        unresolved magnet is reported as still preparing, with no tree."""
        resolved = self._name()
        record = {"id": magnet_id, "filename": resolved or "noname",
                  "hash": self.magnets[magnet_id]["hash"], "size": 7168,
                  "downloaded": 0, "downloadSpeed": 0}
        if not resolved:
            return {**record, "statusCode": 1, "status": "Downloading"}
        return {**record, "statusCode": 4, "status": "Ready", "files": self.tree}

    async def upload_magnet(self, magnet):
        digest = re.search(r"btih:([0-9a-fA-F]{40})", magnet).group(1).lower()
        self.magnets["991"] = {"hash": digest}
        return {"id": "991", "hash": digest, "ready": True}

    async def get_magnet_status(self, magnet_id=None):
        self.polls += 1
        if magnet_id is not None:
            return [self._record(str(magnet_id))] if str(magnet_id) in self.magnets else []
        return [self._record(key) for key in sorted(self.magnets)]

    async def get_magnet_files(self, ids):
        return [{"id": str(i), "files": self.tree} for i in ids if str(i) in self.magnets]

    async def unlock_link(self, link):
        """AllDebrid unlocks each member link into a direct download."""
        name = str(link).rsplit("/", 1)[-1].split("?")[0]
        return {"link": str(link), "filename": name, "filesize": _SIZES.get(name, 1)}

    async def _post(self, base, endpoint, data=None):
        assert base == API_V4 and endpoint == "magnet/delete"
        self.deleted.append(str(data["id"]))
        self.magnets.pop(str(data["id"]), None)
        return {}


class HttpsExecutor(MemoryExecutor):
    """Claims the https members AllDebrid fans a magnet out into, and records the
    exact FILE target core handed it -- executors consume a plan, never repair it."""

    descriptor = IntegrationDescriptor("https-lab", "HTTPS lab", frozenset())
    claim_schemes = frozenset({"https"})

    def __init__(self, authorize):
        super().__init__(authorize)
        self.planned = []

    def prepare(self, request):
        self.planned.append(request.work.materialization.target)
        return super().prepare(request)


def build(tmp_path, client, *, clock=None):
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = AllDebridProvider(client=client)
    # What AllDebrid's own supported-host runtime would publish for the member
    # links it issues; without it no route claims the fanned-out https members.
    provider.applicability = ProviderApplicability(
        specialized_hosts=(HostClaim("alldebrid.example", HostClaimScope.DOMAIN,
                                     frozenset({"https"})),))
    registry.register_provider(provider)
    executor = HttpsExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    clock = clock or Clock(1000.0)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(max_attempts=50, retry_delay=0, resolution_retry_delay=0,
                              adoption_stability_seconds=0, resource_poll_interval=5,
                              max_active_executions=4),
        clock=clock)
    return SimpleNamespace(engine=engine, repository=repository, provider=provider,
                           client=client, clock=clock, executor=executor,
                           root=str(tmp_path / "payloads"))


@pytest_asyncio.fixture
async def ad(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "manifest-root.db")
    await database.init_db()
    built = build(tmp_path, FakeAllDebridClient())
    await built.engine.initialize()
    return built


def magnet(name=ROOT, *, digest=HASH):
    return TransferRequest("magnet", f"magnet:?xt=urn:btih:{digest}", name=name,
                           fingerprint=digest, selection_mode="all")


def magnet_interactive(name=ROOT, *, digest=HASH):
    return TransferRequest("magnet", f"magnet:?xt=urn:btih:{digest}", name=name,
                           fingerprint=digest, selection_mode="interactive")


async def targets(transfer_id):
    async with database.get_db() as db:
        rows = await db.fetchall(
            "SELECT filename, local_path FROM download_files WHERE torrent_id=? ORDER BY id", (transfer_id,))
    return [row["local_path"] for row in rows]


async def candidate_paths(transfer_id):
    async with database.get_db() as db:
        rows = await db.fetchall(
            "SELECT metadata FROM transfer_requests WHERE transfer_id=? AND parent_id IS NOT NULL", (transfer_id,))
    from transfers import codec
    return sorted(codec.entry(codec.load(row["metadata"])).relative_path for row in rows)


async def drive(built, request, *, cycles=4):
    """Submit and run resolution to child fan-out, advancing the injected clock
    past the provider-poll interval between cycles (never a real sleep)."""
    transfer = await built.engine.submit((request,), name=request.name)
    for _ in range(cycles):
        await built.engine.resolve_pending()
        built.clock.advance(10)
    return transfer


# --------------------------------------------------------------------------- #
# A. Provider-native wrapper -> neutral member path (BOTH surfaces)
# --------------------------------------------------------------------------- #

def test_a_early_manifest_drops_the_native_collection_wrapper():
    manifest = observation_from_native(
        {"id": "991", "statusCode": 4, "filename": ROOT, "files": wrapped_tree()}).file_manifest
    assert {entry.relative_path for entry in manifest.entries} == MEMBER_PATHS


@pytest.mark.asyncio
async def test_a_executable_manifest_drops_the_native_collection_wrapper():
    client = FakeAllDebridClient()
    client.magnets["991"] = {"hash": HASH}
    provider = AllDebridProvider(client=client)
    resource = (await provider.observe(ProviderResource("alldebrid", {"id": "991"}))).resource
    entries = await provider.manifest(resource)
    assert {entry.relative_path for entry in entries} == MEMBER_PATHS


# --------------------------------------------------------------------------- #
# B. No false stripping of legitimate hierarchy
# --------------------------------------------------------------------------- #

def test_b_a_real_first_level_directory_survives():
    manifest = observation_from_native(
        {"id": "991", "statusCode": 4, "filename": ROOT, "files": wrapped_tree()}).file_manifest
    assert "Disc 1/track02.flac" in {entry.relative_path for entry in manifest.entries}


def test_b_a_shared_first_directory_is_not_the_wrapper_unless_it_is_the_resource_name():
    """Every member shares ``Disc 1`` and the tree has a single top-level node,
    but ``Disc 1`` is not the resource's authoritative name -- so it stays."""
    tree = [{"n": "Disc 1", "e": [leaf("track01.flac", 10), leaf("track02.flac", 20)]}]
    manifest = observation_from_native(
        {"id": "991", "statusCode": 4, "filename": ROOT, "files": tree}).file_manifest
    assert {entry.relative_path for entry in manifest.entries} == {
        "Disc 1/track01.flac", "Disc 1/track02.flac"}


def test_b_no_wrapper_is_removed_without_an_authoritative_name():
    """The ``noname`` placeholder is not a name fact, so nothing is unwrapped."""
    manifest = observation_from_native(
        {"id": "991", "statusCode": 4, "filename": "noname", "files": wrapped_tree()}).file_manifest
    assert {entry.relative_path for entry in manifest.entries} == {
        f"{ROOT}/{path}" for path in MEMBER_PATHS}


def test_b_multiple_top_level_nodes_are_never_unwrapped():
    tree = [{"n": ROOT, "e": [leaf("track01.flac", 10)]}, leaf("readme.txt", 5)]
    manifest = observation_from_native(
        {"id": "991", "statusCode": 4, "filename": ROOT, "files": tree}).file_manifest
    assert {entry.relative_path for entry in manifest.entries} == {
        f"{ROOT}/track01.flac", "readme.txt"}


def test_b_only_one_wrapper_level_is_ever_removed():
    """A torrent whose root directory contains a directory of the same name keeps
    the inner one."""
    tree = [{"n": ROOT, "e": [{"n": ROOT, "e": [leaf("track01.flac", 10)]}]}]
    manifest = observation_from_native(
        {"id": "991", "statusCode": 4, "filename": ROOT, "files": tree}).file_manifest
    assert {entry.relative_path for entry in manifest.entries} == {f"{ROOT}/track01.flac"}


# --------------------------------------------------------------------------- #
# C. Final durable target has exactly one root (real engine, real repository)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_c_durable_child_targets_carry_the_transfer_root_exactly_once(ad):
    transfer = await drive(ad, magnet())
    observed = await targets(transfer.id)
    assert observed and all(path for path in observed)
    assert set(observed) == {str(Path(ad.root) / ROOT / member) for member in MEMBER_PATHS}
    for path in observed:
        assert f"/{ROOT}/{ROOT}/" not in path


@pytest.mark.asyncio
async def test_c_child_candidate_paths_stay_member_relative(ad):
    transfer = await drive(ad, magnet())
    assert await candidate_paths(transfer.id) == sorted(MEMBER_PATHS)


@pytest.mark.asyncio
async def test_c_the_executor_receives_exactly_the_durable_target(ad):
    """No post-execution move is needed to make placement correct: the executor is
    handed the same absolute FILE target core persisted."""
    transfer = await drive(ad, magnet())
    await ad.engine.reconcile_executions()
    assert ad.executor.planned
    assert set(ad.executor.planned) == set(await targets(transfer.id))


# --------------------------------------------------------------------------- #
# D. Early and executable manifest path identity is stable
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_d_early_and_executable_member_paths_are_identical():
    client = FakeAllDebridClient()
    client.magnets["991"] = {"hash": HASH}
    provider = AllDebridProvider(client=client)
    observation = await provider.observe(ProviderResource("alldebrid", {"id": "991"}))
    early = {entry.relative_path for entry in observation.file_manifest.entries}
    late = {entry.relative_path for entry in await provider.manifest(observation.resource)}
    assert early == late == MEMBER_PATHS


def test_d_the_files_endpoint_fallback_uses_the_same_coordinate_system():
    records = [{"id": "991", "files": wrapped_tree()}]
    manifest = file_manifest_from_files_response(records, "991", root_name=ROOT)
    assert {entry.relative_path for entry in manifest.entries} == MEMBER_PATHS


# --------------------------------------------------------------------------- #
# D(e2e). Explicit file selection still reconciles across the corrected paths
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_d_explicit_selection_reconciles_and_materializes_under_one_root(ad):
    """An operator picks a nested member from the EARLY manifest; the confirmed
    subset must still be provable against the executable manifest, and only that
    member may materialize -- under exactly one collection root."""
    transfer = await ad.engine.submit((magnet_interactive(),), name=ROOT)
    await ad.engine.resolve_pending()
    ad.clock.advance(10)

    view = await ad.repository.file_selection_presentation(transfer.id, now=ad.clock())
    assert view and view["decision"] == "pending"
    assert {entry["relative_path"] for entry in view["entries"]} == MEMBER_PATHS

    picked = [entry["entry_id"] for entry in view["entries"]
              if entry["relative_path"] == "Disc 1/CD1/track03.flac"]
    assert (await ad.repository.confirm_file_selection(
        transfer.id, view["manifest_id"], picked, now=ad.clock())).outcome == "confirmed"

    for _ in range(4):
        await ad.engine.resolve_pending()
        ad.clock.advance(10)

    assert await candidate_paths(transfer.id) == ["Disc 1/CD1/track03.flac"]
    assert await targets(transfer.id) == [
        str(Path(ad.root) / ROOT / "Disc 1" / "CD1" / "track03.flac")]


# --------------------------------------------------------------------------- #
# E. Authoritative root differs from the admission name
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_e_authoritative_root_replaces_the_admission_name_and_is_applied_once(ad):
    transfer = await drive(ad, magnet("Uploaded Thing"))
    observed = await targets(transfer.id)
    assert set(observed) == {str(Path(ad.root) / ROOT / member) for member in MEMBER_PATHS}
    assert not any("Uploaded Thing" in path for path in observed)


# --------------------------------------------------------------------------- #
# F. Late authoritative name convergence
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_f_a_late_authoritative_root_name_still_lands_exactly_once(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "late-name.db")
    await database.init_db()
    built = build(tmp_path, FakeAllDebridClient(name_after=1))
    await built.engine.initialize()
    transfer = await drive(built, magnet("Uploaded Thing"), cycles=6)
    observed = await targets(transfer.id)
    assert set(observed) == {str(Path(built.root) / ROOT / member) for member in MEMBER_PATHS}
    assert not any("noname" in path for path in observed)
    assert await candidate_paths(transfer.id) == sorted(MEMBER_PATHS)


# --------------------------------------------------------------------------- #
# G. Single-file manifest keeps its existing policy
# --------------------------------------------------------------------------- #

def test_g_a_single_file_torrent_root_is_the_file_and_is_not_removed():
    """``info.name`` of a single-file torrent IS the file: the top-level node is a
    leaf, never a wrapper, so the member path stays the file name."""
    tree = [leaf("payload.iso", 4096)]
    manifest = observation_from_native(
        {"id": "991", "statusCode": 4, "filename": "payload.iso", "files": tree}).file_manifest
    assert {entry.relative_path for entry in manifest.entries} == {"payload.iso"}


@pytest.mark.asyncio
async def test_g_single_file_manifest_materializes_under_one_root(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "single.db")
    await database.init_db()
    client = FakeAllDebridClient(filename="payload.iso", tree=[leaf("payload.iso", 4096)])
    built = build(tmp_path, client)
    await built.engine.initialize()
    transfer = await drive(built, magnet("payload.iso"))
    assert await targets(transfer.id) == [str(Path(built.root) / "payload.iso" / "payload.iso")]


# --------------------------------------------------------------------------- #
# H. Nested directories survive beneath the one root
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_h_two_nested_levels_survive_beneath_one_root(ad):
    transfer = await drive(ad, magnet())
    assert str(Path(ad.root) / ROOT / "Disc 1" / "CD1" / "track03.flac") in await targets(transfer.id)


# --------------------------------------------------------------------------- #
# I. Path safety and capability validation are unchanged
# --------------------------------------------------------------------------- #

def test_i_core_still_refuses_a_traversing_member_path():
    """Path safety stays where it already is -- at the core boundary. Unwrapping
    the collection root neither adds nor removes any containment rule."""
    from transfers import file_selection as fs
    from transfers.filesystem import destination

    tree = [{"n": ROOT, "e": [leaf("../escape.bin", 10)]}]
    member = next(iter(native_members(tree, root_name=ROOT)))
    assert member.relative_path == "../escape.bin"
    with pytest.raises(TransferError):
        destination("/tmp", member.relative_path)
    with pytest.raises(fs.ManifestInvalid):
        fs.normalize_relative_path(member.relative_path)


def test_i_a_non_public_native_link_is_still_refused_before_materialization():
    tree = [{"n": ROOT, "e": [{"n": "payload.bin", "s": 1, "l": "http://127.0.0.1/payload"}]}]
    with pytest.raises(Exception, match="non-public"):
        native_members(tree, root_name=ROOT, require_link=True)


def test_i_the_neutral_early_manifest_never_carries_a_capability_url():
    manifest = observation_from_native(
        {"id": "991", "statusCode": 4, "filename": ROOT, "files": wrapped_tree()}).file_manifest
    blob = repr(manifest).casefold()
    for token in ("http", "token", "secret", "://", "/dl/"):
        assert token not in blob


# --------------------------------------------------------------------------- #
# The one generic core change, proven provider-neutrally: the executable manifest
# is generated from the resource the provider JUST observed, not the one frozen
# onto the request row at its first resolution attempt.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_core_asks_for_the_manifest_with_the_freshly_observed_resource(tmp_path, monkeypatch):
    from dataclasses import replace as _replace

    from fake_integrations import ParcelProvider
    from transfers.models import ResourceState

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fresh-resource.db")
    await database.init_db()

    class EnrichingParcelProvider(ParcelProvider):
        """A provider-neutral fake that learns something about its OWN resource
        after the binding was recorded, exactly as a real adapter may."""

        def __init__(self):
            super().__init__(file_manifest=True)
            self.manifest_contexts = []

        async def observe(self, resource):
            observation = await super().observe(resource)
            return _replace(observation,
                            resource=_replace(observation.resource,
                                              context={**dict(observation.resource.context),
                                                       "learned_later": "yes"}))

        async def manifest(self, resource):
            self.manifest_contexts.append(dict(resource.context))
            return await super().manifest(resource)

    provider = EnrichingParcelProvider()
    provider.responses.append(provider.parcel(
        "box", state=ResourceState.AVAILABLE, files=[("payload.bin", "folder/payload.bin", 4)]))

    repository = TransferRepository()
    registry = IntegrationRegistry()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    clock = Clock(1000.0)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(max_attempts=50, retry_delay=0,
                                                  resolution_retry_delay=0, adoption_stability_seconds=0,
                                                  resource_poll_interval=5, max_active_executions=4),
                            clock=clock)
    await engine.initialize()
    await engine.submit((TransferRequest("parcel", "box", name="payload.bin",
                                         fingerprint="fp-fresh", selection_mode="all"),), name="P")
    for _ in range(4):
        await engine.resolve_pending()
        clock.advance(10)

    assert provider.manifest_contexts, "the executable manifest was never requested"
    assert all(context.get("learned_later") == "yes" for context in provider.manifest_contexts)
