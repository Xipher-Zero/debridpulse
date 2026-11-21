"""The installed desktop application's private control channel (WS5).

Each case starts a real DownloadPulse backend the way the desktop supervisor
does -- ``desktop.supervisor.Channel`` for the two anonymous pipes,
``executors.process_ownership.ProcessOwnership.spawn`` for the process -- on
its own disposable data root, and then attacks or uses it:

* through the channel, the four tray operations reach their canonical owners
  while the interface is password-locked, and nothing else is accepted;
* over HTTP, the same backend still refuses an unauthenticated request every
  locked operation, including the ones the tray just performed;
* without a verified channel a desktop backend does not start; outside desktop
  mode no channel is served; the supervisor letting go stops the backend
  through its own shutdown, and the old pipe ends are dead -- also while a
  page still holds the event stream open, under the supervisor's own command
  line (``supervisor.backend_argv``).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import aiohttp
import pytest

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(ROOT))

from application import desktop_control  # noqa: E402
from desktop import supervisor  # noqa: E402
from desktop.supervisor import Channel, free_port  # noqa: E402
from executors.process_ownership import ProcessOwnership  # noqa: E402

PASSWORD = "correct horse battery staple 42"


def _config(path: Path, *, locked: bool, folder: bool = True, extra: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not folder and not locked and not extra:
        return   # a new installation: nobody created a configuration file
    value = {"download_folder": str(path.parent.parent / "downloads")} if folder else {}
    value.update(extra or {})
    if locked:   # the operator's one-shot credential input, installed on the default Admin at start
        value.update(auth_password_enabled=True, auth_username="owner", auth_password=PASSWORD)
    path.write_text(json.dumps(value), encoding="utf-8")


class Backend:
    """One real backend on a disposable root, started as the supervisor starts it."""

    def __init__(self, tmp_path: Path, *, desktop: bool = True, channel: bool = True, locked: bool = True,
                 control: str | None = None, folder: bool = True, supervised: bool = False,
                 env: dict | None = None, config_extra: dict | None = None):
        self.root, self.port = tmp_path, free_port()
        self.config_extra = config_extra
        self.supervised, self.env = supervised, env or {}
        self.processes = ProcessOwnership(tmp_path / "desktop-runtime")
        self.channel = Channel() if channel else None
        self.desktop, self.locked, self.control, self.folder = desktop, locked, control, folder
        self.owned = None

    async def start(self):
        (self.root / "downloads").mkdir(parents=True, exist_ok=True)
        _config(self.root / "config" / "config.json", locked=self.locked, folder=self.folder, extra=self.config_extra)
        ends = self.channel.child_ends() if self.channel else ()
        extra = {"DB_PATH": str(self.root / "data" / "downloadpulse.db"),
                 "CONFIG_PATH": str(self.root / "config" / "config.json"),
                 "STATIC_DIR": str(ROOT / "frontend" / "static")}
        if self.desktop:
            extra["DOWNLOADPULSE_DESKTOP"] = "1"
        if self.control is not None or ends:
            extra["DOWNLOADPULSE_DESKTOP_CONTROL"] = self.control if self.control is not None else f"{ends[0]},{ends[1]}"
        for key in ("SYSTEMROOT", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA"):
            if os.environ.get(key):
                extra[key] = os.environ[key]
        extra.update(self.env)
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        argv = supervisor.backend_argv({"backend": BACKEND}, self.port) if self.supervised else [
            sys.executable, "-m", "uvicorn", "main:app", "--app-dir", str(BACKEND), "--host", "127.0.0.1",
            "--port", str(self.port), "--workers", "1", "--no-access-log"]
        self.owned = await self.processes.spawn("backend", argv, env=self.processes.environment(extra),
                                                pass_fds=tuple(ends))
        if self.channel:
            self.channel.started()
        self.log = asyncio.ensure_future(self._collect())
        return self

    async def _collect(self):
        out = bytearray()
        while chunk := await self.owned.process.stderr.read(65536):
            out.extend(chunk)
        self.stderr = bytes(out)

    async def ready(self, timeout=90) -> bool:
        deadline = time.monotonic() + timeout
        async with aiohttp.ClientSession() as session:
            while time.monotonic() < deadline:
                if self.owned.process.returncode is not None:
                    return False
                try:
                    async with session.get(f"http://127.0.0.1:{self.port}/api/health", timeout=2) as reply:
                        if reply.status == 200:
                            return True
                except (OSError, asyncio.TimeoutError, aiohttp.ClientError):
                    pass
                await asyncio.sleep(0.25)
        return False

    async def http(self, method: str, path: str, **kwargs) -> int:
        return (await self.exchange(method, path, **kwargs))[0]

    async def exchange(self, method: str, path: str, **kwargs) -> tuple[int, object]:
        async with aiohttp.ClientSession() as session:
            async with session.request(method, f"http://127.0.0.1:{self.port}{path}", allow_redirects=False,
                                       **kwargs) as reply:
                try:
                    body = await reply.json(content_type=None)
                except ValueError:
                    body = None
                return reply.status, body

    async def ended(self, timeout=60) -> int | None:
        try:
            return await asyncio.wait_for(self.owned.process.wait(), timeout)
        except TimeoutError:
            return None

    async def stop(self):
        if self.channel:
            self.channel.close()
        if self.owned is not None and self.owned.process.returncode is None:
            if await self.ended(30) is None:
                await self.processes.terminate(self.owned, "backend", grace=5)
        self.processes.forget("backend")


@pytest.mark.asyncio
async def test_the_tray_operations_work_while_the_interface_is_locked_and_http_stays_locked(tmp_path):
    backend = await Backend(tmp_path).start()
    try:
        assert await backend.ready(), backend.owned.process.returncode
        # The interface is locked: HTTP without a credential gets nothing it
        # did not get before -- not the counters, the journal or the controls.
        assert await backend.http("GET", "/api/stats") == 401
        assert await backend.http("GET", "/api/events") == 401
        assert await backend.http("POST", "/api/processing/pause") in {401, 403}
        assert await backend.http("GET", "/api/events/stream") == 401
        assert await backend.http("GET", "/", headers={"Accept": "text/html"}) in {302, 303, 307, 401}
        ask = backend.channel.ask
        status = (await ask("status"))["status"]
        # The tray's counts, and Local Port's state (the supervisor's one notice when it is taken).
        assert set(status) == {"active", "queued", "errors", "completed", "paused", "local_port"} and status["paused"] is False
        first = await ask("events")
        assert first["events"] == [] and isinstance(first["cursor"], int)
        assert (await ask("events", after=first["cursor"], limit=10))["cursor"] >= first["cursor"]
        assert (await ask("pause")) == {"id": 4, "ok": True, "paused": True}
        assert (await ask("status"))["status"]["paused"] is True
        assert (await ask("resume"))["paused"] is False
        # Still locked afterwards: the channel signed nothing in.
        assert await backend.http("GET", "/api/stats") == 401
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_anything_but_the_four_operations_is_refused_and_changes_nothing(tmp_path):
    backend = await Backend(tmp_path).start()
    try:
        assert await backend.ready()
        channel = backend.channel
        forged = [{"id": 1, "op": "exec", "argv": ["sh", "-c", "touch pwned"]},
                  {"id": 2, "op": "settings"}, {"id": 3, "op": "delete", "transfer": 1},
                  {"id": 4, "op": "pause", "role": "admin"}, {"id": 5, "op": "status", "principal": "admin"},
                  {"id": "6", "op": "status"}, {"id": True, "op": "pause"}, {"op": "pause"},
                  {"id": 7, "op": "events", "after": -1}, {"id": 8, "op": "events", "after": 0, "limit": 10_000},
                  {"id": 9, "op": "events", "after": "0 OR 1=1"}, ["pause"], "pause", None, 7,
                  {"id": 10, "op": ["pause"]}, {"id": 11, "op": {"action": "pause"}}, {"id": 12, "op": 1},
                  {"id": 13, "op": None}, {"id": 14, "op": True}, {"id": 15, "op": [["pause"]]}]
        loop = asyncio.get_running_loop()
        for request in forged:
            await loop.run_in_executor(None, channel._out.write, json.dumps(request).encode() + b"\n")
            reply = json.loads(await loop.run_in_executor(None, channel._in.readline))
            assert reply.get("error") == "refused" and set(reply) == {"id", "error"}, (request, reply)
        for raw in (b"not json\n", b"{" + b"x" * 8000 + b"}\n", b"\x00\xff\n"):
            await loop.run_in_executor(None, channel._out.write, raw)
            assert json.loads(await loop.run_in_executor(None, channel._in.readline))["error"] == "refused"
        # Nothing was paused, and the channel still answers its four.
        assert (await channel.ask("status"))["status"]["paused"] is False
        assert not (tmp_path / "pwned").exists() and not (BACKEND / "pwned").exists()
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_letting_go_of_the_channel_stops_the_backend_and_the_old_ends_are_dead(tmp_path):
    backend = await Backend(tmp_path).start()
    assert await backend.ready()
    assert (await backend.channel.ask("status"))["status"]["paused"] is False
    stale_in = backend.channel._in
    backend.channel._out.close()     # what Quit (or the supervisor's death) does
    assert await backend.ended(60) == 0     # its own shutdown: a clean exit
    assert stale_in.readline() == b""        # its answer pipe is gone with it
    backend.processes.forget("backend")


async def _quit_with_an_open_event_stream(backend: Backend) -> tuple[float | None, int | None, bytes]:
    """Quit (the channel let go) while a page holds /api/events/stream open:
    how long the backend took to end, its exit status and what it logged."""
    assert await backend.ready()
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{backend.port}/api/events/stream") as stream:
            assert stream.status == 200
            assert (await stream.content.readline()).startswith(b"event: connected")
            started = time.monotonic()
            backend.channel.close()
            code = await backend.ended(supervisor.GRACEFUL_SECONDS + 30)
            took = time.monotonic() - started if code is not None else None
    await asyncio.sleep(0.2)
    return took, code, getattr(backend, "stderr", b"")


@pytest.mark.asyncio
async def test_quit_is_not_held_by_a_page_that_keeps_the_event_stream_open(tmp_path):
    backend = await Backend(tmp_path, supervised=True, locked=False).start()
    try:
        took, code, log = await _quit_with_an_open_event_stream(backend)
        # The stream is cancelled once the graceful window has passed, and
        # DownloadPulse's own shutdown then runs to the end: a clean exit.
        assert code == 0 and took is not None and took < supervisor.GRACEFUL_SECONDS + 20, (took, code)
        assert b"Shutting down DownloadPulse" in log
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_without_the_graceful_bound_an_open_event_stream_holds_quit(tmp_path):
    """The defect the bound removes: uvicorn waits for the open stream, and
    the backend is still running when the supervisor's own wait would end it
    forcibly."""
    backend = await Backend(tmp_path, locked=False).start()
    try:
        took, code, _log = await _quit_with_an_open_event_stream(backend)
        assert code is None and took is None
    finally:
        await backend.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["", "not,numbers", "999990,999991", "file"])
async def test_a_desktop_backend_without_a_verified_channel_does_not_start(tmp_path, control):
    if control == "file":
        handle = open(tmp_path / "plain", "wb+")
        fd = handle.fileno()
        os.set_inheritable(fd, True)
        if os.name == "nt":
            import msvcrt
            fd = msvcrt.get_osfhandle(fd)
        control = f"{fd},{fd}"
    backend = await Backend(tmp_path, channel=False, control=control).start()
    try:
        assert not await backend.ready(60)
        assert await backend.ended(30) not in (None, 0) or backend.owned.process.returncode not in (None, 0)
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_outside_desktop_mode_no_channel_is_served(tmp_path):
    backend = await Backend(tmp_path, desktop=False, locked=False).start()
    try:
        assert await backend.ready()
        backend.channel._out.write(b'{"id":1,"op":"pause"}\n')
        await asyncio.sleep(2)
        assert await backend.http("GET", "/api/stats") == 200   # open mode, unchanged
        status, stats = await backend.exchange("GET", "/api/stats")
        assert stats["paused"] is False     # the request was never read: nothing paused
    finally:
        await backend.processes.terminate(backend.owned, "backend", grace=5)
        backend.processes.forget("backend")
    # The backend ended without one answer: there was no channel to answer on.
    assert backend.channel._in.readline() == b""
    backend.channel.close()


def test_the_tray_projections_carry_no_message_detail_or_error_text():
    row = {"id": 9, "occurred_at": "2026-11-14T00:00:00Z", "category": "transfer", "type": "transfer.failed",
           "severity": "error", "outcome": "failed", "subject_kind": "transfer", "subject_id": 3, "transfer_id": 3,
           "related_transfer_id": None, "transfer_available": True, "integration_id": "alldebrid",
           "name": "clip.mkv", "message": "Transfer failed: token=abc123 at https://u:p@host/x",
           "detail": "secret detail", "error_category": "auth", "error_domain": "provider"}
    assert desktop_control._project_event(row) == {"id": 9, "type": "transfer.failed", "outcome": "failed",
                                                   "transfer_id": 3, "name": "clip.mkv",
                                                   "occurred_at": "2026-11-14T00:00:00Z"}
    assert set(desktop_control._project_status({"active_operations": 2, "queued_downloads": 1, "error_count": 0,
                                                "completed_count": 5, "paused": False, "version": "x",
                                                "by_status": {"error": 1}})) == {
        "active", "queued", "errors", "completed", "paused"}


def test_the_tray_counts_transfers_at_work_and_extractions_never_paused_ones():
    # As the live tray showed with everything paused: the statistics count two
    # paused transfers among active_operations; the tray's Active is 0.
    paused = {"active_operations": 2, "active_downloads": 2, "extracting_count": 0, "queued_downloads": 0,
              "by_status": {"paused": 2}, "paused": True}
    assert desktop_control._project_status(paused)["active"] == 0
    working = {"active_operations": 5, "extracting_count": 1, "queued_downloads": 2,
               "by_status": {"downloading": 2, "processing": 1, "uploading": 0, "paused": 1, "queued": 2}}
    projected = desktop_control._project_status(working)
    assert (projected["active"], projected["queued"]) == (4, 2)


@pytest.mark.asyncio
async def test_the_desktop_setup_is_standalone_and_goes_on_only_with_a_chosen_folder(tmp_path):
    """No Deployment Type page, no invented ``/download``: the setup cannot
    pass Download Directory, or finish, until a real folder was chosen."""
    root = tmp_path / "desktop"
    backend = await Backend(root, locked=False, folder=False).start()
    try:
        assert await backend.ready()
        status, state = await backend.exchange("GET", "/api/setup/state")
        assert status == 200 and state["desktop"] is True and state["download_folder_chosen"] is False
        assert state["initial"]["complete"] is False
        for phase in ("deployment", "administrator"):
            status, body = await backend.exchange("PUT", "/api/setup/initial/phase", json={"phase": phase})
            assert status == 409 and body["code"] == "setup.phase_unavailable", (phase, body)
        status, body = await backend.exchange("PUT", "/api/setup/initial/phase", json={"phase": "premium_services"})
        assert status == 409 and body["code"] == "setup.download_folder_required"
        status, body = await backend.exchange("POST", "/api/setup/initial/finish")
        assert status == 409 and body["code"] == "setup.download_folder_required"
        assert (await backend.exchange("GET", "/api/setup/state"))[1]["initial"]["complete"] is False
    finally:
        await backend.stop()
    # The folder is chosen (the settings write records it in config.json), and
    # the same installation starts again, its setup still unfinished.
    chosen = await Backend(root, locked=False).start()
    try:
        assert await chosen.ready()
        assert (await chosen.exchange("GET", "/api/setup/state"))[1]["download_folder_chosen"] is True
        assert await chosen.http("PUT", "/api/setup/initial/phase", json={"phase": "premium_services"}) == 200
        assert await chosen.http("POST", "/api/setup/initial/finish") == 200
        assert (await chosen.exchange("GET", "/api/setup/state"))[1]["initial"]["complete"] is True
    finally:
        await chosen.stop()


@pytest.mark.asyncio
@pytest.mark.only_on(not (os.name == "nt" or sys.platform == "darwin"),
                    reason="the user's Downloads folder named by the XDG user-dirs file (Linux)")
async def test_the_desktop_folder_browser_opens_in_the_users_downloads_until_a_folder_is_chosen(tmp_path):
    """Fresh desktop setup: the Download Folder browser opens in the user's own
    Downloads folder (as user-dirs.dirs names it), else their home -- never the
    root above the container's ``/download``; once a folder is chosen it opens
    there, and an explicit path is still taken as asked."""
    home, config = tmp_path / "home", tmp_path / "home" / ".config"
    downloads = home / "Fetched Files"
    downloads.mkdir(parents=True)
    config.mkdir(parents=True)
    (config / "user-dirs.dirs").write_text('# xdg-user-dirs\nXDG_DOWNLOAD_DIR="$HOME/Fetched Files"\n',
                                           encoding="utf-8")
    env = {"HOME": str(home), "XDG_CONFIG_HOME": str(config)}

    async def opened(backend, query="purpose=download"):
        status, body = await backend.exchange("GET", f"/api/settings/directories?{query}")
        assert status == 200, body
        return body["current"]["path"]

    backend = await Backend(tmp_path / "desktop", locked=False, folder=False, env=env).start()
    try:
        assert await backend.ready()
        assert await opened(backend) == str(downloads)
        downloads.chmod(0)                               # there, but not browsable: the home
        try:
            assert await opened(backend) == str(home)
        finally:
            downloads.chmod(0o755)
        downloads.rmdir()                                # missing: the home
        assert await opened(backend) == str(home)
        (home / "elsewhere").mkdir()                     # a path typed or selected is taken as asked
        assert await opened(backend, f"purpose=download&path={home / 'elsewhere'}") == str(home / "elsewhere")
        # Only the starting point: nothing was chosen or saved.
        assert (await backend.exchange("GET", "/api/setup/state"))[1]["download_folder_chosen"] is False
    finally:
        await backend.stop()
    # Relaunched with a chosen folder: it opens there.
    chosen = await Backend(tmp_path / "desktop", locked=False, env=env).start()
    try:
        assert await chosen.ready()
        assert await opened(chosen) == str(tmp_path / "desktop" / "downloads")
    finally:
        await chosen.stop()
    # A server keeps its own default: the nearest browsable ancestor of its folder.
    server = await Backend(tmp_path / "server", desktop=False, locked=False, folder=False, env=env).start()
    try:
        assert await server.ready()
        assert await opened(server) == "/"
    finally:
        await server.stop()


@pytest.mark.parametrize("op", [["pause"], {"action": "pause"}, ["status", "pause"], [], {}, 1, 1.5, True, None,
                                [["resume"]], {"op": "resume"}])
def test_an_operation_that_is_not_a_name_is_refused_without_touching_the_application(op):
    class Untouchable:
        def __getattr__(self, name):
            raise AssertionError(f"the application was used: {name}")

    import asyncio
    reply = asyncio.run(desktop_control.answer(Untouchable(), {"id": 1, "op": op}))
    assert reply == {"id": 1, "error": "refused"}


# ── The desktop starting cover's one server fact ────────────────────────────

def test_the_first_paint_script_says_whether_this_is_the_desktop_application():
    """The server's own answer (core.config.desktop_installation: only the
    supervisor starts a backend with DOWNLOADPULSE_DESKTOP=1) is what the first
    paint is told, and the starting cover acts on nothing else."""
    import subprocess
    source = (BACKEND / "api" / "auth_routes.py").read_text(encoding="utf-8")
    assert "window.downloadPulseDesktop={'true' if desktop_installation() else 'false'};" in source
    probe = "from core.config import desktop_installation; print(desktop_installation())"
    for value, expected in (("1", "True"), ("", "False"), ("true", "False")):
        env = {key: item for key, item in os.environ.items() if key != "DOWNLOADPULSE_DESKTOP"}
        if value:
            env["DOWNLOADPULSE_DESKTOP"] = value
        answer = subprocess.run([sys.executable, "-c", probe], cwd=BACKEND, env=env,
                                capture_output=True, text=True, check=True).stdout.strip()
        assert answer == expected, (value, answer)
    cover = (BACKEND.parent / "frontend" / "static" / "ui-desktop-start.js").read_text(encoding="utf-8")
    assert "if (window.downloadPulseDesktop !== true) {" in cover          # only the server's exact true
    # Its words are drawn once, in their loaded face (never first in a fallback that then resizes).
    assert "document.fonts.load(face)" in cover and "const FACE_WAIT_MS = 1000;" in cover
    css = (BACKEND.parent / "frontend" / "static" / "ui-desktop-start.css").read_text(encoding="utf-8")
    assert "html:not([data-dp-desktop-lettered]) :is(.dp-desktop-start-brand, .dp-desktop-start-text) {\n  visibility: hidden;" in css
    assert "line-height: 46px;" in css and "line-height: 24px;" in css      # the card's size does not depend on the face
    assert "const MIN_SHOWN_MS = 3000;" in cover                           # at least 3 s from the page's start ...
    assert "setTimeout(() => lift('ready'), Math.max(0, MIN_SHOWN_MS - performance.now()));" in cover  # ... never added
    assert "const LIFT_AFTER_MS = 20000;" in cover                         # never a trap
    app = (BACKEND.parent / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert app.count("new CustomEvent('downloadpulse:interface-ready')") == 1   # said once, by one owner


# ── Desktop saves: the file goes where the person's own Save dialog says ────

def test_a_desktop_save_waits_for_its_destination_and_writes_only_where_chosen(tmp_path):
    """The backend asks the supervisor nothing: the supervisor asks for the
    saves waiting (each handed out once), shows its dialog, and answers with
    the path chosen -- the file is written there -- or none (cancelled), when
    nothing is written. A malformed answer is refused and changes nothing."""
    from api.routes import _save_where_chosen

    async def scenario():
        chosen = tmp_path / "chosen-trace.json"
        saving = asyncio.ensure_future(_save_where_chosen("trace.json", lambda path: Path(path).write_bytes(b"{}")))
        await asyncio.sleep(0)
        offered = await desktop_control.answer(None, {"id": 1, "op": "saves"})
        assert [entry["name"] for entry in offered["saves"]] == ["trace.json"]
        number = offered["saves"][0]["save"]
        assert (await desktop_control.answer(None, {"id": 2, "op": "saves"}))["saves"] == []      # handed out once
        for bad in ({"save": number, "path": "relative/name.json"}, {"save": number + 99, "path": str(chosen)},
                    {"save": True, "path": str(chosen)}, {"save": number, "path": str(chosen), "more": 1}):
            assert (await desktop_control.answer(None, {"id": 3, "op": "saved", **bad}))["error"] == "refused"
        assert not saving.done() and not chosen.exists()
        assert (await desktop_control.answer(None, {"id": 4, "op": "saved", "save": number, "path": str(chosen)}))["ok"]
        assert await saving == {"saved": True, "name": "chosen-trace.json"} and chosen.read_bytes() == b"{}"
        # Cancelled: nothing is written anywhere.
        before = set(tmp_path.iterdir())
        cancelled = asyncio.ensure_future(_save_where_chosen("backup.zip", lambda path: Path(path).write_bytes(b"x")))
        await asyncio.sleep(0)
        number = (await desktop_control.answer(None, {"id": 5, "op": "saves"}))["saves"][0]["save"]
        await desktop_control.answer(None, {"id": 6, "op": "saved", "save": number, "path": None})
        assert await cancelled == {"saved": False} and set(tmp_path.iterdir()) == before

    asyncio.run(scenario())


def test_saving_where_chosen_is_the_desktop_applications_alone_and_holds_no_maintenance_back():
    """A server installation refuses the save routes (only the desktop shows a
    Save dialog); and like the downloads they mirror they are no application
    operation -- exactly these two paths, nothing beside them."""
    from api.routes import _require_desktop
    from core.localization import DisplayError
    assert os.environ.get("DOWNLOADPULSE_DESKTOP") != "1"
    with pytest.raises(DisplayError) as refused:
        _require_desktop()
    assert refused.value.code == "desktop.only" and refused.value.status_code == 404
    import main
    exempt = main._EXPORT_SAVE_PATTERN.fullmatch
    assert exempt("/api/torrents/12/trace/save") and exempt("/api/admin/backups/2026-11-16_1/package/save")
    for other in ("/api/torrents/12/trace", "/api/torrents/12/save", "/api/admin/backups/a/b/package/save",
                  "/api/admin/backups/restore", "/api/torrents/x/trace/save"):
        assert not exempt(other), other


# ── A connected browser's Open DownloadPulse ───────────────────────────────

class _RecordingShell:
    """The native shell's two seams the loop uses (its events, its calls), recorded."""

    def __init__(self):
        self.events, self.calls = asyncio.Queue(), []

    async def call(self, method, data=None):
        self.calls.append(method)


class _QuietTray:
    tray_shown = True

    async def tray(self, menu):
        pass

    async def notice(self, title, body):
        pass


@pytest.mark.asyncio
async def test_a_connected_browsers_open_brings_the_window_forward_as_the_trays_open_does(tmp_path):
    from test_browser_pairing import CHROMIUM, Person, extension, pair

    backend = await Backend(tmp_path).start()
    loop_task = None
    try:
        assert await backend.ready()
        backend.base = f"http://127.0.0.1:{backend.port}"
        owner = await Person.sign_in(backend, "owner", PASSWORD)
        token, _ = await pair(backend, owner, "Brave on Linux")
        await owner.close()
        # Nothing asked yet: the channel answers no, and asking does not ask.
        assert (await backend.channel.ask("show"))["show"] is False
        shell = _RecordingShell()
        loop_task = asyncio.ensure_future(supervisor._serve(
            shell, backend.channel, supervisor.Words(tmp_path / "config" / "config.json"), backend.owned, _QuietTray()))
        # The window's own calls (macOS also sets its application menu first, once: not the window's state).
        window = lambda: [call for call in shell.calls if call != "window.setMainMenu"]  # noqa: E731
        await shell.events.put(("windowClose", None))              # closed to the tray
        deadline = time.monotonic() + 10
        while "window.hide" not in window() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert window() == ["window.hide"]
        # Without a browser token the window is not anyone's to call forward.
        assert (await extension(backend, "POST", "/api/browser/v1/application/show", origin=CHROMIUM))[0] == 401
        status, body = await extension(backend, "POST", "/api/browser/v1/application/show", token, origin=CHROMIUM)
        assert status == 200 and body == {"desktop": True}, body
        asked = time.monotonic()
        while window()[-1:] != ["window.focus"] and time.monotonic() - asked < 5:
            await asyncio.sleep(0.05)
        # Shown again and raised -- the tray's Open for a hidden window -- within the supervisor's next look.
        assert window() == ["window.hide", "window.show", "window.focus"]
        assert time.monotonic() - asked < 2
        await asyncio.sleep(supervisor.SHOW_POLL_SECONDS * 3)
        assert window() == ["window.hide", "window.show", "window.focus"]   # asked once, done once
    finally:
        if loop_task is not None:
            loop_task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)
        await backend.stop()
