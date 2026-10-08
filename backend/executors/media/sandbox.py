"""DebridPulse's side of the Media Downloads worker (``executors.media.worker``).

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
  .netless``): the kernel refuses that helper, and anything it starts, every
  network socket, so the guarded worker stays the only network path.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from executors.process_ownership import OwnedProcess, ProcessOwnership
from integrations.media.outcomes import MediaFailure
from services.downloader_egress_guard import downloader_egress_guard

WORKER = Path(__file__).with_name("worker.py")
NETLESS = Path(__file__).with_name("netless.py")
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
        try:
            import yt_dlp  # noqa: F401 -- presence only
        except ImportError:
            absent += ("yt-dlp",)
        return absent


def public_scope(identity: str) -> str:
    """The route scope of one attempt (or one extraction)."""
    import hashlib
    return hashlib.sha256(str(identity).encode("utf-8")).hexdigest()[:32]


class MediaSandbox:
    def __init__(self, runtime_dir: str, *, egress=None, python: str = sys.executable, budget: str = "yt_dlp",
                 tools: MediaTools | None = None):
        self.processes = ProcessOwnership(runtime_dir)
        self.scratch = Path(runtime_dir) / "extract"
        self.egress = egress or downloader_egress_guard
        self.python = python
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
        return self.processes.environment({
            "YTDLP_NO_PLUGINS": "1", "NO_COLOR": "1", "TMPDIR": str(temporary),
            "XDG_CACHE_HOME": str(temporary / ".cache"), "XDG_CONFIG_HOME": str(temporary / ".config"),
            "DENO_DIR": str(temporary / ".deno"), "DENO_NO_UPDATE_CHECK": "1",
        })

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
        (re)written atomically whenever it does not say exactly that."""
        if not self.python or any(char.isspace() for char in self.python):
            raise MediaFailure("runtime_unavailable", "interpreter path cannot start a confined helper")
        directory = self.processes.root / "tools"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        confined = {}
        for name, real in asdict(self.tools).items():
            script = (f"#!{self.python} -I\nimport sys\nsys.path.insert(0, {str(NETLESS.parent)!r})\n"
                      f"import netless\nnetless.run({real!r})\n")
            path = directory / name
            try:
                current = path.read_text(encoding="utf-8")
            except OSError:
                current = None
            if current != script:
                temporary = directory / f".{name}.tmp"
                temporary.write_text(script, encoding="utf-8")
                temporary.chmod(0o700)
                temporary.replace(path)
            confined[name] = str(path)
        return confined

    def _spec(self, mode: str, url: str, proxy: str, guard, **extra) -> bytes:
        missing = self.tools.missing()
        if missing:
            raise MediaFailure("runtime_unavailable", "missing: " + ", ".join(missing))
        return json.dumps({"mode": mode, "url": url, "proxy": proxy, "guard": list(guard),
                           "tools": self.confined_tools(), "socket_timeout": SOCKET_TIMEOUT_SECONDS, **extra},
                          separators=(",", ":")).encode("utf-8")

    async def extract(self, url: str, *, selection: dict, collection_bound: int) -> dict:
        """Read-only facts about ``url`` (``worker.extract``), or the outcome
        that ended the extraction as ``MediaFailure``."""
        scope = uuid.uuid4().hex
        identity = f"extract:{scope}"
        workspace = self.scratch / scope
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        owned = None
        try:
            proxy, guard = await self.route(scope)
            spec = self._spec("extract", url, proxy, guard, selection=selection, collection_bound=collection_bound,
                              workspace=str(workspace))
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
                    result: Path) -> OwnedProcess:
        """Start the attempt's one acquisition worker; its route stays live
        until ``revoke(public_scope(attempt_id))``."""
        scope = public_scope(attempt_id)
        proxy, guard = await self.route(scope)
        try:
            spec = self._spec("acquire", url, proxy, guard, plan=plan, attempt=str(attempt_id),
                              workspace=str(workspace), target=str(target), result=str(result))
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
