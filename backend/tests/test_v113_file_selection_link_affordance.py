"""Transfer 1080 (live acceptance of 7f10b9d6): a download submitted as direct
links, one file offered by several mirrors, showed "Choose Files" -- whose
click said DebridPulse was still waiting for "the file list for this torrent".

The submission asked for interactive selection, so each provider resource a
link root bound to (here a provider still preparing a web download) opened an
interactive selection generation with no manifest. The one classifier
(``transfers.file_selection.file_selection_affordance``) called any such
generation ``pending_manifest``. A file list always comes only for a torrent or
magnet root; any other root offers a choice only once a manifest has actually
been reported. Mirror rows and alternate candidates are never files to choose.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from db.database import get_db
from file_selection_support import Clock, file_manifest, seed_window
from transfers import codec
from transfers import file_selection as fs
from transfers.models import TransferRequest
from transfers.repository import TransferRepository

pytestmark = pytest.mark.asyncio
ROOT = Path(__file__).resolve().parents[2]


@pytest_asyncio.fixture
async def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "link-affordance.db")
    await database.init_db()
    return TransferRepository()


async def interactive_generation(repo, clock, tag, *, kind, payload):
    """A root of ``kind`` bound to a provider resource that is still
    preparing, with the interactive generation the submission asked for and
    no manifest -- exactly transfer 1080's TorBox web-download roots."""
    seed = await seed_window(transfer_hash=f"{tag}" * 4, provider_id="prov-web", request_kind=kind,
                             request_payload=payload, transfer_status="downloading")
    await repo.begin_file_selection_window(seed.request_id, seed.transfer_id, seed.provider_resource_id,
                                           seed.provider_id, initially_available=False, now=clock(),
                                           interactive=True)
    return seed


async def mirror_rows(transfer_id):
    """One canonical file and two duplicate mirror rows of it, each from its
    own link root (1080's three download-file rows): candidates of one file,
    not three files."""
    async with get_db() as db:
        for index, (status, mirror) in enumerate((("downloading", "primary"), ("duplicate", "standby"),
                                                  ("duplicate", "standby"))):
            request_id = f"mirror-{transfer_id}-{index}"
            await db.execute(
                "INSERT INTO transfer_requests(id,transfer_id,ordinal,payload,state) VALUES(?,?,?,?,'resolved')",
                (request_id, transfer_id, 10 + index, codec.dump(TransferRequest("https", f"https://m{index}.example/x"))))
            await db.execute(
                "INSERT INTO download_files(torrent_id,filename,size_bytes,status,mirror_state,request_id) "
                "VALUES(?,?,?,?,?,?)", (transfer_id, "GF171026-CBC-RN.rar", 4, status, mirror, request_id))
        await db.commit()


async def listed(repo, transfer_id):
    from api.operational_downloads import list_operational_torrents
    application = SimpleNamespace(engine=None, repository=repo, definitions={})
    page = await list_operational_torrents(status=None, search=None, limit=0, offset=0, order=None,
                                           application=application)
    items = page["items"] if isinstance(page, dict) else page
    return next(item for item in items if item["id"] == transfer_id)


async def test_a_link_root_waiting_on_its_provider_offers_no_file_choice(repo):
    """1080's shape: an https root, an interactive generation with no
    manifest on a resource still preparing, and three mirror rows of one
    file. Neither the fresh read nor the list offers anything to choose."""
    clock = Clock()
    seed = await interactive_generation(repo, clock, "a", kind="https", payload="https://vikingfile.com/f/x")
    await mirror_rows(seed.transfer_id)

    view = await repo.file_selection_presentation(seed.transfer_id, now=clock())
    assert view["decision"] == "pending" and view["manifest_id"] is None    # the internal record exists...
    assert view["file_selection_affordance"] == "none"                      # ...but it is not a choice
    assert view["file_count"] == 0                                          # mirror rows are not files
    assert (await listed(repo, seed.transfer_id))["file_selection_affordance"] == "none"


async def test_a_torrent_root_still_waits_for_its_file_list(repo):
    """Control: a magnet root's pending generation still says its file list
    is coming, on the fresh read and the list alike."""
    clock = Clock()
    seed = await interactive_generation(repo, clock, "b", kind="magnet",
                                        payload="magnet:?xt=urn:btih:" + "b" * 40)
    view = await repo.file_selection_presentation(seed.transfer_id, now=clock())
    assert view["file_selection_affordance"] == "pending_manifest"
    assert (await listed(repo, seed.transfer_id))["file_selection_affordance"] == "pending_manifest"


@pytest.mark.parametrize("kind,payload", [("https", "https://example.net/folder/"),
                                          ("magnet", "magnet:?xt=urn:btih:" + "c" * 40)])
async def test_a_reported_multi_file_manifest_is_offered_whatever_the_root(repo, kind, payload):
    """A resource that actually reports several files keeps its selection,
    torrent or not: the choice comes from the manifest, never from the kind."""
    clock = Clock()
    seed = await interactive_generation(repo, clock, kind[0] + "d", kind=kind, payload=payload)
    manifest = file_manifest(("a.bin", "Folder/a.bin", 4), ("b.bin", "Folder/b.bin", 4),
                             ("c.bin", "Folder/c.bin", 4))
    assert await repo.record_file_manifest(seed.request_id, seed.provider_resource_id, manifest,
                                           now=clock()) is not None
    view = await repo.file_selection_presentation(seed.transfer_id, now=clock())
    assert (view["file_selection_affordance"], view["file_count"]) == ("choose", 3)
    assert (await listed(repo, seed.transfer_id))["file_selection_affordance"] == "choose"


def test_the_classifier_waits_only_where_a_file_list_always_comes():
    assert fs.awaits_manifest("magnet") and fs.awaits_manifest("torrent")
    for kind in ("https", "http", "ftp", "sftp", "webdav", "rsync", "meta4", "nzb", "", None):
        assert not fs.awaits_manifest(kind), kind
    assert fs.file_selection_affordance(None, "pending", None, 0, awaiting_manifest=True) == "pending_manifest"
    assert fs.file_selection_affordance(None, "pending", None, 0, awaiting_manifest=False) == "none"
    # A bound manifest decides by itself, whatever the root.
    assert fs.file_selection_affordance("m", "pending", None, 3, awaiting_manifest=False) == "choose"


def test_the_waiting_message_names_no_resource_type():
    source = (ROOT / "frontend" / "static" / "ui-file-selection.js").read_text()
    message = source.split("function openPendingManifestModal()", 1)[1].split("\n  }\n", 1)[0]
    assert "torrent" not in message.lower()
    assert "DebridPulse is still waiting for the file list for this download." in message
