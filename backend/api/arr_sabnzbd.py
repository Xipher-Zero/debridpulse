"""The SABnzbd download-client dialect for Sonarr and Radarr.

Wire syntax only: ``GET|POST /api?mode=…`` -- SABnzbd's own path, which
Sonarr and Radarr call with URL Base blank -- answered in SABnzbd's JSON
shapes, over the native external contract (``external.service``) and the
shared *arr translation (``external.arr``). ``/api/sabnzbd/api`` (URL Base
``/api/sabnzbd``) is the same endpoint. The client configures DebridPulse as a
SABnzbd download client with an Integration token as its API key; the
authentication boundary verifies that key exactly as a Bearer token, on those
two exact paths only (``external.arr.QUERY_KEY_PATHS``).

Only what Sonarr and Radarr call is answered: ``version``, ``get_config``,
``addfile``, ``queue``, ``history`` and their ``name=delete``. Everything else
is declined explicitly. Refusals are SABnzbd's ``{"status": false, "error"}``
with HTTP 200, the form *arr reports. A ``nzo_id`` is the native job ID: stable
from queue to history, across restarts and across whatever transfer serves it.
The full protocol mapping is ``docs/architecture/ARR_SABNZBD_COMPATIBILITY.md``.
"""
from __future__ import annotations

import base64
import math
import posixpath
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from application.dependencies import get_application
from application.service import ApplicationService
from core.config import get_settings
from core.version import read_version
from external import arr
from external.intake import MAX_NZB_BYTES
from external.service import ExternalClients, ExternalRefused, client_of
from services.maintenance_gate import ApplicationMaintenanceActive
from transfers.errors import TransferError

router = APIRouter()

# The most items one history or delete request names.
MAX_HISTORY_PAGE = 500
MAX_DELETE_IDS = 100
# SABnzbd fields a submission may not carry: DebridPulse owns post-processing,
# scripts, naming and archive passwords for its own work.
_UNSUPPORTED_SUBMIT_FIELDS = ("pp", "script", "nzbname", "password")
# SABnzbd's "add paused" priority: there is no client-held paused submission.
_PAUSED_PRIORITY = "-2"
# A native queue reason -> the SABnzbd slot status *arr reads. Each is in
# *arr's ``SabnzbdDownloadStatus``; an unknown string would fail its whole poll.
_QUEUE_STATUS = {
    "awaiting_resolution": "Grabbing", "queued": "Queued", "resolving": "Grabbing",
    "input_required": "Queued", "downloading": "Downloading", "verifying": "Verifying",
    "verifying_material": "Verifying", "finalizing": "Extracting", "paused": "Paused",
}


def _error(message: str) -> JSONResponse:
    return JSONResponse({"status": False, "error": message})


def _int(params, name: str, default: int) -> int:
    value = params.get(name)
    if value in (None, ""):
        return default
    if not str(value).isdigit():
        raise ExternalRefused("invalid_request", f"{name} must be a non-negative integer")
    return int(value)


def _category(params) -> str | None:
    value = (params.get("category") or params.get("cat") or "").strip()
    return None if value in ("", "*") else value


# SABnzbd's own path, exactly ``/api`` (URL Base blank), and the prefixed one
# (URL Base ``/api/sabnzbd``): one endpoint (``external.arr.SABNZBD_API_PATHS``).
@router.api_route("", methods=["GET", "POST"], include_in_schema=False)
@router.api_route("/sabnzbd/api", methods=["GET", "POST"])
async def sabnzbd_api(request: Request, application: ApplicationService = Depends(get_application)):
    params = request.query_params
    mode = params.get("mode", "")
    principal = getattr(request.state, "principal", None)
    external = ExternalClients(application)
    try:
        client_id = client_of(principal)
        if mode == "version":
            # DebridPulse's own version: SABnzbd's is never claimed.
            return {"version": read_version()}
        if mode == "get_config":
            return _config(client_id)
        if mode == "addfile":
            return await _addfile(request, external, principal)
        if mode in {"queue", "history"}:
            name = params.get("name")
            if name == "delete":
                return await _delete(application, external, principal, params)
            if name:
                return _error(f"{mode} {name} is not supported")
            if mode == "queue":
                return await _queue(external, principal, params)
            return await _history(external, principal, params)
        if mode == "addurl":
            return _error("addurl is not supported; submit the NZB file with addfile")
        return _error("not implemented")
    except ExternalRefused as exc:
        return _error(f"{exc.message} ({exc.code})")


def _config(client_id: str) -> dict:
    """The categories this client may submit an NZB under, each one's folder
    as the client sees it (its profile's import root). No sorting, no
    pre-check; completed deliveries leave the history after the shortest
    profile retention."""
    profiles = [profile for profile in get_settings().external_delivery.for_client(client_id)
                if arr.ADAPTER_SOURCE_KINDS[arr.SABNZBD] & set(profile.source_kinds) and profile.categories]
    roots = [profile.effective_import_root for profile in profiles]
    days = max(1, math.ceil(min(profile.retention_hours for profile in profiles) / 24)) if profiles else 7
    return {"config": {
        "misc": {"complete_dir": posixpath.commonpath(roots) if roots else "/", "pre_check": False,
                 "history_retention": "", "history_retention_option": "days-delete",
                 "history_retention_number": days},
        "categories": [{"name": category, "dir": profile.effective_import_root, "pp": "", "script": "",
                        "priority": 0} for profile in profiles for category in profile.categories],
        "sorters": [],
    }}


async def _addfile(request: Request, external: ExternalClients, principal):
    """One uploaded NZB, received whole, then submitted through the native
    contract (``submit_live``). Success -- its ``nzo_id`` -- is answered only
    once the job and its one demand are durable."""
    params = request.query_params
    if request.method != "POST":
        return _error("addfile requires a multipart POST")
    if params.get("priority") == _PAUSED_PRIORITY:
        return _error("adding a paused job is not supported")
    for field in _UNSUPPORTED_SUBMIT_FIELDS:
        if params.get(field):
            return _error(f"{field} is not supported")
    category = (params.get("cat") or params.get("category") or "").strip()
    if not category:
        return _error("A category is required")
    try:
        form = await request.form(max_files=1, max_fields=16)
    except Exception:  # noqa: BLE001 - any unparseable body is one refusal
        return _error("The request body is not a valid multipart form")
    try:
        upload = form.get("name")
        if not isinstance(upload, UploadFile):
            return _error("The NZB must be the multipart file field 'name'")
        data = await upload.read(MAX_NZB_BYTES + 1)
        filename = PurePosixPath(str(upload.filename or "").replace("\\", "/")).name or "upload.nzb"
    finally:
        await form.close()
    if len(data) > MAX_NZB_BYTES:
        return _error(f"The NZB exceeds the {MAX_NZB_BYTES // (1024 * 1024)} MB limit (source_too_large)")
    display_name = filename[:-4] if filename.lower().endswith(".nzb") else filename
    job, _created = await external.submit_live(principal, {
        "category": category, "display_name": display_name[:256],
        "source": {"kind": "nzb", "nzb_base64": base64.b64encode(data).decode("ascii"), "filename": filename}})
    return {"status": True, "nzo_ids": [job["id"]]}


def _name(job: dict) -> str:
    return job["display_name"] or str((job.get("source") or {}).get("name") or "") or job["id"]


async def _queue(external: ExternalClients, principal, params) -> dict:
    start, limit = _int(params, "start", 0), _int(params, "limit", 0)
    listing = await external.visible(principal, settled=False, category=_category(params), start=start,
                                     **({"limit": limit} if limit else {}))
    slots = []
    for index, job in enumerate(listing["jobs"], start=start):
        percent = (job.get("progress") or {}).get("percent")
        slots.append({
            "index": index, "nzo_id": job["id"], "filename": _name(job), "cat": job["category"],
            "status": _QUEUE_STATUS.get(job["reason"], "Downloading"), "priority": "Normal",
            # Sizes and time left are unknown to the external contract:
            # SABnzbd's own unknown forms, never an invented figure.
            "mb": "0.00", "mbleft": "0.00", "timeleft": "0:00:00",
            "percentage": str(int(percent or 0)),
        })
    return {"queue": {"paused": bool(listing["globally_paused"]), "noofslots": len(slots),
                      "noofslots_total": listing["total"], "slots": slots}}


async def _history(external: ExternalClients, principal, params) -> dict:
    start = _int(params, "start", 0)
    limit = min(_int(params, "limit", 60) or MAX_HISTORY_PAGE, MAX_HISTORY_PAGE)
    listing = await external.visible(principal, settled=True, category=_category(params), start=start, limit=limit)
    slots = []
    for job in listing["jobs"]:
        slot = {"nzo_id": job["id"], "name": _name(job), "category": job["category"], "bytes": 0,
                "fail_message": "", "storage": ""}
        if job["phase"] == "failed":
            slot.update(status="Failed", fail_message=f"DebridPulse: {job['reason']}")
        else:
            location = arr.output_location(job["delivery"])
            if location is None:
                # Completed, but not importable now (material unavailable, or
                # no single folder holds it): never offered as Completed.
                slot["status"] = "Verifying"
            else:
                slot.update(status="Completed", storage=location[0], bytes=location[1])
        slots.append(slot)
    return {"history": {"noofslots": listing["total"], "slots": slots}}


async def _delete(application: ApplicationService, external: ExternalClients, principal, params):
    """Remove the named items from this client's view (``external.arr.remove``).
    ``del_files`` is accepted and never deletion authority: what happens to the
    work and its files is decided by each job's own condition, and DebridPulse
    deletes no file because a client asked. Another client's or an unknown ID
    is skipped, indistinguishably."""
    if params.get("del_files") not in (None, "", "0", "1"):
        return _error("del_files must be 0 or 1")
    ids = [item.strip() for item in str(params.get("value") or "").split(",") if item.strip()]
    if not ids:
        return _error("value is required")
    if len(ids) > MAX_DELETE_IDS or any(item.lower() == "all" for item in ids):
        return _error(f"name one to {MAX_DELETE_IDS} items by nzo_id")
    removed = []
    try:
        # A GET mutation: the same admission the mutation boundary gives POST.
        async with application.application_operation():
            for job_id in ids:
                try:
                    await arr.remove(external, principal, job_id)
                except ExternalRefused as exc:
                    if exc.code == "job_not_found":
                        continue
                    raise
                removed.append(job_id)
    except (ApplicationMaintenanceActive, TransferError):
        return _error("DebridPulse is not accepting changes right now; retry later")
    return {"status": True, "nzo_ids": removed}
