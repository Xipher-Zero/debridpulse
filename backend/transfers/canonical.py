"""Provider-neutral canonical artifact ownership across transfer scopes.

Equivalence evidence is gathered by the engine before entering this owner.  This
module performs only durable discovery and short SQLite ownership transactions;
it never performs provider or executor I/O.  Phase 1 keeps foreign candidate
origin through the existing standby artifact plus route-attempt provenance so
Phase 2 can formalize the binding without URL, filename, host, or path inference.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from db.database import get_db
from transfers import codec
from transfers.models import Artifact, RequestRecord, TransferCandidate


_TERMINAL_TRANSFERS = {"completed", "deleted", "cancelled", "error"}
_TERMINAL_ARTIFACTS = {"completed", "cancelled", "error", "duplicate"}


@dataclass(frozen=True)
class CandidateOrigin:
    canonical_artifact_id: int
    contributing_artifact_id: int
    contributing_transfer_id: int
    request: RequestRecord
    resolution_attempt_id: str
    candidate_id: str
    provider_id: str
    source: object | None


class CanonicalOwnership:
    """Durable lookup, deterministic admission ordering, and atomic attachment."""

    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def _record(row) -> RequestRecord:
        return RequestRecord(
            row["id"], int(row["transfer_id"]), codec.request(codec.load(row["payload"])), row["state"],
            row["parent_id"], codec.resource(codec.load(row["resource"])), int(row["attempts"] or 0),
            float(row["retry_at"] or 0), codec.error(row["error"]), codec.entry(codec.load(row["metadata"])),
        )

    @staticmethod
    def _artifact(row) -> Artifact:
        return Artifact(
            int(row["id"]), int(row["torrent_id"]), row["request_id"], row["filename"], row["local_path"],
            int(row["size_bytes"] or 0), row["status"],
            tuple(codec.candidate(item) for item in codec.load(row["candidates"], [])),
            int(row["selected_candidate"] or 0), codec.handle(codec.load(row["handle"])),
            int(row["retry_count"] or 0), float(row["retry_at"] or 0), codec.error(row["normalized_error"]),
        )

    @staticmethod
    async def _origin_attempt(db, request_id: str, candidate: TransferCandidate):
        """Resolve origin strictly through durable request/candidate IDs."""
        rows = await db.fetchall(
            """SELECT p.resolution_attempt_id,p.candidate_summary,p.ordinal,a.provider_id
                FROM route_attempt_provenance p JOIN resolution_attempts a ON a.id=p.resolution_attempt_id
                WHERE p.request_id=? AND a.state='succeeded'
                ORDER BY p.ordinal DESC,a.updated_at DESC,a.id DESC""",
            (request_id,),
        )
        candidate_id = str(candidate.id)
        for row in rows:
            provider_id = str(row.get("provider_id") or "")
            if candidate.provider_id and provider_id != str(candidate.provider_id):
                continue
            for item in codec.load(row.get("candidate_summary"), []):
                if str(item.get("candidate_id") or "") == candidate_id:
                    return str(row["resolution_attempt_id"]), provider_id, item.get("source")
        return None

    async def canonical_artifacts(self) -> tuple[Artifact, ...]:
        """Eligible established destination owners across active transfers."""
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT f.*,e.handle FROM download_files f
                    JOIN torrents t ON t.id=f.torrent_id
                    LEFT JOIN execution_attempts e ON e.id=f.execution_attempt_id
                    WHERE f.request_id IS NOT NULL AND COALESCE(f.blocked,0)=0
                    AND COALESCE(f.mirror_state,'')!='standby'
                    AND (f.mirror_group_id IS NULL OR f.mirror_group_id=f.id)
                    AND f.status NOT IN ('completed','cancelled','error','duplicate')
                    AND t.status NOT IN ('completed','deleted','cancelled','error')
                    ORDER BY f.torrent_id,f.id"""
            )
        return tuple(self._artifact(row) for row in rows)

    async def lower_materializing(self, record: RequestRecord):
        """Return only contenders with an earlier durable admission identity.

        Transfer ids are AUTOINCREMENT admission identities.  Request ordinal and
        the persisted SQLite insertion rowid provide deterministic ordering for
        requests admitted within the same transfer.
        """
        async with get_db() as db:
            current = await db.fetchone(
                """SELECT r.transfer_id,r.ordinal,r.rowid AS admission_rowid,r.state,t.status AS transfer_status
                    FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id WHERE r.id=?""",
                (record.id,),
            )
            if (not current or current["state"] != "materializing"
                    or current["transfer_status"] in _TERMINAL_TRANSFERS):
                return ()
            current_order = (int(current["transfer_id"]), int(current["ordinal"] or 0), int(current["admission_rowid"]))
            rows = await db.fetchall(
                """SELECT r.*,r.rowid AS admission_rowid,t.status AS transfer_status
                    FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id
                    WHERE r.id!=? AND r.state='materializing'
                    AND t.status NOT IN ('completed','deleted','cancelled','error')
                    ORDER BY r.transfer_id,r.ordinal,r.rowid,r.id""",
                (record.id,),
            )
            result = []
            for row in rows:
                order = (int(row["transfer_id"]), int(row["ordinal"] or 0), int(row["admission_rowid"]))
                if order >= current_order:
                    continue
                attempt = await db.fetchone(
                    """SELECT a.result FROM resolution_attempts a
                        LEFT JOIN route_attempt_provenance p ON p.resolution_attempt_id=a.id
                        WHERE a.request_id=? AND a.state='succeeded'
                        ORDER BY COALESCE(p.ordinal,0) DESC,a.updated_at DESC,a.id DESC LIMIT 1""",
                    (row["id"],),
                )
                payload = codec.load(attempt["result"], {}) if attempt and attempt.get("result") else {}
                candidates = tuple(codec.candidate(item) for item in payload.get("candidates", []))
                if candidates:
                    result.append((self._record(row), candidates, order))
        return tuple(result)

    async def attach(self, primary: Artifact, record: RequestRecord, candidates, size: int) -> bool:
        """Atomically revalidate an established owner and attach one foreign source."""
        alternatives = tuple(replace(item, expected_bytes=size) for item in candidates)
        if not alternatives:
            return False
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            current = await db.fetchone(
                """SELECT f.* FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                    WHERE f.id=? AND f.request_id IS NOT NULL AND COALESCE(f.blocked,0)=0
                    AND COALESCE(f.mirror_state,'')!='standby'
                    AND (f.mirror_group_id IS NULL OR f.mirror_group_id=f.id)
                    AND f.status NOT IN ('completed','cancelled','error','duplicate')
                    AND t.status NOT IN ('completed','deleted','cancelled','error')""",
                (primary.id,),
            )
            incoming = await db.fetchone(
                """SELECT r.id FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id
                    WHERE r.id=? AND r.transfer_id=? AND r.state='materializing'
                    AND t.status NOT IN ('completed','deleted','cancelled','error')""",
                (record.id, record.transfer_id),
            )
            if not current or not incoming:
                await db.rollback()
                return False

            # A merge is forbidden unless every attached candidate retains a
            # durable route-attempt origin.  Failure here falls back to ordinary
            # independent materialization rather than producing lossy provenance.
            for candidate in alternatives:
                if await self._origin_attempt(db, record.id, candidate) is None:
                    await db.rollback()
                    return False

            retained = [replace(codec.candidate(item), expected_bytes=size)
                        for item in codec.load(current["candidates"], [])]
            seen = {str(item.id) for item in retained}
            for candidate in alternatives:
                if str(candidate.id) not in seen:
                    retained.append(candidate)
                    seen.add(str(candidate.id))

            standby = await db.fetchone("SELECT * FROM download_files WHERE request_id=?", (record.id,))
            if standby and not (
                    standby.get("mirror_state") == "standby"
                    and int(standby.get("mirror_group_id") or 0) == int(primary.id)):
                await db.rollback()
                return False

            await db.execute(
                """UPDATE download_files SET candidates=?,size_bytes=?,mirror_group_id=?,mirror_state='primary',
                    updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (codec.dump(retained), size, primary.id, primary.id),
            )
            if standby:
                await db.execute(
                    """UPDATE download_files SET torrent_id=?,filename=?,size_bytes=?,local_path=?,status='duplicate',blocked=NULL,
                        mirror_group_id=?,mirror_state='standby',candidates=?,download_client='',normalized_error=NULL,
                        updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (record.transfer_id, alternatives[0].name, size, primary.target, primary.id,
                     codec.dump(alternatives), standby["id"]),
                )
            else:
                await db.execute(
                    """INSERT INTO download_files(torrent_id,request_id,filename,size_bytes,local_path,status,blocked,
                        mirror_group_id,mirror_state,candidates,download_client)
                        VALUES(?,?,?,?,?,'duplicate',NULL,?,'standby',?,'')""",
                    (record.transfer_id, record.id, alternatives[0].name, size, primary.target,
                     primary.id, codec.dump(alternatives)),
                )
            cursor = await db.execute(
                "UPDATE transfer_requests SET state='resolved',error=NULL WHERE id=? AND transfer_id=? AND state='materializing'",
                (record.id, record.transfer_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            await db.commit()
        return True

    async def _origin_for_candidate(self, db, canonical_artifact_id: int, candidate: TransferCandidate):
        rows = await db.fetchall(
            """SELECT f.id,f.torrent_id,f.request_id,f.candidates FROM download_files f
                WHERE f.id=? OR (f.mirror_group_id=? AND f.mirror_state='standby')
                ORDER BY CASE WHEN f.id=? THEN 0 ELSE 1 END,f.id""",
            (canonical_artifact_id, canonical_artifact_id, canonical_artifact_id),
        )
        candidate_id = str(candidate.id)
        for row in rows:
            if not row.get("request_id"):
                continue
            stored = tuple(codec.candidate(item) for item in codec.load(row.get("candidates"), []))
            if not any(str(item.id) == candidate_id for item in stored):
                continue
            origin = await self._origin_attempt(db, row["request_id"], candidate)
            if origin is None:
                # Canonical rows also contain appended foreign candidates.  A
                # missing request-local route here means keep looking for that
                # candidate's standby row rather than fabricating provenance.
                continue
            request_row = await db.fetchone("SELECT * FROM transfer_requests WHERE id=?", (row["request_id"],))
            if not request_row:
                continue
            attempt_id, provider_id, source = origin
            return CandidateOrigin(
                canonical_artifact_id, int(row["id"]), int(row["torrent_id"]), self._record(request_row),
                attempt_id, candidate_id, provider_id, source,
            )
        return None

    async def origin_for(self, artifact: Artifact, candidate: TransferCandidate) -> CandidateOrigin | None:
        async with get_db() as db:
            return await self._origin_for_candidate(db, artifact.id, candidate)

    async def origins(self, canonical_artifact_id: int) -> tuple[CandidateOrigin, ...]:
        """Read the complete durable Phase-1 candidate/source handoff."""
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT f.id,f.torrent_id,f.request_id,f.candidates FROM download_files f
                    WHERE f.id=? OR (f.mirror_group_id=? AND f.mirror_state='standby')
                    ORDER BY CASE WHEN f.id=? THEN 0 ELSE 1 END,f.id""",
                (canonical_artifact_id, canonical_artifact_id, canonical_artifact_id),
            )
            result = []
            seen = set()
            for row in rows:
                if not row.get("request_id"):
                    continue
                request_row = await db.fetchone("SELECT * FROM transfer_requests WHERE id=?", (row["request_id"],))
                if not request_row:
                    continue
                record = self._record(request_row)
                for candidate in tuple(codec.candidate(item) for item in codec.load(row.get("candidates"), [])):
                    key = (record.id, str(candidate.id))
                    if key in seen:
                        continue
                    origin = await self._origin_attempt(db, record.id, candidate)
                    if origin is None:
                        continue
                    attempt_id, provider_id, source = origin
                    result.append(CandidateOrigin(
                        canonical_artifact_id, int(row["id"]), int(row["torrent_id"]), record,
                        attempt_id, str(candidate.id), provider_id, source,
                    ))
                    seen.add(key)
        return tuple(result)

    async def refresh_candidate(self, artifact: Artifact, origin: CandidateOrigin,
                                old_candidate: TransferCandidate, replacements) -> bool:
        """Replace a candidate while preserving the request that actually produced it."""
        replacements = tuple(replacements)
        if not replacements:
            return False
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            current = await db.fetchone(
                """SELECT f.* FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                    WHERE f.id=? AND COALESCE(f.mirror_state,'')!='standby'
                    AND f.status NOT IN ('completed','cancelled','error','duplicate')
                    AND t.status NOT IN ('completed','deleted','cancelled','error')""",
                (artifact.id,),
            )
            holder = await db.fetchone(
                "SELECT * FROM download_files WHERE id=? AND request_id=?",
                (origin.contributing_artifact_id, origin.request.id),
            )
            if not current or not holder:
                await db.rollback()
                return False
            old_id = str(old_candidate.id)
            canonical_candidates = [codec.candidate(item) for item in codec.load(current["candidates"], [])]
            holder_candidates = [codec.candidate(item) for item in codec.load(holder["candidates"], [])]
            try:
                canonical_index = next(i for i, item in enumerate(canonical_candidates) if str(item.id) == old_id)
                holder_index = next(i for i, item in enumerate(holder_candidates) if str(item.id) == old_id)
            except StopIteration:
                await db.rollback()
                return False
            for candidate in replacements:
                if await self._origin_attempt(db, origin.request.id, candidate) is None:
                    await db.rollback()
                    return False
            canonical_candidates[canonical_index:canonical_index + 1] = replacements
            holder_candidates[holder_index:holder_index + 1] = replacements
            await db.execute(
                "UPDATE download_files SET candidates=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (codec.dump(tuple(canonical_candidates)), artifact.id),
            )
            if int(holder["id"]) != int(artifact.id):
                await db.execute(
                    "UPDATE download_files SET candidates=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (codec.dump(tuple(holder_candidates)), holder["id"]),
                )
            await db.execute(
                "UPDATE transfer_requests SET state='resolved',error=NULL WHERE id=?",
                (origin.request.id,),
            )
            await db.commit()
        return True
