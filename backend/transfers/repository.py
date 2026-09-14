"""Canonical transfer persistence, including durable recovery accounting.

The qualified base remains the owner of ordinary transfer/request persistence.
This public owner contains the atomic recovery extensions, progress-aware epoch
accounting, candidate provenance presentation, and execution-discovered size
acceptance. Current recovery state lives in the single-row-per-artifact
``artifact_recovery_state`` table (DP 1.0.12 recovery leveling, Section 14);
sparse, semantically-meaningful transitions are additionally recorded as
durable ``application_events`` audit rows (Section 18), separate from that
current-state row (Section 15).

Recovery epoch / generation / candidate-attempt semantic model (Section 16)
----------------------------------------------------------------------------
Three independent counters answer three different questions. Each has
exactly one owner and none is derived from either of the others:

``recovery_epoch`` (``execution()``'s ``MEANINGFUL_PROGRESS`` reset profile,
the ONLY writer): "how many times has this artifact crossed the meaningful-
progress byte threshold." Advances only on genuine forward progress -- a
plain retry or candidate switch does NOT advance it. Crossing it rezeroes
every no-progress-budget counter (``apply_recovery_reset``) but does NOT
itself clear ``candidate_attempt_history`` -- only an explicit
``OPERATOR_RETRY`` full reset does that (see the RESET/PRESERVED/ARCHIVED/
ADVANCED matrix on ``apply_recovery_reset`` below).

``recovery_generation`` (``claim_recovery()`` and ``set_pause_and_fence()``,
the ONLY writers): the exclusive-claim fencing generation. Advances once per
successfully acquired recovery claim (any trigger) and once per pause/resume
fence-everything event. Every claim-scoped mutation (``record_phase3_decision``,
``record_recovery_quiescence``, ``reserve_recovery_refresh``,
``finish_recovery_claim``, ...) requires its ``RecoveryClaim.generation`` to
still match the persisted value -- this is what a stale/superseded claim
cannot forge, which is what prevents it from mutating a newer generation's
state (Section 25's adversarial concurrency guarantee).

``candidate_generation`` (historical/explainability only, Section 15): NOT a
persisted running counter in current state at all -- reconstructed on
demand by ``_historical_audit_facts`` as a COUNT of ``finish_claim`` audit
transitions carrying ``candidate_changed=True`` for this artifact. Answers
"how many times has this artifact's candidate actually changed," for
explainability only; nothing branches on it, and it costs nothing in
``artifact_recovery_state`` because it is never stored there.

``candidate_attempt_history`` (Section 12, current state, own field --
unrelated to the three counters above): the list of candidate ids actually
attempted in the artifact's current recovery episode. Only ever grows
(``record_candidate_attempt``) or is wholly cleared by an explicit
``OPERATOR_RETRY`` full reset -- never inferred from ``recovery_epoch`` or
``recovery_generation`` moving.
"""
from __future__ import annotations

from enum import StrEnum

from db.database import get_db
from transfers import codec
from transfers import file_selection as fs
from transfers._repository_base import TransferRepository as _QualifiedTransferRepository
from transfers.errors import Category, Domain, NormalizedError, Stage, TransferError
from transfers.manual_failover import SWITCH_ELIGIBLE_LIFECYCLE_STATES as _SWITCHABLE_ARTIFACT_STATES
from transfers.models import ExecutionState, TransferProgress
from transfers.policy import failure_signature, meaningful_progress_threshold


class RecoveryResetAuthority(StrEnum):
    """DP 1.0.12 recovery leveling, Section 17: the one canonical recovery
    reset / new-attempt transition. Every caller that begins a new recovery
    attempt/epoch names its authority here instead of hand-writing its own
    ad-hoc snapshot-dict reset (the pattern Section 17 forbids -- e.g. the
    pre-leveling ``manual_failover.py`` manually assigning snapshot fields).
    Each authority intentionally resets a DIFFERENT, named set of facts --
    they are deliberately not identical; see ``apply_recovery_reset``.
    """
    MEANINGFUL_PROGRESS = "meaningful_progress"
    SOURCE_RESET = "source_reset"
    OPERATOR_RETRY = "operator_retry"
    CANDIDATE_ACTIVATION_BUDGET = "candidate_activation_budget"
    CANDIDATE_SWITCHED = "candidate_switched"


def apply_recovery_reset(snapshot: dict, authority: "RecoveryResetAuthority | str") -> None:
    """Mutate ``snapshot`` in place per the named authority's canonical reset
    profile (Section 17): the one canonical recovery reset / new-attempt
    transition. Every caller that begins a new recovery attempt/epoch names
    its authority; this function is the single place each authority's
    RESET / PRESERVED / ARCHIVED / ADVANCED facts are defined, replacing the
    pre-leveling pattern of ad-hoc snapshot-dict edits scattered across
    calling code (e.g. the old ``manual_failover.py`` manually assigning
    snapshot fields inline).

    RESET / PRESERVED / ARCHIVED / ADVANCED matrix
    ------------------------------------------------
    Columns are the five authorities; a cell shows what happens to that row's
    fact under that authority. "reset" = zeroed/cleared here. "preserved" =
    left untouched by this function (the caller may still separately set it).
    "archived" = never touched here; captured only as a sparse audit fact by
    the caller (Section 15/18), never part of this reset. "advanced" = the
    ADVANCED fact incremented/updated here.

    ============================== ================ ============ ============== ========================== ==================
    fact                            MEANINGFUL_       SOURCE_      OPERATOR_       CANDIDATE_ACTIVATION_      CANDIDATE_
                                     PROGRESS          RESET        RETRY           BUDGET                     SWITCHED
    ============================== ================ ============ ============== ========================== ==================
    consecutive_no_progress_fail.   reset             reset        reset          reset                      reset
    failure_signature               reset             reset        reset          reset                      reset
    same_signature_failures         reset             reset        reset          reset                      reset
    candidate_refreshes             reset             reset        reset          reset                      reset
    failures_since_meaningful_prog. reset             reset        reset          reset                      preserved
    candidate_switches              reset (=0)        preserved    reset (=0)     preserved                  advanced (+1)
    decision_action/reason          reset             reset        reset          preserved                  preserved
    quiescence_reason/wake_cond.    reset             preserved    reset          preserved                  preserved
    recovery_epoch                  advanced (+1)     preserved    preserved      preserved                  preserved
    ============================== ================ ============ ============== ========================== ==================

    Every cell above is a direct transcription of the pre-leveling per-caller
    inline behavior (``execution()``'s threshold-crossed branch,
    ``reset_source_recovery``, ``reset_retry_budget``,
    ``transition_recovery(reset_budget=True)``, and
    ``transition_recovery(candidate_switched=True)`` respectively), verified
    field-by-field before this refactor -- the authorities are deliberately
    NOT homogenized to a single shared profile; each preserves its own
    pre-existing, independently-tuned behavior. Historical/audit-only facts
    (Section 15) are never touched by this function under any authority --
    they are ARCHIVED separately, as sparse audit facts, by the calling
    method itself.
    """
    authority = RecoveryResetAuthority(authority)
    common = {
        "consecutive_no_progress_failures": 0,
        "failure_signature": None,
        "same_signature_failures": 0,
        "candidate_refreshes": 0,
    }
    if authority is RecoveryResetAuthority.MEANINGFUL_PROGRESS:
        snapshot.update(common)
        snapshot.update({
            "recovery_epoch": int(snapshot.get("recovery_epoch") or 0) + 1,
            "failures_since_meaningful_progress": 0,
            "candidate_switches": 0,
            "decision_action": None,
            "decision_reason": None,
            "quiescence_reason": None,
            "wake_condition": None,
        })
    elif authority is RecoveryResetAuthority.SOURCE_RESET:
        snapshot.update(common)
        snapshot.update({
            "failures_since_meaningful_progress": 0,
            "decision_action": None,
            "decision_reason": None,
        })
    elif authority is RecoveryResetAuthority.OPERATOR_RETRY:
        snapshot.update(common)
        snapshot.update({
            "failures_since_meaningful_progress": 0,
            "candidate_switches": 0,
            "decision_action": None,
            "decision_reason": None,
            "quiescence_reason": None,
            "wake_condition": None,
        })
    elif authority is RecoveryResetAuthority.CANDIDATE_ACTIVATION_BUDGET:
        snapshot.update(common)
        snapshot["failures_since_meaningful_progress"] = 0
    elif authority is RecoveryResetAuthority.CANDIDATE_SWITCHED:
        snapshot.update(common)
        snapshot["candidate_switches"] = int(snapshot.get("candidate_switches") or 0) + 1
    else:  # pragma: no cover - RecoveryResetAuthority(...) already rejects this
        raise ValueError(authority)


# DP 1.0.12 recovery leveling, Section 15: facts that are recorded for
# durable explainability but are never read back to make a policy decision
# anywhere in the codebase (verified by repository-wide search before this
# leveling pass -- see the Phase 3 evidence report). These live SOLELY in
# the sparse append-only ``recovery_audit`` trail (application_events) --
# never in artifact_recovery_state, in any shape, current-state-adjacent or
# otherwise. ``_historical_audit_facts`` reconstructs them on demand by
# scanning that trail; ``recovery_context()`` merges the result in for its
# own (non-policy) callers so existing readers (tests, future Details/audit
# UI) see an unchanged flat shape. ``_recovery_snapshot()`` itself -- read
# directly by every fencing/traversal/reset policy decision -- never sees
# these keys at all.
_HISTORICAL_SNAPSHOT_KEYS = frozenset({
    "last_applied_trigger", "last_application_outcome", "last_execution_attempt",
    "last_execution_identity", "last_reconstruction_reason", "last_execution_retirement_reason",
    "durable_target", "candidate_generation", "last_candidate_id", "decision_recovery_epoch",
    "failure_classification", "classification_confidence", "classification_evidence",
    "bytes_at_failure", "bytes_since_prior_failure", "last_refresh_reason",
    "last_candidate_switch_reason", "last_terminalization_reason", "partial_state_preserved",
    "target_change_reason", "last_budget_before", "last_budget_after",
})


_TERMINAL_EXECUTION_STATES = frozenset({"failed", "absent", "cancelled", "succeeded"})
_MUTATING_EXECUTION_STATES = frozenset({"prepared", "queued", "transferring", "paused", "unknown"})
_RUNTIME_TOTAL_STATES = frozenset({
    ExecutionState.QUEUED,
    ExecutionState.TRANSFERRING,
    ExecutionState.PAUSED,
})
_FAILED_CANDIDATE_OUTCOMES = frozenset({"failed", "error", "rejected", "absent"})


def _safe_source_label(scope, key) -> str:
    """Project durable source identity without exposing opaque candidate keys."""
    normalized_scope = str(scope or "").strip().lower()
    normalized_key = str(key or "").strip()
    if normalized_scope == "host" and normalized_key:
        return normalized_key.lower()
    if normalized_scope in {"scheme", "protocol"} and normalized_key:
        return normalized_key.upper()
    return "Source"


# Re-exported (not redefined -- Section 31) from the ONE canonical owner,
# ``transfers.manual_failover.SWITCH_ELIGIBLE_LIFECYCLE_STATES``, so this
# module's read-time group-switch eligibility projection can never drift from
# the actual command gate. The authoritative switch command still
# re-validates every other gate (live route/candidate-expiry, provider
# health) itself; this frozenset only ever answers the lifecycle-state
# question, identically everywhere it is asked.


def _group_source_host(scope, key) -> str | None:
    """Normalized canonical host identity for transfer-level group intersection.

    Kept byte-identical to the SQL host normalization in
    ``api/operational_downloads.py`` so the Details-derived group set and the
    bounded Downloads/Recent common-source count agree for the same transfer:
    lower-case, drop a leading ``www.``, drop a trailing dot, reject empty or
    over-long values. This is the established canonical lower-case host
    semantics; it is redefined here rather than imported from
    ``presentation_repository`` only to avoid an import cycle.
    """
    if str(scope or "").strip().lower() != "host":
        return None
    host = str(key or "").strip().lower().removeprefix("www.").rstrip(".")
    if not host or len(host) > 253:
        return None
    return host


class TransferRepository(_QualifiedTransferRepository):
    @staticmethod
    def _recovery_event_kind(artifact_id: int) -> str:
        """The legacy pre-leveling snapshot-event kind (Section 19).

        No longer written. Retained only so the one-time migration reader
        (``db.database._migrate_recovery_state_from_events``) and historical
        ``application_events`` rows predating DP 1.0.12 recovery leveling
        remain identifiable/queryable.
        """
        return f"transfer_recovery:{int(artifact_id)}"

    @classmethod
    async def _recovery_snapshot(cls, db, artifact_id: int, *, row=None) -> dict:
        """Read the ONE canonical current-state row for this artifact (Section 14).

        Before DP 1.0.12 recovery leveling, "current" state was reconstructed
        by scanning the latest ``application_events`` row of kind
        ``transfer_recovery:<artifact_id>`` -- an unbounded, append-only
        history that grew a full-snapshot row on every meaningful mutation,
        including ordinary progress-byte advancement at roughly scheduler
        cadence. It now reads ``artifact_recovery_state``, which holds
        exactly one row per artifact, updated in place by
        ``_save_recovery_snapshot`` below.
        """
        if row is None:
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (artifact_id,),
            )
        if not row:
            raise KeyError(artifact_id)
        snapshot = {
            "version": 2,
            "recovery_epoch": 0,
            "progress_anchor": None,
            "consecutive_no_progress_failures": int(row.get("recovery_failures") or 0),
            "failures_since_meaningful_progress": int(row.get("recovery_failures") or 0),
            "failure_signature": None,
            "same_signature_failures": 0,
            "candidate_refreshes": int(row.get("recovery_refreshes") or 0),
            "candidate_switches": 0,
            "decision_action": None,
            "decision_reason": None,
            "quiescence_reason": None,
            "wake_condition": None,
            # DP 1.0.12 recovery leveling, Section 12: candidates this artifact
            # has actually been activated onto (as either the original
            # selection or a later switch), separate from ``selected_candidate``
            # (a plain array index that says nothing about traversal history).
            # Current, actionable state -- not historical audit trivia -- since
            # it directly gates which candidates transfers._engine_recovery
            # .TransferEngine._next_alternate_index treats as still eligible.
            "candidate_attempt_history": [],
        }
        state_row = await db.fetchone(
            "SELECT * FROM artifact_recovery_state WHERE artifact_id=?", (artifact_id,),
        )
        if state_row:
            stored = dict(state_row)
            history = stored.get("candidate_attempt_history")
            if isinstance(history, str):
                try:
                    history = codec.load(history, [])
                except (TypeError, ValueError):
                    history = []
            stored["candidate_attempt_history"] = history if isinstance(history, list) else []
            # Import EVERY stored key, not only this layer's own template keys
            # -- the phase3/audit layers below rely on this single read
            # already carrying their own fields (via ``setdefault``) instead
            # of each re-querying the same row again (Section 20: this was
            # three redundant reads/decodes of the same row before leveling).
            # This row holds ONLY current/policy-relevant facts (Section 14) --
            # no historical pocket in any shape. Historical facts are never
            # merged in here; ``recovery_context()`` below merges them in
            # separately, read-only, from the sparse audit trail, for its own
            # (non-policy) callers.
            snapshot.update({
                key: value for key, value in stored.items()
                if key not in {"artifact_id", "transfer_id", "updated_at"}
            })
        # Existing Phase-1 counters are known facts. If no current-state row
        # exists yet they seed only counters, never a fabricated signature/progress.
        snapshot["consecutive_no_progress_failures"] = max(
            int(snapshot.get("consecutive_no_progress_failures") or 0),
            int(row.get("recovery_failures") or 0),
        )
        snapshot["candidate_refreshes"] = max(
            int(snapshot.get("candidate_refreshes") or 0),
            int(row.get("recovery_refreshes") or 0),
        )
        return snapshot

    @classmethod
    async def _save_recovery_snapshot(cls, db, transfer_id: int, artifact_id: int, snapshot: dict) -> None:
        """Upsert the ONE canonical current-state row for this artifact (Section 14).

        This never appends -- an artifact has exactly one row, updated in
        place, so ordinary progress advancement (the highest-frequency
        caller, via ``execution()`` below) costs one bounded write instead of
        unbounded history growth. Semantically meaningful transitions
        additionally get a small, separate, sparse audit record via
        ``_append_recovery_audit`` (Section 18) -- this method alone is not
        the audit trail.
        """
        history = snapshot.get("candidate_attempt_history")
        columns = (
            "recovery_epoch", "progress_anchor", "consecutive_no_progress_failures",
            "failures_since_meaningful_progress", "failure_signature", "same_signature_failures",
            "candidate_refreshes", "candidate_switches", "decision_action", "decision_reason",
            "quiescence_reason", "wake_condition", "recovery_generation", "recovery_claim_token",
            "recovery_claim_trigger", "recovery_claim_until", "recovery_claim_id", "recovery_decision_id",
            "last_failure_identity", "last_refresh_decision_id", "refresh_inflight_decision_id",
            "refresh_inflight_attempt_id", "blocked_retry_at", "last_applied_action", "last_applied_reason",
        )
        values = {name: snapshot.get(name) for name in columns}
        values["version"] = max(3, int(snapshot.get("version") or 0))
        # DP 1.0.12 recovery leveling: phase3-layer fields (recovery_generation,
        # recovery_claim_until, blocked_retry_at) are NOT NULL-with-default
        # columns, but the lower/legacy transfers.repository.TransferRepository
        # stack (no phase3 layer applied) never populates them in its snapshot
        # dict at all -- an explicit SQL NULL would bypass the column DEFAULT
        # and violate the constraint, so coalesce here rather than assume every
        # caller's snapshot dict was built by the full production layer chain.
        for numeric_column in ("recovery_generation",):
            values[numeric_column] = int(values[numeric_column] or 0)
        for float_column in ("recovery_claim_until", "blocked_retry_at"):
            values[float_column] = float(values[float_column] or 0.0)
        values["candidate_attempt_history"] = codec.dump(history if isinstance(history, list) else [])
        insert_cols = ["artifact_id", "transfer_id", "version", "candidate_attempt_history", *columns]
        placeholders = ",".join("?" for _ in insert_cols)
        update_assignments = ",".join(
            f"{name}=excluded.{name}" for name in ["transfer_id", "version", "candidate_attempt_history", *columns]
        )
        params = [artifact_id, transfer_id, values["version"], values["candidate_attempt_history"]]
        params.extend(values[name] for name in columns)
        await db.execute(
            f"""INSERT INTO artifact_recovery_state({','.join(insert_cols)}) VALUES({placeholders})
                ON CONFLICT(artifact_id) DO UPDATE SET {update_assignments},updated_at=CURRENT_TIMESTAMP""",
            tuple(params),
        )

    @staticmethod
    async def _append_recovery_audit(db, transfer_id: int, artifact_id: int, transition: str, **fields) -> None:
        """Sparse, semantically-meaningful recovery audit trail (Section 18).

        Distinct from the current-state row above: this only grows for
        discrete transitions named by ``transition`` (claim, decision,
        candidate activation, refresh, execution retirement, quiescence
        entry/exit, generation/epoch advancement, terminal recovery, operator
        retry, operator source switch) -- never for ordinary byte-progress
        advancement, which updates only the current-state row. One shared
        ``kind`` ("recovery_audit") keeps every transition orderable/
        queryable together; ``transition`` inside ``detail`` distinguishes
        the kind of event.
        """
        detail = {"artifact_id": int(artifact_id), "transition": str(transition), **fields}
        await db.execute(
            "INSERT INTO application_events(transfer_id,kind,detail,claimed) VALUES(?,?,?,1)",
            (int(transfer_id), "recovery_audit", codec.dump(detail)),
        )

    @classmethod
    async def _historical_audit_facts(cls, db, artifact_id: int, transfer_id: int) -> dict:
        """Reconstruct historical/explainability facts read-only, ON DEMAND,
        from the sparse ``recovery_audit`` trail (Section 15): these facts
        live SOLELY in the append-only audit log -- never persisted back as
        a mutable "current" copy of any shape, current-state-adjacent or
        otherwise. Cheap to scan because the trail is genuinely sparse
        (bounded by real transitions, never by poll frequency, per Section
        18/20). Newest-row-wins per field, except ``candidate_generation``,
        which is a count of ``finish_claim`` transitions that actually
        changed the candidate -- reconstructed fresh each call rather than
        maintained as a persisted running counter.
        """
        result: dict = {key: None for key in _HISTORICAL_SNAPSHOT_KEYS}
        remaining = set(_HISTORICAL_SNAPSHOT_KEYS) - {"candidate_generation"}
        candidate_generation = 0
        rows = await db.fetchall(
            "SELECT detail FROM application_events WHERE transfer_id=? AND kind='recovery_audit' ORDER BY id DESC",
            (transfer_id,),
        )
        for row in rows:
            try:
                detail = codec.load(row["detail"], {})
            except (TypeError, ValueError):
                continue
            if not isinstance(detail, dict) or int(detail.get("artifact_id") or -1) != int(artifact_id):
                continue
            if detail.get("transition") == "finish_claim" and detail.get("candidate_changed"):
                candidate_generation += 1
            if not remaining:
                continue
            for key in list(remaining):
                if key in detail:
                    result[key] = detail[key]
                    remaining.discard(key)
        result["candidate_generation"] = candidate_generation
        return result

    async def recovery_context(self, artifact_id: int) -> dict:
        """Return durable recovery/accounting facts without inventing legacy
        history. Historical/explainability facts (Section 15) are merged in
        here, live, from the sparse audit trail -- this is the one place
        that reconstruction happens; ``_recovery_snapshot()`` itself (read
        directly by every fencing/traversal/reset policy decision elsewhere
        in this file and its layered subclasses) never sees them.
        """
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (artifact_id,),
            )
            if not row:
                raise KeyError(artifact_id)
            snapshot = await self._recovery_snapshot(db, artifact_id, row=row)
            historical = await self._historical_audit_facts(db, artifact_id, int(row["torrent_id"]))
            attempts = await db.fetchone(
                "SELECT COUNT(*) AS n FROM execution_attempt_provenance WHERE artifact_id=?",
                (artifact_id,),
            )
        snapshot.update(historical)
        snapshot["execution_attempts"] = int((attempts or {}).get("n") or 0)
        return snapshot

    async def record_recovery_decision(self, artifact_id: int, action: str, reason: str) -> None:
        """Append the core-owned decision/reason without rewriting factual history."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (artifact_id,),
            )
            if not row:
                await db.rollback()
                raise KeyError(artifact_id)
            snapshot = await self._recovery_snapshot(db, artifact_id, row=row)
            snapshot["decision_action"] = str(action)
            snapshot["decision_reason"] = str(reason)
            await self._save_recovery_snapshot(db, int(row["torrent_id"]), artifact_id, snapshot)
            await self._append_recovery_audit(
                db, int(row["torrent_id"]), artifact_id, "decision", action=str(action), reason=str(reason),
            )
            await db.commit()

    async def record_candidate_attempt(self, artifact_id: int, *candidate_ids: str) -> None:
        """Durably mark one or more candidate ids as attempted in the current
        recovery episode (DP 1.0.12 recovery leveling, Section 12).

        Idempotent set-union, never a duplicate/blind append.
        transfers._engine_recovery.TransferEngine._next_alternate_index reads
        this instead of assuming ``selected + 1`` means "never tried". Cleared
        only by an explicit full reset (recovery_repository.TransferRepository
        .reset_retry_budget, the live USER_RETRY mechanism) -- an operator
        candidate switch only ever adds to it.
        """
        wanted = {str(item) for item in candidate_ids if item is not None}
        if not wanted:
            return
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (artifact_id,),
            )
            if not row:
                await db.rollback()
                raise KeyError(artifact_id)
            snapshot = await self._recovery_snapshot(db, artifact_id, row=row)
            history = snapshot.get("candidate_attempt_history")
            existing = list(history) if isinstance(history, list) else []
            merged = existing + [item for item in sorted(wanted) if item not in existing]
            if merged != existing:
                snapshot["candidate_attempt_history"] = merged
                await self._save_recovery_snapshot(db, int(row["torrent_id"]), artifact_id, snapshot)
                added = [item for item in merged if item not in existing]
                await self._append_recovery_audit(
                    db, int(row["torrent_id"]), artifact_id, "candidate_attempt",
                    added=added, history=merged,
                )
            await db.commit()

    @staticmethod
    def build_candidate_activation_detail(
        *, transfer_id: int, artifact_id: int, old_candidate, new_candidate,
        authority: str, recovery_generation: int | None, old_execution_id: str | None,
        partial_decision: str, admission_decision: str, outcome: str,
    ) -> dict:
        """The one candidate-activation provenance shape (DP 1.0.12 recovery
        leveling, Section 29), shared by both write paths: the atomic
        in-transaction write ``transition_recovery(activation_provenance=...)``
        performs for a COMMITTED activation, and the standalone
        ``record_candidate_activation`` below for a REJECTED one (nothing was
        durably committed in that case, so there is no commit transaction to
        piggyback on)."""
        return {
            "transfer_id": int(transfer_id),
            "artifact_id": int(artifact_id),
            "authority": str(authority),
            "recovery_generation": recovery_generation,
            "old_candidate_id": str(old_candidate.id) if old_candidate is not None else None,
            "old_provider_id": str(old_candidate.provider_id or "") if old_candidate is not None else None,
            "new_candidate_id": str(new_candidate.id) if new_candidate is not None else None,
            "new_provider_id": str(new_candidate.provider_id or "") if new_candidate is not None else None,
            "old_execution_id": old_execution_id,
            # Not knowable at commit time -- the replacement execution does
            # not exist yet. transfers._repository_base.TransferRepository
            # .prepare_execution links it in-place the first time this
            # artifact actually dispatches afterward (Section 29).
            "new_execution_id": None,
            "partial_decision": str(partial_decision),
            "admission_decision": str(admission_decision),
            "outcome": str(outcome),
        }

    async def record_candidate_activation(
        self, *, transfer_id: int, artifact_id: int, old_candidate, new_candidate,
        authority: str, recovery_generation: int | None, old_execution_id: str | None,
        partial_decision: str, admission_decision: str, outcome: str,
    ) -> None:
        """Standalone provenance write for a REJECTED activation (DP 1.0.12
        recovery leveling, Section 29) -- nothing was durably committed, so
        there is no commit transaction to atomically piggyback the record on,
        unlike the committed path (see ``transition_recovery``'s
        ``activation_provenance`` parameter, used instead for that case)."""
        detail = self.build_candidate_activation_detail(
            transfer_id=transfer_id, artifact_id=artifact_id, old_candidate=old_candidate,
            new_candidate=new_candidate, authority=authority, recovery_generation=recovery_generation,
            old_execution_id=old_execution_id, partial_decision=partial_decision,
            admission_decision=admission_decision, outcome=outcome,
        )
        async with get_db() as db:
            await db.execute(
                "INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,?,?)",
                (int(transfer_id), "candidate_activation", codec.dump(detail)),
            )
            await db.commit()

    async def record_source_failure(self, artifact_id: int, error=None) -> tuple[int, int]:
        """Consume one factual no-progress failure in the current recovery epoch."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (artifact_id,),
            )
            if not row:
                await db.rollback()
                raise KeyError(artifact_id)
            snapshot = await self._recovery_snapshot(db, artifact_id, row=row)
            cursor = await db.execute(
                "UPDATE download_files SET recovery_failures=recovery_failures+1 WHERE id=?",
                (artifact_id,),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise KeyError(artifact_id)
            failures = int(row.get("recovery_failures") or 0) + 1
            snapshot["consecutive_no_progress_failures"] = failures
            snapshot["failures_since_meaningful_progress"] = int(
                snapshot.get("failures_since_meaningful_progress") or 0
            ) + 1
            if isinstance(error, NormalizedError):
                signature = failure_signature(error)
                snapshot["same_signature_failures"] = (
                    int(snapshot.get("same_signature_failures") or 0) + 1
                    if snapshot.get("failure_signature") == signature else 1
                )
                snapshot["failure_signature"] = signature
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), artifact_id, snapshot,
            )
            await self._append_recovery_audit(
                db, int(row["torrent_id"]), artifact_id, "source_failure",
                failures=failures, same_signature_failures=snapshot.get("same_signature_failures"),
            )
            await db.commit()
        return failures, int(row.get("recovery_refreshes") or 0)

    async def consume_recovery_refresh(self, artifact_id: int) -> bool:
        """Consume one refresh in this recovery epoch and persist the accounting."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (artifact_id,),
            )
            if not row:
                await db.rollback()
                return False
            snapshot = await self._recovery_snapshot(db, artifact_id, row=row)
            if int(row.get("recovery_refreshes") or 0) >= 1:
                await db.rollback()
                return False
            cursor = await db.execute(
                "UPDATE download_files SET recovery_refreshes=recovery_refreshes+1 WHERE id=? AND recovery_refreshes<1",
                (artifact_id,),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            snapshot["candidate_refreshes"] = int(snapshot.get("candidate_refreshes") or 0) + 1
            await self._save_recovery_snapshot(db, int(row["torrent_id"]), artifact_id, snapshot)
            await self._append_recovery_audit(
                db, int(row["torrent_id"]), artifact_id, "refresh",
                candidate_refreshes=snapshot["candidate_refreshes"],
            )
            await db.commit()
        return True

    async def reset_source_recovery(self, artifact_id: int) -> None:
        """Explicitly reset bounded counters without manufacturing progress/epoch."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (artifact_id,),
            )
            if not row:
                await db.rollback()
                raise KeyError(artifact_id)
            snapshot = await self._recovery_snapshot(db, artifact_id, row=row)
            await db.execute(
                "UPDATE download_files SET recovery_failures=0,recovery_refreshes=0 WHERE id=?",
                (artifact_id,),
            )
            apply_recovery_reset(snapshot, RecoveryResetAuthority.SOURCE_RESET)
            await self._save_recovery_snapshot(db, int(row["torrent_id"]), artifact_id, snapshot)
            await self._append_recovery_audit(db, int(row["torrent_id"]), artifact_id, "source_reset")
            await db.commit()

    async def reset_retry_budget(self, artifact_id):
        """Operator retry clears exhaustion but is not counted as forward progress."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (artifact_id,),
            )
            if not row:
                await db.rollback()
                raise KeyError(artifact_id)
            snapshot = await self._recovery_snapshot(db, artifact_id, row=row)
            await db.execute(
                "UPDATE download_files SET retry_count=0,recovery_failures=0,recovery_refreshes=0 WHERE id=?",
                (artifact_id,),
            )
            apply_recovery_reset(snapshot, RecoveryResetAuthority.OPERATOR_RETRY)
            await self._save_recovery_snapshot(db, int(row["torrent_id"]), artifact_id, snapshot)
            await self._append_recovery_audit(db, int(row["torrent_id"]), artifact_id, "operator_retry")
            await db.commit()

    async def execution_idle_seconds(self, observation, now):
        """Activity clock: byte movement resets stall time, not recovery epochs."""
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT state,progress,progress_at FROM execution_attempts WHERE id=?",
                (observation.handle.attempt_id,),
            )
            if not row:
                return 0
            previous = TransferProgress(**codec.load(row["progress"], {}))
            active = observation.state == ExecutionState.TRANSFERRING and observation.error is None
            changed = previous.completed_bytes != observation.progress.completed_bytes or row["state"] != observation.state
            if row["progress_at"] is None or not active or changed:
                await db.execute(
                    "UPDATE execution_attempts SET progress_at=? WHERE id=?",
                    (now, observation.handle.attempt_id),
                )
                await db.commit()
                return 0
            return max(0, now - row["progress_at"])

    async def collection_route_provider(self, transfer_id: int) -> str | None:
        async with get_db() as db:
            row = await db.fetchone("SELECT collection_route_provider_id FROM torrents WHERE id=?", (transfer_id,))
        value = str((row or {}).get("collection_route_provider_id") or "").strip()
        return value or None

    async def bind_collection_route(self, transfer_id: int, provider_id: str) -> str | None:
        provider_id = str(provider_id or "").strip()
        if not provider_id:
            raise ValueError("Collection route provider identity is required")
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            parent = await db.fetchone("SELECT source,collection_route_provider_id FROM torrents WHERE id=?", (transfer_id,))
            if not parent or str(parent.get("source") or "") != "direct_link":
                await db.rollback(); return None
            existing = str(parent.get("collection_route_provider_id") or "").strip()
            if existing:
                await db.rollback(); return existing
            roots = await db.fetchone("SELECT COUNT(*) AS count FROM transfer_requests WHERE transfer_id=? AND parent_id IS NULL", (transfer_id,))
            if int((roots or {}).get("count") or 0) <= 1:
                await db.rollback(); return None
            routed = await db.fetchone("""SELECT 1 AS present FROM resolution_attempts a JOIN transfer_requests r ON r.id=a.request_id WHERE r.transfer_id=? LIMIT 1""", (transfer_id,))
            if routed:
                await db.rollback(); return None
            cursor = await db.execute("UPDATE torrents SET collection_route_provider_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND collection_route_provider_id IS NULL", (provider_id, transfer_id))
            if cursor.rowcount != 1:
                current = await db.fetchone("SELECT collection_route_provider_id FROM torrents WHERE id=?", (transfer_id,))
                await db.rollback()
                value = str((current or {}).get("collection_route_provider_id") or "").strip()
                return value or None
            await db.commit()
        return provider_id

    async def bound_route_provider(self, request_id: str) -> str | None:
        routed = await super().bound_route_provider(request_id)
        if routed:
            return routed
        async with get_db() as db:
            row = await db.fetchone("""SELECT t.collection_route_provider_id FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id WHERE r.id=?""", (request_id,))
        value = str((row or {}).get("collection_route_provider_id") or "").strip()
        return value or None

    async def accept_execution_total(self, artifact_id: int, handle, total_bytes: int) -> bool:
        if (not isinstance(total_bytes, int) or isinstance(total_bytes, bool) or total_bytes <= 0 or handle is None):
            return False
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT f.size_bytes,f.execution_attempt_id,e.executor_id,e.handle FROM download_files f LEFT JOIN execution_attempts e ON e.id=f.execution_attempt_id WHERE f.id=?""", (artifact_id,))
            if (not row or row.get("execution_attempt_id") != handle.attempt_id or row.get("executor_id") != handle.executor_id or codec.load(row.get("handle")) != codec.load(codec.dump(handle)) or int(row.get("size_bytes") or 0) > 0):
                await db.rollback(); return False
            cursor = await db.execute("UPDATE download_files SET size_bytes=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND execution_attempt_id=? AND COALESCE(size_bytes,0)<=0", (total_bytes, artifact_id, handle.attempt_id))
            await db.commit()
        return cursor.rowcount == 1

    async def execution(self, observation) -> None:
        """Persist execution evidence and reset recovery only on meaningful progress."""
        handle = observation.handle
        accepted_total = None
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("SELECT * FROM execution_attempts WHERE id=?", (handle.attempt_id,))
            if not row or codec.load(row["handle"]) != codec.load(codec.dump(handle)):
                await db.rollback()
                raise TransferError(NormalizedError(Domain.LIFECYCLE, Category.OWNERSHIP_CONFLICT, Stage.RECONCILIATION))
            if (not bool(row.get("authorized")) and row.get("state") in _TERMINAL_EXECUTION_STATES):
                await db.rollback()
                return
            previous = TransferProgress(**codec.load(row["progress"], {}))
            artifact = await db.fetchone(
                "SELECT id,torrent_id,size_bytes,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (row["artifact_id"],),
            )
            if not artifact:
                await db.rollback()
                raise KeyError(row["artifact_id"])
            snapshot = await self._recovery_snapshot(db, int(artifact["id"]), row=artifact)
            initialized = snapshot.get("progress_anchor") is not None
            if not initialized:
                snapshot["progress_anchor"] = int(previous.completed_bytes or 0)
            completed = int(observation.progress.completed_bytes or 0)
            if completed > int(previous.completed_bytes or 0):
                anchor = int(snapshot.get("progress_anchor") or 0)
                threshold = meaningful_progress_threshold(int(artifact.get("size_bytes") or 0))
                meaningful = completed - anchor >= threshold
                if meaningful:
                    await db.execute(
                        "UPDATE download_files SET recovery_failures=0,recovery_refreshes=0 WHERE id=?",
                        (artifact["id"],),
                    )
                    apply_recovery_reset(snapshot, RecoveryResetAuthority.MEANINGFUL_PROGRESS)
                    snapshot["progress_anchor"] = completed
            else:
                meaningful = False
            # DP 1.0.12 recovery leveling, Section 14/18/20 (post-review
            # correction): recovery-state is written ONLY for (a) the one-
            # time-per-artifact initialization that durably anchors
            # progress_anchor (needed so a later observation, even after a
            # process restart, can still tell whether meaningful progress
            # has occurred since), or (b) an actual meaningful-progress
            # epoch advance. Ordinary byte advancement that does not cross
            # the threshold causes ZERO writes to artifact_recovery_state --
            # not merely a bounded/upserted write, none at all (Section 18's
            # named regression,
            # test_progress_observations_do_not_append_full_recovery_snapshot_each_tick).
            # Byte progress itself is still durably persisted every call, in
            # execution_attempts below -- that is its correct, always-was-
            # correct home (Section 18: "ordinary byte progress belongs in
            # execution progress state, not a full recovery-history snapshot").
            if not initialized or meaningful:
                await self._save_recovery_snapshot(db, int(artifact["torrent_id"]), int(artifact["id"]), snapshot)
            if meaningful:
                await self._append_recovery_audit(
                    db, int(artifact["torrent_id"]), int(artifact["id"]), "meaningful_progress",
                    recovery_epoch=snapshot.get("recovery_epoch"), progress_anchor=snapshot.get("progress_anchor"),
                )

            error = codec.dump(observation.error) if observation.error else None
            revoked = observation.error is not None and observation.error.category == Category.OWNERSHIP_CONFLICT
            await db.execute("""UPDATE execution_attempts SET state=?,progress=?,error=?,authorized=CASE WHEN ? THEN 0 ELSE authorized END,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                             (observation.state, codec.dump(observation.progress), error, revoked, handle.attempt_id))
            await db.execute("UPDATE execution_attempt_provenance SET outcome=?,updated_at=CURRENT_TIMESTAMP WHERE execution_attempt_id=?",
                             (self._execution_outcome(observation.state), handle.attempt_id))
            states = {ExecutionState.TRANSFERRING: "downloading", ExecutionState.QUEUED: "queued", ExecutionState.PAUSED: "paused",
                      ExecutionState.SUCCEEDED: "verifying", ExecutionState.FAILED: "error", ExecutionState.CANCELLED: "cancelled",
                      ExecutionState.ABSENT: "lost", ExecutionState.UNKNOWN: "unknown"}
            await db.execute("""UPDATE download_files SET status=?,normalized_error=?,updated_at=CURRENT_TIMESTAMP WHERE execution_attempt_id=? AND torrent_id IN (SELECT id FROM torrents WHERE status NOT IN ('deleted','consolidated','cancelled'))""",
                             (states[observation.state], error, handle.attempt_id))
            total = observation.progress.total_bytes
            credible = (
                observation.state in _RUNTIME_TOTAL_STATES
                and isinstance(total, int) and not isinstance(total, bool) and total > 0
                and isinstance(observation.progress.completed_bytes, int)
                and not isinstance(observation.progress.completed_bytes, bool)
                and 0 <= observation.progress.completed_bytes <= total
                and int(artifact.get("size_bytes") or 0) <= 0
            )
            if credible:
                accepted_total = (int(artifact["id"]), total)
            await db.commit()
        if accepted_total:
            await self.accept_execution_total(accepted_total[0], handle, accepted_total[1])

    async def refine_execution_total(self, artifact_id: int, handle, total_bytes: int) -> bool:
        if (not isinstance(total_bytes, int) or isinstance(total_bytes, bool) or total_bytes < 0 or handle is None):
            return False
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT f.size_bytes,f.execution_attempt_id,f.torrent_id,e.executor_id,e.handle,e.state,e.candidate FROM download_files f JOIN execution_attempts e ON e.id=f.execution_attempt_id WHERE f.id=?""", (artifact_id,))
            candidate = None
            if row and row.get("candidate"):
                try:
                    candidate = codec.candidate(codec.load(row["candidate"]))
                except (TypeError, ValueError, KeyError):
                    candidate = None
            if (not row or row.get("execution_attempt_id") != handle.attempt_id or row.get("executor_id") != handle.executor_id or codec.load(row.get("handle")) != codec.load(codec.dump(handle)) or row.get("state") != "succeeded" or candidate is None or candidate.expected_bytes > 0):
                await db.rollback(); return False
            previous = int(row.get("size_bytes") or 0)
            cursor = await db.execute("UPDATE download_files SET size_bytes=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND execution_attempt_id=?", (total_bytes, artifact_id, handle.attempt_id))
            if cursor.rowcount and previous != total_bytes:
                await db.execute("INSERT INTO events(torrent_id,level,message) VALUES(?,'warning','Final verified materialization refined execution-observed artifact size')", (row["torrent_id"],))
            await db.commit()
        return cursor.rowcount == 1

    async def transition_recovery(self, artifact_id: int, state: str, *, error=None,
                                  retry_at: float = 0, selected: int | None = None,
                                  expected_bytes: int | None = None,
                                  reset_budget: bool = False,
                                  quiescence_reason: str | None = None,
                                  wake_condition: str | None = None,
                                  clear_quiescence: bool = False,
                                  candidate_switched: bool = False,
                                  continuation_reservation_until: float | None = None,
                                  activation_provenance: dict | None = None) -> bool:
        """Atomically revoke terminal writer authority and persist recovery state.

        DP 1.0.12 recovery leveling, Section 13: ``continuation_reservation_until``
        is the ONLY way to durably hold a continuation-admission reservation
        (transfers.candidate_activation.activate_candidate's commit, when the
        writer it just retired was genuinely occupying a live slot). Every
        other caller implicitly releases any reservation this artifact might
        still be holding, by leaving the parameter at its default -- a
        candidate switch is the sole transition allowed to carry one forward.

        Section 26/29: ``activation_provenance``, when given, is written as
        the durable ``candidate_activation`` audit record in this SAME
        transaction as the candidate-selection commit itself -- not a
        separate post-commit INSERT. This makes "the switch committed but its
        provenance was lost" structurally impossible for a committed
        activation: either both persist together, or (on any failure) neither
        does and ``committed`` is correctly ``False``.
        """
        if expected_bytes is not None and expected_bytes < 0:
            return False
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT f.*,t.status AS transfer_status FROM download_files f JOIN torrents t ON t.id=f.torrent_id WHERE f.id=?""", (artifact_id,))
            if not row or row["transfer_status"] in {"deleted", "completed", "consolidated", "cancelled"}:
                await db.rollback(); return False
            current_id = row.get("execution_attempt_id")
            if current_id is not None:
                current = await db.fetchone("SELECT state,authorized FROM execution_attempts WHERE id=? AND artifact_id=?", (current_id, artifact_id))
                if not current or current["state"] not in _TERMINAL_EXECUTION_STATES:
                    await db.rollback(); return False
                await db.execute("UPDATE execution_attempts SET authorized=0,updated_at=CURRENT_TIMESTAMP WHERE id=?", (current_id,))
            placeholders = ",".join("?" for _ in _MUTATING_EXECUTION_STATES)
            active = await db.fetchone(f"SELECT id FROM execution_attempts WHERE artifact_id=? AND authorized=1 AND state IN ({placeholders}) LIMIT 1", (artifact_id, *_MUTATING_EXECUTION_STATES))
            if active:
                await db.rollback(); return False

            snapshot = await self._recovery_snapshot(db, artifact_id, row=row)
            assignments = [
                "status=?", "normalized_error=?", "retry_at=?", "execution_attempt_id=NULL",
                "continuation_reservation_expires_at=?", "updated_at=CURRENT_TIMESTAMP",
            ]
            params = [state, codec.dump(error) if error else None, retry_at, continuation_reservation_until]
            if selected is not None:
                assignments.append("selected_candidate=?"); params.append(selected)
            if expected_bytes is not None:
                assignments.append("size_bytes=?"); params.append(expected_bytes)
            if reset_budget:
                assignments.extend(["retry_count=0", "recovery_failures=0", "recovery_refreshes=0"])
                apply_recovery_reset(snapshot, RecoveryResetAuthority.CANDIDATE_ACTIVATION_BUDGET)
            if candidate_switched:
                assignments.extend(["recovery_failures=0", "recovery_refreshes=0"])
                apply_recovery_reset(snapshot, RecoveryResetAuthority.CANDIDATE_SWITCHED)
            if clear_quiescence:
                snapshot["quiescence_reason"] = None; snapshot["wake_condition"] = None
            if quiescence_reason is not None:
                snapshot["quiescence_reason"] = str(quiescence_reason)
                snapshot["wake_condition"] = str(wake_condition or "") or None
            params.append(artifact_id)
            cursor = await db.execute(f"UPDATE download_files SET {','.join(assignments)} WHERE id=?", tuple(params))
            if cursor.rowcount:
                await self._save_recovery_snapshot(db, int(row["torrent_id"]), artifact_id, snapshot)
                if activation_provenance is not None:
                    await db.execute(
                        "INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,?,?)",
                        (int(row["torrent_id"]), "candidate_activation", codec.dump(activation_provenance)),
                    )
                elif candidate_switched:
                    # Defensive fallback (Section 18/29): the canonical committed
                    # path (transfers.candidate_activation.activate_candidate)
                    # always supplies activation_provenance, whose atomic INSERT
                    # above is already the meaningful audit record for this
                    # transition. A non-canonical caller that sets
                    # candidate_switched without activation_provenance still
                    # gets a minimal sparse audit row rather than none at all.
                    await self._append_recovery_audit(
                        db, int(row["torrent_id"]), artifact_id, "candidate_switched",
                        candidate_switches=snapshot.get("candidate_switches"), selected=selected,
                    )
                if reset_budget:
                    await self._append_recovery_audit(db, int(row["torrent_id"]), artifact_id, "operator_retry")
            await db.commit()
        return cursor.rowcount == 1

    async def _candidate_presentation(self, transfer_id: int) -> dict[int, dict]:
        async with get_db() as db:
            files = await db.fetchall("""SELECT id,candidates,selected_candidate,execution_attempt_id,status FROM download_files WHERE torrent_id=? AND request_id IS NOT NULL AND COALESCE(blocked,0)=0 AND COALESCE(mirror_state,'')!='standby' ORDER BY id""", (transfer_id,))
            artifact_ids = [int(row["id"]) for row in files]
            if not artifact_ids:
                return {}
            placeholders = ",".join("?" for _ in artifact_ids)
            bindings = await db.fetchall(f"""SELECT canonical_artifact_id,candidate_id,provider_id,source_scope,source_key,role,candidate_order FROM canonical_candidate_bindings WHERE canonical_artifact_id IN ({placeholders}) ORDER BY canonical_artifact_id,candidate_order,id""", tuple(artifact_ids))
            attempts = await db.fetchall(f"""SELECT p.artifact_id,p.candidate_id,p.outcome,p.delivered,p.ordinal,p.execution_attempt_id,e.state,e.authorized FROM execution_attempt_provenance p LEFT JOIN execution_attempts e ON e.id=p.execution_attempt_id WHERE p.artifact_id IN ({placeholders}) ORDER BY p.artifact_id,p.ordinal,p.execution_attempt_id""", tuple(artifact_ids))
        selected_ids = {}; current_attempt_ids = {}; has_durable_candidate = {}; artifact_states = {}
        for row in files:
            artifact_id = int(row["id"]); selected_id = None
            artifact_states[artifact_id] = str(row.get("status") or "").strip().lower()
            try:
                candidates = [codec.candidate(value) for value in codec.load(row.get("candidates"), [])]
                has_durable_candidate[artifact_id] = bool(candidates)
                selected = int(row.get("selected_candidate") or 0)
                if 0 <= selected < len(candidates): selected_id = str(candidates[selected].id)
            except (TypeError, ValueError, KeyError, IndexError):
                has_durable_candidate[artifact_id] = False
            selected_ids[artifact_id] = selected_id; current_attempt_ids[artifact_id] = row.get("execution_attempt_id")
        attempt_history = {}
        for row in attempts:
            candidate_id = str(row.get("candidate_id") or "").strip()
            if candidate_id: attempt_history.setdefault((int(row["artifact_id"]), candidate_id), []).append(dict(row))
        by_artifact = {}; group_by_artifact = {}; seen = {}
        for row in bindings:
            artifact_id = int(row["canonical_artifact_id"]); candidate_id = str(row["candidate_id"])
            if candidate_id in seen.setdefault(artifact_id, set()): continue
            seen[artifact_id].add(candidate_id); history = attempt_history.get((artifact_id, candidate_id), [])
            delivered = any(bool(item.get("delivered")) for item in history); latest = history[-1] if history else None
            failed = bool(latest and (str(latest.get("outcome") or "").strip().lower() in _FAILED_CANDIDATE_OUTCOMES or str(latest.get("state") or "").strip().lower() in _FAILED_CANDIDATE_OUTCOMES)) and not delivered
            selected = selected_ids.get(artifact_id) == candidate_id
            current = bool(latest and current_attempt_ids.get(artifact_id) == latest.get("execution_attempt_id"))
            active = bool(current and latest and bool(latest.get("authorized")) and str(latest.get("state") or "").strip().lower() in _MUTATING_EXECUTION_STATES)
            dispositions = []
            if delivered: dispositions.append("Delivering")
            elif failed: dispositions.append("Failed")
            elif active: dispositions.append("Active")
            elif selected: dispositions.append("Selected")
            by_artifact.setdefault(artifact_id, []).append({"candidate_id": candidate_id, "source_label": _safe_source_label(row.get("source_scope"), row.get("source_key")), "provider_id": str(row.get("provider_id") or "").strip() or None, "relationship": "Original" if row.get("role") == "canonical" else "Consolidated", "dispositions": dispositions, "is_selected": selected, "is_delivering": delivered,
                # Read-side, provider/candidate-neutral switch-presentation
                # eligibility (DP 1.0.12 Contextual Candidate Action Scope
                # task, §6.1/§10.1): the exact same backend-owned
                # _SWITCHABLE_ARTIFACT_STATES rule the host-scoped
                # ``source_candidates`` projection below already uses, so the
                # generic per-file candidate UI (ui-detail-candidates.js) can
                # consume it without recreating lifecycle policy in JS. False
                # for the selected candidate. Never a live provider/executor
                # check; the switch endpoint remains the sole mutation
                # authority and re-validates everything itself.
                "switch_eligible": (not selected) and artifact_states.get(artifact_id, "") in _SWITCHABLE_ARTIFACT_STATES})
            # Ungated per-artifact canonical host projection for the transfer-level
            # common-source group wrapper (never rendered by the per-file UI). Only
            # host-scoped candidates carry a group identity; ``switch_eligible``
            # mirrors the existing per-file semantics exactly.
            group_host = _group_source_host(row.get("source_scope"), row.get("source_key"))
            if group_host is not None:
                group_by_artifact.setdefault(artifact_id, []).append({
                    "source_host": group_host,
                    "candidate_id": candidate_id,
                    "is_selected": selected,
                    "switch_eligible": (not selected) and artifact_states.get(artifact_id, "") in _SWITCHABLE_ARTIFACT_STATES,
                })
        result = {}
        for artifact_id in artifact_ids:
            candidates = by_artifact.get(artifact_id, []); candidate_count = len(candidates)
            if candidate_count == 0 and has_durable_candidate.get(artifact_id, False): candidate_count = 1
            result[artifact_id] = {
                "candidate_count": candidate_count,
                "acquisition_candidates": candidates if candidate_count > 1 else [],
                "source_candidates": group_by_artifact.get(artifact_id, []),
            }
        return result

    async def presentation(self, transfer_id: int, details: bool = False, **_admission_facts):
        """``**_admission_facts`` (Section 9 live admission facts) are accepted
        and ignored here -- this repository layer predates and does not
        itself use them; only transfers.presentation_repository does. Callers
        (application.service, api.routes) do not need to know which concrete
        repository is wired in before supplying them."""
        result = await super().presentation(transfer_id, details=details)
        if not result or not details:
            return result
        candidate_projection = await self._candidate_presentation(transfer_id)
        for file_row in result.get("files", []):
            artifact_id = int(file_row.get("id") or 0)
            projection = candidate_projection.get(artifact_id)
            file_row["candidate_count"] = projection["candidate_count"] if projection else 0
            if projection is not None:
                # Present on exactly the group-eligible artifacts (physical,
                # unblocked, non-standby) — the set the common-source group
                # intersects over. Never rendered by the per-file candidate UI.
                file_row["source_candidates"] = projection["source_candidates"]
            if projection and projection["candidate_count"] > 1:
                file_row["acquisition_candidates"] = projection["acquisition_candidates"]
        return result

    # -------------------------------------------------------------------------
    # Universal file-selection manifest overlay (specification sections 13-38).
    #
    # The repository stores and atomically transitions durable facts; it never
    # sources wall time for product timing. Core passes ``now`` and absolute
    # deadlines derived from the injected engine clock. The
    # Confirm-vs-materialization race is serialized entirely by SQLite
    # ``BEGIN IMMEDIATE`` on the ``transfer_file_selections`` row; an in-memory
    # lock is never the correctness authority.
    #
    # Selection provenance follows the provider resource that produced the file
    # facts. Each row in ``transfer_file_selections`` is one selection
    # *generation* keyed by (request_id, provider_resource_id). A request that is
    # re-resolved onto a new provider resource gets a fresh generation; the prior
    # generation stays as historical truth and is never inherited or overwritten.
    # Every mutating/materializing operation binds to a specific generation, not
    # merely to the durable request id.
    # -------------------------------------------------------------------------

    @staticmethod
    async def _selection_generation(db, request_id: str, provider_resource_id: str):
        return await db.fetchone(
            "SELECT * FROM transfer_file_selections WHERE request_id=? AND provider_resource_id=?",
            (request_id, provider_resource_id),
        )

    async def selection_generation_exists(self, request_id: str, provider_resource_id: str) -> bool:
        """Whether a durable selection generation already exists for this
        (request, provider-resource binding).

        ``selection_mode`` gates only whether a NEW generation is *created*. Once
        a generation exists — including one persisted on a database that predates
        ``selection_mode``, whose owning request now deserializes with the
        default ``selection_mode="all"`` — that generation, not the request's
        current policy field, governs manifest recording, selection gating,
        Confirm/Close/timeout, and executable-manifest filtering. Every engine
        step past generation creation checks existence here, never the policy.
        """
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT 1 FROM transfer_file_selections WHERE request_id=? AND provider_resource_id=?",
                (request_id, provider_resource_id),
            )
        return row is not None

    async def transfer_has_selection_generation(self, transfer_id: int) -> bool:
        """Whether the transfer owns any selection generation (any binding).

        A transfer that already owns one was interactive; a re-resolution onto a
        new provider resource stays interactive and opens a fresh generation for
        the new binding (never inheriting the prior subset — specification
        section 13), regardless of the request's current/defaulted
        ``selection_mode``.
        """
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT 1 FROM transfer_file_selections WHERE transfer_id=? LIMIT 1", (transfer_id,))
        return row is not None

    @staticmethod
    async def _current_generation(db, transfer_id: int):
        """The transfer's current selection generation: the newest one.

        A re-resolution onto a new provider resource always creates a strictly
        newer generation, so the newest row is the live selector; older
        generations remain only as historical truth.
        """
        return await db.fetchone(
            """SELECT * FROM transfer_file_selections WHERE transfer_id=?
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (transfer_id,),
        )

    @classmethod
    async def _selection_by_manifest(cls, db, transfer_id: int, manifest_id: str):
        """Resolve the CURRENT selection generation only if it observed this
        manifest. ``manifest_id`` is UUIDv5(provider-resource-id : digest), so a
        stale browser tab holding an older generation's manifest id resolves to
        nothing here and the caller returns a stale-manifest conflict — never a
        re-interpretation against the newer generation.
        """
        current = await cls._current_generation(db, transfer_id)
        if current is not None and str(current["manifest_id"] or "") == str(manifest_id):
            return current
        return None

    @staticmethod
    def _selection_state(row, file_count: int, *, resource_available: bool | None = None) -> fs.SelectionWindowState:
        available_at = row["available_at"]
        if resource_available is None:
            # Read-model / offer-list callers do not consult a live provider
            # state; only ``evaluate_gate`` needs the fact and it is always
            # passed one explicitly. The provenance flag is a safe default.
            resource_available = bool(row["initially_available"])
        return fs.SelectionWindowState(
            decision=str(row["decision"]),
            initially_available=bool(row["initially_available"]),
            resource_available=bool(resource_available),
            available_grace_until=(
                float(available_at) + fs.POST_AVAILABLE_MANIFEST_GRACE_SECONDS
                if available_at is not None else None
            ),
            hold_until=row["hold_until"],
            manifest_id=row["manifest_id"],
            manifest_file_count=int(file_count),
            manifest_committed_at=row["manifest_committed_at"],
            auto_offer_dismissed_at=row["auto_offer_dismissed_at"],
        )

    @staticmethod
    async def _manifest_file_count(db, manifest_id) -> int:
        if not manifest_id:
            return 0
        row = await db.fetchone(
            "SELECT COUNT(*) AS n FROM transfer_file_manifest_entries WHERE manifest_id=?",
            (manifest_id,),
        )
        return int((row or {}).get("n") or 0)

    async def begin_file_selection_window(
        self, request_id: str, transfer_id: int, provider_resource_id: str,
        provider_id: str, *, initially_available: bool, now: float,
    ):
        """Idempotently open the durable file-selection generation for
        (request, provider resource).

        A different provider resource for the same durable request creates a new
        generation; it never overwrites the prior generation and never inherits
        its explicit subset.

        No submission-relative or creation-relative countdown is anchored here.
        A generation that is created while the resource is already AVAILABLE
        records ``available_at`` = ``now`` so the bounded post-AVAILABLE
        manifest-acquisition grace can start; a generation created while the
        resource is PREPARING leaves ``available_at`` NULL until the first
        AVAILABLE observation anchors it (see :meth:`file_selection_gate`). The
        factual initial-availability observation is captured once per
        generation. A later call, an application restart, or a re-resolution
        never resets any of these fields (``INSERT OR IGNORE``).
        """
        selection_id = fs.selection_identity(request_id, provider_resource_id)
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            parent = await db.fetchone("SELECT status FROM torrents WHERE id=?", (transfer_id,))
            if not parent or parent["status"] in {"deleted", "completed", "consolidated", "cancelled"}:
                await db.rollback()
                return None
            if not await db.fetchone("SELECT 1 FROM transfer_requests WHERE id=? AND transfer_id=?", (request_id, transfer_id)):
                await db.rollback()
                return None
            if not await db.fetchone("SELECT 1 FROM provider_resources WHERE id=?", (provider_resource_id,)):
                await db.rollback()
                return None
            # ``manifest_wait_until`` mirrors the post-AVAILABLE grace deadline:
            # ``0.0`` while the grace has not started, an absolute deadline once
            # it has. It is retired as a submission-relative window and is never
            # read by the gate; the gate reads ``available_at``.
            await db.execute(
                """INSERT OR IGNORE INTO transfer_file_selections(
                        id, request_id, transfer_id, provider_resource_id, provider_id,
                        initially_available, manifest_wait_until, available_at, created_at, updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (selection_id, request_id, transfer_id, provider_resource_id, str(provider_id),
                 int(bool(initially_available)),
                 fs.manifest_grace_deadline(now) if initially_available else 0.0,
                 now if initially_available else None, now, now),
            )
            row = await self._selection_generation(db, request_id, provider_resource_id)
            await db.commit()
        return row

    async def record_file_manifest(self, request_id: str, provider_resource_id: str, manifest, *, now: float):
        """Validate and persist a neutral early manifest, then bind it to the
        (request, provider resource) selection generation.

        Returns the canonical manifest on success, or ``None`` when there is no
        open generation for this resource, when mutation is already closed, or
        when the optional early manifest is malformed. A malformed early manifest
        is deliberately non-fatal before explicit confirmation: the selector
        stays unavailable and default ALL keeps governing the full transfer.
        """
        async with get_db() as db:
            sel = await self._selection_generation(db, request_id, provider_resource_id)
            if not sel or sel["manifest_committed_at"] is not None or sel["decision"] != "pending":
                return None
            try:
                canonical = fs.canonicalize_manifest(provider_resource_id, manifest)
            except fs.ManifestInvalid:
                return None
            await db.execute("BEGIN IMMEDIATE")
            sel = await self._selection_generation(db, request_id, provider_resource_id)
            if not sel or sel["manifest_committed_at"] is not None or sel["decision"] != "pending":
                await db.rollback()
                return None
            await db.execute(
                """INSERT OR IGNORE INTO transfer_file_manifests(
                        id, transfer_id, request_id, provider_resource_id, provider_id,
                        manifest_digest, observed_at)
                    VALUES(?,?,?,?,?,?,?)""",
                (canonical.manifest_id, sel["transfer_id"], request_id, provider_resource_id,
                 sel["provider_id"], canonical.manifest_digest, now),
            )
            for entry in canonical.entries:
                await db.execute(
                    """INSERT OR IGNORE INTO transfer_file_manifest_entries(
                            manifest_id, entry_id, ordinal, name, relative_path, expected_bytes)
                        VALUES(?,?,?,?,?,?)""",
                    (canonical.manifest_id, entry.entry_id, entry.ordinal, entry.name,
                     entry.relative_path, entry.expected_bytes),
                )
            assignments = ["manifest_id=?", "updated_at=?"]
            params = [canonical.manifest_id, now]
            # The 120-second user-decision hold begins in this same durable
            # transaction the moment the FIRST actionable multi-file manifest is
            # bound — whether the provider resource is PREPARING or AVAILABLE,
            # and regardless of how long provider preparation has taken
            # (specification sections 4-5, 12, 13, 19). It is anchored once to
            # this arrival and never restarted by a repeat observation, a
            # duplicate manifest, a scheduler pass, a browser reconnect, or a
            # restart. There is no submission-relative cutoff.
            hold_until = sel["hold_until"]
            if canonical.file_count > 1 and hold_until is None:
                hold_until = fs.decision_hold_deadline(now)
                assignments.append("hold_until=?")
                params.append(hold_until)
                # A usable manifest now exists: the post-AVAILABLE grace no
                # longer applies to this generation.
                assignments.append("manifest_wait_until=?")
                params.append(0.0)
            # An offer is queued (and the durable browser event emitted, once)
            # only when this manifest is genuinely auto-presentable now:
            # multi-file, not previously dismissed, with an active decision
            # hold. Repeated provider polls cannot re-queue it.
            bound_state = fs.SelectionWindowState(
                decision="pending", initially_available=bool(sel["initially_available"]),
                resource_available=bool(sel["initially_available"]),
                available_grace_until=None, hold_until=hold_until,
                manifest_id=canonical.manifest_id, manifest_file_count=canonical.file_count,
                manifest_committed_at=None, auto_offer_dismissed_at=sel["auto_offer_dismissed_at"],
            )
            queue_offer = sel["auto_offer_queued_at"] is None and fs.auto_offer_active(bound_state, now)
            if queue_offer:
                assignments.append("auto_offer_queued_at=?")
                params.append(now)
            params.append(sel["id"])
            await db.execute(
                f"UPDATE transfer_file_selections SET {','.join(assignments)} WHERE id=?",
                tuple(params),
            )
            if queue_offer:
                await db.execute(
                    "INSERT INTO application_events(transfer_id,kind,detail,claimed) VALUES(?,?,?,0)",
                    (sel["transfer_id"], "file_selection_available", None),
                )
            await db.commit()
        return canonical

    async def file_selection_gate(
        self, request_id: str, provider_resource_id: str, *, now: float,
        poll_interval: float | None = None, resource_available: bool | None = None,
    ) -> str:
        """Neutral gate: may executable child fan-out proceed for this
        (request, provider resource)?

        Gate authority and the scheduling of a wait produced by that gate are
        transactionally coupled (specification section 8). One ``BEGIN
        IMMEDIATE`` on the ``transfer_file_selections`` row:

        1. loads the exact selection generation;
        2. anchors the post-AVAILABLE manifest-acquisition grace exactly once,
           the first time the resource is observed AVAILABLE while no usable
           manifest and no decision hold exist;
        3. evaluates the pure neutral gate;
        4. settles a still-``pending`` timeout/single-file decision to durable
           ALL with a neutral reason;
        5. only for a genuine still-pending selection WAIT, and only when a
           ``poll_interval`` is supplied by the engine, persists the next
           selection-derived request retry time — never over a provider backoff
           (specification sections 9, 22), and never after the same
           transaction has just observed a settled EXPLICIT/ALL.

        A stale WAIT can therefore never recreate ``retry_at`` after Confirm /
        Close / timeout has settled the decision: a concurrent settle either
        commits first (this transaction then sees EXPLICIT/ALL and schedules
        nothing) or blocks on this row's write lock until this transaction
        commits and then releases the wait itself.

        ``resource_available`` is the live provider fact; the engine passes
        ``True`` from its AVAILABLE branch. When omitted it is read from
        ``provider_resources``.
        """
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await self._selection_generation(db, request_id, provider_resource_id)
            if not row:
                await db.rollback()
                return str(fs.SelectionGate.PROCEED)
            if resource_available is None:
                res = await db.fetchone(
                    "SELECT state FROM provider_resources WHERE id=?", (provider_resource_id,))
                resource_available = str((res or {}).get("state") or "").strip().lower() == "available"

            # (2) Anchor the 60s post-AVAILABLE manifest-acquisition grace once.
            # It never runs while PREPARING, and never applies once a usable
            # decision hold exists. Legacy pre-correction rows have
            # ``available_at`` NULL, so their grace also starts fresh here and a
            # stale submission-relative ``manifest_wait_until`` can never settle
            # ALL for an uncached transfer (correction section 14).
            if (resource_available and row["available_at"] is None
                    and str(row["decision"]) == "pending"
                    and row["manifest_committed_at"] is None
                    and row["hold_until"] is None):
                await db.execute(
                    "UPDATE transfer_file_selections "
                    "SET available_at=?, manifest_wait_until=?, updated_at=? "
                    "WHERE id=? AND available_at IS NULL",
                    (now, fs.manifest_grace_deadline(now), now, row["id"]),
                )
                row = await self._selection_generation(db, request_id, provider_resource_id)

            file_count = await self._manifest_file_count(db, row["manifest_id"])
            evaluation = fs.evaluate_gate(
                self._selection_state(row, file_count, resource_available=bool(resource_available)), now,
            )
            if (evaluation.resolve_decision is not None and row["decision"] == "pending"
                    and row["manifest_committed_at"] is None):
                await db.execute(
                    """UPDATE transfer_file_selections
                       SET decision=?, decision_reason=?, decision_at=?, updated_at=?
                       WHERE id=? AND decision='pending' AND manifest_committed_at IS NULL""",
                    (str(evaluation.resolve_decision), str(evaluation.resolve_reason), now, now, row["id"]),
                )
            elif (evaluation.gate != fs.SelectionGate.PROCEED and poll_interval is not None
                    and str(row["decision"]) == "pending" and row["manifest_committed_at"] is None):
                # (5) A genuine still-pending selection wait. Only a request that
                # is ``state='waiting' AND error IS NULL`` — the exclusive
                # signature of the file-selection gate wait / a benign PREPARING
                # re-poll (correction §9, §9a) — is rescheduled to the selection
                # poll cadence. A coexisting provider backoff always records a
                # non-null ``error`` and a longer ``retry_at``; it is left
                # entirely untouched (§9, §22): never shortened to a selection
                # cadence, never stripped of its failure evidence.
                await db.execute(
                    "UPDATE transfer_requests SET retry_at=? "
                    "WHERE id=? AND state='waiting' AND error IS NULL",
                    (float(now) + float(poll_interval), request_id),
                )
            await db.commit()
        return str(evaluation.gate)

    @staticmethod
    async def _release_selection_poll_wait(db, request_id: str, now: float) -> None:
        """Atomically end the scheduler wait that the file-selection gate created.

        §9a investigation — ``transfer_requests.retry_at`` is MULTI-PURPOSE. Two
        code paths set it forward on a request that ends up ``state='waiting'``:

          * the interactive file-selection gate wait (scheduled atomically
            inside :meth:`file_selection_gate` when the gate WAITs for a
            still-pending decision) and the generic ``poll_after()`` PREPARING
            re-poll cadence. Neither records an ``error``.
          * ``_repository_base.request_failure()`` (via
            ``_engine_base._request_failure(..., waiting=True)``) — a provider
            observation error / ABSENT / EXPIRED / reconciliation-exception
            backoff, whose delay is ``policy.retry_resolution(error).retry_at``.
            This path ALWAYS writes a non-null ``error`` blob and increments
            ``attempts``.

        A cross-transfer equivalence proof-retry also writes ``retry_at`` forward
        (``cohorts.py``: ``UPDATE ... retry_at=? WHERE id=? AND
        state='materializing'``) but only for ``state='materializing'`` rows,
        never ``state='waiting'``.

        The distinction the correction relies on is therefore
        ``state='waiting' AND error IS NULL``: the file-selection gate wait, and
        only it (or a benign PREPARING re-poll), leaves the request without an
        error. A provider backoff on the same request keeps its longer,
        legitimate ``retry_at`` because ``error IS NOT NULL``. The atomic gate
        transaction upholds this both ways: it releases only an
        ``error IS NULL`` selection wait, and it reschedules only an
        ``error IS NULL`` selection wait.
        """
        await db.execute(
            "UPDATE transfer_requests SET retry_at=? "
            "WHERE id=? AND state='waiting' AND error IS NULL AND retry_at > ?",
            (now, request_id, now),
        )

    async def confirm_file_selection(
        self, transfer_id: int, manifest_id: str, entry_ids, *, now: float,
    ) -> "fs.SelectionCommandResult":
        """Durably commit an explicit file subset, or lose the race to materialization.

        The selection generation is resolved from (transfer, manifest): the
        manifest id is bound to exactly one provider resource, so a stale browser
        tab holding an older manifest id cannot reach a newer generation. The
        ``BEGIN IMMEDIATE`` write lock on ``transfer_file_selections`` is the sole
        correctness authority for the Confirm-vs-materialization race. Confirm
        never reports success once the executable manifest is committed.
        """
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await self._selection_by_manifest(db, transfer_id, str(manifest_id))
            if not row:
                await db.rollback()
                return fs.SelectionCommandResult(str(fs.SelectionOutcome.CONFLICT), "stale_manifest")
            if row["manifest_committed_at"] is not None:
                await db.rollback()
                return fs.SelectionCommandResult(
                    str(fs.SelectionOutcome.CONFLICT), "materialization_committed",
                    decision=str(row["decision"]), manifest_id=row["manifest_id"], committed=True,
                )
            if str(row["decision"]) == "all":
                await db.rollback()
                return fs.SelectionCommandResult(
                    str(fs.SelectionOutcome.CONFLICT), "already_all",
                    decision="all", manifest_id=row["manifest_id"],
                )
            known = {
                r["entry_id"] for r in await db.fetchall(
                    "SELECT entry_id FROM transfer_file_manifest_entries WHERE manifest_id=?", (manifest_id,))
            }
            requested, seen = [], set()
            for value in (entry_ids or ()):
                text = str(value)
                if text in seen:
                    await db.rollback()
                    return fs.SelectionCommandResult(str(fs.SelectionOutcome.INVALID), "duplicate_selection")
                seen.add(text)
                requested.append(text)
            if not requested:
                await db.rollback()
                return fs.SelectionCommandResult(str(fs.SelectionOutcome.INVALID), "empty_selection")
            if len(requested) > fs.MAX_SELECTION_ENTRIES:
                await db.rollback()
                return fs.SelectionCommandResult(str(fs.SelectionOutcome.INVALID), "too_many_selected")
            if not seen.issubset(known):
                await db.rollback()
                return fs.SelectionCommandResult(str(fs.SelectionOutcome.INVALID), "unknown_selection_entry")
            existing = {
                r["entry_id"] for r in await db.fetchall(
                    "SELECT entry_id FROM transfer_file_selection_entries WHERE selection_id=?", (row["id"],))
            }
            if str(row["decision"]) == "explicit":
                if existing != seen:
                    await db.rollback()
                    return fs.SelectionCommandResult(
                        str(fs.SelectionOutcome.CONFLICT), "selection_superseded",
                        decision="explicit", manifest_id=row["manifest_id"],
                    )
                # Idempotent same-subset Confirm — self-heal a scheduler wait that
                # a crash/restart left in place after an earlier run persisted
                # EXPLICIT but before it released the file-selection gate wait
                # (§12). Never broadens the selection; still first-writer safe.
                await self._release_selection_poll_wait(db, str(row["request_id"]), now)
                await db.commit()
                return fs.SelectionCommandResult(
                    str(fs.SelectionOutcome.CONFIRMED), "idempotent",
                    decision="explicit", manifest_id=row["manifest_id"],
                )
            for entry_id in requested:
                await db.execute(
                    "INSERT OR IGNORE INTO transfer_file_selection_entries(selection_id, manifest_id, entry_id) VALUES(?,?,?)",
                    (row["id"], str(manifest_id), entry_id),
                )
            cursor = await db.execute(
                """UPDATE transfer_file_selections
                   SET decision='explicit', decision_reason=?, decision_at=?, updated_at=?
                   WHERE id=? AND decision='pending' AND manifest_committed_at IS NULL""",
                (str(fs.DecisionReason.CONFIRMED), now, now, row["id"]),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return fs.SelectionCommandResult(str(fs.SelectionOutcome.CONFLICT), "materialization_won")
            # The decision is settled; the 120s hold is no longer active. Release
            # the file-selection gate wait in the SAME transaction so the next
            # resolution cycle materialises the confirmed subset immediately —
            # without waiting out the old decision deadline or the last provider
            # poll timestamp. Only the selection-induced wait is released (§9a).
            await self._release_selection_poll_wait(db, str(row["request_id"]), now)
            await db.commit()
        return fs.SelectionCommandResult(
            str(fs.SelectionOutcome.CONFIRMED), "confirmed",
            decision="explicit", manifest_id=str(manifest_id),
        )

    async def dismiss_file_selection(
        self, transfer_id: int, manifest_id: str, *, now: float,
    ) -> "fs.SelectionCommandResult":
        """Record a Close/X. Releases an active cached hold immediately; otherwise
        leaves default ALL and keeps the decision mutable for later Details use."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await self._selection_by_manifest(db, transfer_id, str(manifest_id))
            if not row:
                await db.rollback()
                return fs.SelectionCommandResult(str(fs.SelectionOutcome.CONFLICT), "stale_manifest")
            if row["manifest_committed_at"] is not None:
                await db.rollback()
                return fs.SelectionCommandResult(
                    str(fs.SelectionOutcome.CONFLICT), "materialization_committed",
                    decision=str(row["decision"]), committed=True,
                )
            file_count = await self._manifest_file_count(db, row["manifest_id"])
            # Close/X on any live auto-presented multi-file hold settles ALL and
            # releases immediately, whatever the resource's initial availability
            # (specification section 6.5).
            active_hold = (
                file_count > 1
                and row["hold_until"] is not None and str(row["decision"]) == "pending"
            )
            if active_hold:
                cursor = await db.execute(
                    """UPDATE transfer_file_selections
                       SET decision='all', decision_reason=?, decision_at=?,
                           auto_offer_dismissed_at=?, updated_at=?
                       WHERE id=? AND decision='pending' AND manifest_committed_at IS NULL""",
                    (str(fs.DecisionReason.CLOSED), now, now, now, row["id"]),
                )
                if cursor.rowcount == 1:
                    # Close/X settled the decision to default ALL; the 120s hold
                    # is over. Release the file-selection gate wait in the same
                    # transaction so ALL materialisation proceeds immediately
                    # (§10). Only the selection-induced wait is released (§9a).
                    await self._release_selection_poll_wait(db, str(row["request_id"]), now)
            else:
                await db.execute(
                    """UPDATE transfer_file_selections
                       SET auto_offer_dismissed_at=?, updated_at=?
                       WHERE id=? AND manifest_committed_at IS NULL""",
                    (now, now, row["id"]),
                )
            await db.commit()
        return fs.SelectionCommandResult(
            str(fs.SelectionOutcome.DISMISSED), "closed_hold" if active_hold else "dismissed",
            decision="all" if active_hold else str(row["decision"]), manifest_id=str(manifest_id),
        )

    async def commit_selected_manifest(self, record, full_entries, *, now: float):
        """Filter the full executable manifest to the authorized subset and durably
        record the materialization-commit fact for this provider resource.

        The selection generation is bound to ``(record.id, record.resource.id)``:
        a replacement provider resource for the same durable request can never
        consume a prior resource's selection rows. Returns the authorized
        ``tuple[SourceEntry, ...]``:

        * no selection generation for this resource, or a settled ALL /
          still-pending decision -> the full provider list;
        * a confirmed EXPLICIT subset -> only the members proven to match the
          executable manifest by normalized relative path and compatible size.

        A confirmed explicit subset that can no longer be proven fails closed
        with a neutral ``RESOURCE_STATE_CONFLICT`` and never broadens to ALL.

        ``manifest_committed_at`` marks that core has *authorized* materialization
        for this generation and frozen mutation. The child-request fan-out
        (``repository.manifest``) is a following idempotent transaction; a crash
        between the two is recovered by the engine re-driving observation ->
        gate PROCEED -> this call (idempotent) -> fan-out (INSERT OR IGNORE).
        """
        full_entries = tuple(full_entries)
        canonical_resource_id = record.resource.id if record.resource is not None else None
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            binding_id = (
                await self._resolve_binding(db, record.transfer_id, canonical_resource_id)
                if canonical_resource_id else None
            )
            row = (
                await self._selection_generation(db, record.id, binding_id)
                if binding_id else None
            )
            if not row:
                await db.rollback()
                return full_entries
            already = row["manifest_committed_at"] is not None
            if str(row["decision"]) in ("pending", "all"):
                authorized = full_entries
                if not already:
                    reason = row["decision_reason"] or str(fs.DecisionReason.DEFAULT_MATERIALIZATION)
                    await db.execute(
                        """UPDATE transfer_file_selections
                           SET decision='all',
                               decision_reason=COALESCE(decision_reason, ?),
                               decision_at=COALESCE(decision_at, ?),
                               manifest_committed_at=?, updated_at=?
                           WHERE id=?""",
                        (reason, now, now, now, row["id"]),
                    )
            else:
                selected = await db.fetchall(
                    """SELECT e.relative_path AS relative_path, e.expected_bytes AS expected_bytes
                       FROM transfer_file_selection_entries s
                       JOIN transfer_file_manifest_entries e
                         ON e.manifest_id=s.manifest_id AND e.entry_id=s.entry_id
                       WHERE s.selection_id=? ORDER BY e.ordinal""",
                    (row["id"],),
                )
                if not selected:
                    await db.rollback()
                    raise TransferError(NormalizedError(
                        Domain.LIFECYCLE, Category.RESOURCE_STATE_CONFLICT, Stage.RECONCILIATION))
                try:
                    authorized = fs.reconcile_executable_subset(
                        [(r["relative_path"], int(r["expected_bytes"] or 0)) for r in selected],
                        full_entries,
                    )
                except fs.SelectionUnprovable as exc:
                    await db.rollback()
                    raise TransferError(NormalizedError(
                        Domain.LIFECYCLE, Category.RESOURCE_STATE_CONFLICT, Stage.RECONCILIATION,
                    )) from exc
                if not already:
                    await db.execute(
                        "UPDATE transfer_file_selections SET manifest_committed_at=?, updated_at=? WHERE id=?",
                        (now, now, row["id"]),
                    )
            await db.commit()
        return authorized

    async def file_selection_presentation(self, transfer_id: int, *, now: float):
        """Safe core-only read model for one transfer's file selection.

        Reads durable state only: no provider call, no executor handle, no
        signed URL, no per-row Downloads-projection work.
        """
        async with get_db() as db:
            row = await self._current_generation(db, transfer_id)
            if row is None:
                return None
            file_count = await self._manifest_file_count(db, row["manifest_id"])
            entries = []
            if row["manifest_id"]:
                entries = await db.fetchall(
                    """SELECT entry_id, name, relative_path, expected_bytes, ordinal
                       FROM transfer_file_manifest_entries WHERE manifest_id=? ORDER BY ordinal""",
                    (row["manifest_id"],),
                )
            selected = [
                r["entry_id"] for r in await db.fetchall(
                    "SELECT entry_id FROM transfer_file_selection_entries WHERE selection_id=?", (row["id"],))
            ]
        state = self._selection_state(row, file_count)
        return {
            "eligible": True,
            "mutable": fs.selection_mutable(state),
            "selection_id": row["id"],
            "request_id": row["request_id"],
            "provider_resource_id": row["provider_resource_id"],
            "manifest_id": row["manifest_id"],
            "decision": str(row["decision"]),
            "decision_reason": row["decision_reason"],
            "file_count": file_count,
            "total_size_bytes": sum(int(e["expected_bytes"] or 0) for e in entries),
            "entries": [
                {"entry_id": e["entry_id"], "name": e["name"],
                 "relative_path": e["relative_path"], "size_bytes": int(e["expected_bytes"] or 0)}
                for e in entries
            ],
            "selected_entry_ids": selected,
            "auto_offer": fs.auto_offer_active(state, now),
            # The selector auto-presents for exactly the life of the active
            # user-decision hold; there is no separate pre-hold window.
            "auto_offer_until": row["hold_until"] if str(row["decision"]) == "pending" else None,
            # The 120s decision deadline is current control authority ONLY while
            # the decision is still pending. Once Confirm/Close/timeout settles it
            # the durable ``hold_until`` is retained as historical evidence but is
            # never presented as an active deadline (§11).
            "decision_deadline": row["hold_until"] if str(row["decision"]) == "pending" else None,
            "initially_available": bool(row["initially_available"]),
            "server_now": float(now),
        }

    async def active_file_selection_offers(self, *, now: float) -> list:
        """Bounded list of currently auto-presentable multi-file offers."""
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT s.*,
                          (SELECT COUNT(*) FROM transfer_file_manifest_entries e
                           WHERE e.manifest_id=s.manifest_id) AS file_count
                   FROM transfer_file_selections s
                   JOIN torrents t ON t.id=s.transfer_id
                   WHERE s.manifest_committed_at IS NULL AND s.decision='pending'
                     AND s.manifest_id IS NOT NULL AND s.auto_offer_dismissed_at IS NULL
                     AND t.status NOT IN ('deleted','completed','consolidated','cancelled')
                   ORDER BY s.transfer_id LIMIT 500""",
            )
        offers = []
        for row in rows:
            file_count = int(row["file_count"] or 0)
            if file_count <= 1:
                continue
            if fs.auto_offer_active(self._selection_state(row, file_count), now):
                offers.append({
                    "transfer_id": int(row["transfer_id"]),
                    "selection_id": row["id"],
                    "request_id": row["request_id"],
                    "manifest_id": row["manifest_id"],
                    "file_count": file_count,
                    "decision_deadline": row["hold_until"],
                    "auto_offer_until": row["hold_until"],
                })
        return offers
