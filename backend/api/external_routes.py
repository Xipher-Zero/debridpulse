"""The native external-client API, protocol v1 (``external.service``), and
its Admin-owned delivery profile catalog.

Every client route is ``external.client`` (``auth.permissions``) and acts on
the calling token's own client binding only; another client's job is a 404.
Refusals carry a stable machine-readable ``code`` in ``detail``.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from application.dependencies import get_application
from application.service import ApplicationService
from core.config import config_write_lock, get_settings, journal_configuration_change, load_settings, \
    save_settings, apply_settings
from db.database import get_db
from external.profiles import DeliveryProfile, ExternalDeliverySettings, confined_to
from external.service import ExternalClients, ExternalRefused

router = APIRouter()
PREFIX = "/external/v1"


def _refused(exc: ExternalRefused) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.body())


async def _call(coroutine):
    try:
        return await coroutine
    except ExternalRefused as exc:
        raise _refused(exc) from None


def _principal(request: Request):
    return getattr(request.state, "principal", None)


@router.get(PREFIX + "/capabilities")
async def external_capabilities(request: Request, application: ApplicationService = Depends(get_application)):
    return await _call(ExternalClients(application).capabilities(_principal(request)))


@router.post(PREFIX + "/jobs")
async def submit_external_job(request: Request, body: Annotated[dict, Body()],
                              application: ApplicationService = Depends(get_application)):
    """Submit one source under a client idempotency key. ``201``: a new job;
    ``200``: the same key and intent again, answered with the same job."""
    job, created = await _call(ExternalClients(application).submit(_principal(request), body))
    return JSONResponse(status_code=201 if created else 200, content=job)


@router.get(PREFIX + "/jobs")
async def list_external_jobs(request: Request, application: ApplicationService = Depends(get_application),
                             after: Annotated[int, Query(ge=0)] = 0,
                             limit: Annotated[int, Query(ge=1, le=200)] = 100,
                             phase: Annotated[str | None, Query(max_length=32)] = None):
    """The client's own jobs whose ``cursor`` is greater than ``after``, in
    cursor order: from 0 a full listing, from a saved ``next_cursor`` exactly
    the jobs that changed since. Polling is authoritative."""
    return await _call(ExternalClients(application).jobs(_principal(request), after=after, limit=limit, phase=phase))


@router.get(PREFIX + "/jobs/{job_id}")
async def get_external_job(job_id: str, request: Request, application: ApplicationService = Depends(get_application)):
    return await _call(ExternalClients(application).job(_principal(request), job_id))


@router.post(PREFIX + "/jobs/{job_id}/withdraw")
async def withdraw_external_job(job_id: str, request: Request,
                                application: ApplicationService = Depends(get_application)):
    """Withdraw this job's own demand. Idempotent. Shared work continues for
    anyone else who still needs it; nothing is deleted."""
    return await _call(ExternalClients(application).withdraw(_principal(request), job_id))


@router.post(PREFIX + "/jobs/{job_id}/controls")
async def request_external_control(job_id: str, request: Request, body: Annotated[dict, Body()],
                                   application: ApplicationService = Depends(get_application)):
    """An advisory pause or resume request: DebridPulse's decision
    (``honored``/``declined``/``revoked``), its reason and the observed
    effect. Idempotent per ``request_key``."""
    return await _call(ExternalClients(application).control(_principal(request), job_id, body))


@router.get(PREFIX + "/jobs/{job_id}/delivery")
async def get_external_delivery(job_id: str, request: Request,
                                application: ApplicationService = Depends(get_application)):
    return await _call(ExternalClients(application).delivery(_principal(request), job_id))


@router.post(PREFIX + "/jobs/{job_id}/delivery/ack")
async def acknowledge_external_delivery(job_id: str, request: Request, body: Annotated[dict, Body()],
                                        application: ApplicationService = Depends(get_application)):
    """Acknowledge one exact delivery generation: this job's interest in it
    is satisfied. Deletes nothing; a stale generation is a conflict."""
    return await _call(ExternalClients(application).acknowledge(_principal(request), job_id, body))


# ── the delivery profile catalog (Admin) ────────────────────────────────────

class ProfilesUpdate(BaseModel):
    profiles: list[DeliveryProfile] = Field(default_factory=list)
    # The catalog revision this write was based on.
    expected_revision: int = Field(ge=0)


@router.get("/external/profiles")
async def get_external_profiles():
    return get_settings().external_delivery.model_dump()


def _narrowing(before: DeliveryProfile, after: DeliveryProfile | None) -> tuple[str, set[str] | None] | None:
    """What a catalog write takes away from a profile's existing jobs: the
    whole profile (deleted, or its roots moved -- every job's delivery is
    re-verified against them) or some of its clients (only those clients'
    jobs lose their grant). Categories, source kinds, retention and added
    clients govern only what is admitted or committed next."""
    if after is None:
        return "deleted", None
    if after.local_root != before.local_root or after.effective_import_root != before.effective_import_root:
        return "roots_changed", None
    removed = set(before.clients) - set(after.clients)
    return ("clients_removed", removed) if removed else None


@router.put("/external/profiles")
async def put_external_profiles(body: ProfilesUpdate, application: ApplicationService = Depends(get_application)):
    """Replace the delivery profile catalog. A write based on a stale
    revision is refused (409). A profile's ``local_root`` must be the
    download folder or beneath it.

    A write that would take a profile away from jobs that still depend on it
    -- deleting it, moving its roots, or removing a client -- is refused
    (``409 profile_in_use``) with the affected job count, while any such job
    is unsettled (``ExternalClients.profile_dependents``). The count and the
    catalog replacement happen inside one database write transaction, the
    same one an external reservation takes, so no admission can slip between
    them."""
    async with application.application_operation():
        async with config_write_lock():
            previous = get_settings()
            current = load_settings()
            if current.external_delivery.revision != body.expected_revision:
                raise HTTPException(409, {"code": "stale_revision", "revision": current.external_delivery.revision,
                                          "message": "The profile catalog changed; reload it and try again"})
            for profile in body.profiles:
                if not confined_to(current.download_folder, profile.local_root):
                    raise HTTPException(422, {"code": "invalid_profile", "profile": profile.id,
                                              "message": "local_root must be the download folder or beneath it"})
            try:
                catalog = ExternalDeliverySettings(profiles=body.profiles,
                                                   revision=current.external_delivery.revision + 1)
            except ValidationError as exc:
                raise HTTPException(422, {"code": "invalid_profile",
                                          "message": "; ".join(str(item.get("msg")) for item in exc.errors())})
            replaced = {profile.id: profile for profile in catalog.profiles}
            async with get_db() as db:
                await db.execute("BEGIN IMMEDIATE")
                in_use = []
                for before in current.external_delivery.profiles:
                    narrowing = _narrowing(before, replaced.get(before.id))
                    if narrowing is None:
                        continue
                    count = await ExternalClients.profile_dependents(db, before.id, narrowing[1])
                    if count:
                        in_use.append({"id": before.id, "change": narrowing[0], "affected_jobs": count})
                if in_use:
                    await db.rollback()
                    total = sum(item["affected_jobs"] for item in in_use)
                    raise HTTPException(409, {
                        "code": "profile_in_use", "profiles": in_use, "affected_jobs": total,
                        "message": f"{total} unsettled external job{'s' if total != 1 else ''} still depend"
                                   f"{'s' if total == 1 else ''} on this profile; nothing was changed"})
                current.external_delivery = catalog
                save_settings(current)
                apply_settings(current)
                await db.commit()
            await journal_configuration_change(previous, current)
    return catalog.model_dump()
