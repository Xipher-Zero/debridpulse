"""Provider-neutral canonical artifact ownership and durable candidate provenance.

Equivalence evidence is gathered by the engine before entering this owner. This
module performs durable discovery and short SQLite ownership transactions only;
it never performs provider or executor I/O. Cross-transfer provenance is bound
through request, resolution-attempt, candidate and source identities already
persisted by the resolver. URLs, filenames, hostnames, destinations and current
provider state are never used to reconstruct historical origin.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from db.database import get_db
from transfers import codec
from transfers.models import Artifact, RequestRecord, TransferCandidate


_TERMINAL_TRANSFERS = {"completed", "consolidated", "deleted", "cancelled", "error"}


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
    """Canonical ownership, P1 migration, candidate provenance and consolidation."""

    def __init__(self, repository):
        self.repository = repository
        self._initialize_lock = asyncio.Lock()
        self._initialized = False

    @staticmethod
    def _record(row) -> RequestRecord:
        # Joined provenance rows also contain their own integer primary key.
        # request_id is the exact durable request identity and therefore wins
        # when it is present; no payload-derived reconstruction is permitted.
        identity = row.get("request_id") or row["id"]
        return RequestRecord(
            identity, int(row["transfer_id"]), codec.request(codec.load(row["payload"])), row["state"],
            row["parent_id"], codec.resource(codec.load(row["resource"])), int(row["attempts"] or 0),
            float(row["retry_at"] or 0), codec.error(row["error"]), codec.entry(codec.load(row["metadata"])),
        )

    @staticmethod
    def _artifact(row) -> Artifact:
        return Artifact(
            int(row["id"]), int(row["torrent_id"]), row["request_id"], row["filename"], row["local_path"],
            int(row["size_bytes"] or 0), row["status"],
            tuple(codec.candidate(item) for item in codec.load(row["candidates"], [])),
            int(row["selected_candidate"] or 0), codec.handle(codec.load(row.get("handle"))),
            int(row["retry_count"] or 0), float(row["retry_at"] or 0), codec.error(row["normalized_error"]),
        )

    @staticmethod
    def _source_parts(source) -> tuple[str | None, str | None]:
        if not isinstance(source, dict):
            return None, None
        scope = str(source.get("scope") or "").strip()
        key = str(source.get("key") or "").strip()
        return (scope, key) if scope and key else (None, None)

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

    @staticmethod
    async def _binding_for(db, canonical_artifact_id: int, candidate: TransferCandidate, source=None):
        scope, key = CanonicalOwnership._source_parts(source)
        if scope is not None:
            row = await db.fetchone(
                """SELECT * FROM canonical_candidate_bindings
                    WHERE canonical_artifact_id=? AND provider_id=? AND source_scope=? AND source_key=?""",
                (canonical_artifact_id, str(candidate.provider_id or ""), scope, key),
            )
            if row:
                return row
        return await db.fetchone(
            "SELECT * FROM canonical_candidate_bindings WHERE canonical_artifact_id=? AND candidate_id=?",
            (canonical_artifact_id, str(candidate.id)),
        )

    @staticmethod
    async def _insert_origin(db, binding_id: int, origin: CandidateOrigin) -> None:
        await db.execute(
            """INSERT OR IGNORE INTO canonical_candidate_origins(
                binding_id,contributing_artifact_id,contributing_transfer_id,request_id,resolution_attempt_id,discovered_candidate_id)
                VALUES(?,?,?,?,?,?)""",
            (binding_id, origin.contributing_artifact_id, origin.contributing_transfer_id, origin.request.id,
             origin.resolution_attempt_id, origin.candidate_id),
        )

    async def _ensure_binding(self, db, canonical_artifact_id: int, canonical_transfer_id: int,
                              candidate: TransferCandidate, origin: CandidateOrigin, candidate_order: int):
        binding = await self._binding_for(db, canonical_artifact_id, candidate, origin.source)
        if binding:
            await self._insert_origin(db, int(binding["id"]), origin)
            return binding, False
        scope, key = self._source_parts(origin.source)
        role = "canonical" if origin.contributing_transfer_id == canonical_transfer_id else "alternate"
        binding_id = await db.execute_returning_id(
            """INSERT INTO canonical_candidate_bindings(
                canonical_artifact_id,candidate_id,provider_id,source_scope,source_key,role,candidate_order)
                VALUES(?,?,?,?,?,?,?)""",
            (canonical_artifact_id, str(candidate.id), str(origin.provider_id or candidate.provider_id or ""),
             scope, key, role, candidate_order),
        )
        await self._insert_origin(db, int(binding_id), origin)
        return await db.fetchone("SELECT * FROM canonical_candidate_bindings WHERE id=?", (binding_id,)), True

    async def _p1_origin_for_candidate(self, db, canonical_artifact_id: int, candidate: TransferCandidate):
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

    @staticmethod
    async def _full_consolidation(db, transfer_id: int) -> bool:
        leaves = await db.fetchall(
            """SELECT r.id,r.state FROM transfer_requests r WHERE r.transfer_id=?
                AND NOT EXISTS(SELECT 1 FROM transfer_requests child WHERE child.parent_id=r.id)
                ORDER BY r.ordinal,r.id""",
            (transfer_id,),
        )
        material = 0
        for request in leaves:
            if request["state"] == "skipped":
                continue
            artifact = await db.fetchone(
                "SELECT id,blocked,mirror_state FROM download_files WHERE request_id=?",
                (request["id"],),
            )
            if artifact and bool(artifact.get("blocked")):
                continue
            material += 1
            if not await db.fetchone(
                "SELECT contributing_artifact_id FROM artifact_consolidations WHERE source_request_id=?",
                (request["id"],),
            ):
                return False
        return material > 0

    @classmethod
    async def _finalize_transfer(cls, db, transfer_id: int) -> bool:
        if not await cls._full_consolidation(db, transfer_id):
            return False
        row = await db.fetchone("SELECT status FROM torrents WHERE id=?", (transfer_id,))
        if not row:
            return False
        if row["status"] == "consolidated":
            return True
        if row["status"] in {"completed", "deleted", "cancelled"}:
            return False
        await db.execute(
            """UPDATE torrents SET status='consolidated',progress=100,normalized_error=NULL,error_message=NULL,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (transfer_id,),
        )
        await db.execute(
            "INSERT INTO events(torrent_id,level,message) VALUES(?,'info','Transfer consolidated into canonical artifacts')",
            (transfer_id,),
        )
        await db.execute(
            "INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,'consolidated',NULL)",
            (transfer_id,),
        )
        return True

    async def initialize(self) -> None:
        """Losslessly formalize the Phase-1 durable origin handoff."""
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            async with get_db() as db:
                await db.execute("BEGIN IMMEDIATE")
                primaries = await db.fetchall(
                    """SELECT f.* FROM download_files f
                        WHERE f.request_id IS NOT NULL AND COALESCE(f.mirror_state,'')!='standby'
                        AND (f.mirror_group_id IS NULL OR f.mirror_group_id=f.id)
                        ORDER BY f.torrent_id,f.id"""
                )
                for row in primaries:
                    candidates = tuple(codec.candidate(item) for item in codec.load(row.get("candidates"), []))
                    next_order = 1
                    for candidate in candidates:
                        origin = await self._p1_origin_for_candidate(db, int(row["id"]), candidate)
                        if origin is None:
                            continue
                        binding = await self._binding_for(db, int(row["id"]), candidate, origin.source)
                        if binding:
                            await self._insert_origin(db, int(binding["id"]), origin)
                            next_order = max(next_order, int(binding["candidate_order"]) + 1)
                            continue
                        await self._ensure_binding(
                            db, int(row["id"]), int(row["torrent_id"]), candidate, origin, next_order,
                        )
                        next_order += 1

                standbys = await db.fetchall(
                    """SELECT f.* FROM download_files f
                        JOIN download_files c ON c.id=f.mirror_group_id
                        WHERE f.mirror_state='standby' AND f.request_id IS NOT NULL
                        ORDER BY f.torrent_id,f.id"""
                )
                affected = set()
                for standby in standbys:
                    canonical_id = int(standby["mirror_group_id"])
                    canonical = await db.fetchone("SELECT torrent_id FROM download_files WHERE id=?", (canonical_id,))
                    if not canonical or int(canonical["torrent_id"]) == int(standby["torrent_id"]):
                        continue
                    candidates = tuple(codec.candidate(item) for item in codec.load(standby.get("candidates"), []))
                    valid_origin = False
                    for candidate in candidates:
                        origin = await self._p1_origin_for_candidate(db, canonical_id, candidate)
                        if origin and origin.contributing_artifact_id == int(standby["id"]):
                            valid_origin = True
                            break
                    if not valid_origin:
                        continue
                    await db.execute(
                        """INSERT OR IGNORE INTO artifact_consolidations(
                            contributing_artifact_id,source_transfer_id,source_request_id,canonical_artifact_id)
                            VALUES(?,?,?,?)""",
                        (int(standby["id"]), int(standby["torrent_id"]), standby["request_id"], canonical_id),
                    )
                    affected.add(int(standby["torrent_id"]))

                await db.execute(
                    """UPDATE execution_attempt_provenance AS e SET route_attempt_id=(
                        SELECT o.resolution_attempt_id FROM canonical_candidate_bindings b
                        JOIN canonical_candidate_origins o ON o.binding_id=b.id
                        WHERE b.canonical_artifact_id=e.artifact_id AND b.candidate_id=e.candidate_id
                        ORDER BY CASE WHEN o.discovered_candidate_id=b.candidate_id THEN 0 ELSE 1 END,o.id LIMIT 1)
                        WHERE e.route_attempt_id IS NULL AND e.candidate_id IS NOT NULL
                        AND EXISTS(SELECT 1 FROM canonical_candidate_bindings b
                            WHERE b.canonical_artifact_id=e.artifact_id AND b.candidate_id=e.candidate_id)"""
                )
                for transfer_id in sorted(affected):
                    await self._finalize_transfer(db, transfer_id)
                await db.commit()
            self._initialized = True

    async def canonical_artifacts(self) -> tuple[Artifact, ...]:
        await self.initialize()
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT f.*,e.handle FROM download_files f
                    JOIN torrents t ON t.id=f.torrent_id
                    LEFT JOIN execution_attempts e ON e.id=f.execution_attempt_id
                    WHERE f.request_id IS NOT NULL AND COALESCE(f.blocked,0)=0
                    AND COALESCE(f.mirror_state,'')!='standby'
                    AND (f.mirror_group_id IS NULL OR f.mirror_group_id=f.id)
                    AND f.status NOT IN ('completed','cancelled','error','duplicate')
                    AND t.status NOT IN ('completed','consolidated','deleted','cancelled','error')
                    ORDER BY f.torrent_id,f.id"""
            )
        return tuple(self._artifact(row) for row in rows)

    async def lower_materializing(self, record: RequestRecord):
        await self.initialize()
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
                    WHERE r.id!=? AND r.transfer_id!=? AND r.state='materializing'
                    AND t.status NOT IN ('completed','consolidated','deleted','cancelled','error')
                    ORDER BY r.transfer_id,r.ordinal,r.rowid,r.id""",
                (record.id, record.transfer_id),
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
        """Atomically revalidate an established owner and attach one source."""
        await self.initialize()
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
                    AND t.status NOT IN ('completed','consolidated','deleted','cancelled','error')""",
                (primary.id,),
            )
            incoming = await db.fetchone(
                """SELECT r.id FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id
                    WHERE r.id=? AND r.transfer_id=? AND r.state='materializing'
                    AND t.status NOT IN ('completed','consolidated','deleted','cancelled','error')""",
                (record.id, record.transfer_id),
            )
            if not current or not incoming:
                await db.rollback()
                return False

            origin_meta = []
            for candidate in alternatives:
                origin = await self._origin_attempt(db, record.id, candidate)
                if origin is None:
                    await db.rollback()
                    return False
                origin_meta.append(origin)

            retained = [replace(codec.candidate(item), expected_bytes=size)
                        for item in codec.load(current["candidates"], [])]
            # A primary established after engine initialization may not yet have
            # passed through the P1 migration scan. Formalize its exact route
            # before adding a foreign alternate so ordering begins at canonical 1.
            for order, candidate in enumerate(retained, start=1):
                existing_binding = await self._binding_for(db, int(primary.id), candidate)
                if existing_binding:
                    continue
                origin = await self._p1_origin_for_candidate(db, int(primary.id), candidate)
                if origin is not None:
                    await self._ensure_binding(
                        db, int(primary.id), int(current["torrent_id"]), candidate, origin, order,
                    )

            standby = await db.fetchone("SELECT * FROM download_files WHERE request_id=?", (record.id,))
            if standby and not (
                    standby.get("mirror_state") == "standby"
                    and int(standby.get("mirror_group_id") or 0) == int(primary.id)):
                await db.rollback()
                return False

            if standby:
                standby_id = int(standby["id"])
                await db.execute(
                    """UPDATE download_files SET torrent_id=?,filename=?,size_bytes=?,local_path=?,status='duplicate',blocked=NULL,
                        mirror_group_id=?,mirror_state='standby',candidates=?,download_client='',normalized_error=NULL,
                        updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (record.transfer_id, alternatives[0].name, size, primary.target, primary.id,
                     codec.dump(alternatives), standby_id),
                )
            else:
                standby_id = int(await db.execute_returning_id(
                    """INSERT INTO download_files(torrent_id,request_id,filename,size_bytes,local_path,status,blocked,
                        mirror_group_id,mirror_state,candidates,download_client)
                        VALUES(?,?,?,?,?,'duplicate',NULL,?,'standby',?,'')""",
                    (record.transfer_id, record.id, alternatives[0].name, size, primary.target,
                     primary.id, codec.dump(alternatives)),
                ))

            for candidate, meta in zip(alternatives, origin_meta):
                attempt_id, provider_id, source = meta
                request_row = await db.fetchone("SELECT * FROM transfer_requests WHERE id=?", (record.id,))
                origin = CandidateOrigin(
                    int(primary.id), standby_id, int(record.transfer_id), self._record(request_row),
                    attempt_id, str(candidate.id), provider_id, source,
                )
                binding = await self._binding_for(db, int(primary.id), candidate, source)
                if binding:
                    await self._insert_origin(db, int(binding["id"]), origin)
                    continue
                retained.append(candidate)
                await self._ensure_binding(
                    db, int(primary.id), int(current["torrent_id"]), candidate, origin, len(retained),
                )

            await db.execute(
                """UPDATE download_files SET candidates=?,size_bytes=?,mirror_group_id=?,mirror_state='primary',
                    updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (codec.dump(retained), size, primary.id, primary.id),
            )
            cursor = await db.execute(
                "UPDATE transfer_requests SET state='resolved',error=NULL WHERE id=? AND transfer_id=? AND state='materializing'",
                (record.id, record.transfer_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            if int(record.transfer_id) != int(current["torrent_id"]):
                await db.execute(
                    """INSERT INTO artifact_consolidations(
                        contributing_artifact_id,source_transfer_id,source_request_id,canonical_artifact_id)
                        VALUES(?,?,?,?) ON CONFLICT(contributing_artifact_id) DO UPDATE SET
                        source_transfer_id=excluded.source_transfer_id,source_request_id=excluded.source_request_id,
                        canonical_artifact_id=excluded.canonical_artifact_id,updated_at=CURRENT_TIMESTAMP""",
                    (standby_id, record.transfer_id, record.id, primary.id),
                )
                await self._finalize_transfer(db, int(record.transfer_id))
            await db.commit()
        return True

    async def _bound_origin(self, db, canonical_artifact_id: int, candidate: TransferCandidate):
        binding = await db.fetchone(
            "SELECT * FROM canonical_candidate_bindings WHERE canonical_artifact_id=? AND candidate_id=?",
            (canonical_artifact_id, str(candidate.id)),
        )
        if not binding:
            return None
        origin = await db.fetchone(
            """SELECT o.*,r.* FROM canonical_candidate_origins o
                JOIN transfer_requests r ON r.id=o.request_id WHERE o.binding_id=?
                ORDER BY CASE WHEN o.discovered_candidate_id=? THEN 0 ELSE 1 END,o.id LIMIT 1""",
            (binding["id"], binding["candidate_id"]),
        )
        if not origin:
            return None
        source = None
        if binding.get("source_scope") and binding.get("source_key"):
            source = {"scope": binding["source_scope"], "key": binding["source_key"]}
        return CandidateOrigin(
            canonical_artifact_id, int(origin["contributing_artifact_id"]), int(origin["contributing_transfer_id"]),
            self._record(origin), str(origin["resolution_attempt_id"]), str(binding["candidate_id"]),
            str(binding["provider_id"]), source,
        )

    async def origin_for(self, artifact: Artifact, candidate: TransferCandidate) -> CandidateOrigin | None:
        await self.initialize()
        async with get_db() as db:
            bound = await self._bound_origin(db, artifact.id, candidate)
            if bound is not None:
                return bound
            await db.execute("BEGIN IMMEDIATE")
            current = await db.fetchone(
                """SELECT f.torrent_id,f.candidates FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                    WHERE f.id=? AND f.request_id IS NOT NULL AND COALESCE(f.mirror_state,'')!='standby'
                    AND f.status NOT IN ('completed','cancelled','error','duplicate')
                    AND t.status NOT IN ('completed','consolidated','deleted','cancelled','error')""",
                (artifact.id,),
            )
            if not current:
                await db.rollback()
                return None
            stored = tuple(codec.candidate(item) for item in codec.load(current.get("candidates"), []))
            order = next((index for index, item in enumerate(stored, start=1)
                          if str(item.id) == str(candidate.id)), None)
            if order is None:
                await db.rollback()
                return None
            exact = await self._p1_origin_for_candidate(db, artifact.id, candidate)
            if exact is None:
                await db.rollback()
                return None
            occupied = await db.fetchone(
                "SELECT candidate_id FROM canonical_candidate_bindings WHERE canonical_artifact_id=? AND candidate_order=?",
                (artifact.id, order),
            )
            if occupied and str(occupied["candidate_id"]) != str(candidate.id):
                await db.rollback()
                return None
            scope, key = self._source_parts(exact.source)
            binding_id = await db.execute_returning_id(
                """INSERT OR IGNORE INTO canonical_candidate_bindings(
                    canonical_artifact_id,candidate_id,provider_id,source_scope,source_key,role,candidate_order)
                    VALUES(?,?,?,?,?,'canonical',?)""",
                (artifact.id, str(candidate.id), str(exact.provider_id or candidate.provider_id or ""), scope, key, order),
            )
            binding = await db.fetchone(
                "SELECT id FROM canonical_candidate_bindings WHERE canonical_artifact_id=? AND candidate_id=?",
                (artifact.id, str(candidate.id)),
            )
            if not binding:
                await db.rollback()
                return None
            await self._insert_origin(db, int(binding["id"]), exact)
            rebound = await self._bound_origin(db, artifact.id, candidate)
            await db.commit()
            return rebound

    async def origins(self, canonical_artifact_id: int) -> tuple[CandidateOrigin, ...]:
        await self.initialize()
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT b.candidate_id,b.provider_id,b.source_scope,b.source_key,o.*,r.*
                    FROM canonical_candidate_bindings b
                    JOIN canonical_candidate_origins o ON o.binding_id=b.id
                    JOIN transfer_requests r ON r.id=o.request_id
                    WHERE b.canonical_artifact_id=?
                    ORDER BY b.candidate_order,o.id""",
                (canonical_artifact_id,),
            )
        result = []
        for row in rows:
            source = None
            if row.get("source_scope") and row.get("source_key"):
                source = {"scope": row["source_scope"], "key": row["source_key"]}
            result.append(CandidateOrigin(
                canonical_artifact_id, int(row["contributing_artifact_id"]), int(row["contributing_transfer_id"]),
                self._record(row), str(row["resolution_attempt_id"]), str(row["candidate_id"]),
                str(row["provider_id"]), source,
            ))
        return tuple(result)

    async def bindings(self, canonical_artifact_id: int) -> tuple[dict, ...]:
        await self.initialize()
        async with get_db() as db:
            bindings = await db.fetchall(
                """SELECT * FROM canonical_candidate_bindings WHERE canonical_artifact_id=?
                    ORDER BY candidate_order,id""",
                (canonical_artifact_id,),
            )
            result = []
            for binding in bindings:
                origins = await db.fetchall(
                    """SELECT contributing_artifact_id,contributing_transfer_id,request_id,resolution_attempt_id,
                        discovered_candidate_id FROM canonical_candidate_origins WHERE binding_id=? ORDER BY id""",
                    (binding["id"],),
                )
                source = None
                if binding.get("source_scope") and binding.get("source_key"):
                    source = {"scope": binding["source_scope"], "key": binding["source_key"]}
                result.append({
                    "candidate_id": binding["candidate_id"],
                    "provider_id": binding["provider_id"],
                    "source_identity": source,
                    "role": binding["role"],
                    "candidate_order": int(binding["candidate_order"]),
                    "origins": [dict(item) for item in origins],
                })
        return tuple(result)

    async def consolidation(self, transfer_id: int) -> dict:
        await self.initialize()
        async with get_db() as db:
            transfer = await db.fetchone("SELECT status FROM torrents WHERE id=?", (transfer_id,))
            rows = await db.fetchall(
                """SELECT a.contributing_artifact_id,a.source_request_id,a.canonical_artifact_id,
                    c.torrent_id AS canonical_transfer_id
                    FROM artifact_consolidations a JOIN download_files c ON c.id=a.canonical_artifact_id
                    WHERE a.source_transfer_id=? ORDER BY a.contributing_artifact_id""",
                (transfer_id,),
            )
        targets = sorted({int(row["canonical_transfer_id"]) for row in rows})
        complete = bool(transfer and transfer["status"] == "consolidated")
        return {
            "state": "complete" if complete else "partial" if rows else "none",
            "consolidated_into": targets[0] if complete and len(targets) == 1 else None,
            "canonical_transfer_ids": targets,
            "artifact_mappings": [dict(row) for row in rows],
        }

    @staticmethod
    async def _move_origins(db, source_binding_id: int, target_binding_id: int) -> None:
        rows = await db.fetchall("SELECT * FROM canonical_candidate_origins WHERE binding_id=? ORDER BY id", (source_binding_id,))
        for row in rows:
            await db.execute(
                """INSERT OR IGNORE INTO canonical_candidate_origins(
                    binding_id,contributing_artifact_id,contributing_transfer_id,request_id,resolution_attempt_id,discovered_candidate_id)
                    VALUES(?,?,?,?,?,?)""",
                (target_binding_id, row["contributing_artifact_id"], row["contributing_transfer_id"], row["request_id"],
                 row["resolution_attempt_id"], row["discovered_candidate_id"]),
            )
        await db.execute("DELETE FROM canonical_candidate_origins WHERE binding_id=?", (source_binding_id,))
        await db.execute("DELETE FROM canonical_candidate_bindings WHERE id=?", (source_binding_id,))

    async def refresh_candidate(self, artifact: Artifact, origin: CandidateOrigin,
                                old_candidate: TransferCandidate, replacements) -> bool:
        await self.initialize()
        replacements = tuple(replacements)
        if not replacements:
            return False
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            current = await db.fetchone(
                """SELECT f.* FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                    WHERE f.id=? AND COALESCE(f.mirror_state,'')!='standby'
                    AND f.status NOT IN ('completed','cancelled','error','duplicate')
                    AND t.status NOT IN ('completed','consolidated','deleted','cancelled','error')""",
                (artifact.id,),
            )
            holder = await db.fetchone(
                "SELECT * FROM download_files WHERE id=? AND request_id=?",
                (origin.contributing_artifact_id, origin.request.id),
            )
            old_binding = await db.fetchone(
                "SELECT * FROM canonical_candidate_bindings WHERE canonical_artifact_id=? AND candidate_id=?",
                (artifact.id, str(old_candidate.id)),
            )
            if not current or not holder or not old_binding:
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

            metadata = []
            for candidate in replacements:
                meta = await self._origin_attempt(db, origin.request.id, candidate)
                if meta is None:
                    await db.rollback()
                    return False
                metadata.append(meta)

            accepted = []
            primary_binding_id = int(old_binding["id"])
            first = True
            for candidate, meta in zip(replacements, metadata):
                attempt_id, provider_id, source = meta
                existing = await self._binding_for(db, artifact.id, candidate, source)
                if existing and int(existing["id"]) != primary_binding_id:
                    if first:
                        await self._move_origins(db, primary_binding_id, int(existing["id"]))
                        primary_binding_id = int(existing["id"])
                        first = False
                    replacement_origin = CandidateOrigin(
                        artifact.id, origin.contributing_artifact_id, origin.contributing_transfer_id, origin.request,
                        attempt_id, str(candidate.id), provider_id, source,
                    )
                    await self._insert_origin(db, int(existing["id"]), replacement_origin)
                    continue

                scope, key = self._source_parts(source)
                replacement_origin = CandidateOrigin(
                    artifact.id, origin.contributing_artifact_id, origin.contributing_transfer_id, origin.request,
                    attempt_id, str(candidate.id), provider_id, source,
                )
                if first:
                    await db.execute(
                        """UPDATE canonical_candidate_bindings SET candidate_id=?,provider_id=?,source_scope=?,source_key=?,
                            updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (str(candidate.id), str(provider_id or candidate.provider_id or ""), scope, key, primary_binding_id),
                    )
                    await self._insert_origin(db, primary_binding_id, replacement_origin)
                    accepted.append(candidate)
                    first = False
                else:
                    max_order = await db.fetchone(
                        "SELECT COALESCE(MAX(candidate_order),0) AS n FROM canonical_candidate_bindings WHERE canonical_artifact_id=?",
                        (artifact.id,),
                    )
                    new_id = int(await db.execute_returning_id(
                        """INSERT INTO canonical_candidate_bindings(
                            canonical_artifact_id,candidate_id,provider_id,source_scope,source_key,role,candidate_order)
                            VALUES(?,?,?,?,?,?,?)""",
                        (artifact.id, str(candidate.id), str(provider_id or candidate.provider_id or ""), scope, key,
                         old_binding["role"], int(max_order["n"] or 0) + 1),
                    ))
                    await self._insert_origin(db, new_id, replacement_origin)
                    accepted.append(candidate)

            if not accepted:
                canonical_candidates.pop(canonical_index)
            else:
                canonical_candidates[canonical_index:canonical_index + 1] = accepted
            holder_candidates[holder_index:holder_index + 1] = list(replacements)
            if not canonical_candidates:
                await db.rollback()
                return False

            await db.execute(
                "UPDATE canonical_candidate_bindings SET candidate_order=candidate_order+100000 WHERE canonical_artifact_id=?",
                (artifact.id,),
            )
            ordered = []
            for candidate in canonical_candidates:
                binding = await db.fetchone(
                    "SELECT id FROM canonical_candidate_bindings WHERE canonical_artifact_id=? AND candidate_id=?",
                    (artifact.id, str(candidate.id)),
                )
                if not binding:
                    continue
                await db.execute(
                    "UPDATE canonical_candidate_bindings SET candidate_order=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (len(ordered) + 1, binding["id"]),
                )
                ordered.append(candidate)
            await db.execute(
                "UPDATE download_files SET candidates=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (codec.dump(tuple(ordered)), artifact.id),
            )
            if int(holder["id"]) != int(artifact.id):
                await db.execute(
                    "UPDATE download_files SET candidates=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (codec.dump(tuple(holder_candidates)), holder["id"]),
                )
            await db.execute("UPDATE transfer_requests SET state='resolved',error=NULL WHERE id=?", (origin.request.id,))
            await db.commit()
        return True
