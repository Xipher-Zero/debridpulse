"""The qBittorrent download-client dialect for Sonarr and Radarr (Bearer only).

Wire syntax only: ``/api/qbittorrent/api/v2/{section}/{method}`` answered in
qBittorrent WebAPI 2.15.1 shapes, over the native external contract
(``external.service``) and the shared *arr translation (``external.arr``).
The client configures DebridPulse as a qBittorrent download client with URL
Base ``/api/qbittorrent`` and an Integration token as its **API Key**, which
it sends as ``Authorization: Bearer`` -- the existing authentication boundary
verifies it like every Bearer token. There is no login, cookie, session or
key in the URL.

Only what Sonarr (>= v4.0.18.2971) and Radarr (>= v6.2.1.10461) call is
answered; the calls they make that DebridPulse does not honor are declined
with qBittorrent's own ``409``; anything else is ``404``. The item a client
sees is the native job, keyed on the wire by the torrent's v1 info-hash that
the intake owner computed. The full mapping is
``docs/architecture/ARR_QBITTORRENT_COMPATIBILITY.md``.
"""
from __future__ import annotations

import base64
import posixpath
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.datastructures import UploadFile

from application.dependencies import get_application
from application.service import ApplicationService
from core.config import get_settings
from external import arr
from external.intake import MAX_TORRENT_BYTES
from external.service import ExternalClients, ExternalRefused, client_of
from services.maintenance_gate import ApplicationMaintenanceActive
from transfers.errors import TransferError

router = APIRouter()

# qBittorrent 5.2.0 introduced API-key (Bearer) authentication with WebAPI
# 2.15.1 (src/webui/webapplication.h); this dialect answers that revision's
# shapes for the calls *arr makes. *arr reads it to choose content_path
# (>= 2.6.1), share limits on add (>= 2.8.1) and ``stopped`` (>= 2.11.0).
WEBAPI_VERSION = "2.15.1"
TORRENT_KINDS = ("magnet", "torrent")
# qBittorrent's own "ETA unknown" value, which *arr reads as no estimate.
UNKNOWN_ETA = 8640000
MAX_DELETE_HASHES = 100
# The *arr calls DebridPulse declines: it owns queue order, start, seeding and
# categories (``createCategory`` succeeds only for a category that exists).
_DECLINED = {("torrents", "setCategory"), ("torrents", "setShareLimits"), ("torrents", "topPrio"),
             ("torrents", "setForceStart")}
# A native unfinished reason -> the qBittorrent state *arr reads.
_STATE = {"awaiting_resolution": "queuedDL", "queued": "queuedDL", "resolving": "queuedDL",
          "input_required": "queuedDL", "downloading": "downloading", "verifying": "checkingDL",
          "verifying_material": "checkingDL", "finalizing": "moving", "paused": "stoppedDL"}
_STATUS = {"unknown_category": 409, "duplicate_torrent": 409, "source_kind_not_permitted": 409,
           "invalid_source": 415, "unsupported_source": 415, "source_too_large": 413, "rate_limited": 429,
           "client_job_limit": 429, "submission_outcome_unknown": 503, "source_unavailable": 503,
           "client_credential_required": 403, "invalid_request": 400, "job_not_found": 404}


def _text(message: str, status: int = 200) -> PlainTextResponse:
    return PlainTextResponse(message, status_code=status)


def _refused(exc: ExternalRefused) -> PlainTextResponse:
    return _text(f"{exc.message} ({exc.code})", _STATUS.get(exc.code, 409))


def _profiles(client_id: str):
    """The client's delivery profiles that take a torrent source."""
    return [profile for profile in get_settings().external_delivery.for_client(client_id)
            if set(profile.source_kinds) & set(TORRENT_KINDS) and profile.categories]


@router.api_route("/qbittorrent/api/v2/{section}/{method}", methods=["GET", "POST"])
async def qbittorrent_api(section: str, method: str, request: Request,
                          application: ApplicationService = Depends(get_application)):
    principal = getattr(request.state, "principal", None)
    external = ExternalClients(application)
    try:
        client_id = client_of(principal)
        call = (section, method)
        if call == ("app", "webapiVersion"):
            return _text(WEBAPI_VERSION)
        if call == ("app", "preferences"):
            return _preferences(client_id)
        if call == ("torrents", "categories"):
            return {category: {"name": category, "savePath": profile.effective_import_root}
                    for profile in _profiles(client_id) for category in profile.categories}
        if call == ("torrents", "info"):
            return [_torrent(job) for job in await _items(external, principal, request.query_params.get("category"),
                                                          _hashes(request.query_params.get("hashes")))]
        if call == ("torrents", "properties"):
            return await _properties(external, principal, request.query_params.get("hash"))
        if request.method != "POST" and call in {("torrents", "add"), ("torrents", "delete"),
                                                 ("torrents", "createCategory")} | _DECLINED:
            return _text("Method Not Allowed", 405)
        if call == ("torrents", "add"):
            return await _add(request, external, principal)
        if call == ("torrents", "delete"):
            return await _delete(request, application, external, principal)
        if call == ("torrents", "createCategory"):
            form = await request.form(max_files=0, max_fields=8)
            category = str(form.get("category") or "").strip()
            known = {name for profile in _profiles(client_id) for name in profile.categories}
            # A category is a DebridPulse delivery profile's: it exists, or
            # it cannot be created by a client.
            return _text("") if category in known else _text("Unable to create category", 409)
        if call in _DECLINED:
            return _text(f"{method} is not supported: DebridPulse owns queue order, start, seeding and categories",
                         409)
        return _text("Not Found", 404)
    except ExternalRefused as exc:
        return _refused(exc)


def _preferences(client_id: str) -> dict:
    """What *arr reads from the preferences: no share-ratio or seeding-time
    removal (DebridPulse never seeds), no queueing (it owns queue order), and
    ``dht`` true -- the field *arr uses to decide whether a magnet without
    trackers can be resolved, which DebridPulse's providers do."""
    roots = [profile.effective_import_root for profile in _profiles(client_id)]
    return {"save_path": posixpath.commonpath(roots) if roots else "/", "max_ratio_enabled": False,
            "max_ratio": -1, "max_seeding_time_enabled": False, "max_seeding_time": -1,
            "max_inactive_seeding_time_enabled": False, "max_inactive_seeding_time": -1, "max_ratio_act": 0,
            "queueing_enabled": False, "dht": True}


def _hashes(value) -> set[str] | None:
    if value in (None, "", "all"):
        return None
    return {item.strip().lower() for item in str(value).split("|") if item.strip()}


async def _items(external: ExternalClients, principal, category, hashes: set[str] | None) -> list[dict]:
    """The client's visible torrent jobs: one item per info-hash, the most
    recently submitted job of that hash (a hash is one torrent to the client).
    ``category`` absent lists every category, as qBittorrent does."""
    category = None if category is None else str(category)
    if category == "":
        return []
    jobs = []
    for settled in (False, True):
        jobs.extend((await external.visible(principal, settled=settled, category=category,
                                            kinds=TORRENT_KINDS))["jobs"])
    items: dict[str, dict] = {}
    for job in jobs:
        info_hash = str((job.get("source") or {}).get("info_hash") or "").lower()
        if not info_hash or (hashes is not None and info_hash not in hashes):
            continue
        if info_hash not in items or (job["submitted_at"] or "") > (items[info_hash]["submitted_at"] or ""):
            items[info_hash] = job
    return [items[key] for key in sorted(items)]


def _torrent(job: dict) -> dict:
    """One job as the torrent *arr parses (``QBittorrentTorrent``). No
    speed, seed or tracker figure is reported: none exists. A finished,
    importable job is ``stoppedUP`` -- downloaded and not transferring --
    with a share-ratio limit of 0 already met: DebridPulse never seeds, so
    *arr may remove it once imported."""
    info_hash = str(job["source"]["info_hash"]).lower()
    profile = get_settings().external_delivery.by_id(job["profile_id"])
    save_path = profile.effective_import_root if profile is not None else ""
    percent = (job.get("progress") or {}).get("percent")
    torrent = {"hash": info_hash, "infohash_v1": info_hash, "infohash_v2": "",
               "name": job["display_name"] or str(job["source"].get("name") or "") or info_hash,
               "category": job["category"], "save_path": save_path, "content_path": "", "size": 0,
               "progress": round(float(percent or 0) / 100, 4), "eta": UNKNOWN_ETA, "ratio": 0,
               "ratio_limit": -2, "seeding_time_limit": -2, "inactive_seeding_time_limit": -2}
    if job["phase"] == "failed":
        torrent["state"] = "error"
    elif job["phase"] == "completed":
        location = arr.output_location(job["delivery"])
        if location is None:
            # Downloaded, but not importable now: still being checked.
            torrent.update(state="checkingUP", progress=1.0)
        else:
            torrent.update(state="stoppedUP", progress=1.0, eta=0, ratio_limit=0,
                           save_path=job["delivery"]["import_root"], content_path=location[0], size=location[1])
    else:
        torrent["state"] = _STATE.get(job["reason"], "downloading")
    return torrent


async def _properties(external: ExternalClients, principal, value) -> JSONResponse | dict:
    hashes = _hashes(value)
    found = await _items(external, principal, None, hashes) if hashes else []
    if len(found) != 1:
        return _text("Not Found", 404)
    torrent = _torrent(found[0])
    return {"hash": torrent["hash"], "save_path": torrent["save_path"], "seeding_time": 0}


async def _params(request: Request, *, files: int):
    form = await request.form(max_files=files, max_fields=32)
    return form, {**dict(request.query_params), **{key: value for key, value in form.items()
                                                   if not isinstance(value, UploadFile)}}


async def _add(request: Request, external: ExternalClients, principal):
    """One magnet (``urls``) or one ``.torrent`` (``torrents``), received
    whole and submitted through the native contract (``submit_live``). The
    answer -- qBittorrent's added-torrent summary -- is given only once the
    job and its one demand are durable."""
    try:
        form, params = await _params(request, files=2)
    except Exception:  # noqa: BLE001 - any unparseable body is one refusal
        return _text("The request body is not a valid form", 400)
    try:
        uploads = [item for item in form.getlist("torrents") if isinstance(item, UploadFile)]
        urls = [line.strip() for line in str(params.get("urls") or "").splitlines() if line.strip()]
        if len(uploads) + len(urls) != 1:
            return _text("Submit exactly one magnet or one .torrent per request", 400)
        for name in ("savepath", "downloadPath"):
            if str(params.get(name) or "").strip():
                return _text(f"{name} is not supported: DebridPulse owns where it writes", 409)
        for name in ("stopped", "paused", "useDownloadPath", "autoTMM"):
            if str(params.get(name) or "").strip().lower() == "true":
                return _text(f"{name}=true is not supported", 409)
        category = str(params.get("category") or "").strip()
        if not category:
            return _text("A category is required", 409)
        if urls:
            if not urls[0].lower().startswith("magnet:"):
                return _text("Only a magnet link is accepted; DebridPulse does not fetch URLs here", 415)
            source = {"kind": "magnet", "magnet": urls[0]}
        else:
            data = await uploads[0].read(MAX_TORRENT_BYTES + 1)
            if len(data) > MAX_TORRENT_BYTES:
                return _text(f"The torrent exceeds the {MAX_TORRENT_BYTES // (1024 * 1024)} MB limit "
                             f"(source_too_large)", 413)
            filename = PurePosixPath(str(uploads[0].filename or "").replace("\\", "/")).name or "upload.torrent"
            source = {"kind": "torrent", "torrent_base64": base64.b64encode(data).decode("ascii"),
                      "filename": filename}
    finally:
        await form.close()
    job, _created = await external.submit_live(principal, {"category": category, "source": source})
    return {"success_count": 1, "failure_count": 0, "pending_count": 0,
            "added_torrent_ids": [str(job["source"]["info_hash"]).lower()]}


async def _delete(request: Request, application: ApplicationService, external: ExternalClients, principal):
    """Remove the named torrents from this client (``external.arr.remove``,
    for every visible job of each hash). ``deleteFiles`` is accepted and never
    deletion authority; an unknown hash, or another client's, is skipped."""
    try:
        form, params = await _params(request, files=0)
        await form.close()
    except Exception:  # noqa: BLE001
        return _text("The request body is not a valid form", 400)
    if str(params.get("deleteFiles") or "false").lower() not in {"true", "false"}:
        return _text("deleteFiles must be true or false", 400)
    hashes = _hashes(params.get("hashes"))
    if not hashes or len(hashes) > MAX_DELETE_HASHES:
        return _text(f"name one to {MAX_DELETE_HASHES} hashes", 400)
    try:
        # The same admission every application mutation is given.
        async with application.application_operation():
            for settled in (False, True):
                listing = await external.visible(principal, settled=settled, kinds=TORRENT_KINDS)
                for job in listing["jobs"]:
                    if str((job.get("source") or {}).get("info_hash") or "").lower() in hashes:
                        await arr.remove(external, principal, job["id"])
    except (ApplicationMaintenanceActive, TransferError):
        return _text("DebridPulse is not accepting changes right now; retry later", 503)
    return _text("")
