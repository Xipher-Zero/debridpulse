"""Interactive intent meets remote collections through the neutral selection owner.

Real engine, real ``general_http``/``general_ftp``/``general_scp`` providers
and the one core-run discovery; only the transport is an in-memory fake that
claims HTTPS, FTP and SFTP endpoints. Interactive intent never forces a
picker: a single file proceeds with no selection generation, while a proven
multi-file directory is held before ANY child request or writer exists until
the neutral selection owner's decision settles (confirm -> only the subset;
close or timeout -> ALL).
"""
from __future__ import annotations

from urllib.parse import unquote, urlsplit

import pytest

from db.database import get_db
from test_v113_transfer_auth_context import CountingVault, lab  # noqa: F401
from transfers.models import (
    DiscoveredEntry, DiscoveryResult, ExecutorCapabilities, IntegrationDescriptor, RemoteObjectKind, TransferRequest,
    TransferState,
)

pytestmark = pytest.mark.asyncio

DIRECTORY = ["one.iso", "two.iso", "three.iso"]


class RemoteMemory(CountingVault):
    """In-memory HTTPS/FTP/SFTP transport with read-only remote classification."""

    descriptor = IntegrationDescriptor("remote-memory", "Remote memory", frozenset())
    capabilities = ExecutorCapabilities(candidate_sampling=True, per_execution_pause=True, transient_input=True,
                                        remote_discovery=True)
    claim_schemes = frozenset({"https", "ftp", "sftp"})

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.discovered = []

    @staticmethod
    def _object(candidate):
        parts = urlsplit(candidate.endpoints[0].address)
        return f"{parts.hostname}{unquote(parts.path)}"

    async def discover(self, subject, submitted=None):
        parts = urlsplit(subject.candidate.endpoints[0].address)
        path = unquote(parts.path)
        self.discovered.append(path)
        if path.rstrip("/").endswith("/dir"):
            return DiscoveryResult(tuple(DiscoveredEntry(name, 4) for name in DIRECTORY), path)
        return DiscoveryResult(kind=RemoteObjectKind.FILE, expected_bytes=4)


def _setup(lab):
    from providers.general_ftp.provider import GeneralFtpProvider
    from providers.general_http.provider import GeneralHttpProvider
    from providers.general_scp.provider import ScpProvider
    repository, registry, engine, *_rest, now = lab
    for provider in (GeneralHttpProvider(), GeneralFtpProvider(), ScpProvider()):
        registry.register_provider(provider)
    objects = {f"remote.example/{name}": b"four" for name in ("file.bin",)}
    objects |= {f"remote.example/dir/{name}": b"four" for name in DIRECTORY}
    executor = RemoteMemory(repository.authorize_execution, objects=objects)
    registry.register_executor(executor)
    return repository, engine, executor, now


async def _ticks(engine, now, count, step=1.0):
    for _ in range(count):
        now[0] += step
        await engine.tick()


async def _selection_rows(transfer_id):
    async with get_db() as db:
        return await db.fetchall("SELECT decision FROM transfer_file_selections WHERE transfer_id=?", (transfer_id,))


async def _children(repository, transfer_id):
    return sorted(record.request.payload for record in await repository.requests(transfer_id) if record.parent_id)


def _submit(engine, url):
    return engine.submit((TransferRequest(url.split(":", 1)[0], url, name="", selection_mode="interactive"),),
                         deduplicate=False)


@pytest.mark.parametrize("url", ["https://remote.example/file.bin", "ftp://remote.example/file.bin",
                                 "sftp://remote.example/file.bin", "scp://remote.example/file.bin"])
async def test_a_single_file_with_interactive_intent_never_offers_a_picker(lab, url):
    repository, engine, executor, now = _setup(lab)
    transfer = await _submit(engine, url)
    await _ticks(engine, now, 12)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert await _selection_rows(transfer.id) == []


@pytest.mark.parametrize("url", ["ftp://remote.example/dir", "sftp://remote.example/dir/",
                                 "scp://remote.example/dir/"])
async def test_a_multi_file_directory_is_held_for_selection_before_any_child_exists(lab, url):
    repository, engine, executor, now = _setup(lab)
    transfer = await _submit(engine, url)
    await _ticks(engine, now, 8)
    assert [row["decision"] for row in await _selection_rows(transfer.id)] == ["pending"]
    assert await _children(repository, transfer.id) == []
    assert await repository.artifacts(transfer.id) == ()
    assert executor.calls == []  # no writer of any kind

    view = await repository.file_selection_presentation(transfer.id, now=now[0])
    chosen = [entry["entry_id"] for entry in view["entries"] if entry["name"] in {"one.iso", "three.iso"}]
    result = await repository.confirm_file_selection(transfer.id, view["manifest_id"], chosen, now=now[0])
    assert result.decision == "explicit"
    await _ticks(engine, now, 12)
    scheme = url.split(":", 1)[0]
    assert await _children(repository, transfer.id) == [
        f"{scheme}://remote.example/dir/one.iso", f"{scheme}://remote.example/dir/three.iso"]
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert executor.discovered.count("/dir") + executor.discovered.count("/dir/") == 1  # frozen, never re-listed


async def test_closing_the_selector_keeps_every_member(lab):
    repository, engine, executor, now = _setup(lab)
    transfer = await _submit(engine, "sftp://remote.example/dir")
    await _ticks(engine, now, 8)
    view = await repository.file_selection_presentation(transfer.id, now=now[0])
    await repository.dismiss_file_selection(transfer.id, view["manifest_id"], now=now[0])
    await _ticks(engine, now, 12)
    assert len(await _children(repository, transfer.id)) == len(DIRECTORY)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED


async def test_an_unanswered_selector_times_out_to_every_member(lab):
    repository, engine, executor, now = _setup(lab)
    transfer = await _submit(engine, "ftp://remote.example/dir/")
    await _ticks(engine, now, 8)
    assert await _children(repository, transfer.id) == []
    await _ticks(engine, now, 16, step=15.0)  # well past the decision hold
    assert len(await _children(repository, transfer.id)) == len(DIRECTORY)
