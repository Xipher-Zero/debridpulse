import asyncio
import base64
import hashlib
import json
import logging
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urlparse

import aiohttp

from core.config import AppSettings, get_settings
from core.logging_utils import sanitize_exception, sanitize_log_value
import aiosqlite  # noqa: F401 — used by tests via unittest.mock.patch
from db.database import get_db
from services.alldebrid import AllDebridAPIError, AllDebridService, flatten_files
from services.aria2 import Aria2ConnectionError, Aria2DownloadStatus, Aria2RPCError, Aria2Service
from services.aria2_runtime import aria2_global_options, effective_rpc_config, is_builtin_mode
from services.extractor import archive_paths_from_downloads, get_extractor
from services.event_bus import publish
from services.notifications import NotificationService
from services.torrent_state import (
    TorrentStatus,
    assert_transition,
    is_terminal,
    is_active_download,
    POLL_EXCLUDED,
    TERMINAL,
    ACTIVE_DOWNLOAD,
)

logger = logging.getLogger("alldebrid.manager")

READY_CODE = 4
ERROR_CODES = set(range(5, 16))
UPLOAD_FAILED_CODE = 5  # AllDebrid statusCode 5 = "Upload failed"
EXPIRED_CODE = 3        # AllDebrid statusCode 3 = "Expired — files removed from cache"
DIRECT_LINK_SOURCE = "direct_link"
_PROVIDER_DELETE_OWNED_SOURCES = frozenset({"manual", "manual_file", "api"})
DEFERRED_PROVIDER_STATUS = "deferred"
DEFERRED_TORRENT_KIND = "torrent_file"
MAX_DIRECT_LINKS_PER_BATCH = 100


class TransientAllDebridStateError(Exception):
    """Raised when AllDebrid is temporarily inconsistent but not actually failed."""


MAX_FILE_RETRIES = 3
READY_FILE_RETRIES = 5
PROVIDER_FAILURE_THRESHOLD = 6


def extract_hash(magnet: str) -> Optional[str]:
    match = re.search(r"xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet, re.I)
    if not match:
        return None
    value = match.group(1)
    if len(value) == 32:
        try:
            value = base64.b32decode(value.upper()).hex()
        except Exception:
            return None
    return value.lower()


def safe_name(value: str) -> str:
    """Sanitise a torrent/folder name for use as a filesystem path component.
    Replaces forbidden characters, strips leading dots to prevent '..'-style
    names, and ensures the result is non-empty.
    """
    sanitised = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)[:200].strip()
    # Remove leading dots (prevents names like '..' or '...') while keeping
    # hidden-file dots elsewhere (e.g. 'Movie.2024.mkv' → unchanged)
    sanitised = sanitised.lstrip(".")
    return sanitised or "download"


def safe_rel_path(value: str) -> Path:
    raw = str(PurePosixPath(value.replace("\\", "/"))).strip("/")
    cleaned = [safe_name(part) for part in raw.split("/") if part not in {"", ".", ".."}]
    if not cleaned:
        return Path("download.bin")
    return Path(*cleaned)


def normalize_direct_links(values: List[str]) -> List[str]:
    """Validate and de-duplicate ordinary HTTP(S) links without fetching them."""
    normalized: List[str] = []
    seen: Set[str] = set()
    for raw in values or []:
        value = str(raw or "").strip()
        if not value:
            continue
        parsed = urlparse(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Invalid debrid link: {value[:120]}")
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    if not normalized:
        raise ValueError("At least one HTTP or HTTPS link is required")
    if len(normalized) > MAX_DIRECT_LINKS_PER_BATCH:
        raise ValueError(
            f"A maximum of {MAX_DIRECT_LINKS_PER_BATCH} links may be submitted at once"
        )
    return normalized


def direct_link_filename(url: str, fallback_index: int = 1) -> str:
    """Return a safe initial filename for a direct-link transaction."""
    parsed = urlparse(str(url or ""))
    candidate = unquote(PurePosixPath(parsed.path or "").name).strip()
    if not candidate:
        # Query-only hosters such as 1fichier encode the opaque file identity
        # in the leading bare query component, sometimes followed by ordinary
        # parameters (for example: ?<token>&af=...). Retain only that leading
        # opaque component and never expose key=value query parameters.
        raw_query = str(parsed.query or "").strip()
        leading_query_part = raw_query.split("&", 1)[0].strip()
        query_token = unquote(leading_query_part).strip()
        if query_token and "=" not in query_token and "&" not in query_token:
            candidate = f"{parsed.hostname or 'debrid-link'} - {query_token}"
        else:
            candidate = parsed.hostname or f"debrid-link-{fallback_index}"
    candidate = safe_name(candidate)
    return candidate or f"debrid-link-{fallback_index}"


def _direct_link_collection_base(filename: str) -> str:
    """Return a conservative collection stem for known multipart filenames."""
    name = safe_name(str(filename or "").strip())
    patterns = (
        r"(?i)^(?P<base>.+)\.part\d+\.rar$",
        r"(?i)^(?P<base>.+)\.r\d{2,3}$",
        r"(?i)^(?P<base>.+)\.(?:7z|zip|rar)\.\d{3}$",
    )
    for pattern in patterns:
        match = re.match(pattern, name)
        if match:
            base = match.group("base").rstrip(" .-_")
            if base:
                return base
    return name


def direct_link_collection_name(
    resolved_names: List[str], source_urls: List[str]
) -> str:
    """Build a useful parent label without inventing unavailable filenames."""
    urls = list(source_urls or [])
    total = len(urls)
    resolved = [
        safe_name(str(name))
        for name in (resolved_names or [])
        if str(name or "").strip()
    ]

    if total <= 0:
        return "Debrid links"

    if total == 1:
        return (
            resolved[0]
            if resolved
            else direct_link_filename(urls[0], 1)
        )

    if resolved:
        bases = [_direct_link_collection_base(name) for name in resolved]
        first_base = bases[0]
        if all(base.casefold() == first_base.casefold() for base in bases[1:]):
            return safe_name(f"{first_base} ({total} links)")

        return safe_name(f"{resolved[0]} + {total - 1} more")

    fallback = direct_link_filename(urls[0], 1)
    return safe_name(f"{fallback} + {total - 1} more")


def is_blocked(filename: str, cfg: AppSettings, size_bytes: int = 0) -> Tuple[bool, str]:
    if not cfg.filters_enabled:
        return False, ""
    ext = Path(filename).suffix.lower()
    if ext in [entry.lower() for entry in cfg.blocked_extensions]:
        return True, f"extension {ext}"
    for keyword in cfg.blocked_keywords:
        if keyword.lower() in filename.lower():
            return True, f"keyword '{keyword}'"
    if cfg.min_file_size_mb > 0 and size_bytes > 0 and size_bytes < cfg.min_file_size_mb * 1024 * 1024:
        return True, f"smaller than {cfg.min_file_size_mb} MB"
    # Smart File Selection: block common sample patterns
    if getattr(cfg, "block_samples", False):
        _sample_patterns = ["sample", "-sample.", ".sample.", "_sample_", "trailer", "-trailer.", "teaser"]
        fname_lower = filename.lower()
        if any(p in fname_lower for p in _sample_patterns):
            return True, "sample/trailer file"
    # Smart File Selection: block extras / featurettes
    if getattr(cfg, "block_extras", False):
        _extras_patterns = [
            "/extras/", "/featurettes/", "/behind the scenes/", "/deleted scenes/",
            "/interviews/", "/scenes/", "/shorts/", "/trailers/", "/specials/",
            "\\extras\\", "\\featurettes\\",
        ]
        fname_lower = filename.lower()
        if any(p in fname_lower for p in _extras_patterns):
            return True, "extras/featurette"
    return False, ""


def fmt_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size or 0)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.1f} {units[idx]}"


def _size_sum(items: List[dict]) -> int:
    return sum(int(item.get("size_bytes", 0) or 0) for item in items)


def _terminal_torrent_status(status: str) -> bool:
    return status in {"completed", "deleted", "error"}


def _safe_persisted_error(exc: BaseException, capability: str = "") -> str:
    """Never persist provider/download capability material from an exception."""
    raw = str(exc).strip() or repr(exc)
    exact = str(capability or "").strip()
    if exact:
        raw = raw.replace(exact, "<capability-url>")
    return sanitize_exception(Exception(raw), max_length=300)


def _aria2_status_rank(status: str) -> int:
    order = {
        "complete": 0,
        "removed": 1,
        "active": 2,
        "waiting": 3,
        "paused": 4,
        "error": 5,
    }
    return order.get((status or "").strip().lower(), 99)


def _normalize_aria2_path(path: str) -> str:
    if not path:
        return ""
    return str(PurePosixPath(str(path).replace("\\", "/"))).strip()


def normalize_provider_state(magnet: Dict) -> Dict[str, object]:
    code = int(magnet.get("statusCode", 0) or 0)
    size = int(magnet.get("size", 0) or 0)
    downloaded = int(magnet.get("downloaded", 0) or 0)
    progress = (downloaded / size * 100) if size > 0 else 0.0

    # Return plain string values so callers never accidentally write
    # "TorrentStatus.PROCESSING" (the Enum repr) into the database.
    if code == READY_CODE:
        provider_status = "ready"
        local_status = TorrentStatus.READY.value
    elif code == EXPIRED_CODE:
        provider_status = "expired"
        local_status = TorrentStatus.ERROR.value
    elif code in ERROR_CODES:
        provider_status = "error"
        local_status = TorrentStatus.ERROR.value
    elif code <= 0:
        provider_status = "queued"
        local_status = TorrentStatus.UPLOADING.value
    else:
        provider_status = "processing"
        local_status = TorrentStatus.PROCESSING.value

    return {
        "provider_status": provider_status,
        "local_status": local_status,
        "status_code": code,
        "progress": progress,
        "size_bytes": size,
        "message": str(magnet.get("status", "") or ""),
    }


async def _retry_async(
    fn, *args, attempts: int = MAX_FILE_RETRIES, delay: float = 1.0, retry_if=None
):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn(*args)
        except Exception as exc:
            last_error = exc
            if retry_if is not None and not retry_if(exc):
                break
            if attempt >= attempts:
                break
            await asyncio.sleep(delay * attempt)
    raise last_error


MAX_CONCURRENT_AD_UPLOADS = 5


class TorrentManager:
    def __init__(self):
        self._architecture = None
        self._ad: Optional[AllDebridService] = None
        self._aria2: Optional[Aria2Service] = None
        self._sem: Optional[asyncio.Semaphore] = None
        self._upload_sem: asyncio.Semaphore = asyncio.Semaphore(MAX_CONCURRENT_AD_UPLOADS)
        self._deferred_submission_lock = asyncio.Lock()
        self._active: Set[int] = set()
        self._direct_link_tasks: Set[asyncio.Task] = set()
        self._direct_link_task_ids: Set[int] = set()
        self._ready_parent_tasks: Set[asyncio.Task] = set()
        self._ready_parent_task_ids: Set[int] = set()
        self._maintenance_tasks: Set[asyncio.Task] = set()
        self._materialization_quiescing = False
        self._aria2_state_lock = asyncio.Lock()
        self._aria2_dispatch_lock = asyncio.Lock()
        self._aria2_ownership_lock = asyncio.Lock()
        self._aria2_ownership_ready = False
        self._aria2_owned_gid_cache: Set[str] = set()
        # Disk-space guard state
        self._disk_guard_active: bool = False          # True = guard triggered, new dispatches deferred

    def is_paused(self) -> bool:
        return bool(get_settings().paused)

    @staticmethod
    def _provider_delete_authorized(source: object) -> bool:
        """Automatic provider deletion requires explicit local-creation provenance."""
        normalized = str(source or "").strip()
        return normalized in _PROVIDER_DELETE_OWNED_SOURCES

    def set_materialization_quiescing(self, value: bool) -> None:
        self._materialization_quiescing = bool(value)

    def _track_maintenance_task(self, coro, *, label: str) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._maintenance_tasks.add(task)

        def _finished(done: asyncio.Task) -> None:
            self._maintenance_tasks.discard(done)
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error(
                    "Background materialization task %s failed: %s",
                    label,
                    sanitize_exception(exc, max_length=300),
                )

        task.add_done_callback(_finished)
        return task

    async def wait_for_materialization_idle(self) -> None:
        """Drain provider-triggered/materialization tasks before destructive maintenance."""
        while True:
            tasks = [
                task
                for task in (
                    *self._direct_link_tasks,
                    *self._ready_parent_tasks,
                    *self._maintenance_tasks,
                )
                if not task.done()
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                continue
            if self._active:
                await asyncio.sleep(0.05)
                continue
            async with self._deferred_submission_lock:
                pass
            if not self._active and not any(
                not task.done()
                for task in (
                    *self._direct_link_tasks,
                    *self._ready_parent_tasks,
                    *self._maintenance_tasks,
                )
            ):
                return

    def notify(self):
        if self._architecture is not None:
            return self._architecture.notifications.client()
        return self._engine_notify()

    def reset_services(self):
        self._ad = None
        self._aria2 = None
        self._sem = None
        # A settings/database transition may change the durable ownership
        # source. Rebuild the cache lazily on the next external aria2 access.
        self._aria2_ownership_ready = False
        self._aria2_owned_gid_cache.clear()

    def ad(self) -> AllDebridService:
        if self._ad is None:
            cfg = get_settings()
            self._ad = AllDebridService(cfg.alldebrid_api_key, cfg.alldebrid_agent)
        return self._ad

    def aria2(self) -> Aria2Service:
        if self._aria2 is None:
            cfg = get_settings()
            url, secret = effective_rpc_config(cfg)
            self._aria2 = Aria2Service(url, secret, cfg.aria2_operation_timeout_seconds)
        return self._aria2

    async def _ensure_aria2_ownership_table(self) -> None:
        """Create the persistent ledger used to prove ADC ownership of GIDs."""
        if self._aria2_ownership_ready:
            return
        async with self._aria2_ownership_lock:
            if self._aria2_ownership_ready:
                return
            async with get_db() as db:
                await db.execute(
                    """CREATE TABLE IF NOT EXISTS debridpulse_aria2_owned_gids (
                           gid TEXT PRIMARY KEY,
                           download_file_id INTEGER,
                           torrent_id INTEGER,
                           created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                       )"""
                )
                # Bootstrap ownership for jobs created before this ledger
                # existed, while the original DB association still proves it.
                await db.execute(
                    """INSERT INTO debridpulse_aria2_owned_gids
                           (gid, download_file_id, torrent_id)
                       SELECT download_id, id, torrent_id
                         FROM download_files
                        WHERE download_client='aria2'
                          AND download_id IS NOT NULL
                       ON CONFLICT(gid) DO NOTHING"""
                )
                rows = await db.fetchall(
                    """SELECT gid
                         FROM debridpulse_aria2_owned_gids
                        WHERE gid IS NOT NULL
                       UNION
                       SELECT download_id AS gid
                         FROM download_files
                        WHERE download_client='aria2'
                          AND download_id IS NOT NULL"""
                )
                await db.commit()
            self._aria2_owned_gid_cache = {
                str(row["gid"]).strip()
                for row in rows
                if str(row.get("gid") or "").strip()
            }
            self._aria2_ownership_ready = True

    async def _record_aria2_owned_gid(
        self,
        gid: str,
        *,
        download_file_id: Optional[int] = None,
        torrent_id: Optional[int] = None,
    ) -> None:
        """Persist a GID immediately after ADC creates or reuses its own job."""
        gid = str(gid or "").strip()
        if not gid:
            raise ValueError("Cannot record an empty aria2 GID")
        await self._ensure_aria2_ownership_table()
        async with get_db() as db:
            await db.execute(
                """INSERT INTO debridpulse_aria2_owned_gids
                       (gid, download_file_id, torrent_id)
                   VALUES (?, ?, ?)
                   ON CONFLICT(gid) DO UPDATE SET
                       download_file_id=excluded.download_file_id,
                       torrent_id=excluded.torrent_id""",
                (gid, download_file_id, torrent_id),
            )
            await db.commit()
        self._aria2_owned_gid_cache.add(gid)

    async def _aria2_owned_gids(self) -> Set[str]:
        """Return a copy of the durable DebridPulse aria2 ownership cache."""
        await self._ensure_aria2_ownership_table()
        return set(self._aria2_owned_gid_cache)

    async def _aria2_owned_downloads(self, downloads) -> List:
        if is_builtin_mode():
            return list(downloads)
        owned_gids = await self._aria2_owned_gids()
        return [dl for dl in downloads if str(dl.gid) in owned_gids]

    def _aria2_job_options(self, base: Optional[Dict] = None) -> Dict[str, str]:
        """Build ADC transfer policy for one addUri request."""
        cfg = get_settings()
        options = {str(k): str(v) for k, v in dict(base or {}).items()}
        options.update({
            # DebridPulse owns the deterministic target path for each file.
            # Never let aria2 create file.1.ext after a transfer is removed
            # from the application and later submitted again.  An existing
            # partial with its control file may still resume; otherwise aria2
            # replaces the prior file at the requested path.
            "allow-overwrite": "true",
            "auto-file-renaming": "false",
            "split": str(max(1, int(getattr(cfg, "aria2_split", 1) or 1))),
            "min-split-size": str(
                getattr(cfg, "aria2_min_split_size", "10M") or "10M"
            ),
            "max-connection-per-server": str(
                max(
                    1,
                    int(
                        getattr(
                            cfg,
                            "aria2_max_connection_per_server",
                            1,
                        )
                        or 1
                    ),
                )
            ),
            "continue": (
                "true"
                if bool(getattr(cfg, "aria2_continue_downloads", True))
                else "false"
            ),
        })
        return options

    async def _remove_owned_aria2_gid(self, gid: str) -> bool:
        """Cancel an ADC-owned live job without erasing external result history."""
        gid = str(gid or "").strip()
        if not gid:
            return False
        if not is_builtin_mode():
            if gid not in await self._aria2_owned_gids():
                logger.warning(
                    "Blocked attempt to remove foreign aria2 GID %s", gid
                )
                return False
            try:
                status = await self.aria2().tell_status(gid)
            except Exception as exc:
                logger.debug(
                    "aria2 ownership check could not resolve GID %s: %s",
                    gid,
                    exc,
                )
                return False
            if status.status not in {"active", "waiting", "paused"}:
                logger.debug(
                    "Preserved stopped aria2 result %s (%s)",
                    gid,
                    status.status,
                )
                return False
        await self.aria2().remove(gid)
        return True

    async def _engine_control_aria2_gid(self, gid: str, action: str) -> dict:
        """Apply a user action only to a GID owned by DebridPulse."""
        gid = str(gid or "").strip()
        if action not in {"pause", "resume", "remove"}:
            raise ValueError("Unsupported aria2 action")
        if not is_builtin_mode() and gid not in await self._aria2_owned_gids():
            raise PermissionError(
                f"aria2 GID {gid} is not owned by DebridPulse"
            )
        if action == "remove":
            mutated = await self._remove_owned_aria2_gid(gid)
            return {
                "mutated": mutated,
                "result_preserved": not is_builtin_mode(),
            }
        status = await self.aria2().tell_status(gid)
        if status.status not in {"active", "waiting", "paused"}:
            return {
                "mutated": False,
                "reason": f"aria2 GID is already {status.status}",
            }
        if action == "pause":
            await self.aria2().pause(gid)
        else:
            await self.aria2().resume(gid)
        return {"mutated": True}

    def sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(get_settings().max_concurrent_downloads)
        return self._sem

    def _engine_notify(self) -> NotificationService:
        cfg = get_settings()
        return NotificationService(
            webhook_url=cfg.discord_webhook_url,
            added_webhook_url=getattr(cfg, "discord_webhook_added", ""),
        )

    async def _notify_provider_error(
        self,
        name: str,
        reason: str,
        *,
        context: str = "",
        source: str = "AllDebrid",
        provider: str = "AllDebrid",
        alldebrid_id: str = "",
        status_code: int | str | None = None,
    ) -> None:
        cfg = get_settings()
        if not getattr(cfg, "discord_notify_error", False):
            return
        await self.notify().send_error(
            name,
            reason=reason,
            context=context,
            source=source,
            provider=provider,
            alldebrid_id=str(alldebrid_id or ""),
            status_code="" if status_code is None else str(status_code),
        )

    def download_client_name(self) -> str:
        return "aria2"

    async def add_magnet_direct(self, magnet: str, source: str = "manual") -> dict:
        hash_value = extract_hash(magnet)
        if not hash_value:
            raise ValueError("Invalid magnet: no btih hash found")

        # ── Duplicate gate (before any AllDebrid contact) ──────────────────
        from services.duplicates import DuplicateCandidate, check_before_add
        decision = await check_before_add(DuplicateCandidate(
            source=source,
            magnet=magnet,
            infohash=hash_value,
        ))
        if decision.action == "skip":
            existing = decision.matches[0] if decision.matches else None
            result = {}
            if existing:
                try:
                    async with get_db() as db:
                        row = await db.fetchone(
                            "SELECT * FROM torrents WHERE id=?", (existing.torrent_id,)
                        )
                    result = dict(row) if row else {}
                except Exception:
                    pass
            result["_duplicate"] = decision.as_dict()
            return result
        # ──────────────────────────────────────────────────────────────────

        result = await self._add_magnet(
            magnet, hash_value, source, duplicate_check=False
        )
        if decision.action == "warn":
            result["_duplicate"] = decision.as_dict()
        return result

    async def _persist_deferred_magnet(
        self, magnet: str, hash_value: str, source: str
    ) -> dict:
        """Persist magnet intake without contacting AllDebrid while Pause All is active."""
        name = hash_value[:16]
        async with get_db() as db:
            await db.execute(
                """INSERT INTO torrents
                       (hash, magnet, name, status, source, provider_status,
                        progress, download_client, error_message, alldebrid_id)
                   VALUES (?, ?, ?, 'paused', ?, ?, 0, 'aria2', NULL, NULL)
                   ON CONFLICT(hash) DO UPDATE SET
                       magnet=excluded.magnet,
                       name=excluded.name,
                       source=excluded.source,
                       status='paused',
                       provider_status=excluded.provider_status,
                       provider_status_code=NULL,
                       polling_failures=0,
                       progress=0,
                       error_message=NULL,
                       alldebrid_id=NULL,
                       completed_at=NULL,
                       updated_at=CURRENT_TIMESTAMP""",
                (hash_value, magnet, name, source, DEFERRED_PROVIDER_STATUS),
            )
            row = await db.fetchone("SELECT * FROM torrents WHERE hash=?", (hash_value,))
            if not row:
                raise RuntimeError("Could not persist deferred magnet submission")
            torrent_id = int(row["id"])
            await db.execute("DELETE FROM download_files WHERE torrent_id=?", (torrent_id,))
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                (
                    torrent_id,
                    "Accepted while Pause All is active; queued for AllDebrid upload on resume",
                ),
            )
            await db.commit()
        result = dict(row)
        result["_deferred"] = True
        return result

    async def _persist_deferred_torrent_file(
        self,
        file_bytes: bytes,
        filename: str,
        source: str,
        local_hash: str,
    ) -> dict:
        """Persist a .torrent payload so paused intake survives restart."""
        if not local_hash:
            raise ValueError(
                "Could not determine torrent infohash; cannot queue this .torrent while processing is paused"
            )
        name = Path(filename or "upload.torrent").stem or local_hash[:16]
        async with get_db() as db:
            await db.execute(
                """INSERT INTO torrents
                       (hash, name, status, source, provider_status, progress,
                        download_client, error_message, alldebrid_id)
                   VALUES (?, ?, 'paused', ?, ?, 0, 'aria2', NULL, NULL)
                   ON CONFLICT(hash) DO UPDATE SET
                       name=excluded.name,
                       source=excluded.source,
                       status='paused',
                       provider_status=excluded.provider_status,
                       provider_status_code=NULL,
                       polling_failures=0,
                       progress=0,
                       error_message=NULL,
                       alldebrid_id=NULL,
                       completed_at=NULL,
                       updated_at=CURRENT_TIMESTAMP""",
                (local_hash, name, source, DEFERRED_PROVIDER_STATUS),
            )
            row = await db.fetchone("SELECT * FROM torrents WHERE hash=?", (local_hash,))
            if not row:
                raise RuntimeError("Could not persist deferred torrent-file submission")
            torrent_id = int(row["id"])
            await db.execute("DELETE FROM download_files WHERE torrent_id=?", (torrent_id,))
            await db.execute(
                """INSERT INTO deferred_provider_submissions
                       (torrent_id, kind, payload, filename, source, created_at)
                   VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(torrent_id) DO UPDATE SET
                       kind=excluded.kind,
                       payload=excluded.payload,
                       filename=excluded.filename,
                       source=excluded.source,
                       created_at=CURRENT_TIMESTAMP""",
                (
                    torrent_id,
                    DEFERRED_TORRENT_KIND,
                    bytes(file_bytes),
                    filename or "upload.torrent",
                    source,
                ),
            )
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                (
                    torrent_id,
                    "Accepted .torrent while Pause All is active; queued for AllDebrid upload on resume",
                ),
            )
            await db.commit()
        result = dict(row)
        result["_deferred"] = True
        return result

    async def _upload_torrent_file_provider(
        self,
        file_bytes: bytes,
        filename: str,
        source: str,
        local_hash: str,
        *,
        deferred_torrent_id: Optional[int] = None,
    ) -> dict:
        if self.is_paused():
            if deferred_torrent_id is not None:
                async with get_db() as db:
                    row = await db.fetchone(
                        "SELECT * FROM torrents WHERE id=?", (int(deferred_torrent_id),)
                    )
                result = dict(row or {"id": int(deferred_torrent_id)})
                result["_deferred"] = True
                return result
            return await self._persist_deferred_torrent_file(
                file_bytes, filename, source, local_hash
            )

        async with self._upload_sem:
            if self.is_paused():
                if deferred_torrent_id is not None:
                    async with get_db() as db:
                        row = await db.fetchone(
                            "SELECT * FROM torrents WHERE id=?", (int(deferred_torrent_id),)
                        )
                    result = dict(row or {"id": int(deferred_torrent_id)})
                    result["_deferred"] = True
                    return result
                return await self._persist_deferred_torrent_file(
                    file_bytes, filename, source, local_hash
                )
            result = await self.ad().upload_torrent_file(
                file_bytes, filename or "upload.torrent"
            )

        ad_id = str(result.get("id", ""))
        name = (
            result.get("name")
            or result.get("filename")
            or Path(filename or "upload.torrent").stem
        )
        hash_value = str(local_hash or result.get("hash", ad_id) or ad_id).strip().lower()
        logger.info("Torrent file uploaded %s (ad_id=%s)", name, ad_id)

        if deferred_torrent_id is None:
            row = await self._upsert(hash_value, None, name, ad_id, source)
        else:
            async with get_db() as db:
                await db.execute(
                    """UPDATE torrents
                       SET hash=?, name=?, alldebrid_id=?, status='uploading',
                           source=?, provider_status='queued', provider_status_code=NULL,
                           polling_failures=0, progress=0, error_message=NULL,
                           completed_at=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (
                        hash_value,
                        name,
                        ad_id,
                        source,
                        int(deferred_torrent_id),
                    ),
                )
                await db.execute(
                    "DELETE FROM deferred_provider_submissions WHERE torrent_id=?",
                    (int(deferred_torrent_id),),
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                    (
                        int(deferred_torrent_id),
                        f"Uploaded deferred .torrent to AllDebrid (id={ad_id})",
                    ),
                )
                await db.commit()
                row = await db.fetchone(
                    "SELECT * FROM torrents WHERE id=?", (int(deferred_torrent_id),)
                )
            row = dict(row or {})

        if get_settings().discord_notify_added:
            await self.notify().send_added(name, source=source, alldebrid_id=ad_id)

        status_code = int(result.get("statusCode") or result.get("status_code") or 0)
        if status_code == READY_CODE:
            logger.info(
                "Fast-path: %s already ready on AllDebrid (cached torrent file) — starting immediately",
                sanitize_log_value(name[:60]),
            )
            torrent_id = row.get("id")
            if torrent_id:
                self._schedule_ready_parent_download(int(torrent_id), ad_id, name)

        return row

    async def add_torrent_file_direct(
        self,
        file_bytes: bytes,
        filename: str,
        source: str = "manual",
        preferred_hash: Optional[str] = None,
    ) -> dict:
        if not get_settings().alldebrid_api_key:
            raise Exception("AllDebrid API key not configured")
        if not file_bytes:
            raise ValueError("Empty torrent file")

        local_hash = preferred_hash or ""
        if not local_hash:
            try:
                from services.alldebrid import extract_hash_from_torrent
                local_hash = extract_hash_from_torrent(file_bytes) or ""
            except Exception as exc:
                logger.debug("Failed to extract hash from torrent file: %s", exc)

        from services.duplicates import DuplicateCandidate, check_before_add
        decision = await check_before_add(DuplicateCandidate(
            source=source,
            infohash=local_hash,
            title=Path(filename or "").stem,
        ))
        if decision.action == "skip":
            existing = decision.matches[0] if decision.matches else None
            result: dict = {}
            if existing:
                try:
                    async with get_db() as db:
                        row = await db.fetchone(
                            "SELECT * FROM torrents WHERE id=?", (existing.torrent_id,)
                        )
                    result = dict(row) if row else {}
                except Exception:
                    pass
            result["_duplicate"] = decision.as_dict()
            return result

        result = await self._upload_torrent_file_provider(
            file_bytes, filename, source, local_hash
        )
        if decision.action == "warn":
            result["_duplicate"] = decision.as_dict()
        return result

    async def add_direct_links(self, links: List[str]) -> dict:
        """Create one tracked transfer collection from ordinary hoster URLs."""
        if not get_settings().alldebrid_api_key:
            raise Exception("AllDebrid API key not configured")

        normalized = normalize_direct_links(links)
        initial_name = (
            direct_link_filename(normalized[0])
            if len(normalized) == 1
            else f"Debrid link batch ({len(normalized)} links)"
        )
        payload = json.dumps(normalized, separators=(",", ":"))
        nonce = uuid.uuid4().hex
        collection_hash = "direct:" + hashlib.sha256(
            f"{nonce}:{payload}".encode("utf-8")
        ).hexdigest()

        async with get_db() as db:
            torrent_id = await db.execute_returning_id(
                """INSERT INTO torrents
                       (hash, name, magnet, status, source, provider_status,
                        progress, download_client, error_message)
                   VALUES (?, ?, ?, 'processing', ?, 'submitted', 0, 'aria2', NULL)""",
                (
                    collection_hash,
                    initial_name,
                    payload,
                    DIRECT_LINK_SOURCE,
                ),
            )
            if not torrent_id:
                raise RuntimeError("Could not create the debrid-link transaction")
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                (
                    torrent_id,
                    f"Accepted {len(normalized)} direct link(s)",
                ),
            )
            await db.commit()
            row = await db.fetchone("SELECT * FROM torrents WHERE id=?", (torrent_id,))

        if self.is_paused():
            async with get_db() as db:
                await db.execute(
                    """UPDATE torrents
                       SET status='paused', provider_status=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (DEFERRED_PROVIDER_STATUS, int(torrent_id)),
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                    (
                        int(torrent_id),
                        "Pause All is active; direct-link generation is queued for resume",
                    ),
                )
                await db.commit()
                row = await db.fetchone("SELECT * FROM torrents WHERE id=?", (torrent_id,))
            await self._broadcast_direct_link_update(
                int(torrent_id), "paused", initial_name, 0.0
            )
            return {
                **dict(row or {}),
                "accepted_links": len(normalized),
                "_deferred": True,
            }

        await self._broadcast_direct_link_update(
            int(torrent_id), "processing", initial_name, 0.0
        )
        self._schedule_direct_link_collection(int(torrent_id), normalized)
        return {**dict(row or {}), "accepted_links": len(normalized)}

    def _schedule_direct_link_collection(
        self, torrent_id: int, links: List[str]
    ) -> None:
        """Keep a strong reference to the background preparation task."""
        if self._materialization_quiescing:
            return
        if torrent_id in self._active or torrent_id in self._direct_link_task_ids:
            return
        task = asyncio.create_task(
            self._prepare_direct_link_collection(torrent_id, links)
        )
        self._direct_link_tasks.add(task)
        self._direct_link_task_ids.add(torrent_id)

        def _finished(done: asyncio.Task) -> None:
            self._direct_link_tasks.discard(done)
            self._direct_link_task_ids.discard(torrent_id)
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error(
                    "Direct-link preparation task failed for transfer %s: %s",
                    torrent_id,
                    sanitize_exception(exc, max_length=300),
                )

        task.add_done_callback(_finished)

    async def _broadcast_direct_link_update(
        self,
        torrent_id: int,
        status: str,
        name: str,
        progress: float,
    ) -> None:
        try:
            await publish(
                "torrent_updated",
                {
                    "id": torrent_id,
                    "status": status,
                    "name": name,
                    "progress": progress,
                    "source": DIRECT_LINK_SOURCE,
                },
            )
        except Exception as exc:
            logger.debug(
                "Direct-link event publication failed for transfer %s: %s",
                torrent_id,
                exc,
            )

    @staticmethod
    def _unique_direct_link_path(
        root: Path,
        filename: str,
        reserved: Set[str],
        *,
        reuse_existing: bool = False,
    ) -> Path:
        """Choose a direct-link target without overwriting unrelated content."""
        candidate = root / safe_name(filename)
        stem = candidate.stem or "download"
        suffix = candidate.suffix
        counter = 2
        while (
            str(candidate).lower() in reserved
            or (candidate.exists() and not reuse_existing)
        ):
            candidate = root / f"{stem} ({counter}){suffix}"
            counter += 1
        reserved.add(str(candidate).lower())
        return candidate

    async def _prepare_direct_link_collection(
        self, torrent_id: int, links: List[str]
    ) -> None:
        """Generate AllDebrid URLs and stage their files for the aria2 dispatcher."""
        if self._materialization_quiescing:
            return
        if self.is_paused():
            async with get_db() as db:
                current = await db.fetchone(
                    "SELECT status, provider_status, name FROM torrents WHERE id=?",
                    (torrent_id,),
                )
                if current and current["status"] not in {"completed", "deleted", "error"}:
                    was_deferred = str(current.get("provider_status") or "") == DEFERRED_PROVIDER_STATUS
                    await db.execute(
                        """UPDATE torrents
                           SET status='paused', provider_status=?, updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (DEFERRED_PROVIDER_STATUS, torrent_id),
                    )
                    if not was_deferred:
                        await db.execute(
                            "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                            (torrent_id, "Pause All deferred direct-link generation until resume"),
                        )
                    await db.commit()
                    await self._broadcast_direct_link_update(
                        torrent_id,
                        "paused",
                        str(current.get("name") or "Debrid links"),
                        0.0,
                    )
            return
        if torrent_id in self._active:
            return
        self._active.add(torrent_id)
        try:
            normalized = normalize_direct_links(links)
            cfg = get_settings()
            output_root = Path(cfg.download_folder)
            reusable_source_urls: Set[str] = set()
            protected_live_paths: Set[str] = set()

            async with get_db() as db:
                current = await db.fetchone(
                    "SELECT status, name FROM torrents WHERE id=?", (torrent_id,)
                )
                if not current or current["status"] in {"completed", "deleted"}:
                    return
                live_path_rows = await db.fetchall(
                    """SELECT DISTINCT f.local_path
                         FROM download_files f
                         JOIN torrents t ON t.id=f.torrent_id
                        WHERE t.status!='deleted'
                          AND t.id!=?
                          AND f.local_path IS NOT NULL
                          AND f.local_path!=''""",
                    (torrent_id,),
                )
                protected_live_paths = {
                    str(row["local_path"] or "").strip().lower()
                    for row in live_path_rows
                    if str(row["local_path"] or "").strip()
                }
                if normalized:
                    placeholders = ",".join("?" for _ in normalized)
                    previous_rows = await db.fetchall(
                        f"""SELECT DISTINCT f.source_url
                              FROM download_files f
                              JOIN torrents t ON t.id=f.torrent_id
                             WHERE t.source=? AND t.status='deleted'
                               AND f.source_url IN ({placeholders})""",
                        (DIRECT_LINK_SOURCE, *normalized),
                    )
                    reusable_source_urls = {
                        str(row["source_url"] or "").strip()
                        for row in previous_rows
                        if str(row["source_url"] or "").strip()
                    }
                await db.execute(
                    "DELETE FROM download_files WHERE torrent_id=?", (torrent_id,)
                )
                await db.execute(
                    """UPDATE torrents
                       SET status='processing', provider_status='unlocking',
                           progress=0, size_bytes=0, error_message=NULL,
                           completed_at=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (torrent_id,),
                )
                file_rows: List[dict] = []
                for index, source_url in enumerate(normalized, start=1):
                    provisional = direct_link_filename(source_url, index)
                    file_id = await db.execute_returning_id(
                        """INSERT INTO download_files
                               (torrent_id, filename, size_bytes, source_url,
                                download_url, local_path, status, download_client,
                                blocked, updated_at)
                           VALUES (?, ?, 0, ?, NULL, NULL, 'unlocking', 'aria2', 0,
                                   CURRENT_TIMESTAMP)""",
                        (torrent_id, provisional, source_url),
                    )
                    file_rows.append(
                        {
                            "file_id": int(file_id),
                            "source_url": source_url,
                            "index": index,
                        }
                    )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                    (torrent_id, "AllDebrid is generating direct download links"),
                )
                await db.commit()

            await self._broadcast_direct_link_update(
                torrent_id,
                "processing",
                str(current.get("name") or "Debrid links"),
                0.0,
            )

            unlock_sem = asyncio.Semaphore(3)

            async def _unlock(row: dict) -> dict:
                async with unlock_sem:
                    try:
                        generated = await _retry_async(
                            self.ad().unlock_link,
                            row["source_url"],
                            retry_if=lambda exc: not (
                                isinstance(exc, AllDebridAPIError)
                                and exc.code == "LINK_DOWN"
                            ),
                        )
                        generated_url = str(generated.get("link") or "").strip()
                        parsed = urlparse(generated_url)
                        if (
                            parsed.scheme.lower() not in {"http", "https"}
                            or not parsed.netloc
                        ):
                            raise Exception("AllDebrid returned no usable download URL")
                        filename = (
                            generated.get("filename")
                            or generated.get("name")
                            or direct_link_filename(
                                row["source_url"], row["index"]
                            )
                        )
                        size_bytes = int(
                            generated.get("filesize")
                            or generated.get("size")
                            or 0
                        )
                        return {
                            **row,
                            "generated_url": generated_url,
                            "filename": safe_name(str(filename)),
                            "size_bytes": max(0, size_bytes),
                            "error": None,
                            "missing": False,
                        }
                    except Exception as exc:
                        return {
                            **row,
                            "generated_url": "",
                            "filename": direct_link_filename(
                                row["source_url"], row["index"]
                            ),
                            "size_bytes": 0,
                            "error": _safe_persisted_error(exc, row["source_url"]),
                            "missing": (
                                isinstance(exc, AllDebridAPIError)
                                and exc.code == "LINK_DOWN"
                            ),
                        }

            results = await asyncio.gather(*[_unlock(row) for row in file_rows])
            # A deleted transfer releases its old filename, but any non-deleted
            # transfer still owns its persisted local path. This prevents stale
            # deleted-source history from bypassing a live filename collision.
            reserved_paths: Set[str] = set(protected_live_paths)
            succeeded = 0
            failed = 0
            missing = 0
            total_size = 0
            resolved_names: List[str] = []
            failed_updates: List[tuple] = []
            success_updates: List[tuple] = []
            generation_events: List[tuple] = []

            for position, result in enumerate(results, start=1):
                if result["error"]:
                    failed += 1
                    is_missing = bool(result.get("missing"))
                    if is_missing:
                        missing += 1
                    failure_status = "missing" if is_missing else "error"
                    failure_reason = (
                        "File is no longer available on the source host"
                        if is_missing
                        else result["error"]
                    )
                    failed_updates.append(
                        (failure_status, failure_reason, result["file_id"])
                    )
                    generation_events.append(
                        (
                            torrent_id,
                            "error",
                            f"AllDebrid could not generate link {position}: {failure_reason}",
                        )
                    )
                else:
                    succeeded += 1
                    total_size += int(result["size_bytes"] or 0)
                    resolved_names.append(result["filename"])
                    local_path = self._unique_direct_link_path(
                        output_root,
                        result["filename"],
                        reserved_paths,
                        reuse_existing=(
                            result["source_url"] in reusable_source_urls
                        ),
                    )
                    success_updates.append(
                        (
                            result["filename"],
                            result["size_bytes"],
                            result["generated_url"],
                            str(local_path),
                            result["file_id"],
                        )
                    )

            if failed_updates or success_updates or generation_events:
                async with get_db() as db:
                    if failed_updates:
                        await db.executemany(
                            """UPDATE download_files
                               SET status=?, block_reason=?,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE id=?""",
                            failed_updates,
                        )
                    if success_updates:
                        await db.executemany(
                            """UPDATE download_files
                               SET filename=?, size_bytes=?, download_url=?,
                                   local_path=?, status='pending', block_reason=NULL,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE id=?""",
                            success_updates,
                        )
                    if generation_events:
                        await db.executemany(
                            "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                            generation_events,
                        )
                    await db.commit()

            final_name = direct_link_collection_name(
                resolved_names, normalized
            )

            if succeeded:
                queue_status = "paused" if self.is_paused() else "queued"
                error_message = (
                    f"{failed} of {len(normalized)} links could not be generated"
                    if failed
                    else None
                )
                event_level = "warn" if failed else "info"
                queue_action = (
                    "parked by Pause All"
                    if queue_status == "paused"
                    else "queued for aria2"
                )
                event_message = (
                    f"Generated {succeeded} of {len(normalized)} AllDebrid links; "
                    f"{queue_action}"
                )
                async with get_db() as db:
                    if queue_status == "paused":
                        await db.execute(
                            """UPDATE download_files
                               SET status='paused', updated_at=CURRENT_TIMESTAMP
                               WHERE torrent_id=? AND status='pending'""",
                            (torrent_id,),
                        )
                    await db.execute(
                        """UPDATE torrents
                           SET name=?, status=?, provider_status='ready',
                               size_bytes=?, progress=0, error_message=?,
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (
                            final_name,
                            queue_status,
                            total_size,
                            error_message,
                            torrent_id,
                        ),
                    )
                    await db.execute(
                        "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                        (torrent_id, event_level, event_message),
                    )
                    await db.commit()
                await self._broadcast_direct_link_update(
                    torrent_id, queue_status, final_name, 0.0
                )
                await self.advance_aria2_queue()
            else:
                all_missing = failed > 0 and missing == failed
                message = (
                    "File is no longer available on the source host"
                    if all_missing and len(normalized) == 1
                    else (
                        f"{missing} submitted files are no longer available on their source hosts"
                        if all_missing
                        else "All submitted links failed during AllDebrid generation"
                    )
                )
                provider_status = "missing" if all_missing else "error"
                async with get_db() as db:
                    await db.execute(
                        """UPDATE torrents
                           SET name=?, status='error', provider_status=?,
                               error_message=?, progress=0,
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (final_name, provider_status, message, torrent_id),
                    )
                    await db.execute(
                        "INSERT INTO events (torrent_id, level, message) VALUES (?, 'error', ?)",
                        (torrent_id, message),
                    )
                    await db.commit()
                await self._broadcast_direct_link_update(
                    torrent_id, "error", final_name, 0.0
                )
        except Exception as exc:
            message = sanitize_exception(exc, max_length=500)
            logger.error(
                "Direct-link preparation failed for transfer %s: %s",
                torrent_id,
                message,
            )
            async with get_db() as db:
                await db.execute(
                    """UPDATE torrents
                       SET status='error', provider_status='error',
                           error_message=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status != 'deleted'""",
                    (message, torrent_id),
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'error', ?)",
                    (torrent_id, f"Direct-link generation failed: {message}"),
                )
                await db.commit()
            await self._broadcast_direct_link_update(
                torrent_id, "error", "Debrid links", 0.0
            )
        finally:
            self._active.discard(torrent_id)

    async def retry_direct_link_collection(self, torrent_id: int) -> dict:
        """Regenerate every URL in an existing direct-link collection."""
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT * FROM torrents WHERE id=?", (torrent_id,)
            )
            if not row:
                raise ValueError("Transfer not found")
            if str(row.get("source") or "") != DIRECT_LINK_SOURCE:
                raise ValueError("Transfer is not a direct-link collection")
            try:
                links = normalize_direct_links(json.loads(row.get("magnet") or "[]"))
            except Exception as exc:
                raise ValueError("Stored direct-link payload is invalid") from exc
            gids = await db.fetchall(
                """SELECT download_id FROM download_files
                   WHERE torrent_id=? AND download_id IS NOT NULL""",
                (torrent_id,),
            )
            await db.execute(
                """UPDATE torrents
                   SET status='processing', provider_status='submitted',
                       error_message=NULL, progress=0, completed_at=NULL,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (torrent_id,),
            )
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                (torrent_id, "Manual retry — regenerating direct links through AllDebrid"),
            )
            await db.commit()

        for gid_row in gids:
            await self._remove_owned_aria2_gid(str(gid_row["download_id"] or ""))
        self._schedule_direct_link_collection(torrent_id, links)
        await self._broadcast_direct_link_update(
            torrent_id,
            "processing",
            str(row.get("name") or "Debrid links"),
            0.0,
        )
        return {"ok": True, "new_status": "processing", "link_count": len(links)}

    async def recover_direct_link_collections(self) -> int:
        """Resume direct-link generation interrupted before aria2 queueing."""
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT id, magnet FROM torrents
                   WHERE source=?
                     AND status IN ('processing', 'uploading', 'ready')""",
                (DIRECT_LINK_SOURCE,),
            )
        recovered = 0
        for row in rows:
            try:
                links = normalize_direct_links(json.loads(row.get("magnet") or "[]"))
            except Exception as exc:
                logger.warning(
                    "Could not recover direct-link transfer %s: %s",
                    row["id"],
                    exc,
                )
                continue
            self._schedule_direct_link_collection(int(row["id"]), links)
            recovered += 1
        return recovered

    async def resume_deferred_provider_submissions(self) -> dict:
        """Start provider work that was durably accepted while Pause All was active."""
        if self._materialization_quiescing:
            return {"started": 0, "failed": 0}
        if self.is_paused():
            return {"started": 0, "failed": 0}

        async with self._deferred_submission_lock:
            if self.is_paused():
                return {"started": 0, "failed": 0}
            async with get_db() as db:
                rows = await db.fetchall(
                    """SELECT t.*,
                              d.kind AS deferred_kind,
                              d.payload AS deferred_payload,
                              d.filename AS deferred_filename,
                              d.source AS deferred_source
                         FROM torrents t
                         LEFT JOIN deferred_provider_submissions d
                           ON d.torrent_id=t.id
                        WHERE t.provider_status=?
                          AND t.status NOT IN ('paused','completed','deleted','error')
                        ORDER BY t.priority DESC, t.id ASC""",
                    (DEFERRED_PROVIDER_STATUS,),
                )

            started = failed = 0
            for row in rows:
                if self.is_paused():
                    break
                torrent_id = int(row["id"])
                try:
                    async with get_db() as db:
                        current = await db.fetchone(
                            "SELECT status, provider_status FROM torrents WHERE id=?",
                            (torrent_id,),
                        )
                    if (
                        not current
                        or current["status"] == "paused"
                        or str(current.get("provider_status") or "") != DEFERRED_PROVIDER_STATUS
                    ):
                        continue

                    if str(row.get("source") or "") == DIRECT_LINK_SOURCE:
                        links = normalize_direct_links(
                            json.loads(row.get("magnet") or "[]")
                        )
                        async with get_db() as db:
                            await db.execute(
                                """UPDATE torrents
                                   SET status='processing', provider_status='submitted',
                                       error_message=NULL, updated_at=CURRENT_TIMESTAMP
                                   WHERE id=? AND provider_status=? AND status!='paused'""",
                                (torrent_id, DEFERRED_PROVIDER_STATUS),
                            )
                            await db.execute(
                                "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                                (torrent_id, "Pause All released; starting deferred direct-link generation"),
                            )
                            await db.commit()
                        self._schedule_direct_link_collection(torrent_id, links)
                        started += 1
                        continue

                    if str(row.get("deferred_kind") or "") == DEFERRED_TORRENT_KIND:
                        payload = row.get("deferred_payload")
                        if isinstance(payload, memoryview):
                            payload = payload.tobytes()
                        if not isinstance(payload, (bytes, bytearray)) or not payload:
                            raise ValueError("Deferred .torrent payload is missing")
                        result = await self._upload_torrent_file_provider(
                            bytes(payload),
                            str(row.get("deferred_filename") or "upload.torrent"),
                            str(row.get("deferred_source") or row.get("source") or "manual"),
                            str(row.get("hash") or ""),
                            deferred_torrent_id=torrent_id,
                        )
                        if not result.get("_deferred"):
                            started += 1
                        continue

                    magnet = str(row.get("magnet") or "").strip()
                    if not magnet:
                        raise ValueError("Deferred magnet payload is missing")
                    result = await self._add_magnet(
                        magnet,
                        str(row.get("hash") or ""),
                        str(row.get("source") or "manual"),
                        duplicate_check=False,
                        resume_deferred=True,
                    )
                    if not result.get("_deferred"):
                        started += 1
                except Exception as exc:
                    failed += 1
                    message = sanitize_exception(exc, max_length=300)
                    logger.warning(
                        "Deferred provider submission %s could not start: %s",
                        torrent_id,
                        message,
                    )
                    async with get_db() as db:
                        await db.execute(
                            """UPDATE torrents
                               SET error_message=?, updated_at=CURRENT_TIMESTAMP
                               WHERE id=? AND provider_status=?""",
                            (message, torrent_id, DEFERRED_PROVIDER_STATUS),
                        )
                        await db.execute(
                            "INSERT INTO events (torrent_id, level, message) VALUES (?, 'warn', ?)",
                            (torrent_id, f"Deferred provider submission retry failed: {message}"),
                        )
                        await db.commit()
            return {"started": started, "failed": failed}

    async def _add_magnet(
        self,
        magnet: str,
        hash_value: str,
        source: str,
        *,
        duplicate_check: bool = True,
        resume_deferred: bool = False,
    ) -> dict:
        decision = None
        if duplicate_check:
            from services.duplicates import DuplicateCandidate, check_before_add

            decision = await check_before_add(DuplicateCandidate(
                source=source,
                magnet=magnet,
                infohash=hash_value,
            ))
            if decision.action == "skip":
                existing = decision.matches[0] if decision.matches else None
                result: dict = {}
                if existing:
                    try:
                        async with get_db() as db:
                            row = await db.fetchone(
                                "SELECT * FROM torrents WHERE id=?", (existing.torrent_id,)
                            )
                        result = dict(row) if row else {}
                    except Exception:
                        pass
                result["_duplicate"] = decision.as_dict()
                return result

        async with get_db() as db:
            existing = await db.fetchone("SELECT * FROM torrents WHERE hash=?", (hash_value,))
        deferred_existing = bool(
            existing
            and str(existing.get("provider_status") or "") == DEFERRED_PROVIDER_STATUS
            and not str(existing.get("alldebrid_id") or "").strip()
        )
        if (
            existing
            and existing["status"] in (
                "uploading", "processing", "queued", "downloading", "ready", "completed"
            )
            and not (resume_deferred and deferred_existing)
        ):
            return dict(existing)

        if self.is_paused():
            result = await self._persist_deferred_magnet(magnet, hash_value, source)
            if decision is not None and decision.action == "warn":
                result["_duplicate"] = decision.as_dict()
            return result

        async with self._upload_sem:
            if self.is_paused():
                result = await self._persist_deferred_magnet(magnet, hash_value, source)
                if decision is not None and decision.action == "warn":
                    result["_duplicate"] = decision.as_dict()
                return result
            result = await self.ad().upload_magnet(magnet)
        ad_id = str(result.get("id", ""))
        name = result.get("name") or result.get("filename") or hash_value[:16]
        normalized_hash = result.get("hash", hash_value).lower()
        logger.info("Magnet uploaded %s (ad_id=%s)", sanitize_log_value(name[:80]), ad_id)

        row = await self._upsert(normalized_hash, magnet, name, ad_id, source)
        if decision is not None and decision.action == "warn":
            row["_duplicate"] = decision.as_dict()
        if get_settings().discord_notify_added:
            await self.notify().send_added(name, source=source, alldebrid_id=ad_id)

        status_code = int(result.get("statusCode") or result.get("status_code") or 0)
        if status_code == READY_CODE:
            logger.info(
                "Fast-path: %s already ready on AllDebrid (cached) — starting download immediately",
                sanitize_log_value(name[:60]),
            )
            torrent_id = row.get("id")
            if torrent_id:
                self._schedule_ready_parent_download(int(torrent_id), ad_id, name)

        return row

    async def _upsert(self, hash_value: str, magnet: Optional[str], name: str, ad_id: str, source: str) -> dict:
        async with get_db() as db:
            await db.execute(
                """INSERT INTO torrents
                   (hash, magnet, name, alldebrid_id, status, source, provider_status, download_client)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(hash) DO UPDATE SET
                     magnet=COALESCE(excluded.magnet, torrents.magnet),
                     alldebrid_id=excluded.alldebrid_id,
                     name=excluded.name,
                     source=excluded.source,
                     status='uploading',
                     provider_status='queued',
                     updated_at=CURRENT_TIMESTAMP""",
                (hash_value, magnet, name, ad_id, "uploading", source, "queued", self.download_client_name()),
            )
            await db.execute(
                "INSERT INTO events (torrent_id,level,message) SELECT id,'info',? FROM torrents WHERE hash=?",
                (f"Uploaded to AllDebrid (id={ad_id})", hash_value),
            )
            await db.commit()
            row = await (await db.execute("SELECT * FROM torrents WHERE hash=?", (hash_value,))).fetchone()
        return dict(row) if row else {}

    async def reconcile_provider_inventory(self) -> dict:
        """Run one provider inventory cycle from one authoritative bulk snapshot."""
        if self.is_paused() or not get_settings().alldebrid_api_key:
            return {"imported": 0, "updated": 0, "snapshot_count": 0}

        try:
            all_magnets = await self.ad().get_magnet_status()
        except Exception as exc:
            error = str(exc)
            if any(
                keyword in error
                for keyword in (
                    "DISCONTINUED",
                    "discontinued",
                    "deprecated",
                    "migrate",
                )
            ):
                raise Exception(
                    "AllDebrid has disabled 'list all magnets' for your account. "
                    "Add magnets manually through the DebridPulse UI."
                ) from exc
            raise

        imported = await self.import_existing_magnets(all_magnets=all_magnets)
        updated = await self.full_alldebrid_sync(all_magnets=all_magnets)
        return {
            "imported": len(imported),
            "updated": int(updated or 0),
            "snapshot_count": len(all_magnets or []),
        }

    async def full_alldebrid_sync(
        self, all_magnets: Optional[List[Dict]] = None
    ) -> int:
        """
        Full reconciliation: fetches all magnets from AllDebrid and syncs
        every known torrent — including those marked 'completed' or 'error'.

        Returns number of torrents updated.

        This catches cases where:
        - A torrent is 'ready' on AllDebrid but locally stuck as 'error'
        - A torrent is in an unexpected state after restart
        - 100+ queued torrents that were never picked up
        """
        if self.is_paused() or not get_settings().alldebrid_api_key:
            return 0

        if all_magnets is None:
            try:
                all_magnets = await self.ad().get_magnet_status()
            except Exception as exc:
                logger.warning("full_alldebrid_sync: could not fetch magnets: %s", exc)
                return 0

        if not all_magnets:
            return 0

        # Index by alldebrid_id
        ad_by_id: dict = {str(m.get("id", "")): m for m in all_magnets}

        # Fetch all torrents that have an alldebrid_id (any status)
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT id, name, alldebrid_id, status, provider_status, provider_status_code, polling_failures, progress, size_bytes, magnet, source
                   FROM torrents
                   WHERE alldebrid_id IS NOT NULL AND alldebrid_id != ''
                     AND COALESCE(provider_status, '') NOT IN ('failed', 'missing')"""
            )

        updated = 0
        for row in rows:
            ad_id = str(row["alldebrid_id"])
            magnet = ad_by_id.get(ad_id)

            if not magnet:
                # Not in bulk response. AllDebrid only returns the most recent ~100
                # magnets in a bulk call — older magnets may simply be outside the
                # window, not actually gone. Verify with an individual call before
                # marking the torrent as deleted.
                if row["status"] in ("completed", "deleted", "error"):
                    continue
                try:
                    individual = await self.ad().get_magnet_status(str(row["alldebrid_id"]))
                    if individual:
                        magnet = individual[0]
                    else:
                        logger.info("full_alldebrid_sync: magnet %s confirmed missing on provider", row["alldebrid_id"])
                        await self._set_provider_missing(row["id"], "Magnet no longer exists on AllDebrid")
                        updated += 1
                        continue
                except Exception as exc:
                    if "MAGNET_INVALID_ID" in str(exc):
                        logger.info("full_alldebrid_sync: magnet %s invalid on provider", row["alldebrid_id"])
                        await self._set_provider_missing(row["id"], "Magnet no longer exists on AllDebrid")
                        updated += 1
                    else:
                        logger.debug("full_alldebrid_sync: individual check failed for %s: %s", row["alldebrid_id"], exc)
                    continue

            normalized = normalize_provider_state(magnet)
            provider_status = normalized["provider_status"]
            local_status = row["status"]

            # Ready on AllDebrid but locally stuck — trigger download
            # NEVER restart a torrent that is already downloading/queued in aria2.
            # Those are handled by _dispatch_pending_aria2_queue / reconcile_aria2_on_startup.
            _restartable = ("error", "pending", "uploading", "processing", "ready")
            if provider_status == "ready" and local_status in _restartable:
                logger.info(
                    "full_alldebrid_sync: torrent %s (local=%s) is ready on AllDebrid → starting download",
                    row["id"], local_status,
                )
                async with get_db() as db:
                    await db.execute(
                        """UPDATE torrents
                           SET status='ready', error_message=NULL, polling_failures=0,
                               provider_status=?, provider_status_code=?, updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (provider_status, int(normalized["status_code"]), row["id"]),
                    )
                    await db.commit()
                name = magnet.get("filename") or magnet.get("name") or row["name"]
                self._schedule_ready_parent_download(row["id"], ad_id, str(name))
                updated += 1
            elif provider_status == "ready" and local_status in ("queued", "downloading", "paused"):
                # Already in progress locally — do not restart. Just keep provider_status in sync.
                try:
                    await self._apply_provider_update(row, magnet, normalized)
                    updated += 1
                except Exception as exc:
                    logger.error("full_alldebrid_sync: update failed for %s: %s", ad_id, exc)

            elif local_status not in ("completed", "deleted") and provider_status != (row["provider_status"] or ""):
                # Status changed — apply update
                try:
                    await self._apply_provider_update(row, magnet, normalized)
                    updated += 1
                except Exception as exc:
                    logger.error("full_alldebrid_sync: update failed for %s: %s", ad_id, exc)

        if updated:
            logger.info("full_alldebrid_sync: %d torrents updated", updated)
        return updated

    async def sync_alldebrid_status(self):
        if self.is_paused() or not get_settings().alldebrid_api_key:
            return

        # Fetch only torrents that still need AllDebrid polling.
        # Torrents in queued/downloading/paused are already tracked by aria2 —
        # polling AllDebrid for them is unnecessary and wastes API quota.
        async with get_db() as db:
            rows = await (
                await db.execute(
                    """SELECT id, name, alldebrid_id, status, provider_status, provider_status_code, polling_failures, progress, size_bytes, magnet, source
                       FROM torrents
                       WHERE alldebrid_id IS NOT NULL AND alldebrid_id != ''
                         AND COALESCE(provider_status, '') NOT IN ('failed', 'missing')
                         AND status NOT IN ('completed', 'deleted', 'queued', 'downloading', 'paused')
                       ORDER BY priority DESC, id ASC"""
                )
            ).fetchall()

        if not rows:
            return

        # Attempt a single bulk call first.
        # IMPORTANT: AllDebrid /magnet/status without an id returns only the most
        # recent ~100 magnets — older entries may be absent from the response.
        # We use the bulk result where available and fall back to individual per-ID
        # calls for any torrent not found in the bulk window.  This keeps the common
        # case fast while correctly handling large backlogs without triggering
        # spurious polling failures for old-but-valid magnets.
        magnet_by_id: dict = {}
        try:
            all_magnets = await self.ad().get_magnet_status()
            magnet_by_id = {str(m.get("id", "")): m for m in all_magnets}
        except Exception as exc:
            logger.warning("sync_alldebrid_status: bulk fetch failed (%s) — using per-ID fallback", exc)

        for row in rows:
            try:
                ad_id = str(row["alldebrid_id"])
                magnet = magnet_by_id.get(ad_id)

                if magnet is None:
                    # Not in bulk response — could be an older magnet beyond AllDebrid's
                    # bulk window. Fall back to an individual API call instead of
                    # incrementing the polling failure counter (which would eventually
                    # mark a perfectly valid torrent as error).
                    try:
                        individual = await self.ad().get_magnet_status(ad_id)
                        magnet = individual[0] if individual else None
                    except Exception as exc_ind:
                        if "MAGNET_INVALID_ID" in str(exc_ind):
                            await self._set_provider_missing(row["id"], "Magnet no longer exists on AllDebrid")
                        else:
                            logger.error("Individual poll failed for %s: %s", ad_id, exc_ind)
                            await self._increment_poll_failure(row["id"], row["name"], str(exc_ind))
                        continue

                    if magnet is None:
                        await self._set_provider_missing(row["id"], "Magnet no longer exists on AllDebrid")
                        continue

                normalized = normalize_provider_state(magnet)
                await self._apply_provider_update(row, magnet, normalized)
            except Exception as exc:
                if "MAGNET_INVALID_ID" in str(exc):
                    await self._set_provider_missing(row["id"], "Magnet no longer exists on AllDebrid")
                else:
                    logger.error("Status poll failed for %s: %s", row["alldebrid_id"], exc)
                    await self._increment_poll_failure(row["id"], row["name"], str(exc))


    async def deep_sync_aria2_finished(self):
        async with self._aria2_state_lock:
            return await self._deep_sync_aria2_finished()

    async def _deep_sync_aria2_finished(self):
        """
        API-based deep sync for aria2 downloads.

        Runs against the aria2 JSON-RPC API — no filesystem access.

        For every download_file record in pending/queued/downloading/paused:

        1. Look up the recorded GID. Built-in mode may recover a stale GID by
           URI; external mode never adopts an unknown shared-daemon job.

        2. Based on the aria2 status:
           - complete   → mark download_file as 'completed' and preserve history
           - error      → add a fresh job with the same URL, update the current
                          GID in DB, preserve the failed result, send webhook
           - active     → update progress (completedLength/totalLength)
           - waiting/paused → keep current DB status, update size if available

        3. After processing all files, finalize torrents where all files are done.
        """
        if self.is_paused() or self.download_client_name() != "aria2":
            return

        # Fetch full aria2 state once — avoid hammering RPC per file
        try:
            all_downloads = await self._aria2_get_all()
        except Aria2ConnectionError:
            logger.warning("deep_sync: aria2 not reachable, skipping")
            return

        by_gid, uri_to_dl, path_to_dl = self._build_aria2_indexes(all_downloads)

        async with get_db() as db:
            rows = await (await db.execute(
                """SELECT f.id AS file_id, f.torrent_id, f.local_path,
                          f.size_bytes, f.download_id, f.download_url, f.filename,
                          f.status,
                          t.name AS torrent_name, t.alldebrid_id, t.status AS torrent_status
                   FROM download_files f
                   JOIN torrents t ON t.id = f.torrent_id
                   WHERE f.download_client = 'aria2'
                     AND f.blocked = 0
                     AND f.status IN ('pending', 'queued', 'downloading', 'paused', 'error')
                     AND t.status NOT IN ('completed', 'deleted')
                   ORDER BY t.id ASC, f.id ASC"""
            )).fetchall()

        if not rows:
            logger.info("deep_sync_aria2_finished: no active files to check")
            return

        touched: Set[int] = set()
        completed_count = 0
        restarted_count = 0
        cfg = get_settings()

        for row in rows:
            gid = str(row["download_id"] or "").strip()
            url = str(row["download_url"] or "").strip()
            file_id = row["file_id"]
            torrent_id = row["torrent_id"]

            # ── Step 1: find the aria2 entry ────────────────────────────────
            dl = by_gid.get(gid) if gid else None

            if dl is None and gid:
                try:
                    dl = await self._aria2_confirm_gid(gid)
                except Aria2ConnectionError as exc:
                    logger.warning(
                        "deep_sync: aria2 connection lost while confirming GID %s; skipping cycle: %s",
                        gid, exc,
                    )
                    return

            if dl is None and url and is_builtin_mode(cfg):
                # GID stale or missing — try to find via URI
                dl = uri_to_dl.get(url)
                if dl:
                    # Update stale GID in DB
                    async with get_db() as db:
                        await db.execute(
                            "UPDATE download_files SET download_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (dl.gid, file_id),
                        )
                        await db.commit()
                    logger.info(
                        "deep_sync: updated stale GID %s → %s for file %s (torrent %s)",
                        gid or "(none)", dl.gid, file_id, torrent_id,
                    )

            if dl is None:
                # Not found in aria2 at all — skip (can't act without API info)
                logger.debug(
                    "deep_sync: no aria2 entry for file %s (torrent %s, gid=%s)",
                    file_id, torrent_id, gid or "none",
                )
                continue

            # ── Step 2: act on aria2 status ──────────────────────────────────
            if dl.status == "complete":
                # aria2 says done — mark completed
                async with get_db() as db:
                    await db.execute(
                        "UPDATE download_files SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (file_id,),
                    )
                    await db.commit()
                touched.add(torrent_id)
                completed_count += 1
                logger.info(
                    "deep_sync: complete → torrent %s file %s (%s)",
                    torrent_id, file_id, row["filename"],
                )

            elif dl.status == "removed":
                logger.info(
                    "deep_sync: aria2 job was removed for torrent %s file %s (%s) â€” leaving recovery to the regular sync loop",
                    torrent_id, file_id, row["filename"],
                )
                continue
            elif dl.status == "error":
                # aria2 reports error — check retry count before restarting
                reason = f"{dl.error_code}: {dl.error_message}".strip(": ")
                max_retries = int(getattr(cfg, "aria2_error_retry_count", 3))
                current_retry = int(row.get("retry_count") or 0)

                if current_retry < max_retries:
                    # Still have retries left — restart
                    logger.warning(
                        "deep_sync: aria2 error torrent %s file %s (retry %d/%d) — restarting. %s",
                        torrent_id, file_id, current_retry + 1, max_retries, reason,
                    )
                    await self._log_event(
                        torrent_id, "warn",
                        f"deep_sync: aria2 error retry {current_retry+1}/{max_retries} for {row['filename']!r}: {reason}",
                    )
                    new_gid = None
                    if url:
                        try:
                            local_path_str = row["local_path"] or ""
                            options: dict = {}
                            if local_path_str:
                                from pathlib import PurePosixPath as _PPP
                                lp = Path(local_path_str)
                                options["dir"] = str(_PPP(str(lp.parent).replace(chr(92), "/")))
                                options["out"] = lp.name
                            options = self._aria2_job_options(options)
                            retry_snapshot = (
                                None
                                if is_builtin_mode(cfg)
                                else []
                            )
                            new_gid = await self.aria2().ensure_download(
                                url,
                                options,
                                start_paused=False,
                                cached_downloads=retry_snapshot,
                            )
                            await self._record_aria2_owned_gid(
                                new_gid,
                                download_file_id=file_id,
                                torrent_id=torrent_id,
                            )
                            async with get_db() as db:
                                await db.execute(
                                    """UPDATE download_files
                                       SET download_id=?, status='queued',
                                           retry_count=?, updated_at=CURRENT_TIMESTAMP
                                       WHERE id=?""",
                                    (new_gid, current_retry + 1, file_id),
                                )
                                await db.commit()
                            restarted_count += 1
                        except Exception as exc:
                            logger.error("deep_sync: restart failed for file %s: %s", file_id, exc)

                    if cfg.discord_notify_error:
                        torrent_name = row["torrent_name"] or f"torrent {torrent_id}"
                        await self.notify().send_error(
                            torrent_name,
                            reason=f"aria2 error (retry {current_retry+1}/{max_retries}): {reason}",
                            context=f"File: {row['filename']!r} — auto-restarted" if new_gid else "restart failed",
                            source="aria2",
                            provider="aria2",
                        )
                else:
                    # Max retries exhausted — mark as error, notify, remove from aria2
                    logger.error(
                        "deep_sync: max retries (%d) exhausted for torrent %s file %s — marking error. %s",
                        max_retries, torrent_id, file_id, reason,
                    )
                    await self._log_event(
                        torrent_id, "error",
                        f"deep_sync: max retries ({max_retries}) exhausted for {row['filename']!r}: {reason}",
                    )
                    async with get_db() as db:
                        await db.execute(
                            "UPDATE download_files SET status='error', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (file_id,),
                        )
                        await db.commit()
                    touched.add(torrent_id)

                    if cfg.discord_notify_error:
                        torrent_name = row["torrent_name"] or f"torrent {torrent_id}"
                        await self.notify().send_error(
                            torrent_name,
                            reason=f"aria2 download failed after {max_retries} retries: {reason}",
                            context=f"File: {row['filename']!r} — removed from queue",
                            source="aria2",
                            provider="aria2",
                        )

            elif dl.status == "active":
                # Persist the live file state. Parent progress is calculated
                # collectively by _update_aria2_parent_progress().
                if dl.total_length > 0:
                    async with get_db() as db:
                        await db.execute(
                            """UPDATE download_files
                               SET status='downloading', size_bytes=?,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE id=?""",
                            (dl.total_length, file_id),
                        )
                        await db.commit()

            elif dl.status in ("waiting", "paused"):
                # Update size if aria2 knows it
                if dl.total_length > 0:
                    async with get_db() as db:
                        await db.execute(
                            "UPDATE download_files SET size_bytes=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (dl.total_length, file_id),
                        )
                        await db.commit()

        logger.info(
            "deep_sync_aria2_finished: checked %d file(s), completed %d, restarted %d error(s), finalized %d torrent(s)",
            len(rows), completed_count, restarted_count, len(touched),
        )

        for torrent_id in touched:
            await self._finalize_aria2_torrent(torrent_id)

        # ── Stragglers (same logic as sync_aria2_downloads) ──────────────────
        try:
            async with get_db() as db:
                straggler_rows = await (await db.execute(
                    """SELECT DISTINCT torrent_id
                       FROM download_files
                       WHERE torrent_id IN (
                           SELECT id FROM torrents
                           WHERE status IN ('queued', 'downloading')
                             AND download_client = 'aria2'
                       )
                       GROUP BY torrent_id
                       HAVING SUM(CASE WHEN blocked=0 AND status != 'completed' THEN 1 ELSE 0 END) = 0
                          AND SUM(CASE WHEN blocked=0 THEN 1 ELSE 0 END) > 0""",
                )).fetchall()
            straggler_ids = (
                {r["torrent_id"] for r in straggler_rows}
                - touched
                - set(self._active)
            )
            if straggler_ids:
                logger.info(
                    "deep_sync: found %d straggler torrent(s) with all files completed "
                    "but torrent still active — finalising: %s",
                    len(straggler_ids), sorted(straggler_ids),
                )
                for torrent_id in straggler_ids:
                    if torrent_id in self._active:
                        logger.debug(
                            "deep_sync: torrent %s is rebuilding its manifest; deferring finalization",
                            torrent_id,
                        )
                        continue
                    await self._finalize_aria2_torrent(torrent_id)
        except Exception as exc:
            logger.warning("deep_sync: straggler check failed: %s", exc)

        await self._advance_aria2_queue_locked()


    async def cleanup_stuck_downloads(self):
        """
        Resets torrents stuck in active states for too long.

        Three checks:
        1. status='downloading' but NO download_files records → stuck pre-_download;
           reset immediately to 'ready' (no timeout needed — this is always wrong).
        2. Local download stuck (queued/downloading) > stuck_download_timeout_hours
           → reset to 'ready' so the download restarts.
        3. AllDebrid processing stuck (processing/uploading) > 24h without update
           → reset to trigger re-poll; AllDebrid may have finished or errored.
        """
        from core.config import get_settings
        cfg = get_settings()
        timeout_hours = getattr(cfg, "stuck_download_timeout_hours", 6)

        async with get_db() as db:
            # Check 0: downloading with no download_files — torrent is stuck waiting
            # for the semaphore but _download() has never run.  This can happen when
            # many _start_download tasks pile up.  Reset immediately so polling resumes.
            stuck_no_files = await (await db.execute(
                """SELECT t.id, t.name, t.alldebrid_id, t.status
                   FROM torrents t
                   WHERE t.status = 'downloading'
                     AND NOT EXISTS (
                         SELECT 1 FROM download_files f
                         WHERE f.torrent_id = t.id AND f.blocked = 0
                     )"""
            )).fetchall()
            # Skip torrents that are actively in self._active (their task is running)
            stuck_no_files = [r for r in stuck_no_files if r["id"] not in self._active]

            # Check 1: local download stuck
            stuck_local = []
            if timeout_hours and timeout_hours > 0:
                _cutoff_local = f"datetime('now','-{int(timeout_hours)} hours')"
                stuck_local = await (await db.execute(
                    f"""SELECT id, name, alldebrid_id, status FROM torrents
                       WHERE status IN ('queued', 'downloading')
                         AND updated_at < {_cutoff_local}"""
                )).fetchall()

            # Check 2: AllDebrid processing stuck > 24h (configurable separately)
            _cutoff_ad = "datetime('now','-24 hours')"
            stuck_ad = await (await db.execute(
                f"""SELECT id, name, alldebrid_id, status FROM torrents
                   WHERE status IN ('processing', 'uploading')
                     AND updated_at < {_cutoff_ad}
                     AND alldebrid_id IS NOT NULL AND alldebrid_id != ''"""
            )).fetchall()

        # Combine, no_files first (immediate reset), then timed-out, de-duplicated
        seen = set()
        rows = []
        for row in list(stuck_no_files) + list(stuck_local) + [r for r in stuck_ad]:
            if row["id"] not in seen:
                seen.add(row["id"])
                rows.append(row)

        if stuck_no_files:
            logger.info(
                "cleanup_stuck_downloads: %d torrent(s) stuck in 'downloading' with no "
                "download_files — resetting immediately",
                len(stuck_no_files),
            )

        if not rows:
            return

        logger.info("cleanup_stuck_downloads: %d stuck torrent(s) found", len(rows))
        for row in rows:
            logger.info("Resetting stuck torrent %s (%s) [was %s]", row["id"], row["name"], row["status"])
            reason = (
                f"Auto-reset: stuck in '{row['status']}' for >{timeout_hours}h"
                if row["status"] in ("queued", "downloading")
                else f"Auto-reset: stuck in '{row['status']}' for >24h on AllDebrid"
            )
            async with get_db() as db:
                await db.execute(
                    "UPDATE torrents SET status='ready', polling_failures=0, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (row["id"],)
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'warn', ?)",
                    (row["id"], reason)
                )
                await db.commit()

    async def cleanup_no_peer_errors(self):
        """Clean confirmed fatal provider errors only for locally owned objects."""
        async with get_db() as db:
            rows = await (await db.execute(
                """SELECT id, name, alldebrid_id, source, error_message, provider_status_code
                   FROM torrents
                   WHERE status = 'error'
                     AND provider_status = 'error'
                     AND (
                       provider_status_code = 8
                       OR provider_status_code = 7
                       OR LOWER(COALESCE(error_message, '')) LIKE '%no peer%'
                       OR LOWER(COALESCE(error_message, '')) LIKE '%more than 3 day%'
                       OR LOWER(COALESCE(error_message, '')) LIKE '%took more than%'
                       OR LOWER(COALESCE(error_message, '')) LIKE '%timeout%'
                       OR LOWER(COALESCE(error_message, '')) LIKE '%timed out%'
                     )"""
            )).fetchall()

        if not rows:
            return

        logger.info("cleanup_no_peer_errors: found %d torrent(s) to clean up", len(rows))

        for row in rows:
            ad_id = str(row.get("alldebrid_id") or "").strip()
            name = row.get("name") or f"torrent {row['id']}"
            owned = self._provider_delete_authorized(row.get("source"))
            removed_from_provider = False

            if ad_id and ad_id.lower() not in ("none", "null", "") and owned:
                try:
                    logger.info(
                        "no-peer cleanup: removing owned AllDebrid object for %s (%s)",
                        row["id"],
                        name,
                    )
                    removed_from_provider = bool(await self.ad().delete_magnet(ad_id))
                except Exception as exc:
                    logger.warning(
                        "no-peer cleanup: could not delete owned magnet %s: %s",
                        ad_id,
                        sanitize_exception(exc),
                    )
                event_msg = (
                    "Provider download failed — owned failed object removed from AllDebrid; local history retained"
                    if removed_from_provider
                    else "Provider download failed — owned AllDebrid cleanup failed; local history retained"
                )
            elif ad_id and ad_id.lower() not in ("none", "null", ""):
                logger.info(
                    "no-peer cleanup: preserving unowned AllDebrid object %s for torrent %s",
                    ad_id,
                    row["id"],
                )
                event_msg = (
                    "Provider download failed — AllDebrid object preserved because this instance does not own it"
                )
            else:
                logger.info(
                    "no-peer cleanup: torrent %s (%s) has no AllDebrid ID — retaining failed local record",
                    row["id"],
                    name,
                )
                event_msg = "Provider download failed — no AllDebrid ID remains; local history retained"

            async with get_db() as db:
                await db.execute(
                    """UPDATE torrents
                       SET status='error', provider_status='failed',
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (row["id"],),
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'warn', ?)",
                    (row["id"], event_msg),
                )
                await db.commit()

            await self._notify_provider_error(
                name,
                reason=str(row.get("error_message") or "Provider download failed"),
                context=(
                    f"Failed owned AllDebrid ID {ad_id} removed; DebridPulse history retained"
                    if removed_from_provider
                    else (
                        f"AllDebrid ID {ad_id} preserved; DebridPulse history retained"
                        if ad_id and ad_id.lower() not in ("none", "null", "")
                        else "No AllDebrid ID available; DebridPulse history retained"
                    )
                ),
                alldebrid_id=str(ad_id or ""),
                status_code=row.get("provider_status_code"),
            )

    async def cleanup_alldebrid_orphans(self) -> int:
        """Conservatively clean only locally owned error objects.

        Absence from the local database is never deletion authority. Imported
        objects and local-only deleted rows remain untouched.
        """
        try:
            magnets = await self.ad().get_magnet_status()
        except Exception as exc:
            logger.warning("cleanup_alldebrid_orphans: provider scan failed: %s", exc)
            return 0
        async with get_db() as db:
            rows = await db.fetchall(
                "SELECT alldebrid_id, status, source, provider_status FROM torrents WHERE alldebrid_id IS NOT NULL"
            )
        known = {str(row["alldebrid_id"]): row for row in rows if row.get("alldebrid_id")}
        deleted = 0
        for magnet in magnets or []:
            ad_id = str(magnet.get("id") or "").strip()
            status_code = int(magnet.get("statusCode") or 0)
            status_text = str(magnet.get("status") or "").lower()
            fatal = status_code in ERROR_CODES or "no peer" in status_text or "not available" in status_text
            if not ad_id or not fatal:
                continue
            local = known.get(ad_id)
            if (
                local is None
                or str(local.get("status") or "") != "error"
                or str(local.get("provider_status") or "") == "failed"
                or not self._provider_delete_authorized(local.get("source"))
            ):
                logger.debug(
                    "cleanup_alldebrid_orphans: preserving unowned/unknown provider object %s",
                    ad_id,
                )
                continue
            try:
                if await self.ad().delete_magnet(ad_id):
                    deleted += 1
            except Exception as exc:
                logger.warning("cleanup_alldebrid_orphans: delete %s failed: %s", ad_id, exc)
        return deleted

    async def _apply_provider_update(self, row: Dict, magnet: Dict, normalized: Dict[str, object]):
        provider_status = str(normalized["provider_status"])
        local_status    = str(normalized["local_status"])   # always a plain string from normalize_provider_state
        status_code = int(normalized["status_code"])
        progress = float(normalized["progress"])
        size_bytes = int(normalized["size_bytes"])
        provider_message = str(normalized["message"])
        current_status = row["status"]
        current_progress = float(row.get("progress") or 0.0)
        current_size_bytes = int(row.get("size_bytes") or 0)
        current_provider_code = row.get("provider_status_code")
        provider_state_changed = (
            provider_status != (row["provider_status"] or "")
            or status_code != int(
                current_provider_code
                if current_provider_code is not None
                else -1
            )
        )
        local_delivery_active = (
            current_status in {"queued", "downloading", "paused"}
            and provider_status == "ready"
        )
        persisted_status = current_status if local_delivery_active else local_status
        # Once provider preparation is complete, aria2 owns local transfer
        # progress/size. A full provider reconciliation must not overwrite that
        # live local telemetry with AllDebrid's already-ready 100% state.
        persisted_progress = current_progress if local_delivery_active else progress
        persisted_size_bytes = (
            current_size_bytes
            if local_delivery_active and current_size_bytes > 0
            else size_bytes
        )
        status_changed = persisted_status != current_status
        progress_changed = abs(persisted_progress - current_progress) > 1e-6
        size_changed = persisted_size_bytes != current_size_bytes
        polling_failures_present = int(row.get("polling_failures") or 0) != 0
        meaningful_changed = (
            provider_state_changed
            or status_changed
            or progress_changed
            or size_changed
            or polling_failures_present
        )

        if meaningful_changed:
            async with get_db() as db:
                if provider_state_changed:
                    await db.execute(
                        "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                        (row["id"], "info", f"AllDebrid status -> {provider_status} [{status_code}] {provider_message}".strip()),
                    )
                await db.execute(
                    """UPDATE torrents
                       SET status=?, provider_status=?, provider_status_code=?, progress=?, size_bytes=?,
                           polling_failures=0, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (
                        persisted_status,
                        provider_status,
                        status_code,
                        persisted_progress,
                        persisted_size_bytes,
                        row["id"],
                    ),
                )
                await db.commit()

        visible_changed = (
            provider_state_changed
            or status_changed
            or progress_changed
            or size_changed
        )

        # SSE is emitted only for a visible change. Stable provider polling no
        # longer generates a write + broadcast cycle merely to say nothing changed.
        if visible_changed:
            try:
                progress_item = {
                    "id": row["id"],
                    "status": persisted_status,
                    "name": str(row["name"] or ""),
                    "progress": persisted_progress,
                    "status_changed": status_changed,
                }
                await publish(
                    "torrent_updated",
                    {
                        **progress_item,
                        "progress_only": not (
                            status_changed
                            or provider_state_changed
                            or size_changed
                        ),
                        "items": [progress_item],
                    },
                )
            except Exception as exc:
                logger.debug(
                    "Provider progress SSE broadcast failed for torrent %s: %s",
                    row["id"],
                    exc,
                )

        if provider_status == "ready" and current_status in {"pending", "uploading", "processing", "ready", "error"}:
            # Also restart if local status is 'error' but AllDebrid reports ready —
            # the torrent may have recovered or been re-uploaded
            name = magnet.get("filename") or magnet.get("name") or row["name"]
            if current_status == TorrentStatus.ERROR:
                logger.info("Torrent %s recovered on AllDebrid (was error, now ready) — restarting download", row["id"])
                async with get_db() as _db:
                    await _db.execute(
                        "UPDATE torrents SET status='ready', error_message=NULL, polling_failures=0, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (row["id"],),
                    )
                    await _db.commit()
            self._schedule_ready_parent_download(
                row["id"], str(row["alldebrid_id"]), str(name)
            )
        elif provider_status == "ready" and current_status not in (
            TorrentStatus.DOWNLOADING,
            TorrentStatus.QUEUED,
            TorrentStatus.PAUSED,
            TorrentStatus.COMPLETED,
            TorrentStatus.DELETED,
        ):
            # AllDebrid reports the torrent as ready — start the download.
            logger.info(
                "sync: torrent %s (id=%s) is ready on AllDebrid → starting download",
                row["id"], str(row.get("alldebrid_id", "?")),
            )
            async with get_db() as _db:
                await _db.execute(
                    "UPDATE torrents SET status='ready', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (row["id"],),
                )
                await _db.commit()
            self._schedule_ready_parent_download(
                row["id"], str(row["alldebrid_id"]), str(name)
            )

        elif provider_status == "expired":
            # AllDebrid statusCode 3: "Expired — files removed from cache".
            # The AllDebrid entry is no longer usable.  If we have the original
            # magnet, silently re-upload it so the user does not need to intervene.
            magnet_link = str(row.get("magnet") or "").strip()
            torrent_name = str(row.get("name") or f"torrent {row['id']}")

            # If no magnet was stored (e.g. added via .torrent file or torrent URL),
            # synthesize a bare magnet from the infohash.  AllDebrid accepts
            # magnet:?xt=urn:btih:<hash> identically to full magnet links.
            if not magnet_link:
                stored_hash = str(row.get("hash") or "").strip().lower()
                if stored_hash and len(stored_hash) in (40, 64):  # SHA-1 or SHA-256 btih
                    import urllib.parse as _up
                    magnet_link = (
                        f"magnet:?xt=urn:btih:{stored_hash}"
                        f"&dn={_up.quote(torrent_name[:120])}"
                    )
                    logger.info(
                        "expired_reimport: no magnet stored for torrent %s — "
                        "synthesized from hash %s",
                        row["id"], stored_hash[:16],
                    )

            if magnet_link:
                logger.warning(
                    "Magnet expired on AllDebrid (torrent %s '%s') — reimporting",
                    row["id"], torrent_name[:60],
                )
                # Clear the stale AllDebrid ID so duplicate detection allows re-add
                async with get_db() as _db:
                    await _db.execute(
                        """UPDATE torrents
                              SET alldebrid_id = NULL,
                                  status = 'pending',
                                  provider_status = NULL,
                                  provider_status_code = NULL,
                                  error_message = 'AllDebrid expired — reimporting',
                                  updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?""",
                        (row["id"],),
                    )
                    await _db.commit()
                self._track_maintenance_task(self._handle_expired_reimport(row, magnet_link), label=f"expired-reimport-{row['id']}")
            else:
                logger.warning(
                    "Magnet expired on AllDebrid (torrent %s '%s') — no magnet stored, marking error",
                    row["id"], torrent_name[:60],
                )
                async with get_db() as _db:
                    await _db.execute(
                        """UPDATE torrents
                              SET status = 'error',
                                  error_message = 'AllDebrid: expired — files removed from cache. Add magnet again to retry.',
                                  updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?""",
                        (row["id"],),
                    )
                    await _db.commit()

        elif provider_status == "error" and current_status != TorrentStatus.ERROR:
            error_message = f"AllDebrid error code {status_code}: {provider_message}".strip()
            # statusCode 8 = "No peer after 30 minutes" — re-upload if magnet stored,
            # otherwise delete and notify.
            if status_code == 8 or "no peer" in provider_message.lower():
                magnet_link = str(row.get("magnet") or "").strip()
                if magnet_link:
                    logger.info(
                        "No peers for torrent %s (id=%s) — scheduling re-upload attempt",
                        row["id"], row.get("alldebrid_id", "?"),
                    )
                    self._track_maintenance_task(self._handle_upload_failed(row, error_message), label=f"upload-failed-{row['id']}")
                else:
                    logger.info(
                        "No peers for torrent %s (id=%s) — no magnet stored, removing",
                        row["id"], row.get("alldebrid_id", "?"),
                    )
                    await self._notify_provider_error(
                        str(row["name"] or magnet.get("filename") or magnet.get("name") or f"torrent {row['id']}"),
                        reason="No peers found after 30 minutes — no magnet link stored for re-upload",
                        context="AllDebrid reported the torrent as unavailable. Add the magnet manually to retry.",
                        alldebrid_id=str(row.get("alldebrid_id") or ""),
                        status_code=status_code,
                    )
                    if self._provider_delete_authorized(row.get("source")):
                        await self._log_event(
                            row["id"],
                            "warn",
                            f"No peers after 30 minutes (code {status_code}) — no magnet stored; removing owned AllDebrid object",
                        )
                        try:
                            await self.ad().delete_magnet(str(row["alldebrid_id"]))
                        except Exception as exc:
                            logger.debug(
                                "Could not delete owned no-peer magnet %s: %s",
                                row["alldebrid_id"],
                                sanitize_exception(exc),
                            )
                    else:
                        await self._log_event(
                            row["id"],
                            "warn",
                            f"No peers after 30 minutes (code {status_code}) — observed AllDebrid object preserved",
                        )
                    await self._fail_torrent(row["id"], "No peers after 30 minutes — no magnet stored for re-upload", notify=False)
            elif status_code == UPLOAD_FAILED_CODE:
                self._track_maintenance_task(self._handle_upload_failed(row, error_message), label=f"upload-failed-{row['id']}")
            else:
                await self._fail_torrent(row["id"], error_message, notify=True)
        elif provider_status == "error" and current_status == TorrentStatus.ERROR and status_code in (7, 8):
            # Already in error state but code=8/7 — ensure cleanup runs.
            # This covers torrents that were already error before the no-peer handler ran
            # or that arrived via import_existing_magnets without going through
            # _apply_provider_update for the first time (e.g. container restart mid-error).
            # cleanup_no_peer_errors() handles the actual deletion; nothing to do here
            # except ensure error_message is set so the cleanup SQL can match it.
            async with get_db() as _edb:
                await _edb.execute(
                    """UPDATE torrents
                       SET error_message=COALESCE(NULLIF(error_message,''), ?),
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='error'""",
                    (f"AllDebrid error code {status_code}: {provider_message}".strip(), row["id"]),
                )
                await _edb.commit()
        elif provider_status == "error" and current_status == TorrentStatus.ERROR and provider_state_changed:
            await self._notify_provider_error(
                str(row["name"] or magnet.get("filename") or magnet.get("name") or f"torrent {row['id']}"),
                reason=f"AllDebrid reported magnet error {status_code}",
                context=provider_message,
                alldebrid_id=str(row.get("alldebrid_id") or ""),
                status_code=status_code,
            )

    async def _increment_poll_failure(self, torrent_id: int, name: str, reason: str):
        async with get_db() as db:
            await db.execute(
                "UPDATE torrents SET polling_failures=polling_failures+1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (torrent_id,),
            )
            row = await (await db.execute("SELECT polling_failures FROM torrents WHERE id=?", (torrent_id,))).fetchone()
            failures = int(row["polling_failures"] or 0)
            if failures == 1 or failures == PROVIDER_FAILURE_THRESHOLD:
                level = "warn" if failures < PROVIDER_FAILURE_THRESHOLD else "error"
                message = f"AllDebrid polling issue ({failures}): {reason}"
                await db.execute("INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)", (torrent_id, level, message))
                if failures >= PROVIDER_FAILURE_THRESHOLD:
                    await db.execute(
                        "UPDATE torrents SET status='error', error_message=? WHERE id=?",
                        (message, torrent_id),
                    )
            await db.commit()

        if failures >= PROVIDER_FAILURE_THRESHOLD:
            await self._notify_provider_error(
                name,
                reason=reason,
                context=f"Polling failed {failures} times in a row",
                source="AllDebrid polling",
                provider="AllDebrid",
            )

    def _engine_schedule_ready_parent_download(
        self, torrent_id: int, ad_id: str, name: str
    ) -> bool:
        """Claim and schedule one provider-ready parent exactly once.

        Every path that discovers ready provider work uses this helper. The
        synchronous claim closes the gap between ``create_task`` and
        ``_start_download`` adding the parent to ``_active``.
        """
        torrent_id = int(torrent_id)
        if (
            self._materialization_quiescing
            or self.is_paused()
            or self._disk_guard_active
            or torrent_id in self._active
            or torrent_id in self._ready_parent_task_ids
        ):
            return False

        self._ready_parent_task_ids.add(torrent_id)
        task = asyncio.create_task(
            self._start_download(torrent_id, str(ad_id), str(name or ""))
        )
        self._ready_parent_tasks.add(task)

        def _finished(done: asyncio.Task) -> None:
            self._ready_parent_tasks.discard(done)
            self._ready_parent_task_ids.discard(torrent_id)
            try:
                done.result()
            except asyncio.CancelledError:
                logger.debug("Ready-parent task cancelled for transfer %s", torrent_id)
            except Exception as exc:
                logger.error(
                    "Ready-parent task failed for transfer %s: %s",
                    torrent_id,
                    sanitize_exception(exc, max_length=300),
                )

        task.add_done_callback(_finished)
        return True

    async def _engine_start_download(self, torrent_id: int, ad_id: str, name: str):
        if self.is_paused() or torrent_id in self._active:
            return
        # Atomically claim this torrent_id BEFORE any await to prevent TOCTOU:
        # two concurrent tasks could both pass "torrent_id in self._active"
        # if there is an await between the check and the add.
        # We add first, then validate — if validation fails we discard and return.
        self._active.add(torrent_id)
        try:
            # Guard: do not restart a torrent that is actively downloading.
            # Check both status AND whether download_files records exist.
            # If download_files is empty (after _reset_torrent_for_redownload)
            # the torrent is NOT actively downloading — restart is intended.
            try:
                async with get_db() as _guard_db:
                    _t = await (await _guard_db.execute(
                        "SELECT status FROM torrents WHERE id=?", (torrent_id,)
                    )).fetchone()
                    if _t is None:
                        logger.debug("_start_download: torrent %s no longer exists", torrent_id)
                        return
                    _status = _t["status"]
                    if _status in ("completed", "deleted"):
                        logger.debug(
                            "_start_download: torrent %s is terminal (status=%s) — skipping",
                            torrent_id, _status,
                        )
                        return
                    if _status in ACTIVE_DOWNLOAD:
                        _file_count = await (await _guard_db.execute(
                            "SELECT COUNT(*) AS c FROM download_files "
                            "WHERE torrent_id=? AND blocked=0 "
                            "  AND status IN ('pending','queued','downloading','paused')",
                            (torrent_id,),
                        )).fetchone()
                        if _file_count and _file_count["c"] > 0:
                            logger.debug(
                                "_start_download: torrent %s already in progress "
                                "(status=%s, %d active files) — skipping",
                                torrent_id, _status, _file_count["c"],
                            )
                            return
            except Exception as exc:
                logger.debug("_start_download guard DB check failed: %s — proceeding", exc)
            # Acquire the semaphore BEFORE marking status='downloading'.
            # Previously status was set to 'downloading' immediately in the guard block,
            # which meant 100 waiting tasks all showed 'downloading' in the DB even though
            # _download() hadn't run yet for 97 of them.  This caused:
            #   - import_existing_magnets to see them as 'downloading' → should_queue=False
            #   - sync_alldebrid_status to exclude them ('downloading' is excluded)
            #   - _dispatch_pending_aria2_queue to find no download_files → nothing to send
            # Net effect: torrents stuck in 'downloading' with no progress, no polling,
            # no recovery — until cleanup_stuck_downloads fires after 6 hours.
            # Fix: mark 'downloading' only after acquiring the semaphore, so the DB
            # reflects reality. While waiting, status stays as-is (ready/error/etc.) so
            # the poll loop can still see and re-evaluate it if needed.
            async with self.sem():
                # Pause All may have been pressed while this task waited for a
                # preparation slot. Do not cross that boundary and create new
                # queue work behind the user's global pause.
                if self.is_paused():
                    return
                # Now we have a slot — mark as downloading so aria2 sync and AllDebrid
                # poll don't interfere while _download() runs.
                try:
                    async with get_db() as _slot_db:
                        await _slot_db.execute(
                            "UPDATE torrents SET status='downloading', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (torrent_id,),
                        )
                        await _slot_db.commit()
                except Exception as exc:
                    logger.debug("_start_download: could not set downloading status: %s", exc)
                await self._download(torrent_id, ad_id, name)
        except TransientAllDebridStateError as exc:
            logger.warning("Download deferred db_id=%s: %s", torrent_id, exc)
            async with get_db() as db:
                await db.execute(
                    "UPDATE torrents SET status='ready', error_message=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (torrent_id,),
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                    (torrent_id, "warn", str(exc)),
                )
                await db.commit()
        except Exception as exc:
            logger.error("Download failed db_id=%s: %s", torrent_id, exc)
            await self._fail_torrent(torrent_id, str(exc), notify=True)
        finally:
            self._active.discard(torrent_id)

    async def _engine_download(self, torrent_id: int, ad_id: str, name: str):
        cfg = get_settings()
        client_name = self.download_client_name()


        initial_status = "queued"  # aria2 is the only non-symlink client

        # ── Disk-space guard ─────────────────────────────────────────────────
        # If the guard is active, defer this dispatch.  Set status back to
        # 'ready' so the torrent is visible to polling and is not mistaken for
        # a stuck download by cleanup_stuck_downloads.
        min_free_gb = float(getattr(cfg, "min_free_disk_gb", 0) or 0)
        if min_free_gb > 0:
            if self._disk_guard_active:
                logger.info(
                    "disk_guard: deferring torrent %s dispatch — guard is active (low disk), "
                    "resetting to 'ready'", torrent_id
                )
                async with get_db() as _gd_db:
                    await _gd_db.execute(
                        "UPDATE torrents SET status='ready', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (torrent_id,),
                    )
                    await _gd_db.commit()
                return  # disk_guard_loop will call _start_download when space recovers
            free_gb = self._get_free_gb(str(cfg.download_folder or "/download"))
            if free_gb >= 0 and free_gb < min_free_gb:
                msg = (
                    f"Not enough disk space: {free_gb:.1f} GB free, "
                    f"{min_free_gb:.1f} GB required — download deferred until space recovers"
                )
                logger.info("Disk-space guard: torrent %s uploaded to AllDebrid but aria2 download deferred (low disk) — will resume automatically", torrent_id)
                self._disk_guard_active = True
                async with get_db() as _gd_db:
                    await _gd_db.execute(
                        "UPDATE torrents SET status='ready', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (torrent_id,),
                    )
                    await _gd_db.commit()
                return  # disk_guard_loop will resume when space recovers

        # Cancel any existing aria2 jobs for this torrent before clearing the DB rows.
        # Without this, the old aria2 entries become orphans that download in parallel.
        try:
            async with get_db() as _pre_db:
                old_gids = await (await _pre_db.execute(
                    "SELECT download_id FROM download_files "
                    "WHERE torrent_id=? AND download_client='aria2' "
                    "AND download_id IS NOT NULL AND status NOT IN ('completed','error','blocked')",
                    (torrent_id,),
                )).fetchall()
            for _r in old_gids:
                _gid = str(_r["download_id"] or "")
                if _gid:
                    try:
                        await self._remove_owned_aria2_gid(_gid)
                        logger.debug("_download: cancelled stale aria2 GID %s for torrent %s", _gid, torrent_id)
                    except Exception:
                        pass  # GID already gone — fine
        except Exception as exc:
            logger.debug("_download: stale aria2 cleanup skipped: %s", exc)

        async with get_db() as db:
            await db.execute("DELETE FROM download_files WHERE torrent_id=?", (torrent_id,))
            await db.execute(
                "UPDATE torrents SET status=?, download_client=?, error_message=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (initial_status, client_name, torrent_id),
            )
            await db.commit()

        flat_files = await self._fetch_ready_files(ad_id)
        if not flat_files:
            raise Exception("No downloadable files returned from AllDebrid")

        destination_root = Path(cfg.download_folder) / safe_name(name)
        # Directory creation is left to aria2

        total_files = len(flat_files)
        blocked_items: List[dict] = []
        transferred_items: List[dict] = []
        queued_items: List[dict] = []
        failed_items: List[dict] = []
        seen_queue_keys: Set[Tuple[str, str]] = set()

        # ── Dedupe and categorise files ───────────────────────────────────────
        # Build work list: filter out duplicates and immediately-blocked files
        work_items: List[Dict] = []
        manifest_rows: List[tuple] = []
        for file_info in flat_files:
            relative_path = file_info.get("path") or file_info.get("name") or "download.bin"
            display_name = str(PurePosixPath(relative_path.replace("\\", "/")))
            file_size = int(file_info.get("size", 0) or 0)
            blocked, reason = is_blocked(display_name, cfg, file_size)
            source_link = file_info["link"]

            # AllDebrid commonly returns paths already rooted beneath the
            # torrent name. destination_root already supplies that directory,
            # so strip exactly one matching sanitized root component.
            relative_target = safe_rel_path(display_name)
            torrent_root = safe_name(name)

            if relative_target.parts and relative_target.parts[0] == torrent_root:
                remaining_parts = relative_target.parts[1:]
                relative_target = (
                    Path(*remaining_parts)
                    if remaining_parts
                    else Path("download.bin")
                )

            local_path = destination_root / relative_target
            dedupe_key = (display_name.lower(), source_link.strip())

            if dedupe_key in seen_queue_keys:
                logger.info("Skipping duplicate AllDebrid file entry for %s", display_name)
                continue
            seen_queue_keys.add(dedupe_key)

            if blocked:
                blocked_items.append({"filename": display_name, "size_bytes": file_size, "reason": reason})
                manifest_rows.append(
                    (
                        torrent_id,
                        display_name,
                        file_size,
                        source_link,
                        source_link,
                        str(local_path),
                        "blocked",
                        client_name,
                        1,
                        reason,
                    )
                )
                continue

            work_items.append({
                "display_name": display_name,
                "file_size": file_size,
                "source_link": source_link,
                "local_path": local_path,
            })

        # ── Materialize the provider manifest without eager URL generation ───
        # The dispatcher owns direct-URL generation because it knows which files
        # actually have an aria2 slot. Eagerly unlocking every manifest entry here
        # doubled provider API calls and made large cached torrents slow to queue.
        for item in work_items:
            display_name = item["display_name"]
            file_size = item["file_size"]
            source_link = item["source_link"]
            local_path = item["local_path"]

            if local_path.exists() and (
                file_size <= 0
                or local_path.stat().st_size >= max(file_size - 1024, 0)
            ):
                transferred_items.append(
                    {"filename": display_name, "size_bytes": file_size}
                )
                manifest_rows.append(
                    (
                        torrent_id,
                        display_name,
                        file_size,
                        source_link,
                        source_link,
                        str(local_path),
                        "completed",
                        client_name,
                        0,
                        None,
                    )
                )
                continue

            queued_items.append({"filename": display_name, "size_bytes": file_size})
            manifest_rows.append(
                (
                    torrent_id,
                    display_name,
                    file_size,
                    source_link,
                    source_link,
                    str(local_path),
                    "pending",
                    "aria2",
                    0,
                    None,
                )
            )

        if manifest_rows:
            async with get_db() as db:
                await db.executemany(
                    """INSERT INTO download_files
                       (torrent_id, filename, size_bytes, source_url,
                        download_url, local_path, status, download_id,
                        download_client, blocked, block_reason, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, CURRENT_TIMESTAMP)""",
                    manifest_rows,
                )
                await db.commit()

        blocked_count = len(blocked_items)
        failed_count = len(failed_items)
        completed_count = len(transferred_items)
        queued_count = len(queued_items)
        downloadable_count = total_files - blocked_count

        # Compute total size from all processed files — more reliable than the
        # AllDebrid magnet-status value which is often 0 until the torrent is ready.
        total_size_bytes = _size_sum(blocked_items + transferred_items + queued_items + failed_items)

        # All files go through aria2 — final_status is queued or error
        if blocked_count == total_files and total_files > 0 and failed_count == 0:
            # ALL files filtered — nothing to download; treat as completed so
            # the torrent is removed from AllDebrid and counted in statistics.
            final_status = "completed"
        elif queued_count > 0:
            # Permit successfully prepared files to proceed even when individual
            # AllDebrid links fail. Failed files remain recorded as errors for
            # inspection, but do not block valid HTTP(S) downloads.
            final_status = "queued"
        elif failed_count == 0 and completed_count > 0:
            final_status = "completed"
        else:
            final_status = "error"

        # A preparation task can finish after Pause All was pressed. Persist
        # those newly materialized children as paused so the database and UI
        # cannot claim they are runnable while the global gate is active.
        if final_status == "queued" and self.is_paused():
            final_status = "paused"
            async with get_db() as db:
                await db.execute(
                    """UPDATE download_files
                       SET status='paused', updated_at=CURRENT_TIMESTAMP
                       WHERE torrent_id=? AND blocked=0 AND status='pending'""",
                    (torrent_id,),
                )
                await db.commit()

        async with get_db() as db:
            source_row = await db.fetchone(
                "SELECT source FROM torrents WHERE id=?", (torrent_id,)
            )
            transfer_source = source_row.get("source") if source_row else None
            await db.execute(
                "UPDATE torrents SET status=?, local_path=?, size_bytes=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (final_status, str(destination_root), total_size_bytes, torrent_id),
            )
            if final_status == "completed":
                await db.execute("UPDATE torrents SET completed_at=CURRENT_TIMESTAMP WHERE id=?", (torrent_id,))
            # Build a descriptive event message
            if blocked_count == total_files and total_files > 0:
                _evt_msg = f"All {blocked_count} file(s) filtered/blocked — marked completed, removed from AllDebrid"
                _evt_lvl = "info"
            elif blocked_count > 0:
                _evt_msg = f"Download {final_status}: {completed_count + queued_count} files prepared, {blocked_count} filtered"
                _evt_lvl = "info" if final_status in {"completed", "queued", "paused"} else "warn"
            else:
                _evt_msg = f"Download {final_status}: {completed_count + queued_count} files prepared"
                _evt_lvl = "info" if final_status in {"completed", "queued", "paused"} else "warn"
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                (torrent_id, _evt_lvl, _evt_msg),
            )
            await db.commit()

        await self._send_partial_summary(
            torrent_id,
            name,
            flat_files,
            blocked_items,
            transferred_items + queued_items,
            failed_items,
        )

        if final_status == "completed":
            await self._delete_magnet_after_completion(torrent_id, ad_id, transfer_source)
            await self._mark_finished(torrent_id, name=name)
            # For all-blocked torrents: partial notification already sent above;
            # skip the completed notification to avoid a confusing "0 files" message.
            if cfg.discord_notify_finished and blocked_count < total_files:
                await self.notify().send_complete(name, file_count=completed_count, destination=str(destination_root), download_client="aria2")
        elif final_status in {"queued", "paused"}:
            await self._log_event(
                torrent_id,
                "info",
                "Prepared for slot-based aria2 delivery",
            )
            await self.advance_aria2_queue()
        else:
            await self._notify_provider_error(
                name,
                reason="Kept on AllDebrid for inspection",
                context="At least one file failed during preparation, so the torrent was left on AllDebrid.",
                alldebrid_id=str(ad_id or ""),
            )

    async def _fetch_ready_files(self, ad_id: str) -> List[Dict]:
        for attempt in range(1, READY_FILE_RETRIES + 1):
            files_data = await self.ad().get_magnet_files([ad_id])
            for entry in files_data:
                if str(entry.get("id", "")) == str(ad_id):
                    flat_files = flatten_files(entry.get("files", []))
                    if flat_files:
                        return flat_files
            await asyncio.sleep(attempt)
        try:
            status_rows = await self.ad().get_magnet_status(ad_id)
        except Exception:
            status_rows = []
        if status_rows:
            magnet = status_rows[0]
            normalized = normalize_provider_state(magnet)
            provider_status = str(normalized["provider_status"])
            provider_message = str(normalized["message"] or "").strip()
            status_code = int(normalized["status_code"])
            if provider_status in {"ready", "processing", "queued"}:
                raise TransientAllDebridStateError(
                    f"AllDebrid did not expose downloadable files yet (status {provider_status} [{status_code}] {provider_message})"
                )
            if provider_status == "error":
                raise Exception(
                    f"AllDebrid reported magnet error {status_code}: {provider_message}".strip()
                )
        raise TransientAllDebridStateError(
            "AllDebrid did not return downloadable files and magnet status could not be confirmed yet"
        )

    def _remote_aria2_path(self, local_path: Path) -> str:
        cfg = get_settings()
        if cfg.aria2_download_path and not is_builtin_mode(cfg):
            relative = local_path.relative_to(Path(cfg.download_folder))
            return str(PurePosixPath(cfg.aria2_download_path.replace("\\", "/")) / PurePosixPath(str(relative).replace("\\", "/")))
        return str(PurePosixPath(str(local_path).replace("\\", "/")))

    def _build_aria2_indexes(self, all_downloads):
        by_gid = {download.gid: download for download in all_downloads}
        uri_to_dl = {}
        path_to_dl = {}
        for dl in all_downloads:
            for fi in dl.files or []:
                current_path = _normalize_aria2_path(str(fi.get("path", "")))
                if current_path:
                    path_to_dl[current_path] = dl
                for u in fi.get("uris", []) or []:
                    uri = str(u.get("uri", "")).strip()
                    if uri:
                        uri_to_dl[uri] = dl
        return by_gid, uri_to_dl, path_to_dl

    def _aria2_slot_limit(self) -> int:
        cfg = get_settings()
        value = int(getattr(cfg, "aria2_max_active_downloads", 0) or 0)
        if value <= 0:
            value = int(cfg.max_concurrent_downloads or 1)
        return max(1, value)

    @staticmethod
    def _aria2_slot_occupants(downloads):
        """Return jobs currently occupying the controlled aria2 queue.

        Paused jobs remain registered with aria2 so they can resume from their
        control files, but they must not reserve a DebridPulse transfer slot.
        """
        return [
            download for download in downloads
            if download.status in {"active", "waiting"}
        ]

    def _aria2_state_windows(self) -> tuple[int, int]:
        cfg = get_settings()
        waiting = int(getattr(cfg, "aria2_waiting_window", 100) or 100)
        stopped = int(getattr(cfg, "aria2_stopped_window", 100) or 100)
        return max(10, min(1000, waiting)), max(10, min(1000, stopped))

    async def _engine_aria2_get_all(self):
        waiting, stopped = self._aria2_state_windows()
        return await self.aria2().get_all(waiting_limit=waiting, stopped_limit=stopped)

    async def _engine_aria2_confirm_gid(self, gid: str):
        """Resolve one GID atomically after a bulk snapshot misses it.

        aria2's active, waiting, and stopped lists are separate RPC snapshots.
        A job that changes state while get_all() is gathering them can be absent
        from that combined result even though the GID still exists.  Only an
        explicit tellStatus failure confirms that the GID is gone.
        """
        gid = str(gid or "").strip()
        if not gid:
            return None
        try:
            return await self.aria2().tell_status(gid)
        except Aria2ConnectionError:
            raise
        except Aria2RPCError as exc:
            logger.debug("aria2 tellStatus could not resolve GID %s: %s", gid, exc)
            return None

    async def _aria2_get_memory_diagnostics(self):
        waiting, stopped = self._aria2_state_windows()
        return await self.aria2().get_memory_diagnostics(waiting_limit=waiting, stopped_limit=stopped)

    async def _engine_dispatch_pending_aria2_queue(self, all_downloads=None):
        """
        The single authoritative gate between our DB and aria2.

        Invariant: at any point, at most aria2_max_active_downloads ADC-owned
        files may have status active/waiting in aria2 at the same time. Paused
        jobs remain resumable but do not reserve transfer capacity.
        Foreign jobs in an external daemon are neither counted nor changed.

        Steps:
        1. Fetch current aria2 state once.
        2. Count slot occupants (active + waiting; paused is parked).
        3. If over the limit (e.g. settings were reduced): remove the
           excess entries from aria2 and reset those download_files to
           pending so they are re-queued in order on the next cycle.
        4. Fill available slots from pending download_files, oldest first.
        """
        # Global Pause is a strict queue-wide gate. Item-level Resume exits
        # global pause before it reaches the manager, leaving any other paused
        # parents as selective pauses instead of creating hidden exceptions.
        if self.download_client_name() != "aria2" or self.is_paused():
            return
        # Disk-space guard: block ALL new dispatches while active.
        # This is the authoritative gate for aria2 dispatching — it runs on
        # every sync cycle, so every code path that would start an aria2
        # download is blocked here, regardless of how the torrent got ready.
        if self._disk_guard_active:
            return

        async with self._aria2_dispatch_lock:
            current_downloads = (
                all_downloads if all_downloads is not None
                else await self._aria2_get_all()
            )
            owned_gids = (
                {str(dl.gid) for dl in current_downloads}
                if is_builtin_mode()
                else await self._aria2_owned_gids()
            )
            owned_downloads = [
                dl for dl in current_downloads
                if str(dl.gid) in owned_gids
            ]
            limit = self._aria2_slot_limit()
            in_flight = self._aria2_slot_occupants(owned_downloads)

            # ── Step 3: trim excess if limit was lowered ─────────────────────
            if len(in_flight) > limit:
                excess = in_flight[limit:]  # oldest are last — remove from end
                excess_gids = {dl.gid for dl in excess}
                logger.info(
                    "aria2 queue trim: %d in-flight > limit %d, removing %d",
                    len(in_flight), limit, len(excess),
                )
                # Find download_files rows for these GIDs and reset to pending
                async with get_db() as db:
                    gid_placeholders = ",".join("?" * len(excess_gids))
                    stale = await (await db.execute(
                        f"SELECT id FROM download_files WHERE download_id IN ({gid_placeholders})",
                        list(excess_gids),
                    )).fetchall()
                    if stale:
                        ids = [r["id"] for r in stale]
                        id_placeholders = ",".join("?" * len(ids))
                        await db.execute(
                            f"""UPDATE download_files
                               SET status='pending', download_id=NULL,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE id IN ({id_placeholders})""",
                            ids,
                        )
                        await db.commit()
                for dl in excess:
                    await self._remove_owned_aria2_gid(dl.gid)
                owned_downloads = [
                    dl for dl in owned_downloads if dl.gid not in excess_gids
                ]
                in_flight = in_flight[:limit]

            # ── Step 4: fill available slots ─────────────────────────────────
            available_slots = max(0, limit - len(in_flight))
            if available_slots <= 0:
                return

            async with get_db() as db:
                pending_rows = await (
                    await db.execute(
                        """SELECT f.id AS file_id, f.torrent_id, f.filename,
                                  f.source_url, f.download_url, f.local_path,
                                  t.name AS torrent_name, t.source AS transfer_source
                           FROM download_files f
                           JOIN torrents t ON t.id = f.torrent_id
                           WHERE f.download_client='aria2'
                             AND f.blocked=0
                             AND f.status='pending'
                             AND t.status NOT IN ('completed','deleted','error')
                           ORDER BY t.priority DESC, t.id ASC
                           LIMIT ?""",
                        (available_slots,),
                    )
                ).fetchall()

            if not pending_rows:
                return

            # Limit how many files enter aria2's waiting queue at once.
            # Each waiting download allocates ~5–15 KB in aria2's C++ heap.
            # Without a cap, a 200-file torrent adds 200 waiting entries →
            # aria2 grows 1–3 MB per large torrent and glibc does not release it.
            # Keep the queue at max_concurrent × 4 slots; the rest stay as
            # "pending" in the DB and get dispatched on the next poll cycle.
            cfg_disp = get_settings()
            _max_concurrent = int(getattr(cfg_disp, "aria2_max_active_downloads", 3) or 3)
            _dispatch_cap = _max_concurrent * 4
            if len(pending_rows) > _dispatch_cap:
                logger.debug(
                    "aria2 dispatch: capping batch from %d → %d to limit aria2 RAM",
                    len(pending_rows), _dispatch_cap,
                )
                pending_rows = pending_rows[:_dispatch_cap]

            logger.info(
                "aria2 dispatch: %d slot(s) free, dispatching %d file(s)",
                available_slots, len(pending_rows),
            )

            # Reuse the authoritative ownership-filtered snapshot from the
            # start of this serialized dispatch pass. ensure_download() receives
            # the same view used for slot accounting, eliminating a redundant
            # active/waiting/stopped snapshot immediately before addUri.
            dispatch_snapshot = list(owned_downloads)

            # ── Unlock all pending links in parallel (rate-limited) ──────────
            # Semaphore caps concurrent AllDebrid API calls to avoid 503 errors
            # when dispatching large batches (100+ files).
            _dispatch_sem = asyncio.Semaphore(3)

            async def _unlock_for_dispatch(row_: dict) -> dict:
                async with _dispatch_sem:
                    sl = str(
                        row_.get("source_url")
                        if row_.get("transfer_source") == DIRECT_LINK_SOURCE
                        else row_.get("download_url")
                        or ""
                    ).strip()
                    try:
                        result = await _retry_async(self.ad().unlock_link, sl)
                        dl_url = result.get("link", "")
                        if not dl_url:
                            raise Exception("Empty download URL from unlock")
                        return {**row_, "_dl_url": dl_url, "_err": None}
                    except Exception as exc:
                        return {**row_, "_dl_url": "", "_err": exc}

            unlocked_rows = await asyncio.gather(
                *[_unlock_for_dispatch(r) for r in pending_rows]
            )

            for row in unlocked_rows:
                local_path = Path(row["local_path"])
                if row["_err"]:
                    error = row["_err"]
                    capability = str(
                        row.get("source_url")
                        if row.get("transfer_source") == DIRECT_LINK_SOURCE
                        else row.get("download_url")
                        or ""
                    ).strip()
                    error_text = _safe_persisted_error(error, capability)
                    provider_code = str(getattr(error, "code", "") or "")
                    if (
                        provider_code == "LINK_HOST_NOT_SUPPORTED"
                        or "LINK_HOST_NOT_SUPPORTED" in error_text
                    ):
                        logger.warning(
                            "aria2 dispatch blocked unsupported provider file [%s]: %s",
                            row["filename"],
                            error_text,
                        )
                        async with get_db() as db:
                            await db.execute(
                                """UPDATE download_files
                                   SET status='blocked', blocked=1, block_reason=?,
                                       download_id=NULL, updated_at=CURRENT_TIMESTAMP
                                   WHERE id=?""",
                                (error_text, row["file_id"]),
                            )
                            await db.commit()
                    else:
                        logger.error(
                            "aria2 dispatch failed [%s]: %s",
                            row["filename"],
                            error_text,
                        )
                        await self._update_file_state(
                            row["file_id"],
                            "error",
                            row["local_path"],
                            reason=error_text,
                        )
                    await self._finalize_aria2_torrent(row["torrent_id"])
                    continue
                try:
                    download_url = row["_dl_url"]
                    remote_path = self._remote_aria2_path(local_path)
                    remote_dir  = str(PurePosixPath(remote_path).parent)
                    remote_name = PurePosixPath(remote_path).name
                    job_options = self._aria2_job_options({
                        "dir": remote_dir,
                        "out": remote_name,
                    })
                    gid = await self.aria2().ensure_download(
                        download_url,
                        job_options,
                        get_settings().aria2_start_paused,
                        cached_downloads=dispatch_snapshot,
                    )
                    await self._record_aria2_owned_gid(
                        gid,
                        download_file_id=row["file_id"],
                        torrent_id=row["torrent_id"],
                    )
                    queued_status = "paused" if get_settings().aria2_start_paused else "queued"
                    async with get_db() as db:
                        await db.execute(
                            """UPDATE download_files
                               SET status=?, download_id=?, download_url=?,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE id=?""",
                            (queued_status, gid, download_url, row["file_id"]),
                        )
                        await db.execute(
                            """UPDATE torrents SET status=?, updated_at=CURRENT_TIMESTAMP
                               WHERE id=? AND status NOT IN ('completed','deleted','error')""",
                            (queued_status, row["torrent_id"]),
                        )
                        await db.commit()
                    logger.info(
                        "aria2 dispatch: %s → GID %s (torrent %s)",
                        row["filename"], gid, row["torrent_id"],
                    )
                    if row.get("transfer_source") == DIRECT_LINK_SOURCE:
                        await self._log_event(
                            row["torrent_id"],
                            "info",
                            f"Generated URL queued in aria2: {row['filename']}",
                        )
                        await self._broadcast_direct_link_update(
                            row["torrent_id"],
                            queued_status,
                            str(row.get("torrent_name") or "Debrid links"),
                            0.0,
                        )
                except Exception as exc:
                    safe_error = _safe_persisted_error(exc)
                    logger.error("aria2 dispatch failed [%s]: %s", row["filename"], safe_error)
                    await self._update_file_state(
                        row["file_id"], "error", row["local_path"], reason=safe_error
                    )
                    await self._finalize_aria2_torrent(row["torrent_id"])

    async def _schedule_ready_aria2_parents(self) -> int:
        """Materialize the next provider-ready parents into the local file queue.

        ``ready`` is a real queue stage but has no download_files yet, so the
        aria2 dispatcher cannot see it. Scheduling it here keeps provider-ready
        and file-pending advancement in the same control path.
        """
        if (
            self.download_client_name() != "aria2"
            or self.is_paused()
            or self._disk_guard_active
        ):
            return 0

        limit = max(1, int(get_settings().max_concurrent_downloads or 1))
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT id, alldebrid_id, name
                   FROM torrents
                   WHERE status='ready'
                     AND provider_status='ready'
                     AND alldebrid_id IS NOT NULL
                     AND alldebrid_id != ''
                   ORDER BY priority DESC, id ASC
                   LIMIT ?""",
                (limit,),
            )

        scheduled = 0
        for row in rows:
            torrent_id = int(row["id"])
            if torrent_id in self._active:
                continue
            if self._schedule_ready_parent_download(
                torrent_id,
                str(row["alldebrid_id"]),
                str(row["name"] or ""),
            ):
                scheduled += 1
        return scheduled

    async def _engine_advance_aria2_queue_locked(self) -> int:
        """Advance both local file slots and provider-ready parent work."""
        if (
            self.download_client_name() != "aria2"
            or self.is_paused()
            or self._disk_guard_active
        ):
            return 0
        await self._dispatch_pending_aria2_queue()
        return await self._schedule_ready_aria2_parents()

    async def advance_aria2_queue(self) -> int:
        """Public serialized queue kick used by controls and preparation tasks."""
        async with self._aria2_state_lock:
            return await self._advance_aria2_queue_locked()

    async def _engine_sync_download_clients(self):
        async with self._aria2_state_lock:
            if self.download_client_name() == "aria2":
                await self.sync_aria2_downloads()
                # Enforce slots and materialize provider-ready successors.
                await self._advance_aria2_queue_locked()
                await self._cleanup_aria2_orphans()

    async def _cleanup_aria2_orphans(self):
        """
        Removes 'complete' or 'error' entries from aria2 that either:
        - Have no matching download_files row (orphaned GID)
        - Correspond to a download_files row already marked 'completed'
        This prevents aria2's stopped list from accumulating stale entries.
        """
        if self.download_client_name() != "aria2" or self.is_paused():
            return
        if not is_builtin_mode():
            # A shared daemon's stopped list is user-visible history. ADC does
            # not own its retention policy, including for ADC-created GIDs.
            return
        try:
            all_downloads = await self._aria2_get_all()
        except Exception:
            return

        stopped = [dl for dl in all_downloads if dl.status in {"complete", "removed", "error"}]
        if not stopped:
            return

        # Collect all known GIDs from DB that are still active
        try:
            async with get_db() as db:
                rows = await (await db.execute(
                    """SELECT download_id, status FROM download_files
                       WHERE download_id IS NOT NULL
                         AND status NOT IN ('completed', 'error', 'blocked')"""
                )).fetchall()
            active_gids = {str(r["download_id"]) for r in rows}
        except Exception as exc:
            logger.debug("_cleanup_aria2_orphans: DB query failed: %s", exc)
            return

        removed = 0
        for dl in stopped:
            if dl.gid not in active_gids:
                await self.aria2().remove(dl.gid)
                removed += 1

        if removed:
            logger.info("aria2 orphan cleanup: removed %d stale finished/error entries", removed)

    async def sync_aria2_downloads(self):
        # Global pause blocks new dispatches, but status monitoring must remain
        # live. Otherwise an individually resumed transfer would run in aria2
        # without progress or completion being reflected in DebridPulse.
        if self.download_client_name() != "aria2":
            return

        all_downloads = await self._aria2_get_all()
        by_gid, uri_to_dl, path_to_dl = self._build_aria2_indexes(all_downloads)

        async with get_db() as db:
            rows = await (
                await db.execute(
                    """SELECT t.id AS torrent_id, t.name, t.alldebrid_id, t.status AS torrent_status,
                              f.id AS file_id, f.filename, f.local_path, f.download_url,
                              f.download_id, f.status, f.blocked, f.size_bytes
                       FROM torrents t
                       JOIN download_files f ON f.torrent_id = t.id
                       WHERE f.download_client='aria2'
                         AND f.blocked=0
                         AND f.status IN ('queued', 'downloading', 'paused')
                         AND f.download_id IS NOT NULL"""
                )
            ).fetchall()

        touched: Set[int] = set()
        reset_on_sync: Set[int] = set()
        for row in rows:
            if row["torrent_id"] in reset_on_sync:
                continue  # already scheduled for reset
            if row["torrent_id"] in self._active:
                continue  # _start_download/_download is running — leave it alone

            gid = str(row["download_id"] or "").strip()
            dl = by_gid.get(gid)

            if dl is None and gid:
                try:
                    dl = await self._aria2_confirm_gid(gid)
                except Aria2ConnectionError as exc:
                    logger.warning(
                        "sync_aria2: connection lost while confirming GID %s; skipping cycle: %s",
                        gid, exc,
                    )
                    return

            if row["status"] == "pending":
                continue

            if dl is None:
                remote_path = ""
                if row["local_path"]:
                    try:
                        remote_path = _normalize_aria2_path(self._remote_aria2_path(Path(row["local_path"])))
                    except Exception:
                        remote_path = _normalize_aria2_path(str(row["local_path"]))
                url = str(row["download_url"] or "").strip()
                if is_builtin_mode():
                    dl = path_to_dl.get(remote_path) if remote_path else None
                    if dl is None and url:
                        dl = uri_to_dl.get(url)

                if dl is not None:
                    # Found under different GID — update DB and fall through to status sync
                    async with get_db() as db:
                        await db.execute(
                            "UPDATE download_files SET download_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (dl.gid, row["file_id"]),
                        )
                        await db.commit()
                    logger.info(
                        "Synced stale GID -> %s for torrent %s file %s via %s",
                        dl.gid,
                        row["torrent_id"],
                        row["file_id"],
                        "path" if remote_path and path_to_dl.get(remote_path) is dl else "url",
                    )
                elif remote_path or url:
                    logger.info(
                        "sync_aria2: aria2 entry not found for torrent %s file %s (path=%s) -> scheduling reset",
                        row["torrent_id"], row["file_id"], remote_path or "-",
                    )
                    reset_on_sync.add(row["torrent_id"])
                    continue
                else:
                    # No URL to look up — reset
                    reset_on_sync.add(row["torrent_id"])
                    continue

            # Status sync from aria2. Avoid rewriting stable rows every poll;
            # download_files.updated_at should advance only for a real state/size change.
            sz = dl.total_length if dl.total_length > 0 else None
            current_file_status = str(row["status"] or "")
            current_file_size = int(row.get("size_bytes") or 0)
            size_changed = sz is not None and int(sz) != current_file_size

            def file_state_needs_update(desired_status: str) -> bool:
                return desired_status != current_file_status or size_changed

            if dl.status == "paused":
                if file_state_needs_update("paused"):
                    await self._update_file_state(row["file_id"], "paused", row["local_path"], size_bytes=sz)
            elif dl.status == "waiting":
                if file_state_needs_update("queued"):
                    await self._update_file_state(row["file_id"], "queued", row["local_path"], size_bytes=sz)
            elif dl.status == "active":
                if file_state_needs_update("downloading"):
                    await self._update_file_state(row["file_id"], "downloading", row["local_path"], size_bytes=sz)
            elif dl.status == "complete":
                if row["status"] != "completed":
                    await self._update_file_state(row["file_id"], "completed", row["local_path"], size_bytes=sz)
                    touched.add(row["torrent_id"])
                logger.debug(
                    "aria2 completion retained for GID %s, file %s (torrent %s)",
                    dl.gid,
                    row["file_id"],
                    row["torrent_id"],
                )
            elif dl.status == "removed":
                logger.info(
                    "sync_aria2: aria2 job was removed for torrent %s file %s -> scheduling reset",
                    row["torrent_id"], row["file_id"],
                )
                reset_on_sync.add(row["torrent_id"])
                continue
            elif dl.status == "error":
                reason = f"{dl.error_code}: {dl.error_message}".strip(": ")
                await self._update_file_state(row["file_id"], "error", row["local_path"], reason=reason, size_bytes=sz)
                touched.add(row["torrent_id"])

        # Reset torrents whose entries are gone from aria2 (can't confirm completion)
        for torrent_id in reset_on_sync - touched:
            t = await self._get_torrent_completion_snapshot(torrent_id)
            if not t:
                continue
            # Don't reset if torrent is already in a terminal state
            if t["status"] in ("completed", "deleted", "error"):
                logger.debug(
                    "sync_aria2: skip reset for torrent %s — already %s",
                    torrent_id, t["status"],
                )
                continue
            # Don't reset if all non-blocked files are already completed
            if t["total"] > 0 and t["done"] >= t["total"]:
                logger.info(
                    "sync_aria2: torrent %s — all %d files completed, finalising instead of reset",
                    torrent_id, t["total"],
                )
                await self._finalize_aria2_torrent(torrent_id)
                continue
            logger.info(
                "sync_aria2: resetting torrent %s (aria2 entry lost, %d/%d files done)",
                torrent_id, t["done"], t["total"],
            )
            await self._reset_torrent_for_redownload(
                torrent_id, "aria2 entry lost during sync — reset for re-download"
            )
            if t["alldebrid_id"]:
                self._schedule_ready_parent_download(
                    torrent_id,
                    str(t["alldebrid_id"]),
                    str(t["name"] or ""),
                )

        # Finalize torrents where aria2 reported complete or error
        for torrent_id in touched:
            await self._finalize_aria2_torrent(torrent_id)

        # ── Stragglers: torrents stuck in active state but all files already done ──
        # Happens when _finalize previously threw an exception after files were marked
        # completed, or after a restart where download_files rows survived but the
        # torrent status was not updated.  The normal rows query (status IN queued/
        # downloading/paused) skips files that are already 'completed', so these
        # torrents never appear in touched and _finalize is never called again.
        try:
            async with get_db() as db:
                straggler_rows = await (await db.execute(
                    """SELECT DISTINCT torrent_id
                       FROM download_files
                       WHERE torrent_id IN (
                           SELECT id FROM torrents
                           WHERE status IN ('queued', 'downloading')
                             AND download_client = 'aria2'
                       )
                       GROUP BY torrent_id
                       HAVING SUM(CASE WHEN blocked=0 AND status != 'completed' THEN 1 ELSE 0 END) = 0
                          AND SUM(CASE WHEN blocked=0 THEN 1 ELSE 0 END) > 0""",
                )).fetchall()
            straggler_ids = (
                {r["torrent_id"] for r in straggler_rows}
                - touched
                - set(self._active)
            )
            if straggler_ids:
                logger.info(
                    "sync_aria2: found %d straggler torrent(s) with all files completed "
                    "but torrent still active — finalising now: %s",
                    len(straggler_ids), sorted(straggler_ids),
                )
                for torrent_id in straggler_ids:
                    if torrent_id in self._active:
                        logger.debug(
                            "sync_aria2: torrent %s is rebuilding its manifest; deferring finalization",
                            torrent_id,
                        )
                        continue
                    await self._finalize_aria2_torrent(torrent_id)
        except Exception as exc:
            logger.warning("sync_aria2: straggler check failed: %s", exc)

        # Publish aggregate collection progress. The serialized caller advances
        # the queue once after reconciliation, avoiding two competing kicks.
        await self._update_aria2_parent_progress(all_downloads)

    async def _engine_reset_torrent_for_redownload(self, torrent_id: int, reason: str):
        """Clear download_files and mark torrent as downloading so the sync loop
        ignores it while _start_download/_download re-runs and re-registers
        the new URIs with aria2. Status is updated to 'queued' or 'paused' once
        _download() completes and the new download_files rows are written."""
        direct_links: List[str] = []
        async with get_db() as db:
            transfer = await db.fetchone(
                "SELECT source, magnet FROM torrents WHERE id=?", (torrent_id,)
            )
            is_direct = bool(
                transfer
                and str(transfer.get("source") or "") == DIRECT_LINK_SOURCE
            )
            if is_direct:
                try:
                    direct_links = normalize_direct_links(
                        json.loads(transfer.get("magnet") or "[]")
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not restore direct-link payload for transfer %s: %s",
                        torrent_id,
                        exc,
                    )
            await db.execute("DELETE FROM download_files WHERE torrent_id=?", (torrent_id,))
            await db.execute(
                """UPDATE torrents
                   SET status=?, provider_status=?, error_message=NULL,
                       progress=0, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (
                    "processing" if is_direct else "downloading",
                    "submitted" if is_direct else None,
                    torrent_id,
                ),
            )
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                (torrent_id, "warn", reason),
            )
            await db.commit()
        if is_direct and direct_links:
            self._schedule_direct_link_collection(torrent_id, direct_links)

    async def reconcile_aria2_on_startup(self):
        """Called once at startup to reconcile DB state with what aria2 actually has.

        1. GID still in aria2 → sync status directly.
        2. In built-in mode only, a matching URI/path under a new GID may be
           adopted. External mode never adopts an unknown shared-daemon job.
        3. GID gone, URI not in aria2 → reset download_files and re-queue via
           _start_download. We cannot know if the download completed cleanly or
           was dropped, so a safe re-download is the only correct action.
        """
        if self.download_client_name() != "aria2":
            return
        try:
            all_downloads = await self._aria2_get_all()
        except Exception as exc:
            logger.warning("Startup aria2 reconciliation skipped: %s", exc)
            return

        all_downloads = await self._dedupe_aria2_downloads_on_startup(all_downloads)

        by_gid, uri_to_dl, path_to_dl = self._build_aria2_indexes(all_downloads)

        async with get_db() as db:
            rows = await (
                await db.execute(
                    """SELECT t.id AS torrent_id, t.alldebrid_id, t.name,
                              f.id AS file_id, f.download_id, f.download_url,
                              f.local_path, f.status
                       FROM torrents t
                       JOIN download_files f ON f.torrent_id = t.id
                       WHERE f.download_client='aria2'
                         AND f.blocked=0
                         AND f.status IN ('pending', 'queued', 'downloading', 'paused')
                       ORDER BY t.id ASC, f.id ASC"""
                )
            ).fetchall()

        touched: Set[int] = set()
        reset_torrents: Set[int] = set()

        for row in rows:
            if row["torrent_id"] in reset_torrents:
                continue  # whole torrent already scheduled for reset

            if row["status"] == "pending":
                continue

            gid = str(row["download_id"] or "").strip()
            dl = by_gid.get(gid)

            if dl is None and gid:
                try:
                    dl = await self._aria2_confirm_gid(gid)
                except Aria2ConnectionError as exc:
                    logger.warning(
                        "Startup aria2 reconciliation deferred while confirming GID %s: %s",
                        gid, exc,
                    )
                    return

            if dl is None:
                remote_path = ""
                if row["local_path"]:
                    try:
                        remote_path = _normalize_aria2_path(self._remote_aria2_path(Path(row["local_path"])))
                    except Exception:
                        remote_path = _normalize_aria2_path(str(row["local_path"]))
                url = str(row["download_url"] or "").strip()
                if is_builtin_mode():
                    dl = path_to_dl.get(remote_path) if remote_path else None
                    if dl is None and url:
                        dl = uri_to_dl.get(url)
                if dl:
                    # Case 2: same path or URI under new GID — update and fall through to sync
                    async with get_db() as db:
                        await db.execute(
                            "UPDATE download_files SET download_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (dl.gid, row["file_id"]),
                        )
                        await db.commit()
                    logger.info(
                        "Reconciled GID %s -> %s for torrent %s file %s via %s",
                        gid or "(none)",
                        dl.gid,
                        row["torrent_id"],
                        row["file_id"],
                        "path" if remote_path and path_to_dl.get(remote_path) is dl else "url",
                    )
                else:
                    # Case 3: gone — reset whole torrent and re-queue
                    snapshot = await self._get_torrent_completion_snapshot(row["torrent_id"])
                    if snapshot and snapshot["status"] not in ("completed", "deleted", "error") and snapshot["total"] > 0 and snapshot["done"] >= snapshot["total"]:
                        logger.info(
                            "Startup reconcile: torrent %s already has all %d files completed -> finalising instead of reset",
                            row["torrent_id"], snapshot["total"],
                        )
                        touched.add(row["torrent_id"])
                        continue
                    logger.info(
                        "Startup reconcile: GID %s not in aria2 for torrent %s → resetting",
                        gid, row["torrent_id"],
                    )
                    reset_torrents.add(row["torrent_id"])
                    await self._reset_torrent_for_redownload(
                        row["torrent_id"],
                        f"aria2 entry lost (GID {gid}) — reset for re-download on startup",
                    )
                    continue

            # Cases 1 + 2: sync status from aria2
            sz = dl.total_length if dl.total_length > 0 else None
            if dl.status == "complete":
                await self._update_file_state(row["file_id"], "completed", row["local_path"], size_bytes=sz)
                touched.add(row["torrent_id"])
            elif dl.status == "removed":
                logger.info(
                    "Startup reconcile: aria2 job was removed for torrent %s file %s -> scheduling reset",
                    row["torrent_id"], row["file_id"],
                )
                reset_torrents.add(row["torrent_id"])
                await self._reset_torrent_for_redownload(
                    row["torrent_id"],
                    f"aria2 entry removed (GID {dl.gid}) -> reset for re-download on startup",
                )
            elif dl.status == "error":
                reason = f"{dl.error_code}: {dl.error_message}".strip(": ")
                await self._update_file_state(row["file_id"], "error", row["local_path"], reason=reason, size_bytes=sz)
                touched.add(row["torrent_id"])
            elif dl.status == "active":
                await self._update_file_state(row["file_id"], "downloading", row["local_path"], size_bytes=sz)
            elif dl.status == "waiting":
                await self._update_file_state(row["file_id"], "queued", row["local_path"], size_bytes=sz)
            elif dl.status == "paused":
                await self._update_file_state(row["file_id"], "paused", row["local_path"], size_bytes=sz)

        for torrent_id in touched:
            await self._finalize_aria2_torrent(torrent_id)

        for torrent_id in reset_torrents:
            async with get_db() as db:
                t = await (await db.execute(
                    "SELECT alldebrid_id, name FROM torrents WHERE id=?", (torrent_id,)
                )).fetchone()
            if t and t["alldebrid_id"]:
                self._schedule_ready_parent_download(
                    torrent_id,
                    str(t["alldebrid_id"]),
                    str(t["name"] or ""),
                )

        await self.advance_aria2_queue()
        recovered = await self.recover_direct_link_collections()
        if recovered:
            logger.info(
                "Startup: resumed %d interrupted direct-link transfer(s)", recovered
            )

    async def _dedupe_aria2_downloads_on_startup(self, all_downloads):
        if not is_builtin_mode():
            # Deduplication by URI alone cannot prove ownership in a shared
            # daemon and may remove an AriaNg-created job.
            return all_downloads
        by_uri: Dict[str, List] = {}
        removed_gids: Set[str] = set()
        for dl in all_downloads:
            for fi in dl.files or []:
                for u in fi.get("uris", []) or []:
                    uri = str(u.get("uri", "")).strip()
                    if uri:
                        by_uri.setdefault(uri, []).append(dl)

        duplicate_sets = 0
        removed = 0
        for uri, matches in by_uri.items():
            unique: List = []
            seen_gids: Set[str] = set()
            for dl in matches:
                if dl.gid and dl.gid not in seen_gids:
                    unique.append(dl)
                    seen_gids.add(dl.gid)

            if len(unique) <= 1:
                continue

            duplicate_sets += 1
            unique.sort(key=lambda dl: (_aria2_status_rank(dl.status), dl.gid))
            keep = unique[0]
            for dup in unique[1:]:
                logger.warning(
                    "Startup aria2 dedupe removed duplicate gid %s for %s; keeping %s (%s)",
                    dup.gid,
                    uri,
                    keep.gid,
                    keep.status,
                )
                await self.aria2().remove(dup.gid)
                removed_gids.add(dup.gid)
                removed += 1

        if duplicate_sets:
            logger.info(
                "Startup aria2 dedupe finished: %s duplicate url groups, %s duplicate jobs removed",
                duplicate_sets,
                removed,
            )
            return [dl for dl in all_downloads if dl.gid not in removed_gids]

        return all_downloads


    async def _engine_update_aria2_parent_progress(self, all_downloads=None):
        """Aggregate per-file aria2 progress into each active parent torrent.

        Completed files remain represented by their persisted size after aria2
        purges their GIDs. Live jobs contribute completed_length. Blocked files
        are excluded from both numerator and denominator.
        """
        if all_downloads is None:
            all_downloads = await self._aria2_get_all()

        by_gid, _, _ = self._build_aria2_indexes(all_downloads)

        async with get_db() as db:
            rows = await (
                await db.execute(
                    """SELECT
                           t.id AS torrent_id,
                           t.status AS torrent_status,
                           t.progress AS torrent_progress,
                           f.id AS file_id,
                           f.status AS file_status,
                           f.size_bytes,
                           f.download_id
                       FROM torrents t
                       JOIN download_files f ON f.torrent_id = t.id
                       WHERE t.download_client = 'aria2'
                         AND t.status IN ('queued', 'downloading', 'paused')
                         AND f.download_client = 'aria2'
                         AND f.blocked = 0
                         AND f.status != 'missing'
                       ORDER BY t.id, f.id"""
                )
            ).fetchall()

        grouped = {}
        for row in rows:
            grouped.setdefault(row["torrent_id"], []).append(row)

        updates = []
        changed_updates = []
        broadcast_needed = False

        for torrent_id, files in grouped.items():
            total_bytes = 0
            completed_bytes = 0
            completed_files = 0
            unfinished_files = 0
            paused_files = 0
            live_active = False

            for row in files:
                status = str(row["file_status"] or "")
                gid = str(row["download_id"] or "")
                dl = by_gid.get(gid) if gid else None

                persisted_size = int(row["size_bytes"] or 0)
                live_size = int(dl.total_length or 0) if dl is not None else 0
                effective_size = max(persisted_size, live_size)

                total_bytes += effective_size

                if status == "completed":
                    completed_files += 1
                    completed_bytes += effective_size
                    continue

                unfinished_files += 1

                if status == "paused":
                    paused_files += 1

                if dl is not None:
                    if dl.status == "active":
                        live_active = True

                    live_completed = max(int(dl.completed_length or 0), 0)

                    if effective_size > 0:
                        live_completed = min(live_completed, effective_size)

                    completed_bytes += live_completed

            if total_bytes > 0:
                progress = round(completed_bytes / total_bytes * 100, 1)
            elif files:
                progress = round(completed_files / len(files) * 100, 1)
            else:
                progress = 0.0

            # The finalizer owns the transition to a true completed state.
            if unfinished_files > 0:
                progress = min(progress, 99.9)
            else:
                progress = 100.0

            if live_active:
                parent_status = "downloading"
            elif unfinished_files > 0 and paused_files == unfinished_files:
                parent_status = "paused"
            elif unfinished_files > 0:
                parent_status = "queued"
            else:
                parent_status = str(files[0]["torrent_status"])

            # Dashboard-visible aggregate change
            current_progress = float(
                files[0]["torrent_progress"] or 0.0
            )
            current_status = str(
                files[0]["torrent_status"] or ""
            )

            # Persist any real progress movement so updated_at continues to
            # represent transfer activity. SSE remains integer-boundary based to
            # avoid UI churn for sub-percent movement.
            persist_progress_changed = progress != current_progress
            broadcast_progress_changed = int(progress) != int(current_progress)
            status_changed = parent_status != current_status

            if persist_progress_changed or status_changed:
                updates.append((progress, parent_status, torrent_id))

            if broadcast_progress_changed or status_changed:
                broadcast_needed = True
                changed_updates.append(
                    {
                        "id": int(torrent_id),
                        "progress": progress,
                        "status": parent_status,
                        "status_changed": status_changed,
                    }
                )

        if not updates:
            return

        async with get_db() as db:
            await db.executemany(
                """UPDATE torrents
                   SET progress=?, status=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?
                     AND status IN ('queued', 'downloading', 'paused')""",
                updates,
            )
            await db.commit()

        logger.debug(
            "Updated aggregate aria2 progress for %d torrent(s)",
            len(updates),
        )

        # SSE: aggregate aria2 progress update
        if broadcast_needed:
            try:
                await publish(
                    "torrent_updated",
                    {
                        "progress_only": not any(
                        item["status_changed"]
                        for item in changed_updates
                    ),
                        "items": changed_updates,
                    },
                )
            except Exception as exc:
                logger.debug(
                    "Aggregate aria2 progress SSE broadcast failed: %s",
                    exc,
                )

    async def _update_file_state(
        self,
        file_id: int,
        status: str,
        local_path: Optional[str],
        reason: Optional[str] = None,
        size_bytes: Optional[int] = None,
    ):
        async with get_db() as db:
            if size_bytes is not None and size_bytes > 0:
                await db.execute(
                    """UPDATE download_files
                       SET status=?, local_path=?, block_reason=?, size_bytes=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (status, local_path, reason, size_bytes, file_id),
                )
            else:
                await db.execute(
                    """UPDATE download_files
                       SET status=?, local_path=?, block_reason=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (status, local_path, reason, file_id),
                )
            await db.commit()

    async def _get_torrent_completion_snapshot(self, torrent_id: int) -> Optional[dict]:
        async with get_db() as db:
            row = await (
                await db.execute(
                    """SELECT t.id, t.alldebrid_id, t.name, t.status,
                              COUNT(CASE WHEN f.blocked=0 AND f.status='completed' THEN 1 END) AS done,
                              COUNT(CASE WHEN f.blocked=0 THEN 1 END) AS total
                       FROM torrents t
                       LEFT JOIN download_files f ON f.torrent_id=t.id
                       WHERE t.id=? GROUP BY t.id""",
                    (torrent_id,),
                )
            ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "alldebrid_id": row["alldebrid_id"],
            "name": row["name"],
            "status": row["status"],
            "done": int(row["done"] or 0),
            "total": int(row["total"] or 0),
        }

    async def _finalize_aria2_torrent(self, torrent_id: int):
        # _download() rebuilds download_files progressively.  Finalizing while
        # that manifest is incomplete can make a partial prefix look complete.
        if torrent_id in self._active:
            logger.debug(
                "torrent %s is rebuilding its manifest; deferring aria2 finalization",
                torrent_id,
            )
            return

        async with get_db() as db:
            torrent = await (await db.execute("SELECT * FROM torrents WHERE id=?", (torrent_id,))).fetchone()
            if not torrent or _terminal_torrent_status(torrent["status"]):
                return

            torrent_dict = dict(torrent)  # always available below

            counts = await (
                await db.execute(
                    """SELECT
                           SUM(CASE WHEN blocked=0 AND status!='missing' THEN 1 ELSE 0 END) AS required_count,
                           SUM(CASE WHEN blocked=0 AND status='completed' THEN 1 ELSE 0 END) AS completed_count,
                           SUM(CASE WHEN blocked=0 AND status='error' THEN 1 ELSE 0 END) AS error_count,
                           SUM(CASE WHEN blocked=0 AND status='missing' THEN 1 ELSE 0 END) AS missing_count,
                           SUM(CASE WHEN blocked=0 AND status IN ('pending', 'queued', 'downloading', 'paused') THEN 1 ELSE 0 END) AS active_count,
                           SUM(CASE WHEN blocked=0 AND status='paused' THEN 1 ELSE 0 END) AS paused_count,
                           SUM(CASE WHEN blocked=0 AND status='downloading' THEN 1 ELSE 0 END) AS downloading_count,
                           COUNT(*) AS total_files
                       FROM download_files WHERE torrent_id=?""",
                    (torrent_id,),
                )
            ).fetchone()

            required_count = int(counts["required_count"] or 0)
            completed_count = int(counts["completed_count"] or 0)
            error_count = int(counts["error_count"] or 0)
            missing_count = int(counts["missing_count"] or 0)
            active_count = int(counts["active_count"] or 0)
            paused_count = int(counts["paused_count"] or 0)
            downloading_count = int(counts["downloading_count"] or 0)
            total_files = int(counts["total_files"] or 0)

            should_complete = False

            if total_files == 0:
                # No file records yet — _download() hasn't run, nothing to do
                return
            elif required_count == 0 and missing_count > 0:
                # Missing source files are terminal failures, not filtered files.
                # Preserve the parent missing/error state established during unlock.
                return
            elif required_count == 0:
                # All files were filtered/blocked — nothing to download
                should_complete = True
                event_msg = "All files were filtered/blocked — marked completed"
            elif required_count > 0 and completed_count == required_count and error_count == 0 and active_count == 0:
                should_complete = True
                event_msg = (
                    f"aria2 completed {completed_count} files; "
                    f"{missing_count} source file(s) missing"
                    if missing_count
                    else f"aria2 completed {completed_count} files"
                )
            elif error_count > 0 and active_count == 0:
                await db.execute(
                    "UPDATE torrents SET status='error', error_message='One or more aria2 transfers failed', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (torrent_id,),
                )
                await db.commit()
                if get_settings().discord_notify_error:
                    await self.notify().send_error(
                        torrent_dict["name"],
                        reason="One or more aria2 transfers failed",
                        source="aria2",
                        provider="aria2",
                    )
                return
            elif active_count > 0:
                # Parent state reflects the most active non-blocked child state:
                # downloading wins over paused; fully paused wins over queued.
                if downloading_count > 0:
                    new_status = "downloading"
                elif paused_count == active_count:
                    new_status = "paused"
                else:
                    new_status = "queued"

                status_changed = str(torrent_dict.get("status") or "") != new_status

                await db.execute(
                    "UPDATE torrents SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (new_status, torrent_id),
                )
                await db.commit()

                if status_changed:
                    try:
                        await publish(
                            "torrent_updated",
                            {
                                "id": torrent_id,
                                "status": new_status,
                                "name": str(torrent_dict.get("name") or ""),
                            },
                        )
                    except Exception as exc:
                        logger.debug(
                            "aria2 parent-status SSE broadcast failed for torrent %s: %s",
                            torrent_id,
                            exc,
                        )
                return
            else:
                return

            if should_complete:
                # Recompute total size from actual file sizes — aria2 provides the
                # authoritative value once downloads run, overwriting any 0 from AllDebrid.
                size_row = await (
                    await db.execute(
                        "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM download_files WHERE torrent_id=?",
                        (torrent_id,),
                    )
                ).fetchone()
                total_size = int(size_row["total"] or 0)
                if total_size > 0:
                    await db.execute(
                        """UPDATE torrents
                           SET status='completed', completed_at=CURRENT_TIMESTAMP,
                               size_bytes=?, progress=100.0,
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (total_size, torrent_id),
                    )
                else:
                    await db.execute(
                        """UPDATE torrents
                           SET status='completed', completed_at=CURRENT_TIMESTAMP,
                               progress=100.0, updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (torrent_id,),
                    )
                await db.execute("INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)", (torrent_id, "info", event_msg))
                await db.commit()

        if str(torrent_dict.get("source") or "") == DIRECT_LINK_SOURCE:
            await self._log_event(
                torrent_id,
                "info",
                "Direct-link transaction completed; no AllDebrid magnet cleanup required",
            )
        else:
            await self._delete_magnet_after_completion(
                torrent_id,
                torrent_dict["alldebrid_id"],
                torrent_dict.get("source"),
            )
        await self._mark_finished(torrent_id, name=torrent_dict.get("name",""))
        # Trigger auto-extraction if enabled
        self._track_maintenance_task(
            self._extract_torrent(torrent_id, torrent_dict),
            label=f"extract-{torrent_id}",
        )
        if get_settings().discord_notify_finished:
            await self.notify().send_complete(
                torrent_dict["name"],
                file_count=completed_count,
                size_bytes=total_size,
                download_client="aria2",
            )

        # Only the dedicated built-in daemon may have its result history purged.
        if is_builtin_mode():
            try:
                await self.aria2().purge_download_results()
            except Exception:
                pass  # non-critical — housekeeping loop will catch up

        # Release kernel page-cache for all downloaded files.
        # When aria2 writes to disk, Linux caches every byte in RAM (page cache).
        # On Unraid / mergerfs this can grow to 10-20+ GB and is not released
        # until another process needs memory. posix_fadvise(DONTNEED) tells the
        # kernel to reclaim those pages immediately — the file on disk is intact.
        try:
            from services.page_cache import drop_page_cache_for_file
            async with get_db() as db:
                rows = await (await db.execute(
                    "SELECT local_path FROM download_files "
                    "WHERE torrent_id=? AND status='completed' AND local_path IS NOT NULL",
                    (torrent_id,),
                )).fetchall()
            dropped = 0
            for row in rows:
                lp = row["local_path"]
                if lp and drop_page_cache_for_file(lp):
                    dropped += 1
            if dropped:
                logger.debug(
                    "Page cache released for %d file(s) of torrent %s", dropped, torrent_id
                )
        except Exception as exc:
            logger.debug("page cache drop skipped: %s", exc)

    async def _engine_pause_torrent(self, torrent_id: int):
        async with self._aria2_state_lock:
            return await self._pause_torrent_locked(torrent_id)

    async def _pause_torrent_locked(self, torrent_id: int):
        if self.download_client_name() != "aria2":
            raise ValueError("Pause is only supported for the aria2 download client")
        # Serialize item control with slot dispatch so a pending child cannot
        # acquire an aria2 GID while its parent is being paused.
        async with self._aria2_dispatch_lock:
            async with get_db() as db:
                rows = await (
                    await db.execute(
                        """SELECT download_id FROM download_files
                           WHERE torrent_id=? AND download_client='aria2'
                             AND blocked=0 AND download_id IS NOT NULL
                             AND status IN ('queued','downloading')""",
                        (torrent_id,),
                    )
                ).fetchall()
            for row in rows:
                await self.aria2().pause(row["download_id"])
            async with get_db() as db:
                await db.execute(
                    """UPDATE download_files
                       SET status='paused', updated_at=CURRENT_TIMESTAMP
                       WHERE torrent_id=? AND download_client='aria2' AND blocked=0
                         AND status IN ('pending','queued','downloading')""",
                    (torrent_id,),
                )
                await db.execute(
                    """UPDATE torrents SET status='paused', updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('queued','downloading')""",
                    (torrent_id,),
                )
                await db.commit()
        await self._log_event(torrent_id, "info", "Paused aria2 transfer queue")
        # Individual pause releases capacity and must advance both pending
        # files and provider-ready parents immediately.
        await self._advance_aria2_queue_locked()

    async def _engine_resume_torrent(self, torrent_id: int):
        async with self._aria2_state_lock:
            return await self._resume_torrent_locked(torrent_id)

    async def _resume_torrent_locked(self, torrent_id: int):
        if self.download_client_name() != "aria2":
            raise ValueError("Resume is only supported for the aria2 download client")
        async with self._aria2_dispatch_lock:
            async with get_db() as db:
                rows = await (
                    await db.execute(
                        """SELECT download_id FROM download_files
                           WHERE torrent_id=? AND download_client='aria2'
                             AND blocked=0 AND download_id IS NOT NULL
                             AND status='paused'""",
                        (torrent_id,),
                    )
                ).fetchall()
            for row in rows:
                await self.aria2().resume(row["download_id"])
            async with get_db() as db:
                await db.execute(
                    """UPDATE download_files
                       SET status=CASE
                             WHEN download_id IS NULL THEN 'pending'
                             ELSE 'queued'
                           END,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE torrent_id=? AND download_client='aria2' AND blocked=0
                         AND status='paused'""",
                    (torrent_id,),
                )
                await db.execute(
                    """UPDATE torrents SET status='queued', updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='paused'""",
                    (torrent_id,),
                )
                await db.commit()
        await self._log_event(torrent_id, "info", "Resumed aria2 transfer queue")
        # Rebalance resumed GIDs and materialize the next ready parent through
        # the same authoritative queue path used by pause and polling.
        await self._advance_aria2_queue_locked()

    async def _engine_pause_all_downloads(self) -> dict:
        """Pause every active DebridPulse-owned aria2 transfer.

        The application-level paused setting prevents new work from being
        dispatched. This method performs the complementary transfer state
        transition so dashboard and download-list actions reflect reality and
        individual downloads can be resumed while global processing remains
        paused.
        """
        if self.download_client_name() != "aria2":
            return {"paused": 0, "failed": 0}
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT DISTINCT t.id
                   FROM torrents t
                   JOIN download_files f ON f.torrent_id=t.id
                   WHERE t.status IN ('queued','downloading')
                     AND f.download_client='aria2' AND f.blocked=0
                     AND f.status IN ('pending','queued','downloading')
                   ORDER BY t.id"""
            )
        paused = 0
        failed = 0
        for row in rows:
            try:
                await self.pause_torrent(row["id"])
                paused += 1
            except Exception as exc:
                failed += 1
                logger.warning(
                    "Pause All could not pause torrent %s: %s",
                    row["id"],
                    sanitize_exception(exc),
                )
        return {"paused": paused, "failed": failed}

    async def _engine_resume_all_downloads(self) -> dict:
        """Resume every paused DebridPulse-owned aria2 transfer."""
        if self.download_client_name() != "aria2":
            return {"resumed": 0, "failed": 0}
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT DISTINCT t.id
                   FROM torrents t
                   JOIN download_files f ON f.torrent_id=t.id
                   WHERE t.status='paused'
                     AND f.download_client='aria2' AND f.blocked=0
                     AND f.status='paused'
                   ORDER BY t.id"""
            )
        resumed = 0
        failed = 0
        for row in rows:
            try:
                await self.resume_torrent(row["id"])
                resumed += 1
            except Exception as exc:
                failed += 1
                logger.warning(
                    "Resume All could not resume torrent %s: %s",
                    row["id"],
                    sanitize_exception(exc),
                )
        return {"resumed": resumed, "failed": failed}

    async def _send_partial_summary(self, torrent_id: int, torrent_name: str, flat_files: List[Dict], blocked_items: List[dict], transferred_items: List[dict], failed_items: List[dict]):
        if not blocked_items:
            return
        total_size = _size_sum([{"size_bytes": int(item.get("size", 0) or 0)} for item in flat_files])
        downloaded_size = _size_sum(transferred_items)
        await self._log_event(torrent_id, "warn", "Filtered files were skipped while the remaining files continued normally")
        if get_settings().discord_webhook_url:
            await self.notify().send_partial(
                name=torrent_name,
                total_files=len(flat_files),
                downloaded_files=len(transferred_items),
                blocked_files=len(blocked_items) + len(failed_items),
                total_size=total_size,
                downloaded_size=downloaded_size,
            )

    # Direct download mode removed — aria2 handles all transfers

    async def _log_file(
        self,
        torrent_id: int,
        filename: str,
        url: str,
        local: Optional[str],
        status: str,
        reason: Optional[str],
        size_bytes: int = 0,
        download_id: Optional[str] = None,
        download_client: str = "aria2",
    ):
        async with get_db() as db:
            await db.execute(
                """INSERT INTO download_files
                   (torrent_id, filename, size_bytes, download_url, local_path, status, download_id, download_client, blocked, block_reason, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (
                    torrent_id,
                    filename,
                    size_bytes,
                    url,
                    local,
                    status,
                    download_id,
                    download_client,
                    1 if status == "blocked" else 0,
                    reason,
                ),
            )
            await db.commit()

    async def _log_event(self, torrent_id: int, level: str, message: str):
        async with get_db() as db:
            await db.execute("INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)", (torrent_id, level, message))
            await db.commit()

    async def _delete_magnet_after_completion(
        self, torrent_id: int, ad_id: str, source: object = None
    ) -> bool:
        """Delete a completed provider object only with positive local ownership."""
        ad_id = str(ad_id or "").strip()
        if not self._provider_delete_authorized(source):
            await self._log_event(
                torrent_id,
                "info",
                "Completed locally; observed AllDebrid object preserved (not owned by this instance)",
            )
            return False
        if not ad_id or ad_id.lower() in ("none", "null"):
            logger.warning(
                "torrent %s: skipping AllDebrid deletion — no alldebrid_id", torrent_id
            )
            await self._log_event(
                torrent_id,
                "warn",
                "Completed locally, but no AllDebrid ID — cannot remove from AllDebrid",
            )
            return False

        logger.info("torrent %s: removing owned AllDebrid object (id=%s)", torrent_id, ad_id)
        deleted = bool(await self.ad().delete_magnet(ad_id))
        msg = (
            "Removed owned object from AllDebrid after completion"
            if deleted
            else f"Completed, but AllDebrid removal failed (id={ad_id})"
        )
        await self._log_event(torrent_id, "info" if deleted else "warn", msg)
        return deleted

    async def _mark_finished(self, torrent_id: int, name: str = ""):
        await self._log_event(torrent_id, "info", "Finished")
        # Push live update to SSE subscribers
        try:
            await publish("torrent_updated", {
                "id": torrent_id,
                "status": "completed",
                "name": name,
            })
            await publish("stats_changed", {})
        except Exception:
            pass


    # ── Disk-space guard ─────────────────────────────────────────────────────

    @staticmethod
    def _get_free_gb(path: str) -> float:
        """
        Return free disk space in GB for the filesystem containing *path*.

        Uses os.statvfs() on POSIX (Linux, macOS, Unraid, NFS, FUSE, ZFS, XFS)
        which queries the mount point directly and works on any filesystem the
        kernel exposes via VFS.  Falls back to shutil.disk_usage() elsewhere
        (Windows).  Never raises — returns -1.0 on any error so callers can
        distinguish "unknown" from "zero".
        """
        try:
            import os
            st = os.statvfs(path)
            # f_bavail = blocks available to unprivileged users (respects reserved blocks)
            return (st.f_bavail * st.f_frsize) / (1024 ** 3)
        except (AttributeError, OSError):
            pass
        try:
            import shutil
            usage = shutil.disk_usage(path)
            return usage.free / (1024 ** 3)
        except Exception:
            return -1.0

    async def check_disk_space_guard(self) -> dict:
        """
        Periodic disk-space guard check.

        Called by disk_guard_loop every disk_guard_interval_seconds (default 60 s).

        When free space < min_free_disk_gb:
          - Allows active aria2 downloads to finish normally
          - Blocks new downloads until space recovers
          - Logs a WARNING once per guard activation

        When free space >= min_free_disk_gb + hysteresis:
          - Allows deferred dispatch to resume
          - Clears the guard state

        Returns a dict with current guard state for the /api/disk-guard endpoint.
        """
        cfg = get_settings()
        min_gb  = float(getattr(cfg, "min_free_disk_gb", 0) or 0)
        hyst_gb = float(getattr(cfg, "disk_guard_resume_hysteresis_gb", 0.5) or 0.5)
        folder  = str(cfg.download_folder or "").strip() or "/download"

        if min_gb <= 0:
            # Guard disabled — clear it before kicking deferred dispatch so the
            # queue path does not immediately no-op on the old guard state.
            if self._disk_guard_active:
                self._disk_guard_active = False
                await self._disk_guard_resume_all()
            return {"enabled": False, "active": False, "free_gb": -1.0, "min_free_gb": 0}

        free_gb = self._get_free_gb(folder)
        if free_gb < 0:
            logger.warning(
                "disk_guard: could not determine free space for %s — guard skipped", folder
            )
            return {"enabled": True, "active": self._disk_guard_active, "free_gb": -1.0,
                    "min_free_gb": min_gb, "error": "stat failed"}

        if not self._disk_guard_active and free_gb < min_gb:
            # Threshold crossed — activate guard.
            # We do NOT pause currently active aria2 downloads: they are already
            # consuming the space, interrupting them creates half-finished files
            # and blocks normal finalization and cleanup.
            # Instead we only block NEW dispatches via _dispatch_pending_aria2_queue.
            # Completion bookkeeping continues for transfers that finish while
            # the guard is active.
            self._disk_guard_active = True
            logger.warning(
                "disk_guard: ACTIVATED — %.2f GB free < %.2f GB required on %s; "
                "new downloads blocked, active downloads will finish normally",
                free_gb, min_gb, folder,
            )

        elif self._disk_guard_active and free_gb >= (min_gb + hyst_gb):
            # Space recovered — deactivate guard
            self._disk_guard_active = False
            logger.info(
                "disk_guard: DEACTIVATED — %.2f GB free >= %.2f GB (threshold + hysteresis); "
                "resuming downloads",
                free_gb, min_gb + hyst_gb,
            )
            await self._disk_guard_resume_all()

        return {
            "enabled": True,
            "active": self._disk_guard_active,
            "free_gb": round(free_gb, 2),
            "min_free_gb": min_gb,
            "folder": folder,
        }

    async def _disk_guard_resume_all(self) -> None:
        """Trigger an immediate dispatch cycle when the disk guard deactivates.

        Active downloads were never paused (they were allowed to finish normally),
        so no aria2 unpause is needed.  We only need to kick the dispatch loop so
        'ready' torrents that were deferred by the guard start without waiting
        for the next sync cycle.
        """
        try:
            await self.sync_download_clients()
        except Exception as exc:
            logger.debug("disk_guard: dispatch after deactivation failed: %s", exc)

    async def _fail_torrent(self, torrent_id: int, message: str, notify: bool = False):
        async with get_db() as db:
            row = await (await db.execute(
                "SELECT name, alldebrid_id, provider_status_code FROM torrents WHERE id=?",
                (torrent_id,),
            )).fetchone()
            logger.warning("fail_torrent: id=%s msg=%r", torrent_id, message[:120])
            await db.execute(
                "UPDATE torrents SET status='error', error_message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (message, torrent_id),
            )
            await db.execute("INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)", (torrent_id, "error", message))
            await db.commit()
        if notify and row:
            await self._notify_provider_error(
                row["name"],
                reason=message,
                context="Torrent marked as failed during processing",
                alldebrid_id=str(row.get("alldebrid_id") or ""),
                status_code=row.get("provider_status_code"),
            )
        # Push live update to SSE subscribers
        try:
            await publish("torrent_updated", {
                "id": torrent_id,
                "status": "error",
                "name": str(row["name"] if row else ""),
            })
            await publish("stats_changed", {})
        except Exception as exc:
            logger.debug(
                "Unable to broadcast missing-provider state for transfer %s: %s",
                torrent_id,
                sanitize_exception(exc, max_length=200),
            )

    async def _handle_expired_reimport(self, row: dict, magnet_link: str) -> None:
        """Re-upload a magnet whose AllDebrid entry expired (statusCode 3).

        Steps:
          1. Upload the magnet to AllDebrid fresh (bypassing duplicate detection
             because the local alldebrid_id was already cleared).
          2. Update the torrent row with the new AllDebrid ID.
          3. Let the normal sync cycle pick it up from 'pending'.

        Guards:
          - Checks that the torrent is still in 'pending' before uploading
            (avoids double-reimport if the loop fires twice).
          - A single reimport attempt; if it fails the torrent stays in error.
        """
        torrent_id = int(row.get("id") or 0)
        name = str(row.get("name") or f"torrent {torrent_id}")
        try:
            # Guard: verify the row is still pending (not already being handled)
            async with get_db() as db:
                current = await db.fetchone(
                    "SELECT status, alldebrid_id FROM torrents WHERE id=?", (torrent_id,)
                )
            if not current or current["status"] != "pending" or current.get("alldebrid_id"):
                logger.debug("expired_reimport: torrent %s no longer pending — skip", torrent_id)
                return

            async with self._upload_sem:
                result = await self.ad().upload_magnet(magnet_link)
            new_ad_id = str(result.get("id", ""))
            new_hash  = str(result.get("hash", row.get("hash", "")) or "").lower()
            if not new_ad_id:
                raise ValueError("AllDebrid returned no ID for re-uploaded magnet")

            async with get_db() as db:
                await db.execute(
                    """UPDATE torrents
                          SET alldebrid_id = ?,
                              hash = COALESCE(NULLIF(?, ''), hash),
                              status = 'uploading',
                              error_message = NULL,
                              provider_status = NULL,
                              provider_status_code = NULL,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?""",
                    (new_ad_id, new_hash, torrent_id),
                )
                await db.commit()
            logger.info(
                "expired_reimport: torrent %s '%s' re-uploaded → new ad_id=%s",
                torrent_id, name[:60], new_ad_id,
            )
        except Exception as exc:
            logger.error(
                "expired_reimport: torrent %s '%s' re-upload failed: %s",
                torrent_id, name[:60], exc,
            )
            async with get_db() as db:
                await db.execute(
                    """UPDATE torrents
                          SET status = 'error',
                              error_message = 'Expired reimport failed: ' || ?,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?""",
                    (str(exc)[:200], torrent_id),
                )
                await db.commit()

    async def _handle_upload_failed(self, row: dict, error_message: str) -> None:
        """Handle AllDebrid statusCode 5 (Upload failed) with automatic re-queue.

        On each occurrence:
          1. Increment upload_retry_count on the torrent row.
          2. If retries remain: delete the failed magnet from AllDebrid, wait
             upload_fail_retry_delay_minutes, then re-upload.
          3. Send a Discord notification for each attempt and on permanent failure.
          4. If all retries exhausted: mark as error and notify permanently.
        """
        cfg = get_settings()
        max_retries = max(0, int(getattr(cfg, "upload_fail_retry_count", 3) or 3))
        delay_minutes = max(0, int(getattr(cfg, "upload_fail_retry_delay_minutes", 5) or 5))

        torrent_id  = int(row["id"])
        name        = str(row["name"] or f"torrent {torrent_id}")
        ad_id       = str(row.get("alldebrid_id") or "")
        magnet_link = str(row.get("magnet") or "")
        source      = str(row.get("source") or "manual")

        # Read current retry counter
        async with get_db() as db:
            r = await (await db.execute(
                "SELECT upload_retry_count FROM torrents WHERE id=?", (torrent_id,)
            )).fetchone()
        current_retry = int((r["upload_retry_count"] if r else None) or 0)
        attempt = current_retry + 1

        logger.warning(
            "Upload failed (code 5) for torrent %s (id=%s) — attempt %s/%s",
            name, torrent_id, attempt, max_retries,
        )

        if attempt > max_retries or not magnet_link:
            # No retries left or no magnet stored → permanent failure
            msg = (
                f"Upload failed permanently after {max_retries} retries"
                if attempt > max_retries
                else "Upload failed: no magnet link stored for re-upload"
            )
            logger.error("Upload failed permanently for torrent %s (source=%s): %s", torrent_id, source, msg)
            await self._log_event(torrent_id, "error",
                f"Upload failed permanently (code 5, {attempt-1} retries exhausted): {error_message}")
            # Clear alldebrid_id so a manual retry can re-upload cleanly
            async with get_db() as db:
                await db.execute(
                    """UPDATE torrents
                       SET alldebrid_id=NULL, upload_retry_count=0,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (torrent_id,),
                )
                await db.commit()
            await self._fail_torrent(torrent_id, msg, notify=False)
            if getattr(cfg, "discord_notify_error", False):
                await self.notify().send_upload_failed_permanent(
                    name,
                    max_attempts=max_retries,
                    reason=error_message,
                    alldebrid_id=ad_id,
                )
            return

        # Increment retry counter and set status back to uploading
        async with get_db() as db:
            await db.execute(
                """UPDATE torrents
                   SET upload_retry_count=?, status='uploading',
                       provider_status='queued', provider_status_code=NULL,
                       error_message=NULL, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (attempt, torrent_id),
            )
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                (torrent_id, "warn",
                 f"Upload failed (code 5) — scheduling retry {attempt}/{max_retries} in {delay_minutes} min"),
            )
            await db.commit()

        # Notify Discord about the re-queue
        if getattr(cfg, "discord_notify_error", False):
            await self.notify().send_requeue(
                name,
                attempt=attempt,
                max_attempts=max_retries,
                reason=error_message,
                alldebrid_id=ad_id,
            )

        # Delete the failed magnet from AllDebrid so we can re-upload
        if ad_id and self._provider_delete_authorized(row.get("source")):
            try:
                await self.ad().delete_magnet(ad_id)
                logger.info("Deleted failed magnet %s from AllDebrid before re-upload", ad_id)
            except Exception as exc:
                logger.debug("Could not delete failed magnet %s: %s", ad_id, exc)

        # Wait before retrying
        if delay_minutes > 0:
            logger.info("Waiting %s min before re-uploading torrent %s", delay_minutes, torrent_id)
            await asyncio.sleep(delay_minutes * 60)

        # Re-upload via magnet
        try:
            async with self._upload_sem:
                result = await self.ad().upload_magnet(magnet_link)
            new_ad_id = str(result.get("id", ""))
            new_name  = result.get("name") or result.get("filename") or name
            logger.info(
                "Re-upload successful for torrent %s: new ad_id=%s", torrent_id, new_ad_id
            )
            async with get_db() as db:
                await db.execute(
                    """UPDATE torrents
                       SET alldebrid_id=?, name=?, status='uploading',
                           provider_status='queued', provider_status_code=NULL,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (new_ad_id, new_name, torrent_id),
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                    (torrent_id, "info",
                     f"Re-upload attempt {attempt}/{max_retries} succeeded (new ad_id={new_ad_id})"),
                )
                await db.commit()
        except Exception as exc:
            logger.error("Re-upload attempt %s failed for torrent %s: %s", attempt, torrent_id, exc)
            async with get_db() as db:
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                    (torrent_id, "error", f"Re-upload attempt {attempt} failed: {exc}"),
                )
                await db.commit()
            # Will be caught on next status poll and retried again


    async def _extract_torrent(self, torrent_id: int, torrent_dict: dict) -> None:
        """Auto-extract archives in the torrent download folder after completion.

        Respects ``extract_enabled``, ``extract_delete_archive``, and
        ``extract_max_concurrent`` from config.  Sends Discord notifications
        if ``discord_notify_extract`` is set.
        """
        cfg = get_settings()
        if not getattr(cfg, "extract_enabled", False):
            return

        name = str(torrent_dict.get("name") or f"torrent {torrent_id}")

        # Auto-extract should use the files the downloader already recorded.
        # Walking the full torrent folder is expensive for large media packs and
        # can keep disks/CPU busy long after the download itself completed.
        try:
            async with get_db() as db:
                rows = await (await db.execute(
                    "SELECT local_path FROM download_files "
                    "WHERE torrent_id=? AND status='completed' AND local_path IS NOT NULL",
                    (torrent_id,),
                )).fetchall()
        except Exception as exc:
            logger.error("Extract: DB lookup failed for torrent %s: %s", torrent_id, exc)
            return

        local_paths = [row["local_path"] for row in rows if row["local_path"]]
        archives = archive_paths_from_downloads(local_paths)
        if not archives:
            logger.debug("Auto-extract: no downloaded archives found for torrent %s", torrent_id)
            return
        folder = archives[0].parent

        # Update extractor concurrency from config
        max_concurrent = max(1, int(getattr(cfg, "extract_max_concurrent", 1) or 1))
        extractor = get_extractor()
        extractor.update_max_concurrent(max_concurrent)

        delete_after = bool(getattr(cfg, "extract_delete_archive", True))

        logger.info("Auto-extract: extracting %s archive(s) for torrent %s", len(archives), torrent_id)
        await self._log_event(torrent_id, "info", f"Auto-extract: extracting {len(archives)} archive(s)")

        results = await extractor.extract_archives(archives, delete_after=delete_after)

        if not results:
            logger.debug("Auto-extract: no existing archive files found for torrent %s", torrent_id)
            return

        ok_list   = [(p, msg) for p, ok, msg in results if ok]
        fail_list = [(p, msg) for p, ok, msg in results if not ok]

        summary_parts = []
        if ok_list:
            summary_parts.append(f"{len(ok_list)} archive(s) extracted")
        if fail_list:
            summary_parts.append(f"{len(fail_list)} failed")
        summary = ", ".join(summary_parts)

        await self._log_event(
            torrent_id,
            "warn" if fail_list else "info",
            f"Auto-extract complete: {summary}",
        )

        if getattr(cfg, "discord_notify_extract", True):
            if ok_list:
                await self.notify().send_extract_complete(
                    name,
                    archive_count=len(ok_list),
                    dest=str(folder),
                )
            for _p, err_msg in fail_list:
                await self.notify().send_extract_failed(name, reason=err_msg)


    async def _set_provider_missing(self, torrent_id: int, message: str):
        """Retain a transfer whose provider object disappeared unexpectedly.

        ``deleted`` is reserved for an explicit user deletion. Provider-side
        removal is a visible terminal error so the transfer remains in the
        Downloads log with its original identity and event history.
        """
        async with get_db() as db:
            await db.execute(
                """UPDATE torrents
                   SET status='error', provider_status='missing',
                       provider_status_code=NULL, error_message=?,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status NOT IN ('completed', 'deleted')""",
                (message, torrent_id),
            )
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                (torrent_id, "error", message),
            )
            await db.commit()

        try:
            await publish("torrent_updated", {
                "id": torrent_id,
                "status": "error",
                "provider_status": "missing",
            })
            await publish("stats_changed", {})
        except Exception:
            pass

    async def import_existing_magnets(
        self, all_magnets: Optional[List[Dict]] = None
    ) -> List[dict]:
        if self.is_paused():
            return []
        if all_magnets is None:
            try:
                all_magnets = await self.ad().get_magnet_status()
            except Exception as exc:
                error = str(exc)
                if any(keyword in error for keyword in ("DISCONTINUED", "discontinued", "deprecated", "migrate")):
                    raise Exception("AllDebrid has disabled 'list all magnets' for your account. Add magnets manually through the DebridPulse UI.")
                raise

        if not all_magnets:
            return []

        # Fetch aria2 state once — used to check per-file progress during import
        aria2_by_gid: Dict[str, "Aria2DownloadStatus"] = {}
        aria2_by_uri: Dict[str, "Aria2DownloadStatus"] = {}
        aria2_by_path: Dict[str, "Aria2DownloadStatus"] = {}
        if self.download_client_name() == "aria2":
            try:
                for dl in await self._aria2_get_all():
                    aria2_by_gid[str(dl.gid)] = dl
                    for fi in dl.files or []:
                        current_path = _normalize_aria2_path(str(fi.get("path", "")))
                        if current_path:
                            aria2_by_path[current_path] = dl
                        for u in fi.get("uris", []) or []:
                            uri = str(u.get("uri", "")).strip()
                            if uri:
                                aria2_by_uri[uri] = dl
            except Exception as exc:
                logger.warning("import_existing_magnets: could not fetch aria2 state: %s", exc)

        # Sort by AllDebrid id ascending so oldest magnets are processed first.
        # AllDebrid assigns monotonically increasing IDs, so lower id = older magnet.
        all_magnets = sorted(all_magnets, key=lambda m: int(m.get("id", 0) or 0))

        results = []
        async with get_db() as db:
            for magnet in all_magnets:
                ad_id = str(magnet.get("id", ""))
                hash_value = magnet.get("hash", ad_id).lower()
                name = magnet.get("filename") or magnet.get("name") or hash_value
                normalized = normalize_provider_state(magnet)
                cur = await db.execute(
                    "SELECT id, status, name, alldebrid_id, provider_status, "
                    "provider_status_code, download_client FROM torrents WHERE hash=?",
                    (hash_value,),
                )
                existing = await cur.fetchone()
                should_queue = True
                if existing:
                    torrent_id = existing["id"]
                    local_status = existing["status"]
                    current_download_client = self.download_client_name()
                    current_provider_code = existing.get("provider_status_code")
                    metadata_changed = (
                        str(existing.get("name") or "") != str(name or "")
                        or str(existing.get("alldebrid_id") or "") != ad_id
                        or str(existing.get("provider_status") or "")
                        != str(normalized["provider_status"] or "")
                        or int(current_provider_code if current_provider_code is not None else -1)
                        != int(normalized["status_code"])
                        or str(existing.get("download_client") or "")
                        != current_download_client
                    )
                    if local_status == "completed":
                        # Successfully completed local content remains terminal.
                        should_queue = False
                    elif local_status == "deleted":
                        # A new AllDebrid magnet with the same hash supersedes the
                        # stale deleted provider record. Reuse the local torrent row
                        # while clearing state belonging to the vanished magnet.
                        await db.execute(
                            "DELETE FROM download_files WHERE torrent_id=?",
                            (torrent_id,),
                        )
                        await db.execute(
                            """UPDATE torrents
                               SET name=?,
                                   alldebrid_id=?,
                                   status=?,
                                   provider_status=?,
                                   provider_status_code=?,
                                   progress=?,
                                   size_bytes=?,
                                   download_client=?,
                                   error_message=NULL,
                                   polling_failures=0,
                                   completed_at=NULL,
                                   download_url=NULL,
                                   local_path=NULL,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE id=?""",
                            (
                                name,
                                ad_id,
                                normalized["local_status"],
                                normalized["provider_status"],
                                normalized["status_code"],
                                float(normalized.get("progress", 0.0) or 0.0),
                                int(normalized.get("size_bytes", 0) or 0),
                                self.download_client_name(),
                                torrent_id,
                            ),
                        )
                        logger.info(
                            "import_existing_magnets: revived deleted torrent %s "
                            "with replacement AllDebrid id %s",
                            torrent_id,
                            ad_id,
                        )
                    elif local_status == "error" and normalized["provider_status"] == "ready":
                        # Previously failed (e.g. due to polling bugs) but AllDebrid says
                        # ready — clear the error and allow re-dispatch
                        await db.execute(
                            """UPDATE torrents
                               SET name=?, alldebrid_id=?, provider_status=?, provider_status_code=?,
                                   download_client=?, status='ready', error_message=NULL,
                                   polling_failures=0, updated_at=CURRENT_TIMESTAMP
                               WHERE id=?""",
                            (name, ad_id, normalized["provider_status"], normalized["status_code"],
                             self.download_client_name(), torrent_id),
                        )
                        logger.info(
                            "import_existing_magnets: torrent %s was error, AllDebrid ready → re-queuing",
                            torrent_id,
                        )
                    elif local_status == "error":
                        # Error + AllDebrid not ready yet — leave it alone
                        should_queue = False
                    elif local_status in ("queued", "downloading", "paused"):
                        # Already actively downloading — do not re-dispatch;
                        # sync_aria2_downloads / _dispatch_pending_aria2_queue handle it.
                        # A metadata no-op must not refresh updated_at because the
                        # stuck-transfer watchdog uses that timestamp as real activity.
                        should_queue = False
                        if metadata_changed:
                            await db.execute(
                                """UPDATE torrents
                                   SET name=?, alldebrid_id=?, provider_status=?, provider_status_code=?, download_client=?, updated_at=CURRENT_TIMESTAMP
                                   WHERE id=?""",
                                (name, ad_id, normalized["provider_status"], normalized["status_code"], current_download_client, torrent_id),
                            )
                    else:
                        # Non-terminal, not actively downloading (uploading/processing/ready/error/pending)
                        # → update metadata only when it actually changed. Stable provider
                        # polling must not keep a stuck transfer artificially fresh.
                        if metadata_changed:
                            await db.execute(
                                """UPDATE torrents
                                   SET name=?, alldebrid_id=?, provider_status=?, provider_status_code=?, download_client=?, updated_at=CURRENT_TIMESTAMP
                                   WHERE id=?""",
                                (name, ad_id, normalized["provider_status"], normalized["status_code"], current_download_client, torrent_id),
                            )
                else:
                    torrent_id = await db.execute_returning_id(
                        """INSERT INTO torrents
                           (hash, name, alldebrid_id, status, source, provider_status, provider_status_code, download_client)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (hash_value, name, ad_id, normalized["local_status"], "alldebrid_existing",
                         normalized["provider_status"], normalized["status_code"], self.download_client_name()),
                    )
                results.append({
                    "hash": hash_value,
                    "name": name,
                    "id": ad_id,
                    "status": normalized["local_status"],
                    "torrent_id": torrent_id,
                    "should_queue": should_queue,
                })
            await db.commit()

        for item in results:
            if item["status"] != "ready" or not item["should_queue"]:
                continue

            torrent_id = item["torrent_id"]
            ad_id = item["id"]

            # When using aria2, check existing download_files against aria2 state
            # before blindly re-queuing everything.
            #
            # External mode uses only the recorded GID. URI/path fallback is
            # reserved for the dedicated built-in daemon because a shared
            # daemon may contain a foreign job with the same target or URL.
            #
            # Note: unlocked AllDebrid links expire after some time, so for a torrent
            # that has never been through _download() (no download_files rows yet) we
            # cannot match against aria2 at all — we simply call _start_download which
            # generates fresh links and lets ensure_download handle deduplication.
            if self.download_client_name() == "aria2":
                async with get_db() as db:
                    file_rows = await (
                        await db.execute(
                            "SELECT id AS file_id, download_url, download_id, local_path, status "
                            "FROM download_files WHERE torrent_id=? AND blocked=0 AND download_client='aria2'",
                            (torrent_id,),
                        )
                    ).fetchall()

                if file_rows:
                    completed = 0
                    needs_reset = False
                    for fr in file_rows:
                        remote_path = ""
                        if fr["local_path"]:
                            try:
                                remote_path = _normalize_aria2_path(self._remote_aria2_path(Path(fr["local_path"])))
                            except Exception:
                                remote_path = _normalize_aria2_path(str(fr["local_path"]))
                        url = str(fr["download_url"] or "").strip()
                        gid = str(fr["download_id"] or "").strip()
                        dl = aria2_by_gid.get(gid) if gid else None
                        if dl is None and is_builtin_mode():
                            dl = aria2_by_path.get(remote_path) if remote_path else None
                            if dl is None:
                                dl = aria2_by_uri.get(url)
                        if dl is None:
                            # Not tracked in aria2 — needs re-queue
                            needs_reset = True
                        elif dl.status == "complete":
                            await self._update_file_state(fr["file_id"], "completed", fr["local_path"])
                            completed += 1
                        elif dl.status == "removed":
                            needs_reset = True
                        elif dl.status == "error":
                            reason = f"{dl.error_code}: {dl.error_message}".strip(": ")
                            await self._update_file_state(fr["file_id"], "error", fr["local_path"], reason=reason)
                            needs_reset = True
                        # active/waiting/paused → already tracked, no action needed

                    if needs_reset:
                        await self._reset_torrent_for_redownload(
                            torrent_id, "Partial/missing aria2 state on import — reset for re-download"
                        )
                        self._schedule_ready_parent_download(
                            torrent_id, ad_id, item["name"]
                        )
                    else:
                        # All files accounted for — let _finalize decide if we're done
                        await self._finalize_aria2_torrent(torrent_id)
                    continue  # handled above

            self._schedule_ready_parent_download(torrent_id, ad_id, item["name"])
        return results

    async def delete_torrent(self, torrent_id: int, delete_from_ad: bool = True):
        async with get_db() as db:
            row = await (await db.execute("SELECT * FROM torrents WHERE id=?", (torrent_id,))).fetchone()
            if not row:
                raise ValueError("Torrent not found")
            file_rows = await (
                await db.execute(
                    "SELECT download_id FROM download_files WHERE torrent_id=? AND download_client='aria2' AND download_id IS NOT NULL",
                    (torrent_id,),
                )
            ).fetchall()
            await db.execute("UPDATE torrents SET status='deleted', updated_at=CURRENT_TIMESTAMP WHERE id=?", (torrent_id,))
            await db.execute(
                "DELETE FROM deferred_provider_submissions WHERE torrent_id=?",
                (torrent_id,),
            )
            await db.commit()

        for file_row in file_rows:
            await self._remove_owned_aria2_gid(file_row["download_id"])

        if delete_from_ad and row["alldebrid_id"] and row["status"] != "completed":
            await self.ad().delete_magnet(row["alldebrid_id"])

    async def test_aria2(self) -> dict:
        if not get_settings().aria2_url:
            raise Exception("aria2 URL not configured")
        test = await self.aria2().test()
        diagnostics = await self._aria2_get_memory_diagnostics()
        return {**test, "diagnostics": diagnostics}

    async def apply_aria2_memory_tuning(self) -> dict:
        cfg = get_settings()
        url, _secret = effective_rpc_config(cfg)
        if not url:
            return {"ok": False, "skipped": True, "reason": "aria2 URL not configured"}
        if not is_builtin_mode(cfg):
            return {
                "ok": True,
                "skipped": True,
                "reason": "external aria2 global policy is read-only",
            }
        options = aria2_global_options(cfg, include_safety=True)
        await self.aria2().change_global_options(options)
        return {"ok": True, "applied": options}

    async def run_aria2_housekeeping(self) -> dict:
        cfg = get_settings()
        if not is_builtin_mode(cfg):
            diagnostics = await self._aria2_get_memory_diagnostics()
            return {
                "ok": True,
                "skipped": True,
                "reason": "external aria2 history is daemon-owned",
                "diagnostics": diagnostics,
            }
        await self.apply_aria2_memory_tuning()
        # Remove all completed/error/removed results from aria2's stopped list.
        # purgeDownloadResult removes everything in the stopped list regardless
        # of max-download-result, keeping aria2's heap clean.
        await self.aria2().purge_download_results()
        # Additionally clean up any stopped GIDs that the DB no longer tracks
        # (e.g. if remove() failed silently in a prior cycle).  These would
        # otherwise grow aria2's stopped list until the next purge.
        try:
            all_dl = await self.aria2().get_all()
            orphan_gids = [
                dl.gid for dl in all_dl
                if dl.status in ("complete", "removed", "error")
            ]
            if orphan_gids:
                for gid in orphan_gids:
                    await self._aria2_svc_remove(gid)
                logger.debug("aria2 housekeeping: cleaned %d stopped/orphan GIDs", len(orphan_gids))
        except Exception as exc:
            logger.debug("aria2 housekeeping orphan cleanup skipped: %s", exc)
        diagnostics = await self._aria2_get_memory_diagnostics()
        return {"ok": True, "diagnostics": diagnostics}

    async def _aria2_svc_remove(self, gid: str) -> None:
        """Best-effort removal of a single GID from aria2's memory."""
        if not is_builtin_mode():
            return
        try:
            await self.aria2().remove(gid)
        except Exception as _e:
            logger.debug("aria2 remove failed for gid (already gone): %s", _e)

    def bind_architecture(self, architecture) -> None:
        """Bind explicit v1.0.5 services once; no runtime method replacement."""
        if self._architecture is not None and self._architecture is not architecture:
            raise RuntimeError("TorrentManager architecture already bound")
        self._architecture = architecture

    async def _aria2_get_all(self):
        if self._architecture is not None:
            return await self._architecture.reconciliation.get_all()
        return await self._engine_aria2_get_all()

    async def _aria2_confirm_gid(self, gid: str):
        if self._architecture is not None:
            return await self._architecture.reconciliation.confirm_gid(gid)
        return await self._engine_aria2_confirm_gid(gid)

    async def _dispatch_pending_aria2_queue(self, *args, **kwargs):
        if self._architecture is not None:
            return await self._architecture.dispatch.dispatch_queue(*args, **kwargs)
        return await self._engine_dispatch_pending_aria2_queue(*args, **kwargs)

    async def _advance_aria2_queue_locked(self, *args, **kwargs):
        if self._architecture is not None:
            return await self._architecture.dispatch.advance_queue_locked(*args, **kwargs)
        return await self._engine_advance_aria2_queue_locked(*args, **kwargs)

    def _schedule_ready_parent_download(self, *args, **kwargs):
        if self._architecture is not None:
            return self._architecture.dispatch.schedule_ready_parent(*args, **kwargs)
        return self._engine_schedule_ready_parent_download(*args, **kwargs)

    async def _start_download(self, *args, **kwargs):
        if self._architecture is not None:
            return await self._architecture.control.start_download(*args, **kwargs)
        return await self._engine_start_download(*args, **kwargs)

    async def _download(self, *args, **kwargs):
        if self._architecture is not None:
            return await self._architecture.control.download(*args, **kwargs)
        return await self._engine_download(*args, **kwargs)

    async def _reset_torrent_for_redownload(self, *args, **kwargs):
        if self._architecture is not None:
            return await self._architecture.control.reset_for_redownload(*args, **kwargs)
        return await self._engine_reset_torrent_for_redownload(*args, **kwargs)

    async def _update_aria2_parent_progress(self, *args, **kwargs):
        if self._architecture is not None:
            return await self._architecture.control.update_parent_progress(*args, **kwargs)
        return await self._engine_update_aria2_parent_progress(*args, **kwargs)

    async def sync_download_clients(self):
        if self._architecture is not None:
            return await self._architecture.reconciliation.reconcile()
        return await self._engine_sync_download_clients()

    async def pause_torrent(self, torrent_id: int):
        if self._architecture is not None:
            return await self._architecture.control.pause_transfer(torrent_id)
        return await self._engine_pause_torrent(torrent_id)

    async def resume_torrent(self, torrent_id: int):
        if self._architecture is not None:
            return await self._architecture.control.resume_transfer(torrent_id)
        return await self._engine_resume_torrent(torrent_id)

    async def pause_all_downloads(self):
        if self._architecture is not None:
            return await self._architecture.control.pause_all()
        return await self._engine_pause_all_downloads()

    async def resume_all_downloads(self):
        if self._architecture is not None:
            return await self._architecture.control.resume_all()
        return await self._engine_resume_all_downloads()

    async def control_aria2_gid(self, *args, **kwargs):
        if self._architecture is not None:
            return await self._architecture.control.control_gid(*args, **kwargs)
        return await self._engine_control_aria2_gid(*args, **kwargs)


manager = TorrentManager()
