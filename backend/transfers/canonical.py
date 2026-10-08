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
from transfers._repository_base import (
    _durable_canonical_targets_for_request, _logical_slot_key_for_artifact, _logical_slot_key_for_request,
    _retire_transfer_auxiliary_state_in_db, terminal_unverified_association,
)
from transfers.cohorts import (
    _FAILED_CONTRIBUTION_DISPOSITION, _PROVEN_DISTINCT_DISPOSITIONS, _UNVERIFIED_DISPOSITION,
)
from transfers.models import Artifact, ArtifactFingerprint, FingerprintKind, RequestRecord, SizeKnowledge, TransferCandidate
from transfers.policy import SIDE_STATE_RETIRING_TRANSFER_STATES, dead_source
from transfers.size_evidence import positive_size, reported_sizes_compatible


_SETTLED_TRANSFER_STATES = "(" + ",".join(
    f"'{state.value}'" for state in sorted(SIDE_STATE_RETIRING_TRANSFER_STATES, key=lambda state: state.value)) + ")"

# One material owner row: the head of its mirror group, never a standby.
_MATERIAL_OWNER = """f.request_id IS NOT NULL AND COALESCE(f.blocked,0)=0
    AND COALESCE(f.mirror_state,'')!='standby' AND (f.mirror_group_id IS NULL OR f.mirror_group_id=f.id)"""
# A live owner: its writer may still run, so an equivalent source joins it.
_LIVE_OWNER = """f.status NOT IN ('completed','cancelled','error','duplicate')
    AND t.status NOT IN ('completed','consolidated','deleted','cancelled','error')"""
# A completed owner whose material ownership is frozen but still valid.
_FROZEN_OWNER = "f.status='completed' AND t.status NOT IN ('deleted','cancelled')"


@dataclass(frozen=True)
class CollectionInversion:
    """One member whose canonical ownership is inverted against its collection
    owner: ``canonical`` belongs to a later transfer and carries a contributing
    standby (``contributor_id``) of ``record`` -- the collection owner's own
    request for the same member, whose own routes are ``candidates``."""
    canonical: Artifact
    contributor_id: int
    record: RequestRecord
    candidates: tuple[TransferCandidate, ...]


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

    def __init__(self, repository, *, on_attached=None, material_present=None):
        self.repository = repository
        # Injected by the composition root: awaited with the source transfer id
        # only after ``attach`` has durably committed (never on a refusal or a
        # rollback), so a semantic event can only follow a real consolidation.
        self.on_attached = on_attached
        # Injected by the engine: THE current material verification of a
        # completed artifact (``_engine_base.TransferEngine._delivered_paths``,
        # the delivery-time re-verification). A completed row is history; only
        # material that is present now may satisfy anything.
        self.material_present = material_present
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
            codec.optional_request(codec.load(row.get("interpretation"))),
        )

    @staticmethod
    def _artifact(row) -> Artifact:
        return Artifact(
            int(row["id"]), int(row["torrent_id"]), row["request_id"], row["filename"], row["local_path"],
            int(row["size_bytes"] or 0), row["status"],
            tuple(codec.candidate(item) for item in codec.load(row["candidates"], [])),
            int(row["selected_candidate"] or 0), codec.handle(codec.load(row.get("handle"))),
            int(row["retry_count"] or 0), float(row["retry_at"] or 0), codec.error(row["normalized_error"]),
            # FUNC-001: every loader reconstructs the size fact through the one
            # canonical interpretation, so no reader can observe a different
            # answer depending on which query produced the row.
            SizeKnowledge.durable(row["size_bytes"], row["size_knowledge"]),
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
            if request["state"] == "failed" and await CanonicalOwnership._failed_contribution(db, request["id"]):
                # A dead source associated with a live canonical artifact
                # owes no material work and is settled -- as history, never
                # as membership (no binding, origin or consolidation row).
                continue
            artifact = await db.fetchone(
                "SELECT id,blocked,mirror_state FROM download_files WHERE request_id=?",
                (request["id"],),
            )
            if artifact and bool(artifact.get("blocked")):
                continue
            material += 1
            if await db.fetchone(
                "SELECT contributing_artifact_id FROM artifact_consolidations WHERE source_request_id=?",
                (request["id"],),
            ):
                continue
            # A terminal UNVERIFIED association (``transfers.cohorts``) to a
            # canonical artifact owned by ANOTHER transfer leaves this leaf
            # with no writer-capable work: no writer may ever be created for
            # it, and the object it plausibly is already has its one canonical
            # writer elsewhere. That settles the leaf; it does not make it a
            # canonical member -- nothing here (or anywhere) derives a binding,
            # origin or consolidation row from it.
            if not await terminal_unverified_association(db, request["id"]):
                return False
        return material > 0

    @staticmethod
    async def _failed_contribution(db, request_id: str) -> bool:
        return bool(await db.fetchone(
            """SELECT 1 AS ok FROM transfer_requests r JOIN download_files f ON f.id=r.equivalence_target_artifact_id
                WHERE r.id=? AND r.state='failed' AND r.equivalence_disposition=?""",
            (request_id, _FAILED_CONTRIBUTION_DISPOSITION)))

    @classmethod
    async def _associate_failed_contributions(cls, db, transfer_id: int) -> int:
        """Associate this transfer's DEAD roots with the canonical artifact
        their own submission cohort was proven to be; returns how many.

        Hygiene, never equivalence, and conservative by construction. Inside
        one same-transfer material cohort (the leaves of one parent -- one
        submission's roots, or one manifest's members), a leaf is associated
        only when ALL of these durable facts hold:

        * its resolution ended for good on a dead route (``policy.dead_source``)
          and it never produced an artifact of its own;
        * EVERY other member of the cohort is already decided, and all of them
          name the SAME canonical artifact: proven into it (consolidated, or
          owning it), held unverified against it, or already a failed
          contribution to it -- at least one of them proven. An undecided
          member (still resolving or proving, or failed on anything but a dead
          route) leaves the question open, and a member proven distinct -- or
          owning a second artifact -- closes it;
        * that artifact has not failed and its transfer is not withdrawn;
        * its declared logical identity is that artifact's (same non-empty
          logical key; a size it declared, when known, is compatible).

        The request stays FAILED with its exact error, becomes no candidate,
        binding, origin, consolidation row or writer, and only records the
        association (``failed_contribution``, the target, and the failure
        category as the reason) so history can show it and lifecycle voting
        can stop treating it as this transfer's own unmet obligation."""
        rows = await db.fetchall("SELECT * FROM transfer_requests WHERE transfer_id=? ORDER BY ordinal,rowid",
                                 (int(transfer_id),))
        parents = {row["parent_id"] for row in rows if row["parent_id"]}
        cohorts: dict[object, list] = {}
        for row in rows:
            if row["id"] not in parents and row["state"] != "skipped":
                cohorts.setdefault(row["parent_id"], []).append(row)
        associated = 0
        for cohort in cohorts.values():
            dead = [row for row in cohort if row["state"] == "failed" and not row["equivalence_disposition"]
                    and dead_source(codec.error(row["error"]))]
            if not dead or any(str(row["equivalence_disposition"] or "") in _PROVEN_DISTINCT_DISPOSITIONS
                               for row in cohort):
                continue
            targets, proven, undecided = set(), 0, False
            for row in cohort:
                if row in dead:
                    continue
                disposition = str(row["equivalence_disposition"] or "")
                consolidated = await db.fetchone(
                    "SELECT canonical_artifact_id FROM artifact_consolidations WHERE source_request_id=?", (row["id"],))
                owned = None if consolidated else await db.fetchone(
                    """SELECT id FROM download_files WHERE request_id=? AND COALESCE(blocked,0)=0
                        AND COALESCE(mirror_state,'')!='standby' AND (mirror_group_id IS NULL OR mirror_group_id=id)""",
                    (row["id"],))
                if consolidated or owned:
                    targets.add(int(consolidated["canonical_artifact_id"] if consolidated else owned["id"]))
                    proven += 1
                elif (disposition in {_UNVERIFIED_DISPOSITION, _FAILED_CONTRIBUTION_DISPOSITION}
                      and row["equivalence_target_artifact_id"] is not None
                      and row["state"] in {"materializing", "failed"}):
                    targets.add(int(row["equivalence_target_artifact_id"]))
                else:
                    undecided = True
            if undecided or not proven or len(targets) != 1:
                continue
            target = await db.fetchone(
                """SELECT f.* FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                    WHERE f.id=? AND f.status NOT IN ('error','cancelled') AND t.status NOT IN ('deleted','cancelled')""",
                (next(iter(targets)),))
            if not target:
                continue
            artifact = cls._artifact(target)
            key = _logical_slot_key_for_artifact(artifact)
            known = positive_size(artifact.expected_bytes)
            for row in dead:
                record = cls._record(row)
                declared = positive_size(record.entry.expected_bytes) if record.entry is not None else None
                if (not key or _logical_slot_key_for_request(record) != key
                        or (known is not None and declared is not None
                            and not reported_sizes_compatible(known, declared))):
                    continue
                cursor = await db.execute(
                    """UPDATE transfer_requests SET equivalence_disposition=?,equivalence_target_artifact_id=?,
                        equivalence_reason=? WHERE id=? AND state='failed' AND COALESCE(equivalence_disposition,'')=''""",
                    (_FAILED_CONTRIBUTION_DISPOSITION, artifact.id, codec.error(row["error"]).category.value, row["id"]))
                if cursor.rowcount:
                    associated += 1
                    await db.execute("INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,?,?)",
                                     (int(transfer_id), "failed_contribution_associated", str(artifact.id)))
        return associated

    @classmethod
    async def _finalize_transfer(cls, db, transfer_id: int) -> bool:
        await cls._associate_failed_contributions(db, transfer_id)
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
        # FUNC-001: this path settles the parent into CONSOLIDATED without
        # going through TransferRepository._write_lifecycle_transition, so it
        # must invoke the same transaction-local auxiliary-state retirement.
        await _retire_transfer_auxiliary_state_in_db(db, transfer_id)
        await db.execute(
            "INSERT INTO events(torrent_id,level,message) VALUES(?,'info','Transfer consolidated into canonical artifacts')",
            (transfer_id,),
        )
        await db.execute(
            "INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,'consolidated',NULL)",
            (transfer_id,),
        )
        return True

    async def reconsidering(self) -> frozenset[int]:
        """Settled (CONSOLIDATED) contributor transfers whose unverified
        association still owes the equivalence owner work: a scheduled
        reconsideration, or an associated target that terminally failed (its
        premise is gone). Only the one resolution scheduler reads this, to
        reach that proof work without reopening the contributor for it."""
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT DISTINCT r.transfer_id FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id
                    JOIN download_files c ON c.id=r.equivalence_target_artifact_id
                    WHERE t.status='consolidated' AND r.state='materializing' AND r.equivalence_disposition=?
                    AND (COALESCE(r.retry_at,0)>0 OR c.status='error')""",
                (_UNVERIFIED_DISPOSITION,))
        return frozenset(int(row["transfer_id"]) for row in rows)

    async def reopen(self, transfer_id: int) -> bool:
        """The inverse of ``settle``: a CONSOLIDATED contributor that is no
        longer fully consolidated -- a leaf's unverified association was
        affirmatively contradicted, or its target terminally failed, so it
        owes independent work -- returns to the ordinary lifecycle (QUEUED).
        One ownership transaction, fenced on exactly the settlement predicate
        (``_full_consolidation``): a transfer that is still fully consolidated
        is never reopened. True when this call reopened it."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("SELECT status FROM torrents WHERE id=?", (int(transfer_id),))
            if not row or row["status"] != "consolidated" or await self._full_consolidation(db, int(transfer_id)):
                await db.rollback()
                return False
            await db.execute(
                """UPDATE torrents SET status='queued',normalized_error=NULL,error_message=NULL,
                    updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='consolidated'""", (int(transfer_id),))
            await db.execute(
                "INSERT INTO events(torrent_id,level,message) VALUES(?,'info',?)",
                (int(transfer_id), "A contributed source proved independent; the transfer resumes on its own"))
            await db.execute("INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,'consolidation_reopened',NULL)",
                             (int(transfer_id),))
            await db.commit()
        return True

    async def settle(self, transfer_id: int) -> bool:
        """Re-evaluate transfer settlement after a leaf reached a terminal
        non-writer disposition outside ``attach`` (a terminal ``unverified``
        association). The same ``_finalize_transfer`` decision ``attach`` runs,
        in its own short ownership transaction; True when the transfer is
        settled CONSOLIDATED."""
        await self.initialize()
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            settled = await self._finalize_transfer(db, int(transfer_id))
            await db.commit()
        return settled

    # ``candidate_order`` is a binding's position among its artifact's CURRENT
    # candidates (1..n, readers join it to ``selected_candidate+1``). A binding
    # whose candidate is no longer current keeps its row and every origin as
    # history in the non-current band above this value -- the band candidate
    # refresh and consolidation already move bindings through.
    _NON_CURRENT_ORDER = 100000

    async def _realign_current_candidates(self, db, artifact_id: int) -> tuple:
        """Inside the caller's transaction: make ``artifact_id``'s binding
        orders describe its current candidates again. Each current candidate's
        binding -- the one ``_binding_for`` resolves for it, exactly as every
        reader and the origin backfill do -- takes that candidate's position; a
        binding no current candidate resolves to leaves the active positions
        for the non-current band. Nothing is deleted, no origin moves, and an
        aligned artifact is left untouched, so repeating it changes nothing.

        Returns each current candidate with the origin resolved for it. Origin
        resolution reads no binding, so realignment never changes it: the
        caller's backfill uses these rather than resolving each again."""
        row = await db.fetchone("SELECT candidates FROM download_files WHERE id=?", (artifact_id,))
        if not row:
            return ()
        candidates = tuple(codec.candidate(item) for item in codec.load(row.get("candidates"), []))
        resolved = []
        positions: dict[int, int] = {}
        for position, candidate in enumerate(candidates, start=1):
            origin = await self._p1_origin_for_candidate(db, artifact_id, candidate)
            resolved.append((candidate, origin))
            binding = await self._binding_for(db, artifact_id, candidate, origin.source if origin else None)
            if binding and int(binding["id"]) not in positions:
                positions[int(binding["id"])] = position
        bindings = await db.fetchall(
            "SELECT id,candidate_order FROM canonical_candidate_bindings WHERE canonical_artifact_id=? "
            "ORDER BY candidate_order,id", (artifact_id,))
        misplaced = [item for item in bindings
                     if (int(item["id"]) in positions and int(item["candidate_order"]) != positions[int(item["id"])])
                     or (int(item["id"]) not in positions and int(item["candidate_order"]) <= self._NON_CURRENT_ORDER)]
        if not misplaced:
            return tuple(resolved)
        band = max([self._NON_CURRENT_ORDER, *(int(item["candidate_order"]) for item in bindings)]) + 1
        for offset, item in enumerate(misplaced):
            await db.execute("UPDATE canonical_candidate_bindings SET candidate_order=?,updated_at=CURRENT_TIMESTAMP "
                             "WHERE id=?", (band + offset, item["id"]))
        for item in misplaced:
            if int(item["id"]) in positions:
                await db.execute("UPDATE canonical_candidate_bindings SET candidate_order=?,"
                                 "updated_at=CURRENT_TIMESTAMP WHERE id=?", (positions[int(item["id"])], item["id"]))
        return tuple(resolved)

    async def realign_rebuilt(self, artifact_id: int) -> None:
        """An artifact rebuilt in place by a new acquisition generation (its
        candidates replaced, its row and coordinate kept): its earlier
        candidates' bindings stop holding current positions. The one durable
        correction, so ordinary canonical readers stay truthful."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._realign_current_candidates(db, int(artifact_id))
            await db.commit()

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
                    # A row rebuilt in place before rebuilds were realigned may
                    # still hold its earlier candidates' bindings at current
                    # positions: correct that durable state first.
                    resolved = await self._realign_current_candidates(db, int(row["id"]))
                    next_order = 1
                    for candidate, origin in resolved:
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
        """Every live canonical material owner."""
        await self.initialize()
        async with get_db() as db:
            rows = await db.fetchall(
                f"""SELECT f.*,e.handle FROM download_files f
                    JOIN torrents t ON t.id=f.torrent_id
                    LEFT JOIN execution_attempts e ON e.id=f.execution_attempt_id
                    WHERE {_MATERIAL_OWNER} AND {_LIVE_OWNER}
                    ORDER BY f.torrent_id,f.id"""
            )
        return tuple(self._artifact(row) for row in rows)

    async def equivalence_targets(self, record: RequestRecord) -> tuple[Artifact, ...]:
        """THE canonical material owners ``record`` may be proven equivalent to.

        Lifecycle decides what a match means (``attach``), never whether an
        owner is visible to identity proof: every live owner, plus every
        COMPLETED owner whose frozen material still owns what an equivalent
        member of ``record``'s transfer would need -- one in a transfer
        ``_frozen_satisfiable`` relates to it (the same transfer, an earlier
        generation of the same logical source, or one recognized collection
        with either) AND whose material is present now (``material_present``):
        a completed row alone never proves the payload still exists. Never
        ``record``'s own artifact."""
        await self.initialize()
        async with get_db() as db:
            related = await self._frozen_satisfiable(db, int(record.transfer_id))
            marks = ",".join("?" for _ in related)
            rows = await db.fetchall(
                f"""SELECT f.*,e.handle FROM download_files f
                    JOIN torrents t ON t.id=f.torrent_id
                    LEFT JOIN execution_attempts e ON e.id=f.execution_attempt_id
                    WHERE {_MATERIAL_OWNER} AND f.request_id!=? AND (({_LIVE_OWNER})
                        OR ({_FROZEN_OWNER} AND f.torrent_id IN ({marks})))
                    ORDER BY f.torrent_id,f.id""",
                (str(record.id), *related),
            )
        targets = []
        for artifact, row in ((self._artifact(row), row) for row in rows):
            if not artifact.candidates:
                continue
            if row["status"] == "completed" and not (
                    self.material_present is not None and await self.material_present(artifact)):
                continue
            targets.append(artifact)
        return tuple(targets)

    async def retain_evidence(self, candidate_id: str, evidence: ArtifactFingerprint) -> int:
        """Durably keep the neutral content evidence that proved one canonical
        member candidate, wherever that candidate is stored (the canonical row
        and any standby holder). Only the ``ArtifactFingerprint`` is written --
        never the input that acquired it. Returns the rows updated; a candidate
        that joined no artifact updates nothing."""
        if not isinstance(evidence, ArtifactFingerprint) or evidence.kind == FingerprintKind.UNAVAILABLE:
            return 0
        candidate_id = str(candidate_id)
        updated = 0
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            rows = await db.fetchall("SELECT id,candidates FROM download_files WHERE candidates LIKE ?",
                                     (f'%"{candidate_id}"%',))
            for row in rows:
                stored = tuple(codec.candidate(item) for item in codec.load(row.get("candidates"), []))
                if not any(str(item.id) == candidate_id for item in stored):
                    continue
                kept = tuple(replace(item, content_evidence=evidence) if str(item.id) == candidate_id else item
                             for item in stored)
                await db.execute("UPDATE download_files SET candidates=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                 (codec.dump(kept), row["id"]))
                updated += 1
            await db.commit()
        return updated

    async def durable_owner_for_request(self, request_id: str) -> int | None:
        """DP 1.0.12 Section 6: the one canonical artifact id, if any, that
        ``request_id``'s own candidate provenance durably attaches to.

        Same-transfer canonical convergence intentionally never writes an
        ``artifact_consolidations`` row (that table is cross-transfer
        provenance only), so a same-transfer sibling's durable membership
        would otherwise be invisible to cohort coordination once it becomes
        ``resolved``. This recognizes both same-transfer candidate origin/
        binding provenance and cross-transfer ``artifact_consolidations``.
        Returns ``None`` when there is no durable mapping, or when the
        mapping is ambiguous (more than one distinct target -- never guessed
        around)."""
        await self.initialize()
        async with get_db() as db:
            targets = await _durable_canonical_targets_for_request(db, request_id)
        return next(iter(targets)) if len(targets) == 1 else None

    @staticmethod
    async def _collection_owner(db, transfer_id: int) -> int | None:
        """The earliest admitted live transfer that owns a canonical artifact
        one of ``transfer_id``'s members durably consolidated into, when it was
        admitted before ``transfer_id``. That consolidation is the cross-transfer
        evidence that both submissions are one logical collection; durable
        admission order (``torrents.id``, the same order ``lower_materializing``
        compares first) names its owner."""
        row = await db.fetchone(
            f"""SELECT MIN(c.torrent_id) AS owner FROM artifact_consolidations a
                JOIN download_files c ON c.id=a.canonical_artifact_id
                JOIN torrents t ON t.id=c.torrent_id
                WHERE a.source_transfer_id=? AND c.torrent_id<? AND t.status NOT IN {_SETTLED_TRANSFER_STATES}""",
            (int(transfer_id), int(transfer_id)),
        )
        return int(row["owner"]) if row and row.get("owner") is not None else None

    @staticmethod
    async def _frozen_satisfiable(db, transfer_id: int) -> tuple[int, ...]:
        """The transfers whose completed, ownership-frozen material may satisfy
        an equivalent member of ``transfer_id``: the transfer itself, every
        other generation of its logical source (same ``source_fingerprint``:
        a terminal lifecycle a later independent submission retired, or a
        deleted one), and every transfer that is one recognized collection with
        any of those -- a member of either durably consolidated beneath a
        canonical artifact the other owns. This holds whatever the transfers'
        lifecycle states; whether the material is still present is a separate,
        current fact (``equivalence_targets``)."""
        lineage = {int(transfer_id)}
        for row in await db.fetchall(
                """SELECT p.id FROM torrents p JOIN torrents s ON s.id=?
                    WHERE p.id!=s.id AND s.source_fingerprint IS NOT NULL AND p.source_fingerprint=s.source_fingerprint""",
                (int(transfer_id),)):
            lineage.add(int(row["id"]))
        marks = ",".join("?" for _ in lineage)
        related = set(lineage)
        for row in await db.fetchall(
                f"""SELECT c.torrent_id AS id FROM artifact_consolidations a JOIN download_files c ON c.id=a.canonical_artifact_id
                    WHERE a.source_transfer_id IN ({marks})
                    UNION
                    SELECT a.source_transfer_id AS id FROM artifact_consolidations a
                    JOIN download_files c ON c.id=a.canonical_artifact_id WHERE c.torrent_id IN ({marks})""",
                (*lineage, *lineage)):
            related.add(int(row["id"]))
        return tuple(sorted(related))

    async def collection_owner(self, transfer_id: int) -> int | None:
        await self.initialize()
        async with get_db() as db:
            return await self._collection_owner(db, transfer_id)

    @staticmethod
    async def _inversion_rows(db, condition: str, params: tuple):
        """Canonical artifacts owned by a later transfer that hold a contributing
        standby of their transfer's collection owner. A writer that has
        succeeded is never an inversion here: its material is complete under
        the later transfer."""
        return await db.fetchall(
            f"""SELECT c.id AS canonical_id,s.id AS contributor_id,s.request_id AS contributor_request_id
                FROM download_files c
                JOIN torrents ct ON ct.id=c.torrent_id
                JOIN download_files s ON s.mirror_group_id=c.id AND s.mirror_state='standby' AND s.id!=c.id
                JOIN torrents st ON st.id=s.torrent_id
                JOIN artifact_consolidations a ON a.contributing_artifact_id=s.id AND a.canonical_artifact_id=c.id
                WHERE c.request_id IS NOT NULL AND COALESCE(c.blocked,0)=0
                AND COALESCE(c.mirror_state,'')!='standby' AND (c.mirror_group_id IS NULL OR c.mirror_group_id=c.id)
                AND c.status NOT IN ('completed','duplicate')
                AND NOT EXISTS(SELECT 1 FROM execution_attempts e WHERE e.artifact_id=c.id AND e.state='succeeded')
                AND ct.status NOT IN {_SETTLED_TRANSFER_STATES} AND st.status NOT IN {_SETTLED_TRANSFER_STATES}
                AND s.torrent_id<c.torrent_id
                AND s.torrent_id=(SELECT MIN(o.torrent_id) FROM artifact_consolidations m
                    JOIN download_files o ON o.id=m.canonical_artifact_id JOIN torrents ot ON ot.id=o.torrent_id
                    WHERE m.source_transfer_id=c.torrent_id AND o.torrent_id<c.torrent_id
                    AND ot.status NOT IN {_SETTLED_TRANSFER_STATES})
                AND {condition}
                ORDER BY c.torrent_id,c.id,s.id""",
            params,
        )

    async def collection_inversions(self, transfer_id: int) -> tuple[CollectionInversion, ...]:
        """Every inverted member ``transfer_id`` takes part in, as the later
        canonical owner or as the collection owner."""
        await self.initialize()
        result = []
        async with get_db() as db:
            rows = await self._inversion_rows(db, "(c.torrent_id=? OR s.torrent_id=?)", (transfer_id, transfer_id))
            for row in rows:
                canonical = await db.fetchone(
                    "SELECT f.*,NULL AS handle FROM download_files f WHERE f.id=?", (row["canonical_id"],),
                )
                contributor = await db.fetchone("SELECT candidates FROM download_files WHERE id=?",
                                                (row["contributor_id"],))
                request = await db.fetchone("SELECT * FROM transfer_requests WHERE id=?",
                                            (row["contributor_request_id"],))
                candidates = tuple(codec.candidate(item) for item in codec.load(contributor["candidates"], []))
                if canonical and request and candidates:
                    result.append(CollectionInversion(
                        self._artifact(canonical), int(row["contributor_id"]), self._record(request), candidates,
                    ))
        return tuple(result)

    async def converge(self, inversion: CollectionInversion, target: str, *, claim, now: float) -> bool:
        """Converge one inverted collection member on its collection owner.

        One ownership transaction under ``claim`` -- the later canonical's
        current recovery claim, whose holder has already retired its writer
        (``candidate_activation.retire_writer``) -- revalidated against the
        same durable facts that named the inversion. Every execution attempt
        the later canonical ever had must be terminal without success; each is
        kept as history with its authorization revoked, and the artifact
        detaches from it. The collection owner's contributing standby becomes
        the member's one canonical artifact at ``target`` (its own route
        first, every route the later canonical held retained as an alternate)
        and is dispatched as ordinary queued work; the later canonical becomes
        that transfer's contributing standby. Bindings move with their
        candidates and keep every origin, the existing consolidation
        relationship between the two artifacts is redirected, other
        contributors follow the canonical, and each transfer's activity
        history records the convergence. False when anything changed
        underneath (nothing is written)."""
        await self.initialize()
        canonical_id, contributor_id = int(inversion.canonical.id), int(inversion.contributor_id)
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            if (int(claim.artifact_id) != canonical_id
                    or not await self.repository.recovery_claim_current_in_db(db, claim, now=now)
                    or not await self._inversion_rows(db, "c.id=? AND s.id=?", (canonical_id, contributor_id))
                    or await db.fetchone(
                        """SELECT 1 AS live FROM execution_attempts WHERE artifact_id=?
                            AND state NOT IN ('failed','absent','cancelled')""", (canonical_id,))):
                await db.rollback()
                return False
            await db.execute("UPDATE execution_attempts SET authorized=0,updated_at=CURRENT_TIMESTAMP WHERE artifact_id=?",
                             (canonical_id,))
            current = await db.fetchone("SELECT * FROM download_files WHERE id=?", (canonical_id,))
            contributor = await db.fetchone("SELECT * FROM download_files WHERE id=?", (contributor_id,))
            standbys = await db.fetchall(
                "SELECT id,candidates FROM download_files WHERE mirror_group_id=? AND mirror_state='standby'",
                (canonical_id,),
            )
            retained = [codec.candidate(item) for item in codec.load(current["candidates"], [])]
            owned = [codec.candidate(item) for item in codec.load(contributor["candidates"], [])]
            owned_ids = {str(item.id) for item in owned}
            contributed = {str(item.get("id")) for row in standbys for item in codec.load(row["candidates"], [])}
            later_own = [item for item in retained if str(item.id) not in contributed]
            if not owned or not later_own:
                await db.rollback()
                return False
            converged = owned + [item for item in retained if str(item.id) not in owned_ids]
            owner_transfer_id, later_transfer_id = int(contributor["torrent_id"]), int(current["torrent_id"])

            await db.execute(
                """UPDATE download_files SET mirror_group_id=?,local_path=?,updated_at=CURRENT_TIMESTAMP
                    WHERE mirror_group_id=? AND mirror_state='standby' AND id!=?""",
                (contributor_id, target, canonical_id, contributor_id),
            )
            await db.execute(
                """UPDATE download_files SET status='queued',blocked=NULL,mirror_group_id=id,mirror_state='primary',
                    candidates=?,selected_candidate=0,size_bytes=?,local_path=?,normalized_error=NULL,retry_at=0,
                    download_client='',updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (codec.dump(tuple(converged)), current["size_bytes"], target, contributor_id),
            )
            await db.execute(
                """UPDATE download_files SET status='duplicate',blocked=NULL,mirror_group_id=?,mirror_state='standby',
                    candidates=?,selected_candidate=0,local_path=?,normalized_error=NULL,retry_at=0,download_client='',
                    execution_attempt_id=NULL,continuation_reservation_expires_at=NULL,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (contributor_id, codec.dump(tuple(later_own)), target, canonical_id),
            )

            await db.execute(
                """UPDATE canonical_candidate_bindings SET canonical_artifact_id=?,candidate_order=candidate_order+100000,
                    updated_at=CURRENT_TIMESTAMP WHERE canonical_artifact_id=?""",
                (contributor_id, canonical_id),
            )
            order = 0
            for candidate in converged:
                cursor = await db.execute(
                    """UPDATE canonical_candidate_bindings SET candidate_order=?
                        WHERE canonical_artifact_id=? AND candidate_id=? AND candidate_order>100000""",
                    (order + 1, contributor_id, str(candidate.id)),
                )
                order += int(cursor.rowcount or 0)
            for binding in await db.fetchall(
                    """SELECT id FROM canonical_candidate_bindings WHERE canonical_artifact_id=? AND candidate_order>100000
                        ORDER BY candidate_order""", (contributor_id,)):
                order += 1
                await db.execute("UPDATE canonical_candidate_bindings SET candidate_order=? WHERE id=?",
                                 (order, binding["id"]))
            await db.execute(
                """UPDATE canonical_candidate_bindings SET role=CASE WHEN EXISTS(
                        SELECT 1 FROM canonical_candidate_origins o
                        WHERE o.binding_id=canonical_candidate_bindings.id AND o.contributing_transfer_id=?)
                    THEN 'canonical' ELSE 'alternate' END WHERE canonical_artifact_id=?""",
                (owner_transfer_id, contributor_id),
            )

            await db.execute(
                """UPDATE artifact_consolidations SET contributing_artifact_id=?,source_transfer_id=?,source_request_id=?,
                    canonical_artifact_id=?,updated_at=CURRENT_TIMESTAMP WHERE contributing_artifact_id=?""",
                (canonical_id, later_transfer_id, current["request_id"], contributor_id, contributor_id),
            )
            await db.execute(
                """UPDATE artifact_consolidations SET canonical_artifact_id=?,updated_at=CURRENT_TIMESTAMP
                    WHERE canonical_artifact_id=?""",
                (contributor_id, canonical_id),
            )
            for transfer_id, message in (
                (later_transfer_id, f"Collection member ownership converged into transfer {owner_transfer_id}"),
                (owner_transfer_id, f"Collection member ownership converged from transfer {later_transfer_id}"),
            ):
                await db.execute("INSERT INTO events(torrent_id,level,message) VALUES(?,'info',?)",
                                 (transfer_id, message))
            await self._finalize_transfer(db, later_transfer_id)
            await db.commit()
        if self.on_attached is not None:
            await self.on_attached(later_transfer_id)
        return True

    async def lower_materializing(self, record: RequestRecord):
        await self.initialize()
        async with get_db() as db:
            current = await db.fetchone(
                """SELECT r.transfer_id,r.ordinal,r.rowid AS admission_rowid,r.state,t.status AS transfer_status
                    FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id WHERE r.id=?""",
                (record.id,),
            )
            # DP 1.0.12 leveling remediation: this record's own parent transfer
            # has already settled into a side-state-retiring/generation-done
            # status (transfers.policy.SIDE_STATE_RETIRING_TRANSFER_STATES --
            # membership is identical to the historical local literal this
            # replaces, including FAILED/"error": a failed transfer's
            # materializing residue cannot still be a live consolidation-
            # ordering contender either, even though FAILED itself remains
            # operator-reopenable). Nothing left to race for.
            if (not current or current["state"] != "materializing"
                    or current["transfer_status"] in SIDE_STATE_RETIRING_TRANSFER_STATES):
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
        """Atomically revalidate an established owner and attach one source.

        The owner is ordinarily a live canonical artifact. The one bounded
        exception is a COMPLETED canonical artifact that is still a valid
        equivalence target for ``record`` (``equivalence_targets``): one of a
        transfer ``_frozen_satisfiable`` relates to ``record``'s, whose
        material the caller's target selection found present. Completed material is
        ownership-frozen -- its row, candidates, status and transfer are not
        touched -- and the incoming source only becomes its contributing
        standby with provenance (binding, origin, and consolidation across
        transfers), so the incoming member is satisfied without a second
        writer."""
        await self.initialize()
        alternatives = tuple(replace(item, expected_bytes=size) for item in candidates)
        if not alternatives:
            return False
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            current = await db.fetchone(
                """SELECT f.*,t.status AS transfer_status FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                    WHERE f.id=? AND f.request_id IS NOT NULL AND COALESCE(f.blocked,0)=0
                    AND COALESCE(f.mirror_state,'')!='standby'
                    AND (f.mirror_group_id IS NULL OR f.mirror_group_id=f.id)""",
                (primary.id,),
            )
            frozen = bool(current) and current["status"] == "completed"
            if current and not (
                (current["status"] not in {"completed", "cancelled", "error", "duplicate"}
                 and current["transfer_status"] not in {"completed", "consolidated", "deleted", "cancelled", "error"})
                or (frozen and current["transfer_status"] not in {"deleted", "cancelled"}
                    and int(current["torrent_id"]) in await self._frozen_satisfiable(db, int(record.transfer_id)))):
                current = None
            # The incoming request is still deciding in a live transfer -- or
            # it is a settled contributor's UNVERIFIED association to exactly
            # this artifact, now proven (``cohorts.reconsider_association``).
            incoming = await db.fetchone(
                """SELECT r.id FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id
                    WHERE r.id=? AND r.transfer_id=? AND r.state='materializing'
                    AND (t.status NOT IN ('completed','consolidated','deleted','cancelled','error')
                         OR (t.status='consolidated' AND r.equivalence_disposition=?
                             AND r.equivalence_target_artifact_id=?))""",
                (record.id, record.transfer_id, _UNVERIFIED_DISPOSITION, int(primary.id)),
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

            if not frozen:
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
            else:
                # A same-transfer member proved the cohort's object: its dead
                # siblings may now be associated with it.
                await self._associate_failed_contributions(db, int(record.transfer_id))
            await db.commit()
        if self.on_attached is not None:
            await self.on_attached(record.transfer_id)
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

    async def adopted_candidates(self, transfer_id: int) -> dict[str, int]:
        """Candidates ``transfer_id`` contributed that ANOTHER transfer's
        still-live canonical artifact carries: candidate id -> that canonical
        transfer. Durable provenance only (bindings and their origins)."""
        await self.initialize()
        async with get_db() as db:
            rows = await db.fetchall(
                f"""SELECT b.candidate_id,f.torrent_id FROM canonical_candidate_origins o
                    JOIN canonical_candidate_bindings b ON b.id=o.binding_id
                    JOIN download_files f ON f.id=b.canonical_artifact_id JOIN torrents t ON t.id=f.torrent_id
                    WHERE o.contributing_transfer_id=? AND f.torrent_id!=?
                    AND t.status NOT IN {_SETTLED_TRANSFER_STATES}
                    ORDER BY o.id""",
                (int(transfer_id), int(transfer_id)),
            )
        return {str(row["candidate_id"]): int(row["torrent_id"]) for row in rows}

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
