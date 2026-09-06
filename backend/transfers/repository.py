"""WS2 P1 repository extension over the qualified WS1 P2 persistence owner.

The base module is the exact qualified WS1 P2 repository. This public owner adds
only atomic recovery transitions and provider-/executor-neutral acceptance of
execution-discovered artifact size. Writer authority and durable artifact
knowledge remain repository-owned.
"""
from __future__ import annotations

from db.database import get_db
from transfers import codec
from transfers._repository_base import TransferRepository as _QualifiedTransferRepository
from transfers.models import ExecutionState


_TERMINAL_EXECUTION_STATES = frozenset({"failed", "absent", "cancelled", "succeeded"})
_MUTATING_EXECUTION_STATES = frozenset({"prepared", "queued", "transferring", "paused", "unknown"})
_RUNTIME_TOTAL_STATES = frozenset({
    ExecutionState.QUEUED,
    ExecutionState.TRANSFERRING,
    ExecutionState.PAUSED,
})
_FAILED_CANDIDATE_OUTCOMES = frozenset({"failed", "error", "rejected", "cancelled", "absent"})


def _safe_source_label(scope, key) -> str:
    """Project durable source identity without exposing opaque candidate keys."""
    normalized_scope = str(scope or "").strip().lower()
    normalized_key = str(key or "").strip()
    if normalized_scope == "host" and normalized_key:
        return normalized_key.lower()
    if normalized_scope in {"scheme", "protocol"} and normalized_key:
        return normalized_key.upper()
    return "Source"


class TransferRepository(_QualifiedTransferRepository):
    async def collection_route_provider(self, transfer_id: int) -> str | None:
        """Return the durable specialized route owner for a collection, if any."""
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT collection_route_provider_id FROM torrents WHERE id=?",
                (transfer_id,),
            )
        value = str((row or {}).get("collection_route_provider_id") or "").strip()
        return value or None

    async def bind_collection_route(self, transfer_id: int, provider_id: str) -> str | None:
        """Atomically establish one collection route owner before resolution starts.

        Existing route attempts are a hard compatibility veto: historical mixed
        transfers retain their truthful request-level provenance rather than
        being retroactively rebound by this correction. New or still-untouched
        multi-request direct-link collections may acquire exactly one owner.
        """
        provider_id = str(provider_id or "").strip()
        if not provider_id:
            raise ValueError("Collection route provider identity is required")
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            parent = await db.fetchone(
                "SELECT source,collection_route_provider_id FROM torrents WHERE id=?",
                (transfer_id,),
            )
            if not parent or str(parent.get("source") or "") != "direct_link":
                await db.rollback()
                return None
            existing = str(parent.get("collection_route_provider_id") or "").strip()
            if existing:
                await db.rollback()
                return existing
            roots = await db.fetchone(
                """SELECT COUNT(*) AS count FROM transfer_requests
                    WHERE transfer_id=? AND parent_id IS NULL""",
                (transfer_id,),
            )
            if int((roots or {}).get("count") or 0) <= 1:
                await db.rollback()
                return None
            routed = await db.fetchone(
                """SELECT 1 AS present FROM resolution_attempts a
                    JOIN transfer_requests r ON r.id=a.request_id
                    WHERE r.transfer_id=? LIMIT 1""",
                (transfer_id,),
            )
            if routed:
                await db.rollback()
                return None
            cursor = await db.execute(
                """UPDATE torrents SET collection_route_provider_id=?,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND collection_route_provider_id IS NULL""",
                (provider_id, transfer_id),
            )
            if cursor.rowcount != 1:
                current = await db.fetchone(
                    "SELECT collection_route_provider_id FROM torrents WHERE id=?",
                    (transfer_id,),
                )
                await db.rollback()
                value = str((current or {}).get("collection_route_provider_id") or "").strip()
                return value or None
            await db.commit()
        return provider_id

    async def bound_route_provider(self, request_id: str) -> str | None:
        """Prefer truthful request history, then inherit durable collection affinity."""
        routed = await super().bound_route_provider(request_id)
        if routed:
            return routed
        async with get_db() as db:
            row = await db.fetchone(
                """SELECT t.collection_route_provider_id
                    FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id
                    WHERE r.id=?""",
                (request_id,),
            )
        value = str((row or {}).get("collection_route_provider_id") or "").strip()
        return value or None

    async def accept_execution_total(self, artifact_id: int, handle, total_bytes: int) -> bool:
        """Durably accept positive runtime size evidence for the current execution.

        Candidate-declared expected size remains immutable candidate evidence.
        Runtime observation may fill artifact-level size knowledge only while it
        is unknown. Once positive artifact size exists, contradictory transient
        observations cannot rewrite the denominator.
        """
        if (not isinstance(total_bytes, int) or isinstance(total_bytes, bool)
                or total_bytes <= 0 or handle is None):
            return False
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                """SELECT f.size_bytes,f.execution_attempt_id,e.executor_id,e.handle
                    FROM download_files f
                    LEFT JOIN execution_attempts e ON e.id=f.execution_attempt_id
                    WHERE f.id=?""",
                (artifact_id,),
            )
            if (not row or row.get("execution_attempt_id") != handle.attempt_id
                    or row.get("executor_id") != handle.executor_id
                    or codec.load(row.get("handle")) != codec.load(codec.dump(handle))
                    or int(row.get("size_bytes") or 0) > 0):
                await db.rollback()
                return False
            cursor = await db.execute(
                """UPDATE download_files SET size_bytes=?,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND execution_attempt_id=? AND COALESCE(size_bytes,0)<=0""",
                (total_bytes, artifact_id, handle.attempt_id),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def execution(self, observation) -> None:
        """Persist neutral execution evidence and promote a credible live total.

        This is deliberately below every provider and executor adapter. Any
        future executor that normalizes a credible positive total into
        TransferProgress receives the same behavior without provider- or
        transport-specific code. Revoked terminal history is immutable: a late
        stale observation cannot reactivate a retired writer or alter its record.
        """
        async with get_db() as db:
            previous = await db.fetchone(
                "SELECT state,authorized,handle FROM execution_attempts WHERE id=?",
                (observation.handle.attempt_id,),
            )
        if (previous and not bool(previous.get("authorized"))
                and previous.get("state") in _TERMINAL_EXECUTION_STATES
                and codec.load(previous.get("handle")) == codec.load(codec.dump(observation.handle))):
            return

        await super().execution(observation)
        total = observation.progress.total_bytes
        completed = observation.progress.completed_bytes
        credible = (
            observation.state in _RUNTIME_TOTAL_STATES
            and isinstance(total, int) and not isinstance(total, bool) and total > 0
            and isinstance(completed, int) and not isinstance(completed, bool)
            and 0 <= completed <= total
        )
        if not credible:
            return
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT artifact_id FROM execution_attempts WHERE id=?",
                (observation.handle.attempt_id,),
            )
        if row:
            await self.accept_execution_total(
                int(row["artifact_id"]), observation.handle, total,
            )

    async def refine_execution_total(self, artifact_id: int, handle, total_bytes: int) -> bool:
        """Accept contradictory final size only after caller verified materialization.

        This exception is intentionally narrow: the provider/candidate must have
        declared size unknown, the same current execution must already be
        SUCCEEDED, and the caller must have independently verified the final
        payload against ``total_bytes``. The refinement is recorded as an event
        rather than silently rewriting earlier runtime evidence.
        """
        if (not isinstance(total_bytes, int) or isinstance(total_bytes, bool)
                or total_bytes < 0 or handle is None):
            return False
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                """SELECT f.size_bytes,f.execution_attempt_id,f.torrent_id,
                    e.executor_id,e.handle,e.state,e.candidate
                    FROM download_files f JOIN execution_attempts e ON e.id=f.execution_attempt_id
                    WHERE f.id=?""",
                (artifact_id,),
            )
            candidate = None
            if row and row.get("candidate"):
                try:
                    candidate = codec.candidate(codec.load(row["candidate"]))
                except (TypeError, ValueError, KeyError):
                    candidate = None
            if (not row or row.get("execution_attempt_id") != handle.attempt_id
                    or row.get("executor_id") != handle.executor_id
                    or codec.load(row.get("handle")) != codec.load(codec.dump(handle))
                    or row.get("state") != "succeeded"
                    or candidate is None or candidate.expected_bytes > 0):
                await db.rollback()
                return False
            previous = int(row.get("size_bytes") or 0)
            cursor = await db.execute(
                """UPDATE download_files SET size_bytes=?,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND execution_attempt_id=?""",
                (total_bytes, artifact_id, handle.attempt_id),
            )
            if cursor.rowcount and previous != total_bytes:
                await db.execute(
                    """INSERT INTO events(torrent_id,level,message)
                        VALUES(?,'warning','Final verified materialization refined execution-observed artifact size')""",
                    (row["torrent_id"],),
                )
            await db.commit()
        return cursor.rowcount == 1

    async def transition_recovery(
        self,
        artifact_id: int,
        state: str,
        *,
        error=None,
        retry_at: float = 0,
        selected: int | None = None,
        expected_bytes: int | None = None,
        reset_budget: bool = False,
    ) -> bool:
        """Atomically retire proven-terminal writer authority and enter recovery.

        UNKNOWN, prepared, queued, transferring, or paused authority is a hard
        veto. No transition that can permit another writer clears the current
        execution pointer unless all authorized mutation-capable attempts for
        the artifact are already proven non-writing.
        """
        if expected_bytes is not None and expected_bytes < 0:
            return False
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                """SELECT f.execution_attempt_id,t.status AS transfer_status
                    FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                    WHERE f.id=?""",
                (artifact_id,),
            )
            if not row or row["transfer_status"] in {"deleted", "completed", "consolidated", "cancelled"}:
                await db.rollback()
                return False

            current_id = row.get("execution_attempt_id")
            if current_id is not None:
                current = await db.fetchone(
                    "SELECT state,authorized FROM execution_attempts WHERE id=? AND artifact_id=?",
                    (current_id, artifact_id),
                )
                if not current or current["state"] not in _TERMINAL_EXECUTION_STATES:
                    await db.rollback()
                    return False
                await db.execute(
                    "UPDATE execution_attempts SET authorized=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (current_id,),
                )

            placeholders = ",".join("?" for _ in _MUTATING_EXECUTION_STATES)
            active = await db.fetchone(
                f"""SELECT id FROM execution_attempts WHERE artifact_id=? AND authorized=1
                    AND state IN ({placeholders}) LIMIT 1""",
                (artifact_id, *_MUTATING_EXECUTION_STATES),
            )
            if active:
                await db.rollback()
                return False

            assignments = [
                "status=?",
                "normalized_error=?",
                "retry_at=?",
                "execution_attempt_id=NULL",
                "updated_at=CURRENT_TIMESTAMP",
            ]
            params = [state, codec.dump(error) if error else None, retry_at]
            if selected is not None:
                assignments.append("selected_candidate=?")
                params.append(selected)
            if expected_bytes is not None:
                assignments.append("size_bytes=?")
                params.append(expected_bytes)
            if reset_budget:
                assignments.extend([
                    "retry_count=0",
                    "recovery_failures=0",
                    "recovery_refreshes=0",
                ])
            params.append(artifact_id)
            cursor = await db.execute(
                f"UPDATE download_files SET {','.join(assignments)} WHERE id=?",
                tuple(params),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def _candidate_presentation(self, transfer_id: int) -> dict[int, dict]:
        """Return the minimal safe Details projection of durable candidate ownership."""
        async with get_db() as db:
            files = await db.fetchall(
                """SELECT id,candidates,selected_candidate,execution_attempt_id
                    FROM download_files WHERE torrent_id=? AND request_id IS NOT NULL
                    AND COALESCE(blocked,0)=0 AND COALESCE(mirror_state,'')!='standby'
                    ORDER BY id""",
                (transfer_id,),
            )
            artifact_ids = [int(row["id"]) for row in files]
            if not artifact_ids:
                return {}
            placeholders = ",".join("?" for _ in artifact_ids)
            bindings = await db.fetchall(
                f"""SELECT canonical_artifact_id,candidate_id,provider_id,source_scope,source_key,role,candidate_order
                    FROM canonical_candidate_bindings
                    WHERE canonical_artifact_id IN ({placeholders})
                    ORDER BY canonical_artifact_id,candidate_order,id""",
                tuple(artifact_ids),
            )
            attempts = await db.fetchall(
                f"""SELECT p.artifact_id,p.candidate_id,p.outcome,p.delivered,p.ordinal,
                    p.execution_attempt_id,e.state,e.authorized
                    FROM execution_attempt_provenance p
                    LEFT JOIN execution_attempts e ON e.id=p.execution_attempt_id
                    WHERE p.artifact_id IN ({placeholders})
                    ORDER BY p.artifact_id,p.ordinal,p.execution_attempt_id""",
                tuple(artifact_ids),
            )

        selected_ids: dict[int, str | None] = {}
        current_attempt_ids: dict[int, str | None] = {}
        has_durable_candidate: dict[int, bool] = {}
        for row in files:
            artifact_id = int(row["id"])
            selected_id = None
            try:
                candidates = [codec.candidate(value) for value in codec.load(row.get("candidates"), [])]
                has_durable_candidate[artifact_id] = bool(candidates)
                selected = int(row.get("selected_candidate") or 0)
                if 0 <= selected < len(candidates):
                    selected_id = str(candidates[selected].id)
            except (TypeError, ValueError, KeyError, IndexError):
                has_durable_candidate[artifact_id] = False
                selected_id = None
            selected_ids[artifact_id] = selected_id
            current_attempt_ids[artifact_id] = row.get("execution_attempt_id")

        attempt_history: dict[tuple[int, str], list[dict]] = {}
        for row in attempts:
            candidate_id = str(row.get("candidate_id") or "").strip()
            if not candidate_id:
                continue
            key = (int(row["artifact_id"]), candidate_id)
            attempt_history.setdefault(key, []).append(dict(row))

        by_artifact: dict[int, list[dict]] = {}
        seen: dict[int, set[str]] = {}
        for row in bindings:
            artifact_id = int(row["canonical_artifact_id"])
            candidate_id = str(row["candidate_id"])
            if candidate_id in seen.setdefault(artifact_id, set()):
                continue
            seen[artifact_id].add(candidate_id)
            history = attempt_history.get((artifact_id, candidate_id), [])
            delivered = any(bool(item.get("delivered")) for item in history)
            latest = history[-1] if history else None
            failed = bool(latest and (
                str(latest.get("outcome") or "").strip().lower() in _FAILED_CANDIDATE_OUTCOMES
                or str(latest.get("state") or "").strip().lower() in _FAILED_CANDIDATE_OUTCOMES
            )) and not delivered
            selected = selected_ids.get(artifact_id) == candidate_id
            current = bool(latest and current_attempt_ids.get(artifact_id) == latest.get("execution_attempt_id"))
            active = bool(current and latest and bool(latest.get("authorized"))
                          and str(latest.get("state") or "").strip().lower() in _MUTATING_EXECUTION_STATES)
            dispositions = []
            if delivered:
                dispositions.append("Delivering")
            elif failed:
                dispositions.append("Failed")
            elif active:
                dispositions.append("Active")
            elif selected:
                dispositions.append("Selected")
            by_artifact.setdefault(artifact_id, []).append({
                "candidate_id": candidate_id,
                "source_label": _safe_source_label(row.get("source_scope"), row.get("source_key")),
                "provider_id": str(row.get("provider_id") or "").strip() or None,
                "relationship": "Original" if row.get("role") == "canonical" else "Consolidated",
                "dispositions": dispositions,
                "is_selected": selected,
                "is_delivering": delivered,
            })

        result: dict[int, dict] = {}
        for artifact_id in artifact_ids:
            candidates = by_artifact.get(artifact_id, [])
            candidate_count = len(candidates)
            if candidate_count == 0 and has_durable_candidate.get(artifact_id, False):
                candidate_count = 1
            result[artifact_id] = {
                "candidate_count": candidate_count,
                "acquisition_candidates": candidates if candidate_count > 1 else [],
            }
        return result

    async def presentation(self, transfer_id: int, details: bool = False):
        """Add safe candidate multiplicity only to the Details read model."""
        result = await super().presentation(transfer_id, details=details)
        if not result or not details:
            return result
        candidate_projection = await self._candidate_presentation(transfer_id)
        for file_row in result.get("files", []):
            artifact_id = int(file_row.get("id") or 0)
            projection = candidate_projection.get(artifact_id, {
                "candidate_count": 0,
                "acquisition_candidates": [],
            })
            file_row["candidate_count"] = projection["candidate_count"]
            if projection["candidate_count"] > 1:
                file_row["acquisition_candidates"] = projection["acquisition_candidates"]
        return result
