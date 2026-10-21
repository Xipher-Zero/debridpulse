"""Durable transfer, request, resource and attempt identities.

The existing parent/artifact table names remain a database-format obligation.
Their integration-specific columns are not read by this repository. Native data
is persisted only as opaque context on a resource or execution attempt.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from core.presentation_safety import safe_public_host, safe_route_endpoint
from db.database import get_db, validate_transfer_repository_schema
from db.event_journal import JournalEvent, record as journal, transfer_events as journal_transfer_events
from transfers import codec
from transfers import journal_events as je
from transfers import material as mat
from transfers.cohorts import (
    _FAILED_CONTRIBUTION_DISPOSITION, _HELD_DISPOSITIONS, _PROVEN_DISTINCT_DISPOSITIONS, _UNVERIFIED_DISPOSITION,
)
from transfers.errors import Category, Domain, NormalizedError, Stage, TransferError
from transfers.filesystem import safe_name
from transfers.input_required import public_challenge
from transfers.mirrors import logical_key
from transfers.models import (
    BITTORRENT_REQUEST_KINDS, Artifact, CachePresence, CleanupAuthority, ContinuationCapability, ContinuationPlan,
    ContinuationStrategy, DeliveryKind, ExecutionAttempt, ExecutionHandle, ExecutionState, MaterializationAdmission,
    MaterializationAdmissionKind, MaterializationResult, OutcomeKind, Ownership, ProviderResource, RequestRecord, ResolutionAttempt, ResolutionResult,
    ResourceState, SizeKnowledge, SourceEntry, SubmissionDemand, Transfer, TransferCandidate, TransferOutcome,
    TransferRequest, TransferState, TransferProgress, new_identity,
)
from transfers.policy import SIDE_STATE_RETIRING_TRANSFER_STATES, TERMINAL_TRANSFER_STATES, transition_allowed

# Resolution-attempt states that END a route: ``exhausted`` -- the attempt with
# which its provider was exhausted in the current routing campaign, so that
# provider is excluded until the campaign ends -- and ``released``, the same
# attempt once a later campaign began. Both are failures, neither binds.
_ENDED_ROUTE_STATES = frozenset({"exhausted", "released"})
# Route-attempt operations of a deliberate root route replacement
# (``TransferRepository.replace_root_route``); their transitions are recorded
# with the operation as the reason.
_ROUTE_REPLACEMENT_OPERATIONS = frozenset({"operator_switch", "readiness_promotion"})

# Execution-attempt states of a writer still doing (or about to do) work; an
# ``authorized`` attempt in one of them is a live writer of its artifact.
# THE queue membership predicate over ``torrents``: an unfinished transfer.
# Settled history (completed, cancelled) and the absorbed/deleted are never
# queue entries.
_QUEUE_MEMBER_SQL = "status NOT IN ('completed','consolidated','deleted','cancelled')"
# The application event recording that a root request is exactly the resource
# the admission boundary received (``TransferRepository.admit``).
REQUEST_ADMITTED_VERBATIM = "request_admitted_verbatim"
_MUTATING_EXECUTION_STATES = frozenset({"prepared", "queued", "running", "paused", "unknown"})

# The forms a retired generation's unique ``torrents.hash`` takes once it no
# longer holds its source's active dedupe identity (``_tombstone_hash``); the
# original logical fingerprint stays in ``source_fingerprint``.
RETIRED_KEY_PREFIXES = ("deleted:", "retired:")


# DP 1.0.12 recovery leveling, Section 21/22: parent lifecycle terminal states
# where aggregation has nothing left to decide. The canonical definition now
# lives in transfers.policy (FUNC-001) so this module owns no independent
# literal.
_AGGREGATE_TERMINAL_STATES = TERMINAL_TRANSFER_STATES

# DP 1.0.12 canonical lifecycle/recovery/completion rework, Section 7.2:
# execution-attempt states that mean a durable native writer might still be
# doing something -- while paused, the parent may not claim PAUSED until
# none of a transfer's recorded attempts are in one of these.
_UNSETTLED_EXECUTION_STATES = frozenset({
    "prepared", ExecutionState.QUEUED.value, ExecutionState.RUNNING.value, ExecutionState.UNKNOWN.value,
})


def _lifecycle_error(stored: str | None) -> str | None:
    """A durable normalized-error text as the lifecycle no-op test compares
    it: unchanged, except that a ``diagnostic_evidence`` member is removed.
    The text otherwise stays the prerequisite's exact comparison (a row
    without evidence is returned as stored), so every fact it distinguished
    still distinguishes and evidence alone never does."""
    if not stored or '"diagnostic_evidence"' not in stored:
        return stored
    data = codec.load(stored)
    if not isinstance(data, dict) or "diagnostic_evidence" not in data:
        return stored
    data.pop("diagnostic_evidence")
    return codec.dump(data)


async def _retire_transfer_auxiliary_state_in_db(db, transfer_id: int) -> None:
    """Transaction-local: retire a settled transfer's old pause intent and
    INPUT_REQUIRED challenge (FUNC-001). Called from inside every write path
    that can settle a parent into ``policy.SIDE_STATE_RETIRING_TRANSFER_STATES``
    -- ``_write_lifecycle_transition`` and the direct status-update paths that
    intentionally bypass it (``delete``, ``cancel_with_execution_cleanup``,
    ``transfers.canonical.CanonicalOwnership._finalize_transfer``) -- so a
    settled transfer can never present stale actionable side state from a
    prior lifecycle generation. Always safe to call (no-op when nothing is
    stored); never a second, independent cleanup path."""
    await db.execute("DELETE FROM transfer_pause_intents WHERE torrent_id=?", (transfer_id,))
    await db.execute("DELETE FROM transfer_input_challenges WHERE transfer_id=?", (transfer_id,))


async def _reset_postprocessing_in_db(db, transfer_id: int) -> bool:
    """Transaction-local: THE reset of a transfer's post-processing, so the
    next delivery queues every processor afresh (``queue_postprocessing``
    inserts each attempt only once). ``False`` -- nothing changed -- while an
    attempt is still pending or processing: that work is live and is never
    discarded under it. Otherwise every attempt is removed AND the transfer's
    extraction status and error are cleared with them: they describe the
    removed attempts, so keeping them would present a finished (or failed)
    extraction for material that has not been post-processed in this
    lifecycle -- and, with no processor configured any more, forever."""
    if await db.fetchone("SELECT transfer_id FROM postprocess_attempts WHERE transfer_id=? AND state IN ('pending','processing')", (transfer_id,)):
        return False
    await db.execute("DELETE FROM postprocess_attempts WHERE transfer_id=?", (transfer_id,))
    await db.execute("UPDATE torrents SET extraction_status='',extraction_error=NULL WHERE id=?", (transfer_id,))
    return True


def origin_provider(requests, route_attempts) -> str | None:
    """THE origin provider of one transfer: the provider owning the route of
    every root request, when that is one provider, else ``None``.

    Derived from durable root route truth -- each root request's latest route
    attempt, never one whose provider positively declined that request -- and
    never persisted as a second provider identity. It sits beside the current,
    delivering, route-attempt and execution provider facts and redefines none
    of them: a collection decomposed into members another provider resolves
    keeps its root provider as its origin. The bounded list projection
    (``api.operational_downloads``) derives the same fact in its one SQL read.
    ``requests`` rows carry ``id``/``parent_id``; ``route_attempts`` rows, in
    ordinal order, carry ``request_id``/``provider_id``/``resolution_state``."""
    roots = {row["id"] for row in requests if row.get("parent_id") is None}
    declined = {(row["request_id"], row["provider_id"]) for row in route_attempts
                if row.get("resolution_state") == "declined"}
    owners = {}
    for row in route_attempts:
        if (row.get("request_id") in roots and row.get("provider_id")
                and (row["request_id"], row["provider_id"]) not in declined):
            owners[row["request_id"]] = row["provider_id"]
    distinct = set(owners.values())
    return next(iter(distinct)) if roots and owners.keys() == roots and len(distinct) == 1 else None


def current_route_provider(route_attempts) -> str | None:
    """THE current provider of one transfer: the provider of its most recent
    LIVE route, else ``None``.

    A request's route is its latest route attempt not declined, by a provider
    that did not decline that request -- the rule ``bound_route_provider``
    routes by. A route whose latest attempt ended it (``_ENDED_ROUTE_STATES``:
    exhausted, or released by a later campaign) is nobody's, so a transfer
    whose every route ended has no current provider while its history keeps
    every provider that served it. Derived from durable route truth only, and
    never persisted. The bounded list projection (``api.operational_downloads``)
    derives the same fact in its one SQL read. ``route_attempts`` rows, in
    ordinal order, carry ``request_id``/``provider_id``/``resolution_state``/
    ``ordinal``."""
    live = _live_routes(route_attempts)
    return str(max(live, key=lambda row: int(row["ordinal"]))["provider_id"]) if live else None


def _live_routes(route_attempts) -> list:
    """Each request's LIVE route: its latest route attempt not declined, by a
    provider that did not decline that request, unless that attempt ended the
    route (``_ENDED_ROUTE_STATES``)."""
    declined = {(row["request_id"], row["provider_id"]) for row in route_attempts
                if row.get("resolution_state") == "declined"}
    latest = {}
    for row in route_attempts:
        if (row.get("provider_id") and row.get("resolution_state") != "declined"
                and (row["request_id"], row["provider_id"]) not in declined):
            latest[row["request_id"]] = row
    return [row for row in latest.values() if row.get("resolution_state") not in _ENDED_ROUTE_STATES]


def active_provider(execution_history, route_attempts) -> tuple[str | None, str]:
    """THE active provider of one transfer -- the provider currently executing
    its work, or, once no writer is live, the provider of its last execution
    activity -- as ``(provider_id, basis)``.

    Execution authority decides. Each artifact's own execution is its latest
    execution attempt (the durable per-artifact provenance ``ordinal``); an
    attempt a later one replaced -- a candidate switch, a retry -- never
    counts. When any of those is a live writer (``authorized`` and in
    ``_MUTATING_EXECUTION_STATES``), the latest-started live writer is the
    active one; otherwise the one whose recorded activity ended last
    (provenance ``updated_at``). No durable sequence orders executions of
    different artifacts finer than those timestamps, so different providers
    at the same instant -- or a winner whose provider was never recorded --
    are ``(None, "ambiguous")``, never a tie-break. Only a transfer with no
    execution at all falls back to its live routes, and only when they name
    one provider (``"route"``); else ``(None, "none")``. Derived from durable
    truth only, never persisted. The bounded list projection
    (``api.operational_downloads``) derives the same fact in its one SQL read.
    ``execution_history`` rows carry ``artifact_id``/``ordinal``/
    ``provider_id``/``execution_state``/``authorized``/
    ``provenance_created_at``/``provenance_updated_at``."""
    final = {}
    for row in execution_history:
        current = final.get(row["artifact_id"])
        if current is None or int(row["ordinal"]) > int(current["ordinal"]):
            final[row["artifact_id"]] = row
    if final:
        def live(row) -> bool:
            return bool(row.get("authorized")) and row.get("execution_state") in _MUTATING_EXECUTION_STATES

        any_live = any(live(row) for row in final.values())
        rows = [row for row in final.values() if live(row) == any_live]
        moment = "provenance_created_at" if any_live else "provenance_updated_at"
        latest = max(str(row.get(moment) or "") for row in rows)
        providers = {row.get("provider_id") or None for row in rows if str(row.get(moment) or "") == latest}
        provider = next(iter(providers)) if len(providers) == 1 else None
        return (str(provider), "execution") if provider else (None, "ambiguous")
    providers = {str(row["provider_id"]) for row in _live_routes(route_attempts)}
    return (next(iter(providers)), "route") if len(providers) == 1 else (None, "none")


def manifest_child_identity(parent_id: str, relative_path: str, alternate: int = 0) -> str:
    """The durable identity of one manifest member request under ``parent_id``:
    the member's own request, or its ``alternate``-th further request
    (``SourceEntry.alternates``, 1-based). A path never holds NUL, so no
    member's identity can collide with another member's alternate."""
    if alternate:
        return uuid5(NAMESPACE_URL, f"request:{parent_id}:{relative_path}\x00{int(alternate)}").hex
    return uuid5(NAMESPACE_URL, f"request:{parent_id}:{relative_path}").hex


def manifest_member_requests(entry: SourceEntry) -> tuple[tuple[int, TransferRequest], ...]:
    """``(alternate, request)`` for every request of one manifest member, in
    the provider's order: its own request first (``0``)."""
    return ((0, entry.request), *enumerate(entry.alternates, start=1))


# -- the live transfer's current file-selection intent ------------------------------------------------------------
#
# THE one owner of what the operator wants now for a manifest root: a concrete
# member set (logical coordinates), owned by the root request for as long as
# the transfer lives. Established by the first settled usable multi-file
# choice, revised only by the operator's member control, ended by Delete.
# Provider generations (``transfer_file_selections``) prove and record it;
# they never edit it. Every read and write is transaction-local.

async def _selection_intent_in_db(db, request_id: str) -> list[tuple[str, int]] | None:
    """The intent's members (``(logical path, size)``), or ``None`` when the
    root has none -- an empty list is an intent to materialize nothing."""
    if not await db.fetchone("SELECT 1 FROM transfer_file_selection_intents WHERE request_id=?", (request_id,)):
        return None
    return [(row["relative_path"], int(row["expected_bytes"] or 0)) for row in await db.fetchall(
        "SELECT relative_path,expected_bytes FROM transfer_file_selection_intent_entries WHERE request_id=? "
        "ORDER BY relative_path", (request_id,))]


async def _release_selection_poll_wait_in_db(db, request_id: str, now: float) -> None:
    """End the scheduler wait the file-selection gate created for the root --
    only it: a request ``waiting`` with no ``error`` -- never a provider
    backoff (see ``TransferRepository._release_selection_poll_wait``)."""
    await db.execute(
        "UPDATE transfer_requests SET retry_at=? "
        "WHERE id=? AND state='waiting' AND error IS NULL AND retry_at > ?",
        (now, request_id, now),
    )


async def _set_selection_intent_in_db(db, request_id: str, transfer_id: int, members, now: float, *,
                                      origin_selection_id: str | None = None) -> None:
    """Make ``members`` the intent of ``request_id`` (creating it if absent).

    An intent that comes into being here also governs the root's current
    generation if that is still an open choice: it is decided as carrying the
    intent in this same transaction -- no longer pending, no longer offered,
    its decision wait released -- so a generation that opened before the
    intent existed can never settle another choice. A committed generation,
    or one already decided, is never touched."""
    from transfers import file_selection as fs

    created = await db.fetchone("SELECT 1 FROM transfer_file_selection_intents WHERE request_id=?",
                                (request_id,)) is None
    await db.execute(
        """INSERT INTO transfer_file_selection_intents(request_id,transfer_id,origin_selection_id,created_at,updated_at)
           VALUES(?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET updated_at=excluded.updated_at""",
        (request_id, int(transfer_id), origin_selection_id, now, now))
    await db.execute("DELETE FROM transfer_file_selection_intent_entries WHERE request_id=?", (request_id,))
    for path, size in members:
        await db.execute(
            "INSERT OR REPLACE INTO transfer_file_selection_intent_entries(request_id,relative_path,expected_bytes) "
            "VALUES(?,?,?)", (request_id, str(path), int(size or 0)))
    if not created:
        return
    current = await db.fetchone(
        """SELECT id,decision,manifest_committed_at FROM transfer_file_selections WHERE request_id=?
           ORDER BY created_at DESC, id DESC LIMIT 1""", (request_id,))
    if current is None or current["decision"] != "pending" or current["manifest_committed_at"] is not None:
        return
    cursor = await db.execute(
        """UPDATE transfer_file_selections SET decision='explicit', decision_reason=?, decision_at=?, updated_at=?
           WHERE id=? AND decision='pending' AND manifest_committed_at IS NULL""",
        (str(fs.DecisionReason.INHERITED), now, now, current["id"]))
    if cursor.rowcount == 1:
        await _release_selection_poll_wait_in_db(db, request_id, now)


async def _established_children_in_db(db, root_id: str) -> list[tuple[str, object]]:
    """``(recorded path, size)`` of each member the root has fanned out and not
    set aside: its own (primary) request, active. A member's alternates are
    sibling requests at its path; they never keep a member the operator
    deselected (whose own request is skipped) established."""
    members = []
    for child in await db.fetchall(
            "SELECT id,metadata FROM transfer_requests WHERE parent_id=? AND state!='skipped'", (root_id,)):
        entry = codec.load(child["metadata"], {}) or {}
        path = str(entry.get("relative_path") or "")
        if path and child["id"] == manifest_child_identity(root_id, path):
            members.append((path, entry.get("expected_bytes")))
    return members


async def _edit_selection_intent_member_in_db(db, root_id: str, transfer_id: int, child, selected: bool,
                                               now: float) -> None:
    """The operator's member control, inside its own transaction: deselect
    removes the member from the root's intent, reselect restores it. A root
    without intent gains one here from its committed decomposition -- an
    operator action, never a backfill. A root no generation committed has no
    intent to edit. A member the latest committed generation no longer
    carries cannot be reselected: that generation is history, and refusing
    changes nothing."""
    from transfers import file_selection as fs

    committed = await db.fetchone(
        """SELECT id FROM transfer_file_selections WHERE request_id=? AND manifest_committed_at IS NOT NULL
           ORDER BY manifest_committed_at DESC LIMIT 1""", (root_id,))
    if committed is None:
        return
    if selected and str(child["materialized_selection_id"] or "") != str(committed["id"]):
        raise TransferError(NormalizedError(Domain.LIFECYCLE, Category.RESOURCE_STATE_CONFLICT, Stage.QUEUE,
                                            diagnostic="member_not_in_committed_selection"))
    entry = codec.load(child["metadata"], {}) or {}
    path, size = str(entry.get("relative_path") or ""), int(entry.get("expected_bytes") or 0)
    intent = await _selection_intent_in_db(db, root_id)
    if intent is None:
        intent = [(member, int(member_size or 0)) for member, member_size in await _established_children_in_db(db, root_id)]
    key = fs.normalize_relative_path(path)
    kept = [(member, member_size) for member, member_size in intent if fs.normalize_relative_path(member) != key]
    await _set_selection_intent_in_db(db, root_id, transfer_id, [*kept, (path, size)] if selected else kept, now,
                                      origin_selection_id=committed["id"])


@dataclass(frozen=True)

class AggregateLifecycleOutcome:
    """Result of one atomic ``TransferRepository.aggregate_lifecycle`` call.

    ``artifacts`` is exactly the voting canonical-membership snapshot the
    decision used -- canonical-membership rows (Section 7) minus any failed
    row whose own logical delivery obligation a different, already-completed
    canonical artifact durably satisfies (DP 1.0.12 Root Cause B, Section 5)
    -- the caller must reuse it (never re-read a second, independently timed
    set) for any follow-up step such as completion verification.
    -- and minus every cancelled row, which is withdrawn from the obligation.
    ``should_complete`` is True only when every PARTICIPATING (voting, not
    cancelled) canonical artifact is already "completed", at least one
    participates, and nothing is still resolving; the caller runs the
    separate, non-transactional, executor-touching completion sequence in
    that case (verifying payloads, cancelling stray writers, queuing
    post-processing) since that work cannot happen inside a single bounded
    SQLite transaction.
    """
    should_complete: bool
    artifacts: tuple


def active_execution_progress_sql(transfer_scope: str, *, reconstruction_only: bool = False) -> str:
    """THE one read of in-flight execution progress that is not DP material.

    A current, authorized, running writer's executor progress is execution
    activity only -- durable completion stays DP-valid material
    (``torrents.progress``) and never moves with it -- when its durable
    continuation plan says none of that work is DP material yet: it was
    admitted under a DESTINATION_AWARE plan (it builds its replacement away
    from the canonical destination), or the continuation capabilities it was
    admitted with cannot export material ranges, the only way in-flight work
    ever becomes DP material before completion. A writer that exports them is
    checkpointed into canonical progress, so it has no second lane.
    ``reconstruction_only`` keeps the DESTINATION_AWARE writers alone (what a
    source switch abandons). ``transfer_scope`` is a SQL predicate over
    ``f.torrent_id``; one row per transfer: ``transfer_id``,
    ``execution_completed``, ``execution_total``, and the facts
    ``active_execution_projection`` decides a percentage from: how many
    writers, how many still acquiring, how many state an exact byte total,
    how many state a consistent unit total with their unit sums, and how many
    state a best-effort estimated percentage (``TransferProgress
    .estimated_percentage``) with the largest such figure.

    The capability test is an anti-join, never a per-row subquery: the left
    join matches only an ``export_material_ranges`` element of a writer that
    is not DESTINATION_AWARE, so every counted writer keeps exactly one row (no
    SUM is multiplied) and a writer declaring it, however often, is excluded."""
    destination_aware = f"json_extract(e.continuation, '$.strategy') = '{ContinuationStrategy.DESTINATION_AWARE.value}'"
    exporter_join = "" if reconstruction_only else f"""
        LEFT JOIN json_each(e.continuation, '$.capabilities') c
          ON NOT ({destination_aware}) AND c.value = '{ContinuationCapability.EXPORT_MATERIAL_RANGES.value}'"""
    not_yet_material = destination_aware if reconstruction_only else f"""({destination_aware}
              OR (json_type(e.continuation, '$.capabilities') = 'array' AND c.key IS NULL))"""
    units_known = ("(COALESCE(json_extract(e.progress, '$.total_units'), 0) > 0"
                   " AND json_extract(e.progress, '$.completed_units') BETWEEN 0"
                   " AND json_extract(e.progress, '$.total_units'))")
    estimate_known = "(json_type(e.progress, '$.estimated_percentage') IN ('integer', 'real'))"
    return f"""SELECT f.torrent_id AS transfer_id,
            SUM(MAX(0, COALESCE(json_extract(e.progress, '$.completed_bytes'), 0))) AS execution_completed,
            SUM(MAX(0, COALESCE(json_extract(e.progress, '$.total_bytes'), 0))) AS execution_total,
            COUNT(*) AS execution_writers,
            SUM(COALESCE(json_extract(e.progress, '$.acquiring'), 1) != 0) AS execution_acquiring,
            SUM(COALESCE(json_extract(e.progress, '$.total_bytes'), 0) > 0) AS execution_bytes_known,
            SUM({units_known}) AS execution_units_known,
            SUM(CASE WHEN {units_known} THEN json_extract(e.progress, '$.completed_units') ELSE 0 END)
                AS execution_units_completed,
            SUM(CASE WHEN {units_known} THEN json_extract(e.progress, '$.total_units') ELSE 0 END)
                AS execution_units_total,
            SUM({estimate_known}) AS execution_estimates_known,
            MAX(CASE WHEN {estimate_known} THEN json_extract(e.progress, '$.estimated_percentage') END)
                AS execution_estimated
        FROM download_files f JOIN execution_attempts e ON e.id = f.execution_attempt_id{exporter_join}
        WHERE e.state = 'running' AND e.authorized = 1
          AND {not_yet_material}
          AND ({transfer_scope})
        GROUP BY f.torrent_id"""


def active_execution_projection(row, prefix: str = "") -> tuple[float | None, str | None]:
    """``(percentage, basis)`` of ``active_execution_progress_sql``'s row (its
    columns under ``prefix``). One coherent scope or none: bytes when every
    counted writer states an exact byte total, else completed units when every
    one states a consistent unit total; a mix, or anything unknown, is no
    percentage (indeterminate, never a fabricated figure). ONE writer may
    also state its own best-effort estimate of its whole execution, kept per
    attempt as a high-water mark: it is ``"estimated"`` where no exact scope
    exists or while it is ahead of that exact figure (so the bar never moves
    back), and that writer's exact figure, once it leads, stays below 100%
    (its execution is not finished until its own finalization). Never
    relabelled bytes or units, and never combined across writers (no common
    denominator). Writers all past acquisition (repair, unpack, remux,
    finalization) are ``"processing"``: their acquisition percentage is
    retired, not shown at 100%; a single writer's estimate is kept only as
    that processing fill."""
    def fact(name):
        return int((row or {}).get(prefix + name) or 0)
    writers = fact("execution_writers")
    if writers <= 0:
        return None, None
    estimate = None
    if writers == 1 and fact("execution_estimates_known") == 1:
        try:
            value = float((row or {}).get(prefix + "execution_estimated"))
        except (TypeError, ValueError):
            value = math.nan
        estimate = min(99.9, max(0.0, value)) if math.isfinite(value) else None
    if fact("execution_acquiring") <= 0:
        return estimate, "processing"
    exact = (None, None)
    if fact("execution_bytes_known") == writers and fact("execution_total") > 0:
        exact = min(100.0, fact("execution_completed") / fact("execution_total") * 100.0), "bytes"
    elif fact("execution_units_known") == writers and fact("execution_units_total") > 0:
        exact = min(100.0, fact("execution_units_completed") / fact("execution_units_total") * 100.0), "units"
    if estimate is None:
        return exact
    if exact[0] is None or estimate > exact[0]:
        return estimate, "estimated"
    return min(99.9, exact[0]), exact[1]


def active_execution_bytes(row, prefix: str = "") -> tuple[int, int] | None:
    """``(completed, total)``: the executors' own exact byte counters behind
    an in-flight percentage of basis ``"bytes"`` (``active_execution_projection``)
    -- the current, authorized, acquiring writers' figures, never a declared
    size and never derived from a percentage; ``None`` for any other basis."""
    if active_execution_projection(row, prefix)[1] != "bytes":
        return None
    total = int((row or {}).get(prefix + "execution_total") or 0)
    completed = int((row or {}).get(prefix + "execution_completed") or 0)
    return min(max(completed, 0), total), total


def effective_progress(status, verified, active_percent, active_basis, active_bytes=None,
                       verified_bytes=None) -> dict:
    """THE primary progress figure of an unfinished transfer, as DebridPulse
    shows it (``frontend/static/app.js`` ``progress()``): while the transfer
    is downloading and nothing is verified yet (no verified percentage, or
    exactly 0), the current in-flight execution figure; otherwise verified
    material. ``source`` says which, ``estimated`` whether the in-flight
    figure is a best-effort estimate. Byte counters ride along only with the
    figure they measure: an exact in-flight byte figure carries the
    executors' counters (``active_bytes``); verified material that is
    nonzero carries its own measurement (``verified_bytes``,
    ``verified_material_measure``), and its percentage is then that
    measurement's -- never combined with an in-flight counter. Nothing
    verified (exactly 0, "unverified" above) is no measured figure: a writer
    whose work is not DP material may have acquired much of it. Writers past
    acquisition (``"processing"``) are no acquisition figure. Verified and
    in-flight facts stay separate: nothing here is stored as material."""
    state = str(status or "").strip().lower()
    figure = {"source": "verified", "estimated": False, "completed_bytes": None, "total_bytes": None}
    if state == "completed":
        return {**figure, "percent": 100.0}
    try:
        current = float(active_percent) if active_percent is not None else math.nan
    except (TypeError, ValueError):
        current = math.nan
    unverified = verified is None or float(verified) <= 0
    if state == "downloading" and active_basis != "processing" and math.isfinite(current) and unverified:
        exact = active_bytes if active_basis == "bytes" else None
        return {"percent": round(min(max(current, 0.0), 100.0), 1), "source": "execution",
                "estimated": active_basis == "estimated", "completed_bytes": exact[0] if exact else None,
                "total_bytes": exact[1] if exact else None}
    if verified_bytes is not None and int(verified_bytes[1]) > 0 and int(verified_bytes[0]) > 0:
        total = int(verified_bytes[1])
        completed = min(int(verified_bytes[0]), total)
        return {**figure, "percent": round(completed / total * 100, 1), "completed_bytes": completed,
                "total_bytes": total}
    return {**figure, "percent": None if verified is None else round(min(max(float(verified), 0.0), 100.0), 1)}


def verified_material_known(artifacts) -> bool:
    """Whether verified material has a percentage at all: every unfinished
    canonical artifact's size is known (otherwise it is unavailable, never a
    fabricated 0%)."""
    return all(item.state == "completed" or item.size_knowledge != SizeKnowledge.UNKNOWN for item in artifacts)


def verified_material_measure(artifacts, valid_bytes) -> tuple[int, int] | None:
    """THE verified-material measurement of a transfer, ``(completed, total)``
    in bytes over its canonical artifacts -- the one definition
    ``torrents.progress`` (``completed / total``) and ``torrents.size_bytes``
    (``total``) are written from: a completed artifact counts its whole
    size, any other its DP-valid material (``valid_bytes``, per artifact id)
    up to its size. ``None`` where the percentage is unavailable
    (``verified_material_known``). Never an executor's own counter."""
    if not verified_material_known(artifacts):
        return None
    total = sum(item.expected_bytes for item in artifacts)
    completed = sum(
        item.expected_bytes if item.state == "completed" else min(item.expected_bytes, valid_bytes.get(item.id, 0))
        for item in artifacts
    )
    return completed, total


def canonical_artifact_membership_sql(alias: str = "f") -> str:
    """The one definition of a canonical actionable ``download_files`` row.

    Lifecycle aggregation (``artifacts()`` below), transfer-level presentation
    voting, recovery eligibility, and current candidate-group operational
    status must all filter on exactly this predicate so they can never
    silently diverge onto different child sets again (DP 1.0.12 recovery
    leveling, Section 7). A blocked, standby, or non-request-bound row may
    still be read and shown historically; it must never vote here.
    """
    return (
        f"{alias}.request_id IS NOT NULL AND COALESCE({alias}.blocked,0)=0 "
        f"AND COALESCE({alias}.mirror_state,'')!='standby'"
    )


def is_canonical_artifact_row(row) -> bool:
    """Python-side twin of ``canonical_artifact_membership_sql`` for a row
    (or dict) already carrying ``request_id``/``blocked``/``mirror_state``."""
    return (
        row["request_id"] is not None
        and not bool(row["blocked"])
        and str(row["mirror_state"] or "") != "standby"
    )


async def _durable_canonical_targets_for_request(db, request_id) -> set[int]:
    """DP 1.0.12 canonical equivalence/lifecycle correction, Section 6: every
    canonical artifact id, if any, that ``request_id``'s own candidate
    provenance is durably bound to.

    Same-transfer canonical convergence intentionally never writes an
    ``artifact_consolidations`` row (that table is cross-transfer provenance
    only), so ``canonical_candidate_origins``/``canonical_candidate_bindings``
    -- populated for both same- and cross-transfer contributors by
    ``transfers.canonical.CanonicalOwnership.attach()`` -- is the durable
    source of same-transfer membership; ``artifact_consolidations`` remains
    an additional valid cross-transfer mapping source. Never inferred from
    URL/filename/hostname text or current provider state. This is the ONE
    owner of this lookup; ``transfers.canonical.CanonicalOwnership
    .durable_owner_for_request`` and this module's own lifecycle-voting
    check both call it rather than duplicating the SQL."""
    if not request_id:
        return set()
    rows = await db.fetchall(
        """SELECT DISTINCT b.canonical_artifact_id AS canonical_artifact_id
            FROM canonical_candidate_origins o
            JOIN canonical_candidate_bindings b ON b.id=o.binding_id
            WHERE o.request_id=?
            UNION
            SELECT canonical_artifact_id FROM artifact_consolidations WHERE source_request_id=?""",
        (request_id, request_id),
    )
    return {int(row["canonical_artifact_id"]) for row in rows}


async def _satisfied_elsewhere(db, artifact) -> bool:
    """DP 1.0.12 Root Cause B (Section 5): True when ``artifact`` is a failed
    representation of a logical delivery obligation that a DIFFERENT,
    already-completed canonical artifact durably satisfies.

    This governs only whether the row may cast an operational FAILED vote --
    it remains fully visible in presentation/details for provenance, and its
    own state/history is never mutated here (Section 5: "without deleting
    history"). A row with no durable canonical mapping -- a genuinely
    independent artifact -- always still votes (Section 5.2)."""
    targets = await _durable_canonical_targets_for_request(db, artifact.request_id)
    targets.discard(int(artifact.id))
    if not targets:
        return False
    placeholders = ",".join("?" * len(targets))
    row = await db.fetchone(
        f"SELECT 1 AS ok FROM download_files WHERE id IN ({placeholders}) AND status='completed' LIMIT 1",
        tuple(targets),
    )
    return row is not None


def _logical_slot_key_for_request(record) -> str:
    """DP 1.0.12 CANON-001 exhausted-identity completion policy, Section 9:
    the durable logical-delivery-slot key for a request -- its own resolved
    ``SourceEntry`` when one exists, else its declared
    ``TransferRequest.name`` -- through the ONE ``transfers.mirrors
    .logical_key`` pairing-key algorithm. This is the SAME dispatch
    ``transfers.cohorts._prospective_logical_key`` already uses to derive a
    same-transfer sibling's best-effort identity before it has necessarily
    resolved; reusing ``mirrors.logical_key`` directly here (never a second,
    independently-maintained normalizer) is what keeps this module and
    ``transfers.cohorts`` from silently drifting onto two different
    interpretations of "logical slot" over time. Empty ("") is a genuinely
    UNKNOWN identity -- never itself evidence of a match."""
    if record.entry is not None:
        return logical_key(record.entry)
    return logical_key(SimpleNamespace(relative_path="", name=str(getattr(record.request, "name", "") or "")))


def _logical_slot_key_for_artifact(artifact) -> str:
    """Same normalization, read directly off a completed canonical artifact's
    own durably stored candidates -- never reconstructed from
    ``artifact.name`` alone with an artificially empty ``relative_path``.
    ``download_files`` has no ``relative_path`` column of its own, but each
    of the artifact's candidates already carries the exact relative_path/name
    facts a resolved ``SourceEntry`` stamped onto it at materialization time
    (``transfers._engine_base.TransferEngine._materialize``); re-deriving the
    key from the bare filename would silently collapse a pathful logical slot
    (e.g. ``disc1/file.iso``) down to its basename, letting an unrelated
    ``file.iso`` in a different directory falsely appear to share its slot.

    Requires exactly one distinct, non-empty logical key across every
    candidate the artifact carries -- any ambiguity (candidates disagreeing)
    or absence (no candidates, or every one keying empty) is conservatively
    UNKNOWN, never a match (Section 10: unknown blocks completion)."""
    keys = {logical_key(candidate) for candidate in artifact.candidates}
    keys.discard("")
    return next(iter(keys)) if len(keys) == 1 else ""


async def failed_unverified_target(db, request_id) -> int | None:
    """DP 1.0.13 stale UNVERIFIED target liveness: the exact canonical artifact
    id ``request_id`` is held UNVERIFIED against, when that exact artifact has
    terminally failed; otherwise ``None``.

    An UNVERIFIED hold is terminal only while the one target that justifies it
    remains a valid non-failed target. Once that exact artifact is ``error``
    the durable premise "held against A" is stale: the ASSOCIATION is stale,
    never the identity question -- A's failure is evidence of nothing about
    this request's identity. Reads durable rows only, on the caller's session
    (so lifecycle aggregation stays one transaction); no provider/executor
    I/O, no inference from URL/host/protocol. The single definition shared by
    ``terminal_unverified_association`` below, parent lifecycle aggregation and
    the equivalence owner's exact-target invalidation
    (``transfers.cohorts``, through ``TransferRepository``). Only the proven
    failed-target case: a completed target remains valid provenance."""
    row = await db.fetchone(
        """SELECT r.equivalence_target_artifact_id AS target FROM transfer_requests r
            JOIN download_files c ON c.id=r.equivalence_target_artifact_id
            WHERE r.id=? AND r.state='materializing' AND r.equivalence_disposition=? AND c.status='error'""",
        (request_id, _UNVERIFIED_DISPOSITION),
    )
    return int(row["target"]) if row else None


async def terminal_unverified_association(db, request_id) -> bool:
    """DP 1.0.12 consolidation corrective, Remediation 4: True when the one
    equivalence owner (``transfers.cohorts``) has durably settled this request
    as a terminal UNVERIFIED association to a canonical artifact owned by a
    DIFFERENT transfer -- and that artifact has not terminally failed
    (``failed_unverified_target``): a failed target no longer excuses any
    material work, it is a stale association awaiting ordinary
    reconsideration.

    That pair of durable facts -- the terminal, writer-forbidden disposition
    and the artifact it is associated with -- is the whole statement: no
    writer may ever be created for this request, and the object it plausibly
    is already has its one canonical writer elsewhere. It is NOT membership,
    and nothing anywhere derives a binding, origin or consolidation row from
    it. The single reader-side definition, shared by the lifecycle/completion
    owner below and by ``transfers.canonical.CanonicalOwnership`` -- never two
    independently-maintained copies of the same predicate.

    A TEMPORARILY unproven association (a transient proof failure whose
    scheduled reconsideration is still pending: ``retry_at`` set,
    ``transfers.cohorts``) settles its leaf exactly the same way: it is just
    as writer-forbidden, and its reconsideration is proof work the equivalence
    owner keeps doing after the contributor settled
    (``CanonicalOwnership.reconsidering``) -- never a reason to keep a
    duplicate top-level transfer operationally alive."""
    row = await db.fetchone(
        """SELECT r.id FROM transfer_requests r
            JOIN download_files c ON c.id=r.equivalence_target_artifact_id
            WHERE r.id=? AND r.equivalence_disposition='unverified' AND c.torrent_id!=r.transfer_id""",
        (request_id,),
    )
    return row is not None and await failed_unverified_target(db, request_id) is None


async def _completion_obligation_satisfied(db, record, completed_canonical_keys) -> bool:
    """DP 1.0.12 CANON-001 exhausted-identity completion policy, Sections 6.2/
    8/9/10: True only when ``record`` -- a quiescently-held (``_HELD_
    DISPOSITIONS``) request whose automatic proof acquisition has stopped --
    owes this transfer no remaining material work, on ONE of two durable
    grounds:

    * its logical delivery obligation is ALREADY satisfied by a different,
      completed canonical artifact in the SAME transfer (the original
      CANON-001 ground); or
    * the equivalence owner has durably settled it as a terminal UNVERIFIED
      association to a canonical artifact owned by ANOTHER transfer (DP
      1.0.12 consolidation corrective, Remediation 4:
      ``terminal_unverified_association``). The association is terminal and
      writer-forbidden, so no independent writer obligation can remain --
      which is exactly what this predicate asks. It is deliberately the SAME
      question and the SAME predicate, generalized from "already delivered
      here" to "no material work is owed by this transfer for this leaf";
      the completion-blocking-hold model it feeds is unchanged.

    This is a one-directional delivery-truth read only (Section 9): it never
    creates a canonical binding, never proves source equivalence, never
    merges artifacts, never makes UNVERIFIED mean completed or verified
    membership, and never touches ``record``'s own equivalence disposition --
    identity for this alternate source remains genuinely unresolved.
    Conservative by construction (Section 10): an unresolved request that is
    NOT terminal (``exhausted`` with no durable association -- several
    plausible targets, or none) still blocks; a request with no derivable
    logical key never matches the first ground (unknown blocks completion);
    and a request that ever produced its own ``download_files`` row -- any
    status, not only a currently-voting one; a real materialized artifact/
    writer of its own is conclusive evidence this is NOT a mere
    identity-unproven alternate for someone else's already-delivered slot --
    is never excused on EITHER ground."""
    key = _logical_slot_key_for_request(record)
    if (not key or key not in completed_canonical_keys) and not await terminal_unverified_association(db, record.id):
        return False
    own_artifact = await db.fetchone(
        "SELECT 1 AS ok FROM download_files WHERE request_id=? LIMIT 1", (record.id,),
    )
    return own_artifact is None


async def _proven_distinct(db, request_id) -> bool:
    """True unless ``request_id`` exists and was never affirmatively proven
    distinct (``transfers.cohorts._PROVEN_DISTINCT_DISPOSITIONS``); an absent
    row is conservatively treated as distinct."""
    row = await db.fetchone("SELECT equivalence_disposition FROM transfer_requests WHERE id=?", (request_id,))
    return row is None or str(row["equivalence_disposition"] or "") in _PROVEN_DISTINCT_DISPOSITIONS


async def _slot_delivered_elsewhere(db, artifact, delivered_slots) -> bool:
    """DP 1.0.13 stale UNVERIFIED target liveness, failed former anchor: True
    when failed ``artifact`` occupied a logical delivery slot that a
    DIFFERENT, completed canonical artifact of the SAME transfer has since
    delivered (``delivered_slots``: logical-slot key -> ids of completed
    artifacts whose own request was never proven distinct, keyed through the
    one ``_logical_slot_key_for_artifact`` owner), and the failed row's own
    originating request was never affirmatively proven distinct either.
    Distinction is normally recorded on the LATER incoming request, so both
    sides are asked: a completed artifact proven distinct (from anything) can
    never satisfy another artifact's slot, and a failed artifact proven
    distinct is never satisfied (``transfers.cohorts
    ._PROVEN_DISTINCT_DISPOSITIONS``).

    This means only "logical slot S has been successfully delivered" -- never
    "failed source A was proven identical to the source that delivered it".
    It is a derived parent-voting fact: nothing is written, the failed row
    keeps its durable error/history, and no binding, mirror-group membership,
    standby/blocked state or equivalence target is created. An empty
    (UNKNOWN) slot key never matches."""
    key = _logical_slot_key_for_artifact(artifact)
    if not key or not delivered_slots.get(key, frozenset()) - {int(artifact.id)}:
        return False
    return not await _proven_distinct(db, artifact.request_id)


async def _voting_artifacts(db, artifacts):
    """DP 1.0.12 Root Cause B (Section 5): the canonical-membership artifacts
    that may actually cast a lifecycle vote (completion/FAILED) for the
    parent transfer -- ``artifacts`` minus a failed row whose own logical
    delivery obligation a different, completed canonical artifact already
    satisfies: durably, through its canonical mapping
    (``_satisfied_elsewhere``), or because a different completed canonical
    artifact of this same transfer delivered the same logical slot and the
    failed row and the completed one were both never proven distinct
    (``_slot_delivered_elsewhere``, DP 1.0.13). A failed artifact on either
    side of an affirmative distinction, or occupying a slot nothing else
    delivered, still votes (Section 5.2); this is not a blanket "completed
    wins" rule."""
    delivered_slots: dict[str, set[int]] = {}
    for item in artifacts:
        if item.state == "completed":
            key = _logical_slot_key_for_artifact(item)
            if key and not await _proven_distinct(db, item.request_id):
                delivered_slots.setdefault(key, set()).add(int(item.id))
    result = []
    for item in artifacts:
        if item.state == "error" and (await _satisfied_elsewhere(db, item)
                                      or await _slot_delivered_elsewhere(db, item, delivered_slots)):
            continue
        result.append(item)
    return tuple(result)


# Route History's middle value is the logical source route, not whatever URL an
# executor was handed. Only these two labels are produced for BitTorrent-class
# lineage; everything about them comes from durable request lineage and the
# durable neutral cache fact, never from a provider name or endpoint domain.
_BITTORRENT_ROUTE_IDENTITY = "BitTorrent"
_TORRENT_CACHE_ROUTE_IDENTITY = "Torrent cache"


def _decode_resolution_result(raw_result):
    """One durable ``resolution_attempts.result`` as a plain mapping, or ``{}``
    when it is absent, undecodable, or not an object. Decodes with the SAME
    ``codec.load`` pattern ``_backfill_provenance`` already uses for this exact
    column -- never a second decode convention."""
    if not raw_result:
        return {}
    try:
        payload = codec.load(raw_result, {})
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _single_route_candidate(payload):
    """The one ``TransferCandidate`` a historical resolution attempt produced,
    or ``None`` when it produced zero, several, or an undecodable set -- all
    ambiguous, never guessed (DP 1.0.12 Route History identity correction,
    Section 16 and Gate 9 revision 2)."""
    try:
        candidates = tuple(codec.candidate(item) for item in payload.get("candidates", []))
    except (TypeError, ValueError, KeyError):
        return None
    return candidates[0] if len(candidates) == 1 else None


def _route_endpoint_projection(candidate):
    """DP 1.0.12 Route History identity correction: the durable historical
    route endpoint of an ORDINARY (direct) route, derived from that SAME
    resolution attempt's own durably stored candidate -- never from current
    artifact/candidate state, current execution, canonical binding, or
    request filename.

    Returns ``(route_origin, route_location)`` via the existing
    ``core.presentation_safety.safe_route_endpoint`` sanitizer, or
    ``(None, None)`` when there is no single candidate, that candidate carries
    zero or more than one endpoint (nothing durable identifies which endpoint
    WITHIN a candidate represented the actual route, so ``endpoints[0]`` would
    be exactly the positional inference this correction eliminates; General
    HTTP's stated invariant is one candidate with exactly one endpoint, so
    this never narrows the target case; Gate 9 revision 2), or it has no
    safely representable endpoint."""
    if candidate is None or len(candidate.endpoints) != 1:
        return None, None
    return safe_route_endpoint(candidate.endpoints[0].address)


def _request_roots(requests):
    """``{request_id: (root_request_id, root_kind)}`` from the transfer's own
    durable ``parent_id`` lineage.

    A transfer may hold several roots, so lineage is resolved per request, not
    read off "the first request". A request whose parent chain never reaches a
    root inside this transfer (orphaned or cyclic) is omitted: its lineage is
    unproven and is never guessed."""
    by_id = {item["id"]: item for item in requests}
    kinds = {}
    roots = {}
    for item in requests:
        current, hops = item, 0
        while current is not None and current.get("parent_id") is not None and hops <= len(by_id):
            current, hops = by_id.get(current["parent_id"]), hops + 1
        if current is None or current.get("parent_id") is not None:
            continue
        root_id = current["id"]
        if root_id not in kinds:
            try:
                kinds[root_id] = str(codec.load(current["payload"], {}).get("kind") or "").strip().lower()
            except (TypeError, ValueError, AttributeError):
                kinds[root_id] = ""
        roots[item["id"]] = (root_id, kinds[root_id])
    return roots


def _root_cache_presence(route_attempts, payloads, roots):
    """``{(root_request_id, provider_id): CachePresence}``: the FIRST
    authoritative (HIT/MISS) cache observation a provider recorded for a
    root's own acquisition.

    ``route_attempts`` arrive ordered by the durable per-transfer route
    ordinal, so "first" is chronological acquisition order, deterministically.
    It is the observation that established that root's provider acquisition --
    never the latest one, so a torrent that was a MISS cannot become a HIT
    because a retry or re-resolution found it ready later. Only attempts on
    the root request itself count: descendants describe provider-generated
    materialization, not the acquisition. UNKNOWN never establishes a fact,
    and the fact is per provider because cache presence is a provider's
    statement."""
    facts = {}
    for row, payload in zip(route_attempts, payloads):
        root = roots.get(row["request_id"])
        if root is None or root[0] != row["request_id"]:
            continue
        key = (root[0], row["provider_id"])
        if key in facts:
            continue
        observation = payload.get("observation")
        presence = codec.cache_presence(observation.get("cache_presence") if isinstance(observation, dict) else None)
        if presence is not CachePresence.UNKNOWN:
            facts[key] = presence
    return facts


def _logical_route_identity(row, candidate, roots, root_cache):
    """``(True, identity)`` when this attempt's route identity is its LOGICAL
    source rather than its execution endpoint, else ``(False, None)`` for an
    ordinary native/direct route whose endpoint is the route.

    * Lineage from a BitTorrent-class root wins over the request's own kind: a
      provider-generated HTTP(S) descendant still belongs to its magnet/torrent
      root. Identity is "Torrent cache" only for an authoritative HIT on that
      root's acquisition, otherwise "BitTorrent".
    * A provider-issued delivery endpoint is an execution capability. The
      route is the upstream host durably attested by the candidate's own
      ``SourceIdentity``; without a provable safe host the identity is unknown
      (``None``) -- never the delivery endpoint, and never a guess."""
    root = roots.get(row["request_id"])
    if root is not None and root[1] in BITTORRENT_REQUEST_KINDS:
        hit = root_cache.get((root[0], row["provider_id"])) is CachePresence.HIT
        return True, _TORRENT_CACHE_ROUTE_IDENTITY if hit else _BITTORRENT_ROUTE_IDENTITY
    if candidate is not None and candidate.delivery is DeliveryKind.PROVIDER_ISSUED:
        source = candidate.source_identity
        if source is not None and str(source.scope or "").strip().lower() == "host":
            return True, safe_public_host(source.key)
        return True, None
    return False, None


def _assign_route_identities(route_attempts):
    """DP 1.0.12 Route History identity correction, Section 10 (same-origin
    disambiguation): compute each ORDINARY row's display-ready
    ``route_identity`` once, in this one presentation pass over the
    transfer's own route history.

    A ``route_origin`` shared by 2+ rows in this SAME list falls back to the
    more specific ``route_location`` for exactly those rows; a uniquely-
    occurring origin uses the bare origin. A row with no safely representable
    origin gets ``route_identity=None`` -- never fabricated."""
    origin_counts = Counter(item["route_origin"] for item in route_attempts if item.get("route_origin"))
    for item in route_attempts:
        origin = item.get("route_origin")
        if not origin:
            item["route_identity"] = None
        elif origin_counts[origin] > 1:
            item["route_identity"] = item.get("route_location") or origin
        else:
            item["route_identity"] = origin


def _project_route_history(route_attempts, requests, *, lineage_attempts=()):
    """The one Route History presentation owner: sets ``route_origin``,
    ``route_location`` and ``route_identity`` on every historical route row.

    Everything derives from the exact historical ``resolution_attempts.result``
    (carried on each row as ``resolution_result`` and consumed here) plus the
    durable request lineage -- never from current provider
    enablement/applicability, current cache contents, current artifact binding,
    or current execution state. A logical-identity row (see
    ``_logical_route_identity``) carries no endpoint origin/location, so a
    provider-issued capability path can never reach the browser or its tooltip;
    ordinary rows keep the endpoint projection and same-origin disambiguation
    exactly.

    ``route_attempts`` are the rows being presented. ``lineage_attempts`` are
    further historical attempts, in durable order, that are NOT presented but
    belong to the lineage of a presented row contributed by another transfer:
    they only establish that lineage's first authoritative cache fact, exactly
    as they do in the contributing transfer's own Details. ``requests`` must
    cover the lineage of every presented row."""
    displayed = {row["id"] for row in route_attempts}
    lineage = [row for row in lineage_attempts if row["id"] not in displayed]
    history = [*route_attempts, *lineage]
    decoded = [_decode_resolution_result(row.pop("resolution_result", None)) for row in history]
    roots = _request_roots(requests)
    # Roots never span transfers, so ordering only matters within one
    # transfer's own durable ordinal sequence, which both inputs preserve.
    ordered = sorted(zip(history, decoded), key=lambda pair: (int(pair[0].get("transfer_id") or 0), int(pair[0]["ordinal"])))
    root_cache = _root_cache_presence([row for row, _ in ordered], [payload for _, payload in ordered], roots)
    payloads = decoded[:len(route_attempts)]
    ordinary = []
    for row, payload in zip(route_attempts, payloads):
        candidate = _single_route_candidate(payload)
        logical, identity = _logical_route_identity(row, candidate, roots, root_cache)
        if logical:
            row["route_origin"], row["route_location"], row["route_identity"] = None, None, identity
        else:
            row["route_origin"], row["route_location"] = _route_endpoint_projection(candidate)
            ordinary.append(row)
    _assign_route_identities(ordinary)


# The canonical terminal set, as the status strings the transfer table stores.
# Derived from policy so a new terminal state cannot be missed here, and sorted
# so the SQL parameter order is stable.
_TERMINAL_TRANSFER_STATUSES = tuple(sorted(str(state) for state in TERMINAL_TRANSFER_STATES))



class DemandChanged(Exception):
    """A destructive act was refused, nothing changed: the transfer's
    outstanding demand is no longer the demand the act was authorized
    against (another account's demand arrived meanwhile)."""


class KnownProviderResource(Exception):
    """An inventory observation was refused admission, nothing changed: the
    provider resource it observes is one some DebridPulse transfer has durably
    bound (``provider_resources.resource_key``) -- live or retired. Inventory
    is evidence that the resource exists, never new demand for work
    DebridPulse already holds or retired."""


# What ``torrents.delete_remote`` records about a deleted transfer's remote
# resources (an existing INTEGER column): kept as they are; removed because
# the operator asked Delete to (any resource it knows, observed ones too); or
# retired with the final external demand (only what DebridPulse itself
# created or adopted).
DELETE_REMOTE_KEEP, DELETE_REMOTE_USER_REQUEST, DELETE_REMOTE_OWNED = 0, 1, 2


def terminal_cleanup_authority(status: str, delete_remote) -> str | None:
    """THE cleanup a resource bound to a transfer that is no longer live is
    owed: a deleted transfer's by what its Delete recorded
    (``DELETE_REMOTE_*``); any other ended transfer's (finished, cancelled,
    consolidated) is surplus DebridPulse owns."""
    if str(status) == "deleted":
        mode = int(delete_remote or 0)
        if mode == DELETE_REMOTE_USER_REQUEST:
            return CleanupAuthority.USER_REQUEST
        if mode == DELETE_REMOTE_OWNED:
            return CleanupAuthority.OWNED
        return None
    return CleanupAuthority.OWNED

# An operator switch of the origin's root after the origin's own attempt: the
# durable fact both the replacement's HOLD and its landing key on (``o`` the
# origin, ``op`` its route provenance). A release alone never says it -- an
# operator Retry releases exhausted routes too.
_SWITCHED_AFTER_ORIGIN = """EXISTS(SELECT 1 FROM route_attempt_provenance sp
    WHERE sp.request_id=o.request_id AND sp.operation='operator_switch' AND sp.ordinal>op.ordinal)"""
# The ownership boundary of a root's switch: only an artifact of the switched
# root's OWN transfer (``r`` the origin request, ``f`` the artifact) is ever
# held, landed on or failed by it; an artifact of another transfer is never
# its subject (``_roots_shared_elsewhere`` refuses such a switch up front).
_OWN_TRANSFER = "r.transfer_id=f.torrent_id"


class TransferRepository:
    async def has_integration_references(self, identity=None):
        """Does anything still DEPEND on this integration's connection?

        ``identity`` may be one identity or a collection of them. A paired
        integration owns several durable identities (its provider's and its
        executor's), and work recorded under ANY of them is a reference to that
        integration's configuration.

        Four things are a real dependency, and nothing else is:

        1. a live authorized execution for an owned executor identity;
        2. a NONTERMINAL transfer still holding a non-ABSENT provider resource,
           which is work that may yet need this credential;
        3. genuinely outstanding cleanup -- the durable cleanup owner still has
           responsibility to act -- which needs the credential it was recorded
           against. That is the canonical `pending_cleanup` predicate, not a
           second cleanup state machine;
        4. an outstanding creation reconciliation -- a primary attempt that
           armed or owes one (``reconcile_at``), or a backup claim still
           ``creating`` (a create whose result may never have become durable)
           -- which can only be settled against the account the creation was
           asked of: another account's inventory would prove its absence
           falsely.

        What is NOT a dependency is HISTORY. A completed or deleted transfer
        keeps its provider-resource row as provenance, and that row keeps
        whatever state it last held -- routinely ``available``. Treating any
        non-absent row as live ownership made a credential permanent the moment
        the integration was ever used: one production installation accumulated
        66 such rows and could no longer change its key. Provenance stays in the
        database; it simply stops masquerading as ownership.

        Nothing here knows which integration it is asked about. The predicate is
        the same for every provider that uses the ownership-field mechanism.
        """
        identities = ([] if identity is None else
                      [identity] if isinstance(identity, str) else sorted(identity))
        terminal = ",".join("?" for _ in _TERMINAL_TRANSFER_STATUSES)
        # A row is owned while its transfer is unfinished, or while cleanup is
        # still owed for it. `cleanup_abandoned` is the canonical terminal
        # marker for cleanup that will not be retried.
        owned = (
            "SELECT r.id FROM provider_resources r JOIN torrents t ON t.id=r.transfer_id"
            " WHERE ("
            f"   (r.state!='absent' AND t.status NOT IN ({terminal}))"
            "    OR (r.cleanup_authority IS NOT NULL AND COALESCE(r.cleanup_abandoned, 0) = 0)"
            " )"
        )
        live_execution = ("SELECT id FROM execution_attempts WHERE authorized=1"
                          " AND state IN ('prepared','queued','running','paused','unknown')")
        owed_creation = ("SELECT provider_id FROM resolution_attempts WHERE reconcile_at IS NOT NULL"
                         " UNION ALL SELECT provider_id FROM standby_resources WHERE state='creating'")
        async with get_db() as db:
            if not identities:
                if await db.fetchone(f"{live_execution} LIMIT 1", ()):
                    return True
                if await db.fetchone(f"{owed_creation} LIMIT 1", ()):
                    return True
                return bool(await db.fetchone(f"{owned} LIMIT 1", _TERMINAL_TRANSFER_STATUSES))
            placeholders = ",".join("?" for _ in identities)
            params = tuple(identities)
            if await db.fetchone(f"{live_execution} AND executor_id IN ({placeholders}) LIMIT 1", params):
                return True
            if await db.fetchone(f"SELECT 1 FROM ({owed_creation}) WHERE provider_id IN ({placeholders}) LIMIT 1",
                                 params):
                return True
            return bool(await db.fetchone(
                f"{owned} AND r.provider_id IN ({placeholders}) LIMIT 1",
                _TERMINAL_TRANSFER_STATUSES + params))

    async def pending_events(self):
        async with get_db() as db:
            return await db.fetchall("SELECT * FROM application_events WHERE claimed=0 ORDER BY id LIMIT 100")

    async def claim_event(self, event_id):
        async with get_db() as db:
            cursor = await db.execute("UPDATE application_events SET claimed=1 WHERE id=? AND claimed=0", (event_id,))
            await db.commit()
            return bool(cursor.rowcount)

    async def queue_postprocessing(self, transfer_id, processors, paths):
        async with get_db() as db:
            created = False
            for processor in processors:
                cursor = await db.execute("INSERT OR IGNORE INTO postprocess_attempts(transfer_id,processor_id,paths) VALUES(?,?,?)",
                    (transfer_id, processor.descriptor.id, codec.dump(paths)))
                created = created or bool(cursor.rowcount)
            if created:
                await db.execute("UPDATE torrents SET extraction_status='pending' WHERE id=? AND status NOT IN ('completed','consolidated','deleted') AND COALESCE(extraction_status,'')!='extracting'", (transfer_id,))
            await db.commit()

    async def postprocessing_jobs(self):
        async with get_db() as db:
            return await db.fetchall("""SELECT p.* FROM postprocess_attempts p JOIN torrents t ON t.id=p.transfer_id
                WHERE p.state='pending' AND t.status='extracting' ORDER BY t.priority DESC,t.id""")

    async def claim_postprocessing(self, transfer_id, processor_id):
        async with get_db() as db:
            cursor = await db.execute("UPDATE postprocess_attempts SET state='processing' WHERE transfer_id=? AND processor_id=? AND state='pending'", (transfer_id, processor_id))
            await db.execute("UPDATE torrents SET extraction_status='extracting' WHERE id=? AND status='extracting'", (transfer_id,))
            if cursor.rowcount:
                await journal(db, je.extraction("started", transfer_id=transfer_id, processor_id=processor_id))
            await db.commit()
            return bool(cursor.rowcount)

    async def finish_postprocessing(self, transfer_id, processor_id, outcome):
        async with get_db() as db:
            await db.execute("UPDATE postprocess_attempts SET state='finished',outcome=? WHERE transfer_id=? AND processor_id=?", (codec.dump(outcome), transfer_id, processor_id))
            await journal(db, je.extraction("finished", transfer_id=transfer_id, processor_id=processor_id, outcome=outcome))
            jobs = await db.fetchall("SELECT state,outcome FROM postprocess_attempts WHERE transfer_id=?", (transfer_id,))
            finished = all(job["state"] == "finished" for job in jobs)
            if finished:
                outcomes = [codec.load(job["outcome"]) for job in jobs if job["outcome"]]
                errors = [outcome.get("error") for outcome in outcomes]
                error = next((NormalizedError.from_dict(item) for item in errors if item), None)
                status = "error" if error else "skipped" if all(item["kind"] == "skipped" for item in outcomes) else "completed"
                await db.execute("UPDATE torrents SET extraction_status=?,extraction_error=? WHERE id=? AND status NOT IN ('deleted','consolidated')", (status, error.message if error else None, transfer_id))
            await db.commit()
            return finished

    async def interrupted_postprocessing(self):
        async with get_db() as db:
            return await db.fetchall("SELECT transfer_id,processor_id FROM postprocess_attempts WHERE state='processing'")

    async def initialize(self) -> None:
        # Runtime repositories consume the schema; database bootstrap/migration
        # is the sole authority allowed to create or repair it.
        await validate_transfer_repository_schema()

    @staticmethod
    def _safe_candidate_source(candidate):
        source = candidate.source_identity if candidate else None
        if source is not None:
            return {"scope": str(source.scope), "key": str(source.key)}
        return {"scope": "candidate", "key": str(candidate.id)} if candidate else None

    @classmethod
    def _candidate_summary(cls, candidates):
        return codec.dump([
            {
                "candidate_id": str(candidate.id),
                "provider_id": str(candidate.provider_id or ""),
                "ordinal": ordinal,
                "source": cls._safe_candidate_source(candidate),
            }
            for ordinal, candidate in enumerate(candidates, start=1)
        ])

    @staticmethod
    def _execution_outcome(state):
        value = str(state)
        if value == "succeeded":
            return "succeeded"
        if value in {"failed", "absent"}:
            return "failed"
        if value == "cancelled":
            return "cancelled"
        if value == "unknown":
            return "unknown"
        return "active"

    @classmethod
    async def _candidate_route(cls, db, transfer_id, candidate, *, artifact_id=None):
        if candidate is None or not candidate.id or not candidate.provider_id:
            return None
        if artifact_id is not None:
            bound = await db.fetchone(
                """SELECT o.resolution_attempt_id FROM canonical_candidate_bindings b
                    JOIN canonical_candidate_origins o ON o.binding_id=b.id
                    WHERE b.canonical_artifact_id=? AND b.candidate_id=? AND b.provider_id=?
                    ORDER BY CASE WHEN o.discovered_candidate_id=b.candidate_id THEN 0 ELSE 1 END,o.id LIMIT 1""",
                (artifact_id, str(candidate.id), str(candidate.provider_id)),
            )
            if bound:
                return bound["resolution_attempt_id"]
        rows = await db.fetchall("""SELECT p.resolution_attempt_id,p.ordinal,p.candidate_summary,a.provider_id
            FROM route_attempt_provenance p JOIN resolution_attempts a ON a.id=p.resolution_attempt_id
            WHERE p.transfer_id=? AND a.provider_id=? ORDER BY p.ordinal DESC,a.updated_at DESC,a.id DESC""",
            (transfer_id, candidate.provider_id))
        for row in rows:
            for item in codec.load(row["candidate_summary"], []):
                if str(item.get("candidate_id") or "") == str(candidate.id):
                    return row["resolution_attempt_id"]
        return None

    @classmethod
    async def _backfill_provenance(cls, db):
        """Idempotently migrate only facts already durably present before Item 9."""
        route_rows = await db.fetchall("""SELECT a.*,r.transfer_id FROM resolution_attempts a
            JOIN transfer_requests r ON r.id=a.request_id
            LEFT JOIN route_attempt_provenance p ON p.resolution_attempt_id=a.id
            WHERE p.resolution_attempt_id IS NULL
            ORDER BY r.transfer_id,a.created_at,a.id""")
        for row in route_rows:
            ordinal_row = await db.fetchone("SELECT COALESCE(MAX(ordinal),0) AS n FROM route_attempt_provenance WHERE transfer_id=?", (row["transfer_id"],))
            ordinal = int(ordinal_row["n"] or 0) + 1
            candidates = ()
            if row.get("result"):
                try:
                    payload = codec.load(row["result"], {})
                    candidates = tuple(codec.candidate(value) for value in payload.get("candidates", []))
                except (TypeError, ValueError, KeyError):
                    candidates = ()
            outcome = "failed" if row["state"] == "failed" else "resolved" if row["state"] == "succeeded" else "unknown"
            await db.execute("""INSERT OR IGNORE INTO route_attempt_provenance(
                resolution_attempt_id,transfer_id,request_id,ordinal,operation,candidate_summary,outcome,history_quality)
                VALUES(?,?,?,?,?,?,?,'legacy_known')""",
                (row["id"], row["transfer_id"], row["request_id"], ordinal, "legacy", cls._candidate_summary(candidates), outcome))

        execution_rows = await db.fetchall("""SELECT e.*,f.status AS artifact_status,f.execution_attempt_id AS current_execution_id,
                f.candidates AS artifact_candidates,f.selected_candidate
            FROM execution_attempts e JOIN download_files f ON f.id=e.artifact_id
            LEFT JOIN execution_attempt_provenance p ON p.execution_attempt_id=e.id
            WHERE p.execution_attempt_id IS NULL
            ORDER BY e.transfer_id,e.artifact_id,e.created_at,e.id""")
        for row in execution_rows:
            candidate = None
            if row.get("candidate"):
                try:
                    candidate = codec.candidate(codec.load(row["candidate"]))
                except (TypeError, ValueError, KeyError):
                    candidate = None
            if candidate is None and row.get("current_execution_id") == row["id"] and row.get("artifact_candidates"):
                try:
                    candidates = [codec.candidate(value) for value in codec.load(row["artifact_candidates"], [])]
                    selected = int(row.get("selected_candidate") or 0)
                    candidate = candidates[selected] if 0 <= selected < len(candidates) else None
                except (TypeError, ValueError, KeyError, IndexError):
                    candidate = None
            provider_id = str(candidate.provider_id) if candidate and candidate.provider_id else None
            route_attempt_id = await cls._candidate_route(
                db, row["transfer_id"], candidate, artifact_id=row["artifact_id"]
            ) if candidate else None
            ordinal_row = await db.fetchone("SELECT COALESCE(MAX(ordinal),0) AS n FROM execution_attempt_provenance WHERE artifact_id=?", (row["artifact_id"],))
            ordinal = int(ordinal_row["n"] or 0) + 1
            delivered = bool(provider_id and row["state"] == "succeeded" and row.get("artifact_status") == "completed" and row.get("current_execution_id") == row["id"])
            await db.execute("""INSERT OR IGNORE INTO execution_attempt_provenance(
                execution_attempt_id,route_attempt_id,transfer_id,artifact_id,ordinal,provider_id,candidate_id,candidate_source,
                outcome,delivered,history_quality) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (row["id"], route_attempt_id, row["transfer_id"], row["artifact_id"], ordinal, provider_id,
                 str(candidate.id) if candidate else None, codec.dump(cls._safe_candidate_source(candidate)) if candidate else None,
                 "completed" if delivered else cls._execution_outcome(row["state"]), int(delivered),
                 "legacy_known" if provider_id else "legacy_unknown"))
            if delivered and route_attempt_id:
                await db.execute("UPDATE route_attempt_provenance SET outcome='completed',updated_at=CURRENT_TIMESTAMP WHERE resolution_attempt_id=?", (route_attempt_id,))

    @classmethod
    async def _begin_route_provenance(cls, db, attempt_id, transfer_id, request_id, provider_id, *, operation,
                                      routing_decision: str | None = None):
        previous = await db.fetchone("""SELECT a.id,a.provider_id,a.state,a.error,p.ordinal,p.outcome
            FROM resolution_attempts a JOIN route_attempt_provenance p ON p.resolution_attempt_id=a.id
            WHERE a.request_id=? AND a.id!=? ORDER BY p.ordinal DESC LIMIT 1""", (request_id, attempt_id))
        ordinal_row = await db.fetchone(
            "SELECT COALESCE(MAX(ordinal),0) AS n FROM route_attempt_provenance WHERE transfer_id=?", (transfer_id,)
        )
        ordinal = int(ordinal_row["n"] or 0) + 1
        previous_id = previous["id"] if previous else None
        transition_kind = None
        transition_reason = None
        if previous:
            if operation == "refresh":
                transition_kind = "candidate_refresh"
                transition_reason = "candidate_refresh"
            elif previous["provider_id"] != provider_id:
                transition_kind = "provider_change"
                transition_reason = "route_reselected"
            elif previous.get("state") == "released" and not previous.get("error"):
                # The same provider again after its earlier attempt was
                # RELEASED -- by a route change or a superseded generation,
                # never by a failure: a renewed resolution, not a retry. The
                # predecessor stays linked (``previous_attempt_id``).
                transition_kind = None
                transition_reason = None
            else:
                transition_kind = "resolution_retry"
                transition_reason = "retry"
            error = codec.error(previous.get("error"))
            if error is not None:
                transition_reason = str(error.category.value)
            if previous.get("outcome") == "declined":
                transition_reason = "provider_declined"
            if transition_kind == "provider_change" and operation in _ROUTE_REPLACEMENT_OPERATIONS:
                # Why the route moved: the operator's explicit switch, or the
                # automatic yield of a still-preparing route to a ready backup.
                transition_reason = operation
            if previous.get("outcome") in {"started", "resolved", "unknown"}:
                await db.execute("UPDATE route_attempt_provenance SET outcome='superseded',updated_at=CURRENT_TIMESTAMP WHERE resolution_attempt_id=?", (previous_id,))
        await db.execute("""INSERT INTO route_attempt_provenance(
            resolution_attempt_id,transfer_id,request_id,ordinal,operation,previous_attempt_id,transition_kind,transition_reason,
            candidate_summary,outcome,history_quality,routing_decision) VALUES(?,?,?,?,?,?,?,?,?,'started','recorded',?)""",
            (attempt_id, transfer_id, request_id, ordinal, operation, previous_id, transition_kind, transition_reason, codec.dump([]),
             routing_decision))
        await journal(db, je.route_started(
            transfer_id=transfer_id, request_id=request_id, attempt_id=attempt_id, provider_id=provider_id,
            operation=operation, transition_kind=transition_kind, transition_reason=transition_reason,
            previous_provider_id=previous["provider_id"] if previous else None))

    @staticmethod
    def _transfer(row) -> Transfer | None:
        if not row:
            return None
        raw_hash = str(row["hash"] or "")
        # A retired (deleted) transfer's active dedupe key is a tombstone; present
        # the original logical fingerprint instead.
        display_hash = str(row.get("source_fingerprint") or "") if raw_hash.startswith(RETIRED_KEY_PREFIXES) else raw_hash
        return Transfer(int(row["id"]), str(row["name"] or ""), TransferState(row["status"]),
                        display_hash, str(row["source"] or ""), int(row["priority"] or 0),
                        bool(row.get("paused_intent")), None if row["progress"] is None else float(row["progress"]),
                        codec.error(row.get("normalized_error")), int(row.get("lifecycle_epoch") or 0),
                        *active_execution_projection(row),
                        row.get("collection_root") or None, bool(row.get("collection_root_conflict")))

    async def get(self, transfer_id: int) -> Transfer | None:
        async with get_db() as db:
            row = await db.fetchone("""SELECT t.*, COALESCE(p.paused,0) AS paused_intent FROM torrents t
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id WHERE t.id=?""", (transfer_id,))
        return self._transfer(row)

    async def presentation(self, transfer_id: int, *, details=False):
        """Explicit canonical read model; opaque integration context stays private."""
        async with get_db() as db:
            row = await db.fetchone("""SELECT id,
                CASE WHEN hash LIKE 'deleted:%' OR hash LIKE 'retired:%' THEN COALESCE(source_fingerprint,'') ELSE hash END AS hash,
                name,status,size_bytes,progress,local_path,source,label,priority,
                error_message,normalized_error,extraction_status,extraction_error,created_at,updated_at,completed_at
                FROM torrents WHERE id=?""", (transfer_id,))
            if not row:
                return None
            files = await db.fetchall("""SELECT f.id,f.torrent_id,f.request_id,f.filename,f.size_bytes,f.local_path,f.status,f.download_client,
                f.blocked,f.block_reason,f.retry_count,f.mirror_group_id,f.mirror_state,f.updated_at,f.normalized_error,
                e.progress AS execution_progress FROM download_files f
                LEFT JOIN execution_attempts e ON e.id=f.execution_attempt_id WHERE f.torrent_id=? ORDER BY f.id""", (transfer_id,))
            requests = await db.fetchall("""SELECT id,parent_id,state,error,payload,metadata,equivalence_disposition,
                equivalence_reason FROM transfer_requests
                WHERE transfer_id=? ORDER BY CASE WHEN parent_id IS NULL THEN 0 ELSE 1 END,ordinal,id""", (transfer_id,))
            resources = await db.fetchall("SELECT id,provider_id,state FROM provider_resources WHERE transfer_id=?", (transfer_id,))
            providers = await db.fetchall("SELECT DISTINCT a.provider_id FROM resolution_attempts a JOIN transfer_requests r ON r.id=a.request_id WHERE r.transfer_id=?", (transfer_id,))
            # ``a.result`` (the durable historical ResolutionResult, used
            # below to derive the safe per-row Route History endpoint) rides
            # along in this SAME join, but only when ``details`` actually
            # needs it (Gate 9 revision 3): the list path calls presentation()
            # for every row on every poll purely for lightweight facts like
            # current_provider_id, so unconditionally pulling the full
            # historical result blob -- materially larger than
            # candidate_summary -- for every historical route attempt on
            # every list row would be a real, avoidable list-path I/O/memory
            # cost for a fact the list projection never reads. This is a
            # trusted internal literal switch, never untrusted input, and
            # keeps the query a single set-based join either way -- no
            # per-route query, no N+1.
            resolution_result_column = ",a.result AS resolution_result" if details else ""
            route_attempts = await db.fetchall(f"""SELECT p.resolution_attempt_id AS id,p.request_id,p.ordinal,p.operation,p.previous_attempt_id,
                p.transition_kind,p.transition_reason,p.candidate_summary,p.outcome,p.history_quality,a.provider_id,
                a.state AS resolution_state{resolution_result_column},a.created_at,a.updated_at FROM route_attempt_provenance p
                JOIN resolution_attempts a ON a.id=p.resolution_attempt_id WHERE p.transfer_id=?
                ORDER BY p.ordinal,p.resolution_attempt_id""", (transfer_id,))
            execution_history = await db.fetchall("""SELECT e.id,e.artifact_id,e.executor_id,e.state AS execution_state,e.created_at,e.updated_at,
                e.authorized,p.created_at AS provenance_created_at,p.updated_at AS provenance_updated_at,
                p.route_attempt_id,p.provider_id,p.candidate_id,p.candidate_source,p.ordinal,p.outcome,p.delivered,p.history_quality
                FROM execution_attempt_provenance p JOIN execution_attempts e ON e.id=p.execution_attempt_id
                WHERE p.transfer_id=? ORDER BY p.created_at,p.artifact_id,p.ordinal,p.execution_attempt_id""", (transfer_id,))
            consolidations = await db.fetchall("""SELECT a.contributing_artifact_id,a.source_request_id,a.canonical_artifact_id,
                c.torrent_id AS canonical_transfer_id FROM artifact_consolidations a
                JOIN download_files c ON c.id=a.canonical_artifact_id
                WHERE a.source_transfer_id=? ORDER BY a.contributing_artifact_id""", (transfer_id,))
            candidate_bindings = await db.fetchall("""SELECT b.* FROM canonical_candidate_bindings b
                JOIN download_files f ON f.id=b.canonical_artifact_id
                WHERE f.torrent_id=? AND COALESCE(f.mirror_state,'')!='standby'
                ORDER BY b.canonical_artifact_id,b.candidate_order,b.id""", (transfer_id,))
            candidate_origins = await db.fetchall("""SELECT o.*,b.canonical_artifact_id FROM canonical_candidate_origins o
                JOIN canonical_candidate_bindings b ON b.id=o.binding_id
                JOIN download_files f ON f.id=b.canonical_artifact_id
                WHERE f.torrent_id=? AND COALESCE(f.mirror_state,'')!='standby'
                ORDER BY b.canonical_artifact_id,b.candidate_order,o.id""", (transfer_id,)) if details else []
            contributed_routes, lineage_routes, lineage_requests = (
                await self._canonical_object_routes(db, transfer_id) if details else ((), (), ())
            )
            events = await journal_transfer_events(db, transfer_id) if details else []
            input_challenge = await db.fetchone("SELECT * FROM transfer_input_challenges WHERE transfer_id=?", (transfer_id,))
        def normalized(item, field="normalized_error"):
            error = codec.error(item.pop(field, None))
            item["error"] = error.as_dict() if error else None
            item["error_message"] = error.message if error else None
            return item
        result = normalized(dict(row))
        result["file_count"] = len(files)
        result["blocked_count"] = sum(bool(item["blocked"]) for item in files)
        result["source_failure_count"] = sum(item["state"] == "failed" for item in requests)
        result["resources"] = [dict(item) for item in resources]
        historical_providers = sorted({item["provider_id"] for item in (*resources, *providers) if item.get("provider_id")})
        delivering_providers = sorted({item["provider_id"] for item in execution_history if item.get("delivered") and item.get("provider_id")})
        current_provider_id = current_route_provider(route_attempts)
        result["active_provider_id"], result["active_provider_basis"] = active_provider(execution_history, route_attempts)
        result["historical_providers"] = historical_providers
        result["current_provider_id"] = current_provider_id
        result["origin_provider_id"] = origin_provider(requests, route_attempts)
        result["delivering_provider_ids"] = delivering_providers
        result["delivering_provider_id"] = delivering_providers[0] if len(delivering_providers) == 1 else None
        result["provider_provenance_status"] = "recorded" if delivering_providers else "unknown_legacy" if result["status"] == "completed" else "pending"
        result["providers"] = delivering_providers if result["status"] == "completed" else historical_providers
        result["executors"] = sorted({item["download_client"] for item in files if item["download_client"]})
        result["input_required"] = public_challenge(input_challenge)
        targets = sorted({int(item["canonical_transfer_id"]) for item in consolidations})
        complete_consolidation = result["status"] == "consolidated"
        result["consolidation"] = {
            "state": "complete" if complete_consolidation else "partial" if consolidations else "none",
            "consolidated_into": targets[0] if complete_consolidation and len(targets) == 1 else None,
            "canonical_transfer_ids": targets,
            "artifact_mappings": [dict(item) for item in consolidations],
        }
        if details:
            result["request"] = codec.load(requests[0]["payload"], {}) if requests else None
            result["files"] = []
            for row in files:
                item = normalized(dict(row))
                # Historical/inactive rows (blocked, standby, or not bound to a
                # live request) remain visible here for provenance, but must
                # never be mistaken for current operational truth by a caller
                # that doesn't separately re-check membership (Section 7).
                item["is_canonical"] = is_canonical_artifact_row(item)
                progress = TransferProgress(**codec.load(item.pop("execution_progress", None), {}))
                item["download_speed"] = progress.bytes_per_second if item["status"] == "downloading" else 0
                # Completion is projected from DP-valid material by the
                # presentation owner; the executor's percentage is not it.
                item["progress"] = 100 if item["status"] == "completed" else 0
                result["files"].append(item)
            result["source_outcomes"] = []
            for item in requests:
                if item["state"] == "failed":
                    request = codec.request(codec.load(item["payload"]))
                    error = codec.error(item["error"])
                    result["source_outcomes"].append({"id": item["id"], "name": request.name or "Source request", "status": "error",
                        "error": error.as_dict() if error else None, "error_message": error.message if error else None})
            # Canonical-object Route History. Native rows are this transfer's
            # own route attempts, unchanged and in their durable order. Rows
            # contributed by OTHER transfers follow, each carrying its real
            # contributing transfer -- they are never rewritten as though this
            # transfer had performed them, and their durable ``ordinal`` stays
            # the contributing transfer's own. ``presentation_ordinal`` is the
            # display sequence only: a native row keeps its durable ordinal and
            # contributed rows continue after the last native one.
            unverified_requests = {
                item["id"]: item for item in requests if item.get("equivalence_disposition") == "unverified"
            }
            result["route_attempts"] = []
            for row in route_attempts:
                item = dict(row)
                item["candidates"] = codec.load(item.pop("candidate_summary"), [])
                held = unverified_requests.get(item["request_id"])
                item["relation"] = "unverified" if held else "original"
                item["verification_state"] = "unverified" if held else "verified"
                item["unverified_reason"] = (held.get("equivalence_reason") or None) if held else None
                item["contributing_transfer_id"] = transfer_id
                item["presentation_ordinal"] = int(item["ordinal"])
                result["route_attempts"].append(item)
            next_ordinal = max((int(item["ordinal"]) for item in result["route_attempts"]), default=0)
            for row in contributed_routes:
                item = dict(row)
                item["candidates"] = codec.load(item.pop("candidate_summary"), [])
                next_ordinal += 1
                item["presentation_ordinal"] = next_ordinal
                result["route_attempts"].append(item)
            _project_route_history(
                result["route_attempts"], (*requests, *lineage_requests),
                lineage_attempts=[dict(row) for row in lineage_routes],
            )
            for item in result["route_attempts"]:
                item.pop("transfer_id", None)
            result["execution_attempts"] = []
            for row in execution_history:
                item = dict(row)
                for field in ("authorized", "provenance_created_at", "provenance_updated_at"):
                    item.pop(field, None)
                item["candidate_source"] = codec.load(item.get("candidate_source"), None)
                item["delivered"] = bool(item.get("delivered"))
                result["execution_attempts"].append(item)
            origins_by_binding = {}
            for origin in candidate_origins:
                origins_by_binding.setdefault(int(origin["binding_id"]), []).append({
                    "contributing_artifact_id": int(origin["contributing_artifact_id"]),
                    "contributing_transfer_id": int(origin["contributing_transfer_id"]),
                    "request_id": origin["request_id"],
                    "resolution_attempt_id": origin["resolution_attempt_id"],
                    "discovered_candidate_id": origin["discovered_candidate_id"],
                })
            result["candidate_bindings"] = []
            for binding in candidate_bindings:
                source_identity = None
                if binding.get("source_scope") and binding.get("source_key"):
                    source_identity = {"scope": binding["source_scope"], "key": binding["source_key"]}
                result["candidate_bindings"].append({
                    "canonical_artifact_id": int(binding["canonical_artifact_id"]),
                    "candidate_id": binding["candidate_id"],
                    "provider_id": binding["provider_id"],
                    "source_identity": source_identity,
                    "role": binding["role"],
                    "candidate_order": int(binding["candidate_order"]),
                    "origins": origins_by_binding.get(int(binding["id"]), []),
                })
            result["events"] = [dict(item) for item in events]
        return result

    @staticmethod
    async def _canonical_object_routes(db, transfer_id: int):
        """Route provenance other transfers contributed to the canonical
        artifacts ``transfer_id`` owns: ``(contributed, lineage, requests)``.

        * ``consolidated`` -- each ``canonical_candidate_origins`` row of a
          binding on an owned canonical artifact names the exact
          ``resolution_attempt_id`` / ``request_id`` / contributing transfer
          that discovered a VERIFIED candidate. That exact route attempt is
          projected, once, in durable binding/origin order.
        * ``unverified`` -- a request the equivalence owner durably associated
          with an owned canonical artifact (``equivalence_disposition=
          'unverified'``, ``equivalence_target_artifact_id``). It has no
          binding; its own most recent successful resolution -- the route
          proof was attempted through -- is projected, in durable
          request/resolution order, with its factual unresolved reason.
        * ``failed`` -- a dead source associated with an owned canonical
          artifact as a failed contribution (``equivalence_disposition=
          'failed_contribution'``). It has no binding and never resolved; its
          own last route attempt -- the one that failed -- is projected with
          the failure category as its reason.

        Nothing is inferred from a current candidate, URL, host, filename or
        transfer adjacency. ``lineage`` / ``requests`` are the contributing
        transfers' own durable history, which ``_project_route_history`` needs
        to read a contributed row's identity exactly as that transfer's own
        Details does."""
        verified = await db.fetchall("""SELECT o.resolution_attempt_id,o.request_id,o.contributing_transfer_id
            FROM canonical_candidate_bindings b
            JOIN download_files f ON f.id=b.canonical_artifact_id
            JOIN canonical_candidate_origins o ON o.binding_id=b.id
            WHERE f.torrent_id=? AND COALESCE(f.mirror_state,'')!='standby' AND o.contributing_transfer_id!=?
            ORDER BY b.canonical_artifact_id,b.candidate_order,o.id""", (transfer_id, transfer_id))
        unverified = await db.fetchall("""SELECT r.id AS request_id,r.transfer_id AS contributing_transfer_id,
            r.equivalence_reason FROM transfer_requests r
            JOIN download_files f ON f.id=r.equivalence_target_artifact_id
            WHERE r.equivalence_disposition='unverified' AND r.transfer_id!=?
            AND f.torrent_id=? AND COALESCE(f.mirror_state,'')!='standby'
            ORDER BY r.transfer_id,r.ordinal,r.id""", (transfer_id, transfer_id))
        failed = await db.fetchall("""SELECT r.id AS request_id,r.transfer_id AS contributing_transfer_id,
            r.equivalence_reason FROM transfer_requests r
            JOIN download_files f ON f.id=r.equivalence_target_artifact_id
            WHERE r.equivalence_disposition='failed_contribution' AND r.state='failed' AND r.transfer_id!=?
            AND f.torrent_id=? AND COALESCE(f.mirror_state,'')!='standby'
            ORDER BY r.transfer_id,r.ordinal,r.id""", (transfer_id, transfer_id))
        if not verified and not unverified and not failed:
            # The ordinary transfer: nothing was contributed, so the native
            # route query stays the only route-provenance read Details makes.
            return (), (), ()
        # The contributing transfers' own durable history, one bounded read per
        # contributor (there are at most as many as contributed sources).
        history, lineage_requests = [], []
        for contributor in sorted({int(row["contributing_transfer_id"]) for row in (*verified, *unverified, *failed)}):
            history.extend(await db.fetchall("""SELECT p.resolution_attempt_id AS id,p.transfer_id,p.request_id,p.ordinal,
                p.operation,p.previous_attempt_id,p.transition_kind,p.transition_reason,p.candidate_summary,p.outcome,
                p.history_quality,a.provider_id,a.state AS resolution_state,a.result AS resolution_result,
                a.created_at,a.updated_at
                FROM route_attempt_provenance p JOIN resolution_attempts a ON a.id=p.resolution_attempt_id
                WHERE p.transfer_id=? ORDER BY p.ordinal,p.resolution_attempt_id""", (contributor,)))
            lineage_requests.extend(await db.fetchall(
                "SELECT id,parent_id,payload FROM transfer_requests WHERE transfer_id=?", (contributor,),
            ))
        by_id = {row["id"]: row for row in history}
        selected = {}
        for row in verified:
            selected.setdefault(row["resolution_attempt_id"], {
                "request_id": row["request_id"], "relation": "consolidated", "verification_state": "verified",
                "unverified_reason": None, "contributing_transfer_id": int(row["contributing_transfer_id"]),
            })
        for row in unverified:
            # ``history`` is in durable ordinal order: the last successful
            # attempt of this request is the route its proof was attempted through.
            attempt_id = next((item["id"] for item in reversed(history)
                               if item["request_id"] == row["request_id"] and item["resolution_state"] == "succeeded"), None)
            if attempt_id is not None:
                selected.setdefault(attempt_id, {
                    "request_id": row["request_id"], "relation": "unverified", "verification_state": "unverified",
                    "unverified_reason": row["equivalence_reason"] or None,
                    "contributing_transfer_id": int(row["contributing_transfer_id"]),
                })
        for row in failed:
            # Its last route attempt is the one whose failure made it dead.
            attempt_id = next((item["id"] for item in reversed(history)
                               if item["request_id"] == row["request_id"]), None)
            if attempt_id is not None:
                selected.setdefault(attempt_id, {
                    "request_id": row["request_id"], "relation": "failed", "verification_state": "failed",
                    "unverified_reason": None, "failure_reason": row["equivalence_reason"] or "failed",
                    "contributing_transfer_id": int(row["contributing_transfer_id"]),
                })
        contributed = []
        for attempt_id, facts in selected.items():
            row = by_id.get(attempt_id)
            # The origin names an exact (attempt, request, transfer); a row that
            # does not match all three is not that provenance and is not shown.
            if (row is None or row["request_id"] != facts["request_id"]
                    or int(row["transfer_id"]) != facts["contributing_transfer_id"]):
                continue
            contributed.append({**dict(row), **{key: value for key, value in facts.items() if key != "request_id"}})
        return tuple(contributed), tuple(history), tuple(lineage_requests)

    async def aggregate_lifecycle(self, transfer_id: int, *, input_required: bool) -> AggregateLifecycleOutcome | None:
        """DP 1.0.12 recovery leveling, Sections 21-22: one atomic read-decide-
        write for ordinary parent-lifecycle aggregation.

        Every fact this decision depends on -- transfer status/pause intent,
        canonical artifacts (Section 7's membership predicate), requests,
        execution progress, and the global-pause flag -- is read from ONE
        ``BEGIN IMMEDIATE`` transaction, and the resulting status/progress
        write (when one applies) happens inside that SAME transaction. A
        concurrent aggregation call for the same transfer, or any other
        mutation that touches this transfer's row, serializes behind this one
        (SQLite's immediate write lock, backed by this codebase's existing
        ``busy_timeout``) rather than racing it -- so this can neither read a
        torn cross-connection snapshot (previously: four independent
        ``get_db()`` calls, each its own connection) nor overwrite a newer
        mutation with a decision computed from facts that were already stale
        by the time the old code's separate final write ran.

        ``input_required`` is the caller's own in-memory input-challenge fact
        (``transfers.input_required.InputChallengeStore``, not database
        state); when true this mirrors the historical short-circuit exactly:
        no metadata/progress recompute, no branch evaluation below, only the
        INPUT_REQUIRED transition when not already there.

        Returns ``None`` when the transfer no longer exists or is already
        terminal (nothing to aggregate).
        """
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                """SELECT t.*, COALESCE(p.paused,0) AS paused_intent FROM torrents t
                   LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id WHERE t.id=?""",
                (transfer_id,),
            )
            transfer = self._transfer(row)
            if transfer is None or transfer.state in _AGGREGATE_TERMINAL_STATES:
                await db.rollback()
                return None

            async def _transition(target, *, progress=None, error=None, verified=False):
                if not transition_allowed(transfer.state, target, verified=verified):
                    return
                await self._write_lifecycle_transition(
                    db, transfer_id, row["status"], row["progress"], row["normalized_error"],
                    target, progress=progress, error=error,
                )

            if input_required:
                if transfer.state != TransferState.INPUT_REQUIRED:
                    await _transition(TransferState.INPUT_REQUIRED)
                await db.commit()
                return AggregateLifecycleOutcome(False, ())

            request_rows = await db.fetchall(
                "SELECT * FROM transfer_requests WHERE transfer_id=? ORDER BY parent_id,ordinal", (transfer_id,),
            )
            requests = tuple(
                RequestRecord(r["id"], transfer_id, codec.request(codec.load(r["payload"])), r["state"],
                              r["parent_id"], codec.resource(codec.load(r["resource"])), r["attempts"],
                              r["retry_at"], codec.error(r["error"]), codec.entry(codec.load(r["metadata"])),
                              codec.optional_request(codec.load(r["interpretation"])))
                for r in request_rows
            )
            artifact_rows = await db.fetchall(
                f"""SELECT f.*,e.handle FROM download_files f
                    LEFT JOIN execution_attempts e ON e.id=f.execution_attempt_id
                    WHERE f.torrent_id=? AND {canonical_artifact_membership_sql('f')} ORDER BY f.id""",
                (transfer_id,),
            )
            artifacts = tuple(
                Artifact(a["id"], transfer_id, a["request_id"], a["filename"], a["local_path"], a["size_bytes"] or 0,
                         a["status"], tuple(codec.candidate(item) for item in codec.load(a["candidates"], [])),
                         a["selected_candidate"], codec.handle(codec.load(a["handle"])), a["retry_count"] or 0,
                         a["retry_at"], codec.error(a["normalized_error"]),
                         SizeKnowledge.durable(a["size_bytes"], a["size_knowledge"]))
                for a in artifact_rows
            )
            # A failed request associated with a canonical artifact as a failed
            # contribution (``transfers.canonical``) is history of that
            # artifact, not an unmet obligation of this transfer: it casts no
            # FAILED vote. Every other failed request still does.
            voting_requests = tuple(
                record for record, row in zip(requests, request_rows)
                if not (row["state"] == "failed"
                        and str(row["equivalence_disposition"] or "") == _FAILED_CONTRIBUTION_DISPOSITION)
            )
            execution_rows = await db.fetchall("SELECT * FROM execution_attempts WHERE transfer_id=?", (transfer_id,))
            # DP 1.0.12 Root Cause B (Section 5): a failed artifact whose own
            # logical delivery obligation a different, already-completed
            # canonical artifact durably satisfies must not vote toward
            # FAILED, or block completion, merely because it is present.
            voting_artifacts = await _voting_artifacts(db, artifacts)

            # DP 1.0.12 canonical lifecycle/recovery/completion rework,
            # Section 6: a request's lifecycle state (does autonomous
            # materialization work exist?) and its equivalence disposition
            # (what did identity proof establish?) are orthogonal durable
            # facts, both read in this SAME transaction. A ``materializing``
            # request whose bounded automatic proof-retry budget is durably
            # exhausted (``transfers.cohorts._HELD_DISPOSITIONS``) has no
            # scheduled autonomous work left -- it must not count as
            # ``pending`` (which would falsely stick the parent RESOLVING, or
            # block completion of the artifacts that already voted). It is
            # equally not equivalent to "nothing is happening": the identity
            # remains genuinely unresolved, so it durably holds the parent at
            # QUEUED (a quiescent, non-resolving wait -- see ``quiescent_hold``
            # below) rather than reporting either false active resolution or
            # a silent, misleading "no change".
            #
            # This ``(state='materializing', equivalence_disposition in
            # _HELD_DISPOSITIONS)`` pair is not a special case invented here:
            # it is the SAME canonical contract ``transfers.cohorts
            # .coordinate_collection`` already established and documents
            # (its own "held-class dispositions... keep the writer barrier up
            # without doing any further proof work" comment) as the sole
            # durable state that stops autonomous materialization for a held
            # request:
            #   - ``_process_request`` (transfers._engine_base.TransferEngine)
            #     routes every ``state='materializing'`` row through
            #     ``_materialize`` -> ``coordinate_collection`` on every tick;
            #     that function reads the SAME disposition and returns
            #     immediately for a held row (no proof work, no writer) --
            #     this aggregation reads the identical fact, never a second
            #     interpretation of it.
            #   - restart: both readers re-derive disposition fresh from this
            #     same durable column on every pass: there is no in-memory
            #     state to lose, so a held row cannot silently resume
            #     autonomous work nor lose its hold across a restart.
            #   - wake: the only writer of a `_HELD_DISPOSITIONS` value is the
            #     bounded proof-retry budget in ``cohorts._schedule_proof_
            #     retry``; the only path back out is an explicit operator
            #     retry (``TransferRepository.retry_requests`` resets
            #     ``equivalence_retry_count``/``equivalence_disposition``) or
            #     new proof evidence re-running the mapping -- never an
            #     automatic scheduler tick re-consuming budget or creating a
            #     writer.
            #   - presentation: a held request never materializes an artifact
            #     (``coordinate_collection`` returns before
            #     ``super()._materialize`` runs), so there is no artifact-
            #     level row for presentation to misrepresent as active; only
            #     the parent's own truthful QUEUED state (this branch)
            #     surfaces the hold.
            #   - DP 1.0.13 stale target: an UNVERIFIED hold whose exact
            #     target has terminally failed (``failed_unverified_target``,
            #     read in this SAME transaction) is not a quiescent hold -- its
            #     premise is gone and the equivalence owner reconsiders it on
            #     the request's next ordinary turn. It counts as pending work,
            #     so the failed former target can never terminalize the parent
            #     first. Nothing here clears it, proves anything or picks a
            #     writer.
            stale_targets = {
                r["id"] for r in request_rows
                if r["state"] == "materializing" and r["equivalence_disposition"] == _UNVERIFIED_DISPOSITION
                and await failed_unverified_target(db, r["id"]) is not None
            }

            def _quiescently_held(row) -> bool:
                return (row["state"] == "materializing" and row["id"] not in stale_targets
                        and str(row["equivalence_disposition"] or "") in _HELD_DISPOSITIONS)

            pending = any(
                r["state"] in {"pending", "waiting", "waiting_parent", "resolving", "materializing"}
                and not _quiescently_held(r)
                for r in request_rows
            )
            held_pairs = tuple(
                (row, requests[index]) for index, row in enumerate(request_rows) if _quiescently_held(row)
            )
            quiescent_hold = bool(held_pairs)
            # DP 1.0.12 CANON-001 exhausted-identity completion policy,
            # Section 8: ``scheduler_held``/``quiescent_hold`` above keeps its
            # existing meaning untouched (still the sole scheduler/
            # materialization fact -- unavailable for writer allocation,
            # unavailable for ``pending``). ``completion_blocking_hold`` is a
            # SEPARATE, narrower question asked only of parent-completion
            # eligibility: does this same held request ALSO still represent
            # an unsatisfied delivery obligation? It starts equal to
            # ``quiescent_hold`` (the conservative default -- Section 10: if
            # this cannot be proven, the hold remains blocking) and is
            # lowered only when every held request's own logical slot is
            # already durably satisfied by a completed canonical artifact
            # (Section 9), which cannot be true when no canonical artifact
            # has completed at all -- skipping the per-row check entirely in
            # that (the common, still-in-flight) case.
            completion_blocking_hold = quiescent_hold
            if quiescent_hold:
                completed_canonical_keys = {
                    key for key in (
                        _logical_slot_key_for_artifact(item) for item in artifacts if item.state == "completed"
                    ) if key
                }
                # Asked of every held request, on the one predicate. The
                # earlier "no completed canonical here, so nothing can be
                # satisfied" short-circuit no longer holds: a terminal
                # UNVERIFIED association is satisfied by a canonical artifact
                # in ANOTHER transfer (Remediation 4), so the question must
                # actually be asked. The conservative default is unchanged --
                # any held request this predicate cannot satisfy still blocks.
                completion_blocking_hold = False
                for _, held_record in held_pairs:
                    if not await _completion_obligation_satisfied(db, held_record, completed_canonical_keys):
                        completion_blocking_hold = True
                        break
            # The artifacts the operator withdrew (``withdrawn_attempt``, see
            # the completion decision below) are no longer this transfer's:
            # its size and progress count only what it still delivers.
            withdrawn = {
                int(row["id"]) for row in artifact_rows
                if row["status"] == "cancelled" and row["withdrawn_attempt"] is not None
                and row["withdrawn_attempt"] == (row["execution_attempt_id"] or "")
            }
            counted = tuple(item for item in artifacts if item.id not in withdrawn)
            total = sum(item.expected_bytes for item in counted)
            # DP 1.0.12 canonical transfer-detail materialized-path
            # correction: the durable "current materialized target"
            # (rendered by frontend/static/app.js as "Local Path") must be
            # the durable canonical owner's own target, never a failed
            # non-owner's -- so this reuses ``voting_artifacts`` (computed
            # just above), the SAME durable-binding-derived set that already
            # excludes a failed row whose logical delivery obligation a
            # different, completed canonical artifact durably satisfies
            # (``_voting_artifacts``/``_satisfied_elsewhere``). Falling back
            # to the full canonical-membership ``artifacts`` only when
            # nothing currently votes preserves prior behavior for that
            # otherwise-untouched edge case rather than inventing a new one.
            path_source = voting_artifacts or artifacts
            local_path = str(Path(path_source[0].target).parent) if path_source else ""
            # A percentage exists only when every unfinished artifact's size is
            # known; otherwise it is unavailable (NULL), never a fabricated 0%.
            size_known = verified_material_known(counted)
            await db.execute(
                """UPDATE torrents SET size_bytes=?,local_path=?,progress=CASE WHEN ? THEN progress ELSE NULL END
                    WHERE id=? AND status NOT IN ('deleted','consolidated')""",
                (total, local_path, int(size_known), transfer_id),
            )
            # Completion is DebridPulse-valid material, never an executor's
            # own percentage: a handoff that keeps all material keeps the
            # figure, and a real rollback lowers it by exactly what was lost.
            measure = verified_material_measure(
                counted, await self._valid_material_bytes(db, [item.id for item in counted]))
            progress = None if measure is None else (
                min(100.0, measure[0] / measure[1] * 100) if measure[1] else 0.0)

            # An artifact the operator cancelled is withdrawn from the
            # transfer's completion obligation: the transfer completes once
            # every artifact still participating completed, and only those are
            # verified and post-processed. Withdrawal is the operator's act
            # alone (``withdrawn_attempt``, recorded by ``cancel_artifact``
            # against the attempt it is cancelled on): an executor's own or an
            # internal cancel never withdraws anything and still blocks
            # completion. The withdrawn one keeps its own state and
            # provenance; an explicit Retry/reacquisition restores it to
            # participation (a new attempt, so the record no longer applies). Every voting artifact cancelled is never a
            # completion: the CANCELLED branch below keeps that outcome (no
            # voting artifact at all -- each failed row satisfied elsewhere --
            # completes as before), and a genuine failure still votes FAILED.
            participating = tuple(item for item in voting_artifacts if item.id not in withdrawn)
            should_complete = False
            paused = transfer.paused or await self._globally_paused(db)
            if not paused:
                # DP 1.0.12 canonical lifecycle/recovery/completion rework,
                # Section 6.4 (Gate 9 revision 2), refined by the CANON-001
                # exhausted-identity completion policy (Section 6.2/8):
                # completion requires every logical delivery obligation to be
                # satisfied -- a durably quiescent, identity-unresolved held
                # request is an unsatisfied one (identity remains UNPROVEN,
                # not proven equivalent) UNLESS its own logical delivery slot
                # is already durably satisfied by a different, completed
                # canonical artifact in this same transfer
                # (``completion_blocking_hold``, Section 9/10) -- never merely
                # an inert placeholder to retire from consideration. It can
                # never appear in ``voting_artifacts`` (a held request has no
                # artifact at all), so without this guard every real artifact
                # completing would silently terminalize the parent while an
                # UNSATISFIED hold sits unresolved -- the exact inference the
                # equivalence correction exists to forbid. An unsatisfied hold
                # must first be resolved (recovered/released) or the request
                # explicitly retried before completion may ever be reached.
                if artifacts and not pending and not completion_blocking_hold and (
                    participating or not voting_artifacts
                ) and all(item.state == "completed" for item in participating):
                    should_complete = True
                elif any(item.state in {"downloading", "verifying"} for item in artifacts):
                    await _transition(TransferState.TRANSFERRING, progress=progress)
                # An attached execution whose native truth is unknown, or was
                # found lost (``ExecutionState.ABSENT``), is still owned by
                # recovery: reconcile revisits it and either requeues it or
                # settles it explicitly ('error' once recovery is exhausted).
                # It is unfinished work, never a terminal vote -- a sibling's
                # failure must not report the transfer FAILED meanwhile.
                elif any(item.state in {"unknown", "lost"} for item in artifacts):
                    await _transition(TransferState.QUEUED, progress=progress)
                # DP 1.0.12 canonical lifecycle/recovery/completion rework,
                # Section 7 (CANON-001): an artifact autonomously waiting on
                # recovery is exactly one more input FACT to this one
                # canonical decision, at the same precedence tier as the
                # other non-transferring "there is still queued-ish work"
                # states -- never a second, independently-timed read-decide-
                # write layered on top of this transaction's own conclusion
                # (the removed ``force_queued_for_autonomous_wait``). A real
                # active download (the ``downloading``/``verifying`` branch
                # above) still always wins.
                elif any(item.state in {"queued", "paused", "refresh_pending", "recovery_wait"} for item in artifacts):
                    await _transition(TransferState.QUEUED, progress=progress)
                elif pending:
                    await _transition(TransferState.RESOLVING, progress=progress)
                # DP 1.0.12 canonical lifecycle/recovery/completion rework,
                # Section 6.4 (Gate 9 revision 3): a genuine, independent
                # terminal failure is its own unsatisfied logical obligation
                # -- it belongs to a DIFFERENT voting artifact/request than
                # whichever one is quiescently held, and the hold must never
                # erase it. Evaluated BEFORE the quiescent-hold fallback
                # below: a prior ordering let ``elif quiescent_hold`` win
                # first, so one artifact's genuinely terminal ERROR sat
                # forever masked behind an unrelated sibling's unresolved
                # equivalence hold, reporting a truthless perpetual QUEUED
                # instead of the real FAILED outcome. The hold must prevent
                # false COMPLETED and false RESOLVING; it must not launder an
                # actual terminal failure into a nonterminal wait.
                elif any(item.state == "error" for item in voting_artifacts) or any(
                        item.state == "failed" for item in voting_requests):
                    error = next((item.error for item in (*voting_artifacts, *voting_requests) if item.error), None)
                    await _transition(TransferState.FAILED, progress=progress, error=error)
                elif artifacts and all(item.state == "cancelled" for item in artifacts):
                    await _transition(TransferState.CANCELLED, progress=progress)
                # DP 1.0.12 canonical lifecycle/recovery/completion rework,
                # Section 6.4: no artifact is actively working, no artifact/
                # request has genuinely failed or been cancelled, and no
                # request has genuine autonomous work left, but identity
                # remains durably unresolved for at least one quiescently-
                # held request -- truthfully a non-resolving wait, not
                # perpetual "processing" and not silent staleness.
                elif quiescent_hold:
                    await _transition(TransferState.QUEUED, progress=progress)
                elif not artifacts:
                    blocked_row = await db.fetchone(
                        "SELECT COUNT(*) AS n FROM download_files WHERE torrent_id=? AND blocked=1", (transfer_id,),
                    )
                    if int((blocked_row or {}).get("n") or 0) and transition_allowed(transfer.state, TransferState.COMPLETED, verified=True):
                        await self._write_lifecycle_transition(
                            db, transfer_id, row["status"], row["progress"], row["normalized_error"],
                            TransferState.COMPLETED, progress=0,
                        )
                        skip_outcome = TransferOutcome(OutcomeKind.SKIPPED, detail="No selected artifacts")
                        await db.execute(
                            "INSERT INTO transfer_outcomes(transfer_id,attempt_id,kind,payload) VALUES(?,?,?,?)",
                            (transfer_id, None, skip_outcome.kind, codec.dump(skip_outcome)),
                        )
            elif not any(str(item.get("state")) in _UNSETTLED_EXECUTION_STATES for item in execution_rows):
                # DP 1.0.12 canonical lifecycle/recovery/completion rework,
                # Section 7.2: durable paused truth folded into this SAME
                # atomic decision (previously a separate post-aggregate
                # repair in ``transfers.engine.TransferEngine._aggregate``,
                # run as its own read-decide-write after this transaction had
                # already committed -- a second parent-lifecycle authority
                # forbidden by Section 3.1). Pause intent alone is not enough
                # to claim parent PAUSED while a durable execution
                # observation is still active/unknown; once every recorded
                # attempt is quiescent, this is metadata-only -- it does not
                # dispatch, refresh, replace native work, or consume recovery
                # authority.
                await _transition(TransferState.PAUSED)
            await db.commit()
        # DP 1.0.12 Root Cause B: the caller's completion-verification sweep
        # (_engine_base.TransferEngine._complete) must never be handed a
        # satisfied-elsewhere failed row to verify -- its own target is not
        # expected to be a valid payload, and the logical obligation it
        # represents was already verified when the OTHER canonical completed.
        # Nor a cancelled one: it holds no delivered material, and the sweep
        # would requeue it -- reviving a file the operator withdrew.
        return AggregateLifecycleOutcome(should_complete, participating)

    @staticmethod
    async def _globally_paused(db) -> bool:
        row = await db.fetchone("SELECT value FROM transfer_controls WHERE key='paused'")
        return bool(row and row["value"] == "1")

    async def update_metadata(self, transfer_id, *, label=None, priority=None):
        async with get_db() as db:
            if not await db.fetchone("SELECT id FROM torrents WHERE id=?", (transfer_id,)):
                raise KeyError(transfer_id)
            # An absolute priority is no operator promotion: it never preempts.
            await db.execute("UPDATE torrents SET label=COALESCE(?,label),priority=COALESCE(?,priority),"
                "queue_promotion=CASE WHEN ? IS NULL THEN queue_promotion END,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (label, priority, priority, transfer_id))
            await db.commit()

    async def active(self) -> tuple[Transfer, ...]:
        async with get_db() as db:
            rows = await db.fetchall(f"""SELECT t.*, COALESCE(p.paused,0) AS paused_intent,
                x.execution_completed, x.execution_total, x.execution_writers, x.execution_acquiring,
                x.execution_bytes_known, x.execution_units_known, x.execution_units_completed,
                x.execution_units_total, x.execution_estimates_known, x.execution_estimated FROM torrents t
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id
                LEFT JOIN ({active_execution_progress_sql("1=1")}) x ON x.transfer_id=t.id
                WHERE t.status NOT IN ('completed','consolidated','deleted','cancelled')
                AND EXISTS(SELECT 1 FROM transfer_requests r WHERE r.transfer_id=t.id)
                ORDER BY t.priority DESC,t.id""")
        return tuple(self._transfer(row) for row in rows)

    async def queue(self) -> tuple[int, ...]:
        """THE operator queue: every unfinished transfer, first first --
        ``torrents.priority`` descending, then the older transfer. One entry
        per logical transfer; settled history is no queue member."""
        async with get_db() as db:
            return await self._queue(db)

    @staticmethod
    async def _queue(db) -> tuple[int, ...]:
        rows = await db.fetchall(f"SELECT id FROM torrents WHERE {_QUEUE_MEMBER_SQL} ORDER BY priority DESC,id")
        return tuple(int(row["id"]) for row in rows)

    async def queue_writers(self) -> tuple[dict, ...]:
        """Every current authorized writer occupying an execution slot, with
        the durable facts a displacement is decided from: its transfer,
        artifact, executor, native state, last recorded progress, the
        continuation plan it was admitted under, and whether its transfer
        holds a pause intent."""
        placeholders = ",".join("?" for _ in self._OCCUPYING_EXECUTION_STATES)
        async with get_db() as db:
            rows = await db.fetchall(
                f"""SELECT e.id AS attempt_id, e.transfer_id, f.id AS artifact_id, e.executor_id, e.state,
                       e.progress, e.continuation, e.native_transition_from,
                       f.status AS artifact_state, t.name, COALESCE(p.paused,0) AS paused
                    FROM execution_attempts e JOIN download_files f ON f.execution_attempt_id=e.id
                    JOIN torrents t ON t.id=e.transfer_id
                    LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id
                    WHERE {_QUEUE_MEMBER_SQL.replace('status', 't.status')} AND e.authorized=1
                    AND e.state IN ({placeholders}) ORDER BY e.transfer_id, f.id""",
                self._OCCUPYING_EXECUTION_STATES)
        return tuple({**row, "progress": codec.load(row["progress"], {}) or {},
                      "continuation": codec.load(row["continuation"], {}) or {}} for row in rows)

    async def artifact_transfers(self, artifact_ids) -> dict[int, int]:
        """``artifact id -> transfer id`` for the given artifacts."""
        ids = sorted({int(item) for item in artifact_ids})
        if not ids:
            return {}
        async with get_db() as db:
            rows = await db.fetchall(
                f"SELECT id, torrent_id FROM download_files WHERE id IN ({','.join('?' for _ in ids)})", ids)
        return {int(row["id"]): int(row["torrent_id"]) for row in rows}

    async def commit_queue_move(self, transfer_id: int, *, expected: tuple[int, ...], proposed: tuple[int, ...],
                                promotions: dict[int, dict], events) -> int | None:
        """Commit one operator queue move atomically, or nothing.

        ``expected`` is the queue the move was planned against: if it is no
        longer the queue, nothing is written and ``None`` is returned.
        Otherwise the unfinished queue is renumbered once to ``proposed``
        (``len`` .. 1, only rows whose priority changes are written; settled
        history is never touched), each overtaking transfer holds a promotion
        that gains the restarts confirmed for it (one an earlier move
        recorded stays), and ``events`` are journaled.
        Returns the moved transfer's new priority."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            if await self._queue(db) != tuple(expected):
                await db.rollback()
                return None
            count = len(proposed)
            for index, member in enumerate(proposed):
                await db.execute("UPDATE torrents SET priority=? WHERE id=? AND priority IS NOT ?",
                                 (count - index, member, count - index))
            for member, promotion in promotions.items():
                row = await db.fetchone("SELECT queue_promotion FROM torrents WHERE id=?", (member,))
                held = codec.load(row["queue_promotion"], None) or {} if row else {}
                merged = {key: sorted({*(held.get(key) or ()), *promotion[key]}) for key in ("restart",)}
                await db.execute("UPDATE torrents SET queue_promotion=? WHERE id=?", (codec.dump(merged), member))
            for event in events:
                await journal(db, event)
            await db.commit()
        return count - proposed.index(transfer_id)

    async def queue_promotion(self, transfer_id: int) -> dict | None:
        """The operator promotion an unfinished transfer holds, if any:
        ``{"restart": [confirmed attempt ids]}``."""
        async with get_db() as db:
            row = await db.fetchone(
                f"SELECT queue_promotion FROM torrents WHERE id=? AND queue_promotion IS NOT NULL "
                f"AND {_QUEUE_MEMBER_SQL}", (int(transfer_id),))
        return (codec.load(row["queue_promotion"], None) or {}) if row else None

    async def writer_demand(self, transfer_id: int) -> int:
        """How many writers the transfer is about to ask for: its queued
        artifacts without a writer, or one for a transfer that has no artifact
        yet (still resolving)."""
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT COUNT(*) AS total, COALESCE(SUM(status='queued' AND execution_attempt_id IS NULL),0) AS waiting "
                "FROM download_files WHERE torrent_id=?", (int(transfer_id),))
        return int(row["waiting"]) if int(row["total"]) else 1

    @staticmethod
    def _tombstone_hash(transfer_id: int, source_fingerprint: str, status: str = "deleted") -> str:
        """Deterministic, per-transfer, non-recursive retired dedupe key. A
        deleted generation keeps the ``deleted:`` form; any other terminal
        generation an independent submission retired takes ``retired:`` --
        its lifecycle is history, never rewritten to look deleted."""
        prefix = "deleted" if str(status) == TransferState.DELETED else "retired"
        return f"{prefix}:{int(transfer_id)}:{source_fingerprint}"

    @classmethod
    async def _retire_active_fingerprint(cls, db, row) -> None:
        """Within an open transaction: preserve the original logical fingerprint
        and retire the active unique ``hash`` key to the tombstone form. Idempotent
        and never recursively prefixed."""
        transfer_id = int(row["id"])
        current_hash = str(row["hash"] or "")
        original = str(row["source_fingerprint"] or current_hash)
        if current_hash.startswith(RETIRED_KEY_PREFIXES):
            if row["source_fingerprint"] is None:
                await db.execute(
                    "UPDATE torrents SET source_fingerprint=? WHERE id=? AND source_fingerprint IS NULL",
                    (original, transfer_id),
                )
            return
        await db.execute(
            "UPDATE torrents SET source_fingerprint=COALESCE(source_fingerprint,?), hash=? WHERE id=?",
            (original, cls._tombstone_hash(transfer_id, original, str(row["status"])), transfer_id),
        )

    @staticmethod
    async def _predecessor_cleanup_blocks(db, transfer_id: int) -> bool:
        """The ONE predecessor-cleanup fence predicate, evaluated inside the
        caller's transaction/session so every same-object resource creation or
        reuse decision (a fresh root's first resolution, an inventory adoption)
        reads the same fact. A predecessor is any retired generation of the
        same logical source -- deleted, or a terminal lifecycle a later
        independent submission retired."""
        row = await db.fetchone("SELECT source_fingerprint FROM torrents WHERE id=?", (transfer_id,))
        fingerprint = row["source_fingerprint"] if row else None
        if not fingerprint:
            return False
        blocker = await db.fetchone(
            """SELECT 1 FROM provider_resources r
               JOIN torrents t ON t.id=r.transfer_id
               WHERE t.id != ? AND (t.hash LIKE 'deleted:%' OR t.hash LIKE 'retired:%') AND t.source_fingerprint=?
                 AND r.cleanup_authority IS NOT NULL
                 AND COALESCE(r.cleanup_abandoned, 0) = 0
                 AND r.state != 'absent'
               LIMIT 1""",
            (transfer_id, fingerprint),
        )
        return blocker is not None

    async def predecessor_cleanup_barrier(self, transfer_id: int) -> bool:
        """True while a retired (deleted) predecessor generation sharing this
        transfer's ``source_fingerprint`` still owns a provider resource whose
        cleanup responsibility is outstanding and has not been permanently
        abandoned.

        The fresh generation is admitted immediately but must not perform a
        conflicting provider-resource creation/reuse until no cleanup operation
        belonging to the predecessor can subsequently act on the shared native
        resource. The predicate deliberately does not distinguish
        pending / leased-in-flight / expired-claim / scheduled-retry — every one
        of those can still act, so every one blocks. Completion (authority
        cleared), an ABSENT resource, and terminal abandonment
        (``cleanup_abandoned``, set only after the provider cleanup call returned
        and policy gave up) release the block, so a fresh transfer is never
        deadlocked. A claim whose owner is lost is not a permanent block: its
        lease expires and the ordinary cleanup cadence re-claims it (see
        :meth:`claim_cleanup`).
        """
        async with get_db() as db:
            return await self._predecessor_cleanup_blocks(db, transfer_id)

    async def admit(self, requests: tuple[TransferRequest, ...], *, name: str, source: str = "manual", priority=None, deduplicate=True,
                    independent=True, alternative_groups: tuple[int | None, ...] | None = None,
                    verbatim: tuple[bool, ...] = (), demand: SubmissionDemand | None = None,
                    observed: tuple[ProviderResource, ResourceState] | None = None) -> tuple[Transfer, bool]:
        """Admit one submission under the source's active dedupe identity.

        Matching live work (any non-terminal lifecycle, a failed one included:
        operator Retry owns it) is the same work: the existing transfer is
        returned. A terminal lifecycle (completed, consolidated, cancelled,
        deleted) is history, never the identity of a later INDEPENDENT
        submission: its active key is retired (``_retire_active_fingerprint``;
        the original fingerprint stays in ``source_fingerprint``) and a new
        lineage is admitted in the same immediate transaction, so concurrent
        same-object submissions still converge on exactly one new generation.
        Whether that lineage needs any acquisition is decided later by the
        canonical material owner, never here. An observation of a resource that
        already exists (``independent=False``: inventory) is not a submission
        and dedupes onto whatever lifecycle holds the key -- except a deleted
        one, which never holds it.

        ``alternative_groups`` (one entry per request, ``None`` = ordinary)
        names the explicit alternative-source group of each root. A group's
        first request in submitted order is admitted ``pending``; every other
        member is admitted dormant -- ``skipped``, the existing "not selected
        for acquisition" state no cohort, canonical or lifecycle owner treats
        as material -- until ``_admit_next_alternative`` selects it.

        A NEW transfer joins the tail of the unfinished queue (``queue``):
        ``priority=None`` takes the lowest priority any unfinished transfer
        holds (never above 0), which the older transfer wins on a tie, so no
        existing position changes and nothing is displaced.

        ``verbatim`` (one entry per request, from the admission boundary
        alone) records, in this transaction, which roots are exactly the
        resource that boundary received: one non-secret
        ``request_admitted_verbatim`` application event naming the root
        (claimed, so it is never delivered). No event is no proof.

        ``demand`` is the accepted submission's own demand, recorded in this
        transaction whether the submission created the transfer or converged
        on an existing one (which admits no request of its own). A transfer
        created without one (an inventory observation) records an
        ``unattributed`` demand: nobody owns it and it keeps the work.

        ``observed``: the provider resource (and its observed state) an
        inventory observation is of. Admission and binding are one
        transition, decided in this same transaction: a NEW transfer is never
        created for a resource any transfer has durably bound -- live or
        retired (``KnownProviderResource``, nothing written) -- and the new
        transfer it does create is bound to that resource in the commit that
        creates it, so no inventory-created transfer ever exists unbound
        while another owner can take the resource. Only the
        predecessor-cleanup fence leaves it unbound (it then waits exactly
        as a first resolution does). Converging on an existing transfer binds
        nothing here (``adopt_inventory_resource`` decides that)."""
        fingerprint = requests[0].fingerprint if len(requests) == 1 else ""
        # Routing preferences and display names are not logical source identity.
        # The same accepted request can be resolved through another integration.
        fingerprint = fingerprint or hashlib.sha256(codec.dump(tuple(
            (item.kind, item.payload, item.fingerprint) for item in requests)).encode()).hexdigest()
        if not deduplicate:
            fingerprint = "request:" + new_identity()
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("SELECT * FROM torrents WHERE hash=?", (fingerprint,))
            if row and (str(row["status"]) == TransferState.DELETED
                        or (independent and TransferState(row["status"]) in TERMINAL_TRANSFER_STATES)):
                # A user-deleted transfer must never remain the active dedupe /
                # recovery identity. A legacy deleted row (or a tombstone race)
                # still holding the active key is retired transactionally here so
                # this submission is a genuinely fresh lifecycle. A terminal
                # lifecycle is retired the same way for an independent
                # submission: historical completion is not its identity.
                await self._retire_active_fingerprint(db, row)
                row = None
            if row:
                transfer_id, created = int(row["id"]), False
            else:
                if observed and await db.fetchone(
                        "SELECT 1 FROM provider_resources WHERE resource_key=? OR (resource_key IS NULL AND id=?) LIMIT 1",
                        (str(observed[0].id), str(observed[0].id))):
                    await db.rollback()
                    raise KnownProviderResource(str(observed[0].id))
                if priority is None:
                    tail = await db.fetchone(f"SELECT MIN(priority) AS low FROM torrents WHERE {_QUEUE_MEMBER_SQL}")
                    priority = min(0, int(tail["low"])) if tail and tail["low"] is not None else 0
                transfer_id = await db.execute_returning_id(
                    """INSERT INTO torrents(hash,name,status,source,priority,download_client,source_fingerprint)
                    VALUES(?,?,'pending',?,?,'',?)""", (fingerprint, name, source, priority, fingerprint))
                created = True
            existing = await db.fetchone("SELECT id FROM transfer_requests WHERE transfer_id=? LIMIT 1", (transfer_id,))
            first_root = None
            if not existing and alternative_groups:
                selected: set[int] = set()
                for ordinal, (request, group) in enumerate(zip(requests, alternative_groups, strict=True)):
                    state = "skipped" if group is not None and group in selected else "pending"
                    if group is not None:
                        selected.add(group)
                    root = new_identity()
                    first_root = first_root or root
                    await db.execute("INSERT INTO transfer_requests(id,transfer_id,ordinal,payload,state,alternative_group) "
                                     "VALUES(?,?,?,?,?,?)",
                                     (root, transfer_id, ordinal, codec.dump(request), state, group))
                    await self._attest_verbatim(db, transfer_id, root, verbatim, ordinal)
            elif not existing:
                for ordinal, request in enumerate(requests):
                    root = new_identity()
                    first_root = first_root or root
                    await db.execute("INSERT INTO transfer_requests(id,transfer_id,ordinal,payload) VALUES(?,?,?,?)",
                                     (root, transfer_id, ordinal, codec.dump(request)))
                    await self._attest_verbatim(db, transfer_id, root, verbatim, ordinal)
            if demand is not None or created:
                await db.execute(
                    "INSERT INTO transfer_demands(id,transfer_id,request_id,account_id,credential_id,origin,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (demand.id if demand is not None else new_identity(), transfer_id, first_root,
                     demand.account_id if demand is not None else None,
                     demand.credential_id if demand is not None else None,
                     "submission" if demand is not None else "unattributed", time.time()))
            if demand is not None and demand.account_id:
                # The REQUEST-level acceptance, committed with the demand it
                # names: an account's own "Download Added" whether or not this
                # request created the transfer. The transfer-level 'accepted'
                # below is unchanged and remains the global one.
                await db.execute("INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,'demand_accepted',?)",
                                 (transfer_id, str(demand.id)))
            if created:
                await journal(db, je.accepted(transfer_id))
                await db.execute("INSERT INTO application_events(transfer_id,kind) VALUES(?,'accepted')", (transfer_id,))
            if created and observed and first_root and not await self._predecessor_cleanup_blocks(db, transfer_id):
                resource, state = observed
                await self._resource(db, transfer_id, resource, state)
                await db.execute("UPDATE transfer_requests SET state='waiting',resource=?,error=NULL WHERE id=?",
                                 (codec.dump(resource), first_root))
            await db.commit()
        return await self.get(transfer_id), created

    # ── submitter demand (1.0.13 ACL) ───────────────────────────────────────
    # Demand is an input to the existing lifecycle, never a lifecycle of its
    # own. A transfer's AFFECTED demand is its own plus the demand of every
    # transfer consolidated into one of its artifacts: destroying or stopping
    # it would deprive all of them. Outstanding = not withdrawn.

    _AFFECTED_DEMAND_SQL = """
        SELECT d.* FROM transfer_demands d WHERE d.transfer_id=:t AND d.withdrawn_at IS NULL
        UNION
        SELECT d.* FROM artifact_consolidations ac
            JOIN download_files f ON f.id=ac.canonical_artifact_id
            JOIN transfer_demands d ON d.transfer_id=ac.source_transfer_id AND d.withdrawn_at IS NULL
        WHERE f.torrent_id=:t"""

    async def hold_demand(self, db, transfer_id: int, expected) -> bool:
        """Inside a destructive transaction: whether the transfer's affected
        outstanding demand is still within ``expected``, the demand the act
        was authorized against (``None``: unconditional). Read under the
        same write transaction that commits the act, so no demand admitted
        or consolidated afterwards can be destroyed by it."""
        if expected is None:
            return True
        rows = await db.fetchall(self._AFFECTED_DEMAND_SQL, {"t": int(transfer_id)})
        return {str(row["id"]) for row in rows} <= {str(item) for item in expected}

    async def outstanding_demand_many(self, transfer_ids) -> dict[int, list[dict]]:
        """``outstanding_demand`` for a page of transfers in one read."""
        ids = sorted({int(item) for item in transfer_ids})
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        async with get_db() as db:
            rows = await db.fetchall(
                f"""SELECT d.*, d.transfer_id AS served_transfer_id FROM transfer_demands d
                    WHERE d.transfer_id IN ({marks}) AND d.withdrawn_at IS NULL
                    UNION
                    SELECT d.*, f.torrent_id AS served_transfer_id FROM artifact_consolidations ac
                        JOIN download_files f ON f.id=ac.canonical_artifact_id
                        JOIN transfer_demands d ON d.transfer_id=ac.source_transfer_id AND d.withdrawn_at IS NULL
                    WHERE f.torrent_id IN ({marks})""", (*ids, *ids))
        served: dict[int, list[dict]] = {item: [] for item in ids}
        for row in rows:
            served[int(row.pop("served_transfer_id"))].append(row)
        return served

    async def demand_history(self, transfer_id: int) -> list[dict]:
        """Every demand the transfer's work serves or served, outstanding or
        withdrawn: its own, and that of every transfer consolidated into one
        of its artifacts (``via_transfer_id``), oldest first. The ledger is
        the attribution: each row keeps the account and credential that
        submitted it, whatever became of them since."""
        async with get_db() as db:
            return await db.fetchall(
                """SELECT d.id, d.account_id, d.credential_id, d.origin, d.created_at, d.withdrawn_at,
                          NULL AS via_transfer_id
                     FROM transfer_demands d WHERE d.transfer_id=:t
                   UNION
                   SELECT d.id, d.account_id, d.credential_id, d.origin, d.created_at, d.withdrawn_at,
                          ac.source_transfer_id AS via_transfer_id
                     FROM artifact_consolidations ac
                     JOIN download_files f ON f.id=ac.canonical_artifact_id
                     JOIN transfer_demands d ON d.transfer_id=ac.source_transfer_id
                    WHERE f.torrent_id=:t AND ac.source_transfer_id<>:t
                   ORDER BY created_at, id""", {"t": int(transfer_id)})

    async def outstanding_demand(self, transfer_id: int) -> list[dict]:
        """Every outstanding demand that the transfer's work serves."""
        async with get_db() as db:
            return await db.fetchall(self._AFFECTED_DEMAND_SQL, {"t": int(transfer_id)})

    async def demand(self, demand_id: str) -> dict | None:
        async with get_db() as db:
            return await db.fetchone("SELECT * FROM transfer_demands WHERE id=?", (str(demand_id),))

    async def demands(self, *, account_ids=None, limit: int = 200) -> list[dict]:
        """Demand rows, newest first, with their transfer's public status;
        ``account_ids`` confines them to those owners."""
        where, params = "", []
        if account_ids is not None:
            ids = sorted({str(item) for item in account_ids if item})
            if not ids:
                return []
            where = f"WHERE d.account_id IN ({','.join('?' * len(ids))})"
            params.extend(ids)
        async with get_db() as db:
            return await db.fetchall(
                f"""SELECT d.id, d.transfer_id, d.account_id, d.credential_id, d.origin, d.created_at,
                        d.withdrawn_at, d.withdrawn_by, t.name, t.status, t.progress
                    FROM transfer_demands d JOIN torrents t ON t.id=d.transfer_id {where}
                    ORDER BY d.created_at DESC, d.id LIMIT ?""",
                (*params, max(1, min(int(limit), 1000))))

    @staticmethod
    async def withdraw_demand_in(db, *, demand_ids=(), account_id: str | None = None, by: str) -> list[int]:
        """Withdraw demand inside the caller's transaction: the given rows, or
        every outstanding demand of ``account_id``. Idempotent: a withdrawn
        row keeps its first withdrawal. Returns the transfers whose demand
        changed."""
        now = time.time()
        if account_id is not None:
            rows = await db.fetchall(
                "SELECT DISTINCT transfer_id FROM transfer_demands WHERE account_id=? AND withdrawn_at IS NULL",
                (str(account_id),))
            await db.execute(
                "UPDATE transfer_demands SET withdrawn_at=?, withdrawn_by=? WHERE account_id=? AND withdrawn_at IS NULL",
                (now, str(by), str(account_id)))
        else:
            ids = [str(item) for item in demand_ids]
            if not ids:
                return []
            marks = ",".join("?" * len(ids))
            rows = await db.fetchall(
                f"SELECT DISTINCT transfer_id FROM transfer_demands WHERE id IN ({marks}) AND withdrawn_at IS NULL", ids)
            await db.execute(
                f"UPDATE transfer_demands SET withdrawn_at=?, withdrawn_by=? WHERE id IN ({marks}) AND withdrawn_at IS NULL",
                (now, str(by), *ids))
        return [int(row["transfer_id"]) for row in rows]

    async def withdraw_demand(self, demand_ids, *, by: str) -> list[int]:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            changed = await self.withdraw_demand_in(db, demand_ids=demand_ids, by=by)
            await db.commit()
        return changed

    async def unwanted_transfers(self) -> list[int]:
        """Live transfers that had demand and now serve none: every demand of
        their own and of every transfer consolidated into their artifacts is
        withdrawn. Derived from durable demand alone, so an interrupted
        withdrawal is found again by the next cycle."""
        terminal = sorted(str(state) for state in TERMINAL_TRANSFER_STATES)
        marks = ",".join("?" * len(terminal))
        async with get_db() as db:
            rows = await db.fetchall(
                f"""SELECT t.id FROM torrents t
                    WHERE t.status NOT IN ({marks})
                      AND EXISTS (SELECT 1 FROM transfer_demands d WHERE d.transfer_id=t.id)
                      AND NOT EXISTS (SELECT 1 FROM transfer_demands d
                                      WHERE d.transfer_id=t.id AND d.withdrawn_at IS NULL)
                      AND NOT EXISTS (SELECT 1 FROM artifact_consolidations ac
                                      JOIN download_files f ON f.id=ac.canonical_artifact_id
                                      JOIN transfer_demands d ON d.transfer_id=ac.source_transfer_id
                                           AND d.withdrawn_at IS NULL
                                      WHERE f.torrent_id=t.id)
                    ORDER BY t.id""", terminal)
        return [int(row["id"]) for row in rows]

    async def retire_vacated_directory(self, artifact_id: int, *, root: str, target: str, expected_demand,
                                       settled=None) -> str:
        """Remove a completed collection artifact's directory if it is empty,
        in ONE write transaction that first proves, at its own commit, that
        the transfer is still completed, no authorized writer of the artifact
        is live, and its affected outstanding demand is still within
        ``expected_demand`` (the claims the caller found settled) -- so a
        claim, a reacquisition or a writer arriving meanwhile keeps it. The
        directory itself is removed only by ``filesystem.remove_empty_directory``
        (descriptor-relative, no-follow, empty only). ``settled(db)``: the
        caller's own proof that those claims are still settled, evaluated in
        this same transaction (so nothing that unsettles them can commit
        between that proof and the removal). A directory another live
        transfer has material planned in -- at or beneath ``target`` (the same
        folder reused by a later submission) -- is that transfer's too and is
        kept (``"claimed"``). Returns that outcome, or ``"not_eligible"`` /
        ``"claimed"`` / ``"unsettled"`` with nothing touched."""
        from transfers.filesystem import DIRECTORY_REMOVED, remove_empty_directory

        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                """SELECT f.torrent_id, f.status, t.status AS transfer_status FROM download_files f
                   JOIN torrents t ON t.id = f.torrent_id WHERE f.id=?""", (int(artifact_id),))
            live = await db.fetchone(
                """SELECT 1 FROM execution_attempts WHERE artifact_id=? AND authorized=1
                   AND state NOT IN ('succeeded','failed','cancelled','absent')""", (int(artifact_id),))
            if row is None or row["transfer_status"] != "completed" or row["status"] != "completed" or live:
                await db.rollback()
                return "not_eligible"
            if not await self.hold_demand(db, int(row["torrent_id"]), expected_demand):
                await db.rollback()
                return "claimed"
            prefix = str(target).rstrip("/") + "/"
            if await db.fetchone(
                    f"""SELECT 1 FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                        WHERE f.torrent_id!=? AND t.status NOT IN ({",".join("?" * len(_TERMINAL_TRANSFER_STATUSES))})
                        AND (f.local_path=? OR substr(f.local_path,1,?)=?) LIMIT 1""",
                    (int(row["torrent_id"]), *_TERMINAL_TRANSFER_STATUSES, str(target), len(prefix), prefix)):
                await db.rollback()
                return "claimed"
            if settled is not None and not await settled(db):
                await db.rollback()
                return "unsettled"
            outcome = remove_empty_directory(root, target)
            if outcome == DIRECTORY_REMOVED:
                await journal(db, je.directory_retired(transfer_id=int(row["torrent_id"]), artifact_id=int(artifact_id)))
            await db.commit()
            return outcome

    async def served_transfers(self, demand_ids) -> list[int]:
        """The transfers whose affected demand these demands are part of:
        each one's own transfer, and every transfer an artifact of it was
        consolidated into (``_AFFECTED_DEMAND_SQL``, read the other way)."""
        ids = json.dumps(sorted({str(item) for item in demand_ids if item}))
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT d.transfer_id AS id FROM transfer_demands d
                       WHERE d.id IN (SELECT value FROM json_each(:ids))
                   UNION
                   SELECT f.torrent_id AS id FROM transfer_demands d
                       JOIN artifact_consolidations ac ON ac.source_transfer_id=d.transfer_id
                       JOIN download_files f ON f.id=ac.canonical_artifact_id
                       WHERE d.id IN (SELECT value FROM json_each(:ids))
                   ORDER BY id""", {"ids": ids})
        return [int(row["id"]) for row in rows]

    @staticmethod
    async def _attest_verbatim(db, transfer_id: int, request_id: str, verbatim: tuple[bool, ...], ordinal: int) -> None:
        if ordinal < len(verbatim) and verbatim[ordinal] is True:
            await db.execute("INSERT INTO application_events(transfer_id,kind,detail,claimed) VALUES(?,?,?,1)",
                             (transfer_id, REQUEST_ADMITTED_VERBATIM, request_id))

    async def first_root_admitted_verbatim(self, transfer_id: int) -> bool:
        """Whether the root Details presents (the first root, in the order
        ``presentation`` reads them) was attested at admission as the resource
        received verbatim. Legacy and unattested roots are ``False``."""
        async with get_db() as db:
            row = await db.fetchone("""SELECT EXISTS(SELECT 1 FROM application_events e
                    WHERE e.kind=? AND e.transfer_id=r.transfer_id AND e.detail=r.id) AS attested
                FROM transfer_requests r WHERE r.transfer_id=? AND r.parent_id IS NULL
                ORDER BY r.ordinal,r.id LIMIT 1""", (REQUEST_ADMITTED_VERBATIM, transfer_id))
        return bool(row and row["attested"])

    @staticmethod
    async def _write_lifecycle_transition(
        db, transfer_id: int, current_status: str, current_progress, current_normalized_error,
        target: TransferState, *, progress=None, error=None,
    ) -> None:
        """Shared UPDATE+event-log body for a transfer status transition
        already validated by the caller (``transition_allowed``/
        ``expected_epoch``/no-op checks all happen before this is called).
        Shared by ``state()`` and ``aggregate_lifecycle()`` /
        ``force_queued_for_autonomous_wait()`` so the two paths can never
        silently diverge on what "the same transition" durably records."""
        # The durable error text, compared exactly as ever, except for its
        # diagnostics-only evidence: forensic facts are never a lifecycle change.
        current_error = _lifecycle_error(current_normalized_error)
        incoming_error = _lifecycle_error(codec.dump(error)) if error else None
        if current_status == target and (progress is None or current_progress == progress) and current_error == incoming_error:
            return
        await db.execute("""UPDATE torrents SET status=?, progress=COALESCE(?,progress), normalized_error=?,
            error_message=?, updated_at=CURRENT_TIMESTAMP,
            completed_at=CASE WHEN ?='completed' THEN COALESCE(completed_at,CURRENT_TIMESTAMP)
                WHEN ? IN ('pending','queued') THEN NULL ELSE completed_at END WHERE id=?""",
            (target, progress, codec.dump(error) if error else None, error.message if error else None, target, target, transfer_id))
        if target in SIDE_STATE_RETIRING_TRANSFER_STATES:
            await _retire_transfer_auxiliary_state_in_db(db, transfer_id)
        if current_status != target or current_error != incoming_error:
            await journal(db, je.lifecycle(transfer_id, current_status, target, error))
            await db.execute("INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,?,?)", (transfer_id, target, error.message if error else None))

    async def state(self, transfer_id: int, target: TransferState, *, progress=None, error=None, operator=False, expected_epoch=None, verified=False,
                    reset_postprocessing=False) -> bool:
        """One lifecycle transition, ``False`` when it is not allowed or the
        epoch moved. ``reset_postprocessing`` makes the transition also the
        post-processing reset (``_reset_postprocessing_in_db``) in the SAME
        transaction: a transfer that leaves delivery to deliver again (an
        operator Retry or reacquisition, a file selected on a completed
        transfer) never keeps its finished attempts -- which would stop the
        new delivery's processors from ever running -- and a transition that
        loses (epoch moved, live post-processing) changes nothing at all,
        never leaving a settled transfer stripped of its attempts and
        extraction status."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("SELECT status,progress,lifecycle_epoch,normalized_error FROM torrents WHERE id=?", (transfer_id,))
            if not row or not transition_allowed(TransferState(row["status"]), target, operator=operator, verified=verified):
                return False
            if expected_epoch is not None and row["lifecycle_epoch"] != expected_epoch:
                return False
            if reset_postprocessing and not await _reset_postprocessing_in_db(db, transfer_id):
                return False
            await self._write_lifecycle_transition(
                db, transfer_id, row["status"], row["progress"], row["normalized_error"], target, progress=progress, error=error,
            )
            await db.commit()
        return True

    async def cancel_with_execution_cleanup(self, transfer_id: int, *, expected_epoch: int, now: float,
                                            expected_demand=None) -> bool:
        """Atomically close logical lifecycle and persist external cleanup responsibility."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            if not await self.hold_demand(db, transfer_id, expected_demand):
                await db.rollback()
                raise DemandChanged(transfer_id)
            row = await db.fetchone(
                "SELECT status,progress,lifecycle_epoch,normalized_error FROM torrents WHERE id=?",
                (transfer_id,),
            )
            if not row:
                return False
            current = TransferState(row["status"])
            if current == TransferState.CANCELLED:
                await db.commit()
                return True
            if not transition_allowed(current, TransferState.CANCELLED) or row["lifecycle_epoch"] != expected_epoch:
                return False

            await db.execute(
                """UPDATE torrents SET status='cancelled',normalized_error=NULL,error_message=NULL,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (transfer_id,),
            )
            # FUNC-001: this path settles the parent into CANCELLED without
            # going through _write_lifecycle_transition, so it must invoke the
            # same transaction-local auxiliary-state retirement directly.
            await _retire_transfer_auxiliary_state_in_db(db, transfer_id)
            await journal(db, je.lifecycle(transfer_id, row["status"], TransferState.CANCELLED))
            await db.execute(
                "INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,'cancelled',NULL)",
                (transfer_id,),
            )
            await db.execute(
                """UPDATE execution_attempts SET cleanup_state='pending',cleanup_attempts=0,
                    cleanup_retry_at=?,cleanup_error=NULL
                    WHERE transfer_id=? AND authorized=1
                    AND state IN ('prepared','queued','running','paused','unknown')
                    AND id IN (SELECT execution_attempt_id FROM download_files
                        WHERE torrent_id=? AND execution_attempt_id IS NOT NULL)""",
                (now, transfer_id, transfer_id),
            )
            await db.execute(
                """UPDATE download_files SET status='cancelled',normalized_error=NULL,
                    continuation_reservation_expires_at=NULL,updated_at=CURRENT_TIMESTAMP
                    WHERE torrent_id=? AND status!='completed'""",
                (transfer_id,),
            )
            await db.commit()
        return True

    async def pending_execution_cleanup(self, now: float, *, transfer_id: int | None = None):
        async with get_db() as db:
            where_transfer = " AND e.transfer_id=?" if transfer_id is not None else ""
            params = (now, transfer_id) if transfer_id is not None else (now,)
            rows = await db.fetchall(
                """SELECT e.* FROM execution_attempts e JOIN torrents t ON t.id=e.transfer_id
                    WHERE e.cleanup_state IN ('pending','blocked') AND e.cleanup_retry_at<=?
                    AND t.status IN ('cancelled','deleted')""" + where_transfer +
                " ORDER BY e.transfer_id,e.created_at,e.id",
                params,
            )
        return tuple((self._execution_attempt(row), int(row["cleanup_attempts"] or 0), codec.error(row["cleanup_error"])) for row in rows)

    async def claim_execution_cleanup(self, attempt_id: str, *, now: float, lease_until: float) -> bool:
        async with get_db() as db:
            cursor = await db.execute(
                """UPDATE execution_attempts SET cleanup_state='pending',cleanup_retry_at=?,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND cleanup_state IN ('pending','blocked') AND cleanup_retry_at<=?""",
                (lease_until, attempt_id, now),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def execution_cleanup_attempt(self, attempt_id: str) -> bool:
        """Consume one destructive executor-cancel attempt after a cleanup lease is held."""
        async with get_db() as db:
            cursor = await db.execute(
                """UPDATE execution_attempts SET cleanup_attempts=cleanup_attempts+1,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND cleanup_state='pending'""",
                (attempt_id,),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def execution_cleanup_retry(self, attempt_id: str, error: NormalizedError, retry_at: float | None) -> None:
        async with get_db() as db:
            await db.execute(
                """UPDATE execution_attempts SET cleanup_state=?,cleanup_error=?,cleanup_retry_at=?,
                    updated_at=CURRENT_TIMESTAMP WHERE id=? AND cleanup_state='pending'""",
                ("pending" if retry_at is not None else "blocked", codec.dump(error), retry_at or 0, attempt_id),
            )
            await db.commit()

    async def execution_cleanup_complete(self, attempt_id: str) -> None:
        async with get_db() as db:
            await db.execute(
                """UPDATE execution_attempts SET cleanup_state='complete',cleanup_error=NULL,cleanup_retry_at=0,
                    authorized=0,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (attempt_id,),
            )
            await db.commit()

    async def execution_cleanup_status(self, attempt_id: str):
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT cleanup_state,cleanup_attempts,cleanup_retry_at,cleanup_error,authorized,state FROM execution_attempts WHERE id=?",
                (attempt_id,),
            )
        if not row:
            return None
        return {
            "state": row["cleanup_state"],
            "attempts": int(row["cleanup_attempts"] or 0),
            "retry_at": float(row["cleanup_retry_at"] or 0),
            "error": codec.error(row["cleanup_error"]),
            "authorized": bool(row["authorized"]),
            "execution_state": row["state"],
        }

    async def pause_intent(self, transfer_id: int, paused: bool) -> None:
        async with get_db() as db:
            await db.execute("""INSERT INTO transfer_pause_intents(torrent_id,paused,holder,generation) VALUES(?,?,NULL,1)
                ON CONFLICT(torrent_id) DO UPDATE SET paused=excluded.paused,holder=NULL,
                generation=transfer_pause_intents.generation+1,updated_at=CURRENT_TIMESTAMP""", (transfer_id, int(paused)))
            await db.commit()

    async def requests(self, transfer_id: int) -> tuple[RequestRecord, ...]:
        async with get_db() as db:
            rows = await db.fetchall("SELECT * FROM transfer_requests WHERE transfer_id=? ORDER BY parent_id,ordinal", (transfer_id,))
        return tuple(RequestRecord(row["id"], transfer_id, codec.request(codec.load(row["payload"])), row["state"],
                                   row["parent_id"], codec.resource(codec.load(row["resource"])), row["attempts"],
                                   row["retry_at"], codec.error(row["error"]), codec.entry(codec.load(row["metadata"])),
                                   codec.optional_request(codec.load(row["interpretation"])),
                                   row["alternative_group"]) for row in rows)

    async def failed_unverified_target(self, request_id: str) -> int | None:
        """The shared ``failed_unverified_target`` reader for callers without
        a session of their own (the equivalence owner's held-disposition
        boundary). One definition; this only supplies the session."""
        async with get_db() as db:
            return await failed_unverified_target(db, request_id)

    async def bound_route_provider(self, request_id: str) -> str | None:
        """Return the provider owning this request's route: the latest durable
        route attempt, else the transfer's collection route binding. A
        provider that positively declined the request, or that it exhausted,
        never owns its route, and a route whose latest attempt exhausted its
        provider -- in this routing campaign or an earlier one
        (``_ENDED_ROUTE_STATES``) -- has ended: nothing owns it until the next
        provider is selected. A provider whose route ended only until its
        re-entry owns a route it opens again afterwards."""
        async with get_db() as db:
            return (await self._bound_route_providers(db, (request_id,))).get(str(request_id))

    @classmethod
    async def _bound_route_providers(cls, db, request_ids) -> dict[str, str | None]:
        """``bound_route_provider``'s one rule for many requests, inside the
        caller's session and set-oriented (a bounded number of queries for any
        number of requests): every reader of a committed route -- routing,
        Details, the bounded list -- derives it here."""
        ids = list(dict.fromkeys(str(item) for item in request_ids))
        found: dict[str, str | None] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            excluded: dict[str, set[str]] = {}
            # A provider whose route merely ended on a transient failure
            # (``reentry_at``) owns the route it opens after re-entering.
            for row in await db.fetchall(
                    f"""SELECT DISTINCT request_id,provider_id FROM resolution_attempts
                    WHERE request_id IN ({marks})
                    AND (state='declined' OR (state='exhausted' AND reentry_at IS NULL))""", tuple(chunk)):
                excluded.setdefault(str(row["request_id"]), set()).add(str(row["provider_id"]))
            latest = {str(row["request_id"]): row for row in await db.fetchall(
                f"""SELECT request_id,provider_id,state FROM (
                    SELECT a.request_id,a.provider_id,a.state,
                        ROW_NUMBER() OVER (PARTITION BY a.request_id ORDER BY p.ordinal DESC) AS row_number
                    FROM route_attempt_provenance p JOIN resolution_attempts a ON a.id=p.resolution_attempt_id
                    WHERE a.request_id IN ({marks}) AND a.state!='declined') WHERE row_number=1""", tuple(chunk))}
            collection = {str(row["id"]): str(row.get("collection_route_provider_id") or "").strip()
                          for row in await db.fetchall(
                              f"""SELECT r.id,t.collection_route_provider_id FROM transfer_requests r
                              JOIN torrents t ON t.id=r.transfer_id WHERE r.id IN ({marks})""", tuple(chunk))}
            for request_id in chunk:
                own = excluded.get(request_id, set())
                row = latest.get(request_id)
                if (row and row.get("state") not in _ENDED_ROUTE_STATES and row.get("provider_id")
                        and str(row["provider_id"]) not in own):
                    found[request_id] = str(row["provider_id"])
                    continue
                if row and row.get("state") == "exhausted" and row.get("provider_id"):
                    # An exhausted latest route owns nothing, permanently or
                    # until its provider's re-entry: ``reentry_at`` governs a
                    # NEW route's eligibility and never revives this one
                    # through the collection binding.
                    own = own | {str(row["provider_id"])}
                value = collection.get(request_id, "")
                found[request_id] = value if value and value not in own else None
        return found

    @staticmethod
    async def _declined_route_providers(db, request_id: str) -> frozenset[str]:
        rows = await db.fetchall(
            "SELECT DISTINCT provider_id FROM resolution_attempts WHERE request_id=? AND state='declined'",
            (request_id,))
        return frozenset(str(row["provider_id"]) for row in rows)

    async def declined_route_providers(self, request_id: str) -> frozenset[str]:
        """The providers that positively declined this request after their
        probe: they have left its provider competition for good."""
        async with get_db() as db:
            return await self._declined_route_providers(db, request_id)

    @staticmethod
    async def _exhausted_route_providers(db, request_id: str, now: float | None = None) -> frozenset[str]:
        rows = await db.fetchall(
            "SELECT DISTINCT provider_id FROM resolution_attempts WHERE request_id=? AND state='exhausted' "
            "AND (? IS NULL OR reentry_at IS NULL OR reentry_at>?)", (request_id, now, now))
        return frozenset(str(row["provider_id"]) for row in rows)

    async def exhausted_route_providers(self, request_id: str, now: float | None = None) -> frozenset[str]:
        """The providers exhausted for this request in its CURRENT routing
        campaign. Durable, so a restart cannot hand the request straight back
        to one of them; released by the next campaign (``retry_requests``).
        With ``now``, a provider whose route ended on a transient failure
        (``reentry_at``) counts only until it may re-enter competition."""
        async with get_db() as db:
            return await self._exhausted_route_providers(db, request_id, now)

    async def provider_reentries(self, request_id: str) -> dict[str, tuple[int, float]]:
        """Per provider whose routes of this request ended on a transient
        failure in the current campaign: ``(how many, the latest re-entry
        instant)``. The provider's own retry budget is spent against the
        first; the second is when it may compete again."""
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT provider_id,COUNT(*) AS failures,MAX(reentry_at) AS reentry_at FROM resolution_attempts
                   WHERE request_id=? AND state='exhausted' AND reentry_at IS NOT NULL GROUP BY provider_id""",
                (request_id,))
        return {str(row["provider_id"]): (int(row["failures"]), float(row["reentry_at"])) for row in rows}

    async def exhaust_route(self, request_id: str, provider_id: str, error: NormalizedError, *,
                            continues: bool, reentry_at: float | None = None, wake_at: float = 0.0) -> bool:
        """THE provider-exhaustion transition, decided atomically.

        ``provider_id``'s route of this root request is exhausted under policy
        (``TransferPolicy.retry_resolution`` -> ``TRY_ALTERNATE_PROVIDER``),
        the same way whether or not another provider remains. In one
        transaction: the route's latest attempt records the exhaustion and its
        normalized error (so route history explains it); the provider resource
        it bound, when DebridPulse owns its cleanup (``CREATED``/``ADOPTED``,
        not already absent), gets the ordinary owned cleanup intent the one
        cleanup cadence then drains; and the route ends -- the request holds
        no resource. A transient provider failure ends the route the same
        way with ``reentry_at``: the provider is excluded from automatic
        competition only until then (``exhausted_route_providers(now=)``).
        Then, when another provider remains or will re-enter (``continues``),
        the request returns to ``pending`` -- runnable at once, or at
        ``wake_at`` when nothing can run before then -- with a fresh budget;
        otherwise it fails with this provider's error, every provider of the
        campaign exhausted. The logical request, its transfer and its lineage
        are untouched. Returns ``False`` -- changing nothing -- when
        the request is no longer live root resolution work, its route is no
        longer ``provider_id``'s, or the route already produced executable
        work -- member requests a generation of its own resource decomposed
        into, or artifacts it materialized: those, their routes and their material are the bound
        provider's own issue, and nothing can hand them to another provider."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                """SELECT r.resource,r.transfer_id FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id
                   WHERE r.id=? AND r.parent_id IS NULL AND r.state NOT IN ('resolved','skipped')
                   AND t.status NOT IN ('deleted','completed','consolidated','cancelled')""", (request_id,))
            latest = await db.fetchone(
                """SELECT a.id,a.provider_id,a.state FROM route_attempt_provenance p
                   JOIN resolution_attempts a ON a.id=p.resolution_attempt_id
                   WHERE a.request_id=? AND a.state!='declined' ORDER BY p.ordinal DESC LIMIT 1""", (request_id,))
            resource = codec.resource(codec.load(row["resource"])) if row and row["resource"] else None
            # Executable work THIS route produced: members fanned out by a
            # decomposition generation of its own resource binding (a member
            # from before generations existed counts as its own), or the
            # root's own artifact. A previous route's generation -- the
            # decomposition an operator switch moved away from -- is not this
            # route's, and is rebuilt in place on whichever route commits next.
            decomposed = (await db.fetchone(
                """SELECT 1 FROM transfer_requests c WHERE c.parent_id=? AND (c.materialized_selection_id IS NULL
                   OR c.materialized_selection_id IN (SELECT s.id FROM transfer_file_selections s
                       JOIN provider_resources b ON b.id=s.provider_resource_id
                       WHERE s.request_id=? AND b.resource_key=?)) LIMIT 1""",
                (request_id, request_id, resource.id if resource else None))
                or await db.fetchone("SELECT 1 FROM download_files WHERE request_id=? LIMIT 1", (request_id,)))
            if (not row or decomposed or not latest or str(latest["provider_id"]) != provider_id
                    or latest["state"] in _ENDED_ROUTE_STATES):
                await db.rollback()
                return False
            error_blob = codec.dump(error)
            await db.execute("UPDATE resolution_attempts SET state='failed',error=?,updated_at=CURRENT_TIMESTAMP "
                             "WHERE request_id=? AND state='started' AND id!=?", (error_blob, request_id, latest["id"]))
            await db.execute("UPDATE resolution_attempts SET state='exhausted',error=?,reentry_at=?,"
                             "updated_at=CURRENT_TIMESTAMP WHERE id=?", (error_blob, reentry_at, latest["id"]))
            await db.execute("UPDATE route_attempt_provenance SET outcome='failed',updated_at=CURRENT_TIMESTAMP "
                             "WHERE resolution_attempt_id=?", (latest["id"],))
            await journal(db, je.route_ended("exhausted", transfer_id=row["transfer_id"], request_id=request_id,
                                             attempt_id=latest["id"], provider_id=provider_id, error=error))
            if (resource is not None and resource.provider_id == provider_id
                    and resource.ownership in {Ownership.CREATED, Ownership.ADOPTED}):
                binding = await db.fetchone(
                    "SELECT state FROM provider_resources WHERE transfer_id=? "
                    "AND (resource_key=? OR (resource_key IS NULL AND id=?))",
                    (row["transfer_id"], resource.id, resource.id))
                if binding and binding["state"] != ResourceState.ABSENT.value:
                    await self.cleanup_intent(row["transfer_id"], resource.id, CleanupAuthority.OWNED, db=db)
            if continues:
                # A request that must wait keeps the failure it waits on.
                await db.execute("UPDATE transfer_requests SET state='pending',resource=NULL,retry_at=?,error=?,"
                                 "attempts=0 WHERE id=?", (wake_at, error_blob if wake_at else None, request_id))
            else:
                await db.execute("UPDATE transfer_requests SET state='failed',resource=NULL,retry_at=0,error=? "
                                 "WHERE id=?", (error_blob, request_id))
                await self._admit_next_alternative(db, request_id)
            await db.commit()
        return True

    async def decline_route(self, attempt: ResolutionAttempt) -> str:
        """THE post-probe decline boundary, decided atomically.

        A provider may decline a request only before it has resolved it: once
        one of its resolution attempts of this request succeeded, or the
        request holds a provider resource, the route is bound and a later
        reading that the resource is not the provider's is an ordinary failure
        of that route -- competition is never reopened. Otherwise the attempt
        and its route provenance record the decline, and the request returns
        to ``pending`` without its error and without spending the attempt (a
        decline is not a failure), ready for the next provider of the same
        competition. Returns ``"declined"``, ``"bound"``, or ``"gone"`` when
        the request is no longer live resolution work.
        """
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                """SELECT r.resource,r.transfer_id,t.status FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id
                   WHERE r.id=? AND r.state IN ('resolving','input_required')""", (attempt.request_id,))
            if not row or row["status"] in {"deleted", "completed", "consolidated", "cancelled"}:
                await db.rollback()
                return "gone"
            bound = row["resource"] is not None or await db.fetchone(
                """SELECT 1 FROM resolution_attempts WHERE request_id=? AND provider_id=? AND state='succeeded'
                   LIMIT 1""", (attempt.request_id, attempt.provider_id))
            if bound:
                await db.rollback()
                return "bound"
            await db.execute("UPDATE resolution_attempts SET state='declined',error=NULL,updated_at=CURRENT_TIMESTAMP "
                             "WHERE id=? AND provider_id=?", (attempt.id, attempt.provider_id))
            await db.execute("UPDATE route_attempt_provenance SET outcome='declined',updated_at=CURRENT_TIMESTAMP "
                             "WHERE resolution_attempt_id=?", (attempt.id,))
            await journal(db, je.route_ended("declined", transfer_id=row["transfer_id"], request_id=attempt.request_id,
                                             attempt_id=attempt.id, provider_id=attempt.provider_id))
            await db.execute("UPDATE transfer_requests SET state='pending',retry_at=0,error=NULL,"
                             "attempts=MAX(0,attempts-1) WHERE id=?", (attempt.request_id,))
            await db.commit()
        return "declined"

    # -- explicit alternative-source groups -----------------------------------
    #
    # Roots of one transfer sharing ``alternative_group`` are the operator's
    # alternate sources for ONE logical member, preferred in ``ordinal``
    # order. At most one of them is ever selected (any state but ``skipped``
    # and ``failed``); the others are dormant ``skipped`` roots, which no
    # cohort, canonical or lifecycle owner counts as material and no
    # scheduler pass resolves. Selection moves only by the transitions below,
    # each inside the caller's one immediate transaction, so concurrent passes
    # and restarts see exactly one selected alternative. A dormant root never
    # attempted after its group was satisfied simply stays ``skipped`` with no
    # attempt: no failure or evidence is ever recorded for it.

    @staticmethod
    async def _alternatives(db, request_id: str):
        """``(group members in submitted order, live)`` for the explicit group
        of root ``request_id``; ``((), False)`` for an ordinary request."""
        row = await db.fetchone(
            """SELECT r.transfer_id,r.alternative_group,t.status FROM transfer_requests r
               JOIN torrents t ON t.id=r.transfer_id
               WHERE r.id=? AND r.parent_id IS NULL AND r.alternative_group IS NOT NULL""", (request_id,))
        if not row:
            return (), False
        members = await db.fetchall(
            """SELECT r.id,r.state,r.attempts,
                      EXISTS(SELECT 1 FROM download_files f WHERE f.request_id=r.id) AS materialized
               FROM transfer_requests r WHERE r.transfer_id=? AND r.parent_id IS NULL AND r.alternative_group=?
               ORDER BY r.ordinal""", (row["transfer_id"], row["alternative_group"]))
        return tuple(members), row["status"] not in {"deleted", "completed", "consolidated", "cancelled"}

    @classmethod
    async def _admit_next_alternative(cls, db, request_id: str) -> None:
        """Hand the group of ``request_id`` -- which just stopped being its
        selected alternative -- to the first dormant alternative in submitted
        order. Nothing while another member still holds the group (it is
        active, or it satisfied the member) or when none is left: the group
        then settles as its members did."""
        members, live = await cls._alternatives(db, request_id)
        if not live or any(item["state"] not in {"skipped", "failed"} for item in members):
            return
        following = next((item for item in members if item["state"] == "skipped" and not item["materialized"]), None)
        if following is not None:
            await db.execute("UPDATE transfer_requests SET state='pending',retry_at=0,error=NULL "
                             "WHERE id=? AND state='skipped'", (following["id"],))

    async def select_alternative(self, request_id: str, chosen_id: str) -> bool:
        """Move a group's selection from its never-attempted selected root
        ``request_id`` to the never-attempted dormant alternative
        ``chosen_id`` -- cache-first preference, decided by core before
        either was resolved. ``False``, changing nothing, unless both are
        still exactly that."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            members, live = await self._alternatives(db, request_id)
            current = {item["id"]: item for item in members}
            selected, chosen = current.get(request_id), current.get(chosen_id)
            if (not live or selected is None or chosen is None or selected["state"] != "pending"
                    or selected["attempts"] or chosen["state"] != "skipped" or chosen["attempts"]
                    or chosen["materialized"]):
                await db.rollback()
                return False
            await db.execute("UPDATE transfer_requests SET state='skipped',retry_at=0 WHERE id=?", (request_id,))
            await db.execute("UPDATE transfer_requests SET state='pending',retry_at=0,error=NULL WHERE id=?", (chosen_id,))
            await db.commit()
        return True

    async def defer_alternative(self, attempt: ResolutionAttempt, error: NormalizedError) -> bool:
        """A cached-only resolution of a selected alternative found its source
        no longer cached and created nothing: the attempt records that, the
        alternative returns to dormancy (its attempt kept as history, so it is
        never again preferred as cached) and the group passes to its first
        dormant alternative in submitted order. ``False`` when the request is
        no longer this live resolution."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            members, live = await self._alternatives(db, attempt.request_id)
            current = next((item for item in members if item["id"] == attempt.request_id), None)
            if not live or current is None or current["state"] != "resolving":
                await db.rollback()
                return False
            error_blob = codec.dump(error)
            ended = await db.execute("UPDATE resolution_attempts SET state='failed',error=?,updated_at=CURRENT_TIMESTAMP "
                                     "WHERE id=? AND state='started'", (error_blob, attempt.id))
            await db.execute("UPDATE route_attempt_provenance SET outcome='failed',updated_at=CURRENT_TIMESTAMP "
                             "WHERE resolution_attempt_id=?", (attempt.id,))
            if ended.rowcount:
                owner = await db.fetchone("SELECT transfer_id FROM transfer_requests WHERE id=?", (attempt.request_id,))
                await journal(db, je.route_ended("failed", transfer_id=owner["transfer_id"], request_id=attempt.request_id,
                                                 attempt_id=attempt.id, provider_id=attempt.provider_id, error=error))
            await db.execute("UPDATE transfer_requests SET state='skipped',retry_at=0,error=NULL WHERE id=?",
                             (attempt.request_id,))
            await self._admit_next_alternative(db, attempt.request_id)
            await db.commit()
        return True

    @staticmethod
    async def _settle_alternative_selection(db, transfer_id: int) -> None:
        """After an operator/recovery requeue: every explicit group of the
        transfer keeps at most one selected alternative. A group whose member
        is active or satisfied the member keeps it, and anything requeued
        beside it returns to dormancy; otherwise the first requeued member in
        submitted order is selected."""
        groups = await db.fetchall(
            """SELECT DISTINCT alternative_group FROM transfer_requests
               WHERE transfer_id=? AND parent_id IS NULL AND alternative_group IS NOT NULL""", (transfer_id,))
        for group in groups:
            members = await db.fetchall(
                """SELECT id,state FROM transfer_requests WHERE transfer_id=? AND parent_id IS NULL
                   AND alternative_group=? ORDER BY ordinal""", (transfer_id, group["alternative_group"]))
            pending = [item["id"] for item in members if item["state"] == "pending"]
            held = any(item["state"] not in {"pending", "skipped", "failed"} for item in members)
            for request_id in pending[0 if held else 1:]:
                await db.execute("UPDATE transfer_requests SET state='skipped',retry_at=0 WHERE id=? AND state='pending'",
                                 (request_id,))

    @classmethod
    async def _route_basis(cls, db, request_id: str) -> tuple:
        """The routing basis of a request, inside the caller's session: the
        latest route attempt (id and state) of the request and of its root.
        Every route change -- a resolution, a failure or exhaustion, an
        operator switch, a readiness yield, a fan-out's release -- opens a
        route attempt or ends one, so a decision taken on one basis is
        current only while the basis is unchanged."""
        row = await db.fetchone("SELECT parent_id FROM transfer_requests WHERE id=?", (request_id,))
        basis = []
        for owner in (request_id, row["parent_id"] if row else None):
            latest = await db.fetchone(
                """SELECT a.id,a.state FROM route_attempt_provenance p
                   JOIN resolution_attempts a ON a.id=p.resolution_attempt_id
                   WHERE p.request_id=? ORDER BY p.ordinal DESC LIMIT 1""", (owner,)) if owner else None
            basis.append((str(latest["id"]), str(latest["state"])) if latest else None)
        return tuple(basis)

    async def route_basis(self, request_id: str) -> tuple:
        """``_route_basis`` read on its own: taken before a route is decided
        and handed to ``begin_resolution`` with the decision."""
        async with get_db() as db:
            return await self._route_basis(db, request_id)

    async def begin_resolution(self, request_id: str, provider_id: str, *,
                               routing_decision: str | None = None,
                               expected_request=None, expected_basis=None) -> ResolutionAttempt | None:
        """``routing_decision``: the canonical selector's encoded decision that
        chose ``provider_id`` (``transfers.registry.RoutingDecision``), kept
        with this route attempt as historical causality; it replaces any hold
        the request recorded while it waited (``record_route_decision``).

        The route was decided outside this transaction, so it opens only while
        what it was decided on still holds -- otherwise nothing begins and the
        next pass routes the request from current truth:

        * the routing basis (``_route_basis``) is still ``expected_basis``,
          the basis read before the route was decided: no route of the request
          or its root opened or ended since (an operator switch, a readiness
          yield, an exhaustion), whatever the request now looks like;
        * no route an operator switch or a readiness yield pinned for another
          provider is open on the request (that route is adopted by
          ``begin_pinned_resolution``; a decision made before it committed must
          not open beside it);
        * the request is still ``expected_request``, the request the provider
          will be asked about (a fan-out did not rewrite it meanwhile);
        * a manifest member's decomposition generation is not superseded
          (``_decomposition_admission`` STALE: its root was rebound or holds
          no binding since). A held generation is not refused here."""
        identity = new_identity()
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT r.* FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id WHERE r.id=? AND r.state='pending'
                AND t.status NOT IN ('deleted','completed','consolidated','cancelled') AND COALESCE(p.paused,0)=0""", (request_id,))
            if not row:
                return None
            pinned = await db.fetchone(
                """SELECT a.provider_id FROM route_attempt_provenance p
                   JOIN resolution_attempts a ON a.id=p.resolution_attempt_id
                   WHERE a.request_id=? AND a.state='started' AND p.operation IN (?,?)
                   ORDER BY p.ordinal DESC LIMIT 1""", (request_id, *sorted(self.READINESS_PINNED_OPERATIONS)))
            superseded = (
                (expected_basis is not None and await self._route_basis(db, request_id) != tuple(expected_basis))
                or (pinned is not None and str(pinned["provider_id"]) != str(provider_id))
                or (expected_request is not None and codec.request(codec.load(row["payload"])) != expected_request)
                or (row["parent_id"] is not None and (await self._decomposition_admission(
                    db, SimpleNamespace(request_id=request_id, transfer_id=row["transfer_id"]))).kind
                    == MaterializationAdmissionKind.STALE))
            if superseded:
                await db.rollback()
                return None
            await db.execute("UPDATE transfer_requests SET state='resolving',attempts=attempts+1,routing_decision=NULL WHERE id=?",
                             (request_id,))
            await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES(?,?,?,'started')", (identity, request_id, provider_id))
            await self._begin_route_provenance(db, identity, row["transfer_id"], request_id, provider_id, operation="resolve",
                                               routing_decision=routing_decision)
            await db.commit()
        return ResolutionAttempt(identity, request_id, provider_id, "started")

    async def record_route_decision(self, request_id: str, routing_decision: str) -> None:
        """Record why a pending root request's routing started no attempt (the
        canonical selector held it, or nothing can take it). Visibility only:
        nothing reads it back to decide anything."""
        async with get_db() as db:
            await db.execute("UPDATE transfer_requests SET routing_decision=? WHERE id=? AND state='pending'",
                             (routing_decision, request_id))
            await db.commit()

    @staticmethod
    def _resource_binding_id(transfer_id: int, resource_key: str) -> str:
        """Durable (transfer, canonical-resource) binding-generation identity.

        Derived only from the neutral canonical resource id (``ProviderResource.id``),
        never a provider-native field. Distinct transfers that bind the *same*
        canonical resource get distinct binding ids, so an identical native
        resource and identical file tree can never alias one transfer's
        manifest/selection generation into another's.
        """
        return uuid5(NAMESPACE_URL, f"transfer-provider-resource:{int(transfer_id)}:{resource_key}").hex

    @classmethod
    async def _resolve_binding(cls, db, transfer_id: int, resource_key: str) -> str | None:
        """The persisted binding-generation id for (transfer, canonical resource).

        Matches a new-model row by ``resource_key`` and a pre-split historical row
        (whose primary key *is* the canonical id) by ``id``.
        """
        row = await db.fetchone(
            "SELECT id FROM provider_resources WHERE transfer_id=? "
            "AND (resource_key=? OR (resource_key IS NULL AND id=?))",
            (transfer_id, resource_key, resource_key),
        )
        return row["id"] if row else None

    async def resource_binding_id(self, transfer_id: int, resource_key: str) -> str:
        """The binding-generation id core keys file-selection state on. Falls back
        to the computed id when the binding is not yet persisted."""
        async with get_db() as db:
            existing = await self._resolve_binding(db, transfer_id, resource_key)
        return existing or self._resource_binding_id(transfer_id, resource_key)

    @classmethod
    async def _resource(cls, db, transfer_id: int, resource: ProviderResource, state: ResourceState, *,
                        journaled: bool = True) -> str:
        """Persist/refresh the (transfer, canonical-resource) binding row.

        ``resource.id`` is the canonical, transfer-independent DP resource identity
        and is stored as ``resource_key`` and inside the payload unchanged. The
        row primary key is the binding-generation id. Returns that binding id.
        """
        resource_key = resource.id
        binding_id = await cls._resolve_binding(db, transfer_id, resource_key)
        if binding_id is None:
            # First binding for this (transfer, canonical resource). Two *live*
            # transfers may never share one native resource; a retired predecessor
            # (deleted / cancelled / consolidated, or any generation whose active
            # key a later submission retired) sharing it is the ordinary re-add
            # generation case and is allowed to coexist.
            other = await db.fetchone(
                "SELECT r.transfer_id, t.status, t.hash FROM provider_resources r JOIN torrents t ON t.id=r.transfer_id "
                "WHERE (r.resource_key=? OR (r.resource_key IS NULL AND r.id=?)) AND r.transfer_id != ? "
                "AND t.status NOT IN ('deleted','cancelled','consolidated') "
                "AND t.hash NOT LIKE 'deleted:%' AND t.hash NOT LIKE 'retired:%' LIMIT 1",
                (resource_key, resource_key, transfer_id),
            )
            if other:
                raise TransferError(NormalizedError(Domain.LIFECYCLE, Category.OWNERSHIP_CONFLICT, Stage.RESOLUTION))
            binding_id = cls._resource_binding_id(transfer_id, resource_key)
        existing = await db.fetchone(
            "SELECT transfer_id, provider_id, payload, state FROM provider_resources WHERE id=?", (binding_id,),
        )
        if existing and (existing["transfer_id"] != transfer_id or existing["provider_id"] != resource.provider_id):
            raise TransferError(NormalizedError(Domain.LIFECYCLE, Category.OWNERSHIP_CONFLICT, Stage.RESOLUTION))
        if existing:
            resource = replace(resource, ownership=codec.resource(codec.load(existing["payload"])).ownership)
        await db.execute(
            """INSERT INTO provider_resources(id,transfer_id,provider_id,payload,state,resource_key) VALUES(?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,state=excluded.state,
                   resource_key=COALESCE(provider_resources.resource_key, excluded.resource_key),
                   preparing_bytes=CASE WHEN excluded.state='preparing' THEN provider_resources.preparing_bytes END,
                   preparing_since=CASE WHEN excluded.state='preparing' THEN provider_resources.preparing_since END,
                   updated_at=CURRENT_TIMESTAMP""",
            (binding_id, transfer_id, resource.provider_id, codec.dump(resource), state, resource_key),
        )
        previous = existing["state"] if existing else None
        if journaled and previous != str(state):
            await journal(db, je.resource_state(transfer_id=transfer_id, binding_id=binding_id,
                                                provider_id=resource.provider_id, previous=previous, state=state))
        return binding_id

    async def resolution(self, attempt: ResolutionAttempt, result: ResolutionResult) -> bool | None:
        """Accept ``attempt``'s answer. ``True``: accepted for a live transfer;
        ``False``: accepted, the transfer is no longer live work; ``None``: not
        accepted -- the attempt is no longer open, so the answer has no route
        to answer for (callers treat it as not live, and owe it nothing)."""
        # Defense in depth: route identity is selected by the universal core.
        identities = [candidate.provider_id for candidate in result.candidates]
        identities.extend(candidate.resource.provider_id for candidate in result.candidates if candidate.resource)
        if result.observation:
            identities.append(result.observation.resource.provider_id)
        if any(identity and identity != attempt.provider_id for identity in identities):
            raise TransferError(NormalizedError(
                Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION,
                integration_id=attempt.provider_id,
            ))
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT r.transfer_id,t.status FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id
                WHERE r.id=?""", (attempt.request_id,))
            if not row:
                return False
            error = codec.dump(result.error) if result.error else None
            status = "failed" if result.error else "succeeded"
            # The answer is the attempt's only while the attempt is still open
            # -- resolving, or waiting on its input question. One that a route
            # replacement released (or that already ended) has no route left
            # to answer for: decided here, in this transaction, so no switch
            # can commit between the decision and the writes. An attempt that
            # now names its resource owes no reconciliation of a creation it
            # may have made: that creation is this resource.
            accepted = await db.execute(
                "UPDATE resolution_attempts SET state=?,error=?,result=?,updated_at=CURRENT_TIMESTAMP,"
                "reconcile_at=CASE WHEN ? THEN NULL ELSE reconcile_at END "
                "WHERE id=? AND state IN ('started','input_required')",
                (status, error, codec.dump(result), bool(result.observation), attempt.id))
            if not accepted.rowcount:
                # A late answer changes nothing it no longer owns -- not the
                # attempt, its provenance, the journal or the request -- but a
                # resource it names was created all the same: it is recorded,
                # never bound to the request, with the owned cleanup a replaced
                # route's resources get while the transfer lives (a deleted
                # transfer's own Delete decides its resources).
                if result.observation:
                    observed = result.observation
                    owned = (observed.resource.ownership in {Ownership.CREATED, Ownership.ADOPTED}
                             and observed.state != ResourceState.ABSENT
                             and row["status"] not in {"deleted", "completed", "consolidated", "cancelled"})
                    await self._record_unrouted_resource(db, row["transfer_id"], observed.resource, observed.state,
                                                         CleanupAuthority.OWNED if owned else None)
                    await db.execute("UPDATE resolution_attempts SET reconcile_at=NULL WHERE id=?", (attempt.id,))
                await db.commit()
                return None
            if result.observation:
                # Persist even after Delete so late-created remote resources can
                # be cleaned up without reviving the transfer.
                await self._resource(db, row["transfer_id"], result.observation.resource, result.observation.state)
            await db.execute("""UPDATE route_attempt_provenance SET outcome=?,candidate_summary=?,updated_at=CURRENT_TIMESTAMP
                WHERE resolution_attempt_id=?""",
                ("failed" if result.error else "resolved", self._candidate_summary(result.candidates), attempt.id))
            await journal(db, je.route_ended("failed" if result.error else "resolved", transfer_id=row["transfer_id"],
                                             request_id=attempt.request_id, attempt_id=attempt.id,
                                             provider_id=attempt.provider_id, error=result.error))
            resource = codec.dump(result.observation.resource) if result.observation else None
            request_state = "failed" if result.error else "waiting" if result.observation and not result.candidates else "materializing" if result.candidates else "resolved"
            if row["status"] not in {"deleted", "completed", "consolidated", "cancelled"}:
                await db.execute("UPDATE transfer_requests SET state=?,resource=COALESCE(?,resource),error=? WHERE id=?",
                                 (request_state, resource, error, attempt.request_id))
            await db.commit()
        return row["status"] not in {"deleted", "completed", "consolidated", "cancelled"}

    async def hold_uncertain_resolution(self, attempt: ResolutionAttempt, error: NormalizedError, retry_at: float,
                                        *, consume_attempt: bool = False) -> bool:
        """Keep a root's creation attempt open while its provider may have
        created the resource although no answer named it (``error`` is that
        uncertain failure). The attempt stays ``started`` -- the route's own,
        never a failure that frees it for another creation -- carrying the
        error, and owes a reconciliation at ``retry_at`` (``reconcile_at``):
        the durable obligation, which outlives the route and the transfer
        until reconciliation settles it (``settle_creation``). The request
        waits, ``pending``, until then, when its next resolution settles the
        attempt first (``TransferEngine._settle_uncertain_resolution``).
        ``consume_attempt``: an inconclusive settlement spends one of the
        request's attempts. ``False`` when the route cannot hold: the attempt
        is no longer open, or its request or transfer is (deleted meanwhile)
        -- the obligation is still recorded on an open attempt then, and
        reconciled without its route (``_settle_unowned_creations``)."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            error_blob = codec.dump(error)
            opened = await db.execute(
                "UPDATE resolution_attempts SET error=?,reconcile_at=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND state='started'", (error_blob, retry_at, attempt.id))
            if not opened.rowcount:
                await db.rollback()
                return False
            held = await db.execute(
                """UPDATE transfer_requests SET state='pending',error=?,retry_at=?,attempts=attempts+? WHERE id=?
                   AND state IN ('resolving','pending') AND transfer_id IN (SELECT id FROM torrents
                   WHERE status NOT IN ('deleted','completed','consolidated','cancelled'))""",
                (error_blob, retry_at, int(consume_attempt), attempt.request_id))
            await db.commit()
        return bool(held.rowcount)

    async def arm_creation(self, attempt_id: str, at: float) -> bool:
        """Owe a creation reconciliation BEFORE a productive provider call
        whose outcome its inventory can settle: from here until the outcome
        is known -- a resource bound (``resolution``), a definitive
        non-creation (``settle_creation``) -- a process that ends mid-call
        leaves the obligation, never a bare interrupted attempt."""
        async with get_db() as db:
            armed = await db.execute(
                "UPDATE resolution_attempts SET reconcile_at=? WHERE id=? AND state='started'", (at, attempt_id))
            await db.commit()
        return bool(armed.rowcount)

    async def armed_resolution(self, request_id: str) -> ResolutionAttempt | None:
        """The request's open attempt that was armed (``arm_creation``) and
        never learned its outcome -- the process ended mid-call; ``None`` for
        an unarmed one, which is never read as a creation."""
        async with get_db() as db:
            row = await db.fetchone(
                """SELECT id,provider_id FROM resolution_attempts WHERE request_id=? AND state='started'
                   AND reconcile_at IS NOT NULL ORDER BY rowid DESC LIMIT 1""", (request_id,))
        return ResolutionAttempt(str(row["id"]), request_id, str(row["provider_id"]), "started") if row else None

    async def uncertain_resolution(self, request_id: str) -> tuple[ResolutionAttempt, NormalizedError] | None:
        """The request's open creation attempt that still owes a
        reconciliation (``hold_uncertain_resolution``), with its uncertain
        error; ``None`` for anything else -- an attempt merely interrupted
        owes none and is never read as one."""
        async with get_db() as db:
            row = await db.fetchone(
                """SELECT id,provider_id,error FROM resolution_attempts WHERE request_id=? AND state='started'
                   AND reconcile_at IS NOT NULL ORDER BY rowid DESC LIMIT 1""", (request_id,))
        error = codec.error(row["error"]) if row else None
        if error is None:
            return None
        return ResolutionAttempt(str(row["id"]), request_id, str(row["provider_id"]), "started", error), error

    async def defer_creation_reconciliation(self, attempt_id: str, at: float) -> None:
        """An owed reconciliation that could not settle yet is next due ``at``."""
        async with get_db() as db:
            await db.execute("UPDATE resolution_attempts SET reconcile_at=? WHERE id=? AND reconcile_at IS NOT NULL",
                             (at, attempt_id))
            await db.commit()

    async def unowned_creations(self, now: float) -> tuple[dict, ...]:
        """Owed reconciliations no route settles any more -- the attempt's
        route ended (an inconclusive settlement spent its budget, or another
        transition closed the attempt), or its transfer was deleted or
        finished -- due by ``now``: ``attempt_id``, ``provider_id``,
        ``request_id``, ``transfer_id``, ``status``, ``delete_remote``.
        Deleting a transfer never discards the obligation; only
        ``settle_creation`` does."""
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT a.id AS attempt_id,a.provider_id,a.request_id,r.transfer_id,t.status,t.delete_remote
                   FROM resolution_attempts a JOIN transfer_requests r ON r.id=a.request_id
                   JOIN torrents t ON t.id=r.transfer_id
                   WHERE a.reconcile_at IS NOT NULL AND a.reconcile_at<=?
                   AND (a.state!='started' OR t.status IN ('deleted','completed','consolidated','cancelled'))
                   ORDER BY a.reconcile_at""", (now,))
        return tuple(dict(row) for row in rows)

    async def settle_creation(self, attempt_id: str, *, transfer_id: int | None = None,
                              resource: ProviderResource | None = None, state: ResourceState | None = None,
                              cleanup: str | None = None) -> str:
        """Settle an owed reconciliation, atomically. Without ``resource``:
        proven absent (or nothing to own). With it: the creation found --
        bound to the transfer as DebridPulse's ordinary resource and, with
        ``cleanup``, given that cleanup intent so the one cleanup cadence
        removes it. A resource this transfer already binds keeps its own
        lifecycle (``"known"``); one another live transfer holds is not
        DebridPulse's to take (``"foreign"``). ``"bound"`` / ``"settled"``
        otherwise."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            outcome = "settled"
            if resource is not None:
                outcome = await self._record_unrouted_resource(db, transfer_id, resource, state, cleanup)
            await db.execute("UPDATE resolution_attempts SET reconcile_at=NULL WHERE id=?", (attempt_id,))
            await db.commit()
        return outcome

    async def _record_unrouted_resource(self, db, transfer_id: int, resource: ProviderResource,
                                        state: ResourceState | None, cleanup: str | None) -> str:
        """Inside the caller's transaction: record a resource a provider
        created that no route binds -- an owed creation found
        (``settle_creation``), or one a late answer named after its route
        was released (``resolution``). A resource this transfer already
        binds keeps its own lifecycle (``"known"``); one another live
        transfer holds is not DebridPulse's to take (``"foreign"``); otherwise
        it is bound to the transfer as an ordinary resource and, with
        ``cleanup``, given that cleanup intent (``"bound"``)."""
        known = await db.fetchone(
            "SELECT 1 FROM provider_resources WHERE transfer_id=? AND (resource_key=? OR "
            "(resource_key IS NULL AND id=?))", (transfer_id, resource.id, resource.id))
        if known:
            return "known"
        try:
            await self._resource(db, transfer_id, resource, state)
        except TransferError as exc:
            if exc.error.category != Category.OWNERSHIP_CONFLICT:
                await db.rollback()
                raise
            return "foreign"
        if cleanup:
            await self.cleanup_intent(transfer_id, resource.id, cleanup, db=db)
        return "bound"

    async def record_interpretation(self, request_id: str, interpretation: TransferRequest) -> None:
        """Durably establish the provider's alternate reading of one request
        (``DiscoveryRequest.alternate``). Set once: an established
        interpretation is never replaced, and the operator's ``payload`` is
        never touched."""
        async with get_db() as db:
            await db.execute("UPDATE transfer_requests SET interpretation=? WHERE id=? AND interpretation IS NULL",
                             (codec.dump(interpretation), request_id))
            await db.commit()

    @staticmethod
    def _request_label(row) -> str | None:
        """Which of its transfer's inputs a request is, safely: a root's
        position as submitted, and the public host its link names."""
        try:
            target = (codec.load(row["payload"], {}) or {}).get("payload")
        except (TypeError, ValueError, AttributeError):
            target = None
        host = None
        if isinstance(target, str) and target.lower().startswith(("http://", "https://", "ftp://", "sftp://")):
            from urllib.parse import urlsplit
            try:
                host = safe_public_host(urlsplit(target).hostname)
            except ValueError:
                host = None
        position = f"source {int(row['ordinal']) + 1}" if row["parent_id"] is None else None
        return ", ".join(part for part in (position, host) if part) or None

    async def request_failure(self, request_id: str, error: NormalizedError, retry_at: float | None, *, retry_state="pending",
                              consume_attempt=False, advance_alternative=False) -> None:
        """``advance_alternative``: the failure owner's decided terminal
        failure (``TransferEngine._request_failure``) of an explicit group's
        root hands the group to its next alternative in the SAME transaction
        (``_admit_next_alternative``). Never for an interrupted resolution
        reconciled at startup: whether its remote creation happened is
        unknown, so no other alternative may begin."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            error_blob = codec.dump(error)
            started = await db.fetchall("""SELECT a.id,a.provider_id,r.transfer_id FROM resolution_attempts a
                JOIN transfer_requests r ON r.id=a.request_id WHERE a.request_id=? AND a.state='started'""", (request_id,))
            for item in started:
                await db.execute("UPDATE resolution_attempts SET state='failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (error_blob, item["id"]))
                await db.execute("UPDATE route_attempt_provenance SET outcome='failed',updated_at=CURRENT_TIMESTAMP WHERE resolution_attempt_id=?", (item["id"],))
                await journal(db, je.route_ended("failed", transfer_id=item["transfer_id"], request_id=request_id,
                                                 attempt_id=item["id"], provider_id=item["provider_id"], error=error))
            failed = await db.execute("""UPDATE transfer_requests SET state=?,error=?,retry_at=?,attempts=attempts+? WHERE id=?
                AND transfer_id IN (SELECT id FROM torrents WHERE status NOT IN ('deleted','completed','consolidated','cancelled'))""",
                (retry_state if retry_at is not None else "failed", error_blob, retry_at or 0, int(consume_attempt), request_id))
            if failed.rowcount:
                owner = await db.fetchone("SELECT transfer_id,parent_id,ordinal,payload FROM transfer_requests WHERE id=?",
                                          (request_id,))
                await journal(db, je.request_failed(transfer_id=owner["transfer_id"], request_id=request_id, error=error,
                                                    retrying=retry_at is not None, source=self._request_label(owner)))
            if retry_at is None and advance_alternative:
                await self._admit_next_alternative(db, request_id)
            await db.commit()

    async def manifest(self, record: RequestRecord, entries: tuple[SourceEntry, ...], *, selection_id: str | None = None) -> None:
        """``selection_id``, when a file-selection generation authorized this
        fan-out (``TransferRepository.commit_selected_manifest
        .selection_id``), is durably stamped onto each child so materialization
        admission can trace it back without re-deriving the generation from
        the root's current (rebindable) resource -- DP 1.0.12 canonical
        architecture correction, Workstream A, specification section 7.2.

        A non-``None`` ``selection_id`` ALWAYS advances a child's stamp to
        the newly supplied generation, never merely preserves whatever it
        already held (a child materialized under a superseded generation A
        must be able to advance to the current generation B, or it would
        report STALE forever -- ``manifest()`` is called again on every
        successful root resolution pass at the SAME generation, so a
        COALESCE that only ever fills a NULL never lets a genuinely new
        generation replace an old one for a path both generations share).
        ``None`` never touches the existing stamp -- non-interactive
        resolution passes that carry no generation at all must not erase a
        previously stamped one.

        Advancing a child from one non-``None`` generation to a DIFFERENT
        one must never detach a still-live (non-terminal) execution's
        ``download_files`` pointer itself -- doing so here would orphan an
        authorized native writer without ever cancelling it (Gate 9
        revision-3 rejection finding 1). Real cancellation requires the
        executor, which this pure-repository method does not hold. Instead,
        when a live execution is found under the OLD generation, the stamp
        advance for that one child is SKIPPED entirely -- the child's stamp
        stays at its old (superseded) generation, which is exactly what
        makes ``materialization_authorization`` keep reporting STALE for it
        (it compares against the transfer's independently-tracked CURRENT
        generation via ``_current_generation``, never against the child's
        own stamp -- so leaving the stamp untouched here does not delay
        STALE detection at all). The existing canonical STALE-retirement
        machinery (``ConvergenceEngine._retire_stale_execution``, which owns
        the executor) then genuinely cancels and confirms the native writer
        on the next dispatch/recovery pass. Once that retirement has run,
        the child request is requeued and reprocessed through ordinary
        resolution, calling ``manifest()`` again for the SAME generation --
        at that point no live execution remains, so the stamp advances
        safely and this method itself now performs the (already-safe)
        detach-and-requeue below. A child whose old execution is already
        terminal (or absent) is retired and advanced immediately, since
        there is nothing left to orphan.

        A generation fans out only once it is committed with continuity
        ``proven``; anything else changes nothing here. The transfer's
        collection folder is frozen in this transaction the first time its
        members are fanned out, from the name it has now: from then on a member
        target never follows a later provider-reported name.
        """
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            parent = await db.fetchone("SELECT status,name FROM torrents WHERE id=?", (record.transfer_id,))
            if not parent or parent["status"] in {"deleted", "completed", "consolidated", "cancelled"}:
                return
            if selection_id is not None:
                generation = await db.fetchone(
                    "SELECT manifest_committed_at,continuity FROM transfer_file_selections WHERE id=?", (selection_id,))
                if not generation or generation["manifest_committed_at"] is None or generation["continuity"] != "proven":
                    await db.rollback()
                    raise TransferError(NormalizedError(
                        Domain.LIFECYCLE, Category.RESOURCE_STATE_CONFLICT, Stage.RECONCILIATION))
            await db.execute(
                """UPDATE torrents SET collection_root=? WHERE id=? AND collection_root IS NULL
                   AND COALESCE(collection_root_conflict,0)=0""",
                (safe_name(parent["name"] or ""), record.transfer_id))
            missing_error = NormalizedError(Domain.RESOLUTION, Category.SOURCE_NOT_FOUND, Stage.RESOLUTION)
            # One child per request of every member: a member's alternates are
            # sibling children of the same member, each its own entry.
            children = [(manifest_child_identity(record.id, entry.relative_path, alternate),
                         replace(entry, request=request, alternates=()))
                        for entry in entries for alternate, request in manifest_member_requests(entry)]
            identities = [identity for identity, _entry in children]
            deferred = await self._retire_superseded_children(db, record, identities, missing_error)
            for ordinal, (identity, entry) in enumerate(children):
                await db.execute("""INSERT OR IGNORE INTO transfer_requests(id,transfer_id,parent_id,ordinal,payload,metadata,materialized_selection_id)
                    VALUES(?,?,?,?,?,?,?)""",
                    (identity, record.transfer_id, record.id, ordinal, codec.dump(entry.request), codec.dump(entry), selection_id))
                advance_selection_id = selection_id
                if selection_id is not None:
                    existing = await db.fetchone(
                        "SELECT materialized_selection_id FROM transfer_requests WHERE id=?", (identity,),
                    )
                    previous_generation = existing["materialized_selection_id"] if existing else None
                    if previous_generation and str(previous_generation) != str(selection_id):
                        live_execution = await db.fetchone(
                            """SELECT f.id FROM download_files f JOIN execution_attempts e ON e.id=f.execution_attempt_id
                                WHERE f.request_id=? AND e.state IN ('prepared','queued','running','paused','unknown')""",
                            (identity,),
                        )
                        if live_execution:
                            advance_selection_id = None
                            deferred = True
                        else:
                            artifact_row = await db.fetchone(
                                "SELECT id, execution_attempt_id FROM download_files WHERE request_id=?", (identity,),
                            )
                            if artifact_row:
                                await db.execute("""UPDATE download_files SET status='unresolved',
                                    execution_attempt_id=NULL,normalized_error=NULL,retry_at=0,
                                    continuation_reservation_expires_at=NULL,updated_at=CURRENT_TIMESTAMP
                                    WHERE id=? AND status!='completed'""", (artifact_row["id"],))
                                if artifact_row["execution_attempt_id"]:
                                    await db.execute("""UPDATE execution_attempts SET authorized=0,updated_at=CURRENT_TIMESTAMP
                                        WHERE id=? AND state IN ('failed','absent','cancelled','succeeded')""",
                                        (artifact_row["execution_attempt_id"],))
                            await db.execute("""UPDATE transfer_requests SET state='pending',retry_at=0,error=NULL
                                WHERE id=?""", (identity,))
                await db.execute("""UPDATE transfer_requests SET payload=?,metadata=?,state=CASE WHEN state='waiting_parent' THEN 'pending' ELSE state END,
                    materialized_selection_id=COALESCE(?,materialized_selection_id)
                    WHERE id=?""", (codec.dump(entry.request), codec.dump(entry), advance_selection_id, identity))
            # A member left to a live writer of a superseded generation is not
            # yet part of this fan-out: the root keeps waiting, so its next
            # ordinary observation repeats this (idempotent) fan-out once the
            # writer is retired, rather than leaving that member behind.
            await db.execute("UPDATE transfer_requests SET state=?,retry_at=0,error=NULL WHERE id=?",
                             ("waiting" if deferred else "resolved", record.id))
            await db.commit()

    @staticmethod
    async def _retire_superseded_children(db, record: RequestRecord, identities: list[str],
                                          missing_error: NormalizedError) -> bool:
        """Make the supplied entries authoritative for this parent's child slots.

        A member's canonical identity is ``uuid5(parent, relative_path)``, so a
        provider that changes a member's canonical coordinate publishes a
        DIFFERENT logical child -- and the child the previous acquisition
        generation created is no longer part of current manifest truth. Two
        things follow, and this method owns both:

        * that child is retired. A child the manifest never delivered at all
          is still a genuine ``SOURCE_NOT_FOUND`` failure, exactly as before.
          A child a PREVIOUS generation established is not a failure -- it was
          superseded -- so it takes the state the architecture already has for
          "retained, readable as history, but not part of current work":
          ``skipped``/``blocked``. That keeps it out of lifecycle aggregation,
          presentation voting and recovery eligibility
          (``canonical_artifact_membership_sql``) without inventing a status,
          and without a superseded coordinate failing a transfer that is
          converging perfectly well on its current one. Its row, its artifact,
          its execution attempts and all of its provenance remain untouched as
          history; only its state and its slot change.
        * it RELEASES its ordinal. ``transfer_requests`` is keyed
          ``UNIQUE(transfer_id,parent_id,ordinal)``, and the fan-out below
          inserts the current entries at ordinals ``0..n-1``. A superseded
          child sitting in one of those slots therefore made the current
          member's ``INSERT OR IGNORE`` a silent no-op: the current generation
          could never establish its own children, and the transfer churned
          against coordinates no current manifest contains. Ordinal is a slot
          index used only for ordering, never provenance, so retired children
          are simply moved above the current range -- history sorts after
          current work and can never collide with it again.

        A child holding a LIVE execution is left entirely alone -- not retired,
        and its slot not taken. Cancelling a native writer needs the executor,
        which this pure-repository method does not hold; the existing canonical
        STALE-retirement machinery (``ConvergenceEngine._retire_stale_execution``)
        does that on the next dispatch/recovery pass, and the following
        manifest pass then completes the fan-out. This is the same handshake
        the generation-advance branch above already relies on.

        Returns whether a live child was left in place for the next pass.

        Nothing here inspects a path. Membership of the current generation is
        decided by identity alone, so this converges any superseded coordinate
        model rather than one historical path shape.
        """
        existing = await db.fetchall(
            "SELECT id,ordinal FROM transfer_requests WHERE parent_id=? ORDER BY ordinal,id", (record.id,))
        current = set(identities)
        superseded = [child for child in existing if child["id"] not in current]
        if not superseded:
            return False
        deferred = False
        highest = max([int(child["ordinal"] or 0) for child in existing] + [len(identities) - 1])
        for child in superseded:
            live = await db.fetchone(
                """SELECT f.id FROM download_files f JOIN execution_attempts e ON e.id=f.execution_attempt_id
                    WHERE f.request_id=? AND e.state IN ('prepared','queued','running','paused','unknown')""",
                (child["id"],))
            if live:
                deferred = True
                continue
            established = await db.fetchone(
                "SELECT id FROM download_files WHERE request_id=?", (child["id"],))
            if not established:
                # Never established: the manifest simply does not contain a
                # member this transfer was waiting for. (State is not the
                # discriminator -- ``renew_parent`` puts an ESTABLISHED child
                # back into ``waiting_parent`` on the reacquisition path, so
                # only the presence of a durable artifact distinguishes a
                # member a previous generation actually built.)
                await db.execute("UPDATE transfer_requests SET state='failed',error=? WHERE id=?",
                                 (codec.dump(missing_error), child["id"]))
                await db.execute(
                    "UPDATE download_files SET status='error',normalized_error=? WHERE request_id=? AND status!='completed'",
                    (codec.dump(missing_error), child["id"]))
            else:
                # Established by a previous generation and superseded by this
                # one. Generic and generation-oriented: it names what happened
                # to the member, never why a particular coordinate moved.
                await db.execute("UPDATE transfer_requests SET state='skipped',error=NULL WHERE id=?", (child["id"],))
                await db.execute(
                    """UPDATE download_files SET blocked=1,block_reason='superseded_generation',
                        status=CASE WHEN status='completed' THEN status ELSE 'blocked' END,
                        normalized_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE request_id=?""",
                    (child["id"],))
            if int(child["ordinal"] or 0) < len(identities):
                highest += 1
                await db.execute("UPDATE transfer_requests SET ordinal=? WHERE id=?", (highest, child["id"]))
        return deferred

    async def resource_observation(self, transfer_id: int, resource: ProviderResource, state: ResourceState):
        async with get_db() as db:
            await self._resource(db, transfer_id, resource, state)
            await db.commit()

    async def inventory_observation(self, transfer_id: int, resource: ProviderResource, state: ResourceState) -> bool:
        """``resource_observation`` for a binding provider inventory reports,
        answering -- once its write has committed -- whether this write made a
        live transfer's bound, unpromoted backup AVAILABLE (it was not
        before). That is readiness recorded outside a coordinator pass: the
        caller wakes the coordinator, whose readiness promotion re-reads and
        re-decides everything. An unchanged AVAILABLE, any other resource or
        state, or a retired transfer answers ``False``."""
        terminal = sorted(str(item) for item in TERMINAL_TRANSFER_STATES)
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            before = await db.fetchone(
                "SELECT state FROM provider_resources WHERE transfer_id=? "
                "AND (resource_key=? OR (resource_key IS NULL AND id=?))", (transfer_id, resource.id, resource.id))
            binding_id = await self._resource(db, transfer_id, resource, state)
            became_ready = (state == ResourceState.AVAILABLE
                            and (before is None or before["state"] != ResourceState.AVAILABLE.value)
                            and await db.fetchone(
                                f"""SELECT 1 FROM standby_resources s JOIN torrents t ON t.id=s.transfer_id
                                    WHERE s.binding_id=? AND s.transfer_id=? AND s.state='bound'
                                    AND s.promoted_at IS NULL
                                    AND t.status NOT IN ({",".join("?" * len(terminal))})""",
                                (binding_id, transfer_id, *terminal)) is not None)
            await db.commit()
        return became_ready

    async def preparation_idle(self, transfer_id: int, resource: ProviderResource, completed_bytes: int,
                               now: float) -> float:
        """Seconds a PREPARING provider resource has gone without useful
        progress, recording this observation. Its first PREPARING observation
        starts the clock, and only reported completed bytes above the highest
        seen so far restart it; a provider that reports no progress at all is
        counted from that first observation. The record belongs to the
        resource's own binding row (a new or replaced resource starts fresh)
        and is cleared when it leaves PREPARING."""
        completed = max(0, int(completed_bytes or 0))
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                """SELECT id,preparing_bytes,preparing_since FROM provider_resources WHERE transfer_id=?
                   AND (resource_key=? OR (resource_key IS NULL AND id=?)) AND state='preparing'""",
                (transfer_id, resource.id, resource.id))
            if row is None:
                await db.rollback()
                return 0.0
            if row["preparing_since"] is None or completed > int(row["preparing_bytes"] or 0):
                await db.execute("UPDATE provider_resources SET preparing_bytes=?,preparing_since=? WHERE id=?",
                                 (max(completed, int(row["preparing_bytes"] or 0)), now, row["id"]))
                await db.commit()
                return 0.0
            await db.rollback()
        return max(0.0, now - float(row["preparing_since"]))

    async def materialize(self, record: RequestRecord, candidates: tuple[TransferCandidate, ...], target: str) -> Artifact | None:
        """Allocate (or rebuild) ``record``'s own canonical artifact.

        ``record`` is the caller's SNAPSHOT, never authority: the opportunity
        is revalidated from durable truth inside this one transaction. The
        transfer is live, the request still belongs to it and is still exactly
        ``materializing``, and any row the request already has is still its
        canonical actionable artifact -- not a standby contribution, not
        blocked, not grouped under another canonical owner. When another
        legitimate owner settled the request first (a canonical attach, a
        consolidation, a failure), nothing is mutated and ``None`` says so:
        losing that opportunity is not a failure."""
        if not candidates:
            return None
        chosen = candidates[0]
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            current = await db.fetchone("""SELECT r.state,t.status FROM transfer_requests r
                JOIN torrents t ON t.id=r.transfer_id WHERE r.id=? AND r.transfer_id=?""",
                                        (record.id, record.transfer_id))
            if (not current or current["status"] in {"deleted", "completed", "consolidated", "cancelled"}
                    or current["state"] != "materializing"):
                await db.rollback()
                return None
            previous = await db.fetchone("SELECT * FROM download_files WHERE request_id=?", (record.id,))
            if previous and not (is_canonical_artifact_row(previous)
                                 and int(previous.get("mirror_group_id") or previous["id"]) == int(previous["id"])):
                await db.rollback()
                return None
            if previous:
                # ``target`` is the coordinate the caller derived from CURRENT
                # canonical truth. It is applied under exactly the condition
                # this row is already re-queued under -- released to
                # ``unresolved`` holding no execution pointer -- so rebuilding
                # a member's executable state and moving it to its current
                # coordinate are one act, evaluated once, in one transaction.
                # A completed artifact, or one an authorized writer still owns,
                # keeps both its status and its target: there is no path by
                # which a live writer can be retargeted underneath itself.
                await db.execute("""UPDATE download_files SET candidates=?,selected_candidate=0,
                    size_bytes=?,normalized_error=NULL,
                    local_path=CASE WHEN status='unresolved' AND execution_attempt_id IS NULL THEN ? ELSE local_path END,
                    status=CASE WHEN status='unresolved' AND execution_attempt_id IS NULL THEN 'queued' ELSE status END
                    WHERE id=?""", (codec.dump(candidates), chosen.expected_bytes, target, previous["id"]))
            else:
                await db.execute("""INSERT INTO download_files(torrent_id,request_id,filename,size_bytes,local_path,status,candidates,download_client)
                    VALUES(?,?,?,?,?,'queued',?,'')""",
                    (record.transfer_id, record.id, chosen.name, chosen.expected_bytes, target, codec.dump(candidates)))
            await db.execute("UPDATE transfer_requests SET state='resolved',error=NULL WHERE id=?", (record.id,))
            await db.commit()
        # Read back through the one canonical projection; a row settled by
        # another owner after this commit is honestly ``None``.
        return next((item for item in await self.artifacts(record.transfer_id) if item.request_id == record.id), None)

    async def artifacts(self, transfer_id: int, *, artifact_id: int | None = None) -> tuple[Artifact, ...]:
        """The transfer's canonical artifacts -- or, with ``artifact_id``, just
        that one (when it is one of them), read the same way."""
        only = "" if artifact_id is None else " AND f.id=?"
        async with get_db() as db:
            rows = await db.fetchall(f"""SELECT f.*,e.handle FROM download_files f
                LEFT JOIN execution_attempts e ON e.id=f.execution_attempt_id
                WHERE f.torrent_id=? AND {canonical_artifact_membership_sql('f')}{only} ORDER BY f.id""",
                                     (transfer_id,) if artifact_id is None else (transfer_id, artifact_id))
        return tuple(Artifact(row["id"], transfer_id, row["request_id"], row["filename"], row["local_path"], row["size_bytes"] or 0,
                              row["status"], tuple(codec.candidate(item) for item in codec.load(row["candidates"], [])),
                              row["selected_candidate"], codec.handle(codec.load(row["handle"])), row["retry_count"] or 0,
                              row["retry_at"], codec.error(row["normalized_error"]),
                              SizeKnowledge.durable(row["size_bytes"], row["size_knowledge"])) for row in rows)

    async def occupied_paths(self) -> set[str]:
        async with get_db() as db:
            rows = await db.fetchall("""SELECT f.local_path FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                WHERE f.local_path IS NOT NULL AND COALESCE(f.mirror_state,'')!='standby'
                AND (t.status NOT IN ('deleted','completed','consolidated','error')
                    OR EXISTS (SELECT 1 FROM execution_attempts e WHERE e.id=f.execution_attempt_id AND e.authorized=1
                        AND e.state IN ('prepared','queued','running','paused','unknown')))""")
        return {str(row["local_path"]).casefold() for row in rows}

    @classmethod
    async def _decomposition_admission(cls, db, artifact) -> MaterializationAdmission:
        """THE decomposition-generation admission of one artifact, inside the
        caller's session (``materialization_authorization`` and
        ``prepare_execution`` both decide through it).

        Only a manifest member is governed: its stamped generation (the one
        its fan-out committed) is runnable only when it is, conjunctively,

        * its root's CURRENT generation -- exactly the generation of the
          root's CURRENT provider-resource binding, never inferred from
          creation order -- and
        * committed with continuity ``proven``.

        A root rebound elsewhere (whether or not the new binding's generation
        has opened yet) or holding no binding at all makes the stamp STALE: a
        stamp is no authority by itself after another rebind. A current
        generation not yet committed, or held, is HOLD. A legacy member
        carrying no stamp is judged by its root's current generation, if any;
        with none it is ungoverned.

        Any other artifact is governed by its selected candidate's root route
        alone (``_route_replacement_admission``)."""
        row = await db.fetchone(
            "SELECT parent_id, materialized_selection_id FROM transfer_requests WHERE id=?", (artifact.request_id,))
        if not row or not row["parent_id"]:
            return await cls._route_replacement_admission(db, artifact)
        root_id = row["parent_id"]
        root = await db.fetchone("SELECT resource FROM transfer_requests WHERE id=?", (root_id,))
        root_resource = codec.resource(codec.load(root["resource"])) if root and root["resource"] else None
        binding = (await cls._resolve_binding(db, artifact.transfer_id, root_resource.id)
                   if root_resource is not None else None)
        current = (await db.fetchone(
            "SELECT * FROM transfer_file_selections WHERE request_id=? AND provider_resource_id=?",
            (root_id, binding)) if binding is not None else None)
        stamp = row["materialized_selection_id"]
        if not stamp:
            if current is None:
                return MaterializationAdmission(MaterializationAdmissionKind.PROCEED)
            stamp = current["id"]
        if current is None or str(current["id"]) != str(stamp):
            return MaterializationAdmission(MaterializationAdmissionKind.STALE,
                                            authority_generation=current["id"] if current else None)
        if current["manifest_committed_at"] is None or current["continuity"] != "proven":
            return MaterializationAdmission(MaterializationAdmissionKind.HOLD, authority_generation=current["id"])
        return MaterializationAdmission(MaterializationAdmissionKind.PROCEED, authority_generation=current["id"])

    @staticmethod
    async def _roots_shared_elsewhere(db, root_ids) -> set[str]:
        """Inside the caller's session: the roots among ``root_ids`` that
        issued a CURRENT canonical binding (below the non-current band,
        ``CanonicalOwnership._NON_CURRENT_ORDER``) of an unfinished artifact
        of ANOTHER transfer -- a cross-transfer consolidation onto their
        source. Switching such a root would invalidate a candidate another
        transfer's artifact holds without fencing that artifact's writer, so
        it is never a switch subject: not offered, and refused before
        anything changes."""
        ids = [str(item) for item in root_ids]
        if not ids:
            return set()
        rows = await db.fetchall(
            f"""SELECT DISTINCT o.request_id FROM canonical_candidate_origins o
               JOIN canonical_candidate_bindings b ON b.id=o.binding_id AND b.candidate_order<=100000
               JOIN download_files f ON f.id=b.canonical_artifact_id
               JOIN transfer_requests r ON r.id=o.request_id
               WHERE o.request_id IN ({",".join("?" * len(ids))}) AND f.torrent_id!=r.transfer_id
               AND f.status NOT IN ('completed','cancelled','deleted')""", tuple(ids))
        return {str(row["request_id"]) for row in rows}

    async def route_replacement_pending(self, request_id: str) -> bool:
        """Whether root ``request_id`` still owes the landing of an operator
        switch: some unfinished artifact's selected binding comes from this
        root alone, from an attempt older than an ``operator_switch`` route
        attempt of it (exactly what ``_route_replacement_admission`` holds).
        False once the replacement landed (the selected bindings' origins
        then follow the switch) and for every root never switched, so its
        re-resolutions keep their ordinary path.

        A CANCELLED artifact still owes it. Cancelling a held file ends the
        file, not the switch: its root's later resolution must still take the
        landing path, where the canonical owner leaves every cancelled
        artifact exactly as it is (``replace_root_candidates``) and marks the
        root resolved. Were it excluded, that resolution would take the
        ordinary path, which rebuilds the root's own artifact in place --
        replacing a cancelled file's candidates and pushing a binding another
        source still supplies out of the current band."""
        async with get_db() as db:
            rows = await db.fetchall(
                f"""SELECT f.id, COUNT(*) AS origins, COALESCE(SUM(CASE WHEN o.request_id=? AND r.parent_id IS NULL
                       AND {_OWN_TRANSFER} AND {_SWITCHED_AFTER_ORIGIN} THEN 1 ELSE 0 END),0) AS replaced
                   FROM download_files f
                   JOIN canonical_candidate_bindings b ON b.canonical_artifact_id=f.id
                       AND b.candidate_order=COALESCE(f.selected_candidate,0)+1
                   JOIN canonical_candidate_origins o ON o.binding_id=b.id
                   JOIN transfer_requests r ON r.id=o.request_id
                   LEFT JOIN route_attempt_provenance op ON op.resolution_attempt_id=o.resolution_attempt_id
                   WHERE f.status NOT IN ('completed','deleted')
                     AND f.id IN (SELECT b2.canonical_artifact_id FROM canonical_candidate_bindings b2
                                  JOIN canonical_candidate_origins o2 ON o2.binding_id=b2.id WHERE o2.request_id=?)
                   GROUP BY f.id""", (request_id, request_id))
        return any(int(row["origins"] or 0) and int(row["replaced"] or 0) == int(row["origins"]) for row in rows)

    async def route_replacement_failure(self, artifact_id: int):
        """``(error, bound)`` for an artifact whose switched source ended in
        ``error``, else ``None`` (``bound``: its candidate is canonically
        bound, so the root's later resolution lands in its place):
        every root its selected candidate comes from (its selected binding's
        origins, or -- with no binding -- its own root request) was switched
        by an operator after issuing it and has since FAILED on its new route.
        Nothing will then land in its place, so the artifact is no longer
        held, unseen, for a landing that cannot come."""
        async with get_db() as db:
            rows = await db.fetchall(
                f"""SELECT r.state, r.error, CASE WHEN r.parent_id IS NULL AND {_OWN_TRANSFER} AND {_SWITCHED_AFTER_ORIGIN}
                       THEN 1 ELSE 0 END AS switched
                   FROM download_files f
                   JOIN canonical_candidate_bindings b ON b.canonical_artifact_id=f.id
                       AND b.candidate_order=COALESCE(f.selected_candidate,0)+1
                   JOIN canonical_candidate_origins o ON o.binding_id=b.id
                   JOIN transfer_requests r ON r.id=o.request_id
                   LEFT JOIN route_attempt_provenance op ON op.resolution_attempt_id=o.resolution_attempt_id
                   WHERE f.id=?""", (int(artifact_id),))
            bound = bool(rows)
            if not rows:
                rows = await db.fetchall(
                    """SELECT r.state, r.error, EXISTS(SELECT 1 FROM route_attempt_provenance sp
                           WHERE sp.request_id=r.id AND sp.operation='operator_switch') AS switched
                       FROM download_files f JOIN transfer_requests r ON r.id=f.request_id
                       WHERE f.id=? AND r.parent_id IS NULL""", (int(artifact_id),))
        if rows and all(int(row["switched"] or 0) and row["state"] == "failed" and row["error"] for row in rows):
            return codec.error(rows[0]["error"]), bound
        return None

    @staticmethod
    async def _route_replacement_admission(db, artifact) -> MaterializationAdmission:
        """Inside the caller's session: HOLD an artifact whose selected
        candidate is bound canonically (``canonical_candidate_bindings`` at its
        selected position) and EVERY origin of that binding names a root
        request an operator switched after it issued the candidate -- an
        ``operator_switch`` route attempt of that root later than the origin's
        own attempt (``_SWITCHED_AFTER_ORIGIN``: the switch's own durable fact,
        never a release, which an operator Retry also records).
        The candidate belongs to a route an operator switch replaced; until the
        root resolves on its new route and its candidates take the replaced
        ones' place (``CanonicalOwnership.replace_root_candidates``), the
        artifact acquires no writer from it. Derived from stored facts alone,
        so it holds across any restart between the switch's commit and that
        landing. An artifact whose selected candidate another source still
        supplies, a member (its decomposition generation governs it) and an
        artifact without bindings (its own request's released candidate is
        never prepared: ``_candidate_current``) are unaffected: PROCEED."""
        artifact_id = getattr(artifact, "id", None)
        if artifact_id is None:
            return MaterializationAdmission(MaterializationAdmissionKind.PROCEED)
        row = await db.fetchone(
            f"""SELECT COUNT(*) AS origins, COALESCE(SUM(CASE WHEN r.parent_id IS NULL AND {_OWN_TRANSFER} AND {_SWITCHED_AFTER_ORIGIN}
               THEN 1 ELSE 0 END),0) AS replaced
               FROM download_files f
               JOIN canonical_candidate_bindings b ON b.canonical_artifact_id=f.id
                   AND b.candidate_order=COALESCE(f.selected_candidate,0)+1
               JOIN canonical_candidate_origins o ON o.binding_id=b.id
               JOIN transfer_requests r ON r.id=o.request_id
               LEFT JOIN route_attempt_provenance op ON op.resolution_attempt_id=o.resolution_attempt_id
               WHERE f.id=?""", (int(artifact_id),))
        if row and int(row["origins"] or 0) and int(row["replaced"] or 0) == int(row["origins"]):
            return MaterializationAdmission(MaterializationAdmissionKind.HOLD)
        return MaterializationAdmission(MaterializationAdmissionKind.PROCEED)

    async def prepare_execution(self, artifact: Artifact, handle: ExecutionHandle, *, from_input_required: bool = False,
                                target_initially_absent: bool | None = None,
                                continuation: ContinuationPlan | None = None) -> bool:
        """``target_initially_absent`` is the caller's direct observation, made
        at this same final admission boundary, of whether the validated target
        held any pre-existing material. It is stored with the attempt and never
        recomputed; ``None`` (no observation) persists as NULL.

        This is also THE writer admission of the artifact's material: in the
        same transaction the continuation plan is re-validated against the
        current material generation (a stale plan admits nothing), everything
        the plan does not retain is reclassified out of VALID, and the attempt
        becomes the next writer generation. Only one writer can hold that
        generation, because only a detached artifact is admitted here.

        And THE decomposition-generation admission, decided in the same
        transaction: a member is admitted only while its generation is its
        root's current, proven, committed generation on the root's current
        binding (``_decomposition_admission``) -- whatever path asked, no
        stale or held generation acquires a writer."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT f.* FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id
                WHERE f.id=? AND f.status=? AND f.execution_attempt_id IS NULL
                AND t.status NOT IN ('deleted','completed','consolidated','cancelled') AND COALESCE(p.paused,0)=0""",
                (artifact.id, "input_required" if from_input_required else "queued"))
            if not row:
                return False
            if (await self._decomposition_admission(db, artifact)).kind != MaterializationAdmissionKind.PROCEED:
                await db.rollback()
                return False
            # And the candidate admitted is the artifact's current one, decided in
            # this same transaction: never a candidate a newer fan-out or
            # resolution replaced after the caller read it.
            if artifact.candidates and not await self._candidate_current(
                    db, artifact, artifact.candidates[artifact.selected]):
                await db.rollback()
                return False
            writer_generation = None
            if continuation is not None:
                writer_generation = await self._admit_material_writer(db, artifact, continuation)
                if writer_generation is None:
                    await db.rollback()
                    return False
            await self._record_writer(db, artifact, handle, writer_generation=writer_generation,
                                      target_initially_absent=target_initially_absent, continuation=continuation,
                                      from_input_required=from_input_required)
            await db.commit()
        return True

    async def candidate_current(self, artifact: Artifact, candidate: TransferCandidate,
                                paired: TransferRequest | None = None) -> bool:
        """``_candidate_current`` in its own session (the dispatch read)."""
        async with get_db() as db:
            return await self._candidate_current(db, artifact, candidate, paired)

    @staticmethod
    async def _candidate_current(db, artifact: Artifact, candidate: TransferCandidate,
                                 paired: TransferRequest | None = None) -> bool:
        """THE coherence of a candidate with its artifact, from durable truth in
        the caller's session: the artifact still selects exactly this
        candidate (identity and provider), and the route attempt of its own
        request that issued it -- the newest one recording it -- was not
        released by a route replacement, so a candidate the replaced route left
        behind is never prepared, or asked to issue material, for the
        successor's member. ``paired``: the request the caller is about to
        hand the candidate's provider, which must be the artifact's own
        current request. A candidate no route attempt of the artifact's own
        request recorded (a sibling's alternative, a canonically bound
        candidate, a legacy row) is judged by its selection alone, as before."""
        row = await db.fetchone("""SELECT f.candidates,f.selected_candidate,f.request_id,r.payload,r.interpretation
            FROM download_files f LEFT JOIN transfer_requests r ON r.id=f.request_id WHERE f.id=?""", (artifact.id,))
        if not row:
            return False
        durable = [codec.candidate(item) for item in codec.load(row["candidates"], [])]
        selected = int(row["selected_candidate"] or 0)
        if not 0 <= selected < len(durable):
            return False
        current = durable[selected]
        if (str(current.id), current.provider_id) != (str(candidate.id), candidate.provider_id):
            return False
        if paired is not None:
            if row["payload"] is None:
                return False
            request = (codec.optional_request(codec.load(row["interpretation"]))
                       or codec.request(codec.load(row["payload"])))
            if (paired.kind, paired.payload) != (request.kind, request.payload):
                return False
        for issued in await db.fetchall(
                """SELECT a.state,p.candidate_summary FROM route_attempt_provenance p
                   JOIN resolution_attempts a ON a.id=p.resolution_attempt_id
                   WHERE a.request_id=? AND a.provider_id=? ORDER BY p.ordinal DESC""",
                (row["request_id"], str(candidate.provider_id))):
            if any(str(item.get("candidate_id") or "") == str(candidate.id)
                   for item in codec.load(issued["candidate_summary"], [])):
                return issued["state"] != "released"
        return True

    async def _record_writer(self, db, artifact: Artifact, handle: ExecutionHandle, *, writer_generation,
                             target_initially_absent: bool | None, continuation: ContinuationPlan | None,
                             from_input_required: bool = False, link_activation: bool = True) -> None:
        """Inside an admission transaction: record ``handle`` as the artifact's
        new, current, 'prepared' writer of ``writer_generation`` -- its durable
        attempt, route provenance and material ownership."""
        candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
        route_attempt_id = await self._candidate_route(
            db, artifact.transfer_id, candidate, artifact_id=artifact.id
        )
        ordinal_row = await db.fetchone("SELECT COALESCE(MAX(ordinal),0) AS n FROM execution_attempt_provenance WHERE artifact_id=?", (artifact.id,))
        ordinal = int(ordinal_row["n"] or 0) + 1
        # Material ownership is explicit and durable: established when the
        # boundary was absent at this admission, or carried forward from
        # this artifact's immediately preceding attempt when THAT attempt
        # owned it (the present material is DebridPulse's own lineage).
        # Anything else -- pre-existing material, or no observation -- is
        # unowned, and no cleanup may delete it.
        owner = None
        if target_initially_absent:
            owner = handle.attempt_id
        elif target_initially_absent is not None:
            predecessor = await db.fetchone(
                "SELECT material_owner_attempt_id FROM execution_attempts WHERE artifact_id=? ORDER BY rowid DESC LIMIT 1",
                (artifact.id,))
            owner = (predecessor or {}).get("material_owner_attempt_id")
        await db.execute("""INSERT INTO execution_attempts(id,transfer_id,artifact_id,executor_id,handle,state,candidate,
            target_initially_absent,material_owner_attempt_id,writer_generation,continuation)
            VALUES(?,?,?,?,?,'prepared',?,?,?,?,?)""",
            (handle.attempt_id, artifact.transfer_id, artifact.id, handle.executor_id, codec.dump(handle),
             codec.dump(candidate) if candidate else None,
             None if target_initially_absent is None else int(bool(target_initially_absent)), owner,
             writer_generation, codec.dump(continuation.as_dict()) if continuation is not None else None))
        await db.execute("""INSERT INTO execution_attempt_provenance(
            execution_attempt_id,route_attempt_id,transfer_id,artifact_id,ordinal,provider_id,candidate_id,candidate_source,
            outcome,delivered,history_quality) VALUES(?,?,?,?,?,?,?,?, 'prepared',0,'recorded')""",
            (handle.attempt_id, route_attempt_id, artifact.transfer_id, artifact.id, ordinal,
             candidate.provider_id if candidate and candidate.provider_id else None, str(candidate.id) if candidate else None,
             codec.dump(self._safe_candidate_source(candidate)) if candidate else None))
        await db.execute("""UPDATE download_files SET execution_attempt_id=?,download_client=?,retry_count=retry_count+1,
            status=CASE WHEN ? THEN 'queued' ELSE status END,normalized_error=NULL,
            continuation_reservation_expires_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (handle.attempt_id, handle.executor_id, int(from_input_required), artifact.id))
        await journal(db, je.execution_started(
            transfer_id=artifact.transfer_id, artifact_id=artifact.id, attempt_id=handle.attempt_id,
            executor_id=handle.executor_id, provider_id=candidate.provider_id if candidate else None))
        # DP 1.0.12 recovery leveling, Section 29: durably link this new
        # execution back to the candidate-activation record that selected
        # it, if any -- a committed activation cannot know the replacement
        # execution's id at commit time (it doesn't exist yet), so the
        # audit link is completed here instead, the first time this
        # artifact actually dispatches afterward. Scans backward for the
        # most recent still-unlinked "activated" record for this artifact;
        # bounded, since only a just-activated, not-yet-dispatched
        # artifact ever has one pending.
        activation_rows = await db.fetchall(
            "SELECT id,detail FROM application_events WHERE transfer_id=? AND kind='candidate_activation' ORDER BY id DESC LIMIT 50",
            (artifact.transfer_id,),
        ) if link_activation else ()
        for activation_row in activation_rows:
            detail = codec.load(activation_row["detail"], {})
            if (detail.get("artifact_id") == artifact.id and detail.get("outcome") == "activated"
                    and detail.get("new_execution_id") is None):
                detail["new_execution_id"] = handle.attempt_id
                await db.execute(
                    "UPDATE application_events SET detail=? WHERE id=?",
                    (codec.dump(detail), activation_row["id"]),
                )
                break

    # ------------------------------------------------------------------
    # Artifact material: the ONE durable owner of reusable bytes
    # (``transfers.material``; DP 1.0.13 protocol-agnostic continuation)
    # ------------------------------------------------------------------

    _MATERIAL_SELECT = """SELECT m.*,f.size_bytes,f.size_knowledge,f.torrent_id AS transfer_id
        FROM artifact_material_state m JOIN download_files f ON f.id=m.artifact_id WHERE m.artifact_id=?"""

    @staticmethod
    def _material(row) -> mat.MaterialState:
        knowledge = SizeKnowledge.durable(row.get("size_bytes"), row.get("size_knowledge"))
        expected = (int(row["size_bytes"]) if knowledge == SizeKnowledge.KNOWN_POSITIVE
                    else 0 if knowledge == SizeKnowledge.KNOWN_ZERO else None)
        members, identities = mat.decode_members(row.get("member_ranges"))
        return mat.MaterialState(
            int(row["artifact_id"]), int(row["material_generation"]), int(row["geometry_version"]),
            mat.decode(row["valid_ranges"]), str(row["destination"]), int(row.get("writer_generation") or 0),
            expected, str(row.get("destination_identity") or ""), row.get("checkpoint_at"), members, identities,
        )

    @staticmethod
    async def _material_audit(db, transfer_id, artifact_id: int, event: str, **fields) -> None:
        """Sparse durable provenance of material decisions (rollback,
        invalidation, forced checkpoint, completion) -- never periodic commits."""
        detail = {"artifact_id": int(artifact_id), "event": str(event), **fields}
        await db.execute("INSERT INTO application_events(transfer_id,kind,detail,claimed) VALUES(?,?,?,1)",
                         (int(transfer_id), "material_audit", codec.dump(detail)))

    async def record_material_event(self, transfer_id: int, artifact_id: int, event: str, **fields) -> None:
        async with get_db() as db:
            await self._material_audit(db, transfer_id, artifact_id, event, **fields)
            await journal(db, je.continuation(event, transfer_id=transfer_id, artifact_id=artifact_id,
                                              detail=str(fields.get("boundary") or "").replace("_", " ") or None))
            await db.commit()

    async def verified_material(self, transfer_id: int) -> tuple[int, int] | None:
        """The transfer's verified-material measurement now
        (``verified_material_measure``), read from one snapshot of the same
        facts ``aggregate_lifecycle`` writes its progress from: its canonical
        artifacts and their DP-valid material."""
        async with get_db() as db:
            await db.execute("BEGIN")
            try:
                rows = await db.fetchall(
                    f"""SELECT f.id,f.status,f.size_bytes,f.size_knowledge FROM download_files f
                        WHERE f.torrent_id=? AND {canonical_artifact_membership_sql('f')} ORDER BY f.id""",
                    (int(transfer_id),))
                valid = await self._valid_material_bytes(db, [row["id"] for row in rows])
            finally:
                await db.rollback()
        return verified_material_measure(tuple(
            SimpleNamespace(id=row["id"], state=row["status"], expected_bytes=row["size_bytes"] or 0,
                            size_knowledge=SizeKnowledge.durable(row["size_bytes"], row["size_knowledge"]))
            for row in rows), valid)

    @staticmethod
    async def _valid_material_bytes(db, artifact_ids) -> dict[int, int]:
        """DP-valid material bytes per artifact (absent = nothing valid)."""
        ids = [int(item) for item in artifact_ids]
        if not ids:
            return {}
        rows = await db.fetchall(
            "SELECT artifact_id,valid_ranges,member_ranges FROM artifact_material_state "
            f"WHERE artifact_id IN ({','.join('?' * len(ids))})", tuple(ids))
        return {int(row["artifact_id"]): mat.total(mat.decode(row["valid_ranges"]))
                + sum(mat.total(ranges) for _member, ranges in mat.decode_members(row["member_ranges"])[0])
                for row in rows}

    async def material_state(self, artifact_id: int) -> mat.MaterialState | None:
        async with get_db() as db:
            row = await db.fetchone(self._MATERIAL_SELECT, (int(artifact_id),))
        return self._material(row) if row else None

    async def open_material_state(self, artifact: Artifact) -> mat.MaterialState:
        """The artifact's material, created lazily at its first planning.

        Migration policy for material that predates this owner: nothing is
        inferred from a partial file. A new row trusts no byte (the next
        writer re-establishes checkpointed validity); only an artifact that is
        already canonically completed carries its verified size as VALID.
        Idempotent: an existing row is returned untouched."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(self._MATERIAL_SELECT, (artifact.id,))
            if row is None:
                current = await db.fetchone("SELECT status,size_bytes FROM download_files WHERE id=?", (artifact.id,))
                if current is None:
                    await db.rollback()
                    raise KeyError(artifact.id)
                size = int(current.get("size_bytes") or 0)
                valid = ((0, size),) if current.get("status") == "completed" and size > 0 else ()
                await db.execute("""INSERT INTO artifact_material_state(artifact_id,geometry_version,valid_ranges,destination)
                    VALUES(?,?,?,?)""", (artifact.id, mat.GEOMETRY_VERSION, mat.encode(valid), str(artifact.target)))
                row = await db.fetchone(self._MATERIAL_SELECT, (artifact.id,))
            await db.commit()
        return self._material(row)

    async def _replace_material(self, db, row, *, valid, reason: str, advance: bool, destination=None,
                                members=None, member_identities=None, **facts) -> None:
        """Replace an artifact's VALID material (and, for a collection, its
        per-member material; ``members=None`` keeps it as it is)."""
        previous = mat.decode(row["valid_ranges"])
        previous_members, previous_identities = mat.decode_members(row.get("member_ranges"))
        members = previous_members if members is None else members
        member_identities = previous_identities if member_identities is None else member_identities
        lost = mat.total(mat.subtract(previous, valid)) + sum(
            mat.total(mat.subtract(ranges, dict(members).get(member, ()))) for member, ranges in previous_members)
        generation = int(row["material_generation"]) + (1 if advance else 0)
        await db.execute("""UPDATE artifact_material_state SET valid_ranges=?,member_ranges=?,material_generation=?,
            destination=?,destination_identity=CASE WHEN ? THEN '' ELSE destination_identity END,
            updated_at=CURRENT_TIMESTAMP WHERE artifact_id=?""",
                         (mat.encode(valid), mat.encode_members(members, member_identities), generation,
                          str(destination or row["destination"]), int(advance), row["artifact_id"]))
        await self._material_audit(
            db, row["transfer_id"], row["artifact_id"], "invalidated" if advance else "moved", reason=reason,
            material_generation=generation, invalidated_bytes=lost,
            invalidated=mat.summary(mat.subtract(previous, valid)), valid_bytes=mat.total(valid)
            + sum(mat.total(ranges) for _member, ranges in members), **facts,
        )
        if advance and lost:
            await journal(db, je.material_invalidated(transfer_id=row["transfer_id"], artifact_id=row["artifact_id"],
                                                      reason=reason))

    @staticmethod
    def _reconciled(valid, identity: str, facts, end_of_file):
        """One payload's VALID ranges against its observed facts:
        ``(kept, reason)``; ``reason`` is empty when nothing changed."""
        if not valid:
            return valid, ""
        if not facts.exists:
            return (), "payload_missing"
        if identity and facts.identity != identity:
            return (), "destination_replaced"
        keep, reason = valid, ""
        if facts.size < valid[-1][1]:
            keep = mat.align_inward(mat.intersect(keep, ((0, facts.size),)), end_of_file=end_of_file)
            reason = "payload_truncated"
        if mat.intersect(keep, facts.holes):
            keep = mat.align_inward(mat.subtract(keep, facts.holes), end_of_file=end_of_file)
            reason = reason or "payload_sparse"
        return keep, reason

    async def reconcile_material(self, artifact_id: int, target: str, facts,
                                 member_facts=None) -> mat.MaterialState | None:
        """Reconcile DebridPulse material truth with observed payload facts.

        Conservative in one direction only: missing, truncated, replaced or
        hole-bearing payload removes affected VALID ranges and advances the
        material generation (previously valid content may no longer mean the
        same thing, so any plan made against it is stale). Extra physical
        bytes, a larger file or a missing executor control file never add
        validity. A payload moved by canonical materialization is recognized
        only by its unchanged physical identity. Filesystem inspection is
        evidence here; this row stays the only semantic owner.

        ``member_facts`` (a collection): facts per member path, reconciled by
        exactly the same rules; any member losing validity advances the
        artifact's material generation."""
        if not getattr(facts, "available", False) and not member_facts:
            return await self.material_state(artifact_id)
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(self._MATERIAL_SELECT, (int(artifact_id),))
            if row is None:
                await db.rollback()
                return None
            state = self._material(row)
            valid, identity = state.valid, state.destination_identity
            moved = str(target) != state.destination
            reason, keep = "", valid
            if getattr(facts, "available", False):
                if moved and not (facts.exists and identity and facts.identity == identity) and valid:
                    keep, reason = (), "destination_replaced"
                else:
                    keep, reason = self._reconciled(valid, identity, facts, state.expected_size)
            members, identities = dict(state.members), dict(state.member_identities)
            for member, member_fact in dict(member_facts or {}).items():
                if member not in members or not getattr(member_fact, "available", False):
                    continue
                kept, why = self._reconciled(members[member], identities.get(member, ""), member_fact, None)
                if why:
                    members[member], reason = kept, reason or f"member_{why}"
            members_changed = tuple(sorted((m, r) for m, r in members.items() if r)) != state.members
            if keep == valid and not moved and not members_changed:
                await db.rollback()
                return state
            await self._replace_material(db, row, valid=keep, reason=reason or "destination_moved",
                                         advance=keep != valid or members_changed, destination=str(target),
                                         members=tuple(sorted((m, r) for m, r in members.items() if r)),
                                         member_identities=tuple(sorted(identities.items())),
                                         observed_bytes=int(facts.size) if getattr(facts, "exists", False) else None)
            row = await db.fetchone(self._MATERIAL_SELECT, (int(artifact_id),))
            await db.commit()
        return self._material(row)

    async def previous_writer(self, artifact_id: int):
        """The artifact's most recent execution attempt and whether DebridPulse
        durably owns the material it wrote (``None`` when it never had one)."""
        async with get_db() as db:
            row = await db.fetchone("SELECT * FROM execution_attempts WHERE artifact_id=? ORDER BY rowid DESC LIMIT 1",
                                    (int(artifact_id),))
        if row is None:
            return None
        attempt = self._execution_attempt(row)
        return SimpleNamespace(handle=attempt.handle, candidate=attempt.candidate, state=attempt.state,
                               owned=bool(row.get("material_owner_attempt_id")))

    async def execution_continuation(self, attempt_id: str) -> ContinuationPlan | None:
        """The continuation plan an attempt was admitted under (its writer
        authorization), or ``None`` when it was admitted without one."""
        async with get_db() as db:
            row = await db.fetchone("SELECT continuation FROM execution_attempts WHERE id=?", (attempt_id,))
        return ContinuationPlan.from_dict(codec.load(row["continuation"])) if row and row["continuation"] else None

    async def native_transition_from(self, attempt_id: str) -> str | None:
        """The attempt whose native object ``attempt_id`` inherited while that
        handoff's source replacement is still unproven; ``None`` otherwise."""
        async with get_db() as db:
            row = await db.fetchone("SELECT native_transition_from FROM execution_attempts WHERE id=?", (attempt_id,))
        return str(row["native_transition_from"]) if row and row["native_transition_from"] else None

    async def resolve_native_transition(self, handle: ExecutionHandle) -> bool:
        """The inherited native object was positively proven to serve this
        attempt's source (or was proven stopped): the transition is resolved
        and the attempt holds its ordinary authority again."""
        async with get_db() as db:
            cursor = await db.execute("""UPDATE execution_attempts SET native_transition_from=NULL,
                updated_at=CURRENT_TIMESTAMP WHERE id=? AND native_transition_from IS NOT NULL""", (handle.attempt_id,))
            await db.commit()
        return cursor.rowcount == 1

    async def execution_start_pending(self, attempt_id: str) -> bool:
        """The attempt was admitted but its native start result is not yet
        recorded: its dispatcher still owns it and will converge it."""
        async with get_db() as db:
            row = await db.fetchone("SELECT state FROM execution_attempts WHERE id=?", (attempt_id,))
        return bool(row) and row["state"] == "prepared"

    async def detach_retired_writer(self, artifact_id: int, attempt_id: str, *, state: str) -> bool:
        """Release a writer that was positively observed stopped: the artifact
        keeps no writer and its old attempt loses every authority, so neither
        a late callback nor a stale plan of it can act again. Only the exact
        still-current attempt, and only once it is terminal, is detached."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("""UPDATE download_files SET status=?,execution_attempt_id=NULL,
                continuation_reservation_expires_at=NULL,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND execution_attempt_id=? AND EXISTS(SELECT 1 FROM execution_attempts e
                    WHERE e.id=? AND e.state IN ('failed','absent','cancelled'))""",
                                      (state, int(artifact_id), attempt_id, attempt_id))
            if cursor.rowcount:
                await db.execute("UPDATE execution_attempts SET authorized=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                 (attempt_id,))
            await db.commit()
        return cursor.rowcount == 1

    async def invalidate_material(self, artifact_id: int, reason: str) -> None:
        """Previously valid content no longer means what it meant (a rejected
        verification, an incompatible rewrite, a repair that mutated it):
        nothing stays VALID and the material generation advances."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(self._MATERIAL_SELECT, (int(artifact_id),))
            if row is None:
                await db.rollback()
                return
            await self._replace_material(db, row, valid=(), reason=reason, advance=True, members=(),
                                         member_identities=())
            await db.commit()

    async def _admit_material_writer(self, db, artifact: Artifact, plan: ContinuationPlan) -> int | None:
        """Inside ``prepare_execution``'s transaction: bind a plan to the next
        writer generation, applying its rollback. ``None`` refuses admission."""
        row = await db.fetchone(self._MATERIAL_SELECT, (artifact.id,))
        if (row is None or plan.artifact_id != artifact.id
                or plan.material_generation != int(row["material_generation"])
                or plan.geometry_version != int(row["geometry_version"])):
            return None
        valid = mat.decode(row["valid_ranges"])
        members, identities = mat.decode_members(row.get("member_ranges"))
        members = dict(members)
        if mat.subtract(plan.retained, valid):
            return None
        kept_members = {}
        for member, boundary in plan.member_boundaries:
            if mat.subtract(((0, boundary),), members.get(member, ())):
                return None  # a stale plan: that member no longer holds what it keeps
            kept_members[member] = ((0, boundary),)
        writer = int(row.get("writer_generation") or 0) + 1
        await db.execute("""UPDATE artifact_material_state SET valid_ranges=?,member_ranges=?,writer_generation=?,
            destination=?,updated_at=CURRENT_TIMESTAMP WHERE artifact_id=?""",
                         (mat.encode(plan.retained), mat.encode_members(kept_members, identities), writer,
                          str(artifact.target), artifact.id))
        if plan.discarded_bytes:
            await self._material_audit(
                db, artifact.transfer_id, artifact.id, "rollback", reason=plan.reason,
                strategy=plan.strategy.value, material_generation=plan.material_generation,
                discarded_bytes=plan.discarded_bytes, discarded=mat.summary(plan.discarded),
                discarded_members=[member for member, _ranges in plan.member_discarded][:8],
                retained_bytes=plan.retained_bytes,
            )
        return writer

    _WRITER_SELECT = """SELECT e.handle,e.authorized,e.writer_generation AS attempt_writer,e.continuation,
            e.native_transition_from,
            f.execution_attempt_id AS current_id,m.*,f.size_bytes,f.size_knowledge,f.torrent_id AS transfer_id
        FROM execution_attempts e JOIN download_files f ON f.id=e.artifact_id
        JOIN artifact_material_state m ON m.artifact_id=e.artifact_id WHERE e.id=?"""

    @staticmethod
    def _writer_current(row, handle: ExecutionHandle) -> bool:
        """THE material writer fence: ``handle`` is the artifact's current,
        authorized writer of the current writer generation, admitted under a
        plan of the current material generation."""
        if (row is None or not row["authorized"] or row.get("current_id") != handle.attempt_id
                or codec.handle(codec.load(row["handle"])) != handle or row.get("attempt_writer") is None
                or int(row["attempt_writer"]) != int(row.get("writer_generation") or 0)
                or not row.get("continuation")):
            return False
        plan = codec.load(row["continuation"])
        return int(plan["material_generation"]) == int(row["material_generation"])

    async def private_reconstruction_bytes(self, artifact_id: int) -> int:
        """What the artifact's current writer has built privately under a
        DESTINATION_AWARE plan (``active_execution_progress_sql``): unverified
        executor work, never DP material, that a switch away abandons."""
        async with get_db() as db:
            row = await db.fetchone(active_execution_progress_sql("f.id = ?", reconstruction_only=True),
                                    (int(artifact_id),))
        return max(0, int((row or {}).get("execution_completed") or 0))

    async def material_writer_stale(self, handle: ExecutionHandle) -> bool:
        """A writer admitted under a continuation plan that no longer passes
        the material writer fence (another writer generation, or its material
        generation changed): its private native state must not be resumed or
        handed on. An attempt admitted without a plan is not a material writer
        and is never stale here."""
        async with get_db() as db:
            row = await db.fetchone(self._WRITER_SELECT, (handle.attempt_id,))
        return row is not None and bool(row.get("continuation")) and not self._writer_current(row, handle)

    async def commit_material(self, handle: ExecutionHandle, ranges, facts, *, now: float,
                              forced: str = "", member: str = "") -> mat.Ranges | None:
        """Commit checkpointed material for the CURRENT writer only.

        ``ranges`` are what the writer reported completely written BEFORE
        ``facts`` were produced by the durability flush. Refused (``None``)
        unless this exact attempt is still the artifact's current, authorized
        writer of the current writer generation and its plan names the
        current material generation; a superseded writer, a late callback or
        a stale plan commits nothing. Accepted material is clipped to the
        plan's authorized ranges, the known size and the flushed length, and
        aligned inward to the geometry. Returns the newly VALID ranges.

        ``member`` commits one member file of a COLLECTION artifact under the
        very same fence; it may be written from its plan boundary onwards."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(self._WRITER_SELECT, (handle.attempt_id,))
            # A writer whose inherited native source is unproven commits
            # nothing: its reports are not yet attributable to its source.
            if (not self._writer_current(row, handle) or row.get("native_transition_from")
                    or facts is None or not facts.exists):
                await db.rollback()
                return None
            plan = ContinuationPlan.from_dict(codec.load(row["continuation"]))
            state = self._material(row)
            if member:
                return await self._commit_member(db, row, state, plan, handle, member, ranges, facts, now=now,
                                                 forced=forced)
            bound = state.expected_size if state.expected_size else mat.OPEN_END
            accepted = mat.intersect(mat.intersect(ranges, plan.authorized), ((0, min(bound, int(facts.size))),))
            accepted = mat.align_inward(accepted, end_of_file=state.expected_size)
            valid = mat.union(state.valid, accepted)
            if state.destination_identity and facts.identity != state.destination_identity and state.valid:
                # A different physical payload keeps nothing -- unless it is
                # the complete verified replacement a destination-aware writer
                # was authorized to install: then it is the whole payload.
                whole = ((0, int(facts.size)),) if int(facts.size) > 0 else ()
                if not (plan.strategy == ContinuationStrategy.DESTINATION_AWARE and forced == "completion"
                        and whole and accepted == whole
                        and (not state.expected_size or int(facts.size) == state.expected_size)):
                    await db.rollback()
                    return None
                valid = accepted
            added = mat.subtract(valid, state.valid)
            await db.execute("""UPDATE artifact_material_state SET valid_ranges=?,destination_identity=?,checkpoint_at=?,
                checkpoint_attempt_id=?,updated_at=CURRENT_TIMESTAMP WHERE artifact_id=?""",
                             (mat.encode(valid), str(facts.identity), float(now), handle.attempt_id, state.artifact_id))
            if forced:
                await self._material_audit(
                    db, row["transfer_id"], state.artifact_id, "checkpoint", boundary=forced,
                    writer_generation=state.writer_generation, material_generation=state.material_generation,
                    valid_bytes=mat.total(valid), committed_bytes=mat.total(added),
                )
            await db.commit()
        return added

    async def _commit_member(self, db, row, state: mat.MaterialState, plan: ContinuationPlan,
                             handle: ExecutionHandle, member: str, ranges, facts, *, now: float,
                             forced: str) -> mat.Ranges | None:
        """``commit_material`` for one member of a collection (inside its
        transaction, already fenced to the current writer and generation)."""
        current = state.member_valid(member)
        identity = dict(state.member_identities).get(member, "")
        if current and identity and facts.identity != identity:
            await db.rollback()
            return None
        # Only a verified completion knows a member's end of file.
        end_of_file = int(facts.size) if forced == "completion" else None
        authorized = ((plan.member_boundary(member), mat.OPEN_END),)
        accepted = mat.intersect(mat.intersect(ranges, authorized), ((0, int(facts.size)),))
        accepted = mat.align_inward(accepted, end_of_file=end_of_file)
        valid = mat.union(current, accepted)
        added = mat.subtract(valid, current)
        members = {**dict(state.members), member: valid}
        identities = {**dict(state.member_identities), member: str(facts.identity)}
        await db.execute("""UPDATE artifact_material_state SET member_ranges=?,checkpoint_at=?,checkpoint_attempt_id=?,
            updated_at=CURRENT_TIMESTAMP WHERE artifact_id=?""",
                         (mat.encode_members(members, tuple(identities.items())), float(now), handle.attempt_id,
                          state.artifact_id))
        if forced:
            await self._material_audit(
                db, row["transfer_id"], state.artifact_id, "checkpoint", boundary=forced, member=member,
                writer_generation=state.writer_generation, material_generation=state.material_generation,
                valid_bytes=mat.total(valid), committed_bytes=mat.total(added),
            )
        await db.commit()
        return added

    async def bind_execution_handle(self, prepared: ExecutionHandle, bound: ExecutionHandle) -> bool:
        """THE one-way native identity binding of a durable execution attempt.

        Atomically permits exactly ``native: None -> value`` for the same
        attempt, executor and correlation, and only while that attempt is
        still authorized and current for its artifact. Idempotent when the
        persisted handle already equals ``bound``. Every other transition --
        attempt/executor/correlation change, native replacement or removal,
        binding a superseded or revoked attempt -- is refused. Nothing here
        interprets either opaque map."""
        if not isinstance(prepared, ExecutionHandle) or not isinstance(bound, ExecutionHandle):
            return False
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT e.handle,e.authorized,e.executor_id,f.execution_attempt_id AS current_id
                FROM execution_attempts e JOIN download_files f ON f.id=e.artifact_id WHERE e.id=?""",
                                    (prepared.attempt_id,))
            if not row:
                await db.rollback()
                return False
            persisted = codec.handle(codec.load(row["handle"]))
            if persisted == bound:
                await db.rollback()
                return True
            if (persisted != prepared or not prepared.binds(bound) or row["executor_id"] != bound.executor_id
                    or not row["authorized"] or row.get("current_id") != prepared.attempt_id):
                await db.rollback()
                return False
            cursor = await db.execute("UPDATE execution_attempts SET handle=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND handle=?",
                                      (codec.dump(bound), prepared.attempt_id, row["handle"]))
            await db.commit()
        return cursor.rowcount == 1

    async def record_materialization(self, handle: ExecutionHandle, result: MaterializationResult) -> bool:
        """Persist core-verified materialization truth for the still-current attempt."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT e.handle FROM execution_attempts e JOIN download_files f
                ON f.id=e.artifact_id AND f.execution_attempt_id=e.id WHERE e.id=? AND e.executor_id=?""",
                                    (handle.attempt_id, handle.executor_id))
            if not row or codec.handle(codec.load(row["handle"])) != handle:
                await db.rollback()
                return False
            await db.execute("UPDATE execution_attempts SET materialization=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             (codec.dump(result), handle.attempt_id))
            await db.commit()
        return True

    async def execution_materialization(self, attempt_id: str) -> MaterializationResult | None:
        async with get_db() as db:
            row = await db.fetchone("SELECT materialization FROM execution_attempts WHERE id=?", (attempt_id,))
        return codec.materialization(row["materialization"]) if row else None

    async def executors_with_live_work(self) -> frozenset[str]:
        """Executors that durably still own native work that might acquire:
        an authorized, not-yet-terminal attempt, or an attempt whose native
        cleanup has not been proven complete. Absence here is positive truth."""
        async with get_db() as db:
            rows = await db.fetchall("""SELECT DISTINCT executor_id FROM execution_attempts
                WHERE (authorized=1 AND state IN ('prepared','queued','running','paused','unknown'))
                   OR cleanup_state IN ('pending','blocked')""")
        return frozenset(str(row["executor_id"]) for row in rows)

    async def execution_owns_target(self, handle: ExecutionHandle) -> bool:
        """Positive material-ownership authority for ``handle``: True only when
        this exact attempt durably carries a material owner (established at
        its own admission or inherited from its artifact's preceding owning
        attempt) AND it is still the artifact's current execution. Unowned,
        superseded, or missing all answer False -- ownership is never inferred
        from the file itself."""
        async with get_db() as db:
            row = await db.fetchone(
                """SELECT e.material_owner_attempt_id FROM execution_attempts e
                    JOIN download_files f ON f.id=e.artifact_id AND f.execution_attempt_id=e.id
                    WHERE e.id=? AND e.executor_id=?""",
                (handle.attempt_id, handle.executor_id),
            )
        return bool(row) and bool(row.get("material_owner_attempt_id"))

    async def authorize_execution(self, handle: ExecutionHandle, action: str) -> bool:
        async with get_db() as db:
            row = await db.fetchone("""SELECT e.*,t.status AS transfer_status,COALESCE(p.paused,0) AS paused_intent,
                f.execution_attempt_id AS current_execution_id
                FROM execution_attempts e JOIN torrents t ON t.id=e.transfer_id
                JOIN download_files f ON f.id=e.artifact_id
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id WHERE e.id=?""", (handle.attempt_id,))
        if not row or not row["authorized"] or row["executor_id"] != handle.executor_id or codec.load(row["handle"]) != codec.load(codec.dump(handle)):
            return False
        is_current = row.get("current_execution_id") == handle.attempt_id
        if action in {"start", "resume", "pause"} and not is_current:
            return False
        if action == "cancel" and not is_current:
            cleanup_owned = row["transfer_status"] in {"deleted", "cancelled"} and row.get("cleanup_state") in {"pending", "blocked"}
            if not cleanup_owned:
                return False
        # An inherited native object whose source replacement is not proven
        # never gains acquisition authority: only the one native-transition
        # reconciliation (observe, pause, cancel) may act on it.
        if action in {"start", "resume"} and row.get("native_transition_from"):
            return False
        if action in {"start", "resume"} and (row["transfer_status"] in {"deleted", "completed", "consolidated", "cancelled"} or row["paused_intent"]):
            return False
        if action in {"start", "resume"} and await self.globally_paused():
            return False
        return action != "start" or row["state"] == "prepared"

    async def converge_staged_input(self, attempt_id: str, *, staged_context: dict,
                                    context_key: str, retire_context_keys=()) -> tuple[str, dict | None]:
        """Replace one execution's OBSOLETE INLINE submitted input with a
        canonical staged reference, atomically, in both durable places that
        carry it.

        A release that moved a large submitted payload out of the request and
        the candidate and into the staged-input owner leaves work resolved by
        the previous release carrying the old representation. Such work is
        still perfectly valid; only its input representation is obsolete, and
        the executor holding it must not write SQLite itself. This is the one
        durable seam for that convergence, handed to an owner as a bound
        callable exactly as ``authorize_execution`` is.

        It is generic: it names no request class, provider or executor. The
        caller states which context key its obsolete representation used and
        which one the canonical reference belongs under; everything else --
        atomicity, compare-and-swap, the digest agreement between the two
        durable copies, and proving the result -- belongs here.

        One transaction:

          1. resolve attempt -> artifact -> its root request;
          2. require the selected candidate to still carry the expected
             obsolete representation, and its bytes to match the staged
             reference BY DIGEST;
          3. require the request payload to be one of exactly two proven
             shapes -- the obsolete inline form carrying the SAME bytes, or a
             canonical reference to those same bytes (a half-converged row,
             where only the candidate side is left to do). Every other shape,
             including a canonical reference to DIFFERENT bytes, is a durable
             inconsistency: a freshly staged copy of the candidate's bytes is
             never authority to overwrite a durable request, so nothing is
             written and no representation is chosen for being the one that
             happened to be recognised;
          4. rewrite the request payload and the SELECTED candidate's context
             only, preserving every other candidate, every other context key,
             the candidate ordering and the selected index exactly. A
             half-converged row converges onto the reference ALREADY durable,
             never onto the caller's newer copy;
          5. prove both rows canonical inside the transaction, commit, then
             prove them again on a connection that never saw the transaction.

        Returns ``(outcome, proven_context)``:

          ``converged`` -- this call performed it; the context is the one now
                           durable.
          ``already``   -- the rows were already canonical, because another
                           worker won or this is a restart. The context is the
                           WINNER's, which is what the caller must use: staged
                           identities are minted per staging, so the caller's
                           own staged object is simply an orphan the ordinary
                           sweep reclaims.
          ``mismatch``  -- a durable inconsistency. Nothing was written.
          ``absent``    -- the attempt, artifact or request no longer exists.
        """
        retire = tuple(str(key) for key in retire_context_keys)
        reference = {"$staged": dict(staged_context)}
        async with get_db() as db:
            row = await db.fetchone(
                """SELECT e.artifact_id AS artifact_id, f.request_id AS request_id,
                          f.candidates AS candidates, f.selected_candidate AS selected,
                          r.payload AS payload
                   FROM execution_attempts e
                   JOIN download_files f ON f.id=e.artifact_id
                   JOIN transfer_requests r ON r.id=f.request_id
                   WHERE e.id=?""", (attempt_id,))
            if not row:
                return "absent", None

            candidates = codec.load(row["candidates"], [])
            selected = int(row["selected"] or 0)
            if not candidates or not (0 <= selected < len(candidates)):
                return "absent", None
            payload = codec.load(row["payload"], {}) or {}
            context = dict((candidates[selected].get("context") or {}))

            # Already canonical: report the durable reference, not ours.
            durable = payload.get("payload")
            if isinstance(durable, dict) and isinstance(durable.get("$staged"), dict) \
                    and isinstance(context.get(context_key), dict):
                return "already", dict(context[context_key])

            inline = next((key for key in retire if key in context), None)
            if inline is None:
                return "absent", None

            # The two durable copies were written from ONE payload. BOTH are
            # proven before EITHER is replaced -- there is no shape of request
            # payload that is simply accepted because it is not the expected
            # obsolete one. A request that is neither the obsolete inline form
            # nor a canonical reference to these exact bytes is a durable
            # inconsistency, and the caller's freshly staged copy is not
            # authority to overwrite it.
            digest = str(staged_context.get("sha256") or "")
            try:
                candidate_bytes = base64.b64decode(str(context[inline]), validate=True)
            except (ValueError, TypeError):
                return "mismatch", None
            if hashlib.sha256(candidate_bytes).hexdigest() != digest:
                return "mismatch", None

            if not isinstance(durable, dict):
                return "mismatch", None
            if "$bytes" in durable:
                # The obsolete inline form: it must be the same bytes.
                try:
                    request_bytes = base64.b64decode(str(durable["$bytes"]), validate=True)
                except (ValueError, TypeError):
                    return "mismatch", None
                if hashlib.sha256(request_bytes).hexdigest() != digest:
                    return "mismatch", None
            elif isinstance(durable.get("$staged"), dict):
                # A half-converged row: the request is already canonical while
                # the candidate is not. Recoverable ONLY when the reference it
                # already carries is to these exact bytes -- otherwise this
                # call would be choosing between two disagreeing authorities.
                existing = durable["$staged"]
                if str(existing.get("sha256") or "") != digest:
                    return "mismatch", None
                if int(existing.get("byte_length") or -1) != len(candidate_bytes):
                    return "mismatch", None
                # Converge the remaining side onto the reference that is
                # already durable, never onto the caller's newer copy.
                reference = {"$staged": dict(existing)}
                staged_context = dict(existing)
            else:
                return "mismatch", None

            converged = list(candidates)
            for key in retire:
                context.pop(key, None)
            context[context_key] = dict(staged_context)
            converged[selected] = {**candidates[selected], "context": context}

            # Compare-and-swap: both rows must still be exactly what was read,
            # so a worker that converged concurrently is never overwritten by
            # this call's stale candidate list.
            updated = await db.execute(
                "UPDATE transfer_requests SET payload=? WHERE id=? AND payload=?",
                (codec.dump({**payload, "payload": reference}), row["request_id"], row["payload"]))
            artifact = await db.execute(
                "UPDATE download_files SET candidates=? WHERE id=? AND candidates=?",
                (codec.dump(converged), row["artifact_id"], row["candidates"]))
            if not updated.rowcount or not artifact.rowcount:
                # Another worker changed a row between the read and the write.
                await db.rollback()
                return "already", None

            # Proven TWICE, deliberately.
            #
            # The frozen ordering is "persist -> commit -> re-read and prove".
            # Proving only after the commit would make an unproven rewrite
            # durable for as long as it took to discover, so the write is first
            # proven inside the transaction -- where a failure can still be
            # rolled back and the obsolete representation left intact and
            # retryable -- and then proven AGAIN after the commit, on a
            # connection that never saw the transaction. The second read is the
            # frozen one; the first only ensures nothing unproven can become
            # durable in order to reach it.
            if not await self._staged_input_is_canonical(
                    db, row["artifact_id"], staged_context, context_key, retire):
                await db.rollback()
                return "mismatch", None
            await db.commit()

        async with get_db() as fresh:
            if not await self._staged_input_is_canonical(
                    fresh, row["artifact_id"], staged_context, context_key, retire):
                return "mismatch", None
        return "converged", dict(staged_context)

    @staticmethod
    async def _staged_input_is_canonical(db, artifact_id, staged_context, context_key, retire) -> bool:
        """Read both durable rows and answer whether they are canonical.

        One reader, used inside the transaction and again afterwards, so the
        two proofs cannot drift apart.
        """
        proof = await db.fetchone(
            """SELECT f.candidates AS candidates, f.selected_candidate AS selected,
                      r.payload AS payload
               FROM download_files f JOIN transfer_requests r ON r.id=f.request_id
               WHERE f.id=?""", (artifact_id,))
        if not proof:
            return False
        candidates = codec.load(proof["candidates"], [])
        index = int(proof["selected"] or 0)
        if not (0 <= index < len(candidates)):
            return False
        context = dict((candidates[index].get("context") or {}))
        payload = (codec.load(proof["payload"], {}) or {}).get("payload")
        return (isinstance(payload, dict)
                and payload.get("$staged") == staged_context
                and context.get(context_key) == staged_context
                and not any(key in context for key in retire))

    async def artifact_state(self, artifact_id: int, state: str, *, error=None, retry_at=0, release=False, selected=None,
                             expected_bytes=None, size_knowledge=None, withdrawn=False):
        """``size_knowledge`` is the ONE durable writer of the canonical size
        fact, and only a verified completion supplies it: the caller has
        already proven the stable local payload against trusted evidence, so
        the value recorded here is always something that was demonstrated, not
        a default or a bookkeeping side effect. Every other artifact write
        leaves the column untouched, so a reported/bookkeeping size never
        manufactures size knowledge -- ``SizeKnowledge.durable`` reads such a
        row back as KNOWN_POSITIVE or UNKNOWN from the byte count alone.

        ``withdrawn`` (only with ``cancelled``): the operator withdrew the
        artifact, recorded against the attempt it is cancelled on
        (``withdrawn_attempt``). An observed native or internal cancel never
        passes it."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            current = await db.fetchone("SELECT execution_attempt_id,torrent_id,status,filename FROM download_files WHERE id=?",
                                        (artifact_id,))
            # Section 13: artifact_state() is never used to hold a continuation
            # reservation across a writer-replacement handoff -- only
            # transition_recovery()'s explicit continuation_reservation_until
            # sets one -- so every artifact_state() write releases it
            # unconditionally (cancellation, deletion, terminal failure, and
            # plain completion all funnel through here).
            cursor = await db.execute("""UPDATE download_files SET status=?,normalized_error=?,retry_at=?,
                withdrawn_attempt=CASE WHEN ? THEN COALESCE(execution_attempt_id,'') ELSE withdrawn_attempt END,
                execution_attempt_id=CASE WHEN ? THEN NULL ELSE execution_attempt_id END,
                selected_candidate=COALESCE(?,selected_candidate),size_bytes=COALESCE(?,size_bytes),
                size_knowledge=COALESCE(?,size_knowledge),
                continuation_reservation_expires_at=NULL,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND torrent_id IN (SELECT id FROM torrents
                    WHERE status NOT IN ('deleted','consolidated') AND (status!='cancelled' OR ?='cancelled'))""",
                (state, codec.dump(error) if error else None, retry_at, bool(withdrawn and state == "cancelled"),
                 release, selected, expected_bytes,
                 str(size_knowledge) if size_knowledge is not None else None, artifact_id, state))
            if cursor.rowcount and release and current and current.get("execution_attempt_id"):
                await db.execute("""UPDATE execution_attempts SET authorized=0,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND state IN ('failed','absent','cancelled','succeeded')""", (current["execution_attempt_id"],))
            if cursor.rowcount and state == "completed" and current and current.get("execution_attempt_id"):
                execution_id = current["execution_attempt_id"]
                await db.execute("""UPDATE execution_attempt_provenance SET delivered=1,outcome='completed',updated_at=CURRENT_TIMESTAMP
                    WHERE execution_attempt_id=?""", (execution_id,))
                route = await db.fetchone("SELECT route_attempt_id FROM execution_attempt_provenance WHERE execution_attempt_id=?", (execution_id,))
                if route and route.get("route_attempt_id"):
                    await db.execute("UPDATE route_attempt_provenance SET outcome='completed',updated_at=CURRENT_TIMESTAMP WHERE resolution_attempt_id=?",
                                     (route["route_attempt_id"],))
            if cursor.rowcount and current and current["status"] != state and state in {"completed", "error"}:
                if state == "completed":
                    await journal(db, je.artifact_completed(transfer_id=current["torrent_id"], artifact_id=artifact_id,
                                                            filename=current["filename"]))
                else:
                    await journal(db, je.artifact_failed(transfer_id=current["torrent_id"], artifact_id=artifact_id,
                                                         filename=current["filename"], error=error))
            await db.commit()

    async def executions(self, transfer_id: int | None = None) -> tuple[ExecutionAttempt, ...]:
        async with get_db() as db:
            rows = await db.fetchall("SELECT * FROM execution_attempts" + (" WHERE transfer_id=?" if transfer_id is not None else "") + " ORDER BY rowid",
                                     (transfer_id,) if transfer_id is not None else ())
        return tuple(self._execution_attempt(row) for row in rows)

    @staticmethod
    def _execution_attempt(row):
        return ExecutionAttempt(codec.handle(codec.load(row["handle"])), row["transfer_id"], row["artifact_id"], row["state"],
                                TransferProgress(**codec.load(row["progress"], {})), codec.error(row["error"]),
                                codec.candidate(codec.load(row["candidate"])) if row.get("candidate") else None)

    async def live_executions(self):
        async with get_db() as db:
            rows = await db.fetchall("""SELECT e.* FROM execution_attempts e
                JOIN download_files f ON f.execution_attempt_id=e.id JOIN torrents t ON t.id=e.transfer_id
                WHERE t.status NOT IN ('deleted','completed','consolidated','cancelled') AND e.authorized=1""")
        return tuple(self._execution_attempt(row) for row in rows)

    _OCCUPYING_EXECUTION_STATES = ("prepared", "queued", "running", "unknown")

    async def occupied_execution_slots(self, now: float, *, exclude_artifact_id: int | None = None) -> int:
        """DP 1.0.12 recovery leveling, Section 13: the ONE canonical execution-
        admission occupancy count -- genuinely live authorized writers plus any
        durable, unexpired continuation reservation (an artifact whose old
        writer was already retired but whose replacement has not yet
        dispatched). Every capacity gate in the engine must call this instead
        of counting live executions alone, or a reserved-but-not-yet-live slot
        could be stolen by unrelated queued work during the short
        writer-replacement handoff. ``exclude_artifact_id`` lets an artifact
        about to consume its own reservation check admission without
        self-blocking on it.

        Admission of that artifact (``exclude_artifact_id``) also counts the
        one slot an operator promotion keeps through an automatic retry: the
        highest-ranked unfinished, unpaused transfer holding a queue
        promotion whose artifact waits out a bounded retry backoff (or the
        candidate refresh a recovery decided) and which holds no slot of its
        own -- counted only against an artifact of a transfer ranked below
        it. It is derived from those durable facts alone, so it ends with
        them: the retry admitted, a pause, a demotion, a final failure.
        """
        placeholders = ",".join("?" for _ in self._OCCUPYING_EXECUTION_STATES)
        async with get_db() as db:
            live_row = await db.fetchone(
                f"""SELECT COUNT(*) AS n FROM execution_attempts e
                    JOIN download_files f ON f.execution_attempt_id=e.id JOIN torrents t ON t.id=e.transfer_id
                    WHERE t.status NOT IN ('deleted','completed','consolidated','cancelled') AND e.authorized=1
                    AND e.state IN ({placeholders}) AND (? IS NULL OR f.id!=?)""",
                (*self._OCCUPYING_EXECUTION_STATES, exclude_artifact_id, exclude_artifact_id),
            )
            reserved_row = await db.fetchone(
                """SELECT COUNT(*) AS n FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                    WHERE f.execution_attempt_id IS NULL AND f.continuation_reservation_expires_at IS NOT NULL
                    AND f.continuation_reservation_expires_at>? AND t.status NOT IN ('deleted','completed','consolidated','cancelled')
                    AND (? IS NULL OR f.id!=?)""",
                (now, exclude_artifact_id, exclude_artifact_id),
            )
            kept = 0
            if exclude_artifact_id is not None:
                kept_row = await db.fetchone(
                    f"""SELECT 1 AS n FROM download_files x JOIN torrents mine ON mine.id=x.torrent_id,
                        (SELECT t.id, t.priority FROM torrents t
                            LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id
                            WHERE t.status NOT IN ('deleted','completed','consolidated','cancelled')
                            AND t.queue_promotion IS NOT NULL AND COALESCE(p.paused,0)=0
                            AND EXISTS(SELECT 1 FROM download_files f
                                       JOIN artifact_recovery_state r ON r.artifact_id=f.id
                                       WHERE f.torrent_id=t.id AND f.execution_attempt_id IS NULL
                                       AND ((f.status='recovery_wait' AND r.quiescence_reason='retry_backoff')
                                            OR (f.status='refresh_pending' AND r.decision_action IS NOT NULL)))
                            AND NOT EXISTS(SELECT 1 FROM execution_attempts e
                                           JOIN download_files f ON f.execution_attempt_id=e.id
                                           WHERE f.torrent_id=t.id AND e.authorized=1
                                           AND e.state IN ({placeholders}))
                            ORDER BY t.priority DESC, t.id LIMIT 1) kept
                        WHERE x.id=? AND mine.id!=kept.id
                        AND (COALESCE(mine.priority,0)<COALESCE(kept.priority,0)
                             OR (COALESCE(mine.priority,0)=COALESCE(kept.priority,0) AND mine.id>kept.id))""",
                    (*self._OCCUPYING_EXECUTION_STATES, exclude_artifact_id),
                )
                kept = 1 if kept_row else 0
        return int((live_row or {}).get("n") or 0) + int((reserved_row or {}).get("n") or 0) + kept

    async def referenced_staged_inputs(self) -> set[str]:
        """Every staged-input identity a live transfer still depends on.

        "Live" is the same predicate execution admission uses: a transfer that
        is completed, consolidated, cancelled or deleted can no longer be
        resolved or executed, so its submitted input is no longer needed --
        while a transfer that may still retry keeps its input, because a retry
        submits the original bytes again.

        This is the survivor set for the one staged-input sweep. Deriving it
        from the durable rows is what lets a single owner cover every terminal
        path, including a crash, without a per-path callback that could be
        forgotten.
        """
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT r.payload FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id
                   WHERE t.status NOT IN ('completed','consolidated','deleted','cancelled')
                   AND r.payload LIKE '%"$staged"%'""")
        referenced: set[str] = set()
        for row in rows or ():
            payload = codec.load(row["payload"], {})
            staged = (payload or {}).get("payload")
            if isinstance(staged, dict) and isinstance(staged.get("$staged"), dict):
                identity = staged["$staged"].get("id")
                if identity:
                    referenced.add(str(identity))
        return referenced

    async def continuation_reservation(self, artifact_id: int) -> float | None:
        """Current raw reservation expiry for one artifact, or None. Test/
        provenance introspection only -- admission gates use
        ``occupied_execution_slots`` instead."""
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT continuation_reservation_expires_at FROM download_files WHERE id=?", (artifact_id,),
            )
        return float(row["continuation_reservation_expires_at"]) if row and row.get("continuation_reservation_expires_at") is not None else None

    async def resources(self, transfer_id: int):
        async with get_db() as db:
            rows = await db.fetchall("SELECT * FROM provider_resources WHERE transfer_id=?", (transfer_id,))
        return tuple((codec.resource(codec.load(row["payload"])), ResourceState(row["state"]), row["cleanup_authority"]) for row in rows)

    async def cleanup_intent(self, transfer_id: int, resource_key: str, authority: str | None, *, error=None,
                             db=None):
        """Set/clear cleanup responsibility for the (transfer, canonical resource)
        binding. A fresh non-null intent also clears any prior terminal-abandon
        marker so the fence and the cleanup cadence treat it as live again.
        Clearing responsibility (``authority is None``) also withdraws any claim
        lease: with nothing left to clean, no owner token can still be current.

        ``db`` joins a caller's open transaction instead of committing alone,
        so a transition that owes cleanup records it atomically with itself."""
        if db is None:
            async with get_db() as db:
                await self.cleanup_intent(transfer_id, resource_key, authority, error=error, db=db)
                await db.commit()
            return
        await db.execute(
            "UPDATE provider_resources SET cleanup_authority=?, cleanup_error=?, "
            "cleanup_abandoned=CASE WHEN ? IS NOT NULL THEN 0 ELSE cleanup_abandoned END, "
            "cleanup_claim_token=CASE WHEN ? IS NULL THEN NULL ELSE cleanup_claim_token END, "
            "cleanup_claim_until=CASE WHEN ? IS NULL THEN 0 ELSE cleanup_claim_until END, "
            "updated_at=CURRENT_TIMESTAMP "
            "WHERE transfer_id=? AND (resource_key=? OR (resource_key IS NULL AND id=?))",
            (authority, codec.dump(error) if error else None, authority, authority, authority,
             transfer_id, resource_key, resource_key),
        )

    # -------------------------------------------------------------------------
    # Provider-cleanup claim: the ONE lease/token owner.
    #
    # A claim is (``cleanup_claim_token``, ``cleanup_claim_until``). It is current
    # while the token is set and the absolute engine-clock expiry has not passed;
    # nothing else (no boolean, no generic timestamp) means "claimed". Every
    # outcome converges: COMPLETED (authority cleared), RETRYABLE at
    # ``cleanup_retry_at`` (claim released), PERMANENTLY ABANDONED (claim
    # released), or LEASED until the bounded expiry. The lease tracks LIVE
    # ownership: an owner whose provider call is still running renews it with its
    # own token (``renew_cleanup_claim``), so a live owner is never reclaimed. An
    # owner that is cancelled, crashes, or fails between acquisition and
    # finalization stops renewing and therefore cannot strand the row: once the
    # lease expires the same ordinary cadence (``pending_cleanup`` ->
    # ``claim_cleanup``) takes it over atomically, and the loser's late
    # finalization is rejected because its token is no longer current.
    # -------------------------------------------------------------------------

    async def pending_cleanup(self, now):
        """Rows the cleanup cadence may claim now: cleanup owed, not abandoned,
        retry time reached, and no *current* claim (unclaimed, or claim expired)."""
        async with get_db() as db:
            rows = await db.fetchall(
                "SELECT * FROM provider_resources WHERE cleanup_authority IS NOT NULL "
                "AND COALESCE(cleanup_abandoned, 0) = 0 AND cleanup_retry_at<=? "
                "AND (cleanup_claim_token IS NULL OR cleanup_claim_until<=?)",
                (now, now),
            )
        return tuple(
            (row["transfer_id"], codec.resource(codec.load(row["payload"])),
             row["cleanup_authority"], row["cleanup_attempts"], row["id"])
            for row in rows
        )

    async def claim_cleanup(self, binding_id: str, *, now: float, lease_until: float) -> str | None:
        """Atomically acquire the cleanup claim; returns the new owner token, or
        ``None`` when another current claim owns it (or nothing is owed). The
        conditional UPDATE is the sole arbiter, so two workers can never both
        hold a current claim; an expired claim is taken over here."""
        token = new_identity()
        async with get_db() as db:
            result = await db.execute(
                "UPDATE provider_resources SET cleanup_attempts=cleanup_attempts+1, "
                "cleanup_claim_token=?, cleanup_claim_until=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND cleanup_authority IS NOT NULL AND COALESCE(cleanup_abandoned, 0) = 0 "
                "AND cleanup_retry_at<=? AND (cleanup_claim_token IS NULL OR cleanup_claim_until<=?)",
                (token, float(lease_until), binding_id, now, now),
            )
            await db.commit()
        return token if result.rowcount == 1 else None

    async def renew_cleanup_claim(self, binding_id: str, token: str, *, now: float, lease_until: float) -> bool:
        """Owner heartbeat: extend the lease of the claim ``token`` currently holds.

        The lease means LIVE ownership, not merely elapsed time: while its
        ``provider.cleanup()`` call runs, the owner renews with the SAME token, so
        no other worker can reclaim (and start a second simultaneous remote
        cleanup on) the same native resource. Conditional on the current token, so
        an owner that has already lost the claim gets ``False`` -- and must stand
        down -- rather than resurrect it. The expiry only ever moves forward. A
        dead owner stops renewing, its lease runs out, and the ordinary cadence
        reclaims it (:meth:`claim_cleanup`)."""
        async with get_db() as db:
            result = await db.execute(
                "UPDATE provider_resources SET cleanup_claim_until=MAX(cleanup_claim_until, ?), "
                "updated_at=CURRENT_TIMESTAMP WHERE id=? AND cleanup_claim_token=? "
                "AND cleanup_authority IS NOT NULL AND COALESCE(cleanup_abandoned, 0) = 0",
                (float(lease_until), binding_id, token),
            )
            await db.commit()
        return result.rowcount == 1

    async def cleanup_complete(self, binding_id: str, token: str, *, absent: ProviderResource | None = None) -> bool:
        """Token-conditional completion (SUCCESS / SKIPPED): clear the cleanup
        responsibility and the claim in one transaction; a SUCCESS also records the
        binding ABSENT. Returns ``False`` (and changes nothing) when ``token`` is no
        longer the current claim -- a stale worker never overwrites a newer owner."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT transfer_id,provider_id FROM provider_resources WHERE id=? AND cleanup_claim_token=?",
                (binding_id, token),
            )
            if not row:
                await db.rollback()
                return False
            await db.execute(
                "UPDATE provider_resources SET cleanup_authority=NULL, cleanup_error=NULL, cleanup_retry_at=0, "
                "cleanup_claim_token=NULL, cleanup_claim_until=0, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (binding_id,),
            )
            if absent is not None:
                await self._resource(db, row["transfer_id"], absent, ResourceState.ABSENT, journaled=False)
            await journal(db, je.resource_cleanup_completed(transfer_id=row["transfer_id"], binding_id=binding_id,
                                                            provider_id=row["provider_id"], token=token,
                                                            absent=absent is not None))
            await db.commit()
        return True

    async def cleanup_retry(self, binding_id: str, token: str, error, retry_at) -> bool:
        """Token-conditional record of a failed provider cleanup call, releasing
        the claim. ``retry_at is None`` means policy has permanently given up: mark
        ``cleanup_abandoned`` so the fence releases and the cadence stops
        re-driving it. Returns ``False`` (and changes nothing) for a stale token."""
        terminal = retry_at is None
        async with get_db() as db:
            result = await db.execute(
                "UPDATE provider_resources SET cleanup_error=?, cleanup_retry_at=?, cleanup_abandoned=?, "
                "cleanup_claim_token=NULL, cleanup_claim_until=0, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND cleanup_claim_token=?",
                (codec.dump(error) if error else None, retry_at or 0, int(terminal), binding_id, token),
            )
            if terminal and result.rowcount == 1:
                row = await db.fetchone("SELECT transfer_id,provider_id FROM provider_resources WHERE id=?", (binding_id,))
                await journal(db, je.resource_cleanup_abandoned(transfer_id=row["transfer_id"], binding_id=binding_id,
                                                                provider_id=row["provider_id"], error=error, token=token))
            await db.commit()
        return result.rowcount == 1

    async def outcome(self, transfer_id: int, outcome, *, attempt_id=None):
        async with get_db() as db:
            await db.execute("INSERT INTO transfer_outcomes(transfer_id,attempt_id,kind,payload) VALUES(?,?,?,?)",
                             (transfer_id, attempt_id, outcome.kind, codec.dump(outcome)))
            await db.commit()

    async def retry_requests(self, transfer_id: int, *, request_id=None, reset_budget=False):
        """Requeue failed requests. ``reset_budget`` is the operator's new
        attempt -- a new routing campaign: besides a fresh retry budget, the
        providers the previous campaign exhausted are released (their ended
        routes stay ended, as history), so current applicability and order may
        consider every provider again. Exhaustion is never a permanent ban."""
        scope = "id=?" if request_id else "state='failed'"
        live = ("transfer_id=? AND transfer_id IN (SELECT id FROM torrents WHERE status NOT IN "
                "('completed','consolidated','deleted','cancelled')) AND " + scope)
        params = (transfer_id, request_id) if request_id else (transfer_id,)
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            if reset_budget:
                await db.execute("UPDATE resolution_attempts SET state='released',updated_at=CURRENT_TIMESTAMP "
                                 f"WHERE state='exhausted' AND request_id IN (SELECT id FROM transfer_requests WHERE {live})",
                                 params)
            await db.execute("UPDATE transfer_requests SET state='pending',retry_at=0,error=NULL,"
                             f"attempts=CASE WHEN ? THEN 0 ELSE attempts END WHERE {live}", (reset_budget, *params))
            await self._settle_alternative_selection(db, transfer_id)
            await db.commit()

    async def renew_parent(self, record, retry_at, *, reset_budget=False):
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("""UPDATE transfer_requests SET state='pending',retry_at=?,error=NULL,attempts=CASE WHEN ? THEN 0 ELSE attempts END
                WHERE id=? AND transfer_id IN (SELECT id FROM torrents WHERE status NOT IN ('completed','consolidated','deleted','cancelled'))""",
                (retry_at, reset_budget, record.id))
            await db.execute("""UPDATE transfer_requests SET state='waiting_parent',error=NULL WHERE parent_id=? AND id IN
                (SELECT request_id FROM download_files WHERE status IN ('error','unresolved','lost','cancelled'))
                AND transfer_id IN (SELECT id FROM torrents WHERE status NOT IN ('completed','consolidated','deleted','cancelled'))""", (record.id,))
            await db.commit()

    async def recovery_budget(self, artifact_id: int) -> tuple[int, int]:
        async with get_db() as db:
            row = await db.fetchone("SELECT recovery_failures,recovery_refreshes FROM download_files WHERE id=?", (artifact_id,))
        if not row:
            raise KeyError(artifact_id)
        return int(row["recovery_failures"] or 0), int(row["recovery_refreshes"] or 0)

    async def reset_postprocessing(self, transfer_id):
        """The post-processing reset on its own, for a caller that changes no
        lifecycle state; one that does passes ``state(reset_postprocessing=True)``."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            if not await _reset_postprocessing_in_db(db, transfer_id):
                return False
            await db.commit()
        return True

    async def globally_paused(self) -> bool:
        async with get_db() as db:
            row = await db.fetchone("SELECT value FROM transfer_controls WHERE key='paused'")
        return bool(row and row["value"] == "1")

    async def throughput_sustained_peak(self) -> int | None:
        """The highest validated sustained download throughput ever observed
        (bytes/second), or ``None`` before the first."""
        async with get_db() as db:
            row = await db.fetchone("SELECT value FROM transfer_controls WHERE key='throughput_sustained_peak'")
        return int(row["value"]) if row else None

    async def raise_throughput_sustained_peak(self, bytes_per_second: int) -> None:
        """Record a higher sustained peak; a lower or equal one changes nothing."""
        async with get_db() as db:
            await db.execute("""INSERT INTO transfer_controls(key,value) VALUES('throughput_sustained_peak',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                WHERE CAST(transfer_controls.value AS INTEGER) < CAST(excluded.value AS INTEGER)""",
                (str(int(bytes_per_second)),))
            await db.commit()

    async def global_pause(self, paused: bool):
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("SELECT value FROM transfer_controls WHERE key='paused'")
            await db.execute("""INSERT INTO transfer_controls(key,value) VALUES('paused',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", ("1" if paused else "0",))
            if bool(row and row["value"] == "1") != paused:
                await journal(db, JournalEvent(
                    "administration", "administration.processing_paused" if paused else "administration.processing_resumed",
                    "info", "Processing paused" if paused else "Processing resumed", "installation"))
            await db.commit()

    async def delete(self, transfer_id: int, *, remote: bool, now: float = 0, expected_demand=None,
                     owned_cleanup: bool = False) -> None:
        """Atomically tombstone a transfer and retain responsibility for launched executions.

        Delete also permanently retires the transfer's active dedupe identity: the
        original logical fingerprint is preserved in ``source_fingerprint`` and the
        unique ``hash`` key is replaced with a deterministic transfer-specific
        tombstone. The historical row, its provider/resource/execution provenance,
        outstanding cleanup responsibility, and any file-selection generations all
        remain scoped to this transfer; re-submitting the same source afterwards
        creates a genuinely fresh transfer (see ``admit``).

        ``torrents.delete_remote`` records what this Delete authorizes for the
        transfer's remote resources -- now and for any found later
        (``terminal_cleanup_authority``): ``remote`` (the operator's
        "remove from the provider"), else ``owned_cleanup`` (a retirement by
        the final external demand: what DebridPulse created or adopted), else
        nothing.
        """
        mode = (DELETE_REMOTE_USER_REQUEST if remote else
                DELETE_REMOTE_OWNED if owned_cleanup else DELETE_REMOTE_KEEP)
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            if not await self.hold_demand(db, transfer_id, expected_demand):
                await db.rollback()
                raise DemandChanged(transfer_id)
            row = await db.fetchone("SELECT id,hash,source_fingerprint,status FROM torrents WHERE id=?", (transfer_id,))
            if row and row["status"] != "deleted":
                await journal(db, je.deleted(transfer_id, remote=remote))
            if row:
                current_hash = str(row["hash"] or "")
                original = str(row["source_fingerprint"] or current_hash)
                tombstone = current_hash if current_hash.startswith("deleted:") else self._tombstone_hash(transfer_id, original)
                await db.execute(
                    """UPDATE torrents SET status='deleted',delete_remote=?,lifecycle_epoch=lifecycle_epoch+1,
                        source_fingerprint=COALESCE(source_fingerprint,?), hash=?,
                        updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (mode, original, tombstone, transfer_id),
                )
            else:
                await db.execute("""UPDATE torrents SET status='deleted',delete_remote=?,lifecycle_epoch=lifecycle_epoch+1,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?""", (mode, transfer_id))
            # Deleting a transfer ends its current file-selection intent; its
            # provider generations' selections stay as history. A later re-add
            # of the same source is a new transfer and chooses afresh.
            await db.execute(
                """DELETE FROM transfer_file_selection_intent_entries WHERE request_id IN
                   (SELECT request_id FROM transfer_file_selection_intents WHERE transfer_id=?)""", (transfer_id,))
            await db.execute("DELETE FROM transfer_file_selection_intents WHERE transfer_id=?", (transfer_id,))
            # FUNC-001: this path settles the parent into DELETED without
            # going through _write_lifecycle_transition, so it must invoke the
            # same transaction-local auxiliary-state retirement directly.
            await _retire_transfer_auxiliary_state_in_db(db, transfer_id)
            await db.execute(
                """UPDATE execution_attempts SET cleanup_state='pending',
                    cleanup_attempts=CASE WHEN cleanup_state IN ('pending','blocked') THEN cleanup_attempts ELSE 0 END,
                    cleanup_retry_at=CASE
                        WHEN cleanup_state IN ('pending','blocked') AND cleanup_retry_at>? THEN cleanup_retry_at
                        ELSE ? END,
                    cleanup_error=CASE WHEN cleanup_state IN ('pending','blocked') THEN cleanup_error ELSE NULL END,
                    updated_at=CURRENT_TIMESTAMP
                    WHERE transfer_id=? AND authorized=1
                    AND state IN ('prepared','queued','running','paused','unknown')""",
                (now, now, transfer_id),
            )
            # DP 1.0.12 recovery leveling, Section 13: DELETED is not a true
            # dead end -- transfers.policy.transition_allowed permits an
            # operator to resurrect a deleted transfer back to ACCEPTED. Relying
            # solely on occupied_execution_slots()'s torrents.status join filter
            # would let a reservation that predates the delete silently
            # reappear and consume capacity the moment that resurrection
            # happens, well before its TTL would have expired on its own.
            # Durably clear it here instead of only excluding it structurally.
            await db.execute(
                "UPDATE download_files SET continuation_reservation_expires_at=NULL WHERE torrent_id=?",
                (transfer_id,),
            )
            await db.commit()

    async def delete_remote_requested(self, transfer_id: int) -> bool:
        async with get_db() as db:
            row = await db.fetchone("SELECT delete_remote FROM torrents WHERE id=?", (transfer_id,))
        return bool(row and row["delete_remote"])

    async def retired_cleanup_authority(self, transfer_id: int) -> str | None:
        """The cleanup a resource of this no-longer-live transfer is owed
        (``terminal_cleanup_authority``); ``None`` for a live transfer."""
        async with get_db() as db:
            row = await db.fetchone("SELECT status,delete_remote FROM torrents WHERE id=?", (transfer_id,))
        if row is None or str(row["status"]) not in {str(state) for state in TERMINAL_TRANSFER_STATES}:
            return None
        return terminal_cleanup_authority(row["status"], row["delete_remote"])

    async def historical_binding(self, resource_key: str) -> dict | None:
        """The most recent binding of a provider resource by ANY transfer --
        live, finished, cancelled or deleted -- with its transfer's lifecycle:
        ``binding_id``, ``transfer_id``, ``status``, ``delete_remote``,
        ``cleanup_authority``, ``cleanup_abandoned``, ``state``, ``payload``.
        ``None`` when DebridPulse never bound it."""
        async with get_db() as db:
            row = await db.fetchone(
                """SELECT r.id AS binding_id,r.transfer_id,r.state,r.cleanup_authority,
                          COALESCE(r.cleanup_abandoned,0) AS cleanup_abandoned,r.payload,t.status,t.delete_remote
                   FROM provider_resources r JOIN torrents t ON t.id=r.transfer_id
                   WHERE r.resource_key=? OR (r.resource_key IS NULL AND r.id=?)
                   ORDER BY r.rowid DESC LIMIT 1""", (str(resource_key), str(resource_key)))
        return dict(row) if row else None

    async def rearm_retired_cleanup(self, binding_id: str, state: ResourceState, authority: str, *,
                                    now: float) -> bool:
        """A retired transfer's resource that its provider still lists: owed
        ``authority`` again (it is still there, whatever an earlier removal
        answered), recorded as observed -- in one transaction, and only while
        its transfer is still not live, nothing is owed or claimed for it
        already, and its cleanup was not abandoned. ``True`` when armed."""
        terminal = sorted(str(item) for item in TERMINAL_TRANSFER_STATES)
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            result = await db.execute(
                f"""UPDATE provider_resources SET state=?,cleanup_authority=?,cleanup_error=NULL,cleanup_retry_at=0,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND cleanup_authority IS NULL AND COALESCE(cleanup_abandoned,0)=0
                   AND (cleanup_claim_token IS NULL OR cleanup_claim_until<=?)
                   AND transfer_id IN (SELECT id FROM torrents WHERE status IN ({",".join("?" * len(terminal))}))""",
                (state.value, authority, binding_id, now, *terminal))
            await db.commit()
        return result.rowcount == 1

    async def standby_fenced_providers(self, transfer_id: int, request_id: str) -> set[str]:
        """The providers that must not prepare a NEW backup of this root now:
        one whose route of the root DebridPulse released (an operator switch
        or a readiness yield), and one whose resource on this transfer still
        owes cleanup (a backup bound to it would be deleted, or overwrite the
        removal). A released route's resource kept as that provider's backup
        (``replace_root_route`` ``retain_replaced``) is no new preparation: it
        is observed, taken over and reclaimed as the backup it is; once it is
        given back, the provider prepares nothing again here -- the fence
        separates a kept resource from a new creation."""
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT provider_id FROM resolution_attempts WHERE request_id=? AND state='released'
                   UNION
                   SELECT provider_id FROM provider_resources WHERE transfer_id=? AND cleanup_authority IS NOT NULL
                   AND COALESCE(cleanup_abandoned,0)=0""", (str(request_id), int(transfer_id)))
        return {str(row["provider_id"]) for row in rows}

    async def rename(self, transfer_id: int, name: str):
        async with get_db() as db:
            await db.execute("UPDATE torrents SET name=? WHERE id=? AND status!='deleted'", (name, transfer_id))
            await db.commit()

    async def adopt_inventory_resource(self, transfer_id: int, resource: ProviderResource, state: ResourceState) -> bool:
        """The ONE inventory-adoption transition: bind an observed provider
        resource to the root request of a transfer that INVENTORY ITSELF created
        (``torrents.source = 'inventory'``), in one transaction.

        Adoption is a binding that happens outside a resolution attempt, so it
        deliberately records no ``resolution_attempts`` / ``route_attempt_provenance``
        row (fabricating an attempt for something that never resolved would be
        untruthful). Its provenance is instead the durable, queryable pair
        ``transfers.source = 'inventory'`` + binding ``ownership = observed``.

        Only such an inventory-created import may be bound this way. A transfer
        that a user submitted owns its own resolution lifecycle (attempt, route
        provenance, cleanup fence, file-selection generation); silently rebinding
        it to whatever the provider happens to list is an unowned transition and is
        refused here, leaving that transfer to resolve through its own route. The
        predecessor-cleanup fence is honoured too: an adoption is a same-object
        provider-resource reuse, so it must wait exactly as a first resolution
        does. Returns whether the resource was adopted.
        """
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            # Decided in this commit: the import is still live work (every
            # terminal lifecycle -- cancelled included -- refuses; a transfer
            # cancelled after its admission never gains a binding), and no
            # other transfer, of any lifecycle, bound this native resource
            # meanwhile (its history owns it: never re-adopted).
            root = await db.fetchone(
                f"""SELECT r.id FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id
                   WHERE r.transfer_id=? AND r.parent_id IS NULL AND r.state IN ('pending','resolving','failed')
                   AND t.source='inventory'
                   AND t.status NOT IN ({",".join("?" * len(_TERMINAL_TRANSFER_STATUSES))})""",
                (transfer_id, *_TERMINAL_TRANSFER_STATUSES),
            )
            held_elsewhere = await db.fetchone(
                "SELECT 1 FROM provider_resources WHERE transfer_id!=? "
                "AND (resource_key=? OR (resource_key IS NULL AND id=?)) LIMIT 1",
                (transfer_id, str(resource.id), str(resource.id)))
            if not root or held_elsewhere or await self._predecessor_cleanup_blocks(db, transfer_id):
                await db.rollback()
                return False
            await self._resource(db, transfer_id, resource, state)
            await db.execute(
                "UPDATE transfer_requests SET state='waiting',resource=?,error=NULL WHERE id=?",
                (codec.dump(resource), root["id"]),
            )
            await db.commit()
        return True

    async def begin_refresh(self, record: RequestRecord, provider_id: str):
        identity = new_identity()
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            # Refresh is deliberately allowed for a request owned by a fully
            # consolidated source transfer: that request still owns the foreign
            # candidate's acquisition provenance even though its submission has
            # no independent scheduling authority.
            await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES(?,?,?,'started')",
                             (identity, record.id, provider_id))
            await self._begin_route_provenance(db, identity, record.transfer_id, record.id, provider_id, operation="refresh")
            await db.commit()
        return ResolutionAttempt(identity, record.id, provider_id, "started")

    async def resolved_candidates(self, request_id: str):
        async with get_db() as db:
            row = await db.fetchone("""SELECT a.result FROM resolution_attempts a
                LEFT JOIN route_attempt_provenance p ON p.resolution_attempt_id=a.id
                WHERE a.request_id=? AND a.state='succeeded'
                ORDER BY COALESCE(p.ordinal,0) DESC,a.updated_at DESC,a.id DESC LIMIT 1""", (request_id,))
        result = codec.load(row["result"], {}) if row else {}
        return tuple(codec.candidate(value) for value in result.get("candidates", []))

    async def poll_after(self, request_id: str, timestamp: float, *, waiting=False):
        # Generic re-poll scheduling for the provider cadence (PREPARING re-poll,
        # cleanup-barrier hold, adopted-resource observation). It never touches
        # ``error``: a request left ``state='waiting' AND error IS NULL`` is by
        # construction a benign poll cadence, which is exactly the distinction
        # confirm_file_selection / dismiss_file_selection / the atomic
        # file-selection gate rely on (see the retry_at multi-purpose note in
        # transfers.repository). The interactive file-selection wait is now
        # scheduled inside the atomic gate transaction, not here.
        async with get_db() as db:
            await db.execute(
                "UPDATE transfer_requests SET retry_at=?,"
                "state=CASE WHEN ? THEN 'waiting' ELSE state END WHERE id=?",
                (timestamp, waiting, request_id),
            )
            await db.commit()

    async def add_alternate(self, primary: Artifact, record: RequestRecord, candidates, size: int):
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            current = await db.fetchone("""SELECT f.* FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                WHERE f.id=? AND t.status NOT IN ('deleted','consolidated')""", (primary.id,))
            if not current:
                return False
            retained = [replace(codec.candidate(item), expected_bytes=size) for item in codec.load(current["candidates"], [])]
            alternatives = tuple(replace(item, expected_bytes=size) for item in candidates)
            retained.extend(alternatives)
            await db.execute("UPDATE download_files SET candidates=?,size_bytes=?,mirror_group_id=?,mirror_state='primary' WHERE id=?",
                             (codec.dump(retained), size, primary.id, primary.id))
            await db.execute("""INSERT INTO download_files(torrent_id,request_id,filename,size_bytes,local_path,status,blocked,
                mirror_group_id,mirror_state,candidates,download_client) VALUES(?,?,?,?,?,'duplicate',NULL,?,'standby',?,'')""",
                (record.transfer_id, record.id, alternatives[0].name, size, primary.target, primary.id, codec.dump(alternatives)))
            await db.execute("UPDATE transfer_requests SET state='resolved',error=NULL WHERE id=?", (record.id,))
            await db.commit()
        return True

    async def select_artifact(self, transfer_id: int, artifact_id: int, selected: bool, *, expected_demand=None) -> bool:
        """Select or block one artifact. Returns whether its selection CHANGED:
        ``False`` when it already was what the operator asked for, which
        changes nothing at all (its owner must not reopen anything for it)."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            if not await self.hold_demand(db, transfer_id, expected_demand):
                await db.rollback()
                raise DemandChanged(transfer_id)
            row = await db.fetchone("SELECT * FROM download_files WHERE id=? AND torrent_id=?", (artifact_id, transfer_id))
            if not row or not row["request_id"]:
                raise KeyError(artifact_id)
            transfer = await db.fetchone("SELECT status FROM torrents WHERE id=?", (transfer_id,))
            if not transfer or transfer["status"] == "consolidated":
                raise TransferError(NormalizedError(Domain.LIFECYCLE, Category.RESOURCE_STATE_CONFLICT, Stage.QUEUE))
            if bool(row["blocked"]) == (not selected):
                await db.rollback()
                return False
            if row["execution_attempt_id"] or row["status"] not in {"queued", "unresolved", "paused", "blocked", "pending"}:
                raise TransferError(NormalizedError(Domain.LIFECYCLE, Category.RESOURCE_STATE_CONFLICT, Stage.QUEUE))
            member = await db.fetchone(
                "SELECT parent_id,metadata,materialized_selection_id FROM transfer_requests WHERE id=?",
                (row["request_id"],))
            if member is not None and member["parent_id"]:
                try:
                    await _edit_selection_intent_member_in_db(db, member["parent_id"], transfer_id, member, selected,
                                                              time.time())
                except TransferError:
                    await db.rollback()
                    raise
            ready = bool(codec.load(row["candidates"], []))
            await db.execute("UPDATE download_files SET blocked=?,status=? WHERE id=?",
                             (int(not selected), ("queued" if ready else "unresolved") if selected else "blocked", artifact_id))
            await db.execute("UPDATE transfer_requests SET state=? WHERE id=?",
                             (("resolved" if ready else "pending") if selected else "skipped", row["request_id"]))
            await db.commit()
        return True
