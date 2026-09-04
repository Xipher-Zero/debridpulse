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
from providers.alldebrid.admin import runtime_status as alldebrid_runtime_status
from providers.alldebrid.client import AllDebridService
from executors.aria2.client import Aria2Service
from services.notifications import NotificationService
from application.dependencies import get_application
from application.service import ApplicationService
from transfers.storage import StorageReason, StorageState


router = APIRouter()


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


class Aria2ValidationRequest(BaseModel):
    mode: Literal["builtin", "external"]
    url: str = Field(default="", max_length=4096)
    secret: str = Field(default="", max_length=4096)
    clear_secret: bool = False


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


def _browse_directory(path: Path, capacity) -> DirectoryBrowseResponse:
    """Build one non-recursive directory listing using WS1 as selectability owner."""
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
    parent_path = current_path.parent
    parent = None if parent_path == current_path else str(parent_path)
    current = DirectoryBrowserCurrent(
        name=_directory_display_name(current_path),
        path=str(current_path),
        accessible=True,
        writable=snapshot.writable,
        selectable=selectable,
        reason=snapshot.reason.value,
        capacity=DirectoryCapacity(
            total_bytes=snapshot.total_bytes,
            free_bytes=snapshot.free_bytes,
        ),
    )
    return DirectoryBrowseResponse(current=current, parent=parent, children=rows)


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
    application: ApplicationService = Depends(get_application),
):
    """Browse one container-visible directory without exposing files or mutations."""
    requested = (
        _resolve_requested_directory(path)
        if path is not None
        else _default_directory_path(get_settings().download_folder)
    )
    result = _browse_directory(requested, application.capacity)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/integration-status/alldebrid")
async def get_alldebrid_runtime_status(application: ApplicationService = Depends(get_application)):
    """Return AllDebrid-specific status without inferring from generic health."""
    provider = application.engine.registry.providers.get("alldebrid")
    return await alldebrid_runtime_status(provider)


@router.post("/settings/validate-alldebrid")
async def validate_alldebrid(payload: AllDebridValidationRequest):
    cfg = get_settings()
    if payload.clear_api_key:
        api_key = ""
    else:
        api_key = payload.api_key.strip() or str(cfg.alldebrid_api_key or "").strip()
    if not api_key:
        raise HTTPException(400, "No API key configured or entered")

    try:
        service = AllDebridService(api_key, cfg.alldebrid_agent)
        user = await service.get_user()
        user_data = user.get("user", user)
        return {
            "ok": True,
            "username": user_data.get("username", ""),
            "isPremium": user_data.get("isPremium", False),
            "premiumUntil": user_data.get("premiumUntil", user_data.get("premium_until", 0)),
        }
    except Exception as exc:
        raise HTTPException(502, _safe_failure(exc)) from exc


@router.post("/settings/validate-aria2")
async def validate_aria2(payload: Aria2ValidationRequest, application: ApplicationService = Depends(get_application)):
    cfg = get_settings()
    try:
        if payload.mode == "builtin":
            if str(getattr(cfg, "aria2_mode", "external")) != "builtin":
                raise HTTPException(
                    400,
                    "Built-in aria2 starts after Apply Settings; apply the mode change before testing it",
                )
            result = await application.integration_admin("aria2").test()
        else:
            url = payload.url.strip() or str(cfg.aria2_url or "").strip()
            if not url:
                raise HTTPException(400, "No external aria2 RPC URL configured or entered")
            secret = "" if payload.clear_secret else (
                payload.secret.strip() or str(cfg.aria2_secret or "").strip()
            )
            service = Aria2Service(
                url,
                secret,
                getattr(cfg, "aria2_operation_timeout_seconds", 15),
            )
            result = await service.test()
        return {"ok": True, **result}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, _safe_failure(exc)) from exc


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
