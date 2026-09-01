"""Transactional v1-to-canonical migration with a verified pre-migration backup.

This is the only integration-aware database upgrade for the v1 format. Concrete
decoders terminate the old provider/job fields at their respective boundaries.
Future providers do not modify this migration or the ordinary repository.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import aiosqlite

import db.database as database
from executors.aria2.migration import legacy_handle
from providers.alldebrid.migration import legacy_candidate, legacy_resource
from transfers import codec
from transfers.errors import Category, Domain, NormalizedError, Stage, safe_diagnostic
from transfers.models import SourceEntry, TransferRequest
from transfers.repository import TransferRepository


def _identity(kind, value):
    return uuid5(NAMESPACE_URL, f"debridpulse:v112:{kind}:{value}").hex


async def _backup() -> Path:
    source_path = Path(database.DB_PATH)
    final = source_path.with_name(source_path.name + ".pre-v112.sqlite3")
    if final.exists():
        async with aiosqlite.connect(final) as existing:
            check = await (await existing.execute("PRAGMA quick_check")).fetchone()
            if check != ("ok",):
                raise RuntimeError("Existing pre-migration backup failed verification")
        return final
    temporary = final.with_name(final.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        async with aiosqlite.connect(source_path) as source, aiosqlite.connect(temporary) as target:
            await source.backup(target)
            check = await (await target.execute("PRAGMA quick_check")).fetchone()
            if check != ("ok",):
                raise RuntimeError("Pre-migration backup failed verification")
        os.replace(temporary, final)
    finally:
        temporary.unlink(missing_ok=True)
    return final


def _requests(parent, deferred):
    if deferred:
        kind = "torrent" if deferred["kind"] == "torrent_file" else "magnet"
        return (TransferRequest(kind, deferred["payload"], name=deferred.get("filename") or parent["name"] or "",
                                fingerprint=parent["hash"], preferred_provider="alldebrid"),)
    payload = str(parent.get("magnet") or "")
    if parent.get("source") == "direct_link":
        try:
            values = json.loads(payload)
        except (TypeError, ValueError):
            values = []
        if isinstance(values, list) and values and all(isinstance(item, str) for item in values):
            return tuple(TransferRequest(urlsplit(item).scheme, item, preferred_provider="alldebrid") for item in values if item)
    if payload.startswith("magnet:"):
        return (TransferRequest("magnet", payload, name=parent["name"] or "", fingerprint=parent["hash"], preferred_provider="alldebrid"),)
    if re.fullmatch(r"[a-fA-F0-9]{40}", str(parent.get("hash") or "")):
        payload = "magnet:?xt=urn:btih:" + parent["hash"]
        return (TransferRequest("magnet", payload, name=parent["name"] or "", fingerprint=parent["hash"], preferred_provider="alldebrid"),)
    return (TransferRequest("legacy-resource", str(parent["id"]), name=parent["name"] or "", fingerprint=parent["hash"]),)


def _error(message, *, execution=False):
    if not message:
        return None
    return NormalizedError(Domain.EXECUTOR if execution else Domain.PROVIDER,
        Category.UNMAPPED_EXECUTOR_ERROR if execution else Category.UNMAPPED_PROVIDER_ERROR,
        Stage.EXECUTION if execution else Stage.RESOLUTION, diagnostic=safe_diagnostic(message))


async def migrate(*, external_executor: bool, globally_paused: bool = False) -> dict:
    async with database.get_db() as db:
        present = await db.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'")
        if present and await db.fetchone("SELECT version FROM schema_migrations WHERE version='1.0.12'"):
            return {"migrated": False}
    backup = await _backup()
    repository = TransferRepository()
    await repository.initialize()
    count = 0
    async with database.get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY,applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        parents = await db.fetchall("SELECT * FROM torrents ORDER BY id")
        owned = {row["gid"] for row in await db.fetchall("SELECT gid FROM debridpulse_aria2_owned_gids")}
        for parent in parents:
            if await db.fetchone("SELECT id FROM transfer_requests WHERE transfer_id=? LIMIT 1", (parent["id"],)):
                continue
            deferred = await db.fetchone("SELECT * FROM deferred_provider_submissions WHERE torrent_id=?", (parent["id"],))
            files = await db.fetchall("SELECT * FROM download_files WHERE torrent_id=? ORDER BY id", (parent["id"],))
            observed = legacy_resource(parent)
            if observed:
                await repository._resource(db, parent["id"], observed.resource, observed.state)
            requests = _requests(parent, deferred)
            root_ids = []
            source_ids = {}
            for ordinal, request in enumerate(requests):
                identity = _identity("root", f"{parent['id']}:{ordinal}")
                root_ids.append(identity)
                if isinstance(request.payload, str):
                    source_ids[request.payload] = identity
                state = "resolved" if files or parent["status"] in {"completed", "deleted"} else "waiting" if observed else "pending"
                if deferred:
                    state = "pending"
                await db.execute("""INSERT INTO transfer_requests(id,transfer_id,ordinal,payload,state,resource,attempts)
                    VALUES(?,?,?,?,?,?,?)""", (identity, parent["id"], ordinal, codec.dump(request), state,
                    codec.dump(observed.resource) if observed else None, int(parent.get("upload_retry_count") or 0)))
            primary_candidates = {}
            used_requests = set()
            for file in files:
                source = str(file.get("source_url") or "")
                request_id = source_ids.get(source) or _identity("file", file["id"])
                if request_id in used_requests:
                    request_id = _identity("file", file["id"])
                used_requests.add(request_id)
                physical = bool(file.get("local_path")) and not file.get("blocked") and file.get("mirror_state") != "standby"
                candidate = legacy_candidate(file, observed.resource if observed else None)
                if request_id not in root_ids:
                    request = candidate.refresh_request if candidate and candidate.refresh_request else TransferRequest(
                        urlsplit(source).scheme or "legacy-artifact", source or str(file["id"]), name=file["filename"] or "", preferred_provider="alldebrid")
                    entry = SourceEntry(file["filename"] or "download", int(file.get("size_bytes") or 0), file["filename"] or "download", request)
                    await db.execute("""INSERT INTO transfer_requests(id,transfer_id,parent_id,ordinal,payload,state,metadata)
                        VALUES(?,?,?,?,?,'resolved',?)""", (request_id, parent["id"], root_ids[0] if root_ids else None,
                        int(file["id"]), codec.dump(request), codec.dump(entry)))
                error = _error(file.get("block_reason"), execution=physical)
                if not physical:
                    if file.get("blocked"):
                        await db.execute("UPDATE download_files SET request_id=?,candidates=?,status='blocked' WHERE id=?",
                                         (request_id, codec.dump((candidate,)) if candidate else None, file["id"]))
                        await db.execute("UPDATE transfer_requests SET state='skipped' WHERE id=?", (request_id,))
                    if file.get("status") in {"error", "missing"} and file.get("mirror_state") != "standby":
                        error = error or _error("Legacy source outcome")
                        await db.execute("UPDATE transfer_requests SET state='failed',error=? WHERE id=?", (codec.dump(error), request_id))
                    # Retain source and mirror rows for historical presentation,
                    # outside the physical denominator and executor authority.
                    if file.get("mirror_state") == "standby" and candidate:
                        primary_candidates.setdefault(int(file.get("mirror_group_id") or 0), []).append(candidate)
                    continue
                # A resolver input was often copied into download_url before
                # unlock in v1. It is not evidence of a usable candidate.
                unresolved = not candidate or (source and source == str(file.get("download_url") or ""))
                candidates = () if unresolved else (candidate,)
                attempt_id = _identity("execution", file["id"])
                handle = legacy_handle(file, attempt_id, candidate)
                state = str(file.get("status") or "queued")
                if state in {"pending", "waiting", "ready"}:
                    state = "queued"
                if state == "completed" and parent["status"] != "completed":
                    state = "verifying" if handle else "queued"
                if unresolved and not handle and state != "completed":
                    state = "unresolved"
                    await db.execute("UPDATE transfer_requests SET state='pending' WHERE id=?", (request_id,))
                await db.execute("""UPDATE download_files SET request_id=?,candidates=?,execution_attempt_id=?,status=?,normalized_error=? WHERE id=?""",
                    (request_id, codec.dump(candidates), attempt_id if handle else None, state, codec.dump(error) if error else None, file["id"]))
                if handle:
                    authorized = not external_executor or str(file["download_id"]) in owned
                    execution_state = {"completed": "succeeded", "verifying": "succeeded", "downloading": "transferring",
                        "queued": "queued", "paused": "paused", "error": "failed"}.get(state, "unknown")
                    if not authorized:
                        error = NormalizedError(Domain.LIFECYCLE, Category.OWNERSHIP_CONFLICT, Stage.RECONCILIATION)
                        await db.execute("UPDATE download_files SET status='error',normalized_error=? WHERE id=?", (codec.dump(error), file["id"]))
                    await db.execute("""INSERT INTO execution_attempts(id,transfer_id,artifact_id,executor_id,handle,state,authorized,error)
                        VALUES(?,?,?,?,?,?,?,?)""", (attempt_id, parent["id"], file["id"], handle.executor_id,
                        codec.dump(handle), execution_state, int(authorized), codec.dump(error) if error else None))
            for primary_id, alternatives in primary_candidates.items():
                primary = await db.fetchone("SELECT candidates FROM download_files WHERE id=? AND torrent_id=? AND request_id IS NOT NULL", (primary_id, parent["id"]))
                if primary:
                    candidates = [codec.candidate(item) for item in codec.load(primary["candidates"], [])] + alternatives
                    await db.execute("UPDATE download_files SET candidates=? WHERE id=?", (codec.dump(candidates), primary_id))
            error = _error(parent.get("error_message"))
            state = "processing" if parent["status"] == "uploading" else parent["status"]
            await db.execute("UPDATE torrents SET status=?,normalized_error=?,error_message=? WHERE id=?",
                             (state, codec.dump(error) if error else None, error.message if error else None, parent["id"]))
            if state == "paused":
                await db.execute("INSERT OR IGNORE INTO transfer_pause_intents(torrent_id,paused) VALUES(?,1)", (parent["id"],))
            count += 1
        await db.execute("""INSERT INTO transfer_controls(key,value) VALUES('paused',?)
            ON CONFLICT(key) DO NOTHING""", ("1" if globally_paused else "0",))
        violations = await db.fetchall("PRAGMA foreign_key_check")
        if violations:
            raise RuntimeError("Migration refused: database contains foreign-key violations")
        await db.execute("INSERT INTO schema_migrations(version) VALUES('1.0.12')")
        await db.commit()
    return {"migrated": True, "transfers": count, "backup": str(backup)}
