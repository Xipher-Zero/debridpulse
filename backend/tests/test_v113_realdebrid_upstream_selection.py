"""Real-Debrid upstream file selection: DebridPulse's authorized selection,
expressed to Real-Debrid by its own file ids (#592 against #591).

DebridPulse decides which members a transfer wants; Real-Debrid is told
exactly those, and only once they are decided (``contracts.UpstreamSelection``,
``TransferRepository.upstream_selection``). Real-Debrid's answer is judged by
what its torrent then states and what its links prove: individual links carry
the selection, an archive or a partial delivery is refused truthfully and
never executed. Every Real-Debrid answer here is a local fake; nothing reaches
a live account.
"""
from __future__ import annotations

import asyncio

import pytest
from test_v113_collection_route_generic_closure import Clock
from test_v113_root_provider_switch import magnet_provider, offer_source, rows

from db import database
from fake_integrations import MemoryExecutor
from providers.realdebrid.client import RealDebridAPIError
from providers.realdebrid.provider import RealDebridProvider
from transfers import codec
from transfers.convergence_engine import TransferEngine
from transfers.errors import (
    Category, Confidence, Domain, EvidenceBasis, MutationOutcome, Origin, Permanence, Retryability, Stage,
    TransferError,
)
from transfers.models import FileManifestEntry, ResourceState, TransferRequest
from transfers.policy import TransferPolicy, provider_attributable
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

WHALE = "The.Whale.2022.HDR.2160p.WEB.H265-NAISU[TGx]"
MOVIE = "the.whale.2022.hdr.2160p.web.h265-naisu.mkv"
MOVIE_BYTES = 22_576_859_233
NFO = "the.whale.2022.hdr.2160p.web.h265-naisu.nfo"
# Transfer 592: the movie, two .txt, an .nfo and an .sfv inside the torrent's
# own folder (22,576,862,416 bytes in all).
FILES_592 = [
    (1, "NEW upcoming releases by Xclusive.txt", 175),
    (2, "[TGx]Downloaded from torrentgalaxy.to .txt", 718),
    (3, MOVIE, MOVIE_BYTES),
    (4, NFO, 400),
    (5, "the.whale.2022.hdr.2160p.web.h265-naisu.sfv", 1890),
]
RAR = f"{WHALE}.rar"
RAR_BYTES = 22_576_862_844
HASH_592 = "c58284abdf4200544b501b2c5a031c5841f6e0bb"
HASH_591 = "dd74e805ea524bbeb3332ff95c20ebf2adcc725f"
MAGNET_592 = f"magnet:?xt=urn:btih:{HASH_592}"
MAGNET_591 = f"magnet:?xt=urn:btih:{HASH_591}"


class Torrent:
    def __init__(self, native_id, name, files, *, wrapped, fingerprint, converting=0, archive=False,
                 preselected=False, ready=True):
        self.id, self.name, self.fingerprint = native_id, name, fingerprint
        self.files = [{"id": file_id, "path": f"/{name}/{path}" if wrapped else f"/{path}", "bytes": size,
                       "selected": int(preselected)} for file_id, path, size in files]
        self.converting, self.archive, self.selected, self.ready = converting, archive, preselected, ready

    def native(self):
        if self.converting:
            self.converting -= 1
            return {"id": self.id, "filename": "", "hash": self.fingerprint, "bytes": 0, "progress": 0,
                    "status": "magnet_conversion", "speed": 0, "files": [], "links": []}
        chosen = [record for record in self.files if record["selected"]]
        links = ([] if not self.ready else
                 [f"https://real-debrid.com/d/{self.id}-archive"] if self.archive and chosen else
                 [f"https://real-debrid.com/d/{self.id}-{record['id']}" for record in chosen])
        return {"id": self.id, "filename": self.name, "original_filename": self.name, "hash": self.fingerprint,
                "bytes": sum(record["bytes"] for record in chosen),
                "progress": 100 if self.selected and self.ready else 0,
                "status": ("downloaded" if self.ready else "downloading") if self.selected
                else "waiting_files_selection", "speed": 0,
                "files": [dict(record) for record in self.files], "links": links}


class FakeRealDebrid:
    """A Real-Debrid account: ``add_magnet`` creates a torrent waiting for its
    file selection; ``select_files`` selects exactly the ids it is given (and,
    being cached, finishes at once); links are one per selected file, or one
    archive of them all when the torrent is ``archive``. ``select_errors``
    script the next selections' failures -- ``"lost"`` applies the selection
    and then loses the answer. Not ``ready``, a selected torrent keeps
    downloading until the test makes it ready; ``during_select`` runs while
    a selection is being answered (an operator action landing meanwhile)."""
    configured = True

    def __init__(self, files=FILES_592, *, name=WHALE, wrapped=True, fingerprint=HASH_592, converting=0,
                 archive=False, preselected=False, ready=True):
        self.template = dict(files=files, name=name, wrapped=wrapped, fingerprint=fingerprint,
                             converting=converting, archive=archive, preselected=preselected, ready=ready)
        self.torrents: dict[str, Torrent] = {}
        self.calls, self.select_errors, self.during_select = [], [], None

    def secrets(self):
        return ("refresh-token-value",)

    def selects(self):
        return [call[2] for call in self.calls if call[0] == "select_files"]

    def count(self, name):
        return len([call for call in self.calls if call[0] == name])

    async def add_magnet(self, magnet):
        self.calls.append(("add_magnet",))
        native_id = f"T{len(self.torrents) + 1}"
        template = dict(self.template)
        self.torrents[native_id] = Torrent(native_id, template.pop("name"), template.pop("files"), **template)
        return {"id": native_id, "uri": f"https://api.real-debrid.com/rest/1.0/torrents/info/{native_id}"}

    async def torrent_info(self, native_id):
        self.calls.append(("torrent_info", native_id))
        if native_id not in self.torrents:
            raise RealDebridAPIError(7, "unknown_ressource", 404)
        return self.torrents[native_id].native()

    async def select_files(self, native_id, files):
        self.calls.append(("select_files", native_id, files))
        if self.during_select is not None:
            await self.during_select()
        failure = self.select_errors.pop(0) if self.select_errors else None
        if isinstance(failure, Exception):
            raise failure
        torrent = self.torrents[native_id]
        wanted = set(files.split(","))
        for record in torrent.files:
            record["selected"] = int(str(record["id"]) in wanted)
        torrent.selected = True
        if failure == "lost":
            raise asyncio.TimeoutError()
        return 204

    async def unrestrict_link(self, link):
        self.calls.append(("unrestrict_link", link))
        native_id, _, part = link.rsplit("/", 1)[1].partition("-")
        torrent = self.torrents[native_id]
        if part == "archive":
            return {"filename": RAR if torrent.name == WHALE else f"{torrent.name}.rar",
                    "filesize": sum(record["bytes"] for record in torrent.files if record["selected"]) + 428,
                    "download": f"https://cdn.real-debrid.example/d/{native_id}/archive"}
        (record,) = [record for record in torrent.files if str(record["id"]) == part]
        return {"filename": record["path"].rsplit("/", 1)[1], "filesize": record["bytes"],
                "download": f"https://cdn.real-debrid.example/d/{native_id}/{part}"}

    async def delete_torrent(self, native_id):
        self.calls.append(("delete_torrent", native_id))
        self.torrents.pop(native_id, None)

    async def torrents_page(self, page, limit=5000):
        self.calls.append(("torrents_page", page))
        records = [torrent.native() for torrent in self.torrents.values()] if page == 1 else []
        return records, len(self.torrents)

    async def active_count(self):
        return {"nb": len(self.torrents), "limit": 25}

    async def user(self):
        return {"id": 1}


def member(path, size):
    return FileManifestEntry(path.rsplit("/", 1)[-1], path, size)


MOVIE_ONLY = (member(MOVIE, MOVIE_BYTES),)
EVERY_592 = tuple(member(path, size) for _id, path, size in FILES_592)


async def created(fake):
    provider = RealDebridProvider(fake)
    result = await provider.resolve(TransferRequest("magnet", MAGNET_592, fingerprint=HASH_592))
    return provider, result.observation.resource


def no_capability_leaks(error):
    durable = codec.dump(error)
    assert "cdn.real-debrid.example" not in durable and "/d/T" not in durable and "magnet:" not in durable
    assert "refresh-token-value" not in durable


# -- A: the #591 and #592 shapes at the adapter ------------------------------------------------------------------

def test_real_debrid_declares_the_capability_and_core_names_no_provider():
    from pathlib import Path

    from transfers.contracts import UpstreamSelection

    assert isinstance(RealDebridProvider(FakeRealDebrid()), UpstreamSelection)
    assert not isinstance(magnet_provider("parcel-a"), UpstreamSelection)
    core = Path(__file__).resolve().parents[1] / "transfers"
    for source in ("contracts.py", "engine.py", "repository.py"):
        text = (core / source).read_text().casefold()
        assert "realdebrid" not in text and "real-debrid" not in text and "selectfiles" not in text


@pytest.mark.asyncio
async def test_creating_the_592_torrent_selects_nothing_and_its_whole_file_list_is_selectable():
    fake = FakeRealDebrid()
    provider, resource = await created(fake)
    observation = await provider.observe(resource)
    assert observation.state == ResourceState.PREPARING and observation.error is None
    assert [(entry.relative_path, entry.expected_bytes) for entry in observation.file_manifest.entries] == [
        (path, size) for _id, path, size in FILES_592]                    # the torrent's folder removed once
    assert fake.selects() == []


@pytest.mark.asyncio
async def test_a_movie_only_selection_sends_only_the_movie_s_native_id_and_its_own_link_is_the_movie():
    fake = FakeRealDebrid()
    provider, resource = await created(fake)
    await provider.synchronize_selection(resource, MOVIE_ONLY)
    assert fake.selects() == ["3"]                                       # never "all", never all five ids
    assert (await provider.observe(resource)).state == ResourceState.AVAILABLE
    assert await provider.synchronize_selection(resource, MOVIE_ONLY) is False   # verified, never sent again
    assert fake.selects() == ["3"]
    (entry,) = await provider.manifest(resource)
    assert (entry.relative_path, entry.expected_bytes) == (MOVIE, MOVIE_BYTES)
    assert entry.request.payload == "https://real-debrid.com/d/T1-3"


@pytest.mark.asyncio
async def test_the_591_single_file_torrent_selects_its_only_file_by_its_id():
    fake = FakeRealDebrid([(1, MOVIE, MOVIE_BYTES)], name=MOVIE, wrapped=False, fingerprint=HASH_591)
    provider = RealDebridProvider(fake)
    resource = (await provider.resolve(TransferRequest("magnet", MAGNET_591))).observation.resource
    (only,) = (await provider.observe(resource)).file_manifest.entries
    await provider.synchronize_selection(resource, (only,))
    assert fake.selects() == ["1"]
    (entry,) = await provider.manifest(resource)
    assert (entry.relative_path, entry.expected_bytes) == (MOVIE, MOVIE_BYTES)


@pytest.mark.asyncio
async def test_native_ids_are_found_by_path_and_size_never_by_position():
    shuffled = [(17, path, size) for _id, path, size in FILES_592 if path == MOVIE] + [
        (11 + index, path, size) for index, (_id, path, size) in enumerate(FILES_592) if path != MOVIE]
    fake = FakeRealDebrid(list(reversed(shuffled)))
    provider, resource = await created(fake)
    await provider.synchronize_selection(resource, MOVIE_ONLY)
    assert fake.selects() == ["17"]


@pytest.mark.asyncio
async def test_a_settled_all_enumerates_every_file_and_is_never_reduced_to_the_media():
    fake = FakeRealDebrid()
    provider, resource = await created(fake)
    await provider.synchronize_selection(resource, EVERY_592)
    assert fake.selects() == ["1,2,3,4,5"]                               # .txt, .nfo and .sfv included


@pytest.mark.asyncio
async def test_two_selected_files_with_their_own_links_are_each_proven_whatever_the_link_order():
    fake = FakeRealDebrid()
    provider, resource = await created(fake)
    chosen = (member(MOVIE, MOVIE_BYTES), member(NFO, 400))
    await provider.synchronize_selection(resource, chosen)
    fake.torrents["T1"].native = (lambda original: lambda: {**original(), "links": list(reversed(original()["links"]))})(
        fake.torrents["T1"].native)
    entries = await provider.manifest(resource)
    assert [(entry.relative_path, entry.request.payload) for entry in entries] == [
        (MOVIE, "https://real-debrid.com/d/T1-3"), (NFO, "https://real-debrid.com/d/T1-4")]


# -- A/D: what Real-Debrid delivers instead of the files -------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("chosen, selected", [(MOVIE_ONLY, 1), (EVERY_592, 5),
                                              ((member(MOVIE, MOVIE_BYTES), member(NFO, 400)), 2)],
                         ids=["movie-only", "settled-all", "two-files"])
async def test_an_archive_in_place_of_the_selected_files_is_refused_truthfully_and_never_executed(chosen, selected):
    fake = FakeRealDebrid(archive=True)
    provider, resource = await created(fake)
    await provider.synchronize_selection(resource, chosen)
    with pytest.raises(TransferError) as refused:
        await provider.manifest(resource)
    error = refused.value.error
    assert (error.domain, error.category, error.stage) == (
        Domain.PROVIDER, Category.CANDIDATE_REJECTED, Stage.CANDIDATE_PREPARATION)
    assert error.diagnostic == "a torrent link is an archive, not a selected file"
    assert (error.retryability, error.permanence) == (Retryability.NEVER, Permanence.PERMANENT)
    assert provider_attributable(error)                                  # this provider's route, nothing else
    assert error.evidence_basis == EvidenceBasis.STRUCTURED and error.native_code == ""   # observed, no code
    evidence = error.as_dict(diagnostics=True)["diagnostic_evidence"]
    assert evidence["archive_indicated"] is True
    assert (evidence["native_file_count"], evidence["native_selected_count"], evidence["link_count"]) == (
        5, selected, 1)
    assert evidence["link_identity"]["returned"] == {"filename": RAR, "filesize": sum(
        entry.expected_bytes for entry in chosen) + 428}
    assert evidence["link_identity"]["reason"] == "no_member"
    no_capability_leaks(error)


@pytest.mark.asyncio
async def test_a_link_matching_no_file_without_archive_evidence_stays_the_unidentified_link():
    fake = FakeRealDebrid(archive=True)
    provider, resource = await created(fake)
    await provider.synchronize_selection(resource, EVERY_592)
    fake.unrestrict_link = lambda link: _answer({"filename": "bundle.bin", "filesize": 7,
                                                 "download": "https://cdn.real-debrid.example/x"})
    with pytest.raises(TransferError) as refused:
        await provider.manifest(resource)
    error = refused.value.error
    assert (error.category, error.diagnostic) == (Category.PROVIDER_PROTOCOL_VIOLATION,
                                                  "a torrent link matches no file")
    assert "archive_indicated" not in error.as_dict(diagnostics=True)["diagnostic_evidence"]


async def _answer(value):
    return value


@pytest.mark.asyncio
async def test_selected_files_some_without_their_own_link_are_a_partial_delivery_never_a_success():
    fake = FakeRealDebrid()
    provider, resource = await created(fake)
    await provider.synchronize_selection(resource, (member(MOVIE, MOVIE_BYTES), member(NFO, 400)))
    fake.torrents["T1"].native = (lambda original: lambda: {**original(), "links": original()["links"][:1]})(
        fake.torrents["T1"].native)
    with pytest.raises(TransferError) as refused:
        await provider.manifest(resource)
    error = refused.value.error
    assert (error.category, error.diagnostic) == (Category.PROVIDER_PROTOCOL_VIOLATION,
                                                  "torrent links do not cover its selected files")
    # The whole normalized refusal: inferred from Real-Debrid's structured
    # file and link lists alone, so its basis is the observation, never a
    # native code it does not have; the protocol-violation semantics (unknown
    # retryability, provider-scoped failover) are the category's own.
    assert (error.domain, error.stage, error.origin, error.integration_id) == (
        Domain.PROVIDER, Stage.CANDIDATE_PREPARATION, Origin.PROVIDER, "realdebrid")
    assert (error.retryability, error.permanence, error.confidence) == (
        Retryability.UNKNOWN, Permanence.UNKNOWN, Confidence.HIGH)
    assert (error.evidence_basis, error.native_code) == (EvidenceBasis.STRUCTURED, "")
    assert error.mutation == MutationOutcome.NOT_COMMITTED and provider_attributable(error)
    evidence = error.as_dict(diagnostics=True)["diagnostic_evidence"]
    assert (evidence["proven_member_count"], evidence["uncovered_selected_count"]) == (1, 1)
    assert (evidence["native_file_count"], evidence["native_selected_count"], evidence["link_count"]) == (5, 2, 1)
    no_capability_leaks(error)


# -- C: the selection request is a remote mutation -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_torrent_already_selected_for_another_subset_is_refused_never_reselected():
    fake = FakeRealDebrid()
    provider, resource = await created(fake)
    await provider.synchronize_selection(resource, EVERY_592)            # an earlier selection: all five
    with pytest.raises(TransferError) as refused:
        await provider.synchronize_selection(resource, MOVIE_ONLY)
    error = refused.value.error
    assert (error.domain, error.category, error.retryability, error.permanence) == (
        Domain.PROVIDER, Category.RESOURCE_STATE_CONFLICT, Retryability.NEVER, Permanence.PERMANENT)
    assert error.diagnostic == "the torrent's selected files are not this selection"
    assert error.evidence_basis == EvidenceBasis.STRUCTURED and error.native_code == ""   # observed, no code
    assert provider_attributable(error)
    evidence = error.as_dict(diagnostics=True)["diagnostic_evidence"]
    assert (evidence["authorized_member_count"], evidence["requested_native_id_count"],
            evidence["native_selected_count"], evidence["outcome"]) == (1, 1, 5, "mismatch")
    assert sorted(item["native_id"] for item in evidence["disagreeing_files"]) == [1, 2, 4, 5]
    assert fake.selects() == ["1,2,3,4,5"]                               # nothing sent for the other subset


@pytest.mark.asyncio
async def test_a_lost_answer_is_uncertain_and_the_torrent_decides_whether_anything_is_sent_again():
    fake = FakeRealDebrid()
    provider, resource = await created(fake)
    fake.select_errors = ["lost"]                                        # applied, answer lost
    with pytest.raises(TransferError) as lost:
        await provider.synchronize_selection(resource, MOVIE_ONLY)
    assert lost.value.error.mutation == MutationOutcome.UNCERTAIN
    assert lost.value.error.category == Category.CONNECTION_TIMEOUT      # the ordinary transient retry
    await provider.synchronize_selection(resource, MOVIE_ONLY)          # read first: it took effect
    assert fake.selects() == ["3"]

    second = FakeRealDebrid()
    provider, resource = await created(second)
    second.select_errors = [asyncio.TimeoutError()]                      # not applied, answer lost
    with pytest.raises(TransferError):
        await provider.synchronize_selection(resource, MOVIE_ONLY)
    await provider.synchronize_selection(resource, MOVIE_ONLY)          # still waiting: the same set again
    assert second.selects() == ["3", "3"]

    from providers.realdebrid.client import OAUTH_GRANT_REJECTED, RealDebridRequestNotSent
    third = FakeRealDebrid()
    provider, resource = await created(third)
    third.select_errors = [RealDebridRequestNotSent(RealDebridAPIError(None, OAUTH_GRANT_REJECTED, 400))]
    with pytest.raises(TransferError) as unsent:                         # never sent: nothing uncertain
        await provider.synchronize_selection(resource, MOVIE_ONLY)
    assert unsent.value.error.mutation != MutationOutcome.UNCERTAIN
    assert unsent.value.error.category == Category.CREDENTIAL_INVALID


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal, category, retryability, native", [
    (RealDebridAPIError(2, "parameter_bad_value", 400), Category.CANDIDATE_REJECTED, Retryability.NEVER, "2"),
    (RealDebridAPIError(35, "infringing_file", 451), Category.CANDIDATE_REJECTED, Retryability.NEVER, "35"),
    (RealDebridAPIError(25, "service_unavailable", 503), Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF, "25"),
], ids=["selection-refused", "infringing-unchanged", "transient"])
async def test_real_debrid_s_refusal_of_the_selection_keeps_its_native_code(refusal, category, retryability, native):
    fake = FakeRealDebrid()
    provider, resource = await created(fake)
    fake.select_errors = [refusal]
    with pytest.raises(TransferError) as refused:
        await provider.synchronize_selection(resource, MOVIE_ONLY)
    error = refused.value.error
    assert (error.category, error.retryability, error.native_code) == (category, retryability, native)
    assert error.evidence_basis == EvidenceBasis.NATIVE_CODE                # Real-Debrid's own code decided
    assert error.mutation != MutationOutcome.UNCERTAIN and provider_attributable(error)
    no_capability_leaks(error)


@pytest.mark.asyncio
async def test_files_that_are_not_this_torrent_s_are_never_mapped_and_nothing_is_sent():
    fake = FakeRealDebrid()
    provider, resource = await created(fake)
    with pytest.raises(TransferError) as refused:
        await provider.synchronize_selection(resource, (member(MOVIE, MOVIE_BYTES + 1),))
    assert refused.value.error.diagnostic == "the selected files are not this torrent's files"
    assert refused.value.error.as_dict(diagnostics=True)["diagnostic_evidence"]["outcome"] == "unmapped"
    assert refused.value.error.evidence_basis == EvidenceBasis.STRUCTURED
    assert fake.selects() == []


def with_native_ids(*identifiers):
    """FILES_592 as Real-Debrid might number it: the same paths and sizes,
    each file given the native id at its position."""
    return [(identifier, path, size) for identifier, (_id, path, size) in zip(identifiers, FILES_592)]


@pytest.mark.asyncio
@pytest.mark.parametrize("identifiers", [
    (1, 2, 3, 3, 5),                    # the .nfo, never authorized, shares the movie's id
    (1, 2, 3, 4, 1),                    # two unselected files share one id
    (1, 2, 3, 0, 5),                    # an unselected file's id is not positive
    (1, 2, 3, "4", 5),                  # an unselected file's id is not an integer
    (1, 2, 3, True, 5),                 # nor is a boolean
    (1, 2, 3, None, 5),                 # an unselected file has no id
], ids=["shared-with-authorized", "shared-between-unselected", "zero", "text", "boolean", "missing"])
async def test_native_ids_that_do_not_name_each_file_once_are_refused_before_anything_is_sent(identifiers):
    """Real-Debrid's ids are judged across the torrent's WHOLE file list,
    never only the authorized members': an id two files share would select a
    file DebridPulse never authorized, and a later comparison by id would
    accept that overbroad selection. Refused for this torrent on this
    provider, from Real-Debrid's own answer, before any selection is sent --
    permanent: reading the same torrent again cannot change it."""
    fake = FakeRealDebrid(with_native_ids(*identifiers))
    provider, resource = await created(fake)
    with pytest.raises(TransferError) as refused:
        await provider.synchronize_selection(resource, MOVIE_ONLY)
    error = refused.value.error
    assert (error.domain, error.category, error.retryability, error.permanence) == (
        Domain.PROVIDER, Category.RESOURCE_STATE_CONFLICT, Retryability.NEVER, Permanence.PERMANENT)
    assert error.diagnostic == "Real-Debrid's file ids do not name each file once"
    assert error.evidence_basis == EvidenceBasis.STRUCTURED and error.native_code == ""
    assert error.mutation != MutationOutcome.UNCERTAIN and provider_attributable(error)
    evidence = error.as_dict(diagnostics=True)["diagnostic_evidence"]
    assert (evidence["outcome"], evidence["native_file_count"], evidence["authorized_member_count"]) == (
        "ambiguous_native_ids", 5, 1)
    assert fake.selects() == []                                          # nothing sent, nothing selected
    assert not any(record["selected"] for record in fake.torrents["T1"].files)
    no_capability_leaks(error)


@pytest.mark.asyncio
async def test_a_shared_native_id_never_lets_an_overbroad_selection_compare_equal():
    """The torrent already holds the movie AND the .nfo, which share the
    movie's id: comparing by id would call it the movie-only selection. It is
    refused instead, never accepted, never reselected."""
    fake = FakeRealDebrid(with_native_ids(1, 2, 3, 3, 5))
    provider, resource = await created(fake)
    torrent = fake.torrents["T1"]
    for record in torrent.files:
        record["selected"] = int(record["id"] == 3)
    torrent.selected = True
    with pytest.raises(TransferError) as refused:
        await provider.synchronize_selection(resource, MOVIE_ONLY)
    assert refused.value.error.category == Category.RESOURCE_STATE_CONFLICT
    assert refused.value.error.diagnostic == "Real-Debrid's file ids do not name each file once"
    assert fake.selects() == []


@pytest.mark.asyncio
async def test_a_magnet_still_converting_has_nothing_to_select_yet():
    fake = FakeRealDebrid(converting=2)
    provider, resource = await created(fake)                             # its first observation: converting
    await provider.synchronize_selection(resource, MOVIE_ONLY)          # the second: still converting
    assert fake.selects() == []
    await provider.synchronize_selection(resource, MOVIE_ONLY)
    assert fake.selects() == ["3"]


@pytest.mark.asyncio
async def test_an_expired_member_link_is_refreshed_without_touching_the_torrent():
    fake = FakeRealDebrid()
    provider, resource = await created(fake)
    await provider.synchronize_selection(resource, MOVIE_ONLY)
    (entry,) = await provider.manifest(resource)
    calls = len(fake.calls)
    result = await provider.resolve(entry.request)                       # what a refresh re-resolves
    assert result.state == ResourceState.AVAILABLE and result.candidates[0].expected_bytes == MOVIE_BYTES
    assert [call[0] for call in fake.calls[calls:]] == ["unrestrict_link"]
    assert fake.count("add_magnet") == 1 and fake.selects() == ["3"]


# -- B/C: the selection lifecycle through the real engine and repository ---------------------------------------------

async def lab(tmp_path, monkeypatch, *providers, fresh=True, clock=None):
    if fresh:
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "upstream.sqlite3")
        await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    for provider in providers:
        registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3), clock=clock or Clock())
    await engine.initialize()
    return repository, engine


async def ticks(engine, count=4, step=1.0):
    for _ in range(count):
        engine.clock.now += step
        await engine.tick()


async def submit(engine, magnet=MAGNET_592, fingerprint=HASH_592, *, interactive=True, preferred=None):
    return await engine.submit((TransferRequest(
        "magnet", magnet, name=WHALE, fingerprint=fingerprint,
        selection_mode="interactive" if interactive else None, preferred_provider=preferred),),
        name=WHALE, deduplicate=False)


async def root(repository, transfer):
    return next(item for item in await repository.requests(transfer.id) if item.parent_id is None)


async def members(repository, transfer):
    return sorted((item.entry.relative_path, item.request.payload)
                  for item in await repository.requests(transfer.id) if item.parent_id)


async def choose(repository, engine, transfer, paths):
    view = await repository.file_selection_presentation(transfer.id, now=engine.clock())
    await repository.confirm_file_selection(transfer.id, view["manifest_id"], [
        entry["entry_id"] for entry in view["entries"] if entry["relative_path"] in paths], now=engine.clock())


@pytest.mark.asyncio
async def test_the_first_interactive_root_offers_every_file_selects_nothing_then_exactly_the_choice(
        tmp_path, monkeypatch):
    """No premature ``all``, no deadlock, no stall: the file list is offered
    while Real-Debrid waits, the operator's wait is no failure, and Confirm
    sends exactly the movie's id -- the root then fans out the movie alone."""
    fake = FakeRealDebrid()
    repository, engine = await lab(tmp_path, monkeypatch, RealDebridProvider(fake))
    transfer = await submit(engine)
    await ticks(engine)
    view = await repository.file_selection_presentation(transfer.id, now=engine.clock())
    assert sorted(entry["relative_path"] for entry in view["entries"]) == sorted(path for _i, path, _s in FILES_592)
    assert fake.selects() == []
    record = await root(repository, transfer)
    assert record.error is None and record.state == "waiting"            # the operator's wait, not a failure
    await choose(repository, engine, transfer, {MOVIE})
    await ticks(engine)
    assert fake.selects() == ["3"]
    assert await members(repository, transfer) == [(MOVIE, "https://real-debrid.com/d/T1-3")]
    assert fake.count("add_magnet") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("settle_by", ["close", "timeout"])
async def test_close_or_the_decision_timeout_settles_all_and_real_debrid_receives_every_file(
        tmp_path, monkeypatch, settle_by):
    fake = FakeRealDebrid()
    repository, engine = await lab(tmp_path, monkeypatch, RealDebridProvider(fake))
    transfer = await submit(engine)
    await ticks(engine)
    assert fake.selects() == []
    if settle_by == "close":
        view = await repository.file_selection_presentation(transfer.id, now=engine.clock())
        await repository.dismiss_file_selection(transfer.id, view["manifest_id"], now=engine.clock())
        await ticks(engine)
    else:
        await ticks(engine, 6, step=30.0)                                # past the 120 s decision hold
    assert fake.selects() == ["1,2,3,4,5"]
    assert [path for path, _ in await members(repository, transfer)] == sorted(path for _i, path, _s in FILES_592)


@pytest.mark.asyncio
async def test_the_591_single_file_torrent_selects_its_file_and_downloads_it(tmp_path, monkeypatch):
    fake = FakeRealDebrid([(1, MOVIE, MOVIE_BYTES)], name=MOVIE, wrapped=False, fingerprint=HASH_591)
    repository, engine = await lab(tmp_path, monkeypatch, RealDebridProvider(fake))
    transfer = await submit(engine, MAGNET_591, HASH_591)
    await ticks(engine)
    assert fake.selects() == ["1"]
    assert await members(repository, transfer) == [(MOVIE, "https://real-debrid.com/d/T1-1")]


@pytest.mark.asyncio
async def test_a_restart_never_resends_a_selection_the_torrent_already_reflects(tmp_path, monkeypatch):
    """Created, then restarted before any selection: still offered, nothing
    sent. Confirmed, the answer lost after Real-Debrid applied it, then
    restarted: the durable selection is regenerated and the torrent read --
    it is verified, never sent again."""
    fake = FakeRealDebrid()
    clock = Clock()
    repository, engine = await lab(tmp_path, monkeypatch, RealDebridProvider(fake), clock=clock)
    transfer = await submit(engine)
    await ticks(engine)
    repository, engine = await lab(tmp_path, monkeypatch, RealDebridProvider(fake), fresh=False, clock=clock)
    await ticks(engine)
    assert fake.selects() == [] and fake.count("add_magnet") == 1
    await choose(repository, engine, transfer, {MOVIE})
    fake.select_errors = ["lost"]
    await ticks(engine, 1)
    assert fake.selects() == ["3"]
    repository, engine = await lab(tmp_path, monkeypatch, RealDebridProvider(fake), fresh=False, clock=clock)
    await ticks(engine, 6, step=5.0)
    assert fake.selects() == ["3"] and fake.count("add_magnet") == 1
    assert await members(repository, transfer) == [(MOVIE, "https://real-debrid.com/d/T1-3")]


def parcel(identity="parcel-b"):
    return magnet_provider(identity)


@pytest.mark.asyncio
async def test_an_archive_after_an_explicit_selection_fails_over_and_the_torrent_is_cleaned_up(tmp_path, monkeypatch):
    """#592's answer, after the movie alone was selected: refused as an
    archive, never executed or unpacked; the root continues on the next
    provider with the same selection, and the owned torrent is removed."""
    fake = FakeRealDebrid(archive=True)
    backup = parcel()
    repository, engine = await lab(tmp_path, monkeypatch, RealDebridProvider(fake), backup)
    offer_source(backup, [(path.rsplit("/", 1)[-1], path, size) for _i, path, size in FILES_592], HASH_592)
    transfer = await submit(engine, preferred="realdebrid")
    await ticks(engine)
    await choose(repository, engine, transfer, {MOVIE})
    await ticks(engine, 8, step=5.0)
    assert fake.selects() == ["3"]
    assert not [payload for _path, payload in await members(repository, transfer) if "real-debrid" in payload]
    attempts = await rows("SELECT provider_id, error FROM resolution_attempts WHERE request_id=? ORDER BY rowid",
                          ((await root(repository, transfer)).id,))
    refused = [codec.error(row["error"]) for row in attempts if row["provider_id"] == "realdebrid" and row["error"]]
    assert refused and refused[-1].category == Category.CANDIDATE_REJECTED
    assert refused[-1].diagnostic == "a torrent link is an archive, not a selected file"
    assert (await root(repository, transfer)).resource.provider_id == "parcel-b"
    assert await members(repository, transfer) == [(MOVIE, f"x:{MOVIE}")]
    await ticks(engine, 4, step=30.0)
    assert ("delete_torrent", "T1") in fake.calls                        # owned: cleaned up by the one owner
    assert fake.count("add_magnet") == 1                                  # never re-created to "fix" it


@pytest.mark.asyncio
async def test_a_torrent_whose_native_ids_are_ambiguous_fails_over_without_selecting_or_executing_anything(
        tmp_path, monkeypatch):
    """Through the real engine: the movie alone is chosen, but Real-Debrid
    numbers the never-authorized .nfo with the movie's id. Nothing is sent to
    Real-Debrid, no Real-Debrid member is created; the refusal is this
    provider's (no transient budget), the root continues on the next provider
    with the same selection, and the owned torrent is removed."""
    fake = FakeRealDebrid(with_native_ids(1, 2, 3, 3, 5))
    backup = parcel()
    repository, engine = await lab(tmp_path, monkeypatch, RealDebridProvider(fake), backup)
    offer_source(backup, [(path.rsplit("/", 1)[-1], path, size) for _i, path, size in FILES_592], HASH_592)
    transfer = await submit(engine, preferred="realdebrid")
    await ticks(engine)
    await choose(repository, engine, transfer, {MOVIE})
    await ticks(engine, 6, step=5.0)
    assert fake.selects() == [] and fake.count("unrestrict_link") == 0
    attempts = await rows("SELECT provider_id, error FROM resolution_attempts WHERE request_id=? ORDER BY rowid",
                          ((await root(repository, transfer)).id,))
    refused = [codec.error(row["error"]) for row in attempts if row["provider_id"] == "realdebrid" and row["error"]]
    assert refused and refused[-1].category == Category.RESOURCE_STATE_CONFLICT
    assert refused[-1].diagnostic == "Real-Debrid's file ids do not name each file once"
    assert refused[-1].evidence_basis == EvidenceBasis.STRUCTURED
    assert (await root(repository, transfer)).resource.provider_id == "parcel-b"
    assert await members(repository, transfer) == [(MOVIE, f"x:{MOVIE}")]   # the movie alone, never the .nfo
    await ticks(engine, 4, step=30.0)
    assert ("delete_torrent", "T1") in fake.calls and fake.count("add_magnet") == 1


@pytest.mark.asyncio
async def test_toggling_the_choice_after_confirm_never_reaches_real_debrid_again(tmp_path, monkeypatch):
    """A confirmed choice is the generation's, first writer wins: toggling it
    afterwards is refused by the one selection owner, so Real-Debrid is asked
    once, for the confirmed movie alone, on one torrent."""
    from transfers import file_selection as fs

    fake = FakeRealDebrid()
    repository, engine = await lab(tmp_path, monkeypatch, RealDebridProvider(fake))
    transfer = await submit(engine)
    await ticks(engine)
    await choose(repository, engine, transfer, {MOVIE})
    await ticks(engine, 1)
    view = await repository.file_selection_presentation(transfer.id, now=engine.clock())
    for chosen in ({MOVIE, NFO}, {NFO}, {MOVIE, NFO}):
        result = await repository.confirm_file_selection(transfer.id, view["manifest_id"], [
            entry["entry_id"] for entry in view["entries"] if entry["relative_path"] in chosen], now=engine.clock())
        assert result.outcome == str(fs.SelectionOutcome.CONFLICT)
        assert result.detail in {"selection_superseded", "materialization_committed"}
        await ticks(engine, 2, step=5.0)
    assert fake.selects() == ["3"] and fake.count("add_magnet") == 1
    assert await members(repository, transfer) == [(MOVIE, "https://real-debrid.com/d/T1-3")]


@pytest.mark.asyncio
async def test_a_torrent_already_selected_for_other_files_is_never_repurposed(tmp_path, monkeypatch):
    """Real-Debrid's torrent already holds another selection (every file, as
    an earlier ``all`` left it): it cannot become the movie-only selection, so
    it is refused for this root, never reselected; the root continues
    elsewhere and the owned torrent is removed -- one torrent, never more."""
    fake = FakeRealDebrid(preselected=True)
    backup = parcel()
    repository, engine = await lab(tmp_path, monkeypatch, RealDebridProvider(fake), backup)
    offer_source(backup, [(path.rsplit("/", 1)[-1], path, size) for _i, path, size in FILES_592], HASH_592)
    transfer = await submit(engine, preferred="realdebrid")
    await ticks(engine)
    await choose(repository, engine, transfer, {MOVIE})
    await ticks(engine, 6, step=5.0)
    assert fake.selects() == [] and fake.count("add_magnet") == 1
    attempts = await rows("SELECT provider_id, error FROM resolution_attempts WHERE request_id=? ORDER BY rowid",
                          ((await root(repository, transfer)).id,))
    refused = [codec.error(row["error"]) for row in attempts if row["provider_id"] == "realdebrid" and row["error"]]
    assert refused and refused[-1].category == Category.RESOURCE_STATE_CONFLICT
    assert refused[-1].diagnostic == "the torrent's selected files are not this selection"
    assert (await root(repository, transfer)).resource.provider_id == "parcel-b"
    assert await members(repository, transfer) == [(MOVIE, f"x:{MOVIE}")]
    await ticks(engine, 4, step=30.0)
    assert ("delete_torrent", "T1") in fake.calls


@pytest.mark.asyncio
async def test_an_inherited_subset_is_selected_exactly_on_real_debrid_after_an_operator_switch_and_back(
        tmp_path, monkeypatch):
    """TorBox-like first (movie chosen) -> the operator switches to
    Real-Debrid: no selection is offered again, Real-Debrid receives the
    movie's id alone, and the movie's destination never moves -- nor when the
    operator switches back."""
    from transfers.manual_route_switch import switch_root_provider

    fake = FakeRealDebrid()
    first = parcel("parcel-a")
    repository, engine = await lab(tmp_path, monkeypatch, first, RealDebridProvider(fake))
    offer_source(first, [(path.rsplit("/", 1)[-1], f"{WHALE}/{path}", size) for _i, path, size in FILES_592],
                 HASH_592)
    transfer = await engine.submit((TransferRequest(
        "magnet", MAGNET_592, name=WHALE, fingerprint=HASH_592, selection_mode="interactive",
        preferred_provider="parcel-a"),), name=WHALE, deduplicate=False)
    await ticks(engine)
    view = await repository.file_selection_presentation(transfer.id, now=engine.clock())
    await repository.confirm_file_selection(transfer.id, view["manifest_id"], [
        entry["entry_id"] for entry in view["entries"] if entry["relative_path"].endswith(MOVIE)], now=engine.clock())
    await ticks(engine)
    targets = await rows("SELECT local_path FROM download_files WHERE torrent_id=?", (transfer.id,))
    assert len(targets) == 1
    await switch_root_provider(engine, transfer.id, "realdebrid", expected_provider_id="parcel-a")
    await ticks(engine, 6)
    assert fake.selects() == ["3"]
    assert (await root(repository, transfer)).resource.provider_id == "realdebrid"
    assert [payload for _path, payload in await members(repository, transfer)] == ["https://real-debrid.com/d/T1-3"]
    assert await rows("SELECT local_path FROM download_files WHERE torrent_id=?", (transfer.id,)) == targets
    offer_source(first, [(path.rsplit("/", 1)[-1], f"{WHALE}/{path}", size) for _i, path, size in FILES_592],
                 HASH_592)
    await switch_root_provider(engine, transfer.id, "parcel-a", expected_provider_id="realdebrid")
    await ticks(engine, 6)
    assert (await root(repository, transfer)).resource.provider_id == "parcel-a"
    assert await rows("SELECT local_path FROM download_files WHERE torrent_id=?", (transfer.id,)) == targets
    assert fake.selects() == ["3"]


def preparing_parcel(identity, files):
    """A neutral manifest provider whose resource keeps reporting PREPARING
    with its complete file list."""
    from transfers.models import ProviderObservation, ResolutionResult

    provider = parcel(identity)
    resource = offer_source(provider, files, HASH_592)
    observed = provider.resources[resource.id]
    provider.resources[resource.id] = ProviderObservation(
        observed.resource, ResourceState.PREPARING, observed.name, observed.fingerprint, observed.progress,
        None, observed.request, file_manifest=observed.file_manifest)
    provider.responses[-1] = ResolutionResult(ResourceState.PREPARING, observation=provider.resources[resource.id])
    return provider


@pytest.mark.asyncio
async def test_a_backup_prepared_before_any_selection_is_never_ready_and_is_selected_once_adopted(
        tmp_path, monkeypatch):
    """A Real-Debrid backup created while nothing is decided waits for its
    selection: it is observed, never selected, never advertised ready and
    never readiness-promoted. Adopted by the operator's switch once the movie
    is chosen, it is taken over -- never created again -- and selected for
    exactly that choice."""
    from transfers.manual_route_switch import switch_root_provider

    fake = FakeRealDebrid()
    realdebrid = RealDebridProvider(fake, prepare_backup_torrents=True)
    first = preparing_parcel("parcel-a", [(path.rsplit("/", 1)[-1], path, size) for _i, path, size in FILES_592])
    repository, engine = await lab(tmp_path, monkeypatch, first, realdebrid)
    transfer = await submit(engine, preferred="parcel-a")
    await ticks(engine, 6, step=30.0)                                    # Prepare Backup Torrents creates it
    (standby,) = await rows("SELECT s.state, s.promoted_at, p.state AS resource_state FROM standby_resources s "
                            "JOIN provider_resources p ON p.id=s.binding_id WHERE s.provider_id=?",
                            ("realdebrid",))
    assert (standby["state"], standby["resource_state"], standby["promoted_at"]) == ("bound", "preparing", None)
    assert fake.count("add_magnet") == 1
    assert fake.selects() == [] and (await root(repository, transfer)).resource.provider_id == "parcel-a"
    await choose(repository, engine, transfer, {MOVIE})
    await ticks(engine, 2)
    assert fake.selects() == []                                          # a backup is never selected ahead
    await switch_root_provider(engine, transfer.id, "realdebrid", expected_provider_id="parcel-a")
    await ticks(engine, 6)
    assert fake.count("add_magnet") == 1 and fake.selects() == ["3"]
    assert (await root(repository, transfer)).resource.provider_id == "realdebrid"
    assert await members(repository, transfer) == [(MOVIE, "https://real-debrid.com/d/T1-3")]


# -- D: a provider that does not declare the capability is unchanged ----------------------------------------------------

def test_only_a_declaring_provider_s_waiting_backup_is_a_switch_target():
    from transfers.manual_route_switch import AVAILABLE, PREPARED, PREPARING, standby_choice

    waiting = {"state": "bound", "resource_state": ResourceState.PREPARING.value}
    assert standby_choice(waiting) == (PREPARING, False)                  # unchanged without the capability
    assert standby_choice(waiting, upstream_selection=True) == (AVAILABLE, True)
    ready = {"state": "bound", "resource_state": ResourceState.AVAILABLE.value}
    assert standby_choice(ready, upstream_selection=True) == standby_choice(ready) == (PREPARED, True)
    creating = {"state": "creating", "resource_state": None}
    assert standby_choice(creating, upstream_selection=True) == (PREPARING, False)


@pytest.mark.asyncio
async def test_a_provider_without_the_capability_keeps_its_preparing_path_unchanged(tmp_path, monkeypatch):
    """A preparing manifest provider's offered choice is never settled while
    it prepares, however long it takes -- exactly as before."""
    from transfers.models import ProviderObservation, ResolutionResult

    preparing = parcel("parcel-a")
    repository, engine = await lab(tmp_path, monkeypatch, preparing)
    resource = offer_source(preparing, [(path.rsplit("/", 1)[-1], path, size) for _i, path, size in FILES_592],
                            HASH_592)
    observed = preparing.resources[resource.id]
    preparing.resources[resource.id] = ProviderObservation(
        observed.resource, ResourceState.PREPARING, observed.name, observed.fingerprint, observed.progress,
        None, observed.request, file_manifest=observed.file_manifest)
    preparing.responses[-1] = ResolutionResult(ResourceState.PREPARING, observation=preparing.resources[resource.id])
    transfer = await submit(engine)
    await ticks(engine, 8, step=30.0)
    (generation,) = await rows("SELECT decision FROM transfer_file_selections WHERE transfer_id=?", (transfer.id,))
    assert generation["decision"] == "pending"
    assert not [item for item in await repository.requests(transfer.id) if item.parent_id]


# -- E: the selection inside one resolution cycle (#594 switch latency) ----------------------------------------------
#
# ``engine.resolve_pending()`` is one production resolution cycle: the scheduler
# (``core.scheduler.sync_status_loop``) runs exactly one per wake and then sleeps
# until the next persisted deadline, at most the 30 s resource poll interval.
# ``ticks`` starts a new cycle every time, so it cannot see a wait the cycle
# itself imposes; these tests hold the virtual clock still instead.

CREATED = ["add_magnet", "torrent_info"]                                  # the torrent created, then read
SELECTED = ["torrent_info", "torrent_info", "select_files"]               # observed; read again, then selected
VERIFIED = ["torrent_info", "torrent_info"]                               # the one immediate re-read and its check
EXECUTABLE = ["torrent_info", "unrestrict_link"]                          # the selected file's own link


def phases(fake):
    return [call[0] for call in fake.calls]


async def switched_to_real_debrid(tmp_path, monkeypatch, fake, *, adopted=False):
    """A movie-only selection confirmed on another provider, then the operator
    switches the root to Real-Debrid -- onto a fresh torrent, or onto the
    backup Prepare Backup Torrents already created (adopted)."""
    from transfers.manual_route_switch import switch_root_provider

    files = [(path.rsplit("/", 1)[-1], path, size) for _i, path, size in FILES_592]
    first = preparing_parcel("parcel-a", files) if adopted else parcel("parcel-a")
    if not adopted:
        offer_source(first, [(name, f"{WHALE}/{path}", size) for name, path, size in files], HASH_592)
    repository, engine = await lab(tmp_path, monkeypatch, first,
                                   RealDebridProvider(fake, prepare_backup_torrents=adopted))
    transfer = await submit(engine, preferred="parcel-a")
    await ticks(engine, 6, step=30.0 if adopted else 1.0)
    view = await repository.file_selection_presentation(transfer.id, now=engine.clock())
    await repository.confirm_file_selection(transfer.id, view["manifest_id"], [
        entry["entry_id"] for entry in view["entries"] if entry["relative_path"].endswith(MOVIE)], now=engine.clock())
    await ticks(engine, 2)
    assert fake.selects() == []
    await switch_root_provider(engine, transfer.id, "realdebrid", expected_provider_id="parcel-a")
    fake.calls.clear()
    return repository, engine, transfer


async def real_debrid_members(repository, transfer):
    return [(path, payload) for path, payload in await members(repository, transfer) if "real-debrid" in payload]


@pytest.mark.asyncio
@pytest.mark.parametrize("adopted", [False, True], ids=["fresh-torrent", "adopted-backup"])
async def test_a_switch_to_real_debrid_is_selected_verified_and_executable_in_its_first_cycle(
        tmp_path, monkeypatch, adopted):
    """W1 + W2: bound while it waits for its selection, the torrent is
    observed at once, selected, read back once and fanned out -- all in the
    switch's own resolution cycle, the clock never advancing a poll."""
    fake = FakeRealDebrid()
    repository, engine, transfer = await switched_to_real_debrid(tmp_path, monkeypatch, fake, adopted=adopted)
    now = engine.clock()
    await engine.resolve_pending()
    assert engine.clock() == now
    bound = ["torrent_info"] if adopted else CREATED                     # an adopted backup is read, not created
    assert phases(fake) == bound + SELECTED + VERIFIED + EXECUTABLE
    assert fake.selects() == ["3"] and list(fake.torrents) == ["T1"]       # one torrent, selected once
    logical = MOVIE if adopted else f"{WHALE}/{MOVIE}"                    # the established path, unchanged
    assert await real_debrid_members(repository, transfer) == [(logical, "https://real-debrid.com/d/T1-3")]
    (generation,) = await rows("SELECT manifest_committed_at FROM transfer_file_selections WHERE provider_id=?",
                               ("realdebrid",))
    assert generation["manifest_committed_at"] == now                     # committed in this very cycle


@pytest.mark.asyncio
async def test_a_selection_still_preparing_is_read_back_once_then_waits_for_the_ordinary_poll(tmp_path, monkeypatch):
    """The one immediate re-read finds the torrent still downloading: the
    unit ends on the ordinary poll deadline, nothing is sent again, and the
    cycle does not touch Real-Debrid until that deadline; later polls only
    read, and the selection executes once Real-Debrid is ready."""
    fake = FakeRealDebrid(ready=False)
    repository, engine, transfer = await switched_to_real_debrid(tmp_path, monkeypatch, fake)
    now = engine.clock()
    await engine.resolve_pending()
    assert phases(fake) == CREATED + SELECTED + VERIFIED
    record = await root(repository, transfer)
    assert record.state == "waiting" and record.error is None
    assert record.retry_at == now + engine.policy.resource_poll_interval
    assert engine.resolution_deadline == record.retry_at                  # the scheduler's own next wake
    fake.calls.clear()
    await engine.resolve_pending()                                        # an unrelated wake: nothing is due
    assert phases(fake) == []
    for _ in range(2):
        engine.clock.now += engine.policy.resource_poll_interval
        await engine.resolve_pending()
        assert phases(fake) == VERIFIED                                    # read and checked, never re-sent
        fake.calls.clear()
    fake.torrents["T1"].ready = True
    engine.clock.now += engine.policy.resource_poll_interval
    await engine.resolve_pending()
    assert phases(fake) == VERIFIED + EXECUTABLE
    assert fake.selects() == []                                           # the whole time: the one selection
    assert await real_debrid_members(repository, transfer) == [(f"{WHALE}/{MOVIE}", "https://real-debrid.com/d/T1-3")]


@pytest.mark.asyncio
async def test_an_open_decision_is_the_operator_s_wait_and_its_confirmation_executes_in_one_cycle(
        tmp_path, monkeypatch):
    """A first interactive torrent: offered, never selected while the
    operator decides, no cycle spinning on it, no early timeout; Confirm
    selects, verifies and fans out within the next cycle."""
    fake = FakeRealDebrid()
    repository, engine = await lab(tmp_path, monkeypatch, RealDebridProvider(fake))
    transfer = await submit(engine)
    await engine.resolve_pending()
    assert fake.selects() == [] and fake.count("add_magnet") == 1
    record = await root(repository, transfer)
    assert record.state == "waiting" and record.error is None and record.retry_at > engine.clock()
    fake.calls.clear()
    await engine.resolve_pending()                                        # woken early: the decision is not due
    assert phases(fake) == []
    engine.clock.now += 60.0                                              # inside the 120 s decision hold
    await engine.resolve_pending()
    assert fake.selects() == [] and await members(repository, transfer) == []
    view = await repository.file_selection_presentation(transfer.id, now=engine.clock())
    assert view["entries"]                                                # still offered
    await choose(repository, engine, transfer, {MOVIE})
    fake.calls.clear()
    await engine.resolve_pending()
    assert phases(fake) == SELECTED + VERIFIED + EXECUTABLE
    assert await members(repository, transfer) == [(MOVIE, "https://real-debrid.com/d/T1-3")]


@pytest.mark.asyncio
async def test_a_selection_sent_is_read_back_and_executable_in_the_cycle_that_sent_it(tmp_path, monkeypatch):
    """W2 alone: the offer reached and confirmed without any binding-cycle
    wait, the cycle that sends the selection also reads it back, verifies
    it and fans it out -- not one poll later."""
    fake = FakeRealDebrid()
    repository, engine = await lab(tmp_path, monkeypatch, RealDebridProvider(fake))
    transfer = await submit(engine)
    await ticks(engine)
    await choose(repository, engine, transfer, {MOVIE})
    fake.calls.clear()
    now = engine.clock()
    await engine.resolve_pending()
    assert engine.clock() == now
    assert phases(fake) == SELECTED + VERIFIED + EXECUTABLE
    assert await members(repository, transfer) == [(MOVIE, "https://real-debrid.com/d/T1-3")]


@pytest.mark.asyncio
async def test_a_pause_landing_while_the_selection_is_sent_executes_nothing(tmp_path, monkeypatch):
    """The operator pauses while Real-Debrid answers the selection: the
    immediate re-read is not taken past the pause, nothing fans out or
    executes, and the paused root is not polled again until it is resumed."""
    fake = FakeRealDebrid()
    repository, engine, transfer = await switched_to_real_debrid(tmp_path, monkeypatch, fake)

    async def pause():
        await engine.pause(transfer.id)

    fake.during_select = pause
    await engine.resolve_pending()
    assert fake.selects() == ["3"]
    assert phases(fake) == CREATED + SELECTED
    assert await real_debrid_members(repository, transfer) == []
    fake.calls.clear()
    engine.clock.now += engine.policy.resource_poll_interval
    await engine.resolve_pending()
    assert phases(fake) == [] and await real_debrid_members(repository, transfer) == []


@pytest.mark.asyncio
async def test_a_provider_without_the_capability_is_not_observed_again_inside_its_binding_cycle(
        tmp_path, monkeypatch):
    """A preparing manifest provider that does not declare upstream
    selection keeps its ordinary cadence: bound PREPARING, it is not read
    again until its next poll."""
    from transfers.models import ProviderObservation, ResolutionResult

    preparing = parcel("parcel-a")
    repository, engine = await lab(tmp_path, monkeypatch, preparing)
    resource = offer_source(preparing, [(path.rsplit("/", 1)[-1], path, size) for _i, path, size in FILES_592],
                            HASH_592)
    observed = preparing.resources[resource.id]
    preparing.resources[resource.id] = ProviderObservation(
        observed.resource, ResourceState.PREPARING, observed.name, observed.fingerprint, observed.progress,
        None, observed.request, file_manifest=observed.file_manifest)
    preparing.responses[-1] = ResolutionResult(ResourceState.PREPARING, observation=preparing.resources[resource.id])
    await submit(engine)
    await engine.resolve_pending()
    assert [call[0] for call in preparing.calls] == ["resolve"]


@pytest.mark.asyncio
async def test_the_selection_diagnostics_name_phases_never_secrets_and_add_no_journal_events(
        tmp_path, monkeypatch, caplog):
    """Bounded diagnostics: the native status, how many files were sent, how
    long the call took and whether the immediate re-read verified it or the
    root went back to its poll -- never a link, magnet, token or path; and a
    torrent polled while it prepares adds no journal event per poll."""
    import logging

    fake = FakeRealDebrid(ready=False)
    repository, engine, transfer = await switched_to_real_debrid(tmp_path, monkeypatch, fake)
    caplog.set_level(logging.INFO)
    await engine.resolve_pending()
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "Real-Debrid selection sent: status=waiting_files_selection files=1" in text
    assert "upstream selection read back on realdebrid: preparing; next poll in 30 s" in text
    journal = len(await rows("SELECT id FROM event_journal WHERE transfer_id=?", (transfer.id,)))
    for _ in range(3):
        engine.clock.now += engine.policy.resource_poll_interval
        await engine.resolve_pending()
    assert len(await rows("SELECT id FROM event_journal WHERE transfer_id=?", (transfer.id,))) == journal
    fake.torrents["T1"].ready = True
    engine.clock.now += engine.policy.resource_poll_interval
    await engine.resolve_pending()
    text = "\n".join(record.getMessage() for record in caplog.records)
    for secret in ("magnet:", "http", "real-debrid.com", "refresh-token-value", HASH_592, MOVIE, WHALE, "T1"):
        assert secret not in text
