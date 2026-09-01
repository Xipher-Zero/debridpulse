"""
aria2 JSON-RPC client with robust connection handling.

Improvements over the original:
- Each HTTP request creates its own ClientSession with force_close=True,
  eliminating "Cannot write to closing transport" errors entirely
- Transient connection errors (aria2 restart, brief outages) are logged
  at DEBUG/WARNING instead of ERROR
- Clear error classes: Aria2RPCError (RPC logic) vs Aria2ConnectionError (network)
- Retry logic with backoff for connection errors
- get_all() returns an empty list on connection error instead of raising
- Hot state snapshots use system.multicall so active/waiting/stopped state is
  collected in one HTTP transaction without reintroducing keep-alive failures
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiohttp
from core.config import get_settings
from core.logging_utils import sanitize_log_value

logger = logging.getLogger("alldebrid.aria2")

_CLOSING_TRANSPORT_MSGS = frozenset({
    "Cannot write to closing transport",
    "Connection reset by peer",
    "Connection closed",
    "ServerDisconnectedError",
    "Cannot connect to host",
})


def _is_builtin_mode() -> bool:
    """Return whether DebridPulse exclusively owns the aria2 daemon."""
    return getattr(get_settings(), "aria2_mode", "external") == "builtin"


def _is_transient_connection_error(exc: Exception) -> bool:
    """Returns True if the exception is an expected transient connection error."""
    msg = str(exc)
    return any(m in msg for m in _CLOSING_TRANSPORT_MSGS) or isinstance(
        exc, (aiohttp.ServerDisconnectedError, aiohttp.ClientConnectorError)
    )


class Aria2RPCError(Exception):
    """RPC error from aria2 (e.g. invalid parameters, unknown GID)."""

    def __init__(self, message: str, *, code=None):
        super().__init__(message)
        self.code = code


class Aria2ConnectionError(Aria2RPCError):
    """Connection error to aria2; subclass retained for compatibility."""


@dataclass
class Aria2DownloadStatus:
    gid: str
    status: str
    total_length: int
    completed_length: int
    download_speed: int
    error_code: str = ""
    error_message: str = ""
    files: Optional[List[Dict[str, Any]]] = None


@dataclass
class _UriLockEntry:
    lock: asyncio.Lock
    users: int = 0


def aria2_download_to_dict(download: Aria2DownloadStatus) -> Dict[str, Any]:
    total = int(getattr(download, "total_length", 0) or 0)
    completed = int(getattr(download, "completed_length", 0) or 0)
    progress = round((completed / total) * 100, 2) if total > 0 else 0.0
    files = []
    for file_info in getattr(download, "files", None) or []:
        path = str(file_info.get("path", "") or "")
        length = int(file_info.get("length", 0) or 0)
        completed_length = int(file_info.get("completedLength", 0) or 0)
        selected = str(file_info.get("selected", "true")).lower() != "false"
        uris = [
            str(uri.get("uri", "") or "")
            for uri in file_info.get("uris", []) or []
            if str(uri.get("uri", "") or "").strip()
        ]
        files.append({
            "path": path,
            "name": Path(path).name if path else "",
            "length": length,
            "completed_length": completed_length,
            "progress": round((completed_length / length) * 100, 2) if length > 0 else 0.0,
            "selected": selected,
            "uris": uris,
        })
    first_file = files[0] if files else {}
    name = first_file.get("name") or getattr(download, "gid", "")
    return {
        "gid": getattr(download, "gid", ""),
        "status": getattr(download, "status", ""),
        "name": name,
        "path": first_file.get("path", ""),
        "total_length": total,
        "completed_length": completed,
        "remaining_length": max(total - completed, 0),
        "progress": progress,
        "download_speed": int(getattr(download, "download_speed", 0) or 0),
        "error_code": getattr(download, "error_code", ""),
        "error_message": getattr(download, "error_message", ""),
        "files": files,
        "eta_seconds": (
            int((total - completed) / max(int(getattr(download, "download_speed", 0) or 0), 1))
            if int(getattr(download, "download_speed", 0) or 0) > 0 and total > completed
            else None
        ),
    }


class Aria2Service:
    def __init__(self, url: str, secret: str = "", timeout_seconds: int = 15):
        self.url = url.strip()
        self.secret = secret.strip()
        self.timeout = aiohttp.ClientTimeout(total=max(5, int(timeout_seconds or 15)))
        self._request_id = 0
        self._uri_locks: Dict[str, _UriLockEntry] = {}
        self._rpc_pace_lock = asyncio.Lock()
        self._last_call_time: float = 0.0
        self._rpc_http_requests = 0
        self._rpc_method_calls = 0
        self._rpc_multicall_requests = 0
        self._rpc_total_seconds = 0.0

    async def test(self) -> Dict[str, Any]:
        version = await self._call("aria2.getVersion")
        return {
            "version": version.get("version", "unknown"),
            "enabled_features": version.get("enabledFeatures", []),
        }

    async def get_global_stat(self) -> Dict[str, int]:
        try:
            result = await self._call("aria2.getGlobalStat")
            return {
                "download_speed": int(result.get("downloadSpeed") or 0),
                "upload_speed": int(result.get("uploadSpeed") or 0),
                "active": int(result.get("numActive") or 0),
                "waiting": int(result.get("numWaiting") or 0),
            }
        except Exception:
            return {"download_speed": 0, "upload_speed": 0, "active": 0, "waiting": 0}

    async def get_active(self) -> List[Aria2DownloadStatus]:
        try:
            result = await self._call("aria2.tellActive", [self._keys()])
            return [self._normalize(raw) for raw in (result or [])]
        except Aria2ConnectionError as exc:
            logger.warning("aria2 unreachable (get_active): %s", exc)
            return []
        except Aria2RPCError as exc:
            logger.error("aria2 RPC error (get_active): %s", exc)
            return []

    async def get_global_options(self) -> Dict[str, Any]:
        return await self._call("aria2.getGlobalOption")

    async def change_global_options(self, options: Dict[str, Any]) -> Any:
        if not _is_builtin_mode():
            logger.warning("Blocked aria2.changeGlobalOption for shared external daemon")
            return {"skipped": True, "reason": "external aria2 policy is read-only"}
        return await self._call("aria2.changeGlobalOption", [options])

    async def purge_download_results(self, *, force: bool = False) -> Any:
        """Preserve bounded built-in result state unless an explicit purge is requested."""
        if not _is_builtin_mode():
            logger.warning("Blocked aria2.purgeDownloadResult for shared external daemon")
            return {"skipped": True, "reason": "external aria2 result history is daemon-owned"}
        if not force:
            logger.debug("Preserving bounded built-in aria2 result state")
            return {"skipped": True, "reason": "bounded built-in aria2 result state is operator-visible"}
        return await self._call("aria2.purgeDownloadResult")

    async def get_memory_diagnostics(
        self,
        waiting_limit: int = 100,
        stopped_limit: int = 100,
    ) -> Dict[str, Any]:
        waiting_limit = self._bounded_window(waiting_limit)
        stopped_limit = self._bounded_window(stopped_limit)
        active, waiting, stopped, options = await self._multicall(
            [
                ("aria2.tellActive", [self._keys()]),
                ("aria2.tellWaiting", [0, waiting_limit, self._keys()]),
                ("aria2.tellStopped", [0, stopped_limit, self._keys()]),
                ("aria2.getGlobalOption", []),
            ]
        )
        return {
            "active_count": len(active or []),
            "waiting_count": len(waiting or []),
            "stopped_count": len(stopped or []),
            "query_limits": {"waiting": waiting_limit, "stopped": stopped_limit},
            "global_options": {
                "max-download-result": str((options or {}).get("max-download-result", "")),
                "keep-unfinished-download-result": str((options or {}).get("keep-unfinished-download-result", "")),
            },
        }

    async def get_all(
        self,
        waiting_limit: int = 100,
        stopped_limit: int = 100,
    ) -> List[Aria2DownloadStatus]:
        """Fetch active/waiting/stopped state, normally in one HTTP request."""
        waiting_limit = self._bounded_window(waiting_limit)
        stopped_limit = self._bounded_window(stopped_limit)
        try:
            results = await self._multicall(
                [
                    ("aria2.tellActive", [self._keys()]),
                    ("aria2.tellWaiting", [0, waiting_limit, self._keys()]),
                    ("aria2.tellStopped", [0, stopped_limit, self._keys()]),
                ]
            )
        except Aria2ConnectionError as exc:
            logger.warning("aria2 unreachable (get_all): %s", exc)
            return []
        except Aria2RPCError as exc:
            logger.error("aria2 RPC error (get_all): %s", exc)
            return []

        downloads: List[Aria2DownloadStatus] = []
        for payload in results:
            for raw in payload or []:
                downloads.append(self._normalize(raw))
        return downloads

    async def tell_status(self, gid: str) -> Aria2DownloadStatus:
        result = await self._call("aria2.tellStatus", [gid, self._keys()])
        return self._normalize(result)

    async def ensure_download(
        self,
        uri: str,
        options: Optional[Dict[str, Any]] = None,
        start_paused: bool = False,
        max_retries: int = 5,
        cached_downloads: Optional[List["Aria2DownloadStatus"]] = None,
    ) -> str:
        normalized_uri = uri.strip()
        target_path = self._target_path_from_options(options)
        async with self._uri_lock(normalized_uri):
            if cached_downloads is not None:
                all_downloads = cached_downloads
            elif _is_builtin_mode():
                all_downloads = await self.get_all()
            else:
                all_downloads = []
            matches = self._find_all_matches(normalized_uri, target_path, all_downloads)

            if _is_builtin_mode():
                for dl in matches:
                    if dl.status in {"complete", "removed"}:
                        for dup in matches:
                            if dup.gid != dl.gid and dup.status not in {"complete", "removed"}:
                                logger.warning("Removing stale duplicate aria2 entry %s for queued download", dup.gid)
                                await self.remove(dup.gid)
                        return dl.gid
            else:
                matches = [dl for dl in matches if dl.status in {"active", "waiting", "paused"}]

            if len(matches) > 1:
                for dup in matches[1:]:
                    logger.warning("Removing duplicate aria2 entry %s for queued download", dup.gid)
                    await self.remove(dup.gid)

            if matches:
                existing = matches[0]
                if start_paused and existing.status != "paused":
                    await self.pause(existing.gid)
                return existing.gid

            rpc_options: Dict[str, Any] = dict(options or {})
            if start_paused:
                rpc_options["pause"] = "true"

            def safe_download_error(exc: BaseException) -> str:
                # Strip the exact capability first; generic sanitization is then
                # defense in depth rather than the capability boundary itself.
                raw = str(exc).replace(normalized_uri, "<download-url>")
                return sanitize_log_value(raw, max_length=200)

            last_error: Optional[Exception] = None
            for attempt in range(1, max_retries + 1):
                try:
                    gid = await self._call("aria2.addUri", [[normalized_uri], rpc_options])
                    logger.info("aria2: queued download accepted as GID %s", gid)
                    return gid
                except Aria2ConnectionError as exc:
                    last_error = exc
                    if attempt >= max_retries:
                        break
                    delay = min(attempt * attempt, 10)
                    logger.warning(
                        "aria2 unreachable (attempt %s/%s), retrying in %ss: %s",
                        attempt,
                        max_retries,
                        delay,
                        safe_download_error(exc),
                    )
                    await asyncio.sleep(delay)
                except Aria2RPCError as exc:
                    logger.warning("aria2 rejected download request: %s", safe_download_error(exc))
                    raise Aria2RPCError("aria2 rejected download request") from exc
                except Exception as exc:
                    last_error = exc
                    if attempt >= max_retries:
                        break
                    delay = min(attempt * attempt, 10)
                    logger.warning(
                        "Error queuing download (attempt %s/%s), retrying in %ss: %s",
                        attempt,
                        max_retries,
                        delay,
                        safe_download_error(exc),
                    )
                    await asyncio.sleep(delay)

        error_type = type(last_error).__name__ if last_error is not None else "unknown error"
        raise Aria2RPCError(f"Unable to queue aria2 download after retries ({error_type})")

    def _find_all_matches(
        self,
        uri: str,
        target_path: str,
        all_downloads: List["Aria2DownloadStatus"],
    ) -> List["Aria2DownloadStatus"]:
        uri = uri.strip()
        target_path = self._normalize_path(target_path)
        matched: List[Aria2DownloadStatus] = []
        for download in all_downloads:
            for file_info in download.files or []:
                current_path = self._normalize_path(str(file_info.get("path", "")))
                if target_path and current_path == target_path:
                    matched.append(download)
                    break
                for u in file_info.get("uris", []) or []:
                    if str(u.get("uri", "")).strip() == uri:
                        matched.append(download)
                        break
                else:
                    continue
                break
        matched.sort(key=lambda d: 0 if d.status in {"complete", "removed"} else 1)
        return matched

    async def find_existing_download(self, uri: str) -> Optional["Aria2DownloadStatus"]:
        all_downloads = await self.get_all()
        for dl in self._find_all_matches(uri, "", all_downloads):
            if dl.status not in {"complete", "removed"}:
                return dl
        return None

    @asynccontextmanager
    async def _uri_lock(self, uri: str):
        """Serialize one URI while dropping the high-cardinality key after use."""
        entry = self._uri_locks.get(uri)
        if entry is None:
            entry = _UriLockEntry(lock=asyncio.Lock())
            self._uri_locks[uri] = entry
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users = max(0, entry.users - 1)
            if entry.users == 0 and not entry.lock.locked() and self._uri_locks.get(uri) is entry:
                self._uri_locks.pop(uri, None)

    def _bounded_window(self, value: int) -> int:
        try:
            return max(10, min(1000, int(value or 100)))
        except Exception:
            return 100

    def _target_path_from_options(self, options: Optional[Dict[str, Any]]) -> str:
        if not options:
            return ""
        directory = str(options.get("dir", "") or "").strip()
        out_name = str(options.get("out", "") or "").strip()
        if not directory or not out_name:
            return ""
        return self._normalize_path(str(PurePosixPath(directory) / out_name))

    async def pause(self, gid: str):
        await self._best_effort("aria2.pause", [gid])

    async def unpause(self, gid: str):
        await self._best_effort("aria2.unpause", [gid])

    async def resume(self, gid: str):
        await self._best_effort("aria2.unpause", [gid])

    async def tell_active(self) -> list[dict]:
        try:
            result = await self._call("aria2.tellActive", [["gid", "status"]])
            return result if isinstance(result, list) else []
        except Exception:
            return []

    async def remove(self, gid: str):
        await self._best_effort("aria2.forceRemove", [gid])
        if _is_builtin_mode():
            await self._best_effort("aria2.removeDownloadResult", [gid])

    def rpc_metrics(self) -> Dict[str, Any]:
        requests = int(self._rpc_http_requests)
        return {
            "http_requests": requests,
            "method_calls": int(self._rpc_method_calls),
            "multicall_requests": int(self._rpc_multicall_requests),
            "total_seconds": round(float(self._rpc_total_seconds), 6),
            "average_http_ms": (
                round((self._rpc_total_seconds / requests) * 1000.0, 3)
                if requests else 0.0
            ),
        }

    async def _best_effort(self, method: str, params: List[Any]):
        try:
            await self._call(method, params)
        except Aria2ConnectionError as exc:
            logger.debug("aria2 %s skipped (connection error): %s", method, exc)
        except Exception as exc:
            logger.debug("aria2 %s failed for %s: %s", method, params, exc)

    def _authorized_params(self, params: Optional[Sequence[Any]] = None) -> List[Any]:
        rpc_params = list(params or [])
        if self.secret:
            rpc_params.insert(0, f"token:{self.secret}")
        return rpc_params

    async def _multicall(
        self,
        calls: Sequence[Tuple[str, Sequence[Any]]],
    ) -> List[Any]:
        """Execute several aria2 methods through one system.multicall request.

        If a caller deliberately overrides the `_call` transport hook (tests,
        embedded adapters, or downstream integrations), preserve the historical
        hook contract by executing through that override instead of bypassing it
        with production-only keyword arguments.
        """
        if not calls:
            return []

        current_call = getattr(self, "_call")
        bound_func = getattr(current_call, "__func__", None)
        if bound_func is not Aria2Service._call:
            return list(await asyncio.gather(*[
                current_call(str(method), list(params))
                for method, params in calls
            ]))

        methods = [
            {
                "methodName": str(method),
                "params": self._authorized_params(params),
            }
            for method, params in calls
        ]
        self._rpc_multicall_requests += 1
        result = await self._call(
            "system.multicall",
            [methods],
            inject_token=False,
            method_call_weight=len(methods),
        )
        if not isinstance(result, list) or len(result) != len(methods):
            raise Aria2RPCError("aria2 system.multicall returned an invalid response")

        values: List[Any] = []
        for index, entry in enumerate(result):
            if isinstance(entry, dict) and ("faultCode" in entry or "faultString" in entry):
                method_name = methods[index]["methodName"]
                raise Aria2RPCError(
                    f"aria2 multicall {method_name} failed: "
                    f"{entry.get('faultString', entry.get('faultCode', 'unknown fault'))}"
                )
            if not isinstance(entry, list) or len(entry) != 1:
                raise Aria2RPCError(
                    f"aria2 multicall {methods[index]['methodName']} returned an invalid result wrapper"
                )
            values.append(entry[0])
        return values

    async def _call(
        self,
        method: str,
        params: Optional[List[Any]] = None,
        *,
        inject_token: bool = True,
        method_call_weight: int = 1,
    ) -> Any:
        """Execute one HTTP JSON-RPC transaction with conservative transport handling."""
        async with self._rpc_pace_lock:
            now = time.monotonic()
            gap = now - self._last_call_time
            if gap < 0.02:
                await asyncio.sleep(0.02 - gap)
            self._last_call_time = time.monotonic()

        self._request_id += 1
        rpc_params = list(params or [])
        if inject_token and self.secret:
            rpc_params.insert(0, f"token:{self.secret}")

        payload = {
            "jsonrpc": "2.0",
            "id": str(self._request_id),
            "method": method,
            "params": rpc_params,
        }

        connector = aiohttp.TCPConnector(force_close=True)
        started = time.monotonic()
        self._rpc_http_requests += 1
        self._rpc_method_calls += max(1, int(method_call_weight or 1))
        try:
            async with aiohttp.ClientSession(timeout=self.timeout, connector=connector) as session:
                try:
                    async with session.post(self.url, json=payload) as response:
                        data = await response.json(content_type=None)
                except (
                    aiohttp.ServerDisconnectedError,
                    aiohttp.ClientConnectorError,
                    aiohttp.ClientOSError,
                    ConnectionResetError,
                ) as exc:
                    raise Aria2ConnectionError(f"Connection to aria2 lost: {exc}") from exc
                except aiohttp.ClientError as exc:
                    if _is_transient_connection_error(exc):
                        raise Aria2ConnectionError(f"Transient connection error to aria2: {exc}") from exc
                    raise Aria2RPCError(f"Network error communicating with aria2: {exc}") from exc
        finally:
            self._rpc_total_seconds += max(0.0, time.monotonic() - started)
            await connector.close()

        if "error" in data:
            error = data["error"] or {}
            raise Aria2RPCError(
                f"aria2 [{error.get('code', 'UNKNOWN')}]: {error.get('message', 'Unknown error')}",
                code=error.get("code"),
            )

        return data.get("result")

    def _normalize(self, raw: Dict[str, Any]) -> Aria2DownloadStatus:
        return Aria2DownloadStatus(
            gid=str(raw.get("gid", "")),
            status=str(raw.get("status", "")),
            total_length=int(raw.get("totalLength", 0) or 0),
            completed_length=int(raw.get("completedLength", 0) or 0),
            download_speed=int(raw.get("downloadSpeed", 0) or 0),
            error_code=str(raw.get("errorCode", "") or ""),
            error_message=str(raw.get("errorMessage", "") or ""),
            files=list(raw.get("files") or []),
        )

    @staticmethod
    def _keys() -> List[str]:
        return [
            "gid",
            "status",
            "totalLength",
            "completedLength",
            "downloadSpeed",
            "errorCode",
            "errorMessage",
            "files",
        ]

    @staticmethod
    def _normalize_path(path: str) -> str:
        if not path:
            return ""
        return str(PurePosixPath(path.replace("\\", "/"))).strip()
