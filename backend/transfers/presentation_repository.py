"""Safe browser projection over canonical transfer and recovery truth.

This extension owns no lifecycle, recovery policy, or persistence state. It
projects safe source identity plus the current durable recovery/lifecycle state
already owned by the canonical repository. Historical execution progress is
used only as retained artifact truth; it never creates another progress store.
"""
from __future__ import annotations

import re

from db.database import get_db
from transfers import codec
from transfers.models import TransferProgress
from transfers.repository import TransferRepository as _CanonicalTransferRepository


_TORRENT_REQUEST_KINDS = frozenset({"torrent", "torrent_file", "file"})
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_OPERATOR_WAKES = frozenset({"operator_retry"})
_AUTONOMOUS_PRESENTATION = frozenset({
    "downloading", "recovering", "waiting_for_retry", "waiting_for_provider",
    "waiting_for_storage", "waiting_for_executor",
})
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


def _public_host(value) -> str | None:
    host = str(value or "").strip().lower().removeprefix("www.").rstrip(".")
    if not host or len(host) > 253:
        return None
    labels = host.split(".")
    if any(not label or not _HOST_LABEL_RE.fullmatch(label) for label in labels):
        return None
    return host


def _candidate_source(value) -> dict[str, str] | None:
    if not isinstance(value, dict) or str(value.get("scope") or "").strip().lower() != "host":
        return None
    host = _public_host(value.get("key"))
    return {"kind": "host", "host": host} if host else None


def public_source_identity(request_kind, candidate_source=None) -> dict[str, str]:
    """Project only a safe source identity, with root request-type precedence."""
    kind = str(request_kind or "").strip().lower()
    if kind == "magnet":
        return {"kind": "magnet"}
    if kind in _TORRENT_REQUEST_KINDS:
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


def recovery_presentation(status, context=None, *, paused=False, input_required=False):
    """Map persisted lifecycle/recovery truth to one thin presentation contract."""
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
    if attention and not any(item.get("presentation_status") in _AUTONOMOUS_PRESENTATION for item in active):
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


class TransferRepository(_CanonicalTransferRepository):
    """Canonical production repository plus safe recovery/source presentation."""

    async def presentation(self, transfer_id: int, details: bool = False):
        result = await super().presentation(transfer_id, details=details)
        if not result:
            return result

        request_kind = ""
        candidate_source = None
        paused = False
        file_rows = []
        progress_rows = []
        contexts = {}
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
                """SELECT id,torrent_id,status,size_bytes,blocked,mirror_state,
                          recovery_failures,recovery_refreshes
                    FROM download_files WHERE torrent_id=? ORDER BY id""", (transfer_id,)
            )
            progress_rows = await db.fetchall(
                """SELECT artifact_id,progress FROM execution_attempts
                    WHERE transfer_id=? AND progress IS NOT NULL ORDER BY created_at,id""", (transfer_id,)
            )

            # Magnet/torrent identities are dictated by the root request and must
            # not be replaced by provider-generated HTTP descendants.
            if request_kind not in {"magnet", *_TORRENT_REQUEST_KINDS}:
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
        total_expected = 0
        total_retained = 0
        for row in file_rows:
            artifact_id = int(row["id"])
            projection = recovery_presentation(
                row.get("status"), contexts.get(artifact_id), paused=paused, input_required=challenge,
            )
            retained_bytes = max(0, retained.get(artifact_id, 0))
            expected_bytes = max(0, int(row.get("size_bytes") or 0))
            projection["retained_bytes"] = min(retained_bytes, expected_bytes) if expected_bytes else retained_bytes
            file_projection[artifact_id] = projection
            file_presentations.append(projection)
            if not bool(row.get("blocked")) and str(row.get("mirror_state") or "") != "standby":
                total_expected += expected_bytes
                total_retained += projection["retained_bytes"]

        result.update(effective_presentation(
            result.get("status"), file_presentations,
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

        result["current_source_identity"] = public_source_identity(request_kind, candidate_source)
        return result
