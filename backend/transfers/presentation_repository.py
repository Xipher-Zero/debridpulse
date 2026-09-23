"""Safe browser projection over canonical transfer and recovery truth.

This extension owns no lifecycle, recovery policy, or persistence state. It
projects safe source identity plus the current durable recovery/lifecycle state
already owned by the canonical repository. Historical execution progress is
used only as retained artifact truth; it never creates another progress store.
"""
from __future__ import annotations

import json

from core.presentation_safety import safe_public_host
from db.database import get_db
from transfers import codec
from transfers._repository_base import is_canonical_artifact_row
from transfers.manual_failover import SWITCH_ELIGIBLE_LIFECYCLE_STATES as _SWITCHABLE_STATES
from transfers.models import BITTORRENT_REQUEST_KINDS, TORRENT_FILE_REQUEST_KINDS, TransferProgress
from transfers.repository import TransferRepository as _CanonicalTransferRepository


_OPERATOR_WAKES = frozenset({"operator_retry"})

# "Can this child make future progress without an operator action?" (DP 1.0.12
# recovery leveling, Section 8) is defined as an EXCLUSION over the small,
# closed family of states that inherently need an operator (or represent no
# further work at all) -- not a hand-maintained inclusion list of every state
# considered autonomous "so far". A new wait/processing presentation status
# introduced later is correctly autonomous by default without touching this
# set; only a new operator-gated or terminal status needs to be added here.
# ``requires_attention``/``input_required`` are exactly the states this
# codebase reserves for "needs an explicit operator action"; ``paused`` needs
# an operator resume; ``failed`` is a permanent (fail_permanently) outcome
# with no further automatic retry -- an operator must retry or switch
# candidate. See ``is_autonomous_presentation`` below, the single semantic
# authority _aggregate_presentation consults.
_OPERATOR_GATED_PRESENTATION = frozenset({"requires_attention", "input_required", "paused", "failed"})
_TERMINAL_PRESENTATION = frozenset({"completed", "cancelled", "deleted", "consolidated"})


def is_autonomous_presentation(child) -> bool:
    """Whether a child's presentation represents work still capable of
    progressing without an operator action.

    ``child`` is a presentation dict as produced by ``recovery_presentation``
    (or anything exposing the same ``presentation_status``/
    ``attention_required`` keys). ``attention_required`` is checked directly
    -- the same semantic property ``recovery_presentation`` already computes
    for "needs an explicit operator decision" -- rather than re-deriving it
    from a status-string comparison.
    """
    if isinstance(child, dict):
        if child.get("attention_required"):
            return False
        status = child.get("presentation_status")
    else:
        status = child
    status = str(status or "").strip().lower()
    return status not in _OPERATOR_GATED_PRESENTATION and status not in _TERMINAL_PRESENTATION


_CAPACITY_WAIT_PRESENTATION = ("waiting_for_slot", "Waiting for execution slot", "queued")
_WAIT_PRESENTATION = {
    "retry_backoff": ("waiting_for_retry", "Waiting for retry", "queued"),
    "provider_disabled": ("waiting_for_provider", "Waiting for provider", "pending"),
    "provider_unavailable": ("waiting_for_provider", "Waiting for provider", "pending"),
    "storage_unavailable": ("waiting_for_storage", "Waiting for storage", "pending"),
    "executor_unavailable": ("waiting_for_executor", "Waiting for executor", "pending"),
}

# Canonical "the provider is preparing the current resource" presentation
# override. This is presentation-only: the durable transfer lifecycle state is
# never changed. It reuses the existing ``waiting_for_provider`` presentation
# status/label already produced for provider-quiescence recovery, so no new
# frontend contract or badge is introduced.
_GENERIC_PENDING_PRESENTATION = frozenset({"pending", "processing"})
_WAITING_FOR_PROVIDER = ("waiting_for_provider", "Waiting for provider", "pending")


def waiting_for_provider_override(presentation_status, current_resource_state):
    """Return the canonical Waiting-for-provider presentation triple, or ``None``.

    Applies only when the transfer's CURRENT authoritative root provider-resource
    binding reports ``PREPARING`` (``current_resource_state``) and the transfer
    has not otherwise advanced past a generic pre-provider-work presentation
    (``presentation_status`` in ``pending``/``processing``). A historical,
    tombstoned, or predecessor resource can never reach this because the caller
    resolves ``current_resource_state`` from the current root binding only.
    """
    if str(current_resource_state or "").strip().lower() != "preparing":
        return None
    if str(presentation_status or "").strip().lower() not in _GENERIC_PENDING_PRESENTATION:
        return None
    state, label, badge = _WAITING_FOR_PROVIDER
    return {
        "presentation_status": state,
        "presentation_label": label,
        "presentation_badge_status": badge,
    }


_RAW_PRESENTATION = {
    "completed": ("completed", "Done", "completed"),
    "paused": ("paused", "Paused", "paused"),
    "downloading": ("downloading", "Downloading", "downloading"),
    "queued": ("queued", "Queued", "queued"),
    "pending": ("pending", "Pending", "pending"),
    "processing": ("processing", "Processing", "processing"),
    "ready": ("ready", "Ready", "ready"),
    "verifying": ("verifying", "Verifying", "verifying"),
    "refresh_pending": ("recovering", "Recovering", "processing"),
    "unresolved": ("unresolved", "Resolving", "unresolved"),
    "cancelled": ("cancelled", "Cancelled", "cancelled"),
    "deleted": ("deleted", "Deleted", "deleted"),
    "consolidated": ("consolidated", "Consolidated", "consolidated"),
    "error": ("failed", "Failed", "error"),
    "failed": ("failed", "Failed", "error"),
    "lost": ("failed", "Failed", "error"),
    "unknown": ("unknown", "Awaiting confirmation", "unknown"),
}


def _candidate_source(value) -> dict[str, str] | None:
    if not isinstance(value, dict) or str(value.get("scope") or "").strip().lower() != "host":
        return None
    host = safe_public_host(value.get("key"))
    return {"kind": "host", "host": host} if host else None


def public_source_identity(request_kind, candidate_source=None) -> dict[str, str]:
    """Project only a safe source identity, with root request-type precedence."""
    kind = str(request_kind or "").strip().lower()
    if kind == "magnet":
        return {"kind": "magnet"}
    if kind in TORRENT_FILE_REQUEST_KINDS:
        return {"kind": "torrent_file"}
    if kind in {"http", "https"}:
        return _candidate_source(candidate_source) or {"kind": "link"}
    return {"kind": "link"}


def _decode_source(value):
    try:
        return codec.load(value, None)
    except (TypeError, ValueError, KeyError):
        return None


def _progress(value) -> TransferProgress:
    try:
        payload = codec.load(value, {}) if value else {}
        return TransferProgress(**payload) if isinstance(payload, dict) else TransferProgress()
    except (TypeError, ValueError, KeyError):
        return TransferProgress()


def recovery_presentation(status, context=None, *, paused=False, input_required=False,
                           capacity_only_blocked=False):
    """Map persisted lifecycle/recovery truth to one thin presentation contract.

    ``capacity_only_blocked`` is a single fact supplied by the caller, sourced
    from ``transfers.convergence_engine.TransferEngine.capacity_only_blocked_ids``
    (DP 1.0.12 recovery leveling, Section 9) -- the set of artifact ids the
    REAL ``_dispatch()`` most recently reached the capacity admission gate for
    and rejected there, having already passed target validation, candidate
    expiry, existing-payload, ``executor.prepare()`` (no InputRequirement),
    and storage/pause admission via actual code execution. This function does
    NOT re-derive or re-check any of those gates itself -- it only reads the
    one boolean the execution-admission owner already decided, refining a
    genuinely plain ``queued`` row (no quiescence recorded) into the distinct
    ``waiting_for_slot`` presentation. Reset every reconcile cycle, so a
    caller that queries a stale/no-longer-true fact simply sees plain
    ``queued`` for at most one scheduler tick -- understating, never
    overstating, capacity as the cause.
    """
    raw = str(status or "").strip().lower()
    context = context if isinstance(context, dict) else {}
    action = str(context.get("decision_action") or context.get("last_applied_action") or "").strip().lower()
    reason = str(context.get("decision_reason") or context.get("last_applied_reason") or "").strip()
    quiescence = str(context.get("quiescence_reason") or "").strip().lower()
    wake = str(context.get("wake_condition") or "").strip()

    if raw == "completed":
        state, label, badge = _RAW_PRESENTATION["completed"]
    elif paused or raw == "paused":
        state, label, badge = _RAW_PRESENTATION["paused"]
    elif input_required or quiescence == "input_required" or wake == "operator_input":
        state, label, badge = "input_required", "Input Required", "input_required"
    else:
        attention = bool(
            action == "wait_for_operator"
            and reason
            and wake in _OPERATOR_WAKES
        )
        if attention:
            state, label, badge = "requires_attention", "Requires attention", "error"
        elif quiescence in _WAIT_PRESENTATION:
            state, label, badge = _WAIT_PRESENTATION[quiescence]
        elif raw in {"refresh_pending", "recovery_wait"} or context.get("recovery_claim_token"):
            state, label, badge = "recovering", "Recovering", "processing"
        elif action == "fail_permanently" and raw in {"error", "failed", "lost"}:
            state, label, badge = "failed", "Failed", "error"
        elif raw == "queued" and not quiescence and capacity_only_blocked:
            state, label, badge = _CAPACITY_WAIT_PRESENTATION
        else:
            state, label, badge = _RAW_PRESENTATION.get(raw, (raw or "unknown", (raw or "Unknown").replace("_", " ").title(), raw or "unknown"))

    return {
        "presentation_status": state,
        "presentation_label": label,
        "presentation_badge_status": badge,
        "attention_required": state == "requires_attention",
        "recovery_action": action or None,
        "recovery_reason": reason or None,
        "quiescence_reason": quiescence or None,
        "wake_condition": wake or None,
    }


# Raw latest-recovery-snapshot keys that ``recovery_presentation`` consults to
# derive an artifact's effective child presentation. The bounded Downloads/
# Dashboard projection selects exactly these as page-scoped facts so it can feed
# the shared owner the same child truth the comprehensive Details projection
# feeds it. Kept here so "which durable facts drive a child presentation" has one
# definition next to the function that reads them.
ARTIFACT_PRESENTATION_SNAPSHOT_KEYS = (
    "quiescence_reason",
    "decision_action",
    "last_applied_action",
    "decision_reason",
    "last_applied_reason",
    "wake_condition",
    "recovery_claim_token",
)


def _aggregate_presentation(raw_status, file_presentations, *, paused=False, input_required=False):
    if str(raw_status or "").lower() == "completed":
        return recovery_presentation("completed")
    if paused:
        return recovery_presentation("paused", paused=True)
    if input_required:
        return recovery_presentation(raw_status, input_required=True)
    active = [item for item in file_presentations if item.get("presentation_status") != "completed"]
    if not active:
        return recovery_presentation(raw_status)
    for wanted in ("downloading", "recovering"):
        found = next((item for item in active if item.get("presentation_status") == wanted), None)
        if found:
            return dict(found)
    for wanted in ("waiting_for_provider", "waiting_for_storage", "waiting_for_retry", "waiting_for_executor"):
        found = next((item for item in active if item.get("presentation_status") == wanted), None)
        if found:
            return dict(found)
    # Operator attention is aggregate truth only after no child remains capable
    # of autonomous useful work.
    attention = next((item for item in active if item.get("presentation_status") == "requires_attention"), None)
    if attention and not any(is_autonomous_presentation(item) for item in active):
        return dict(attention)
    return recovery_presentation(raw_status)


def effective_presentation(
    raw_status,
    file_presentations,
    *,
    paused=False,
    input_required=False,
    current_resource_state=None,
):
    """The one owner of a transfer's effective presentation for BOTH projections.

    The comprehensive Details projection
    (``TransferRepository.presentation``) and the bounded Downloads/Dashboard
    list projection (``api.operational_downloads.list_operational_torrents``)
    both call this with the same logical inputs — the durable transfer status,
    the per-artifact child presentations (already produced by
    ``recovery_presentation``), the pause / input-required signals, and the
    state of the CURRENT authoritative root provider-resource binding — so the
    three user-facing surfaces can never derive a contradictory processing
    truth for the same durable facts.

    The PREPARING -> "Waiting for provider" override is applied last, and only
    against a generic ``pending``/``processing`` aggregate, so it refines the
    generic pre-provider-work presentation without ever overwriting a
    more-specific one (paused, input-required, a recovery-quiescence
    ``waiting_for_*``, ``recovering``, ``requires_attention``, failure, ...).
    """
    aggregate = _aggregate_presentation(
        raw_status, file_presentations, paused=paused, input_required=input_required,
    )
    override = waiting_for_provider_override(
        aggregate.get("presentation_status"), current_resource_state,
    )
    if override:
        aggregate = {**aggregate, **override}
    return aggregate


# --------------------------------------------------------------------------- #
# Details-only canonical-object Files presentation.
#
# ``files[]`` / ``file_count`` are, and remain, PHYSICAL transfer-local artifact
# truth -- the rows of ``download_files WHERE torrent_id=?``. They are not
# redefined, re-scoped or recounted here.
#
# ``file_presentations`` is a separate, additive, Details-only READ MODEL: the
# complete canonical-object source/file story the Details Files card shows --
# this transfer's own physical artifacts, the artifacts other transfers durably
# contributed to the canonical object, and the terminal UNVERIFIED associations
# that have no artifact at all by design. Nothing here is persisted and nothing
# is inferred: a contributed row exists only because
# ``canonical_candidate_bindings`` -> ``canonical_candidate_origins`` names its
# real contributing artifact, and an association row exists only because the
# equivalence owner durably wrote ``equivalence_disposition='unverified'``
# against an artifact this transfer owns. No URL, host, filename or transfer
# adjacency is ever consulted.
#
# NOTE: the unrelated local named ``file_presentations`` inside
# ``presentation()`` below is the per-artifact recovery-presentation list that
# feeds ``effective_presentation``. It is not this collection.
# --------------------------------------------------------------------------- #

_NATIVE_RELATIONSHIP = "original"
_CONTRIBUTED_RELATIONSHIP = "consolidated"
_UNVERIFIED_RELATIONSHIP = "unverified"
_VERIFIED = "verified"
_UNVERIFIED_STATUS = "unverified"
_PRESENTATION_FIELDS = ("presentation_status", "presentation_label", "presentation_badge_status")


def _artifact_presentation_id(artifact_id) -> str:
    return f"artifact:{int(artifact_id)}"


def _association_presentation_id(request_id) -> str:
    return f"request:{request_id}"


def _presentation_fields(status) -> dict:
    """The badge triple, from the ONE presentation owner.

    A contributed artifact must read exactly as a native duplicate reads
    ("Duplicate", not a raw lowercase status), so this goes through
    ``recovery_presentation`` rather than restating any label as a literal.
    """
    projection = recovery_presentation(status)
    return {key: projection[key] for key in _PRESENTATION_FIELDS}


def contributed_artifact_refs(candidate_bindings, transfer_id) -> list[tuple]:
    """``(artifact_id, contributing_transfer_id, request_id)`` for every VERIFIED
    artifact another transfer contributed to this transfer's canonical
    artifacts -- each exactly once, in durable binding/origin order.

    The input is the durable ``canonical_candidate_bindings`` ->
    ``canonical_candidate_origins`` projection the canonical repository already
    assembled for Details. Route History is never read, and no candidate URL,
    host or filename participates.
    """
    refs, seen = [], set()
    for binding in candidate_bindings or []:
        for origin in binding.get("origins") or []:
            try:
                artifact_id = int(origin["contributing_artifact_id"])
                contributor = int(origin["contributing_transfer_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if contributor == int(transfer_id) or artifact_id in seen:
                continue
            seen.add(artifact_id)
            refs.append((artifact_id, contributor, origin.get("request_id")))
    return refs


def canonical_file_presentations(files, refs, contributed, associations, *, transfer_id) -> list[dict]:
    """Assemble the Details-only canonical-object Files rows in one
    backend-owned deterministic order:

    1. native physical artifacts, in their durable artifact order;
    2. verified contributed artifacts, in durable binding/origin order;
    3. terminal UNVERIFIED associations, in durable request order.

    ``presentation_id`` is the row's render/DOM identity and is always present.
    ``artifact_id`` is the real artifact-MUTATION identity and is ``None`` for
    an association row, which has no artifact. The two are deliberately
    separate: a presentation row must never be able to address an artifact
    operation, and no fake artifact id is ever minted to make one look
    addressable.
    """
    rows = []
    for item in files or []:
        artifact_id = int(item["id"])
        rows.append({
            **item,
            "presentation_id": _artifact_presentation_id(artifact_id),
            "artifact_id": artifact_id,
            "relationship": _NATIVE_RELATIONSHIP,
            "verification_state": _VERIFIED,
            "contributing_transfer_id": int(transfer_id),
        })
    by_id = {int(row["id"]): row for row in contributed or []}
    for artifact_id, contributor, request_id in refs:
        artifact = by_id.get(artifact_id)
        if artifact is None:
            # The durable origin names an artifact that no longer exists. The
            # row is omitted; it is never synthesized from anything else.
            continue
        status = artifact.get("status")
        rows.append({
            "presentation_id": _artifact_presentation_id(artifact_id),
            "artifact_id": artifact_id,
            "request_id": request_id,
            "filename": artifact.get("filename"),
            "size_bytes": artifact.get("size_bytes"),
            "status": status,
            **_presentation_fields(status),
            "relationship": _CONTRIBUTED_RELATIONSHIP,
            "verification_state": _VERIFIED,
            "contributing_transfer_id": contributor,
        })
    for item in associations or []:
        rows.append({
            "presentation_id": _association_presentation_id(item["request_id"]),
            # No artifact exists by design, so there is no artifact identity to
            # borrow -- and none is invented.
            "artifact_id": None,
            "request_id": item["request_id"],
            "filename": item.get("filename"),
            # Independently unknown. The canonical artifact's size is a
            # DIFFERENT object's fact and is never borrowed to fill this in.
            "size_bytes": None,
            "status": _UNVERIFIED_STATUS,
            **_presentation_fields(_UNVERIFIED_STATUS),
            "unverified_reason": item.get("unverified_reason"),
            "relationship": _UNVERIFIED_RELATIONSHIP,
            "verification_state": _UNVERIFIED_STATUS,
            "contributing_transfer_id": item.get("contributing_transfer_id"),
        })
    return rows


class TransferRepository(_CanonicalTransferRepository):
    """Canonical production repository plus safe recovery/source presentation."""

    async def presentation(self, transfer_id: int, details: bool = False, *,
                            capacity_only_blocked_ids=frozenset()):
        """``capacity_only_blocked_ids`` (Section 9) is the live set from
        ``transfers.convergence_engine.TransferEngine.capacity_only_blocked_ids``
        -- the REAL dispatch path's own positive record of which artifact ids
        it most recently confirmed are blocked only by execution capacity.
        This repository never holds a live engine reference, so a caller
        that cannot supply it (``application.observability``, an internal
        ``super()`` call, or a repository-only test) simply gets the
        conservative empty-set default: capacity wait is never claimed
        without the execution-admission owner's say-so, which only narrows
        the classification, never widens it.
        """
        result = await super().presentation(transfer_id, details=details)
        if not result:
            return result
        if details:
            await self._overlay_candidate_presentation(result, transfer_id)

        request_kind = ""
        request_kinds: list[str] = []
        candidate_source = None
        selected_source = None
        selected_provider = ""
        failover_transitions = []
        paused = False
        file_rows = []
        progress_rows = []
        contexts = {}
        contributed_refs = []
        contributed_artifacts = []
        associations = []
        async with get_db() as db:
            root = await db.fetchone(
                """SELECT payload FROM transfer_requests
                    WHERE transfer_id=? AND parent_id IS NULL
                    ORDER BY ordinal,id LIMIT 1""",
                (transfer_id,),
            )
            if root is None:
                root = await db.fetchone(
                    """SELECT payload FROM transfer_requests
                        WHERE transfer_id=? ORDER BY ordinal,id LIMIT 1""",
                    (transfer_id,),
                )
            if root and root.get("payload"):
                try:
                    request_kind = str(codec.request(codec.load(root["payload"])).kind or "").strip().lower()
                except (TypeError, ValueError, KeyError):
                    request_kind = ""

            # The canonical request kinds of this transfer's ROOT requests.
            # Derived, never stored a second time: the kind already lives in the
            # durable request payload, so it survives a restart on its own. It
            # is the fact that separates the submission CHANNEL (torrents.source
            # -- how the operator handed the work over) from what was actually
            # submitted, which is what "Submitted As" has to report. A transfer
            # may legitimately own several root requests, so this is the DISTINCT
            # set rather than the first row's kind.
            kind_rows = await db.fetchall(
                """SELECT DISTINCT LOWER(COALESCE(json_extract(payload, '$.kind'), '')) AS kind
                     FROM transfer_requests
                    WHERE transfer_id=? AND parent_id IS NULL""",
                (transfer_id,),
            )
            request_kinds = sorted({str(row.get("kind") or "").strip()
                                    for row in kind_rows} - {""})

            pause_row = await db.fetchone(
                "SELECT paused FROM transfer_pause_intents WHERE torrent_id=?", (transfer_id,)
            )
            paused = bool(pause_row and pause_row.get("paused"))

            # Durable state of the CURRENT authoritative root provider-resource
            # binding only: the root request's own ``resource`` payload id is
            # matched against this transfer's binding row (transfer-scoped, so a
            # predecessor/tombstoned resource on another transfer is excluded;
            # ``resource_key`` NULL is the pre-split historical form where the
            # primary key is the canonical id). Never "any PREPARING resource".
            current_resource_state = None
            resource_row = await db.fetchone(
                """SELECT pr.state AS resource_state
                     FROM transfer_requests r
                     JOIN provider_resources pr
                       ON pr.transfer_id = r.transfer_id
                      AND (pr.resource_key = json_extract(r.resource, '$.id')
                           OR (pr.resource_key IS NULL
                               AND pr.id = json_extract(r.resource, '$.id')))
                    WHERE r.transfer_id = ? AND r.parent_id IS NULL AND r.resource IS NOT NULL
                    ORDER BY r.ordinal, r.id LIMIT 1""",
                (transfer_id,),
            )
            if resource_row:
                current_resource_state = resource_row.get("resource_state")
            file_rows = await db.fetchall(
                """SELECT id,torrent_id,request_id,status,size_bytes,blocked,mirror_state,
                          recovery_failures,recovery_refreshes
                    FROM download_files WHERE torrent_id=? ORDER BY id""", (transfer_id,)
            )
            progress_rows = await db.fetchall(
                """SELECT artifact_id,progress FROM execution_attempts
                    WHERE transfer_id=? AND progress IS NOT NULL ORDER BY created_at,id""", (transfer_id,)
            )

            # Magnet/torrent identities are dictated by the root request and must
            # not be replaced by provider-generated HTTP descendants.
            if request_kind not in BITTORRENT_REQUEST_KINDS:
                if str(result.get("status") or "").strip().lower() == "completed":
                    row = await db.fetchone(
                        """SELECT p.candidate_source FROM execution_attempt_provenance p
                            JOIN execution_attempts e ON e.id=p.execution_attempt_id
                            WHERE p.transfer_id=? AND p.delivered=1
                            ORDER BY e.updated_at DESC,p.ordinal DESC,e.id DESC LIMIT 1""",
                        (transfer_id,),
                    )
                    if row:
                        candidate_source = _decode_source(row.get("candidate_source"))

                if _candidate_source(candidate_source) is None:
                    row = await db.fetchone(
                        """SELECT p.candidate_source FROM download_files f
                            JOIN execution_attempt_provenance p ON p.execution_attempt_id=f.execution_attempt_id
                            WHERE f.torrent_id=? AND f.execution_attempt_id IS NOT NULL
                              AND COALESCE(f.mirror_state,'')!='standby'
                            ORDER BY f.updated_at DESC,p.ordinal DESC,f.id DESC LIMIT 1""",
                        (transfer_id,),
                    )
                    if row:
                        candidate_source = _decode_source(row.get("candidate_source"))

                if _candidate_source(candidate_source) is None:
                    row = await db.fetchone(
                        """SELECT candidate_summary FROM route_attempt_provenance
                            WHERE transfer_id=? ORDER BY ordinal DESC,updated_at DESC LIMIT 1""",
                        (transfer_id,),
                    )
                    if row:
                        try:
                            candidates = codec.load(row.get("candidate_summary"), [])
                        except (TypeError, ValueError, KeyError):
                            candidates = []
                        if isinstance(candidates, list):
                            for candidate in candidates:
                                source = candidate.get("source") if isinstance(candidate, dict) else None
                                if _candidate_source(source) is not None:
                                    candidate_source = source
                                    break

            # The bound candidate of the artifact that is currently (or next)
            # being fetched is the authoritative ACTIVE source of a transfer that
            # is not yet completed; a completed transfer keeps the delivered
            # provenance resolved above.
            if str(result.get("status") or "").lower() != "completed":
                selected_row = await db.fetchone(
                    """SELECT b.source_scope,b.source_key,b.provider_id
                        FROM download_files f
                        JOIN canonical_candidate_bindings b
                          ON b.canonical_artifact_id=f.id
                         AND b.candidate_order=f.selected_candidate+1
                        WHERE f.torrent_id=? AND COALESCE(f.mirror_state,'')!='standby'
                          AND f.status NOT IN ('completed','cancelled','duplicate')
                        ORDER BY CASE f.status WHEN 'downloading' THEN 0 WHEN 'paused' THEN 1 WHEN 'queued' THEN 2 ELSE 3 END,
                          f.updated_at DESC,f.id DESC LIMIT 1""",
                    (transfer_id,),
                )
                if selected_row:
                    selected_source = {"scope": selected_row.get("source_scope"), "key": selected_row.get("source_key")}
                    selected_provider = str(selected_row.get("provider_id") or "")

            if details:
                for event_row in await db.fetchall(
                    """SELECT detail,created_at FROM application_events
                        WHERE transfer_id=? AND kind='manual_candidate_failover' ORDER BY id""",
                    (transfer_id,),
                ):
                    try:
                        item = json.loads(event_row.get("detail") or "{}")
                    except (TypeError, ValueError):
                        continue
                    if isinstance(item, dict):
                        item["created_at"] = event_row.get("created_at")
                        failover_transitions.append(item)

                # Canonical-object Files presentation (Details only), read in the
                # session presentation() already owns. Both reads are bounded and
                # set-based: one IN() over the durably named contributing
                # artifacts, one over the durable equivalence associations. There
                # is no per-row source archaeology and no N+1.
                contributed_refs = contributed_artifact_refs(result.get("candidate_bindings"), transfer_id)
                if contributed_refs:
                    placeholders = ",".join("?" for _ in contributed_refs)
                    contributed_artifacts = [dict(row) for row in await db.fetchall(
                        f"""SELECT id,filename,size_bytes,status FROM download_files
                            WHERE id IN ({placeholders})""",
                        tuple(ref[0] for ref in contributed_refs),
                    )]
                # Exactly the durable predicate the canonical-object Route History
                # projection uses for a terminal association: a request the
                # equivalence owner durably marked 'unverified' against an
                # artifact THIS transfer owns. Its name is the durable request's
                # own name -- never a URL, host or filename guess.
                for row in await db.fetchall(
                    """SELECT r.id AS request_id,r.transfer_id AS contributing_transfer_id,
                        r.equivalence_reason,r.payload FROM transfer_requests r
                        JOIN download_files f ON f.id=r.equivalence_target_artifact_id
                        WHERE r.equivalence_disposition='unverified' AND r.transfer_id!=?
                        AND f.torrent_id=? AND COALESCE(f.mirror_state,'')!='standby'
                        ORDER BY r.transfer_id,r.ordinal,r.id""",
                    (transfer_id, transfer_id),
                ):
                    try:
                        name = codec.request(codec.load(row["payload"])).name
                    except (TypeError, ValueError, KeyError):
                        name = None
                    associations.append({
                        "request_id": row["request_id"],
                        "contributing_transfer_id": int(row["contributing_transfer_id"]),
                        "unverified_reason": row["equivalence_reason"] or None,
                        "filename": name or None,
                    })

            # Presentation already owns this DB session. Reuse it for durable
            # recovery snapshots instead of opening one fresh SQLite connection
            # per artifact through recovery_context(). Details need every child;
            # list rows can omit completed-child recovery history because the
            # completed presentation is independent of recovery context.
            snapshot_reader = getattr(self, "_recovery_snapshot", None)
            if callable(snapshot_reader):
                for row in file_rows:
                    if not details and str(row.get("status") or "").lower() == "completed":
                        continue
                    artifact_id = int(row["id"])
                    try:
                        contexts[artifact_id] = await snapshot_reader(db, artifact_id, row=row)
                    except (KeyError, TypeError, ValueError):
                        contexts[artifact_id] = {}

        retained = {}
        for row in progress_rows:
            artifact_id = int(row["artifact_id"])
            retained[artifact_id] = max(retained.get(artifact_id, 0), int(_progress(row.get("progress")).completed_bytes or 0))
        for row in file_rows:
            if str(row.get("status") or "").lower() == "completed" and int(row.get("size_bytes") or 0) > 0:
                retained[int(row["id"])] = max(retained.get(int(row["id"]), 0), int(row["size_bytes"]))

        challenge = bool(result.get("input_required"))
        file_presentations = []
        file_projection = {}
        voting_presentations = []
        total_expected = 0
        total_retained = 0
        for row in file_rows:
            artifact_id = int(row["id"])
            # Section 9: the only fact consulted for capacity-wait is whether
            # the REAL dispatch path put this exact artifact id in
            # capacity_only_blocked_ids -- no re-derivation of candidate,
            # provider, executor, or storage state here.
            projection = recovery_presentation(
                row.get("status"), contexts.get(artifact_id), paused=paused, input_required=challenge,
                capacity_only_blocked=artifact_id in capacity_only_blocked_ids,
            )
            retained_bytes = max(0, retained.get(artifact_id, 0))
            expected_bytes = max(0, int(row.get("size_bytes") or 0))
            projection["retained_bytes"] = min(retained_bytes, expected_bytes) if expected_bytes else retained_bytes
            file_projection[artifact_id] = projection
            file_presentations.append(projection)
            # Only a canonical actionable artifact may vote in the transfer's
            # aggregate presentation truth (Section 7); a blocked, standby, or
            # non-request-bound row is still projected above for Details but
            # is excluded here.
            if is_canonical_artifact_row(row):
                voting_presentations.append(projection)
            if not bool(row.get("blocked")) and str(row.get("mirror_state") or "") != "standby":
                total_expected += expected_bytes
                total_retained += projection["retained_bytes"]

        result.update(effective_presentation(
            result.get("status"), voting_presentations,
            paused=paused, input_required=challenge,
            current_resource_state=current_resource_state,
        ))
        result["retained_bytes"] = total_retained
        if str(result.get("status") or "").lower() == "completed":
            result["progress"] = 100.0
        elif total_expected > 0:
            retained_percent = min(100.0, total_retained / total_expected * 100.0)
            result["progress"] = max(float(result.get("progress") or 0), retained_percent)

        if details and result.get("files"):
            for item in result["files"]:
                artifact_id = int(item["id"])
                projection = file_projection.get(artifact_id, recovery_presentation(item.get("status")))
                item.update(projection)
                retained_bytes = int(projection.get("retained_bytes") or 0)
                item["retained_bytes"] = retained_bytes
                size = max(0, int(item.get("size_bytes") or 0))
                if str(item.get("status") or "").lower() == "completed":
                    item["progress"] = 100.0
                elif size > 0:
                    item["progress"] = min(100.0, retained_bytes / size * 100.0)
                if item.get("presentation_status") != "downloading":
                    item["download_speed"] = 0

        result["request_kinds"] = request_kinds
        result["current_source_identity"] = public_source_identity(request_kind, candidate_source)
        if _candidate_source(selected_source) is not None:
            result["current_source_identity"] = public_source_identity(request_kind, selected_source)
        if selected_provider:
            result["current_provider_id"] = selected_provider

        if details and isinstance(result.get("files"), list):
            for file in result["files"]:
                state = str(file.get("status") or "").lower()
                candidates = file.get("acquisition_candidates")
                if not isinstance(candidates, list):
                    continue
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    selected = bool(candidate.get("is_selected"))
                    candidate["is_active"] = selected
                    # Historical execution failure is truthful provenance, not a
                    # permanent capability verdict. The write path revalidates the
                    # exact bound candidate/provider before switching, so the UI
                    # must not suppress a retry solely because an older attempt failed.
                    candidate["switch_eligible"] = not selected and state in _SWITCHABLE_STATES
            result["manual_candidate_failovers"] = failover_transitions

        if details:
            # Assembled last, so the native rows carry the SAME fully-overlaid
            # artifact truth files[] carries -- one candidate owner, one
            # presentation owner, no second computation. Contributed and
            # association rows deliberately carry no candidate_count /
            # acquisition_candidates / source_candidates: verified candidate
            # membership and failover remain the canonical artifact's alone.
            result["file_presentations"] = canonical_file_presentations(
                result.get("files"), contributed_refs, contributed_artifacts, associations,
                transfer_id=transfer_id,
            )
        return result
