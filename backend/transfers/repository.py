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


class TransferRepository(_QualifiedTransferRepository):
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
        future executor that normalizes a positive total into TransferProgress
        receives the same behavior without provider- or transport-specific code.
        """
        await super().execution(observation)
        total = observation.progress.total_bytes
        if (observation.state not in _RUNTIME_TOTAL_STATES
                or not isinstance(total, int) or isinstance(total, bool) or total <= 0):
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
