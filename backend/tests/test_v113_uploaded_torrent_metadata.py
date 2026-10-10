"""Uploaded ``.torrent`` bytes as authoritative identity evidence (D2, D8).

The original metainfo DebridPulse already persists for an upload is read --
only when identity evidence is needed -- by one pure validator at the torrent
input owner (``transfers.requests``): the v1 info-hash over the ``info``
value's ORIGINAL bytes, the torrent's own member tree, the v1 side of a
hybrid, and an explicit, actionable refusal of v2-only input.

A Premiumize-first upload uses that verified tree to state its immediate
members at the torrent's own collection-relative coordinates, keeping the
path Premiumize stated for every later request. No network is involved in
reading metadata; magnets are untouched.
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import bencode2
import pytest
from test_v113_premiumize_provider import ok, provider_with, routes

from application.service import ApplicationService
from providers.premiumize.client import parse_member_address
from transfers.requests import (
    V2_ONLY_TORRENT_MESSAGE,
    TorrentMetainfoRejected,
    bittorrent_root_hash,
    extract_hash_from_torrent,
    torrent_identity,
    torrent_member_tree,
)
from transfers.models import ResourceState, TransferRequest

PIECES = b"\x11" * 20


def metainfo(info: dict, **top) -> bytes:
    return bencode2.bencode({b"announce": b"https://tracker.invalid/announce", **top, b"info": info})


def multi(name=b"The Real Show", files=None, extra=None) -> dict:
    files = files if files is not None else [
        {b"length": 1000, b"path": [b"Season 1", b"e01.mkv"]},
        {b"length": 2000, b"path": [b"Season 2", b"e02.mkv"]},
    ]
    return {b"files": files, b"name": name, b"piece length": 16384, b"pieces": PIECES, **(extra or {})}


# -- identity: v1, original bytes ----------------------------------------------------------------------------------

def test_a_v1_single_file_torrent_is_identified_by_its_original_info_bytes():
    info = {b"length": 1234, b"name": b"movie.mkv", b"piece length": 262144, b"pieces": PIECES}
    data = metainfo(info)
    identity = torrent_identity(data)
    assert identity.info_hash == hashlib.sha1(bencode2.bencode(info)).hexdigest() == extract_hash_from_torrent(data)
    assert identity.raw_info == bencode2.bencode(info) and not identity.hybrid
    tree = torrent_member_tree(data)
    assert (tree.name, tree.members, tree.multi_file) == ("movie.mkv", (("movie.mkv", 1234),), False)


def test_a_v1_multi_file_tree_is_relative_to_the_torrent_s_own_name_and_keeps_nesting():
    info = multi(files=[{b"length": 5, b"path": [b"Season 1", b"Extras", b"a.mkv"]},
                        {b"length": 6, b"path": [b"b.nfo"]}])
    tree = torrent_member_tree(metainfo(info))
    assert tree.name == "The Real Show" and tree.multi_file
    assert tree.members == (("Season 1/Extras/a.mkv", 5), ("b.nfo", 6))


def test_the_hash_is_of_the_info_value_exactly_as_submitted_wherever_it_sits():
    info = multi()
    first = bencode2.bencode({b"announce": b"x", b"info": info})
    later = bencode2.bencode({b"info": info, b"z-comment": b"after info"})
    assert torrent_identity(first).info_hash == torrent_identity(later).info_hash == hashlib.sha1(
        bencode2.bencode(info)).hexdigest()


@pytest.mark.parametrize("data", [
    b"d4:infod6:lengthi1e4:name1:a12:piece lengthi1e6:pieces20:" + PIECES + b"e1:ai1ee",     # unsorted top-level keys
    b"d4:infod4:name1:a6:lengthi1e12:piece lengthi1e6:pieces20:" + PIECES + b"ee",           # unsorted info keys
    b"d4:infod6:lengthi01e4:name1:a12:piece lengthi1e6:pieces20:" + PIECES + b"ee",          # leading zero
    b"d4:infod6:lengthi1e4:name1:a",                                                          # truncated
    b"d8:announce1:xe",                                                                       # no info
    b"not-bencode",
], ids=["unsorted-top", "unsorted-info", "leading-zero", "truncated", "no-info", "garbage"])
def test_non_canonical_or_malformed_metainfo_is_refused_never_reencoded(data):
    with pytest.raises(TorrentMetainfoRejected):
        torrent_identity(data)
    assert extract_hash_from_torrent(data) == "" and torrent_member_tree(data) is None


# -- hybrid and v2-only (D8) ---------------------------------------------------------------------------------------

def test_a_hybrid_keeps_its_v1_identity_and_its_v1_tree_without_padding_files():
    info = multi(files=[{b"length": 1000, b"path": [b"Season 1", b"e01.mkv"]},
                        {b"attr": b"p", b"length": 15384, b"path": [b".pad", b"15384"]},
                        {b"length": 2000, b"path": [b"Season 2", b"e02.mkv"]}],
                 extra={b"meta version": 2, b"file tree": {b"Season 1": {b"e01.mkv": {b"": {b"length": 1000}}}}})
    data = metainfo(info)
    identity = torrent_identity(data)
    assert identity.hybrid and identity.info_hash == hashlib.sha1(bencode2.bencode(info)).hexdigest()
    assert torrent_member_tree(data).members == (("Season 1/e01.mkv", 1000), ("Season 2/e02.mkv", 2000))


V2_ONLY = metainfo({b"file tree": {b"movie.mkv": {b"": {b"length": 5, b"pieces root": b"r" * 32}}},
                    b"meta version": 2, b"name": b"movie.mkv", b"piece length": 16384})


def test_a_v2_only_torrent_is_refused_with_an_actionable_reason_and_no_invented_hash():
    with pytest.raises(TorrentMetainfoRejected) as refused:
        torrent_identity(V2_ONLY)
    assert str(refused.value) == V2_ONLY_TORRENT_MESSAGE and "v1 or hybrid" in str(refused.value)
    assert extract_hash_from_torrent(V2_ONLY) == "" and torrent_member_tree(V2_ONLY) is None


@pytest.mark.asyncio
async def test_a_v2_only_upload_is_refused_before_anything_is_admitted():
    admitted = []

    async def submit(*args, **kwargs):
        admitted.append(args)

    with pytest.raises(ValueError, match="v2-only"):
        await ApplicationService.submit_torrent(SimpleNamespace(submit=submit), V2_ONLY, "movie.torrent")
    assert admitted == []
    v1 = metainfo({b"length": 1, b"name": b"a", b"piece length": 1, b"pieces": PIECES})
    await ApplicationService.submit_torrent(SimpleNamespace(submit=submit), v1, "a.torrent")
    (requests,), = admitted
    assert requests[0].fingerprint == extract_hash_from_torrent(v1)


# -- the tree is evidence only when it is unambiguous text ---------------------------------------------------------

@pytest.mark.parametrize("files", [
    [{b"length": 5, b"path": [b"\xff\xfe.mkv"]}],                       # not UTF-8
    [{b"length": 5, b"path": [b".."]}],                                  # escapes
    [{b"length": 5, b"path": [b"a/b.mkv"]}],                             # separator inside a segment
    [{b"length": 5, b"path": []}],                                       # no path
    [{b"length": 5, b"path": [b"a.mkv"]}, {b"length": 6, b"path": [b"a.mkv"]}],   # duplicate member
    [{b"length": -1, b"path": [b"a.mkv"]}],                              # negative size
], ids=["non-utf8", "dotdot", "separator", "empty", "duplicate", "negative"])
def test_an_unreadable_or_unsafe_tree_is_no_evidence_while_the_identity_still_holds(files):
    data = metainfo(multi(files=files))
    assert torrent_identity(data).info_hash
    assert torrent_member_tree(data) is None


def test_the_root_hash_binds_only_a_fingerprint_its_own_payload_reproduces():
    data = metainfo(multi())
    digest = extract_hash_from_torrent(data)
    assert bittorrent_root_hash(TransferRequest("torrent", data, "x.torrent", digest)) == digest
    assert bittorrent_root_hash(TransferRequest("torrent", data, "x.torrent", "b" * 40)) == ""
    assert bittorrent_root_hash(TransferRequest("torrent", V2_ONLY, "x.torrent", "b" * 40)) == ""
    magnet = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Display+Name"
    assert bittorrent_root_hash(TransferRequest("magnet", magnet, "Display Name", "a" * 40)) == "a" * 40
    assert bittorrent_root_hash(TransferRequest("magnet", magnet, "Display Name", "")) == ""
    assert bittorrent_root_hash(TransferRequest("https", "https://x.example/a", "a", "a" * 40)) == ""


# -- Premiumize first: the verified tree states the coordinates ----------------------------------------------------

UPLOAD = metainfo(multi())
DIGEST = extract_hash_from_torrent(UPLOAD)
TORRENT = TransferRequest("torrent", UPLOAD, "The Real Show.torrent", DIGEST)


def directdl(wrapper="The Real Show", sizes=(1000, 2000)):
    return [{"path": f"{wrapper}/Season 1/e01.mkv", "size": sizes[0], "link": "https://cdn.premiumize.example/a"},
            {"path": f"{wrapper}/Season 2/e02.mkv", "size": sizes[1], "link": "https://cdn.premiumize.example/b"}]


async def resolved(request, content):
    provider, transport = provider_with({("POST", "cache/check"): [ok(response=[True])],
                                         ("POST", "transfer/directdl"): [ok(content=content)]})
    return provider, transport, (await provider.resolve(request)).observation


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["The Real Show", "A Label Premiumize Chose"], ids=["torrent-name", "other-label"])
async def test_a_premiumize_first_upload_states_its_members_at_the_torrent_s_own_coordinates(wrapper):
    """The verified tree and Premiumize's complete list correspond by exactly
    one extra leading directory -- whatever Premiumize called it -- so the
    members are the torrent's own paths; the stated path stays the member's
    native identity, and nothing beyond the ordinary two reads is asked."""
    provider, transport, observed = await resolved(TORRENT, directdl(wrapper))
    assert [entry.relative_path for entry in observed.file_manifest.entries] == ["Season 1/e01.mkv",
                                                                                "Season 2/e02.mkv"]
    assert [native for _path, _size, native in observed.resource.context["members"]] == [
        f"{wrapper}/Season 1/e01.mkv", f"{wrapper}/Season 2/e02.mkv"]
    entries = await provider.manifest(observed.resource)
    assert [entry.relative_path for entry in entries] == ["Season 1/e01.mkv", "Season 2/e02.mkv"]
    assert [parse_member_address(entry.request.payload)[2] for entry in entries] == [
        f"{wrapper}/Season 1/e01.mkv", f"{wrapper}/Season 2/e02.mkv"]
    assert routes(transport) == [("POST", "cache/check"), ("POST", "transfer/directdl")]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    directdl(sizes=(1000, 2001)),                                                                 # a size differs
    directdl()[:1],                                                                               # a member missing
    [*directdl(), {"path": "The Real Show/extra.nfo", "size": 9, "link": "https://cdn.premiumize.example/c"}],
], ids=["size", "missing", "extra"])
async def test_without_a_complete_correspondence_the_stated_members_are_kept_as_they_are(content):
    _provider, _transport, observed = await resolved(TORRENT, content)
    assert [entry.relative_path for entry in observed.file_manifest.entries] == [item["path"] for item in content]


@pytest.mark.asyncio
async def test_a_magnet_never_borrows_upload_metadata_and_keeps_its_existing_wrapper_rule():
    magnet = TransferRequest("magnet", f"magnet:?xt=urn:btih:{DIGEST}&dn=Complete+Series", "Complete Series",
                             DIGEST)
    _provider, _transport, observed = await resolved(magnet, directdl())
    assert [entry.relative_path for entry in observed.file_manifest.entries] == [
        "The Real Show/Season 1/e01.mkv", "The Real Show/Season 2/e02.mkv"]


@pytest.mark.asyncio
async def test_an_upload_whose_bytes_are_not_the_asked_torrent_states_nothing_new():
    other = TransferRequest("torrent", metainfo(multi(name=b"Other")), "x.torrent", DIGEST)
    _provider, _transport, observed = await resolved(other, directdl())
    assert [entry.relative_path for entry in observed.file_manifest.entries] == [
        "The Real Show/Season 1/e01.mkv", "The Real Show/Season 2/e02.mkv"]


# -- the uploaded tree as the same-torrent binding at the continuation boundary ------------------------------------

SEASONS_TORRENT = metainfo(multi(name=b"Show", files=[
    {b"length": 100, b"path": [b"S1", b"A.mkv"]}, {b"length": 200, b"path": [b"S2", b"B.mkv"]},
    {b"length": 300, b"path": [b"S3", b"C.mkv"]}]))


async def upload_switched(tmp_path, monkeypatch, target, *, data=SEASONS_TORRENT):
    """An uploaded torrent decomposed by ``parcel-a`` with an explicit two-of-
    three selection, then switched by the operator to ``parcel-b``. Neither
    provider reports a source fingerprint: the only binding is the upload's
    own verified tree."""
    from dataclasses import replace as _replace

    from test_v113_collection_route_generic_closure import Clock
    from test_v113_root_provider_switch import SEASONS, first_hold, magnet_provider, settle

    from db import database
    from fake_integrations import MemoryExecutor
    from transfers.convergence_engine import TransferEngine
    from transfers.manual_route_switch import switch_root_provider
    from transfers.policy import TransferPolicy
    from transfers.recovery_repository import TransferRepository
    from transfers.registry import IntegrationRegistry

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "upload.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    providers = {}
    for identity in ("parcel-a", "parcel-b"):
        provider = magnet_provider(identity)
        provider.descriptor = _replace(provider.descriptor, request_types=provider.descriptor.request_types | {"torrent"})
        providers[identity] = provider
        registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3), clock=Clock())
    await engine.initialize()
    request = TransferRequest("torrent", data, "Show.torrent", extract_hash_from_torrent(data),
                              selection_mode="interactive")

    def offer(provider, files, native):
        result = provider.parcel(native, state=ResourceState.AVAILABLE, files=files)
        observed = _replace(result.observation, request=request)
        provider.resources[observed.resource.id] = observed
        provider.responses.append(_replace(result, observation=observed))

    offer(providers["parcel-a"], SEASONS, "x")
    transfer = await engine.submit((request,), name="Show", deduplicate=False)
    for _ in range(3):
        await engine.tick()
    view = await repository.file_selection_presentation(transfer.id, now=engine.clock())
    await repository.confirm_file_selection(transfer.id, view["manifest_id"], [
        entry["entry_id"] for entry in view["entries"] if entry["relative_path"] in ("S1/A.mkv", "S3/C.mkv")],
        now=engine.clock())
    await settle(engine, 4)
    offer(providers["parcel-b"], target, "y")
    await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    held = await first_hold(engine, transfer.id, "parcel-b", ticks=6)
    return repository, transfer, held


async def _members(repository, transfer_id):
    return sorted((item.entry.relative_path, item.request.payload)
                  for item in await repository.requests(transfer_id) if item.parent_id)


@pytest.mark.asyncio
async def test_an_uploaded_tree_binds_two_fingerprintless_lists_and_carries_the_selection(tmp_path, monkeypatch):
    from test_v113_root_provider_switch import SEASONS
    wrapped = [(name, f"Release Label/{path}", size) for name, path, size in SEASONS]
    repository, transfer, held = await upload_switched(tmp_path, monkeypatch, wrapped)
    assert held is None
    assert await _members(repository, transfer.id) == [("S1/A.mkv", "y:Release Label/S1/A.mkv"),
                                                       ("S3/C.mkv", "y:Release Label/S3/C.mkv")]


@pytest.mark.asyncio
async def test_an_uploaded_tree_that_is_not_the_established_one_proves_nothing(tmp_path, monkeypatch):
    from test_v113_root_provider_switch import SEASONS
    other = metainfo(multi(name=b"Show", files=[
        {b"length": 100, b"path": [b"S1", b"A.mkv"]}, {b"length": 200, b"path": [b"S2", b"B.mkv"]},
        {b"length": 300, b"path": [b"S3", b"C.mkv"]}, {b"length": 400, b"path": [b"S4", b"D.mkv"]}]))
    wrapped = [(name, f"Show/{path}", size) for name, path, size in SEASONS]
    repository, transfer, held = await upload_switched(tmp_path, monkeypatch, wrapped, data=other)
    # No correspondence is no contradiction: never held as final, the
    # ordinary retried refusal (Finding A).
    from transfers.errors import Permanence
    root = next(item for item in await repository.requests(transfer.id) if item.parent_id is None)
    assert held is None and root.error is not None and root.error.diagnostic == "coordinate_no_correspondence"
    assert root.error.permanence == Permanence.UNKNOWN
    assert await _members(repository, transfer.id) == [("S1/A.mkv", "x:S1/A.mkv"), ("S3/C.mkv", "x:S3/C.mkv")]


# -- Finding C: an upload is named by the torrent, before its collection root freezes ------------------------------

def test_the_identity_carries_only_a_safe_declared_name():
    assert torrent_identity(UPLOAD).name == "The Real Show"
    for unsafe in (b"\xff\xfe", b"..", b"a/b", b""):
        assert torrent_identity(metainfo(multi(name=unsafe))).name is None


@pytest.mark.asyncio
@pytest.mark.parametrize("declared, expected", [(b"The Real Show", "The Real Show"), (b"\xff\xfe", "x")],
                         ids=["declared", "undecodable-falls-back"])
async def test_an_upload_is_admitted_under_its_torrent_s_own_name_not_its_file_name(declared, expected):
    admitted = []

    async def submit(requests, **options):
        admitted.append((requests, options))

    data = metainfo(multi(name=declared))
    await ApplicationService.submit_torrent(SimpleNamespace(submit=submit), data, "x.torrent")
    ((request,), options), = admitted
    assert options["name"] == expected
    assert request.name == "x.torrent"                         # the input artifact, as providers receive it
    assert request.fingerprint == extract_hash_from_torrent(data) and request.payload == data


@pytest.mark.asyncio
async def test_a_premiumize_first_upload_reports_the_torrent_s_name_and_a_magnet_its_own():
    _provider, _transport, observed = await resolved(TORRENT, directdl())
    assert observed.name == "The Real Show" and observed.resource.context["name"] == "The Real Show"
    other = TransferRequest("torrent", metainfo(multi(name=b"Other")), "Other.torrent", DIGEST)   # not the asked torrent
    _provider, _transport, observed = await resolved(other, directdl())
    assert observed.name == "Other.torrent"
    magnet = TransferRequest("magnet", f"magnet:?xt=urn:btih:{DIGEST}&dn=Complete+Series", "Complete Series", DIGEST)
    _provider, _transport, observed = await resolved(magnet, directdl())
    assert observed.name == "Complete Series"                   # a display name stays what it is


@pytest.mark.asyncio
async def test_the_collection_root_freezes_under_the_torrent_s_name_and_never_moves_after(tmp_path, monkeypatch):
    """Through the shared submission and naming owners, with a neutral
    provider that reports no name of its own: the first fan-out freezes the
    torrent's ``info.name`` as the collection root. A later, different name a
    provider reports renames the transfer only -- never the frozen root or a
    placed destination."""
    from dataclasses import replace as _replace

    from test_v113_collection_route_generic_closure import Clock
    from test_v113_root_provider_switch import magnet_provider, rows, settle

    from db import database
    from fake_integrations import MemoryExecutor
    from transfers.convergence_engine import TransferEngine
    from transfers.policy import TransferPolicy
    from transfers.recovery_repository import TransferRepository
    from transfers.registry import IntegrationRegistry

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "naming.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    provider = magnet_provider("parcel-a")
    provider.descriptor = _replace(provider.descriptor, request_types=provider.descriptor.request_types | {"torrent"})
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3), clock=Clock())
    await engine.initialize()
    application = ApplicationService(engine)

    async def publish(_kind, _payload):
        return None

    monkeypatch.setattr("application.service.publish", publish)
    data = metainfo(multi(name=b"The Real Show", files=[{b"length": 100, b"path": [b"S1", b"A.mkv"]},
                                                         {b"length": 200, b"path": [b"S2", b"B.mkv"]}]))
    request = TransferRequest("torrent", data, "x.torrent", extract_hash_from_torrent(data))
    result = provider.parcel("x", state=ResourceState.AVAILABLE,
                             files=[("A.mkv", "S1/A.mkv", 100), ("B.mkv", "S2/B.mkv", 200)])
    observed = _replace(result.observation, request=request, name="")
    provider.resources[observed.resource.id] = observed
    provider.responses.append(_replace(result, observation=observed))
    await application.submit_torrent(data, "x.torrent")
    (row,) = await rows("SELECT id,name,collection_root FROM torrents")
    assert (row["name"], row["collection_root"]) == ("The Real Show", None)          # named before anything placed
    await settle(engine, 4)
    placed = sorted(item["local_path"] for item in await rows("SELECT local_path FROM download_files"))
    (row,) = await rows("SELECT name,collection_root FROM torrents")
    assert row["collection_root"] == "The Real Show"
    assert placed == [str(tmp_path / "dl" / "The Real Show" / "S1" / "A.mkv"),
                      str(tmp_path / "dl" / "The Real Show" / "S2" / "B.mkv")]
    provider.resources[observed.resource.id] = _replace(observed, name="Renamed By Provider")
    await settle(engine, 2)
    (row,) = await rows("SELECT name,collection_root FROM torrents")
    assert row["collection_root"] == "The Real Show"                                  # frozen at its boundary
    assert sorted(item["local_path"] for item in await rows("SELECT local_path FROM download_files")) == placed
