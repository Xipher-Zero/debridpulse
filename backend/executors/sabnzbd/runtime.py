"""The internal Usenet acquisition daemon DownloadPulse runs and owns.

The supported deployment bundles this service inside the DownloadPulse
container. It is implementation-private: it binds loopback only, its port is
never published, its web application is never exposed or proxied, and its
control-plane endpoint and API credential are constructed here and never come
from settings. An operator configures news servers, never this service.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import configobj

from db.database import DB_PATH
from core.ports import loopback_port
from executors.process_ownership import output_lines
from core.processes import WINDOWLESS

logger = logging.getLogger("downloadpulse.usenet")

# Loopback only: the service is reachable from the DownloadPulse process and
# from nowhere else. Nothing publishes this port. It is SERVICE_PORT when that
# is free, else one the operating system chooses (core.ports), chosen again at
# every start: another program may already listen on 8090, and a taken port
# must not stop the executor. Its handles carry only the service's job id.
SERVICE_HOST = "127.0.0.1"
SERVICE_PORT = 8090
# The port the running service was started on; None while none runs.
_service_port: int | None = None
# Where the service keeps its own configuration/state: beside DownloadPulse's
# database, as every data-root default (/app/data/usenet in the container).
CONFIG_ROOT = Path(os.environ.get("USENET_CONFIG_DIR", str(DB_PATH.parent / "usenet")))
# The bundled service's program: beside the backend (/app/usenet in the
# container), wherever DownloadPulse is laid out.
SERVICE_ROOT = Path(__file__).resolve().parents[2] / "usenet"
# How long a cold start may take before the runtime reports it unhealthy.
STARTUP_TIMEOUT_SECONDS = 90
_HEALTH_POLL_SECONDS = 0.5
# The service's web engine (CherryPy) answers before it has finished starting,
# and a stop that lands in that window ends it with CherryPy's own exit 70
# before the service's last steps (CherryPy Bus.exit: "exit() was called
# before start() finished"). CherryPy writes this line once it has: the
# service is started when it answers AND has written it. Matched in the
# undecoded bytes, never logged.
_ENGINE_STARTED = b"ENGINE Bus STARTED"

_api_key = ""


def service_url() -> str:
    """The address the service listens at now (its port: chosen at its start)."""
    return f"http://{SERVICE_HOST}:{_service_port or SERVICE_PORT}"


def internal_api_key() -> str:
    """The control-plane credential for the internal service.

    Generated once per installation and persisted beside the service's own
    configuration. It is implementation-private runtime state: it is never an
    operator setting, never part of public settings, provenance, diagnostics
    or review artifacts.
    """
    global _api_key
    if _api_key:
        return _api_key
    stored = _read_ini().get("misc", {}).get("api_key", "")
    _api_key = str(stored or "").strip() or secrets.token_hex(16)
    return _api_key


def _load_ini(path: Path) -> configobj.ConfigObj:
    """The service's configuration file, read the way the service reads it.

    The file belongs to the service: it writes it with configobj (a
    ``__version__`` line above the first section, nested ``[[name]]`` news
    server and category sections), so it is opened with the same library and
    options, never with an INI parser that cannot represent it. A file that
    cannot be parsed yields an empty configuration, and one that is not UTF-8
    still raises, both as before.
    """
    try:
        return configobj.ConfigObj(infile=str(path), default_encoding="utf-8", encoding="utf-8")
    except (OSError, configobj.ConfigObjError):
        return configobj.ConfigObj(default_encoding="utf-8", encoding="utf-8")


def _read_ini() -> dict:
    path = CONFIG_ROOT / "sabnzbd.ini"
    if not path.is_file():
        return {}
    config = _load_ini(path)
    return {section: dict(config[section]) for section in config.sections}


def _write_bootstrap_ini(download_root: str) -> None:
    """Seed the service's configuration so its first start is already correct.

    Only DownloadPulse-owned operational values are written; news servers are
    applied separately through the canonical configuration path. Everything
    else in the file is the service's own and is kept exactly as it saved it.
    """
    from executors.sabnzbd import topology

    CONFIG_ROOT.mkdir(parents=True, exist_ok=True)
    path = CONFIG_ROOT / "sabnzbd.ini"
    config = _load_ini(path)
    if not isinstance(config.get("misc"), configobj.Section):
        if "misc" in config:                  # [misc] is not a section: not the service's file
            config = configobj.ConfigObj(default_encoding="utf-8", encoding="utf-8")
        config["misc"] = {}
    misc = {
        "api_key": internal_api_key(),
        "host": SERVICE_HOST,
        "port": str(_service_port or SERVICE_PORT),
        # No operator ever sees this service, so it needs no web credentials
        # and must never offer a login surface of its own.
        "username": "",
        "password": "",
        "download_dir": topology.incomplete_root(download_root),
        "complete_dir": topology.complete_root(download_root),
        # DownloadPulse owns extraction; the service must never unpack.
        "direct_unpack": "0",
        "enable_filejoin": "0",
        "enable_unrar": "0",
        "enable_7zip": "0",
        # No mode override: everything the service creates (job folders and
        # the files in them) takes the mode the process umask gives it, i.e.
        # the operator's UMASK applied by the entrypoint. SABnzbd 5.1.3 treats
        # an empty value as "apply none" and then never chmods a folder; it
        # only clears execute bits from the files of a job it completes. A
        # previously seeded "0777" is replaced here on the next start;
        # content that already exists keeps the mode it has.
        "permissions": "",
        "auto_browser": "0",
        "check_new_rel": "0",
        # Nor a tray icon of its own: DownloadPulse's desktop tray is the only
        # one (SABnzbd 5.1.3 starts its Windows tray whenever PyWin32 is
        # present and this is on, which is its default).
        "tray_icon": "0",
    }
    config["misc"].update(misc)
    with open(path, "wb") as handle:
        config.write(handle)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


@dataclass(frozen=True)
class UsenetRuntimeConfiguration:
    download_root: str
    operation_timeout_seconds: int = 30
    enabled: bool = False


class UsenetRuntime:
    """Supervises the internal acquisition daemon.

    Mirrors the established managed-daemon shape already used for the direct
    transfer engine: one process, loopback endpoint, start/stop/restart and a
    health probe. Nothing here reads settings.
    """

    def __init__(self) -> None:
        self._config = UsenetRuntimeConfiguration(download_root="/download")
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._started_at = 0.0
        self._last_error = ""
        self._stdout_task = None
        self._stderr_task = None
        # Counts only -- never content. See _drain().
        self._suppressed_output: dict[str, int] = {}
        self._engine_started = asyncio.Event()

    # --- configuration ---------------------------------------------------

    def configure(self, config: UsenetRuntimeConfiguration) -> None:
        self._config = config

    @property
    def enabled(self) -> bool:
        return bool(self._config.enabled)

    # --- process ---------------------------------------------------------

    def _alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @staticmethod
    def _binary() -> str | None:
        for name in ("sabnzbd", "sabnzbdplus", "SABnzbd.py"):
            found = shutil.which(name)
            if found:
                return found
        bundled = SERVICE_ROOT / "SABnzbd.py"
        return str(bundled) if bundled.is_file() else None

    def _command(self) -> list[str]:
        binary = self._binary()
        # The bundled service runs on DownloadPulse's own interpreter: the one
        # Python whose locked packages (sabctools among them) it needs.
        command = ([binary] if not str(binary).endswith(".py")
                   else [sys.executable, str(binary)])
        return [
            *command,
            "--server", f"{SERVICE_HOST}:{_service_port or SERVICE_PORT}",
            "--config-file", str(CONFIG_ROOT / "sabnzbd.ini"),
            "--logging", "1",
            "--browser", "0",
            "--disable-file-log",
        ]

    async def ensure_started(self) -> Dict[str, Any]:
        if not self._config.enabled:
            return await self.status()
        return await self.start()

    async def start(self) -> Dict[str, Any]:
        async with self._lock:
            if self._alive():
                return await self.status()
            if self._binary() is None:
                self._last_error = "the Usenet acquisition service is not installed in this image"
                logger.warning("usenet start skipped: %s", self._last_error)
                return await self.status()
            try:
                # Its usual port when free, else a free one (core.ports), before
                # the configuration and command that name it.
                global _service_port
                _service_port = loopback_port(SERVICE_PORT, name="Usenet service", host=SERVICE_HOST)
                _write_bootstrap_ini(self._config.download_root)
                self._engine_started = asyncio.Event()
                self._process = await asyncio.create_subprocess_exec(
                    *self._command(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **WINDOWLESS,
                )
                self._stdout_task = asyncio.create_task(self._drain(self._process.stdout, "stdout"))
                self._stderr_task = asyncio.create_task(self._drain(self._process.stderr, "stderr"))
                self._started_at = time.time()
                self._last_error = ""
                await self._wait_until_healthy()
                logger.info("usenet acquisition service started on %s", service_url())
            except BaseException as exc:
                self._last_error = str(exc).strip() or exc.__class__.__name__
                await self._cleanup_failed_start()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                logger.warning("usenet start failed: %s", self._last_error)
            return await self.status()

    async def stop(self) -> Dict[str, Any]:
        async with self._lock:
            try:
                if self._process and self._process.returncode is None:
                    if os.name == "nt":
                        # Windows has no SIGTERM: terminate() would be
                        # TerminateProcess, ending the service without letting
                        # it stop its own par2/unrar children. Its own API
                        # shutdown is its graceful stop.
                        await self._request_shutdown()
                    else:
                        self._process.terminate()
                    try:
                        await asyncio.wait_for(self._process.wait(), timeout=15)
                    except asyncio.TimeoutError:
                        self._process.kill()
                self._started_at = 0.0
                await self._cancel_drains()
                if self._process is None or self._process.returncode is not None:
                    global _service_port
                    _service_port = None         # no service: its address is the usual one until a start
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("usenet stop failed: %s", exc)
            return await self.status()

    async def _request_shutdown(self) -> None:
        from executors.sabnzbd.client import SabApiError, SabEndpoint, SabnzbdClient
        client = SabnzbdClient(SabEndpoint(service_url(), internal_api_key(), 10))
        try:
            await client.shutdown()
        except SabApiError as exc:
            logger.warning("usenet shutdown request refused: %s", type(exc).__name__)
        except Exception:  # noqa: BLE001
            # The service may close the connection as it exits, before it has
            # answered: whether it stopped is what the bounded wait observes.
            pass

    async def restart(self) -> Dict[str, Any]:
        await self.stop()
        return await self.start()

    async def status(self) -> Dict[str, Any]:
        """Operational facts only -- never native output, never a credential."""
        exited = (self._process.returncode
                  if self._process is not None and self._process.returncode is not None else None)
        if exited is not None and not self._last_error:
            # The service ended without DownloadPulse asking it to. Its own
            # output is suppressed (see _drain), so the exit code and the
            # suppressed-line counts are the whole diagnostic -- enough to
            # tell "it died" from "it was never started", with none of what
            # it printed crossing the secret boundary.
            self._last_error = f"the acquisition service exited with code {exited}"
        return {
            "running": self._alive(),
            "endpoint": service_url(),
            "started_at": self._started_at,
            "last_error": self._last_error,
            "exit_code": exited,
            "suppressed_output_lines": dict(self._suppressed_output),
        }

    # --- health ----------------------------------------------------------

    async def healthy(self) -> bool:
        from executors.sabnzbd.client import SabEndpoint, SabnzbdClient
        client = SabnzbdClient(SabEndpoint(service_url(), internal_api_key(), 10))
        try:
            return bool(await client.version())
        except Exception:
            return False

    async def _wait_until_healthy(self) -> None:
        deadline = time.time() + STARTUP_TIMEOUT_SECONDS
        while time.time() < deadline:
            if not self._alive():
                raise RuntimeError(self._last_error or "the service exited during startup")
            if self._engine_started.is_set() and await self.healthy():
                return
            await asyncio.sleep(_HEALTH_POLL_SECONDS)
        raise RuntimeError("the service did not become reachable before the startup timeout")

    # --- plumbing --------------------------------------------------------

    async def _drain(self, stream, name: str) -> None:
        """Consume the service's stdout/stderr WITHOUT republishing it.

        The pipes must be read or the child blocks once its buffer fills, but
        the content is the native service's own output: it prints its
        configuration, its API key and news-server details, and none of that
        has been through DownloadPulse's secret boundary. Forwarding it to the
        application log -- even at DEBUG -- would let native output bypass that
        boundary entirely, so the bytes are drained and discarded. Only the
        fact that a stream ended is worth recording.
        """
        suppressed = 0

        def count() -> None:
            # The line's CONTENT is discarded, never logged. Only how many
            # lines were suppressed is kept, so an operator can still tell
            # that the service was talking when it died without any of what
            # it said crossing the secret boundary. An over-long line counts
            # as one too.
            nonlocal suppressed
            suppressed += 1
            self._suppressed_output[name] = suppressed

        try:
            async for line in output_lines(stream, on_overlong=count):
                count()
                if _ENGINE_STARTED in line:
                    self._engine_started.set()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _cancel_drains(self) -> None:
        for task in (self._stdout_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._stdout_task = self._stderr_task = None

    async def _cleanup_failed_start(self) -> None:
        if self._process and self._process.returncode is None:
            self._process.kill()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except Exception:
                pass
        self._process = None
        self._started_at = 0.0
        global _service_port
        _service_port = None                     # no service: the next start chooses again
        await self._cancel_drains()


# The one runtime instance the integration builds against.
runtime = UsenetRuntime()
