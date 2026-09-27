"""DP 1.0.13 canonical/equivalence correction: same-source aliases and
late-completed same-transfer equivalents.

Real engine, real repository/canonical owners, real ``general_http``,
``general_ftp`` and ``general_scp`` providers and the one core-run discovery;
only the transport is an in-memory fake (HTTPS/FTP/SFTP, discovery, neutral
content sampling). Its writers stay live until the test finishes them, so
each scenario states exactly which writer exists when an equivalent source
proves itself -- no timing, no download speed.

A. Source independence is not object identity, and a path is not identity
   either. Two routes to one server (SCP/SSH/SFTP aliases; an FTP directory
   member and the same file submitted exactly) are not independent
   corroboration; the provider-asserted canonical remote coordinate only makes
   them PAIRABLE, and actual material evidence decides. Content replaced at the
   same path stays distinct; a different coordinate is never paired.
B. Completion freezes material ownership, it does not hide it. A later
   equivalent sibling of the same transfer is satisfied by the completed
   canonical artifact instead of becoming a second writer.
"""
from __future__ import annotations

from urllib.parse import unquote, urlsplit

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import VaultExecutor
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.models import (
    ArtifactFingerprint, DiscoveredEntry, DiscoveryResult, ExecutionState, ExecutorCapabilities, FingerprintKind,
    IntegrationDescriptor, RemoteObjectKind, TransferRequest, TransferState,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

BYTES = b"four"


class RemoteServers(VaultExecutor):
    """In-memory HTTPS/FTP/SFTP servers: read-only classification, neutral
    full-content sampling, and writers that run until ``finish_all``."""

    descriptor = IntegrationDescriptor("remote-servers", "Remote servers", frozenset())
    capabilities = ExecutorCapabilities(candidate_sampling=True, per_execution_pause=True, transient_input=True,
                                        remote_discovery=True)
    claim_schemes = frozenset({"https", "ftp", "sftp"})

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.unreachable = set()    # hosts whose classification fails transiently
        self.unsampleable = set()   # hosts whose content sampling times out
        self.replaced = {}          # object -> the bytes the server holds after it was first observed
        self._observed = set()

    @staticmethod
    def _object(candidate):
        parts = urlsplit(candidate.endpoints[0].address)
        return f"{parts.hostname}{unquote(parts.path)}"

    async def discover(self, subject, submitted=None):
        parts = urlsplit(subject.candidate.endpoints[0].address)
        path = unquote(parts.path)
        if parts.hostname in self.unreachable:
            raise TransferError(NormalizedError(Domain.NETWORK, Category.CONNECTION_FAILED, Stage.RESOLUTION,
                                                retryability=Retryability.BACKOFF))
        prefix = f"{parts.hostname}{path.rstrip('/')}/"
        members = sorted((key[len(prefix):], len(body)) for key, body in self.objects.items()
                         if key.startswith(prefix) and "/" not in key[len(prefix):])
        if members:
            return DiscoveryResult(tuple(DiscoveredEntry(name, size) for name, size in members), path)
        key = f"{parts.hostname}{path}"
        if key not in self.objects:
            raise TransferError(NormalizedError(Domain.RESOLUTION, Category.SOURCE_NOT_FOUND, Stage.RESOLUTION,
                                                retryability=Retryability.NEVER))
        return DiscoveryResult(kind=RemoteObjectKind.FILE, expected_bytes=len(self.objects[key]))

    async def fingerprint(self, subject):
        key = self._object(subject.candidate)
        self.samples.append((str(subject.candidate.id), None))
        if key.partition("/")[0] in self.unsampleable:
            return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "timeout")
        if key in self.replaced and key in self._observed:
            import hashlib
            body = self.replaced[key]
            return ArtifactFingerprint(len(body), hashlib.sha256(body).hexdigest())
        self._observed.add(key)
        return self._evidence(key)

    def writers(self):
        return [call for call in self.calls if call[0] == "start"]

    def finish_all(self):
        for observed in list(self.jobs.values()):
            if observed.state == ExecutionState.RUNNING:
                self.finish(observed.handle)


@pytest_asyncio.fixture
async def world(tmp_path, monkeypatch):
    from providers.general_ftp.provider import GeneralFtpProvider
    from providers.general_http.provider import GeneralHttpProvider
    from providers.general_scp.provider import ScpProvider
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "canonical-equivalence.sqlite3")
    await database.init_db()
    now = [1000.0]
    repository = TransferRepository()
    registry = IntegrationRegistry()
    policy = TransferPolicy(retry_delay=1, adoption_stability_seconds=0, max_active_executions=5,
                            resolution_max_attempts=100)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"), policy=policy,
                            clock=lambda: now[0])
    await engine.initialize()
    for provider in (GeneralHttpProvider(), GeneralFtpProvider(), ScpProvider()):
        registry.register_provider(provider)
    executor = RemoteServers(repository.authorize_execution, objects={})
    registry.register_executor(executor)
    return repository, engine, executor, now


async def _ticks(engine, now, count=6):
    for _ in range(count):
        now[0] += 5
        await engine.tick()


async def _until(engine, now, predicate, count=40, *, finishing=None):
    for _ in range(count):
        now[0] += 5
        await engine.tick()
        if finishing is not None:
            finishing.finish_all()  # whatever writer exists is allowed to finish
        if await predicate():
            return True
    return False


async def _material(repository, transfer_ids):
    """Every material (non-standby) artifact of the given transfers."""
    async with database.get_db() as db:
        marks = ",".join("?" for _ in transfer_ids)
        return await db.fetchall(
            f"SELECT * FROM download_files WHERE torrent_id IN ({marks}) "  # nosec B608 - placeholders only
            "AND COALESCE(mirror_state,'')!='standby' ORDER BY id", tuple(transfer_ids))


async def _origin_requests(engine, artifact_id):
    return {str(origin["request_id"]) for binding in await engine.canonical.bindings(artifact_id)
            for origin in binding["origins"]}


async def _proof_reasons(transfer_ids):
    """The evidence kind each attached request was durably proven by."""
    async with database.get_db() as db:
        marks = ",".join("?" for _ in transfer_ids)
        return {row["equivalence_reason"] for row in await db.fetchall(
            f"SELECT equivalence_reason FROM transfer_requests WHERE transfer_id IN ({marks}) "  # nosec B608
            "AND equivalence_disposition='recovered'", tuple(transfer_ids))}


async def _states(repository, transfer_ids):
    return [(await repository.get(transfer_id)).state for transfer_id in transfer_ids]


def _submit(engine, *urls):
    return engine.submit(tuple(TransferRequest(url.split(":", 1)[0], url) for url in urls), deduplicate=False)


# ── A: same-source aliases of one remote object ───────────────────────────────

async def test_scp_ssh_and_sftp_aliases_of_one_remote_object_share_one_writer(world):
    repository, engine, executor, now = world
    executor.objects["files.example/pub/file.bin"] = BYTES
    transfers = [await _submit(engine, url) for url in (
        "scp://files.example/pub/file.bin", "ssh://files.example/pub/file.bin", "sftp://files.example/pub/file.bin")]
    ids = [transfer.id for transfer in transfers]
    await _ticks(engine, now)
    [canonical] = await _material(repository, ids)
    assert len(executor.writers()) == 1
    # Every alias is retained as a proven route of the one canonical artifact.
    assert await _origin_requests(engine, canonical["id"]) == {
        record.id for transfer_id in ids for record in await repository.requests(transfer_id)}
    # Proven from actual material, never the address: every alias's candidate was sampled.
    sampled = {candidate_id for candidate_id, _input in executor.samples}
    assert len(sampled) == 3
    executor.finish_all()
    await _ticks(engine, now)
    # The owning transfer completes its material; each alias's transfer is
    # consolidated into it (cross-transfer provenance), never re-downloaded.
    assert await _states(repository, ids) == [
        TransferState.COMPLETED, TransferState.CONSOLIDATED, TransferState.CONSOLIDATED]
    assert len(executor.writers()) == 1


async def test_an_ftp_directory_member_and_the_same_file_submitted_exactly_share_one_writer(world):
    repository, engine, executor, now = world
    executor.objects |= {"files.example/pub/one.iso": BYTES, "files.example/pub/two.iso": b"two!"}
    directory = await _submit(engine, "ftp://files.example/pub/")
    exact = await _submit(engine, "ftp://files.example/pub/one.iso")
    await _ticks(engine, now)
    material = await _material(repository, [directory.id, exact.id])
    assert sorted(row["filename"] for row in material) == ["one.iso", "two.iso"]
    assert len(executor.writers()) == 2  # one per distinct remote object, never one per route
    one = next(row for row in material if row["filename"] == "one.iso")
    exact_request = (await repository.requests(exact.id))[0].id
    member_request = next(record.id for record in await repository.requests(directory.id)
                          if record.request.payload.endswith("/one.iso"))
    assert {exact_request, member_request} <= await _origin_requests(engine, one["id"])
    assert await _proof_reasons([directory.id, exact.id]) == {"full_content_sample"}


async def test_content_replaced_at_the_same_coordinate_and_size_never_consolidates(world):
    repository, engine, executor, now = world
    executor.objects["files.example/pub/file.bin"] = BYTES
    executor.replaced["files.example/pub/file.bin"] = b"FOUR"  # same size, other bytes
    first = await _submit(engine, "scp://files.example/pub/file.bin")
    second = await _submit(engine, "sftp://files.example/pub/file.bin")
    await _ticks(engine, now)
    assert len(await _material(repository, [first.id, second.id])) == 2
    assert len(executor.writers()) == 2
    assert executor.samples  # the decision came from material evidence, not the address


async def test_the_same_server_with_a_different_object_stays_distinct(world):
    repository, engine, executor, now = world
    executor.objects |= {"files.example/a/file.iso": BYTES, "files.example/b/file.iso": BYTES}
    first = await _submit(engine, "sftp://files.example/a/file.iso")
    second = await _submit(engine, "sftp://files.example/b/file.iso")
    await _ticks(engine, now)
    # Same host, same basename, same size and even the same bytes: two objects.
    assert len(await _material(repository, [first.id, second.id])) == 2
    assert len(executor.writers()) == 2


async def _all_completed(repository, ids):
    return all(state == TransferState.COMPLETED for state in await _states(repository, ids))


# ── B: a late equivalent sibling of the same transfer ─────────────────────────

async def _late_sibling(world, *, late_bytes=BYTES, late_unsampleable=False, complete_first=True):
    repository, engine, executor, now = world
    executor.objects |= {"mirror-a.example/file.bin": BYTES, "mirror-b.example/file.bin": late_bytes}
    executor.unreachable.add("mirror-b.example")
    if late_unsampleable:
        executor.unsampleable.add("mirror-b.example")
    transfer = await _submit(engine, "https://mirror-a.example/file.bin", "ftp://mirror-b.example/file.bin")
    assert await _until(engine, now, lambda: _started(executor))
    [first] = await _material(repository, [transfer.id])
    if complete_first:
        executor.finish_all()
        assert await _until(engine, now, lambda: _artifact_status(first["id"], "completed"))
    first = await _row(first["id"])
    # Only now can the equivalent sibling classify and prove itself.
    executor.unreachable.discard("mirror-b.example")
    late = next(record.id for record in await repository.requests(transfer.id)
                if record.request.payload.startswith("ftp://"))
    return repository, engine, executor, now, transfer, first, late


async def _started(executor):
    return bool(executor.writers())


async def _row(artifact_id):
    async with database.get_db() as db:
        return await db.fetchone("SELECT * FROM download_files WHERE id=?", (artifact_id,))


async def _artifact_status(artifact_id, status):
    return (await _row(artifact_id))["status"] == status


async def _request_state(request_id):
    async with database.get_db() as db:
        return await db.fetchone("SELECT * FROM transfer_requests WHERE id=?", (request_id,))


async def test_a_late_equivalent_attaches_to_the_live_writer(world):
    repository, engine, executor, now, transfer, first, late = await _late_sibling(world, complete_first=False)
    assert await _until(engine, now, lambda: _resolved(late))
    assert len(executor.writers()) == 1
    assert late in await _origin_requests(engine, first["id"])
    executor.finish_all()
    assert await _until(engine, now, lambda: _all_completed(repository, [transfer.id]))
    assert len(executor.writers()) == 1


async def _resolved(request_id):
    return (await _request_state(request_id))["state"] == "resolved"


async def test_a_late_equivalent_is_satisfied_by_the_completed_writer_without_a_second_one(world):
    repository, engine, executor, now, transfer, first, late = await _late_sibling(world)
    assert await _until(engine, now, lambda: _all_completed(repository, [transfer.id]), finishing=executor)
    # No second writer and no second execution attempt.
    assert len(executor.writers()) == 1
    async with database.get_db() as db:
        attempts = await db.fetchall(
            "SELECT e.id FROM execution_attempts e JOIN download_files f ON f.id=e.artifact_id WHERE f.torrent_id=?",
            (transfer.id,))
    assert len(attempts) == 1
    # The late sibling is satisfied by -- and durably recorded against -- the frozen owner.
    assert (await _request_state(late))["state"] == "resolved"
    assert late in await _origin_requests(engine, first["id"])
    [material] = await _material(repository, [transfer.id])
    assert material["id"] == first["id"]
    # Completed material was never reopened, moved or rewritten.
    after = await _row(first["id"])
    for column in ("status", "local_path", "candidates", "size_bytes", "execution_attempt_id", "filename"):
        assert after[column] == first[column], column


async def test_a_completed_writer_never_absorbs_a_late_sibling_proven_distinct(world):
    repository, engine, executor, now, transfer, first, late = await _late_sibling(world, late_bytes=b"diff")
    assert await _until(engine, now, lambda: _all_completed(repository, [transfer.id]), finishing=executor)
    assert len(await _material(repository, [transfer.id])) == 2
    assert late not in await _origin_requests(engine, first["id"])
    assert (await _row(first["id"]))["status"] == "completed"


async def test_a_late_sibling_whose_identity_cannot_be_proven_is_held_never_guessed_into_the_completed_writer(world):
    repository, engine, executor, now, transfer, first, late = await _late_sibling(world, late_unsampleable=True)
    await _ticks(engine, now, 12)
    assert len(executor.writers()) == 1
    assert late not in await _origin_requests(engine, first["id"])
    assert (await _request_state(late))["state"] == "materializing"
    assert (await repository.get(transfer.id)).state != TransferState.COMPLETED
