"""Transient validation, editable Settings helpers, and bundled document reads.

Validation routes deliberately test candidate connection values without
persisting or applying them. The directory-browser route is a read-only Settings
helper: it exposes only container-visible directories and reuses the canonical
Download Storage validator for current-directory selectability without applying
candidate health. The extraction-password route is a narrow Settings read
surface for the operator-maintained archive-password list; operational
credentials remain write-only through the normal public Settings payload.
Bundled legal/reference documents are exposed from a fixed allowlist so the UI
can display the exact files shipped with the installed DebridPulse build
without depending on GitHub or other external network access.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import aiohttp
from fastapi import APIRouter, HTTPException, Depends, Query, Response
from pydantic import BaseModel, Field

from core.branding import APP_SHORT_NAME
from core.config import get_settings
from core.logging_utils import sanitize_exception
from core.version import read_version
from integrations.definition import verification_fingerprint, verification_proof
from providers.alldebrid.admin import runtime_status as alldebrid_runtime_status
from providers.alldebrid.client import AllDebridService
from providers.alldebrid.definition import canonical_options as alldebrid_canonical_options
from services.notifications import NotificationService
from application.dependencies import get_application
from application.service import ApplicationService
from transfers.storage import StorageReason, StorageState


router = APIRouter()

ALLDEBRID_NAMESPACE = "alldebrid"


_LEGAL_DOCUMENTS = {
    "gpl": {
        "title": "GNU General Public License v2.0",
        "path": ("LICENSE",),
        "latest_url": "https://github.com/Xipher-Zero/debridpulse/blob/main/LICENSE",
    },
    "notice": {
        "title": "DebridPulse Attribution Notice",
        "path": ("NOTICE",),
        "latest_url": "https://github.com/Xipher-Zero/debridpulse/blob/main/NOTICE",
    },
    "upstream-mit": {
        "title": "Upstream MIT License",
        "path": ("LICENSES", "MIT.txt"),
        "latest_url": "https://github.com/Xipher-Zero/debridpulse/blob/main/LICENSES/MIT.txt",
    },
    "source-offer": {
        "title": "Corresponding Source Offer",
        "path": ("SOURCE_OFFER.md",),
        "latest_url": "https://github.com/Xipher-Zero/debridpulse/blob/main/SOURCE_OFFER.md",
    },
    "third-party": {
        "title": "Third-Party Dependency Licenses",
        "path": ("docs", "DEPENDENCY_LICENSES.md"),
        "latest_url": "https://github.com/Xipher-Zero/debridpulse/blob/main/docs/DEPENDENCY_LICENSES.md",
    },
}


class AllDebridValidationRequest(BaseModel):
    api_key: str = Field(default="", max_length=4096)
    clear_api_key: bool = False


class DiscordValidationRequest(BaseModel):
    webhook_url: str = Field(default="", max_length=8192)
    clear_webhook: bool = False
    username: str = Field(default="", max_length=80)
    avatar_url: str = Field(default="", max_length=8192)


class StatisticsReportDraftRequest(BaseModel):
    hours: int = Field(default=24, ge=1, le=8760)
    stats_report_webhook_url: str = Field(default="", max_length=8192)
    clear_stats_report_webhook: bool = False
    discord_webhook_url: str = Field(default="", max_length=8192)
    clear_discord_webhook: bool = False


class DirectoryCapacity(BaseModel):
    total_bytes: int | None = None
    free_bytes: int | None = None


class DirectoryBrowserEntry(BaseModel):
    name: str
    path: str
    accessible: bool
    writable: bool | None
    selectable: bool | None
    reason: str


class DirectoryBrowserCurrent(DirectoryBrowserEntry):
    capacity: DirectoryCapacity


class DirectoryBrowseResponse(BaseModel):
    current: DirectoryBrowserCurrent
    parent: str | None
    children: list[DirectoryBrowserEntry]


def _safe_failure(exc: Exception) -> str:
    return sanitize_exception(exc, max_length=200)


def _resolve_repository_root() -> Path:
    """Locate the root that contains the legal files in source and packaged runs."""
    here = Path(__file__).resolve()
    for candidate in (here.parents[2], here.parents[1], Path("/app")):
        if (candidate / "LICENSE").is_file() and (candidate / "NOTICE").is_file():
            return candidate
    raise RuntimeError("Bundled DebridPulse legal documents are unavailable")


def _legal_document_payload(document_id: str) -> dict[str, str]:
    meta = _LEGAL_DOCUMENTS.get(str(document_id or ""))
    if meta is None:
        raise HTTPException(404, "Unknown bundled document")

    root = _resolve_repository_root()
    document_path = root.joinpath(*meta["path"])
    try:
        content = document_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(500, "Bundled document could not be read") from exc

    return {
        "id": document_id,
        "title": meta["title"],
        "content": content,
        "latest_url": meta["latest_url"],
        "bundled_version": read_version(),
    }


def _directory_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code, detail={"code": code, "message": message})


def _resolve_requested_directory(value: str) -> Path:
    """Resolve one explicit absolute path through the canonical symlink policy.

    Browser navigation always follows directory symlinks and returns the resolved
    target path. Relative paths are rejected so process CWD can never influence
    navigation.
    """
    raw = str(value or "").strip()
    if not raw or "\x00" in raw:
        raise _directory_error(400, "invalid_path", "A valid absolute directory path is required")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise _directory_error(400, "relative_path", "Relative directory paths are not supported")
    try:
        return candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise _directory_error(404, "path_unavailable", "Directory path does not exist") from exc
    except NotADirectoryError as exc:
        raise _directory_error(400, "not_directory", "Requested path is not a directory") from exc
    except PermissionError as exc:
        raise _directory_error(403, "path_inaccessible", "Directory path is not accessible") from exc
    except RuntimeError as exc:
        raise _directory_error(400, "symlink_loop", "Directory path cannot be resolved safely") from exc
    except OSError as exc:
        if getattr(exc, "errno", None) == getattr(errno, "ELOOP", None):
            raise _directory_error(400, "symlink_loop", "Directory path cannot be resolved safely") from exc
        raise _directory_error(503, "path_unavailable", "Directory path is temporarily unavailable") from exc


def _try_browsable_directory(path: Path) -> bool:
    try:
        with os.scandir(path) as entries:
            next(entries, None)
        return True
    except OSError:
        return False


def _default_directory_path(configured_path: str) -> Path:
    """Use the configured Download Folder or its nearest browsable ancestor."""
    raw = str(configured_path or "").strip()
    candidate = Path(raw).expanduser() if raw else Path("/")
    if not candidate.is_absolute():
        candidate = Path("/")

    for lexical in (candidate, *candidate.parents):
        try:
            resolved = lexical.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if _try_browsable_directory(resolved):
            return resolved

    try:
        root = Path("/").resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _directory_error(503, "browser_unavailable", "Server filesystem browser is unavailable") from exc
    if not _try_browsable_directory(root):
        raise _directory_error(503, "browser_unavailable", "Server filesystem browser is unavailable")
    return root


def _directory_display_name(path: Path) -> str:
    return path.name or str(path)


def _child_directory_entry(entry) -> DirectoryBrowserEntry | None:
    """Return one child-directory row without performing a write/selectability probe.

    Symlink-to-directory entries are followed and expose the resolved target path.
    Symlinks to files, broken links, loops, and entries that disappear during the
    listing race are omitted. A confirmed directory that exists but cannot be
    entered remains visible as inaccessible.
    """
    try:
        if not entry.is_dir(follow_symlinks=True):
            return None
        resolved = Path(entry.path).resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError, RuntimeError):
        return None
    except OSError:
        return None

    try:
        with os.scandir(resolved) as children:
            next(children, None)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except PermissionError:
        return DirectoryBrowserEntry(
            name=entry.name,
            path=str(resolved),
            accessible=False,
            writable=False,
            selectable=False,
            reason=StorageReason.INACCESSIBLE.value,
        )
    except OSError:
        return DirectoryBrowserEntry(
            name=entry.name,
            path=str(resolved),
            accessible=False,
            writable=False,
            selectable=False,
            reason=StorageReason.INACCESSIBLE.value,
        )

    return DirectoryBrowserEntry(
        name=entry.name,
        path=str(resolved),
        accessible=True,
        writable=None,
        selectable=None,
        reason="not_validated",
    )


def _backup_directory_probe(path: Path) -> tuple[Path, bool]:
    """Plain existence/accessibility/writability probe for Backup Folder browsing.

    Backup Folder does NOT share Download Storage's minimum-space/health-state
    validation contract (`capacity.validate_download_path`) -- that validator
    answers "is this safe to actively download into right now" (free space,
    FULL/READ_ONLY/UNAVAILABLE state), which has no bearing on a backup
    destination. `backend/services/backup.py::run_backup()` already creates
    the configured folder on demand (`mkdir(parents=True, exist_ok=True)`)
    and tolerates it not existing yet (`list_backups()`), so this probe only
    ever answers "does this directory exist, and can DebridPulse write into
    it" -- never a capacity/space judgment, and never a second validator for
    the Save path (Save persists the typed/selected string exactly as today).
    """
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise _directory_error(404, "path_unavailable", "Directory path does not exist")
    if not resolved.is_dir():
        raise _directory_error(400, "not_directory", "Requested path is not a directory")
    try:
        accessible = os.access(resolved, os.R_OK | os.X_OK)
    except OSError:
        accessible = False
    if not accessible:
        raise _directory_error(403, "path_inaccessible", "Directory path is not accessible")
    try:
        writable = os.access(resolved, os.W_OK)
    except OSError:
        writable = False
    return resolved, writable


def _browse_directory(path: Path, capacity, *, purpose: str = "download") -> DirectoryBrowseResponse:
    """Build one non-recursive directory listing.

    ``purpose`` is a narrow discriminator on the ONE existing bounded
    directory-browse endpoint (never a second endpoint): "download" (default,
    existing callers unchanged) uses the canonical Download Storage validator
    as the selectability owner; "backup" uses the plain existence/writability
    probe above instead, since Backup Folder selection is directory
    navigation only, not a Download Storage health judgment.
    """
    if purpose == "backup":
        current_path, current_writable = _backup_directory_probe(path)
        selectable = bool(current_writable)
        current_reason = StorageReason.NONE.value if current_writable else StorageReason.READ_ONLY.value
        current_capacity = DirectoryCapacity(total_bytes=None, free_bytes=None)
    else:
        if capacity is None or not hasattr(capacity, "validate_download_path"):
            raise _directory_error(503, "browser_unavailable", "Download Storage validation is unavailable")

        snapshot = capacity.validate_download_path(path, apply_if_active=False)
        if snapshot.exists is False:
            raise _directory_error(404, "path_unavailable", "Directory path does not exist")
        if snapshot.is_directory is False:
            raise _directory_error(400, "not_directory", "Requested path is not a directory")
        if snapshot.accessible is False:
            if snapshot.reason == StorageReason.INACCESSIBLE:
                raise _directory_error(403, "path_inaccessible", "Directory path is not accessible")
            raise _directory_error(503, "path_unavailable", "Directory path is temporarily unavailable")

        current_path = Path(snapshot.resolved_path)
        current_state = StorageState(snapshot.state)
        selectable = bool(
            snapshot.is_directory is True
            and snapshot.accessible is True
            and snapshot.writable is True
            and current_state not in {
                StorageState.FULL,
                StorageState.READ_ONLY,
                StorageState.UNAVAILABLE,
            }
        )
        current_reason = snapshot.reason.value
        current_capacity = DirectoryCapacity(
            total_bytes=snapshot.total_bytes,
            free_bytes=snapshot.free_bytes,
        )
        current_writable = snapshot.writable

    try:
        with os.scandir(current_path) as entries:
            rows = []
            for entry in entries:
                child = _child_directory_entry(entry)
                if child is not None:
                    rows.append(child)
    except FileNotFoundError as exc:
        raise _directory_error(404, "path_unavailable", "Directory path no longer exists") from exc
    except NotADirectoryError as exc:
        raise _directory_error(400, "not_directory", "Requested path is not a directory") from exc
    except PermissionError as exc:
        raise _directory_error(403, "path_inaccessible", "Directory path is not accessible") from exc
    except OSError as exc:
        raise _directory_error(503, "path_unavailable", "Directory path is temporarily unavailable") from exc

    rows.sort(key=lambda item: (item.name.casefold(), item.name))
    parent_path = current_path.parent
    parent = None if parent_path == current_path else str(parent_path)
    current = DirectoryBrowserCurrent(
        name=_directory_display_name(current_path),
        path=str(current_path),
        accessible=True,
        writable=current_writable,
        selectable=selectable,
        reason=current_reason,
        capacity=current_capacity,
    )
    return DirectoryBrowseResponse(current=current, parent=parent, children=rows)


# ── Verification evidence: what a Test proves about SAVED configuration ──────
#
# A Test is not a configuration writer and never becomes one. What it may do is
# state truth about evidence:
#
#   * it hands back an opaque proof of exactly the material it exercised, which
#     the Save that promotes that draft can present (and which the generic
#     acceptance owner validates against what was actually saved);
#   * if the material it exercised IS the current saved configuration, there is
#     no unsaved draft being promoted, so the outcome is recorded immediately --
#     success establishes verification, failure retires a proof that has stopped
#     being true rather than leaving the provider claiming ``Verified``.
#
# A Test of a draft that is not the saved configuration matches no saved
# subject, so it can neither verify nor revoke anything.


async def _record_verification_outcome(application: ApplicationService, integration_id: str,
                                       fingerprint: str, ok: bool):
    """Persist the outcome of a Test about the CURRENT saved configuration.

    It takes the same narrow config-write lock every other settings mutation
    uses, and it needs no admission of its own: these are POST routes, so the
    application mutation-admission middleware is already holding
    ``application_operation()`` for the whole request.

    Returns the integration's canonical PUBLIC projection when durable evidence
    actually changed, so the caller can publish the accepted state through the
    one neutral acceptance seam -- the header must not keep reporting
    ``Configured`` about a configuration this very request just proved. ``None``
    means nothing changed and there is nothing to publish.
    """
    from core.config import apply_settings, config_write_lock, load_settings, save_settings
    from integrations.configuration import public_integrations, record_verification_outcome

    definition = next((item for item in application.definitions if item.id == integration_id), None)
    if definition is None:
        return None
    async with config_write_lock():
        updated = record_verification_outcome(load_settings(), definition, fingerprint, ok)
        if updated is None:
            return None
        # Evidence is metadata ABOUT canonical configuration, not configuration:
        # nothing routable, native or lifecycle-bound changed, so this persists
        # and republishes the document without a reconfigure.
        save_settings(updated)
        apply_settings(updated)
        return public_integrations(updated, application.definitions).get(integration_id)


def _accepted(integration_id: str, projection) -> dict:
    """The neutral acceptance envelope, or nothing when nothing was accepted."""
    if not projection:
        return {}
    return {"integration_id": integration_id, "integration": projection}


def _resolve_secret_candidate(candidate: str, stored: str, *, clear: bool) -> str:
    """Resolve a redacted Settings secret without persisting draft state.

    A non-empty candidate wins. A blank candidate preserves the stored value
    unless the operator explicitly checked the corresponding clear control.
    """
    if clear:
        return ""
    typed = str(candidate or "").strip()
    if typed:
        return typed
    return str(stored or "").strip()


def _is_discord_webhook(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in {"discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"}


def _draft_discord_identity(username: str, avatar_url: str) -> tuple[str, str]:
    name = str(username or "").strip() or APP_SHORT_NAME
    avatar = str(avatar_url or "").strip()
    if avatar.startswith("data:") or avatar.lower().endswith(".svg"):
        avatar = ""
    return name, avatar


async def _send_discord_draft_test(webhook_url: str, username: str, avatar_url: str) -> None:
    """Send the Discord test using the identity currently shown in Settings."""
    name, avatar = _draft_discord_identity(username, avatar_url)
    payload = {
        "username": name,
        "embeds": [
            {
                "title": "🔔 Test Notification",
                "description": f"**{APP_SHORT_NAME}** is connected and ready.",
                "color": 0x3B82F6,
            }
        ],
    }
    if avatar:
        payload["avatar_url"] = avatar

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(webhook_url, json=payload) as response:
            if response.status not in (200, 204):
                body = await response.text()
                raise RuntimeError(f"Discord webhook returned HTTP {response.status}: {body[:200]}")


@router.get("/legal-documents/{document_id}")
async def get_bundled_legal_document(document_id: str):
    """Return one fixed, locally bundled legal/reference document."""
    return _legal_document_payload(document_id)


@router.get("/settings/extraction-passwords")
async def get_extraction_passwords():
    """Return the operator-maintained archive-password list for editing.

    Archive passwords are content-unlock data rather than operational service
    credentials. The general Settings response continues to redact this field;
    only this purpose-built Settings surface returns the actual newline list.
    """
    return {"passwords": str(get_settings().extraction_password or "")}


@router.get("/settings/directories", response_model=DirectoryBrowseResponse)
def browse_directories(
    response: Response,
    path: str | None = Query(default=None, min_length=1, max_length=4096),
    purpose: Literal["download", "backup"] = Query(default="download"),
    application: ApplicationService = Depends(get_application),
):
    """Browse one container-visible directory without exposing files or mutations.

    ``purpose`` narrows only which setting's default path this browse starts
    from (when ``path`` is omitted) and which selectability rule applies --
    see ``_browse_directory``. Default behavior (``purpose`` omitted) is
    unchanged for existing Download Folder callers.
    """
    default_configured_path = (
        get_settings().backup_folder if purpose == "backup" else get_settings().download_folder
    )
    requested = (
        _resolve_requested_directory(path)
        if path is not None
        else _default_directory_path(default_configured_path)
    )
    result = _browse_directory(requested, application.capacity, purpose=purpose)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/integration-status/alldebrid")
async def get_alldebrid_runtime_status(application: ApplicationService = Depends(get_application)):
    """Return AllDebrid-specific status without inferring from generic health."""
    provider = application.engine.registry.providers.get("alldebrid")
    return await alldebrid_runtime_status(provider)


@router.post("/settings/validate-alldebrid")
async def validate_alldebrid(payload: AllDebridValidationRequest,
                             application: ApplicationService = Depends(get_application)):
    alldebrid = alldebrid_canonical_options(get_settings())
    if payload.clear_api_key:
        api_key = ""
    else:
        api_key = payload.api_key.strip() or str(alldebrid.api_key or "").strip()
    if not api_key:
        raise HTTPException(400, "No API key configured or entered")

    # Exactly what this request authenticates with, in the shape the AllDebrid
    # definition declares as its verification material.
    fingerprint = verification_fingerprint({"api_key": api_key, "agent": str(alldebrid.agent or "")})
    try:
        service = AllDebridService(api_key, alldebrid.agent)
        user = await service.get_user()
    except Exception as exc:
        await _record_verification_outcome(application, ALLDEBRID_NAMESPACE, fingerprint, False)
        raise HTTPException(502, _safe_failure(exc)) from exc
    accepted = await _record_verification_outcome(application, ALLDEBRID_NAMESPACE, fingerprint, True)
    user_data = user.get("user", user)
    return {
        "ok": True,
        "username": user_data.get("username", ""),
        "isPremium": user_data.get("isPremium", False),
        "premiumUntil": user_data.get("premiumUntil", user_data.get("premium_until", 0)),
        "verification": verification_proof(fingerprint),
        **_accepted(ALLDEBRID_NAMESPACE, accepted),
    }


@router.post("/settings/validate-discord")
async def validate_discord(payload: DiscordValidationRequest):
    cfg = get_settings()
    webhook_url = _resolve_secret_candidate(
        payload.webhook_url,
        str(cfg.discord_webhook_url or ""),
        clear=payload.clear_webhook,
    )
    if not webhook_url:
        raise HTTPException(400, "No Discord webhook configured or entered")

    try:
        if _is_discord_webhook(webhook_url):
            await _send_discord_draft_test(
                webhook_url,
                payload.username,
                payload.avatar_url,
            )
            sent = True
        else:
            sent = await NotificationService(webhook_url).test()
        if not sent:
            raise RuntimeError("Discord test did not send a notification")
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(502, _safe_failure(exc)) from exc


@router.post("/settings/send-stats-report")
async def send_stats_report_from_draft(payload: StatisticsReportDraftRequest):
    """Send a report using the current Notifications draft without saving it.

    Secret fields preserve their stored value while redacted/blank, respect an
    explicit clear request, and retain the normal reporting -> primary Discord
    webhook fallback. Only Apply Settings persists any of these draft values.
    """
    cfg = get_settings()
    reporting_url = _resolve_secret_candidate(
        payload.stats_report_webhook_url,
        str(getattr(cfg, "stats_report_webhook_url", "") or ""),
        clear=payload.clear_stats_report_webhook,
    )
    if not reporting_url:
        reporting_url = _resolve_secret_candidate(
            payload.discord_webhook_url,
            str(getattr(cfg, "discord_webhook_url", "") or ""),
            clear=payload.clear_discord_webhook,
        )
    if not reporting_url:
        raise HTTPException(400, "No reporting or primary Discord webhook configured or entered")

    try:
        from services.stats import send_stats_report

        return await send_stats_report(
            hours=payload.hours,
            webhook_url=reporting_url,
            triggered_by="manual",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, _safe_failure(exc)) from exc


# --- Usenet (SAB-backed) integration surfaces --------------------------------
#
# The Usenet integration is one canonical integration owning both a provider and
# an executor, so every surface below reads and writes exactly one namespace:
# ``integrations.usenet``. There is no second settings owner and no second
# enable state.


# DebridPulse's own connection bounds; the floor is 1, not SAB's 0 (see
# integrations/usenet/definition.py).
from integrations.usenet.definition import MAX_CONNECTIONS, MIN_CONNECTIONS


class UsenetServerDraft(BaseModel):
    """A prospective NNTP server, tested WITHOUT being persisted."""
    host: str = ""
    port: int = Field(default=563, ge=1, le=65535)
    ssl: bool = True
    username: str = ""
    password: str = ""
    connections: int = Field(default=8, ge=MIN_CONNECTIONS, le=MAX_CONNECTIONS)
    # The canonical server whose stored password a blank value should reuse.
    server_id: str | None = None


USENET_NAMESPACE = "usenet"


def _usenet_admin(application: ApplicationService):
    try:
        return application.integration_admin("sabnzbd")
    except ValueError:
        raise HTTPException(503, "The Usenet integration is not available") from None


@router.get("/integration-status/usenet")
async def get_usenet_runtime_status(application: ApplicationService = Depends(get_application)):
    """Readiness for the neutral provider-status surface.

    Enabled-but-unconfigured is a legitimate configuration state and is
    reported as such -- never as ready.
    """
    entry = (get_settings().integrations or {}).get("usenet")
    if entry is not None and not getattr(entry, "enabled", True):
        return {"state": "disabled", "configured": False}
    return await _usenet_admin(application).status()


@router.post("/usenet/servers/test")
async def test_usenet_server(payload: UsenetServerDraft,
                             application: ApplicationService = Depends(get_application)):
    """Validate an edited-but-unsaved server against SAB, with no persistence."""
    if not payload.host.strip():
        raise HTTPException(400, "A server host is required")
    admin = _usenet_admin(application)
    password = payload.password
    if not password and payload.server_id:
        # The existing UI contract: a blank secret means "keep the stored one".
        from integrations.usenet.servers import find_server
        existing = find_server(admin.options, payload.server_id)
        if existing is not None:
            password = existing.password
    host = payload.host.strip()
    # Exactly what this request connects with, in the shape the Usenet
    # definition declares as one server's verification material.
    fingerprint = verification_fingerprint({
        "host": host, "port": int(payload.port), "ssl": bool(payload.ssl),
        "username": str(payload.username or ""), "password": str(password or ""),
        "connections": int(payload.connections)})
    try:
        result = await admin.test_server(
            host=host, port=payload.port, ssl=payload.ssl,
            username=payload.username, password=password, connections=payload.connections,
        )
    except Exception as exc:
        await _record_verification_outcome(application, USENET_NAMESPACE, fingerprint, False)
        raise HTTPException(502, _safe_failure(exc)) from exc
    ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
    accepted = await _record_verification_outcome(application, USENET_NAMESPACE, fingerprint, ok)
    envelope = _accepted(USENET_NAMESPACE, accepted)
    if not ok:
        return {**result, **envelope}
    return {**result, "verification": verification_proof(fingerprint), **envelope}


@router.get("/usenet/drift")
async def get_usenet_configuration_drift(application: ApplicationService = Depends(get_application)):
    """Report SAB-side configuration drift. Detection only.

    Nothing here imports SAB state as canonical DebridPulse configuration and
    nothing reconciles continuously; a drifted field is reported by name so the
    operator can decide to re-apply.
    """
    try:
        return (await _usenet_admin(application).drift()).public()
    except Exception as exc:
        raise HTTPException(502, _safe_failure(exc)) from exc
