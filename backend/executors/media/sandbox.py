"""DownloadPulse's side of the Media Downloads worker (``executors.media.worker``).

Every yt-dlp invocation -- the provider's read-only extraction and the
executor's acquisition alike -- runs here, the same way:

* as one owned process group (``executors.process_ownership``): a minimal
  environment, an empty private home, no shell, no inherited configuration,
  and an instruction delivered on one standard-input pipe, so the attempt's
  route credential never reaches argv, the environment or a file;
* on its own attempt-scoped PUBLIC egress route (``services
  .downloader_egress_guard.RouteScope.PUBLIC``): the guard resolves and judges
  every destination at each connection, never admits a private-LAN address,
  and stops admitting anything once the route is revoked; every byte draws on
  the Media Downloads executor's one download budget;
* with the packaged tools only (``MediaTools``): nothing is downloaded,
  updated or discovered at run time;
* with every native helper the worker may start (ffmpeg, ffprobe, deno)
  reachable only through its confined wrapper (``executors.media
  .netless``): the operating system refuses that helper, and anything it
  starts, every network path, so the guarded worker stays the only one. On
  Windows the wrapper is the packaged ``netless-launcher.exe`` beside the
  wrapper script, and the helpers' AppContainer is granted exactly the tool
  directories and the workspace it works in.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from executors.media import netless
from executors.process_ownership import OwnedProcess, ProcessOwnership
from integrations.media.outcomes import MediaFailure
from services.downloader_egress_guard import downloader_egress_guard

WORKER = Path(__file__).with_name("worker.py")
NETLESS = Path(__file__).with_name("netless.py")
# Windows only: the packaged "#!" stand-in each confined wrapper runs as.
LAUNCHER = "netless-launcher"
_WINDOWS = os.name == "nt"
FAILED_EXIT = 3
_OUTPUT_LIMIT = 8 * 1024 * 1024
_STDERR_LIMIT = 16 * 1024
SOCKET_TIMEOUT_SECONDS = 30
# A collection plans every member (one extraction each), so its extraction
# may take a while; it is still bounded, and a worker that outlives it is
# stopped and reported, never waited for.
EXTRACT_TIMEOUT_SECONDS = 15 * 60


@dataclass(frozen=True)
class MediaTools:
    """The packaged local tools the worker may run. ``deno`` is the sandboxed
    JavaScript runtime yt-dlp's EJS challenge solver uses."""
    ffmpeg: str = ""
    ffprobe: str = ""
    deno: str = ""

    @classmethod
    def installed(cls) -> "MediaTools":
        try:
            from deno import find_deno_bin
            deno = find_deno_bin()
        except (ImportError, FileNotFoundError):
            deno = shutil.which("deno") or ""
        return cls(shutil.which("ffmpeg") or "", shutil.which("ffprobe") or "", deno)

    def missing(self) -> tuple[str, ...]:
        absent = tuple(name for name, path in asdict(self).items() if not path)
        if _WINDOWS and not shutil.which(LAUNCHER):
            absent += (LAUNCHER,)
        try:
            import yt_dlp  # noqa: F401 -- presence only
        except ImportError:
            absent += ("yt-dlp",)
        return absent


def public_scope(identity: str) -> str:
    """The route scope of one attempt (or one extraction)."""
    import hashlib
    return hashlib.sha256(str(identity).encode("utf-8")).hexdigest()[:32]



def console_interpreter(executable: str = sys.executable) -> str:
    """The interpreter the worker and every tool wrapper run on: this one,
    except on Windows when it is the windowless ``pythonw.exe`` (the desktop
    application's) -- then the console ``python.exe`` beside it. A windowless
    interpreter has no console to give a console tool and receives no standard
    handles from the packaged launcher, so the confined tool could neither
    start (it may create no console host) nor report an error. The worker is
    started with no window (``ProcessOwnership``), so its console is hidden and
    every helper below it inherits that one: nothing is ever shown."""
    path = Path(executable)
    if _WINDOWS and path.name.lower() == "pythonw.exe" and (path.parent / "python.exe").is_file():
        return str(path.parent / "python.exe")
    return executable

class MediaSandbox:
    def __init__(self, runtime_dir: str, *, egress=None, python: str | None = None, budget: str = "yt_dlp",
                 tools: MediaTools | None = None, youtube_player_clients: tuple[str, ...] = (), cookies=None):
        self.processes = ProcessOwnership(runtime_dir)
        # A browser's per-job video sign-in (``external.browser_cookies``), READ only: its
        # cookies go to the worker on the instruction pipe, never to a file, argv or the
        # environment, and only when the reader hands them out for this very work.
        self.cookies = cookies
        # The effective YouTube player-client preference every extraction and
        # acquisition this sandbox runs asks yt-dlp for; empty is Automatic.
        self.youtube_player_clients = tuple(youtube_player_clients)
        self.scratch = Path(runtime_dir) / "extract"
        self.egress = egress or downloader_egress_guard
        self.python = python or console_interpreter()
        self.budget = budget
        self._tools = tools

    @property
    def tools(self) -> MediaTools:
        if self._tools is None:
            self._tools = MediaTools.installed()
        return self._tools

    def argv(self) -> list[str]:
        # Isolated mode: no environment-driven import path, no user site
        # directory; no bytecode written anywhere.
        return [self.python, "-I", "-B", str(WORKER)]

    def environment(self, temporary: Path) -> dict[str, str]:
        extra = {
            "YTDLP_NO_PLUGINS": "1", "NO_COLOR": "1", "TMPDIR": str(temporary),
            "XDG_CACHE_HOME": str(temporary / ".cache"), "XDG_CONFIG_HOME": str(temporary / ".config"),
            "DENO_DIR": str(temporary / ".deno"), "DENO_NO_UPDATE_CHECK": "1",
        }
        if _WINDOWS:
            # Windows programs take their temporary directory from these.
            extra.update(TEMP=str(temporary), TMP=str(temporary))
        return self.processes.environment(extra)

    @staticmethod
    def _workspace(path: Path) -> None:
        """Windows: the helpers' AppContainer may change ``path``, the one
        tree a confined helper works in (elsewhere the confinement itself
        names it)."""
        if _WINDOWS:
            try:
                netless.permit(str(path), write=True)
            except OSError as exc:
                raise MediaFailure("runtime_unavailable", str(exc)) from None

    async def route(self, scope: str) -> tuple[str, tuple[str, int]]:
        """The proxy URL (credential included) and the guard listener of one
        public route. The URL is handed to the worker on its input pipe only."""
        await self.egress.ensure_started()
        host, port, user, token = self.egress.public_route(scope, budget=self.budget,
                                                           connect_timeout_seconds=SOCKET_TIMEOUT_SECONDS)
        return f"http://{user}:{token}@{host}:{port}", (host, int(port))

    def revoke(self, scope: str) -> None:
        self.egress.revoke_public_route(scope)

    def confined_tools(self) -> dict[str, str]:
        """``{tool: path}`` of the network-confined wrapper of every packaged
        native tool: the only paths the worker is ever given. Each wrapper is
        this interpreter running ``netless.run(<the real tool>)``; it is
        (re)written atomically whenever it does not say exactly that. On
        Windows, which runs no script, the path is the packaged launcher copied
        beside it as ``<tool>.exe``, and the tool's directory is granted to the
        helpers' AppContainer whenever its wrapper is (re)written."""
        if not self.python or (any(char in self.python for char in '"\r\n') if _WINDOWS
                               else any(char.isspace() for char in self.python)):
            raise MediaFailure("runtime_unavailable", "interpreter path cannot start a confined helper")
        launcher = shutil.which(LAUNCHER) if _WINDOWS else None
        if _WINDOWS and not launcher:
            raise MediaFailure("runtime_unavailable", "missing: " + LAUNCHER)
        interpreter = f'"{self.python}"' if _WINDOWS else self.python
        directory = self.processes.root / "tools"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        confined = {}
        for name, real in asdict(self.tools).items():
            script = (f"#!{interpreter} -I\nimport sys\nsys.path.insert(0, {str(NETLESS.parent)!r})\n"
                      f"import netless\nnetless.run({real!r})\n")
            path = directory / name
            written = self._replace(path, script.encode("utf-8"))
            if _WINDOWS:
                executable = directory / f"{name}.exe"
                if self._replace(executable, Path(launcher).read_bytes()) or written:
                    try:
                        netless.permit(str(Path(real).parent), write=False)
                    except OSError as exc:
                        path.unlink(missing_ok=True)   # granted next time, never assumed
                        raise MediaFailure("runtime_unavailable", str(exc)) from None
                path = executable
            confined[name] = str(path)
        return confined

    @staticmethod
    def _replace(path: Path, content: bytes) -> bool:
        """Make ``path`` hold exactly ``content`` (atomically); whether it had to."""
        try:
            if path.read_bytes() == content:
                return False
        except OSError:
            pass
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_bytes(content)
        temporary.chmod(0o700)
        temporary.replace(path)
        return True

    async def _sign_in(self, attempt_id: str | None = None) -> dict:
        """``{"cookies": [...]}`` when the reader hands this work its cookies -- an
        acquisition's by its attempt, an extraction's by the resolution unit it
        runs in -- else ``{}``: any failure is no cookies."""
        if self.cookies is None:
            return {}
        try:
            cookies = await (self.cookies.current() if attempt_id is None
                             else self.cookies.for_attempt(attempt_id))
        except Exception:
            return {}
        return {"cookies": [cookie.as_instruction() for cookie in cookies]} if cookies else {}

    def _spec(self, mode: str, url: str, proxy: str, guard, **extra) -> bytes:
        missing = self.tools.missing()
        if missing:
            raise MediaFailure("runtime_unavailable", "missing: " + ", ".join(missing))
        return json.dumps({"mode": mode, "url": url, "proxy": proxy, "guard": list(guard),
                           "tools": self.confined_tools(), "socket_timeout": SOCKET_TIMEOUT_SECONDS,
                           "youtube_player_clients": list(self.youtube_player_clients), **extra},
                          separators=(",", ":")).encode("utf-8")

    async def extract(self, url: str, *, selection: dict, collection_bound: int, single_item: bool = False) -> dict:
        """Read-only facts about ``url`` (``worker.extract``) -- only the one
        medium it names when ``single_item`` -- or the outcome that ended the
        extraction as ``MediaFailure``."""
        scope = uuid.uuid4().hex
        identity = f"extract:{scope}"
        workspace = self.scratch / scope
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        owned = None
        try:
            self._workspace(workspace)
            proxy, guard = await self.route(scope)
            spec = self._spec("extract", url, proxy, guard, selection=selection, collection_bound=collection_bound,
                              single_item=single_item, workspace=str(workspace),
                              **await self._sign_in())
            owned = await self.processes.spawn(identity, self.argv(), env=self.environment(workspace), stdin=spec)
            stdout, _stderr = await self._collect(owned, identity, EXTRACT_TIMEOUT_SECONDS)
        finally:
            self.revoke(scope)
            if owned is not None:
                await self.processes.terminate(owned, identity, grace=1.0)
            self.processes.forget(identity)
            shutil.rmtree(workspace, ignore_errors=True)
        event = last_event(stdout)
        if event.get("event") == "result" and isinstance(event.get("facts"), dict):
            return event["facts"]
        if event.get("event") == "failure":
            raise MediaFailure(str(event.get("outcome") or ""), str(event.get("detail") or ""))
        raise MediaFailure("extractor_failed", "the extraction ended without an answer")

    async def _collect(self, owned: OwnedProcess, identity: str, timeout: float) -> tuple[bytes, bytes]:
        stdout, stderr = bytearray(), bytearray()

        async def pump(stream, into, cap):
            while chunk := await stream.read(65536):
                if len(into) < cap:
                    into.extend(chunk[:cap - len(into)])

        pumps = [asyncio.ensure_future(pump(owned.process.stdout, stdout, _OUTPUT_LIMIT)),
                 asyncio.ensure_future(pump(owned.process.stderr, stderr, _STDERR_LIMIT))]
        deadline = time.monotonic() + timeout
        try:
            while owned.process.returncode is None:
                if time.monotonic() >= deadline:
                    await self.processes.terminate(owned, identity, grace=1.0)
                    raise MediaFailure("network", "the extraction did not finish in time")
                try:
                    await asyncio.wait_for(asyncio.shield(owned.process.wait()), timeout=0.2)
                except TimeoutError:
                    pass
            await asyncio.wait_for(asyncio.gather(*pumps, return_exceptions=True), timeout=5)
        finally:
            for task in pumps:
                task.cancel()
        return bytes(stdout), bytes(stderr)

    async def start(self, attempt_id: str, *, url: str, plan: dict, workspace: Path, target: Path,
                    result: Path, concurrent_fragments: int = 1) -> OwnedProcess:
        """Start the attempt's one acquisition worker; its route stays live
        until ``revoke(public_scope(attempt_id))``."""
        scope = public_scope(attempt_id)
        self._workspace(workspace)
        proxy, guard = await self.route(scope)
        try:
            spec = self._spec("acquire", url, proxy, guard, plan=plan, attempt=str(attempt_id),
                              workspace=str(workspace), target=str(target), result=str(result),
                              concurrent_fragment_downloads=int(concurrent_fragments),
                              **await self._sign_in(str(attempt_id)))
            return await self.processes.spawn(attempt_id, self.argv(), env=self.environment(workspace), stdin=spec)
        except BaseException:
            self.revoke(scope)
            raise


def last_event(output: bytes) -> dict:
    """The worker's final JSON event line (its answer), or ``{}``."""
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(value, dict) and value.get("event") in {"result", "failure", "completed"}:
            return value
    return {}
