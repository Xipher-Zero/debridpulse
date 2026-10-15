"""DP 1.0.13: an uploaded NZB names its transfer by the submitter's filename.

Live: Radarr uploaded ``Batman_6_The_Dark_Knight_2008_English_XviD.nzb``; the
transfer, its one Files row and the native SABnzbd job were all called
``BANTAG.nfo`` -- the quoted filename in the posting's FIRST ``<file>``
subject, which ``providers.usenet.provider`` preferred over the request's
own name (``safe_name(manifest.name) or safe_name(request.name)``). That was
the first faulty write: the candidate name becomes the collection artifact's
``download_files.filename``, its on-disk root, the native job's name, and
(single artifact) the transfer's display name.

Now a request whose ``name`` the submitter supplied (an upload's filename,
recorded at admission as ``name_supplied``) names the candidate -- as
SABnzbd names a job after its NZB file. The posting's identity
(``source_identity``) is unchanged, and an NZB link, whose name DebridPulse
derives from its path, is named by its posting exactly as before.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from application.service import ApplicationService
from providers.usenet.provider import UsenetProvider
from sab_fakes import staged_store
from test_v113_runtime_telemetry_matrix import converge, mixed  # noqa: F401 -- the real SAB + Usenet fixture
from transfers import codec
from transfers.models import TransferRequest

pytestmark = pytest.mark.asyncio

RELEASE = "Batman_6_The_Dark_Knight_2008_English_XviD"


def posting(names=("BANTAG.nfo", "a8f3c1d09e.mkv", "a8f3c1d09e.par2")) -> bytes:
    """A release posting whose first file is its .nfo and whose payload is
    obfuscated: the shape observed live."""
    files = b"".join(
        b'<file poster="p@e.net" date="1700000000" subject="rel [%d/%d] - &quot;%s&quot; yEnc (1/1)">'
        % (index, len(names), name.encode())
        + b"<groups><group>alt.binaries.test</group></groups>"
        b'<segments><segment bytes="%d" number="1">s%d@e.net</segment></segments></file>'
        % (4096 if name.endswith(".mkv") else 512, index)
        for index, name in enumerate(names, start=1))
    return b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">' + files + b"</nzb>"


async def candidate(data, name, supplied):
    provider = UsenetProvider(staged_input=staged_store())
    result = await provider.resolve(TransferRequest("nzb", data, name=name, name_supplied=supplied))
    [found] = result.candidates
    return found


async def test_an_uploaded_nzb_names_the_candidate_and_the_posting_keeps_its_identity():
    upload = await candidate(posting(), f"{RELEASE}.nzb", True)
    assert upload.name == RELEASE
    # The posting itself is still identified by its first posted file.
    assert (upload.source_identity.scope, upload.source_identity.key) == ("usenet", "bantag.nfo")
    # The declared size is the whole posting's, unchanged.
    assert upload.expected_bytes == 512 + 4096 + 512


@pytest.mark.parametrize("names", [
    ("BANTAG.nfo", "a8f3c1d09e.mkv", "a8f3c1d09e.par2"),
    ("a8f3c1d09e.par2", "a8f3c1d09e.mkv", "BANTAG.nfo"),
    ("a8f3c1d09e.mkv",)])
async def test_the_name_never_depends_on_which_member_is_listed_first(names):
    assert (await candidate(posting(names), f"{RELEASE}.nzb", True)).name == RELEASE


async def test_a_derived_or_missing_name_falls_back_to_the_existing_posting_name():
    # An NZB link's name is derived from its path: the posting names it.
    assert (await candidate(posting(), "api", False)).name == "BANTAG.nfo"
    # A supplied name with nothing useful in it is no name.
    assert (await candidate(posting(), "   ", True)).name == "BANTAG.nfo"
    # Only terminal .nzb is removed from a supplied name, never a release's own dots.
    assert (await candidate(posting(), "The.Dark.Knight.Prologue.2008.1080p.BluRay.x264-SuBHD.nzb", True)).name \
        == "The.Dark.Knight.Prologue.2008.1080p.BluRay.x264-SuBHD"


async def test_the_upload_path_records_its_filename_as_supplied_and_a_stored_request_decodes():
    submitted = []

    class Recorder(ApplicationService):
        def __init__(self):
            pass

        async def submit(self, requests, **kwargs):
            submitted.append((requests, kwargs))
            return {"ok": True}

    await Recorder().submit_nzb(posting(), f"{RELEASE}.nzb", source="external")
    [(requests, kwargs)] = submitted
    [request] = requests
    assert (request.name, request.name_supplied, kwargs["name"]) == (f"{RELEASE}.nzb", True, RELEASE)
    # A request stored before this change carries no such fact: not supplied.
    assert codec.request({"kind": "nzb", "payload": "x", "name": "posting.nzb"}).name_supplied is False


async def test_every_title_surface_shows_the_submitted_release_and_files_stay_truthful(mixed):  # noqa: F811
    from api import operational_downloads as downloads
    transfer = await mixed.engine.submit(
        (TransferRequest("nzb", posting(), name=f"{RELEASE}.nzb", name_supplied=True),),
        name=RELEASE, deduplicate=False)
    await converge(mixed)
    transfer_id = transfer.id if hasattr(transfer, "id") else transfer[0].id
    # The first write: the collection artifact (its Files row) and root.
    [artifact] = await mixed.repository.artifacts(transfer_id)
    assert artifact.name == RELEASE and artifact.target.endswith(RELEASE)
    # Its candidate still carries the posting's own identity.
    assert artifact.candidates[0].source_identity.key == "bantag.nfo"
    # The native job is named after the release, not its .nfo.
    [job] = mixed.sab.queue.values()
    assert job.name == RELEASE
    # Downloads list, Dashboard (same rows) and Details agree.
    listed = await downloads.list_operational_torrents(
        status=None, search=None, limit=0, offset=0,
        application=SimpleNamespace(repository=mixed.repository, definitions=[]))
    [row] = listed["items"]
    assert row["display_name"] == RELEASE
    assert (await mixed.repository.display_names([transfer_id]))[transfer_id] == RELEASE
    # The original submission evidence is untouched.
    [record] = await mixed.repository.requests(transfer_id)
    assert record.request.name == f"{RELEASE}.nzb"
