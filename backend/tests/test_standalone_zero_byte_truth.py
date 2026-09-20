"""FUNC-001 — durable zero-byte size truth and the standalone direct-HTTP path.

``SizeKnowledge`` already distinguished SIZE_UNKNOWN from SIZE_KNOWN(0) at runtime,
but a numeric ``0`` alone cannot encode both facts durably, and the completion path
permitted empty material for a reason (``artifact.execution is not None``) that is
not evidence about size at all: every failed, pathological and unknown-size
execution has an execution too.

These regressions prove the corrected boundary from four independent directions:

* a standalone, non-cohort real-runtime reproduction of the pathological
  ``200 / Content-Length: 0 / empty body`` shape -- distinct from transfer 286,
  which is a multi-member bootstrap question this one deliberately does not have;
* ``_complete()``'s own direct self-defense, driven into the state normal upstream
  sequencing would not produce, because a semantic boundary must hold for its own
  reasons rather than because callers happen to be well behaved;
* durable persistence and restart reconstruction of the size fact;
* additive migration of a real pre-change database, where a historical numeric
  zero must stay UNKNOWN rather than become legitimate zero.

The runtime-side projection (``transfers.filesystem.size_knowledge``) and the
scope-boundary proof that no wired provider/executor supplies affirmative-zero
evidence are covered by ``test_canonical_completion_truth.py``; this module owns the
durable and end-to-end halves.
"""
from __future__ import annotations

import asyncio
import ctypes
import os
import sqlite3
import struct
import threading
import time
from pathlib import Path

import pytest

import db.database as database
from test_multi_mirror_general_http_convergence import MIRROR_FILENAME, _build_runtime
from test_universal_lifecycle import core, submit  # noqa: F401  (pytest fixture)
from transfers.canonical import CanonicalOwnership
from transfers.models import SizeKnowledge, TransferRequest, TransferState
from transfers.repository import TransferRepository


def _seed_legacy_artifact_sql(artifact_id: int, *, size_bytes, status: str, size_knowledge=...) -> tuple[str, tuple]:
    """One durable artifact row bound to its own request, as the real schema requires.

    ``size_knowledge=...`` means "do not name the column at all", which is how a
    pre-change database behaves once the column has been dropped.
    """
    request_id = f"req-{artifact_id}"
    columns = "id,torrent_id,request_id,filename,local_path,size_bytes,status"
    values = [artifact_id, 1, request_id, f"p{artifact_id}.bin", f"/tmp/p{artifact_id}.bin", size_bytes, status]
    if size_knowledge is not ...:
        columns += ",size_knowledge"
        values.append(size_knowledge)
    placeholders = ",".join("?" for _ in values)
    return f"INSERT INTO download_files({columns}) VALUES({placeholders})", tuple(values)


def _seed_request_sql(artifact_id: int) -> tuple[str, tuple]:
    return (
        "INSERT INTO transfer_requests(id,transfer_id,ordinal,payload,state) VALUES(?,1,?,'{}','resolved')",
        (f"req-{artifact_id}", artifact_id),
    )


# --------------------------------------------------------------------------- #
# Execution-window observation
# --------------------------------------------------------------------------- #

_IN_NONBLOCK = 0o4000
_IN_MODIFY, _IN_ATTRIB, _IN_CLOSE_WRITE = 0x02, 0x04, 0x08
_IN_CREATE, _IN_DELETE, _IN_MOVED_FROM, _IN_MOVED_TO = 0x100, 0x200, 0x40, 0x80
_EVENT_NAMES = {
    _IN_CREATE: "CREATE", _IN_DELETE: "DELETE", _IN_MODIFY: "MODIFY",
    _IN_CLOSE_WRITE: "CLOSE_WRITE", _IN_ATTRIB: "ATTRIB",
    _IN_MOVED_FROM: "MOVED_FROM", _IN_MOVED_TO: "MOVED_TO",
}
_WATCH_MASK = (_IN_CREATE | _IN_DELETE | _IN_MODIFY | _IN_CLOSE_WRITE | _IN_ATTRIB
               | _IN_MOVED_FROM | _IN_MOVED_TO)


class _DownloadRootWatcher:
    """Record every filesystem event under the download root, with sizes.

    Polling cannot answer what happens at the canonical target during a
    real execution: the writer creates and the engine retires within tens of
    milliseconds, so a sampled check would miss it and "never observed" would
    be indistinguishable from "observed too late". inotify sees every event.
    """

    def __init__(self, path: Path):
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        self._libc = libc
        self.fd = libc.inotify_init1(_IN_NONBLOCK)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        if libc.inotify_add_watch(self.fd, str(path).encode(), _WATCH_MASK) < 0:
            error = ctypes.get_errno()
            os.close(self.fd)
            raise OSError(error, "inotify_add_watch failed")
        self.root = path
        self.events: list[tuple[str, str]] = []
        self.sizes_at_event: dict[str, set[int]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                time.sleep(0.0005)
                continue
            except OSError:
                return
            offset = 0
            while offset + 16 <= len(data):
                _wd, mask, _cookie, length = struct.unpack_from("iIII", data, offset)
                offset += 16
                name = data[offset:offset + length].split(b"\x00", 1)[0].decode("utf-8", "replace")
                offset += length
                for bit, label in _EVENT_NAMES.items():
                    if not mask & bit:
                        continue
                    self.events.append((label, name))
                    if label in {"CREATE", "MODIFY", "CLOSE_WRITE"}:
                        try:
                            self.sizes_at_event.setdefault(name, set()).add((self.root / name).stat().st_size)
                        except OSError:
                            pass  # Already retired: nothing observable, which is itself fine.

    def stop(self) -> None:
        if self._stop.is_set():
            return
        time.sleep(0.25)  # Let the tail of the event stream drain before closing.
        self._stop.set()
        self._thread.join(timeout=2)
        os.close(self.fd)

    def labels_for(self, name: str) -> list[str]:
        return [label for label, event_name in self.events if event_name == name]


# --------------------------------------------------------------------------- #
# RED-F1 — standalone real-runtime pathological zero
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_standalone_pathological_zero_is_never_published_as_canonical_material(tmp_path, monkeypatch):
    """One transfer, one request, one pathological zero-byte endpoint.

    This is deliberately NOT transfer 286: there is no sibling, no cohort, no
    alternate, and therefore no multi-member bootstrap question to withhold a
    writer on. The standalone request legitimately reaches normal writer
    execution -- an unknown size is not grounds to refuse it (see
    ``test_standalone_unknown_size_positive_http_still_completes``) -- so the
    zero-byte result is rejected at the canonical verification boundary instead.

    Nothing about this result may become canonical artifact material: not a
    completed artifact, not delivered provenance, not an affirmative durable
    zero, and not a user-visible zero-byte target left in the download root.

    The download root is watched with inotify for the whole execution window,
    because the end state alone cannot distinguish two materially different
    architectures: "nothing was ever written at the canonical target" from
    "something was written there and later retired". MEASURED BEHAVIOUR, on
    this candidate and identically on the frozen baseline: aria2 is dispatched
    with ``dir``/``out`` pointing straight at the canonical target, so it does
    create a real zero-byte file there (CREATE + CLOSE_WRITE at size 0), and
    the engine retires it ~50ms later once verification rejects it. This
    regression therefore pins the guarantee that actually protects an operator,
    and states it as an implication rather than asserting the CREATE: whatever
    appears at the canonical target during execution must never hold content,
    must always be retired, and must never be published. A future
    staging/atomic-promote materialization that never creates the canonical
    path at all satisfies this unchanged.
    """
    runtime = await _build_runtime(tmp_path, monkeypatch)
    empty_path = "/empty-" + MIRROR_FILENAME
    runtime.server.route(empty_path, b"", behavior="zero_byte_success")
    try:
        watcher = _DownloadRootWatcher(runtime.downloads)
    except OSError as exc:  # pragma: no cover - inotify is unavailable
        await runtime.close()
        pytest.skip(f"inotify unavailable for execution-window observation: {exc}")
    try:
        request = TransferRequest("http", runtime.server.url(1, empty_path))
        transfer = await runtime.engine.submit((request,), deduplicate=False)

        async def materialized():
            artifacts = await runtime.repository.artifacts(transfer.id)
            return artifacts or None

        await runtime.until(materialized, label="the standalone artifact materializes")

        # Give the scheduler ample opportunity to reach any later state -- the
        # rejection must be stable, not a window that closes into completion.
        for _ in range(25):
            await runtime.engine.tick()
            await asyncio.sleep(0.02)

        artifacts = await runtime.repository.artifacts(transfer.id)
        assert len(artifacts) == 1
        artifact = artifacts[0]
        watcher.stop()

        # No completed artifact.
        assert artifact.state != "completed"
        assert artifact.expected_bytes == 0

        # No canonical/user-visible zero-byte target survives, and the
        # execution-scoped output the real aria2 wrote was retired.
        assert not Path(artifact.target).exists()
        assert [item for item in runtime.downloads.rglob("*") if item.is_file() and item.stat().st_size == 0] == []

        # What actually happened at the canonical target during execution.
        target_name = Path(artifact.target).name
        observed = watcher.labels_for(target_name)
        assert watcher.sizes_at_event.get(target_name, set()) <= {0}, (
            "unverified material at the canonical target must never hold content: "
            f"observed sizes {sorted(watcher.sizes_at_event.get(target_name, set()))}"
        )
        if "CREATE" in observed or "MOVED_TO" in observed:
            assert "DELETE" in observed or "MOVED_FROM" in observed, (
                "material created at the canonical target during execution must be retired, "
                f"but the observed event sequence was {observed}"
            )
        # Whatever the writer did, no sidecar residue is left behind either.
        assert list(runtime.downloads.rglob("*.aria2")) == []

        async with database.get_db() as db:
            stored = await db.fetchone("SELECT size_knowledge FROM download_files WHERE id=?", (artifact.id,))
            delivered = await db.fetchone(
                "SELECT COUNT(*) AS n FROM execution_attempt_provenance WHERE artifact_id=? AND delivered=1",
                (artifact.id,),
            )
            completed_provenance = await db.fetchone(
                "SELECT COUNT(*) AS n FROM execution_attempt_provenance WHERE artifact_id=? AND outcome='completed'",
                (artifact.id,),
            )
        assert int(delivered["n"]) == 0
        assert int(completed_provenance["n"]) == 0

        # No affirmative zero truth, durably or in the reconstructed fact.
        assert stored["size_knowledge"] is None, "an untrusted zero must never be recorded as durable size truth"
        assert artifact.size_knowledge == SizeKnowledge.UNKNOWN

        final = await runtime.repository.get(transfer.id)
        assert final.state not in {TransferState.COMPLETED, TransferState.CONSOLIDATED}
    finally:
        watcher.stop()
        await runtime.close()


@pytest.mark.asyncio
async def test_standalone_unknown_size_positive_http_still_completes(tmp_path, monkeypatch):
    """G-F6 control: closing the zero gap must not make unknown-size HTTP unsupported.

    ``GeneralHttpProvider`` performs no resolution-time sizing, so this transfer's
    size is genuinely unknown until the executor measures it. It must still
    complete normally on real positive material.
    """
    runtime = await _build_runtime(tmp_path, monkeypatch)
    real_path = "/" + MIRROR_FILENAME
    payload = b"debridpulse-func-001-unknown-size-positive" * 512
    runtime.server.route(real_path, payload, behavior="normal")
    try:
        request = TransferRequest("http", runtime.server.url(1, real_path))
        transfer = await runtime.engine.submit((request,), deduplicate=False)

        async def completed():
            artifacts = await runtime.repository.artifacts(transfer.id)
            return next((item for item in artifacts if item.state == "completed"), None)

        artifact = await runtime.until(completed, label="the unknown-size positive payload completes")
        assert artifact.expected_bytes == len(payload)
        assert artifact.size_knowledge == SizeKnowledge.KNOWN_POSITIVE
        assert Path(artifact.target).read_bytes() == payload

        async with database.get_db() as db:
            stored = await db.fetchone("SELECT size_knowledge FROM download_files WHERE id=?", (artifact.id,))
        assert stored["size_knowledge"] == SizeKnowledge.KNOWN_POSITIVE.value
    finally:
        await runtime.close()


# --------------------------------------------------------------------------- #
# RED-F2 — _complete() self-defense
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_complete_rejects_unproven_empty_material_even_with_an_execution(core, tmp_path):
    """Drive ``_complete()`` directly into the state it must defend against.

    Normal sequencing rejects an unknown zero earlier, at the SUCCEEDED
    verification boundary, so this test constructs the adversarial caller state
    deliberately: an artifact with empty material on disk, a live execution, a
    persisted size of ``0`` and no affirmative durable zero. Execution presence
    used to be the empty-acceptance reason here; it is not evidence about size,
    and the boundary must fail closed on canonical size truth alone.
    """
    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    assert artifact.execution is not None, "fixture must reach a real execution"

    # The adversarial state: real empty material, a live execution, size 0, and
    # nothing that affirmatively establishes this object as zero bytes.
    target = Path(artifact.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"")
    # Deliberately does not name the durable size-knowledge column: the defect
    # this proves is a semantic one about the decision input, so the assertions
    # below hold an unfixed tree to the same behavioural standard rather than
    # merely observing that a new column is missing.
    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET size_bytes=0 WHERE id=?", (artifact.id,))
        await db.commit()

    artifacts = await core.repository.artifacts(transfer.id)
    assert artifacts[0].expected_bytes == 0
    assert artifacts[0].execution is not None

    await core.engine._complete(transfer.id, artifacts)

    final = await core.repository.get(transfer.id)
    assert final.state != TransferState.COMPLETED, (
        "an execution's mere presence is not evidence that empty material is legitimately zero bytes"
    )
    assert final.state != TransferState.POST_PROCESSING
    reloaded = (await core.repository.artifacts(transfer.id))[0]
    assert reloaded.state != "completed"
    assert reloaded.size_knowledge == SizeKnowledge.UNKNOWN


@pytest.mark.asyncio
async def test_complete_accepts_empty_material_only_on_affirmative_durable_zero(core):
    """The same boundary, with the one fact that legitimately authorizes it.

    This is the control for the test above: with durable ``KNOWN_ZERO`` truth --
    the single value only a verified affirmative-zero completion ever writes --
    the identical empty material is accepted. The gate is canonical size truth,
    not a blanket refusal of empty files.
    """
    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]

    target = Path(artifact.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"")
    async with database.get_db() as db:
        await db.execute(
            "UPDATE download_files SET size_bytes=0,size_knowledge=? WHERE id=?",
            (SizeKnowledge.KNOWN_ZERO.value, artifact.id),
        )
        await db.commit()

    artifacts = await core.repository.artifacts(transfer.id)
    assert artifacts[0].size_knowledge == SizeKnowledge.KNOWN_ZERO

    await core.engine._complete(transfer.id, artifacts)

    final = await core.repository.get(transfer.id)
    assert final.state in {TransferState.COMPLETED, TransferState.POST_PROCESSING}


# --------------------------------------------------------------------------- #
# RED-F3 — durable persistence and restart reconstruction
# --------------------------------------------------------------------------- #

async def _artifact_row(tmp_path, monkeypatch, *, size_bytes, stored):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "durable-size.db")
    await database.init_db()
    async with database.get_db() as db:
        await db.execute("INSERT INTO torrents(id,hash,name,status) VALUES(1,?,?,'downloading')", ("d" * 40, "t"))
        await db.execute(*_seed_request_sql(1))
        await db.execute(*_seed_legacy_artifact_sql(1, size_bytes=size_bytes, status="downloading", size_knowledge=stored))
        await db.commit()
    # A brand-new repository instance is exactly what a restarted process has.
    return (await TransferRepository().artifacts(1))[0]


@pytest.mark.parametrize(
    "size_bytes,stored,expected",
    [
        (0, None, SizeKnowledge.UNKNOWN),                       # no evidence at all
        (0, "known_zero", SizeKnowledge.KNOWN_ZERO),            # affirmative, durably recorded
        (4096, None, SizeKnowledge.KNOWN_POSITIVE),             # legacy positive row
        (4096, "known_positive", SizeKnowledge.KNOWN_POSITIVE),
        (4096, "known_zero", SizeKnowledge.KNOWN_POSITIVE),     # positive evidence outranks a stale zero
        (0, "unknown", SizeKnowledge.UNKNOWN),
    ],
)
@pytest.mark.asyncio
async def test_size_knowledge_survives_restart(tmp_path, monkeypatch, size_bytes, stored, expected):
    artifact = await _artifact_row(tmp_path, monkeypatch, size_bytes=size_bytes, stored=stored)
    assert artifact.size_knowledge == expected


@pytest.mark.asyncio
async def test_a_bare_persisted_zero_can_never_reconstruct_as_legitimate_zero(tmp_path, monkeypatch):
    """The structural point of the durable field: ``0`` alone is not a fact.

    Two rows carry the identical numeric size. Only the one with durable
    affirmative truth reconstructs as ``KNOWN_ZERO`` -- which is precisely the
    distinction a bare integer column cannot preserve across a restart.
    """
    unknown = await _artifact_row(tmp_path, monkeypatch, size_bytes=0, stored=None)
    assert unknown.size_knowledge == SizeKnowledge.UNKNOWN

    async with database.get_db() as db:
        await db.execute(*_seed_request_sql(2))
        await db.execute(
            *_seed_legacy_artifact_sql(2, size_bytes=0, status="downloading", size_knowledge=SizeKnowledge.KNOWN_ZERO.value)
        )
        await db.commit()

    reloaded = {item.id: item for item in await TransferRepository().artifacts(1)}
    assert reloaded[1].expected_bytes == reloaded[2].expected_bytes == 0
    assert reloaded[1].size_knowledge == SizeKnowledge.UNKNOWN
    assert reloaded[2].size_knowledge == SizeKnowledge.KNOWN_ZERO


@pytest.mark.asyncio
async def test_every_artifact_loader_reconstructs_the_same_size_fact(tmp_path, monkeypatch):
    """One interpretation, whichever query produced the row.

    ``Artifact`` is reconstructed by more than one durable loader. If any of
    them forgot the size fact it would silently default to ``UNKNOWN``, which is
    exactly the second-authority failure mode this correction must not create.
    """
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "loaders.db")
    await database.init_db()
    async with database.get_db() as db:
        await db.execute("INSERT INTO torrents(id,hash,name,status) VALUES(1,?,?,'downloading')", ("a" * 40, "t"))
        await db.execute(*_seed_request_sql(1))
        await db.execute(*_seed_legacy_artifact_sql(1, size_bytes=4096, status="downloading", size_knowledge=None))
        await db.execute(*_seed_request_sql(2))
        await db.execute(
            *_seed_legacy_artifact_sql(2, size_bytes=0, status="downloading",
                                       size_knowledge=SizeKnowledge.KNOWN_ZERO.value)
        )
        await db.commit()

    repository = TransferRepository()
    by_repository = {item.id: item.size_knowledge for item in await repository.artifacts(1)}
    by_catalog = {
        item.id: item.size_knowledge for item in await CanonicalOwnership(repository).canonical_artifacts()
    }

    assert by_repository == {1: SizeKnowledge.KNOWN_POSITIVE, 2: SizeKnowledge.KNOWN_ZERO}
    assert by_catalog == by_repository


# --------------------------------------------------------------------------- #
# RED-F4 — real pre-change database migration
# --------------------------------------------------------------------------- #

def _columns(path: Path, table: str) -> list[str]:
    with sqlite3.connect(path) as conn:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


@pytest.mark.asyncio
async def test_pre_change_database_migrates_additively_without_manufacturing_zero_truth(tmp_path, monkeypatch):
    """A real pre-change database: the column is absent, then added additively.

    Representative legacy rows carry a positive size and a zero/default size.
    After migration the positive row must read ``KNOWN_POSITIVE`` and the zero
    row must read ``UNKNOWN`` -- never ``KNOWN_ZERO``. Nothing backfills, and
    the migration manufactures no semantic history.
    """
    path = tmp_path / "pre-change.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()

    # Simulate a database created before this additive change.
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE download_files DROP COLUMN size_knowledge")
        conn.execute("INSERT INTO torrents(id,hash,name,status) VALUES(1,?,?,'completed')", ("e" * 40, "legacy"))
        for artifact_id, size, status in ((1, 987654321, "completed"), (2, 0, "error")):
            conn.execute(*_seed_request_sql(artifact_id))
            conn.execute(*_seed_legacy_artifact_sql(artifact_id, size_bytes=size, status=status))
        conn.commit()
    assert "size_knowledge" not in _columns(path, "download_files")

    await database.init_db()  # "start new code"
    await database.validate_transfer_repository_schema()
    assert "size_knowledge" in _columns(path, "download_files")

    with sqlite3.connect(path) as conn:
        stored = dict(conn.execute("SELECT id,size_knowledge FROM download_files").fetchall())
    assert stored == {1: None, 2: None}, "the migration must not backfill any semantic value"

    artifacts = {item.id: item for item in await TransferRepository().artifacts(1)}
    assert artifacts[1].size_knowledge == SizeKnowledge.KNOWN_POSITIVE
    assert artifacts[1].expected_bytes == 987654321
    assert artifacts[2].size_knowledge == SizeKnowledge.UNKNOWN, (
        "a historical persisted zero must never become legitimate zero during migration"
    )


@pytest.mark.asyncio
async def test_initialization_is_idempotent_over_a_real_pre_change_database(tmp_path, monkeypatch):
    """§5.5 twice-initialization: no error, no duplicate column, no drift."""
    path = tmp_path / "twice.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE download_files DROP COLUMN size_knowledge")
        conn.execute("INSERT INTO torrents(id,hash,name,status) VALUES(1,?,?,'completed')", ("f" * 40, "legacy"))
        for artifact_id, size, status in ((1, 0, "error"), (2, 512, "completed")):
            conn.execute(*_seed_request_sql(artifact_id))
            conn.execute(*_seed_legacy_artifact_sql(artifact_id, size_bytes=size, status=status))
        conn.commit()

    await database.init_db()
    first = _columns(path, "download_files")
    first_rows = {item.id: item.size_knowledge for item in await TransferRepository().artifacts(1)}

    await database.init_db()
    second = _columns(path, "download_files")
    second_rows = {item.id: item.size_knowledge for item in await TransferRepository().artifacts(1)}

    assert first == second
    assert first.count("size_knowledge") == 1
    assert first_rows == second_rows == {1: SizeKnowledge.UNKNOWN, 2: SizeKnowledge.KNOWN_POSITIVE}
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM download_files").fetchone()[0] == 2
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# --------------------------------------------------------------------------- #
# G-F1 / G-F2 — durable unknown-zero matrix and positive precedence
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value", [0, None, "", "0", -1, False, "not-a-number", 0.0])
def test_no_non_positive_durable_value_is_ever_legitimate_zero(value):
    assert SizeKnowledge.durable(value, None) == SizeKnowledge.UNKNOWN
    assert SizeKnowledge.durable(value, "unknown") == SizeKnowledge.UNKNOWN
    assert SizeKnowledge.durable(value, "known_positive") == SizeKnowledge.UNKNOWN
    assert SizeKnowledge.durable(value, "") == SizeKnowledge.UNKNOWN


@pytest.mark.parametrize("stored", [None, "", "unknown", "known_zero", "known_positive", "garbage"])
def test_positive_durable_evidence_is_never_downgraded(stored):
    assert SizeKnowledge.durable(1, stored) == SizeKnowledge.KNOWN_POSITIVE
    assert SizeKnowledge.durable(11_038_065_950, stored) == SizeKnowledge.KNOWN_POSITIVE


def test_only_the_exact_affirmative_zero_value_authorizes_known_zero():
    assert SizeKnowledge.durable(0, "known_zero") == SizeKnowledge.KNOWN_ZERO
    for near_miss in ("KNOWN_ZERO", "zero", "known-zero", "true", "1", " known_zero"):
        assert SizeKnowledge.durable(0, near_miss) == SizeKnowledge.UNKNOWN
