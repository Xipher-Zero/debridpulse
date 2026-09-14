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
    # DP 1.0.12 recovery leveling, Section 10/11: the first-class trigger an
    # operator-requested candidate switch claims recovery under, so it is
    # fenced by the SAME exclusive claim/generation system as every other
    # trigger (transfers._recovery_repository_claim_base.claim_recovery is
    # exclusive across all trigger types) instead of being an out-of-band
    # mutation the recovery system knows nothing about.
    USER_CANDIDATE_SWITCH = "user_candidate_switch"


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
    # Same reset authority as USER_RETRY: an operator naming a new candidate
    # is at least as strong a signal of intent to make fresh progress as a
    # plain retry, and transition_recovery(candidate_switched=True) has
    # always reset the bounded no-progress streaks on a successful switch.
    RecoveryTrigger.USER_CANDIDATE_SWITCH: TriggerAuthority(reset_exhaustion=True, reset_bounded_streaks=True),
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
