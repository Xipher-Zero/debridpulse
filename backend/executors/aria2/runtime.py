from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from executors.aria2.client import Aria2Service

logger = logging.getLogger("alldebrid.aria2")

# The daemon DebridPulse runs listens on loopback only. Its RPC endpoint and
# secret are constructed here and never come from settings.
RPC_PORT = 6800
RPC_SECRET = "debridpulse-internal-aria2-rpc"


def _canonical_aria2_options(cfg):
    """Decode the executor-owned ``integrations.aria2`` namespace of ``cfg``.

    This is the settings-boundary accessor for callers that already hold an
    ``AppSettings`` (``api/routes.py``). ``Aria2Runtime`` and
    ``Aria2Administration`` never call it: they consume the already-injected
    ``Aria2RuntimeConfiguration``. ``integrations.aria2`` is the only
    authority -- there is no flat-field fallback."""
    from executors.aria2.definition import Aria2Options

    entry = (getattr(cfg, "integrations", None) or {}).get("aria2")
    options = getattr(entry, "options", None) or {}
    return Aria2Options(**options)


def rpc_url() -> str:
    return f"http://127.0.0.1:{RPC_PORT}/jsonrpc"


def rpc_service(options) -> Aria2Service:
    """The RPC client for the daemon DebridPulse runs; the one construction site."""
    return Aria2Service(rpc_url(), RPC_SECRET, options.operation_timeout_seconds)


# Executor-local native queue width. DebridPulse's own global admission is the
# only concurrency policy; this value merely never undercuts any supported
# global setting, and nothing synchronizes it to that setting.
NATIVE_ACTIVE_DOWNLOADS = 64


def build_aria2_global_options(options, *, include_safety: bool = False) -> Dict[str, str]:
    """Pure translation of aria2-owned typed tuning into the native aria2
    global-option dict -- no settings access of any kind, and no DebridPulse
    global policy (concurrency or bandwidth) mirrored into native options. It
    is the single tuning mapping owner used by ``Aria2Runtime`` and
    ``Aria2Administration``. The download ceiling is enforced only through the
    executor's assigned-ceiling operation."""
    options_dict: Dict[str, str] = {
        "max-download-result": str(int(options.max_download_result or 50)),
        "keep-unfinished-download-result": "true" if bool(options.keep_unfinished_download_result) else "false",
        "max-concurrent-downloads": str(NATIVE_ACTIVE_DOWNLOADS),
        "split": str(int(options.split or 8)),
        "min-split-size": str(options.min_split_size or "10M"),
        "max-connection-per-server": str(int(options.max_connection_per_server or 8)),
        # disk-cache=0 per aria2 docs means ~4 MiB for HTTP.
        # On FUSE-based mounts (mergerfs, NFS) a small cache (e.g. 16M) reduces
        # FUSE round-trips and can actually lower peak RSS compared to 0.
        # Users can override this in Settings → Download → aria2 disk-cache.
        "disk-cache": str(options.disk_cache or "0"),
        "file-allocation": str(options.file_allocation or "falloc"),
        "continue": "true" if bool(options.continue_downloads) else "false",
        "lowest-speed-limit": str(options.lowest_speed_limit or "0"),
        "max-overall-upload-limit":   str(int(options.max_upload_limit or 0)),
    }
    if include_safety:
        options_dict.update({
            "follow-torrent": "false",
            "follow-metalink": "false",
            "enable-dht": "false",
            "enable-dht6": "false",
            "enable-peer-exchange": "false",
            "bt-enable-lpd": "false",
        })
    return options_dict


def _default_aria2_options():
    from executors.aria2.definition import Aria2Options
    return Aria2Options()


@dataclass(frozen=True)
class Aria2RuntimeConfiguration:
    """aria2-owned typed configuration: its ``integrations.aria2`` options and
    the download root. Built by the aria2 integration factory on every
    settings change; the runtime never consults global application settings
    and carries no DebridPulse global policy."""
    options: Any = field(default_factory=_default_aria2_options)
    download_root: str = "/download"


class Aria2Runtime:
    def __init__(self) -> None:
        self._process: Optional[asyncio.subprocess.Process] = None
        self._started_at: float = 0.0
        self._last_error: str = ""
        self._last_output: deque[str] = deque(maxlen=30)
        self._stdout_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._config = Aria2RuntimeConfiguration()
        # The aggregate download ceiling core last assigned to this executor
        # (0 = unlimited). Held here so a daemon (re)start enforces it too.
        self._bandwidth_ceiling = 0
        # Process-lifetime (never persisted) exact values to redact from this
        # daemon's diagnostics per DP attempt: endpoint addresses and header
        # values of owned jobs. Bounded; lost on restart, when the generic
        # URL/credential sanitizer and the job's own reported URIs remain.
        self.redactions: OrderedDict[str, tuple[str, ...]] = OrderedDict()

    def configure(self, config: Aria2RuntimeConfiguration) -> None:
        """Injection point: the aria2 integration factory calls this on every
        settings change so this long-lived singleton always acts on current
        aria2 configuration without ever reading global application settings."""
        self._config = config

    def assign_bandwidth_ceiling(self, bytes_per_second: int) -> None:
        self._bandwidth_ceiling = max(0, int(bytes_per_second))

    @property
    def options(self):
        return self._config.options

    def _service(self) -> Aria2Service:
        return rpc_service(self._config.options)

    def _is_process_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def _runtime_paths(self) -> tuple[Path, Path]:
        aria2 = self._config.options
        log_file = Path(aria2.log_file or "/app/data/aria2/aria2.log")
        session_file = Path(aria2.session_file or "/app/data/aria2/aria2.session")
        log_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.touch(exist_ok=True)
        return log_file, session_file

    def _log_rotation_settings(self) -> tuple[int, int]:
        aria2 = self._config.options
        max_mb = int(aria2.log_max_mb or 25)
        backups = int(aria2.log_backups or 0)
        return max(1, max_mb) * 1024 * 1024, max(0, backups)

    def _rotate_log_file(self) -> bool:
        log_file, _ = self._runtime_paths()
        max_bytes, backups = self._log_rotation_settings()
        try:
            if not log_file.exists() or log_file.stat().st_size <= max_bytes:
                return False
            if backups <= 0:
                log_file.write_text("", encoding="utf-8")
                logger.info("aria2 log truncated after reaching rotation limit")
                return True
            for index in range(backups, 0, -1):
                src = log_file.with_name(f"{log_file.name}.{index}")
                dst = log_file.with_name(f"{log_file.name}.{index + 1}")
                if index == backups and src.exists():
                    src.unlink(missing_ok=True)
                elif src.exists():
                    src.replace(dst)
            log_file.replace(log_file.with_name(f"{log_file.name}.1"))
            log_file.touch(exist_ok=True)
            logger.info("aria2 log rotated after reaching rotation limit")
            return True
        except Exception as exc:
            logger.warning("aria2 log rotation failed: %s", exc)
            return False

    def _download_dir(self) -> Path:
        # aria2 runs in the same container as the app and writes into the
        # application download folder (storage root, injected -- specification
        # section 4.2).
        return Path(self._config.download_root or "/download")

    def _command(self) -> list[str]:
        aria2 = self._config.options
        log_file, session_file = self._runtime_paths()
        download_dir = self._download_dir()
        download_dir.mkdir(parents=True, exist_ok=True)
        options = build_aria2_global_options(aria2, include_safety=True)
        options["max-overall-download-limit"] = str(self._bandwidth_ceiling)
        cmd = [
            "aria2c",
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={RPC_PORT}",
            f"--rpc-secret={RPC_SECRET}",
            "--rpc-allow-origin-all=false",
            f"--dir={download_dir}",
            f"--save-session={session_file}",
            "--save-session-interval=30",
            "--auto-save-interval=30",
            f"--log={log_file}",
            "--log-level=notice",
            "--summary-interval=0",
            "--disable-ipv6=true",
            # Disable async DNS resolver threads — they create extra glibc malloc
            # arenas which retain freed memory and cause RSS to grow over time.
            "--async-dns=false",
            # No netrc lookups — we never use FTP credentials
            "--no-netrc=true",
        ]
        # Do not load the session file on startup.
        # The DB is the single source of truth for pending downloads.
        # _dispatch_pending_aria2_queue re-queues them within seconds.
        # Loading a stale session (potentially with hundreds of old entries)
        # causes a RAM spike and re-adds GIDs that are already completed/removed.
        if session_file.exists() and session_file.stat().st_size > 0:
            try:
                session_file.write_text("")  # clear so aria2 starts clean
                logger.debug("Cleared aria2 session file on startup (DB is source of truth)")
            except Exception as _e:
                logger.debug("Session file clear failed (non-critical): %s", _e)
        cmd.extend(f"--{key}={value}" for key, value in options.items())
        return cmd

    async def ensure_started(self) -> Dict[str, Any]:
        if not self._config.options.auto_start:
            return await self.status()
        return await self.start()

    async def start(self) -> Dict[str, Any]:
        async with self._lock:
            if self._is_process_alive():
                return await self.status()
            if not shutil.which("aria2c"):
                self._last_error = "aria2c binary not found in container"
                logger.warning("aria2 start skipped: %s", self._last_error)
                return await self.status()
            try:
                self._rotate_log_file()
                import os as _os
                # MALLOC_ARENA_MAX=1 prevents glibc from creating multiple
                # memory arenas for different threads, which causes RSS to grow
                # even after allocations are freed (glibc never returns arenas
                # to the OS). With =1 there is one arena, trim() works globally.
                # MALLOC_TRIM_THRESHOLD_=65536 makes glibc trim the heap more
                # aggressively (default is 128KB; we use 64KB).
                env = dict(_os.environ)
                env["MALLOC_ARENA_MAX"] = "1"
                env["MALLOC_TRIM_THRESHOLD_"] = "65536"
                self._process = await asyncio.create_subprocess_exec(
                    *self._command(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                self._stdout_task = asyncio.create_task(self._drain_stream(self._process.stdout, "stdout"))
                self._stderr_task = asyncio.create_task(self._drain_stream(self._process.stderr, "stderr"))
                self._started_at = time.time()
                self._last_error = ""
                await self._wait_until_healthy()
                logger.info("aria2 started on %s", rpc_url())
            except BaseException as exc:
                self._last_error = str(exc).strip() or exc.__class__.__name__
                await self._cleanup_failed_start()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                logger.warning("aria2 start failed: %s", exc)
            return await self.status()

    async def stop(self) -> Dict[str, Any]:
        async with self._lock:
            try:
                try:
                    await self._service()._call("aria2.shutdown")
                except Exception as _e:
                    logger.debug("aria2 shutdown RPC failed (process will be killed): %s", _e)
                if self._process and self._process.returncode is None:
                    try:
                        await asyncio.wait_for(self._process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        self._process.terminate()
                        try:
                            await asyncio.wait_for(self._process.wait(), timeout=5)
                        except asyncio.TimeoutError:
                            self._process.kill()
                self._started_at = 0.0
                await self._cancel_drain_tasks()
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("aria2 stop failed: %s", exc)
            return await self.status()

    async def restart(self) -> Dict[str, Any]:
        await self.stop()
        return await self.start()

    async def ensure_log_rotation(self) -> Dict[str, Any]:
        log_file, _ = self._runtime_paths()
        max_bytes, _ = self._log_rotation_settings()
        try:
            size = log_file.stat().st_size if log_file.exists() else 0
        except Exception:
            size = 0
        if size <= max_bytes:
            return {"ok": True, "rotated": False, "size_bytes": size}
        if self._is_process_alive():
            # aria2 keeps the log file handle open. Restarting after rotation is
            # the reliable way to make it write into the fresh log file.
            await self.restart()
            return {"ok": True, "rotated": True, "restarted": True, "size_bytes": size}
        rotated = self._rotate_log_file()
        return {"ok": True, "rotated": rotated, "restarted": False, "size_bytes": size}

    async def apply_options(self) -> Dict[str, Any]:
        svc = self._service()
        options = build_aria2_global_options(self._config.options, include_safety=True)
        await svc.change_global_options(options)
        return {"ok": True, "options": options}

    async def status(self) -> Dict[str, Any]:
        aria2 = self._config.options
        process_running = self._is_process_alive()
        rpc_ok = False
        version = ""
        rpc_error = ""
        try:
            result = await self._service().test()
            rpc_ok = True
            version = result.get("version", "")
        except Exception as exc:
            rpc_error = str(exc)
        return {
            "auto_start": bool(aria2.auto_start),
            "running": bool(process_running or rpc_ok),
            "process_running": process_running,
            "rpc_ok": rpc_ok,
            "download_dir": str(self._download_dir()),
            "version": version,
            "uptime_seconds": int(time.time() - self._started_at) if self._started_at else 0,
            "last_error": self._last_error or rpc_error,
            "last_output": "\n".join(self._last_output),
            "safety": build_aria2_global_options(aria2, include_safety=True),
        }

    async def _wait_until_healthy(self) -> None:
        deadline = time.time() + 10
        last_error = ""
        while time.time() < deadline:
            if self._process and self._process.returncode is not None:
                raise RuntimeError(self._startup_error("aria2 process exited before RPC became healthy"))
            try:
                await self._service().test()
                await self.apply_options()
                return
            except Exception as exc:
                last_error = str(exc)
                await asyncio.sleep(0.25)
        raise RuntimeError(self._startup_error(last_error or "aria2 RPC did not become healthy"))

    async def _drain_stream(self, stream, name: str) -> None:
        if not stream:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self._last_output.append(f"{name}: {text}")

    async def _cancel_drain_tasks(self) -> None:
        tasks = [task for task in (self._stdout_task, self._stderr_task) if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._stdout_task = None
        self._stderr_task = None

    async def _cleanup_failed_start(self) -> None:
        """Roll back every resource allocated by an unsuccessful start attempt."""
        process = self._process
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        await self._cancel_drain_tasks()
        self._process = None
        self._started_at = 0.0

    def _startup_error(self, message: str) -> str:
        log_file, _ = self._runtime_paths()
        details = [message]
        if self._process and self._process.returncode is not None:
            details.append(f"exit code {self._process.returncode}")
        if self._last_output:
            details.append("process output: " + " | ".join(self._last_output))
        try:
            if log_file.exists():
                tail = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-10:]
                if tail:
                    details.append("log tail: " + " | ".join(tail))
        except Exception as _e:
            logger.debug("aria2 log tail failed: %s", _e)
        return "; ".join(details)


runtime = Aria2Runtime()
