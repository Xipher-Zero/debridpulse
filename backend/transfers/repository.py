"""WS2 P1 repository extension over the qualified WS1 P2 persistence owner.

The base module is the exact qualified WS1 P2 repository.  This public owner adds
only atomic recovery transitions and acceptance of executor-discovered artifact
size, keeping writer authority and durable artifact knowledge in the repository.
"""
from __future__ import annotations

from db.database import get_db
from transfers import codec
from transfers._repository_base import TransferRepository as _QualifiedTransferRepository


_TERMINAL_EXECUTION_STATES = frozenset({"failed", "absent", "cancelled", "succeeded"})
_MUTATING_EXECUTION_STATES = frozenset({"prepared", "queued", "transferring", "paused", "unknown"})


class TransferRepository(_QualifiedTransferRepository):
    async def accept_execution_total(self, artifact_id: int, handle, total_bytes: int) -> bool:
        """Durably accept a positive runtime total only for the current execution.

        Candidate-declared size remains on the candidate.  This fills artifact
        size knowledge only when it is still unknown, so later contradictory
        observations cannot rewrite an established denominator.
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
        veto.  No recovery transition that can permit another writer clears the
        current execution pointer unless all authorized mutation-capable attempts
        for the artifact are already proven non-writing.
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
