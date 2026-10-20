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
    # The profiles whose dependent jobs the Admin explicitly authorizes this
    # write to retire (``profile_in_use`` otherwise): deleting the profile,
    # moving its roots, or removing clients from it retires exactly the jobs
    # that change takes it away from.
    retire_dependents: list[str] = Field(default_factory=list, max_length=256)


@router.get("/external/profiles/{profile_id}/dependents")
async def get_external_profile_dependents(profile_id: str):
    """How many unsettled external jobs deleting this profile would retire,
    counted from durable ownership now (the write re-counts at its commit)."""
    async with get_db() as db:
        count = await ExternalClients.profile_dependents(db, str(profile_id)[:64])
    return {"profile": str(profile_id)[:64], "affected_jobs": count}


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
    revision is refused (409). Every profile the write creates or changes
    must have its ``local_root`` in the download folder or beneath it; a
    profile carried exactly as stored is not re-judged here, only at delivery.

    A write that would take a profile away from jobs that still depend on it
    -- deleting it, moving its roots, or removing a client -- is refused
    (``409 profile_in_use``) with the affected job count, while any such job
    is unsettled (``ExternalClients.profile_dependent_jobs``) -- unless the
    Admin named that profile in ``retire_dependents``: then exactly those
    jobs are retired (their demand withdrawn, ``ExternalClients
    .retire_dependents_in``) in the same commit that changes the catalog,
    and settled through the core after it (``settle_retired``; maintenance
    finishes it after a failure or restart). Nothing else is touched: other
    claims on the same work keep it, and no file is deleted. The count, the
    retirement and the catalog replacement happen inside one database write
    transaction, the same one an external reservation takes, so no admission
    can slip between them; one already in flight is retired when it is
    accepted."""
    async with application.application_operation():
        async with config_write_lock():
            previous = get_settings()
            current = load_settings()
            if current.external_delivery.revision != body.expected_revision:
                raise HTTPException(409, {"code": "stale_revision", "revision": current.external_delivery.revision,
                                          "message": "The profile catalog changed; reload it and try again"})
            # Containment is judged for every profile this write creates or
            # changes in any way. Only a profile carried exactly as stored is
            # not re-judged: a Download Folder change must not make every later
            # catalog write fail on a profile nobody is editing. Such a profile
            # is kept but never widened (an edit must first move its root) and
            # stays undeliverable until moved: delivery re-judges containment
            # every time (ExternalClients, ``profile_root_outside_downloads``).
            stored = {profile.id: DeliveryProfile.model_validate(profile.model_dump())
                      for profile in current.external_delivery.profiles}
            for profile in body.profiles:
                if stored.get(profile.id) == profile:
                    continue
                if not confined_to(current.download_folder, profile.local_root):
                    raise HTTPException(422, {
                        "code": "invalid_profile", "profile": profile.id, "field": "local_root",
                        "message": f"Delivery profile '{profile.id}': its DP Download Path (local_root) must be "
                                   f"the Download Folder or beneath it"})
            try:
                catalog = ExternalDeliverySettings(profiles=body.profiles,
                                                   revision=current.external_delivery.revision + 1)
            except ValidationError as exc:
                raise HTTPException(422, {"code": "invalid_profile",
                                          "message": "; ".join(str(item.get("msg")) for item in exc.errors())})
            replaced = {profile.id: profile for profile in catalog.profiles}
            authorized = set(body.retire_dependents)
            retired: list[str] = []
            async with get_db() as db:
                await db.execute("BEGIN IMMEDIATE")
                in_use, retiring = [], []
                for before in current.external_delivery.profiles:
                    narrowing = _narrowing(before, replaced.get(before.id))
                    if narrowing is None:
                        continue
                    dependents = await ExternalClients.profile_dependent_jobs(db, before.id, narrowing[1])
                    if dependents and before.id in authorized:
                        retiring.extend(dependents)
                    elif dependents:
                        in_use.append({"id": before.id, "change": narrowing[0],
                                       "affected_jobs": len(dependents)})
                if in_use:
                    await db.rollback()
                    total = sum(item["affected_jobs"] for item in in_use)
                    raise HTTPException(409, {
                        "code": "profile_in_use", "profiles": in_use, "affected_jobs": total,
                        "message": f"{total} unsettled external job{'s' if total != 1 else ''} still depend"
                                   f"{'s' if total == 1 else ''} on this profile; nothing was changed"})
                retired = await ExternalClients.retire_dependents_in(db, retiring)
                current.external_delivery = catalog
                # The database commit is the write's one commit point: the
                # catalog file is written only once the retirement is durable,
                # so a failed or interrupted commit never leaves a catalog
                # whose dependents were not retired. The new catalog is
                # applied before it, while this write still holds the
                # reservation lock, and withdrawn again if the write does not
                # complete. An interruption after the commit leaves the
                # retirement (settled by maintenance) under the unchanged
                # catalog, which the same write, retried, then replaces.
                apply_settings(current)
                try:
                    await db.commit()
                    save_settings(current)
                except BaseException:
                    apply_settings(previous)
                    raise
            await journal_configuration_change(previous, current)
        if not retired:
            return catalog.model_dump()
        settled = await ExternalClients(application).settle_retired(retired)
    return {**catalog.model_dump(), "retired_jobs": len(retired), "settled_jobs": settled,
            "cleanup_pending": settled < len(retired)}
