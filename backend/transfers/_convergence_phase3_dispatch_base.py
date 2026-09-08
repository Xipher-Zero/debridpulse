"""Public Phase-3 unified recovery execution surface."""
from __future__ import annotations

from transfers._convergence_phase3_truth_base import TransferEngine as _QualifiedPhase3Engine
from transfers.errors import NormalizedError
from transfers.models import Artifact, ExecutionObservation
from transfers.recovery_execution import RecoveryTrigger


class TransferEngine(_QualifiedPhase3Engine):
    """Production owner for every Phase-3 recovery trigger."""

    async def recover_artifact(
        self,
        artifact: Artifact | int,
        *,
        trigger: RecoveryTrigger,
        error: NormalizedError | None = None,
        observed: ExecutionObservation | None = None,
    ) -> bool:
        return await super().recover_artifact(
            artifact,
            trigger=trigger,
            error=error,
            observed=observed,
        )

    async def _dispatch(self, artifact: Artifact):
        """Route pre-execution readiness failures through canonical recovery."""
        candidate = self._candidate(artifact)
        if candidate is not None and not self._candidate_provider_enabled(candidate):
            return await self.recover_artifact(
                artifact,
                trigger=RecoveryTrigger.AUTO_RETRY,
                error=self._provider_wait_error(candidate),
            )
        if candidate is not None and not self.registry.eligible_executors(candidate):
            return await self.recover_artifact(
                artifact,
                trigger=RecoveryTrigger.AUTO_RETRY,
                error=self._executor_wait_error(),
            )
        return await super()._dispatch(artifact)
