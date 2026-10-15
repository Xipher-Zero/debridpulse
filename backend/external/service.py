"""The ONE owner of the native external-client contract (protocol v1).

An external client submits demand, inspects its own jobs and deliveries,
acknowledges a delivery, withdraws its demand and asks for advisory pause or
resume. Everything it does goes through an existing owner:

* admission -- the ``ApplicationService`` submission seams, with the job's own
  ``SubmissionDemand`` (its ID reserved here first, so a job and its demand
  can never diverge: the job is accepted exactly when that demand exists);
* withdrawal -- the demand ledger (``ApplicationService.withdraw_demand``);
  the transfer core alone decides what withdrawn demand means;
* pause/resume -- adjudicated here, applied by the core transition
  (``engine.pause``/``engine.resume``) on the operator-control boundary
  (``ApplicationService.adjudicated_control``), never through an operator
  route, and only while this job's demand is the transfer's only demand;
* delivery -- the engine's own delivery-time re-verification
  (``engine.delivered_material``), confined to the client's profile root;
* a protocol without request IDs (the *arr SABnzbd dialect) -- the same
  admission under a key this owner derives (``submit_live``), and the
  client's whole visible queue and history (``visible``).

No queue, provider, executor, candidate or deletion authority is exposed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import posixpath
import time
import uuid
import weakref
from dataclasses import dataclass

from auth.accounts import owned_account_ids
from auth.models import AuthMechanism, Principal
from auth.throttle import DualWindowRateLimiter
from core.logging_utils import sanitize_exception
from db.database import get_db
from db.event_journal import JournalEvent, record
from external import intake as intake_owner
from external.profiles import DeliveryProfile, category_key, confined_to
from transfers.filesystem import member_payload
from transfers.models import SubmissionDemand

logger = logging.getLogger("debridpulse.external")

PROTOCOL = "debridpulse.external/1"
MAX_ACTIVE_JOBS_PER_CLIENT = 2000
MAX_CONTROLS_PER_JOB = 64
MAX_PAGE = 200
DEFAULT_PAGE = 100
# How many unsettled jobs one list request or one maintenance pass refreshes.
REFRESH_BUDGET = 200
# An available, unacknowledged delivery is re-verified at most this often by
# list polling; inspecting one job or its delivery always re-verifies.
DELIVERY_RECHECK_SECONDS = 60.0
# A reservation whose demand never came to exist was never accepted: the
# caller was never given its ID. It is forgotten after this long.
RESERVATION_TTL_SECONDS = 24 * 3600
_KEY_LIMIT = 200
_TERMINAL = frozenset({"completed", "consolidated", "cancelled", "deleted"})
_PHASES = ("accepted", "queued", "processing", "completed", "failed", "withdrawn")

# Mutations (submit, withdraw, control, acknowledge) per client per minute.
external_mutation_limiter = DualWindowRateLimiter(per_peer_limit=120, global_limit=1200, max_peers=4096)
_submission_locks: "weakref.WeakValueDictionary[tuple, asyncio.Lock]" = weakref.WeakValueDictionary()
_control_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()

REFUSAL_CODES = (
    "client_credential_required", "rate_limited", "client_job_limit", "invalid_request", "idempotency_key_required",
    "idempotency_conflict", "unknown_category", "source_kind_not_permitted", "invalid_source", "unsupported_source",
    "source_too_large", "source_refused", "source_unavailable", "local_network_confirmation_required",
    "acquisition_scope_required", "submission_outcome_unknown", "job_not_found", "control_limit",
    "control_conflict", "delivery_mismatch", "unknown_generation", "stale_generation",
)


class ExternalRefused(Exception):
    def __init__(self, code: str, message: str, *, status: int = 422, retryable: bool = False, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.retryable, self.extra = code, message, status, retryable, extra

    def body(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, **self.extra}


def _lock(table, key) -> asyncio.Lock:
    lock = table.get(key)
    if lock is None:
        lock = asyncio.Lock()
        table[key] = lock
    return lock


def client_of(principal: Principal | None) -> str:
    """The external client a request acts as. Only an Integration token
    carries one; a session, a password or the standalone principal is no
    external client, whatever its role."""
    if (principal is None or principal.mechanism is not AuthMechanism.API_TOKEN
            or not principal.client_id or not principal.account_id):
        raise ExternalRefused("client_credential_required", "An Integration token is required", status=403)
    return principal.client_id


def _holder(control_id: str) -> str:
    """The pause owner's name for the one advisory hold a control applied."""
    return f"external:{control_id}"


def _owns(control: dict | None, hold: dict | None) -> bool:
    """Whether the transfer's standing pause intent is still exactly the hold
    ``control`` applied: paused, that holder, that generation. Anything else
    -- an operator's or global write since (whatever its timestamp), a
    settlement, no intent -- is not the client's."""
    return bool(control is not None and hold is not None and hold["paused"]
                and control["pause_holder"] and hold["holder"] == control["pause_holder"]
                and control["pause_generation"] is not None
                and int(hold["generation"]) == int(control["pause_generation"]))


def _visible(job: dict | None) -> bool:
    """Whether a job is still in its client's view: its demand not withdrawn
    and its delivery neither acknowledged (even when its material has since
    gone, which is what an importing client's move looks like) nor expired.
    A failed job stays visible until its client removes it."""
    if job is None or not job["demand_id"]:
        return False
    projection = json.loads(job["projection"]) if job["projection"] else {}
    delivery = projection.get("delivery") or {}
    return (projection.get("phase") != "withdrawn" and delivery.get("acknowledged_at") is None
            and delivery.get("state") not in {"acknowledged", "expired"})


def _live(job: dict | None) -> bool:
    """Whether a job may still answer an identical live submission: visible
    and not failed."""
    projection = json.loads(job["projection"]) if job and job["projection"] else {}
    return _visible(job) and projection.get("phase") != "failed"


def _admit_mutation(client_id: str) -> None:
    if not external_mutation_limiter.allow(client_id):
        raise ExternalRefused("rate_limited", "Too many requests; slow down", status=429, retryable=True)


def _now() -> float:
    return time.time()


def _iso(value) -> str | None:
    if value is None:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace("+00:00", "Z")


def _settings():
    from core.config import get_settings

    return get_settings()


def _key(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExternalRefused("idempotency_key_required", "An idempotency key is required")
    value = value.strip()
    if len(value) > _KEY_LIMIT or any(ord(char) < 32 for char in value):
        raise ExternalRefused("invalid_request", f"An idempotency key is 1-{_KEY_LIMIT} printable characters")
    return value


async def _audit(db, event_type: str, message: str, job: dict, *, transfer_id=None, outcome=None, detail=None,
                 severity="info") -> None:
    """One credential-safe journal record, in the caller's transaction: the
    job and client IDs only -- never a source, token, path or another
    client's facts."""
    await record(db, JournalEvent(
        "transfer", f"transfer.{event_type}", severity, message, "external_job", job["id"],
        transfer_id=transfer_id, outcome=outcome,
        detail=f"client {job['client_id']}" + (f"; {detail}" if detail else ""),
        provenance=f"external_jobs:{job['id']}"))


@dataclass
class _Plan:
    """What a refresh found: the projection and, when the job's material is
    deliverable now, the delivery generation it would commit."""
    projection: dict
    material_key: str | None = None
    files: list | None = None
    import_root: str = ""
    retention_hours: int = 0
    unavailable_reason: str | None = None
    transfer_id: int | None = None


class ExternalClients:
    """Native external-client operations over one ``ApplicationService``."""

    def __init__(self, application):
        self.application = application

    @property
    def repository(self):
        return self.application.repository

    # ── capability negotiation ──────────────────────────────────────────────

    async def capabilities(self, principal: Principal) -> dict:
        client_id = client_of(principal)
        profiles = _settings().external_delivery.for_client(client_id)
        return {
            "protocol": PROTOCOL, "client_id": client_id,
            "operations": ["submit", "list", "inspect", "withdraw", "control", "delivery", "acknowledge"],
            "source_kinds": sorted({kind for profile in profiles for kind in profile.source_kinds}),
            "profiles": [profile.public() for profile in profiles],
            "phases": list(_PHASES),
            "delivery_states": ["unavailable", "partial", "available", "acknowledged", "expired"],
            "controls": {"kinds": ["pause", "resume"], "advisory": True},
            "polling": {"cursor": "change_seq", "max_page": MAX_PAGE, "authoritative": True},
            "events": {"supported": False},
            "idempotency": {"required": True, "scope": "client", "max_key_length": _KEY_LIMIT},
            "limits": {"active_jobs": MAX_ACTIVE_JOBS_PER_CLIENT, "mutations_per_minute":
                       external_mutation_limiter.per_peer_limit, "links": intake_owner.MAX_LINKS,
                       "torrent_bytes": intake_owner.MAX_TORRENT_BYTES, "nzb_bytes": intake_owner.MAX_NZB_BYTES},
            "refusal_codes": list(REFUSAL_CODES),
        }

    # ── submission ──────────────────────────────────────────────────────────

    async def submit(self, principal: Principal, body: dict) -> tuple[dict, bool]:
        client_id = client_of(principal)
        if not isinstance(body, dict):
            raise ExternalRefused("invalid_request", "The request body must be an object")
        key = _key(body.get("idempotency_key"))
        intent = self._intent(client_id, body, keyed=True)
        _admit_mutation(client_id)
        return await self._submit_keyed(principal, client_id, key, intent)

    async def submit_live(self, principal: Principal, body: dict) -> tuple[dict, bool]:
        """Submit for a client protocol whose caller supplies no request
        identity (the SABnzbd dialect's ``addfile``): a best-effort
        accommodation, never exactly-once.

        The idempotency key is derived here, under this owner, from the
        authenticated client and the exact submitted bytes, category and
        filename, and carries a sequence number. An identical submission
        answers the latest job of that identity only while that job is still
        live for the client -- its demand not withdrawn, not failed, its
        delivery neither acknowledged nor expired. Otherwise it is a new job
        under the next sequence number. Concurrent identical submissions
        serialize on the identity's submission lock, so they resolve to one
        job and one demand. In-process only: the HTTP contract still requires
        a caller key."""
        client_id = client_of(principal)
        if not isinstance(body, dict):
            raise ExternalRefused("invalid_request", "The request body must be an object")
        if "idempotency_key" in body:
            raise ExternalRefused("invalid_request", "A live submission derives its own identity")
        intent = self._intent(client_id, body, keyed=False)
        source = intent["source"]
        if source.kind != "nzb":
            raise ExternalRefused("unsupported_source", "Only an uploaded NZB is submitted without a caller key")
        identity = hashlib.sha256(json.dumps(
            {"v": 1, "source": source.identity, "category": intent["category"], "filename": source.filename},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        base = f"live:{identity}:"
        _admit_mutation(client_id)
        async with _lock(_submission_locks, (client_id, base)):
            async with get_db() as db:
                rows = await db.fetchall(
                    "SELECT * FROM external_jobs WHERE client_id=? AND substr(idempotency_key,1,?)=?",
                    (client_id, len(base), base))
            numbered = [(int(row["idempotency_key"][len(base):]), row) for row in rows
                        if row["idempotency_key"][len(base):].isdigit()]
            sequence, latest = max(numbered, key=lambda item: item[0], default=(0, None))
            if latest is not None and latest["intent_digest"] == intent["digest"]:
                if not latest["demand_id"]:
                    # A reservation an interruption left: completed under its
                    # own key, never admitted twice.
                    return await self._submit_keyed(principal, client_id, latest["idempotency_key"], intent)
                current = await self._refresh(latest["id"])
                if _live(current):
                    logger.info("external live submission replayed: client %s job %s", client_id, current["id"])
                    return await self._view(current), False
            return await self._submit_keyed(principal, client_id, f"{base}{sequence + 1}", intent)

    def _intent(self, client_id: str, body: dict, *, keyed: bool) -> dict:
        category = body.get("category")
        if not isinstance(category, str) or not category.strip():
            raise ExternalRefused("unknown_category", "A category is required")
        display_name = body.get("display_name") or ""
        reference = body.get("client_reference")
        if not isinstance(display_name, str) or len(display_name) > 256:
            raise ExternalRefused("invalid_request", "display_name is at most 256 characters")
        if reference is not None and (not isinstance(reference, str) or len(reference) > 256):
            raise ExternalRefused("invalid_request", "client_reference is at most 256 characters")
        allowed = {"category", "source", "display_name", "client_reference"} | ({"idempotency_key"} if keyed else set())
        unknown = set(body) - allowed
        if unknown:
            # Nothing else is part of the contract: no provider, executor,
            # priority, queue position or destination is ever accepted.
            raise ExternalRefused("invalid_request", f"Unknown field(s): {', '.join(sorted(unknown))}")
        profile = _settings().external_delivery.select(client_id, category)
        if profile is None:
            raise ExternalRefused("unknown_category", "This category selects no delivery profile for this client")
        try:
            source = intake_owner.normalize(body.get("source"))
        except intake_owner.IntakeRefused as exc:
            raise ExternalRefused(exc.code, exc.message, status=exc.status) from None
        if source.kind not in profile.source_kinds:
            raise ExternalRefused("source_kind_not_permitted", "This profile does not accept that source kind")
        digest = intake_owner.intent_digest(source, profile_id=profile.id, category_key=category_key(category),
                                            display_name=display_name.strip(), client_reference=reference)
        return {"source": source, "digest": digest, "category": category.strip(), "profile": profile,
                "display_name": display_name.strip(), "reference": reference}

    async def _submit_keyed(self, principal: Principal, client_id: str, key: str, intent: dict) -> tuple[dict, bool]:
        source, digest = intent["source"], intent["digest"]
        async with _lock(_submission_locks, (client_id, key)):
            async with get_db() as db:
                existing = await db.fetchone(
                    "SELECT * FROM external_jobs WHERE client_id=? AND idempotency_key=?", (client_id, key))
            if existing is not None:
                if existing["intent_digest"] != digest:
                    raise ExternalRefused("idempotency_conflict",
                                          "This idempotency key was used for a different submission", status=409)
                if existing["demand_id"]:
                    logger.info("external submission replayed: client %s job %s", client_id, existing["id"])
                    return await self._view(existing), False
                if await self.repository.demand(existing["reserved_demand_id"]) is not None:
                    return await self._view(await self._accept(existing)), True
                job = existing
            else:
                job = await self._reserve(principal, client_id, key, digest, source, intent["category"],
                                          intent["profile"], intent["display_name"], intent["reference"])
            await self._admit(principal, job, source)
            return await self._view(await self._accept(job)), True

    async def _reserve(self, principal, client_id, key, digest, source, category, profile, display_name, reference):
        now = _now()
        job_id = "job_" + uuid.uuid4().hex
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            active = await db.fetchone(
                """SELECT COUNT(*) AS n FROM external_jobs WHERE client_id=?
                   AND COALESCE(json_extract(projection,'$.settled'),0)=0""", (client_id,))
            if int(active["n"]) >= MAX_ACTIVE_JOBS_PER_CLIENT:
                await db.rollback()
                raise ExternalRefused("client_job_limit", "This client has too many unsettled jobs", status=429,
                                      retryable=True)
            await db.execute(
                """INSERT INTO external_jobs(id,client_id,account_id,credential_id,idempotency_key,intent_digest,
                       source_kind,source_summary,category,profile_id,profile_snapshot,display_name,client_reference,
                       reserved_demand_id,delivery_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, client_id, principal.account_id, principal.credential_id, key, digest, source.kind,
                 json.dumps(source.summary, sort_keys=True), category, profile.id,
                 json.dumps(profile.model_dump(), sort_keys=True), display_name, reference,
                 str(uuid.uuid4()), "dlv_" + uuid.uuid4().hex, now, now))
            await db.commit()
            return await db.fetchone("SELECT * FROM external_jobs WHERE id=?", (job_id,))

    async def _admit(self, principal, job: dict, source) -> None:
        """Admit through the existing submission seam with the job's reserved
        demand. A refusal admitted nothing: the reservation is forgotten. An
        unknown outcome keeps it, and a retry (or maintenance) completes it
        from the ledger -- never by admitting twice."""
        from application.service import AcquisitionScopeRequired, LocalNetworkConfirmationRequired
        from transfers.errors import Retryability, TransferError

        demand = SubmissionDemand(principal.account_id, principal.credential_id, id=job["reserved_demand_id"])
        application = self.application
        try:
            if source.kind == "links":
                await application.submit_links(list(source.links), selection_mode="all", demand=demand)
            elif source.kind == "magnet":
                await application.submit_magnet(source.magnet, source="external", selection_mode="all", demand=demand)
            elif source.kind == "nzb_link":
                await application.submit_nzb_link(source.url, source="external", demand=demand)
            elif source.kind == "nzb":
                # Staged and read by the existing NZB reader before admission.
                await application.submit_nzb(source.nzb, source.filename, source="external", demand=demand,
                                             validate=True)
            else:
                await application.submit_torrent(source.torrent, source.filename, source="external",
                                                 selection_mode="all", demand=demand)
        except LocalNetworkConfirmationRequired:
            await self._forget(job)
            raise ExternalRefused("local_network_confirmation_required",
                                  "This source is on the private network and needs an operator's confirmation") from None
        except AcquisitionScopeRequired:
            await self._forget(job)
            raise ExternalRefused("acquisition_scope_required",
                                  "A link names one item inside a collection; submit the item or the collection") from None
        except ValueError as exc:
            await self._forget(job)
            raise ExternalRefused("invalid_source", sanitize_exception(exc, max_length=200)) from None
        except TransferError as exc:
            if await self.repository.demand(job["reserved_demand_id"]) is None:
                await self._forget(job)
            status = int(getattr(exc, "status_code", 0) or 0)
            retryable = exc.error.retryability is not Retryability.NEVER
            code = "source_unavailable" if status >= 500 or retryable else "source_refused"
            raise ExternalRefused(code, "The source was refused: " + str(exc.error.category),
                                  status=503 if code == "source_unavailable" else 422, retryable=retryable) from None
        except Exception as exc:
            if await self.repository.demand(job["reserved_demand_id"]) is None and not isinstance(
                    exc, (asyncio.CancelledError,)):
                logger.warning("external submission not admitted (client %s): %s", job["client_id"],
                               sanitize_exception(exc, max_length=200))
                raise ExternalRefused("submission_outcome_unknown",
                                      "The submission could not be completed; retry with the same idempotency key",
                                      status=503, retryable=True) from None
            raise

    async def _forget(self, job: dict) -> None:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """DELETE FROM external_jobs WHERE id=? AND demand_id IS NULL AND NOT EXISTS
                   (SELECT 1 FROM transfer_demands d WHERE d.id=external_jobs.reserved_demand_id)""", (job["id"],))
            await db.commit()

    async def _accept(self, job: dict) -> dict:
        """Bind the job to its now-durable demand: accepted exactly once."""
        demand = await self.repository.demand(job["reserved_demand_id"])
        if demand is None:
            raise ExternalRefused("submission_outcome_unknown",
                                  "The submission is not yet registered; retry with the same idempotency key",
                                  status=503, retryable=True)
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("SELECT * FROM external_jobs WHERE id=?", (job["id"],))
            if row is not None and not row["demand_id"]:
                now = _now()
                await db.execute("UPDATE external_jobs SET demand_id=?, accepted_at=?, updated_at=? WHERE id=?",
                                 (row["reserved_demand_id"], now, now, row["id"]))
                await _audit(db, "external_job_accepted", "External job accepted", row,
                             transfer_id=int(demand["transfer_id"]), outcome="accepted")
            await db.commit()
        return await self._refresh(job["id"])

    # ── reads ───────────────────────────────────────────────────────────────

    async def _owned(self, principal: Principal, job_id: str) -> dict:
        client_id = client_of(principal)
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT * FROM external_jobs WHERE id=? AND client_id=? AND demand_id IS NOT NULL",
                (str(job_id or "")[:64], client_id))
        if row is None or str(row["account_id"]) not in set(await owned_account_ids(principal.account_id)):
            # Another client's job is indistinguishable from none.
            raise ExternalRefused("job_not_found", "Job not found", status=404)
        return row

    async def job(self, principal: Principal, job_id: str) -> dict:
        row = await self._owned(principal, job_id)
        return await self._view(await self._refresh(row["id"]))

    async def jobs(self, principal: Principal, *, after: int = 0, limit: int = DEFAULT_PAGE,
                   phase: str | None = None) -> dict:
        client_id = client_of(principal)
        owners = await owned_account_ids(principal.account_id)
        limit = max(1, min(int(limit), MAX_PAGE))
        if phase is not None and phase not in _PHASES:
            raise ExternalRefused("invalid_request", "Unknown phase")
        await self._refresh_unsettled(client_id=client_id)
        marks = ",".join("?" * len(owners))
        params: list = [client_id, *owners, int(after)]
        where = ""
        if phase is not None:
            where = " AND json_extract(projection,'$.phase')=?"
            params.append(phase)
        async with get_db() as db:
            rows = await db.fetchall(
                f"""SELECT * FROM external_jobs WHERE client_id=? AND account_id IN ({marks})
                    AND demand_id IS NOT NULL AND change_seq>?{where} ORDER BY change_seq LIMIT ?""",
                (*params, limit + 1))
            high = await db.fetchone(
                f"SELECT COALESCE(MAX(change_seq),0) AS n FROM external_jobs WHERE client_id=? "
                f"AND account_id IN ({marks}) AND demand_id IS NOT NULL", (client_id, *owners))
        more = len(rows) > limit
        rows = rows[:limit]
        return {"jobs": [await self._view(row) for row in rows],
                "next_cursor": int(rows[-1]["change_seq"]) if rows else int(after),
                "more": more, "high_water": int(high["n"])}

    async def visible(self, principal: Principal, *, settled: bool, category: str | None = None, start: int = 0,
                      limit: int = MAX_ACTIVE_JOBS_PER_CLIENT) -> dict:
        """The client's jobs still in its view (``_visible``), for a client
        protocol that lists a whole queue or history rather than polling by
        cursor: unfinished jobs (``settled=False``) oldest first, or finished
        ones -- completed or failed -- newest first, each finished job with
        its delivery (files only while available, re-verified now: a listing
        that offers a path to import from must not repeat a stale one).
        ``category``, when given, is matched exactly as the job recorded it.
        The same predicate as ``_visible``. Bounded by ``limit``."""
        client_id = client_of(principal)
        owners = await owned_account_ids(principal.account_id)
        await self._refresh_unsettled(client_id=client_id)
        phases = ("completed", "failed") if settled else ("accepted", "queued", "processing")
        marks = ",".join("?" * len(owners))
        params: list = [client_id, *owners, *phases]
        where = ""
        if category is not None:
            where = " AND category=?"
            params.append(category)
        order = "updated_at DESC, id" if settled else "created_at, id"
        async with get_db() as db:
            total = await db.fetchone(
                f"""SELECT COUNT(*) AS n FROM external_jobs WHERE client_id=? AND account_id IN ({marks})
                    AND demand_id IS NOT NULL AND json_extract(projection,'$.phase') IN (?,?{',?' if not settled else ''})
                    AND COALESCE(json_extract(projection,'$.delivery.state'),'') NOT IN ('acknowledged','expired')
                    AND json_extract(projection,'$.delivery.acknowledged_at') IS NULL
                    {where}""", tuple(params))
            rows = await db.fetchall(
                f"""SELECT * FROM external_jobs WHERE client_id=? AND account_id IN ({marks})
                    AND demand_id IS NOT NULL AND json_extract(projection,'$.phase') IN (?,?{',?' if not settled else ''})
                    AND COALESCE(json_extract(projection,'$.delivery.state'),'') NOT IN ('acknowledged','expired')
                    AND json_extract(projection,'$.delivery.acknowledged_at') IS NULL
                    {where} ORDER BY {order} LIMIT ? OFFSET ?""",
                (*params, max(0, int(limit)), max(0, int(start))))
        jobs = []
        for row in rows:
            if settled and json.loads(row["projection"] or "{}").get("phase") == "completed":
                row = await self._refresh(row["id"])
                view = await self._view(row)
                view["delivery"] = await self._delivery_view(row, include_files=True)
            else:
                view = await self._view(row)
            jobs.append(view)
        return {"jobs": jobs, "total": int(total["n"]), "globally_paused": await self.repository.globally_paused()}

    async def delivery(self, principal: Principal, job_id: str) -> dict:
        row = await self._owned(principal, job_id)
        row = await self._refresh(row["id"])
        return await self._delivery_view(row, include_files=True)

    # ── withdrawal ──────────────────────────────────────────────────────────

    async def withdraw(self, principal: Principal, job_id: str) -> dict:
        row = await self._owned(principal, job_id)
        _admit_mutation(row["client_id"])
        demand = await self.repository.demand(row["demand_id"])
        already = demand is not None and demand["withdrawn_at"] is not None
        if not already:
            await self.application.withdraw_demand((row["demand_id"],), by=f"external:{row['client_id']}")
            async with get_db() as db:
                await db.execute("BEGIN IMMEDIATE")
                await _audit(db, "external_job_withdrawn", "External job withdrawn by its client", row,
                             transfer_id=int(demand["transfer_id"]) if demand else None, outcome="withdrawn")
                await db.commit()
        await self._reconcile_controls(job_id=row["id"])
        view = await self._view(await self._refresh(row["id"]))
        return {**view, "already_withdrawn": already}

    # ── advisory pause / resume ─────────────────────────────────────────────

    async def control(self, principal: Principal, job_id: str, body: dict) -> dict:
        row = await self._owned(principal, job_id)
        if not isinstance(body, dict) or body.get("kind") not in {"pause", "resume"}:
            raise ExternalRefused("invalid_request", "kind must be pause or resume")
        request_key = _key(body.get("request_key"))
        if set(body) - {"kind", "request_key"}:
            raise ExternalRefused("invalid_request", "Only kind and request_key are accepted")
        _admit_mutation(row["client_id"])
        async with _lock(_control_locks, row["id"]):
            async with get_db() as db:
                existing = await db.fetchone("SELECT * FROM external_controls WHERE job_id=? AND request_key=?",
                                             (row["id"], request_key))
                count = await db.fetchone("SELECT COUNT(*) AS n FROM external_controls WHERE job_id=?", (row["id"],))
            if existing is not None:
                if existing["kind"] != body["kind"]:
                    raise ExternalRefused("control_conflict", "This request key was used for another control",
                                          status=409)
                if existing["disposition"] == "pending":
                    # An interrupted request, retried under its own identity:
                    # settled from what the pause owner shows, never re-applied.
                    existing = await self._recover_pending(existing)
                return {**await self._control_view(existing), "replayed": True}
            if int(count["n"]) >= MAX_CONTROLS_PER_JOB:
                raise ExternalRefused("control_limit", "This job has too many control requests", status=429)
            control_id = "ctl_" + uuid.uuid4().hex
            decision = await self._adjudicate(row, principal, request_key, body["kind"], control_id)
            control = await self._record_control(row, principal, request_key, body["kind"], decision, control_id)
        await self._refresh(row["id"])
        return {**await self._control_view(control), "replayed": False}

    async def _adjudicate(self, job: dict, principal: Principal, request_key: str, kind: str,
                          control_id: str) -> dict:
        """DebridPulse's decision on one advisory request, made and applied
        on the operator-control boundary so no operator control interleaves
        between what it read and what it did. A pause is applied as this
        control's own hold (``holder``); a resume releases only that exact
        hold, fenced by its generation at the pause owner
        (``TransferRepository.set_pause_and_fence``). Ownership that cannot be
        proven is never assumed: the request is declined.

        A decision that applies a transition is first recorded ``pending``
        (``_write_pending``), committed before the transition, so no
        interruption can leave a hold this owner does not know about."""
        demand = await self.repository.demand(job["demand_id"])
        transfer_id = int(demand["transfer_id"])
        if demand["withdrawn_at"] is not None:
            return {"disposition": "declined", "reason": "job_withdrawn", "transfer_id": transfer_id}
        holder = _holder(control_id)

        async def decide():
            status = await self._status(transfer_id)
            globally = await self.repository.globally_paused()
            hold = await self.repository.pause_hold(transfer_id)
            active = await self._active_pause(job["id"])
            mine = _owns(active, hold)
            paused = bool(hold and hold["paused"]) or status == "paused"
            observed = int(hold["generation"]) if hold else 0
            if status in _TERMINAL or status is None:
                return {"disposition": "declined", "reason": "already_settled"}
            if kind == "pause":
                if mine:
                    return {"disposition": "honored", "reason": "already_paused"}
                if globally:
                    return {"disposition": "declined", "reason": "globally_paused"}
                if paused:
                    return {"disposition": "declined", "reason": "paused_independently"}
                if {str(item["id"]) for item in await self.repository.outstanding_demand(transfer_id)} \
                        != {str(job["demand_id"])}:
                    return {"disposition": "declined", "reason": "shared_interest"}
                await self._write_pending(job, principal, request_key, kind, control_id, transfer_id, holder,
                                          observed)
                return {"disposition": "honored", "reason": "paused", "pending": True,
                        "apply": ("pause", holder)}
            if not mine:
                if paused:
                    return {"disposition": "declined",
                            "reason": "globally_paused" if globally else "paused_independently"}
                return {"disposition": "honored", "reason": "not_paused"}
            if globally:
                return {"disposition": "declined", "reason": "globally_paused"}
            await self._write_pending(job, principal, request_key, kind, control_id, transfer_id,
                                      active["pause_holder"], observed)
            return {"disposition": "honored", "reason": "resumed", "pending": True,
                    "apply": ("release", (active["pause_holder"], int(active["pause_generation"])))}

        from services.maintenance_gate import ApplicationMaintenanceActive
        from transfers.storage import StorageHealthError

        try:
            decision = await self.application.adjudicated_control(transfer_id, decide)
        except (ApplicationMaintenanceActive, StorageHealthError) as exc:  # refused before decide: nothing applied
            logger.info("external control not applied (job %s): %s", job["id"], type(exc).__name__)
            decision = {"disposition": "declined", "reason": "maintenance_active", "retryable": True}
        applied, hold = decision.pop("applied", ()), decision.pop("hold", None)
        if decision["reason"] == "paused":
            if hold and hold["paused"] and hold["holder"] == holder:
                decision.update(pause_holder=holder, pause_generation=int(hold["generation"]))
            else:
                decision.update(disposition="declined", reason="ownership_unproven")
        elif decision["reason"] == "resumed" and applied is None:
            # The pause owner refused the fenced release: not provably ours.
            decision.update(disposition="declined", reason="ownership_unproven")
        decision["transfer_id"] = transfer_id
        # Demand admitted between the check and the pause: the pause cannot
        # stand against another requester's interest.
        if (kind == "pause" and decision["disposition"] == "honored" and decision["reason"] == "paused"
                and {str(item["id"]) for item in await self.repository.outstanding_demand(transfer_id)}
                != {str(job["demand_id"])}):
            decision["revoke"] = "shared_interest_joined"
        return decision

    @staticmethod
    async def _write_pending(job: dict, principal: Principal, request_key: str, kind: str, control_id: str,
                             transfer_id: int, holder: str, observed: int) -> None:
        """The durable pre-application record: this request, the hold it is
        about to apply (pause) or release (resume), and the pause-intent
        generation observed before it -- committed before the transition."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """INSERT INTO external_controls(id,job_id,request_key,kind,credential_id,disposition,reason,
                       transfer_id,pause_holder,observed_generation,created_at)
                   VALUES(?,?,?,?,?,'pending','applying',?,?,?,?)""",
                (control_id, job["id"], request_key, kind, principal.credential_id, transfer_id, holder,
                 int(observed), _now()))
            await db.commit()

    async def _record_control(self, job: dict, principal: Principal, request_key: str, kind: str,
                              decision: dict, control_id: str) -> dict:
        """The decision's durable result: a pending record is completed in
        place; a decision that applied nothing is recorded new."""
        now = _now()
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            if decision["disposition"] == "honored" and decision["reason"] in {"paused", "resumed"}:
                # A new applied decision retires the job's earlier active one.
                await db.execute(
                    """UPDATE external_controls SET retired_at=?, retired_reason=?
                       WHERE job_id=? AND kind='pause' AND disposition='honored' AND retired_at IS NULL""",
                    (now, "resumed_by_client" if kind == "resume" else "superseded", job["id"]))
            if decision.get("pending"):
                await db.execute(
                    """UPDATE external_controls SET disposition=?, reason=?, pause_generation=?
                       WHERE id=? AND disposition='pending'""",
                    (decision["disposition"], decision["reason"], decision.get("pause_generation"), control_id))
            else:
                await db.execute(
                    """INSERT INTO external_controls(id,job_id,request_key,kind,credential_id,disposition,reason,
                           transfer_id,pause_holder,pause_generation,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (control_id, job["id"], request_key, kind, principal.credential_id, decision["disposition"],
                     decision["reason"], decision.get("transfer_id"), decision.get("pause_holder"),
                     decision.get("pause_generation"), now))
            await _audit(db, "external_control_decided", f"External {kind} request {decision['disposition']}", job,
                         transfer_id=decision.get("transfer_id"), outcome=decision["disposition"],
                         detail=decision["reason"])
            await db.commit()
            control = await db.fetchone("SELECT * FROM external_controls WHERE id=?", (control_id,))
        if decision.get("revoke"):
            await self._reconcile_controls(job_id=job["id"])
            async with get_db() as db:
                control = await db.fetchone("SELECT * FROM external_controls WHERE id=?", (control_id,))
        return control

    async def _recover_pending(self, control: dict) -> dict:
        """Settle a control an interruption left ``pending``, from what the
        pause owner shows -- read on the operator-control boundary, applying
        nothing (the caller holds the job's control lock):

        * pause, the intent is still exactly this control's hold -> it was
          applied and is still ours: ``honored`` with the hold's generation
          (an ordinary standing client hold from now on);
        * pause, the intent generation is still the one observed before the
          transition (and the transfer is not settled) -> nothing was ever
          written: ``declined / interrupted``;
        * resume, the hold it was releasing is still exactly the client's ->
          the release never happened: ``declined / interrupted``;
        * otherwise something wrote the intent since -- possibly this
          transition, possibly an operator after it -- so ownership cannot be
          proven: ``superseded / interrupted_unconfirmed``, and nothing is
          touched. A resume's released pause control is retired with it.

        Never re-applies, never creates a hold, never lifts one."""
        transfer_id = int(control["transfer_id"])

        async def decide():
            hold = await self.repository.pause_hold(transfer_id)
            status = await self._status(transfer_id)
            generation = int(hold["generation"]) if hold else 0
            untouched = generation == int(control["observed_generation"] or 0) and status not in _TERMINAL
            if control["kind"] == "pause":
                if hold is not None and hold["paused"] and hold["holder"] == control["pause_holder"]:
                    return {"outcome": ("honored", "paused", generation)}
                if untouched:
                    return {"outcome": ("declined", "interrupted", None)}
                return {"outcome": ("superseded", "interrupted_unconfirmed", None)}
            active = await self._active_pause(control["job_id"])
            if _owns(active, hold):
                return {"outcome": ("declined", "interrupted", None)}
            return {"outcome": ("superseded", "interrupted_unconfirmed", None)}

        decision = await self.application.adjudicated_control(transfer_id, decide)
        disposition, reason, generation = decision["outcome"]
        now = _now()
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            job = await db.fetchone("SELECT id, client_id FROM external_jobs WHERE id=?", (control["job_id"],))
            cursor = await db.execute(
                "UPDATE external_controls SET disposition=?, reason=?, pause_generation=? WHERE id=? "
                "AND disposition='pending'", (disposition, reason, generation, control["id"]))
            if cursor.rowcount:
                if control["kind"] == "pause" and disposition == "honored":
                    await db.execute(
                        """UPDATE external_controls SET retired_at=?, retired_reason='superseded'
                           WHERE job_id=? AND kind='pause' AND disposition='honored' AND retired_at IS NULL
                           AND id<>?""", (now, control["job_id"], control["id"]))
                if control["kind"] == "resume" and disposition == "superseded":
                    await db.execute(
                        """UPDATE external_controls SET retired_at=?, retired_reason='interrupted_unconfirmed'
                           WHERE job_id=? AND kind='pause' AND disposition='honored' AND retired_at IS NULL
                           AND pause_holder=?""", (now, control["job_id"], control["pause_holder"]))
                await _audit(db, "external_control_decided",
                             f"Interrupted external {control['kind']} request settled", job,
                             transfer_id=transfer_id, outcome=disposition, detail=reason)
            await db.commit()
            return await db.fetchone("SELECT * FROM external_controls WHERE id=?", (control["id"],))

    async def _recover_interrupted(self, limit: int = 200) -> int:
        """Every control an interruption left pending, each settled under its
        job's control lock (so an in-flight request is never mistaken for an
        interrupted one in this process)."""
        async with get_db() as db:
            rows = await db.fetchall("SELECT id, job_id FROM external_controls WHERE disposition='pending' "
                                     "ORDER BY created_at LIMIT ?", (int(limit),))
        settled = 0
        for row in rows:
            async with _lock(_control_locks, row["job_id"]):
                async with get_db() as db:
                    control = await db.fetchone("SELECT * FROM external_controls WHERE id=? "
                                                "AND disposition='pending'", (row["id"],))
                if control is None:
                    continue
                try:
                    await self._recover_pending(control)
                except Exception as exc:  # noqa: BLE001 - retried next pass
                    logger.warning("interrupted external control %s not settled yet: %s", row["id"],
                                   sanitize_exception(exc))
                    continue
            settled += 1
            await self._refresh(row["job_id"])
        return settled

    async def _active_pause(self, job_id: str) -> dict | None:
        async with get_db() as db:
            return await db.fetchone(
                """SELECT * FROM external_controls WHERE job_id=? AND kind='pause' AND disposition='honored'
                   AND retired_at IS NULL AND pause_holder IS NOT NULL ORDER BY created_at DESC LIMIT 1""",
                (job_id,))

    @staticmethod
    async def _status(transfer_id: int) -> str | None:
        async with get_db() as db:
            row = await db.fetchone("SELECT status FROM torrents WHERE id=?", (int(transfer_id),))
        return None if row is None else str(row["status"])

    async def _reconcile_controls(self, *, job_id: str | None = None, limit: int = 200) -> int:
        """Re-adjudicate every applied client pause still standing: one whose
        hold an operator, a global control or settlement replaced is retired
        without touching anything; one another requester's demand now shares,
        or whose job was withdrawn while others still need the work, is
        released -- only through the fenced release of that exact hold, so a
        hold that is no longer provably the client's is never lifted.
        Idempotent; derived from durable state alone."""
        async with get_db() as db:
            rows = await db.fetchall(
                f"""SELECT c.*, j.demand_id, j.client_id FROM external_controls c JOIN external_jobs j ON j.id=c.job_id
                    WHERE c.kind='pause' AND c.disposition='honored' AND c.retired_at IS NULL
                    AND c.pause_holder IS NOT NULL {'AND c.job_id=?' if job_id else ''}
                    ORDER BY c.created_at LIMIT ?""", ((job_id,) if job_id else ()) + (int(limit),))
        changed = 0
        for control in rows:
            transfer_id = int(control["transfer_id"])
            demand = await self.repository.demand(control["demand_id"])

            async def decide(control=control, demand=demand, transfer_id=transfer_id):
                hold = await self.repository.pause_hold(transfer_id)
                status = await self._status(transfer_id)
                if not _owns(control, hold):
                    return {"outcome": ("superseded", "operator_changed_pause")}
                if status in _TERMINAL or status is None:
                    return {"outcome": ("superseded", "job_settled")}
                outstanding = {str(item["id"]) for item in await self.repository.outstanding_demand(transfer_id)}
                withdrawn = demand is None or demand["withdrawn_at"] is not None
                if not withdrawn and outstanding == {str(control["demand_id"])}:
                    return {"outcome": None}
                outcome = ("revoked", "job_withdrawn" if withdrawn else "shared_interest_joined")
                if not outstanding:
                    return {"outcome": outcome}      # nobody needs the work: the core settles it
                return {"outcome": outcome, "apply": ("release", (control["pause_holder"],
                                                                  int(control["pause_generation"])))}

            try:
                decision = await self.application.adjudicated_control(transfer_id, decide)
            except Exception as exc:  # noqa: BLE001 - one control never stops the others; retried next pass
                logger.warning("external pause reconciliation deferred (transfer %s): %s", transfer_id,
                               sanitize_exception(exc))
                continue
            outcome = decision["outcome"]
            if "applied" in decision and decision["applied"] is None:
                # The fenced release was refused: no longer provably this
                # client's hold (or a global pause holds it). Never lifted.
                outcome = ("superseded", "operator_changed_pause")
            if outcome is None:
                continue
            disposition, reason = outcome
            async with get_db() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """UPDATE external_controls SET disposition=CASE WHEN ?='revoked' THEN 'revoked'
                           ELSE disposition END, retired_at=?, retired_reason=? WHERE id=? AND retired_at IS NULL""",
                    (disposition, _now(), reason, control["id"]))
                if cursor.rowcount:
                    job = {"id": control["job_id"], "client_id": control["client_id"]}
                    await _audit(db, "external_control_decided", "External pause released", job,
                                 transfer_id=transfer_id, outcome=disposition, detail=reason)
                await db.commit()
            changed += 1
            await self._refresh(control["job_id"])
        return changed

    # ── delivery acknowledgment ─────────────────────────────────────────────

    async def acknowledge(self, principal: Principal, job_id: str, body: dict) -> dict:
        row = await self._owned(principal, job_id)
        if not isinstance(body, dict):
            raise ExternalRefused("invalid_request", "The request body must be an object")
        delivery_id, generation = body.get("delivery_id"), body.get("generation")
        if not isinstance(delivery_id, str) or not isinstance(generation, int) or isinstance(generation, bool):
            raise ExternalRefused("invalid_request", "delivery_id and an integer generation are required")
        _admit_mutation(row["client_id"])
        if delivery_id != row["delivery_id"]:
            raise ExternalRefused("delivery_mismatch", "That delivery is not this job's", status=409)
        already = False
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            target = await db.fetchone("SELECT * FROM external_deliveries WHERE job_id=? AND generation=?",
                                       (row["id"], generation))
            current = await db.fetchone(
                "SELECT MAX(generation) AS g FROM external_deliveries WHERE job_id=?", (row["id"],))
            if target is None:
                await db.rollback()
                raise ExternalRefused("unknown_generation", "No such delivery generation", status=409)
            if target["superseded_at"] is not None:
                await db.rollback()
                raise ExternalRefused("stale_generation", "A newer delivery generation replaced this one", status=409,
                                      current_generation=int(current["g"]))
            if target["acknowledged_at"] is not None:
                already = True
                await db.rollback()
            else:
                await db.execute(
                    "UPDATE external_deliveries SET acknowledged_at=?, acknowledged_by=? WHERE job_id=? AND generation=?",
                    (_now(), principal.credential_id, row["id"], generation))
                await _audit(db, "external_delivery_acknowledged", "External delivery acknowledged", row,
                             outcome="acknowledged", detail=f"generation {generation}")
                await db.commit()
        view = await self._delivery_view(await self._refresh(row["id"]), include_files=False)
        return {**view, "already_acknowledged": already}

    # ── projection ──────────────────────────────────────────────────────────

    async def _refresh(self, job_id: str) -> dict:
        """Recompute one accepted job's projection from authoritative state,
        commit a delivery generation its material now earns, and record a
        new revision when anything public changed."""
        async with get_db() as db:
            job = await db.fetchone("SELECT * FROM external_jobs WHERE id=?", (job_id,))
        if job is None or not job["demand_id"]:
            return job
        plan = await self._plan(job)
        now = _now()
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            job = await db.fetchone("SELECT * FROM external_jobs WHERE id=?", (job_id,))
            current = await db.fetchone(
                "SELECT * FROM external_deliveries WHERE job_id=? ORDER BY generation DESC LIMIT 1", (job_id,))
            if plan.material_key is not None and (current is None or current["material_key"] != plan.material_key):
                generation = (int(current["generation"]) if current else 0) + 1
                if current is not None:
                    await db.execute("UPDATE external_deliveries SET superseded_at=? WHERE job_id=? AND generation=?",
                                     (now, job_id, current["generation"]))
                await db.execute(
                    """INSERT INTO external_deliveries(job_id,generation,material_key,files,import_root,committed_at,
                           expires_at) VALUES(?,?,?,?,?,?,?)""",
                    (job_id, generation, plan.material_key, json.dumps(plan.files, sort_keys=True), plan.import_root,
                     now, now + plan.retention_hours * 3600))
                await _audit(db, "external_delivery_committed", "External delivery committed", job,
                             transfer_id=plan.transfer_id, outcome="committed", detail=f"generation {generation}")
                current = await db.fetchone(
                    "SELECT * FROM external_deliveries WHERE job_id=? AND generation=?", (job_id, generation))
            elif current is not None:
                reason = plan.unavailable_reason if plan.material_key is None else None
                # Not yet verifiable (executors starting) is no observation:
                # the committed generation keeps its last observed state.
                if reason != "verifying_material" and (current["unavailable_reason"] or None) != reason:
                    await db.execute(
                        "UPDATE external_deliveries SET unavailable_reason=? WHERE job_id=? AND generation=?",
                        (reason, job_id, current["generation"]))
                    if reason:
                        await _audit(db, "external_delivery_unavailable", "External delivery became unavailable",
                                     job, transfer_id=plan.transfer_id, outcome="unavailable", detail=reason,
                                     severity="warning")
                if current["expired_at"] is None and now >= float(current["expires_at"]):
                    await db.execute("UPDATE external_deliveries SET expired_at=? WHERE job_id=? AND generation=?",
                                     (now, job_id, current["generation"]))
                    await _audit(db, "external_delivery_expired", "External delivery retention expired", job,
                                 transfer_id=plan.transfer_id, outcome="expired",
                                 detail=f"generation {current['generation']}")
                current = await db.fetchone(
                    "SELECT * FROM external_deliveries WHERE job_id=? AND generation=?", (job_id, current["generation"]))
            projection = self._finish(plan.projection, current, job, now)
            stored = json.loads(job["projection"]) if job["projection"] else None
            if stored != projection:
                seq = await db.fetchone("SELECT COALESCE(MAX(change_seq),0)+1 AS n FROM external_jobs")
                await db.execute(
                    """UPDATE external_jobs SET projection=?, revision=revision+1, change_seq=?, updated_at=?,
                           refreshed_at=? WHERE id=?""",
                    (json.dumps(projection, sort_keys=True), int(seq["n"]), now, now, job_id))
            else:
                await db.execute("UPDATE external_jobs SET refreshed_at=? WHERE id=?", (now, job_id))
            await db.commit()
            return await db.fetchone("SELECT * FROM external_jobs WHERE id=?", (job_id,))

    @staticmethod
    def _finish(projection: dict, current: dict | None, job: dict, now: float) -> dict:
        """Fold the delivery generation into the phase."""
        projection = dict(projection)
        unverifiable = projection.get("_delivery_reason") == "verifying_material"
        delivery = {"state": "unavailable", "reason": projection.pop("_delivery_reason", "not_ready"),
                    "generation": None}
        if projection.pop("_partial", None) is not None and projection["phase"] in {"processing", "queued"}:
            delivery = {"state": "partial", "reason": "members_completing", "generation": None}
        if current is not None:
            generation = int(current["generation"])
            if current["expired_at"] is not None or now >= float(current["expires_at"]):
                state, reason = "expired", "retention_expired"
            elif current["unavailable_reason"]:
                state, reason = "unavailable", str(current["unavailable_reason"])
            elif current["acknowledged_at"] is not None:
                state, reason = "acknowledged", None
            else:
                state, reason = "available", None
            if unverifiable and state in {"available", "acknowledged"}:
                # Executors are starting: nothing is claimed until re-verified.
                state, reason = "unavailable", "verifying_material"
            delivery = {"state": state, "reason": reason, "generation": generation,
                        "committed_at": _iso(current["committed_at"]), "expires_at": _iso(current["expires_at"]),
                        "acknowledged_at": _iso(current["acknowledged_at"])}
            if projection["phase"] not in {"withdrawn"}:
                projection["phase"], projection["reason"], projection["actionability"] = "completed", "delivered", "none"
        projection["delivery"] = delivery
        settled = projection["phase"] == "withdrawn" or delivery["state"] in {"acknowledged", "expired"} or (
            projection["phase"] == "failed" and projection["reason"] in {"cancelled", "deleted"})
        projection["settled"] = bool(settled)
        return projection

    async def _plan(self, job: dict) -> _Plan:
        demand = await self.repository.demand(job["demand_id"])
        if demand is None:
            return _Plan({"phase": "failed", "reason": "demand_missing", "actionability": "operator",
                          "progress": None})
        transfer_id = int(demand["transfer_id"])
        async with get_db() as db:
            transfer = await db.fetchone(
                "SELECT id, status, progress, extraction_status, normalized_error FROM torrents WHERE id=?",
                (transfer_id,))
        status = str(transfer["status"]) if transfer else "deleted"
        plan = _Plan({"phase": "processing", "reason": status, "actionability": "wait", "progress": None},
                     transfer_id=transfer_id)
        projection = plan.projection
        if demand["withdrawn_at"] is not None:
            by = str(demand["withdrawn_by"] or "")
            projection.update(phase="withdrawn", actionability="none", reason=(
                "withdrawn_by_client" if by.startswith("external:") else
                "withdrawn_by_administrator" if by in {"account_disabled", "account_deleted"} else "withdrawn"))
            return plan
        members = await self._members(transfer_id, status)
        if members["total"]:
            projection["progress"] = {"basis": "members", "completed_members": members["completed"],
                                      "total_members": members["total"],
                                      "percent": None if transfer is None or status == "consolidated"
                                      else round(float(transfer["progress"] or 0), 1)}
        if status in {"completed", "consolidated"}:
            await self._deliverable(job, transfer_id, status, members, plan)
            return plan
        if members["completed"] and members["completed"] < members["total"]:
            projection["_partial"] = True
        mapping = {
            "pending": ("accepted", "awaiting_resolution", "wait"), "queued": ("queued", "queued", "wait"),
            "ready": ("queued", "queued", "wait"), "processing": ("processing", "resolving", "wait"),
            "input_required": ("processing", "input_required", "operator"),
            "downloading": ("processing", "downloading", "wait"), "verifying": ("processing", "verifying", "wait"),
            "extracting": ("processing", "finalizing", "wait"), "paused": ("processing", "paused", "wait"),
            "error": ("failed", "acquisition_failed", "operator"), "cancelled": ("failed", "cancelled", "resubmit"),
            "deleted": ("failed", "deleted", "resubmit"),
        }
        phase, reason, actionability = mapping.get(status, ("processing", "processing", "wait"))
        projection.update(phase=phase, reason=reason, actionability=actionability)
        if status == "paused":
            active = await self._active_pause(job["id"])
            hold = await self.repository.pause_hold(transfer_id)
            projection["paused_by"] = (
                "client" if _owns(active, hold)
                else "administrator" if await self.repository.globally_paused() else "operator")
        if status == "error" and transfer and transfer["normalized_error"]:
            try:
                error = json.loads(transfer["normalized_error"])
                projection["error"] = {"category": str(error.get("category") or ""),
                                       "retryability": str(error.get("retryability") or "")}
            except (TypeError, ValueError):
                pass
        return plan

    async def _members(self, transfer_id: int, status: str) -> dict:
        """The artifacts that fulfil the job's transfer: its own canonical
        members (unless it was consolidated away) and every canonical artifact
        of another transfer one of its requests was consolidated into, or is
        terminally associated with."""
        from transfers._repository_base import canonical_artifact_membership_sql

        async with get_db() as db:
            own = [] if status == "consolidated" else await db.fetchall(
                f"SELECT f.id, f.torrent_id, f.status FROM download_files f WHERE f.torrent_id=? "
                f"AND {canonical_artifact_membership_sql('f')}", (transfer_id,))
            foreign = await db.fetchall(
                """SELECT DISTINCT f.id, f.torrent_id, f.status, t.status AS owner_status FROM (
                       SELECT canonical_artifact_id AS id FROM artifact_consolidations WHERE source_transfer_id=:t
                       UNION SELECT r.equivalence_target_artifact_id FROM transfer_requests r
                           WHERE r.transfer_id=:t AND r.equivalence_disposition='unverified'
                           AND r.equivalence_target_artifact_id IS NOT NULL) m
                   JOIN download_files f ON f.id=m.id JOIN torrents t ON t.id=f.torrent_id
                   WHERE f.torrent_id<>:t""", {"t": int(transfer_id)})
        if status == "completed":
            own = [row for row in own if row["status"] == "completed"]
        rows = [*own, *foreign]
        return {"rows": rows, "total": len(rows), "completed": sum(row["status"] == "completed" for row in rows),
                "foreign": foreign}

    async def _deliverable(self, job: dict, transfer_id: int, status: str, members: dict, plan: _Plan) -> None:
        projection = plan.projection

        def blocked(reason: str, *, phase="failed", actionability="operator"):
            projection.update(phase=phase, reason=reason, actionability=actionability, _delivery_reason=reason)
            plan.unavailable_reason = reason

        if not members["rows"]:
            return blocked("nothing_delivered")
        if any(row["status"] != "completed" or row["owner_status"] not in {"completed"}
               for row in members["foreign"]):
            failed = any(row["owner_status"] in {"error", "cancelled", "deleted"} for row in members["foreign"])
            return blocked("shared_transfer_failed" if failed else "served_by_shared_transfer",
                           phase="failed" if failed else "processing",
                           actionability="operator" if failed else "wait")
        settings = _settings()
        profile = settings.external_delivery.by_id(job["profile_id"])
        if profile is None or job["client_id"] not in profile.clients:
            return blocked("profile_unavailable")
        if not confined_to(settings.download_folder, profile.local_root):
            return blocked("profile_root_outside_downloads")
        owners = {int(transfer_id) if status == "completed" else None,
                  *(int(row["torrent_id"]) for row in members["foreign"])} - {None}
        async with get_db() as db:
            marks = ",".join("?" * len(owners))
            finalization = {int(row["id"]): str(row["extraction_status"] or "") for row in await db.fetchall(
                f"SELECT id, extraction_status FROM torrents WHERE id IN ({marks})", tuple(owners))}
            generations = {int(row["artifact_id"]): int(row["material_generation"]) for row in await db.fetchall(
                f"SELECT artifact_id, material_generation FROM artifact_material_state WHERE artifact_id IN "
                f"({','.join('?' * len(members['rows']))})", tuple(int(row["id"]) for row in members["rows"]))}
        if "error" in finalization.values():
            return blocked("finalization_failed")
        if self.application.integrations_starting():
            # Executors are still starting: material is not yet verifiable,
            # which is not the same as missing.
            return blocked("verifying_material", phase="processing", actionability="wait")
        entries: list[tuple[dict | None, str | None]] = []
        for owner in sorted(owners):
            rows = [row for row in members["rows"] if int(row["torrent_id"]) == owner]
            if finalization[owner] != "completed":
                entries.extend((row, None) for row in rows)
                continue
            # Post-processing replaced the delivered material: what the owner
            # consists of is the final file set its post-processor recorded.
            outputs = await self._finalized_outputs(owner)
            if outputs is None:
                return blocked("extraction_outputs_unrecorded")
            if owner != int(transfer_id) and not await self._covers_owner(owner, rows):
                # Extracted files cannot be attributed to individual artifacts:
                # another transfer's finalized set is delivered only whole.
                return blocked("extraction_outputs_unattributable")
            entries.extend((None, path) for path in outputs)
        files = await self._verified_files(entries, profile, generations)
        if isinstance(files, str):
            return blocked(files)
        plan.files = files
        plan.import_root = profile.effective_import_root
        plan.retention_hours = profile.retention_hours
        plan.material_key = "sha256:" + hashlib.sha256(json.dumps(
            {"local_root": profile.local_root, "import_root": plan.import_root,
             "files": [[item["artifact_id"], item["material_generation"], item.get("modified_ns"),
                        item["relative_path"], item["size"]] for item in files]}, sort_keys=True).encode()).hexdigest()
        projection.update(phase="completed", reason="delivered", actionability="none")

    @staticmethod
    async def _finalized_outputs(transfer_id: int) -> list[str] | None:
        """The final file set a finished post-processing recorded for the
        transfer, or ``None`` when a successful post-processor recorded none
        (an outcome written before outputs were recorded)."""
        async with get_db() as db:
            rows = await db.fetchall("SELECT outcome FROM postprocess_attempts WHERE transfer_id=? "
                                     "AND state='finished'", (int(transfer_id),))
        outputs: set[str] = set()
        recorded = False
        for row in rows:
            outcome = json.loads(row["outcome"]) if row["outcome"] else {}
            if outcome.get("kind") != "success":
                continue
            if not isinstance(outcome.get("outputs"), list):
                return None
            recorded = True
            outputs.update(str(item) for item in outcome["outputs"])
        return sorted(outputs) if recorded else None

    @staticmethod
    async def _covers_owner(owner: int, rows) -> bool:
        from transfers._repository_base import canonical_artifact_membership_sql

        async with get_db() as db:
            members = await db.fetchall(
                f"SELECT f.id FROM download_files f WHERE f.torrent_id=? AND f.status='completed' "
                f"AND {canonical_artifact_membership_sql('f')}", (int(owner),))
        return {int(row["id"]) for row in members} <= {int(row["id"]) for row in rows}

    async def _verified_files(self, entries, profile: DeliveryProfile, generations) -> list | str:
        """Every delivered file verified now beneath the profile's root: an
        artifact's paths through the engine's own delivery-time verification,
        a recorded post-processing output by its own lstat (a regular file,
        no link anywhere beneath the root)."""
        engine = self.application.engine
        root = posixpath.normpath(profile.local_root)
        files: dict[str, dict] = {}
        for row, recorded in sorted(entries, key=lambda item: (int(item[0]["id"]) if item[0] else 0,
                                                                item[1] or "")):
            if row is not None:
                found = await self.repository.artifacts(int(row["torrent_id"]), artifact_id=int(row["id"]))
                paths = await engine.delivered_material(found[0]) if found else None
                if not paths:
                    return "material_missing"
            else:
                paths = (recorded,)
            for path in paths:
                normalized = posixpath.normpath(str(path))
                if normalized == root or not normalized.startswith(root.rstrip("/") + "/"):
                    return "outside_export_root"
                relative = normalized[len(root.rstrip("/")) + 1:]
                if member_payload(root, relative) is None:
                    return "material_missing"
                try:
                    info = os.lstat(normalized)
                except OSError:
                    return "material_missing"
                files[relative] = {"artifact_id": int(row["id"]) if row is not None else None,
                                   "material_generation": generations.get(int(row["id"]), 0) if row is not None else 0,
                                   "relative_path": relative, "name": posixpath.basename(relative),
                                   "size": info.st_size,
                                   "import_path": posixpath.join(profile.effective_import_root, relative)}
                if row is None:
                    files[relative]["modified_ns"] = info.st_mtime_ns
        if not files:
            return "material_missing"
        return [files[key] for key in sorted(files)]

    async def _refresh_unsettled(self, *, client_id: str | None = None, budget: int = REFRESH_BUDGET) -> int:
        """Refresh a bounded set of unsettled jobs, least recently refreshed
        first; an available delivery is re-verified at most every
        ``DELIVERY_RECHECK_SECONDS``."""
        now = _now()
        async with get_db() as db:
            rows = await db.fetchall(
                f"""SELECT id FROM external_jobs WHERE demand_id IS NOT NULL
                    AND COALESCE(json_extract(projection,'$.settled'),0)=0
                    {'AND client_id=?' if client_id else ''}
                    AND (COALESCE(json_extract(projection,'$.phase'),'')<>'completed' OR refreshed_at<?)
                    ORDER BY refreshed_at LIMIT ?""",
                ((client_id,) if client_id else ()) + (now - DELIVERY_RECHECK_SECONDS, int(budget)))
        for row in rows:
            try:
                await self._refresh(row["id"])
            except Exception as exc:  # noqa: BLE001 - one job never stops the others
                logger.warning("external job %s refresh failed: %s", row["id"], sanitize_exception(exc))
        return len(rows)

    async def maintain(self) -> dict:
        """The maintenance pass (``core.scheduler``): complete accepted
        reservations from the ledger, forget never-admitted ones, re-adjudicate
        standing pauses, refresh a bounded set of unsettled jobs."""
        now = _now()
        async with get_db() as db:
            pending = await db.fetchall(
                """SELECT j.* FROM external_jobs j JOIN transfer_demands d ON d.id=j.reserved_demand_id
                   WHERE j.demand_id IS NULL LIMIT 200""")
        for job in pending:
            await self._accept(job)
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """DELETE FROM external_jobs WHERE demand_id IS NULL AND created_at<?
                   AND NOT EXISTS (SELECT 1 FROM transfer_demands d WHERE d.id=external_jobs.reserved_demand_id)""",
                (now - RESERVATION_TTL_SECONDS,))
            forgotten = cursor.rowcount
            await db.commit()
        recovered = await self._recover_interrupted()
        released = await self._reconcile_controls()
        refreshed = await self._refresh_unsettled()
        return {"accepted": len(pending), "forgotten": forgotten, "recovered": recovered, "released": released,
                "refreshed": refreshed}

    # ── views ───────────────────────────────────────────────────────────────

    async def _view(self, job: dict) -> dict:
        projection = json.loads(job["projection"]) if job["projection"] else {}
        delivery = dict(projection.get("delivery") or {"state": "unavailable", "reason": "not_ready",
                                                        "generation": None})
        delivery["delivery_id"] = job["delivery_id"]
        control = await self._latest_control(job["id"])
        return {
            "id": job["id"], "revision": int(job["revision"]), "cursor": int(job["change_seq"]),
            "submitted_at": _iso(job["created_at"]), "accepted_at": _iso(job["accepted_at"]),
            "updated_at": _iso(job["updated_at"]), "category": job["category"], "profile_id": job["profile_id"],
            "display_name": job["display_name"], "client_reference": job["client_reference"],
            "source": json.loads(job["source_summary"]),
            "phase": projection.get("phase", "accepted"), "reason": projection.get("reason"),
            "actionability": projection.get("actionability", "wait"),
            "paused_by": projection.get("paused_by"), "error": projection.get("error"),
            "progress": projection.get("progress"), "delivery": delivery, "control": control,
        }

    async def _latest_control(self, job_id: str) -> dict | None:
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT * FROM external_controls WHERE job_id=? ORDER BY created_at DESC, id DESC LIMIT 1", (job_id,))
        return None if row is None else await self._control_view(row)

    async def _control_view(self, row: dict) -> dict:
        """The decision as recorded, and the effect observed NOW at the pause
        owner: the job's own client hold (``pausing``/``paused``) only while
        it still names that exact hold; otherwise the transfer's actual state
        -- paused by an operator or global control (``held_by_operator``) or
        not paused (``running``)."""
        effective = None
        if row["transfer_id"] is not None:
            transfer_id = int(row["transfer_id"])
            hold = await self.repository.pause_hold(transfer_id)
            status = await self._status(transfer_id)
            if _owns(await self._active_pause(row["job_id"]), hold):
                effective = "paused" if status == "paused" else "pausing"
            elif (hold is not None and hold["paused"]) or status == "paused":
                effective = "held_by_operator"
            else:
                effective = "not_paused"
        return {"request_key": row["request_key"], "kind": row["kind"],
                "disposition": "revoked" if row["disposition"] == "revoked" else row["disposition"],
                "reason": row["reason"], "requested_at": _iso(row["created_at"]),
                "retired_at": _iso(row["retired_at"]), "retired_reason": row["retired_reason"],
                "effective": effective}

    async def _delivery_view(self, job: dict, *, include_files: bool) -> dict:
        view = (await self._view(job))["delivery"]
        result = {"job_id": job["id"], **view}
        if include_files and view.get("generation") is not None and view["state"] in {"available", "acknowledged"}:
            async with get_db() as db:
                row = await db.fetchone("SELECT files, import_root FROM external_deliveries WHERE job_id=? "
                                        "AND generation=?", (job["id"], view["generation"]))
            files = json.loads(row["files"])
            result.update(
                representation="shared_path", import_root=row["import_root"], complete=True,
                files=[{"name": item["name"], "relative_path": item["relative_path"],
                        "import_path": item["import_path"], "size": item["size"]} for item in files],
                total_bytes=sum(int(item["size"]) for item in files))
        return result
