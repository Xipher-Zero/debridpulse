"""DP 1.0.13: an uploaded NZB's collection folder carries the submitted name.

Transfer 639 (an earlier build) materialized into
``<release>.vol-01.par2/``: the folder of a root NZB collection is its
candidate's name (``_engine_base._materialization_relative``), and the Usenet
provider named the candidate after the posting's first member. Since
``4d7c8e45`` an uploaded NZB -- every external SABnzbd ``addfile`` included --
names its candidate by the submitted filename (``name_supplied``), so the
folder follows with no further code. These are its regression proofs; the
posting identity, member names and the existing collision rule are unchanged,
and no historical folder is renamed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from test_v113_runtime_telemetry_matrix import converge, mixed  # noqa: F401 -- real SAB + Usenet
from transfers.models import TransferRequest, TransferState
from transfers.recovery_repository import TransferRepository

pytestmark = pytest.mark.asyncio

RELEASE = "Return.to.Silent.Hill.2026.1080p.iT.WEB-DL.DUAL.DD5.1.H.264-TURG"


def posting(first=f"{RELEASE}.vol-01.par2") -> bytes:
    """The observed shape: an incidental member is posted first."""
    names = (first, f"{RELEASE}.mkv", f"{RELEASE}.par2")
    files = b"".join(
        b'<file poster="p@e.net" date="1700000000" subject="rel [%d/3] - &quot;%s&quot; yEnc (1/1)">'
        % (index, name.encode()) + b"<groups><group>alt.binaries.test</group></groups>"
        b'<segments><segment bytes="%d" number="1">s%d@e.net</segment></segments></file>'
        % (4096 if name.endswith(".mkv") else 512, index)
        for index, name in enumerate(names, start=1))
    return b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">' + files + b"</nzb>"


async def acquire(mixed, filename=f"{RELEASE}.nzb"):  # noqa: F811
    transfer = await mixed.engine.submit((TransferRequest("nzb", posting(), name=filename, name_supplied=True),),
                                         name=filename[:-4], deduplicate=False)
    await converge(mixed)
    return transfer.id


async def test_a1_a4_the_folder_is_the_submitted_release_and_members_keep_their_names(mixed):  # noqa: F811
    transfer_id = await acquire(mixed)
    [artifact] = await mixed.repository.artifacts(transfer_id)
    root = Path(mixed.engine.root)
    assert Path(artifact.target) == root / RELEASE                 # not ...vol-01.par2
    # The posting's identity is still its first member (dedup unchanged).
    assert artifact.candidates[0].source_identity.key == f"{RELEASE}.vol-01.par2".casefold()
    [job] = mixed.sab.queue.values()
    mixed.sab.finish(job.nzo_id, files=((f"{RELEASE}.mkv", 4096), (f"{RELEASE}.nfo", 64)))
    await mixed.engine.reconcile_executions()
    await converge(mixed)
    assert (await mixed.repository.get(transfer_id)).state == TransferState.COMPLETED
    # Materialized members live under that folder with their own names (an
    # .nfo stays an .nfo); delivery paths are those same paths.
    delivered = await mixed.engine.delivered_material((await mixed.repository.artifacts(transfer_id))[0])
    assert sorted(Path(path).relative_to(root).as_posix() for path in delivered) == [
        f"{RELEASE}/{RELEASE}.mkv", f"{RELEASE}/{RELEASE}.nfo"]


async def test_a2_identical_submissions_never_share_a_folder(mixed):  # noqa: F811
    first, second = await acquire(mixed), await acquire(mixed)
    targets = [Path((await mixed.repository.artifacts(item))[0].target) for item in (first, second)]
    root = Path(mixed.engine.root)
    assert targets[0] != targets[1] and {target.parent for target in targets} == {root}
    # The existing collision rule (``_unique_target``), unchanged: a counter
    # before the last dot segment (deferred finding: it treats a folder's
    # final dot segment as an extension).
    assert targets[0].name == RELEASE
    assert targets[1].name == f"{Path(RELEASE).stem} (2){Path(RELEASE).suffix}"


async def test_a3_a_recorded_folder_is_stable_and_a_historical_one_untouched(mixed):  # noqa: F811
    root = Path(mixed.engine.root)
    historical = root / f"{RELEASE}.vol-01.par2"
    historical.mkdir()
    (historical / f"{RELEASE}.mkv").write_bytes(b"imported long ago")
    transfer_id = await acquire(mixed)
    [artifact] = await mixed.repository.artifacts(transfer_id)
    for _ in range(3):                                # reconciliation and retries
        await mixed.engine.reconcile_executions()
        await converge(mixed)
    restarted = TransferRepository()                  # a restarted process reads the same record
    assert [item.target for item in await restarted.artifacts(transfer_id)] == [artifact.target]
    assert (historical / f"{RELEASE}.mkv").read_bytes() == b"imported long ago"   # never renamed or moved


# ── through the real entrances, to the directory on disk ───────────────────

def application_for(mixed):  # noqa: F811
    from application.service import ApplicationService
    from integrations.usenet.nzb import read as read_nzb
    from sab_fakes import staged_store
    return ApplicationService(mixed.engine, staged_input=staged_store(), nzb_reader=read_nzb)


async def materialized(mixed, transfer_id):  # noqa: F811
    await converge(mixed)
    [job] = mixed.sab.queue.values()
    # The service completes the job and SAB writes the members under its own
    # name; the executor hands them to the allocated collection folder.
    mixed.sab.finish(job.nzo_id, files=((f"{RELEASE}.mkv", 4096), ("info.txt", 32)))
    await mixed.engine.reconcile_executions()
    await converge(mixed)
    assert (await mixed.repository.get(transfer_id)).state == TransferState.COMPLETED
    root = Path(mixed.engine.root)
    on_disk = sorted(path.relative_to(root).as_posix() for path in root.rglob("*")
                     if path.is_file() and ".dpwork" not in path.parts)
    return on_disk


@pytest.mark.parametrize("first", ["info.txt", "BANTAG.nfo", f"{RELEASE}.vol-01.par2", "sample.srr"])
async def test_an_uploaded_nzb_lands_in_a_folder_named_by_its_file_whatever_is_posted_first(mixed, first):  # noqa: F811
    application = application_for(mixed)
    # The SABnzbd addfile / native upload entrance (validated, staged).
    submitted = await application.submit_nzb(posting(first), f"{RELEASE}.nzb", source="external", validate=True)
    transfer_id = submitted["id"]
    await converge(mixed)
    [artifact] = await mixed.repository.artifacts(transfer_id)
    assert Path(artifact.target).name == RELEASE                  # the allocated folder
    on_disk = await materialized(mixed, transfer_id)
    # The members, under their own truthful names, in the submitted folder.
    assert on_disk == sorted([f"{RELEASE}/info.txt", f"{RELEASE}/{RELEASE}.mkv"])
    assert not (Path(mixed.engine.root) / first).exists()
    # The posting's identity (duplicate detection) is still its first member.
    assert artifact.candidates[0].source_identity.key == first.casefold()


async def test_an_nzb_link_lands_in_a_folder_named_by_the_file_it_was_served_as(mixed, monkeypatch):  # noqa: F811
    import application.service as service_module
    import services.artifact_sampling as sampling

    def served(address, **kwargs):
        if kwargs.get("facts") is not None:
            kwargs["facts"]["filename"] = f"{RELEASE}.nzb"

        async def body():
            yield posting("info.txt")
        return body()

    monkeypatch.setattr(sampling, "http_body", served)

    async def consent(self, addresses, allow_local_network=False):
        return set()

    monkeypatch.setattr(service_module.ApplicationService, "_local_network_consent", consent)
    application = application_for(mixed)
    submitted = await application.submit_nzb_link("https://indexer.example/api?t=get&id=abc")
    on_disk = await materialized(mixed, submitted["id"])
    assert on_disk == sorted([f"{RELEASE}/info.txt", f"{RELEASE}/{RELEASE}.mkv"])


async def test_a_retried_acquisition_keeps_its_allocated_folder(mixed):  # noqa: F811
    application = application_for(mixed)
    transfer_id = (await application.submit_nzb(posting("info.txt"), f"{RELEASE}.nzb", validate=True))["id"]
    await converge(mixed)
    [artifact] = await mixed.repository.artifacts(transfer_id)
    [job] = mixed.sab.queue.values()
    mixed.sab.fail(job.nzo_id)
    await mixed.engine.reconcile_executions()
    await converge(mixed)
    await mixed.engine.retry(transfer_id)
    mixed.now[0] += 60                               # past the retry delay
    on_disk = await materialized(mixed, transfer_id)
    assert [item.target for item in await mixed.repository.artifacts(transfer_id)] == [artifact.target]
    assert on_disk == sorted([f"{RELEASE}/info.txt", f"{RELEASE}/{RELEASE}.mkv"])
