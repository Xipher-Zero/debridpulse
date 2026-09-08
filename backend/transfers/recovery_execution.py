"""Canonical Phase-3 trigger/authority values used by every recovery entry point."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RecoveryTrigger(StrEnum):
    AUTO_RETRY = "auto_retry"
    USER_RETRY = "user_retry"
    RESUME = "resume"
    STARTUP_RECONCILE = "startup_reconcile"
    PROVIDER_RECOVERY = "provider_recovery"
    EXECUTOR_RECOVERY = "executor_recovery"


@dataclass(frozen=True)
class TriggerAuthority:
    reset_exhaustion: bool = False
    reset_bounded_streaks: bool = False
    unpause_intent: bool = False


_AUTHORITY = {
    RecoveryTrigger.AUTO_RETRY: TriggerAuthority(),
    RecoveryTrigger.USER_RETRY: TriggerAuthority(reset_exhaustion=True, reset_bounded_streaks=True),
    RecoveryTrigger.RESUME: TriggerAuthority(unpause_intent=True),
    RecoveryTrigger.STARTUP_RECONCILE: TriggerAuthority(),
    RecoveryTrigger.PROVIDER_RECOVERY: TriggerAuthority(),
    RecoveryTrigger.EXECUTOR_RECOVERY: TriggerAuthority(),
}


def trigger_authority(trigger: RecoveryTrigger) -> TriggerAuthority:
    return _AUTHORITY[RecoveryTrigger(trigger)]


@dataclass(frozen=True)
class RecoveryClaim:
    artifact_id: int
    token: str
    generation: int
    trigger: RecoveryTrigger
    decision_id: str
    target: str
