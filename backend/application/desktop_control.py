"""The installed desktop application's private control channel.

The native desktop supervisor (``desktop/supervisor.py``) starts this backend
as its own child and hands it the read end of one anonymous pipe and the write
end of another, inherited by the child alone: ``DOWNLOADPULSE_DESKTOP_CONTROL``
names them (descriptors on POSIX, handles on Windows; the numbers are no
secret -- only a process that holds the other ends can speak). There is no
socket, port, URL or file: nothing a page, another account or another process
can connect to. The authority is the supervisor's ownership of this process
and of those pipe ends, never a DownloadPulse account -- HTTP authentication,
roles and permissions are untouched, and nothing here signs a session in.

The tray asks four things, each answered by its existing owner:

* ``status`` -- the ``/api/stats`` counters (``api.routes.get_stats``),
  projected to five numbers;
* ``events`` -- the activity journal (``db.event_journal.page``) after a
  cursor, transfer lifecycle events only (a resume: the outcome
  ``resumed``), projected to identifiers, type, outcome and the transfer's
  name (no message, detail or error text);
* ``pause`` / ``resume`` -- ``ApplicationService.pause_all`` / ``resume_all``;
* ``saves`` -- the files waiting for the person to say where they go (a
  desktop save: ``request_destination``), each handed out once, by an opaque
  number and the name the file is offered under;
* ``saved`` -- where one of them goes: the path the person's own Save dialog
  chose, or none (cancelled);
* ``show`` -- whether the window was asked to come forward since the
  supervisor last asked (``request_show``: a connected browser's Open
  DownloadPulse), answered once.

The two save operations are how a desktop save reaches the Save dialog only
the supervisor can show (a page and this backend have no window of their
own): the backend asks nothing of the supervisor -- the supervisor asks, on
its own loop, whether any save is waiting.

One JSON object per line in, ``{"id": <int>, "op": <name>, ...}``; one line
answers it, echoing ``id``. Anything else -- another operation, a malformed or
oversized line, an unexpected field -- is refused and changes nothing.

The end of the supervisor's pipe (an explicit Quit, or the supervisor gone)
stops this backend through its own shutdown: the interrupt uvicorn already
handles, so DownloadPulse's lifespan stops every owned executor as it always
does. A desktop backend without a verified channel does not start.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import stat
import threading

logger = logging.getLogger("downloadpulse.desktop")

ENVIRONMENT = "DOWNLOADPULSE_DESKTOP_CONTROL"
MAX_LINE = 16384                                   # a saved destination (MAX_PATH) and its JSON fit
MAX_EVENTS = 100
# The journal's transfer lifecycle: what the tray's notices are made of.
LIFECYCLE = frozenset({"transfer.accepted", "transfer.queued", "transfer.transferring", "transfer.paused",
                       "transfer.completed", "transfer.failed", "transfer.cancelled"})
_FIELDS = {"status": {"id", "op"}, "events": {"id", "op", "after", "limit"},
           "pause": {"id", "op"}, "resume": {"id", "op"},
           "saves": {"id", "op"}, "saved": {"id", "op", "save", "path"}, "frame": {"id", "op"},
           "locale": {"id", "op"}, "show": {"id", "op"}}
# How long a desktop save waits for its destination: the supervisor's next
# look (its loop) and the person's own time in the dialog.
DESTINATION_WAIT_SECONDS = 600
MAX_PATH = 4096


# The window frame in the page's theme (``set_frame``, from PUT /desktop/frame):
# {"caption": "#rrggbb", "text": "#rrggbb", "dark": bool}, or None until the
# page has said. The supervisor reads it (op ``frame``) and colours the window's
# own frame with it (Windows: its title bar; macOS: the title bar's background).
_frame: dict | None = None
_HEX = re.compile(r"#[0-9a-f]{6}")


def set_frame(caption: str, text: str, dark: bool) -> dict:
    """Record the frame the page's theme asks for; a colour that is not
    ``#rrggbb`` is refused (``ValueError``)."""
    global _frame
    caption, text = str(caption).lower(), str(text).lower()
    if not (_HEX.fullmatch(caption) and _HEX.fullmatch(text)) or not isinstance(dark, bool):
        raise ValueError   # the route answers its own coded refusal (desktop.frame_invalid)
    _frame = {"caption": caption, "text": text, "dark": dark}
    return dict(_frame)


# The desktop saves waiting for their destination: number -> [offered name,
# future, handed out]. Kept and resolved on the application's own loop.
_saves: dict[int, list] = {}
_next_save = 0


async def request_destination(name: str) -> str | None:
    """Where the person wants ``name`` saved, as their own Save dialog answers
    (the supervisor shows it when it next asks for ``saves``); None when they
    cancelled, or nothing answered in DESTINATION_WAIT_SECONDS."""
    global _next_save
    _next_save += 1
    number = _next_save
    future = asyncio.get_running_loop().create_future()
    _saves[number] = [name, future, False]
    try:
        return await asyncio.wait_for(future, DESTINATION_WAIT_SECONDS)
    except TimeoutError:
        logger.warning("desktop save %s: no destination within %ss", number, DESTINATION_WAIT_SECONDS)
        return None
    finally:
        _saves.pop(number, None)


# A connected browser asked for the application (its Open DownloadPulse): the
# supervisor brings its window forward, as the tray's Open does, when it next
# asks (op ``show``). Asked twice before it looks is asked once.
_show_requested = False


def request_show() -> None:
    """Ask the supervisor to bring the window forward on its next look."""
    global _show_requested
    _show_requested = True


class ChannelRefused(RuntimeError):
    """The desktop backend's control channel is absent or not two private pipes."""


def _pipe(value: str, mode: str):
    """One inherited pipe end, verified to be a pipe and kept from every later child."""
    number = int(value)
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetFileType.argtypes = [wintypes.HANDLE]
        kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
        if kernel32.GetFileType(number) != 3:   # FILE_TYPE_PIPE
            raise ChannelRefused("the desktop control handle is not a pipe")
        kernel32.SetHandleInformation(number, 1, 0)   # HANDLE_FLAG_INHERIT off
        number = msvcrt.open_osfhandle(number, os.O_RDONLY if mode == "rb" else os.O_WRONLY)
    elif not stat.S_ISFIFO(os.fstat(number).st_mode):
        raise ChannelRefused("the desktop control descriptor is not a pipe")
    os.set_inheritable(number, False)
    return os.fdopen(number, mode, buffering=0)


def open_channel():
    """The two pipe ends named by ``DOWNLOADPULSE_DESKTOP_CONTROL`` ("<in>,<out>"),
    removed from the environment so nothing this backend starts sees it; or
    ``ChannelRefused``."""
    named = os.environ.pop(ENVIRONMENT, "")
    try:
        inbound, outbound = named.split(",")
        return _pipe(inbound, "rb"), _pipe(outbound, "wb")
    except (ValueError, OSError) as exc:
        raise ChannelRefused(f"the desktop control channel cannot be used: {exc}") from None


# What the tray counts as active: transfers at work and extractions running.
# A paused transfer is not active (the statistics' ``active_operations`` counts
# it with its work); it is what Resume All brings back.
WORKING_STATUSES = ("downloading", "processing", "uploading")


def _project_status(stats: dict) -> dict:
    by_status = stats.get("by_status") or {}
    active = sum(int(by_status.get(status) or 0) for status in WORKING_STATUSES) + int(stats.get("extracting_count") or 0)
    return {"active": active, "queued": int(stats.get("queued_downloads") or 0),
            "errors": int(stats.get("error_count") or 0), "completed": int(stats.get("completed_count") or 0),
            "paused": bool(stats.get("paused"))}


def _project_event(item: dict) -> dict:
    return {"id": int(item["id"]), "type": item["type"], "outcome": item.get("outcome"),
            "transfer_id": item.get("transfer_id"), "name": item.get("name"), "occurred_at": item.get("occurred_at")}


async def answer(application, request) -> dict:
    """The answer to one request; a refusal for anything outside its operations (``_FIELDS``)."""
    if not isinstance(request, dict) or not isinstance(request.get("id"), int) or isinstance(request.get("id"), bool):
        return {"id": None, "error": "refused"}
    ident, op = request["id"], request.get("op")
    if not isinstance(op, str) or op not in _FIELDS or set(request) - _FIELDS[op]:
        return {"id": ident, "error": "refused"}
    if op == "status":
        from api.routes import _configured_local_port, get_stats
        from application.desktop_listener import listener
        return {"id": ident, "status": {**_project_status(await get_stats(application)),
                                        "local_port": listener.state(_configured_local_port())}}
    if op in {"pause", "resume"}:
        result = await (application.pause_all() if op == "pause" else application.resume_all())
        return {"id": ident, "ok": bool(result.get("ok")), "paused": bool(result.get("paused"))}
    if op == "show":
        global _show_requested
        asked, _show_requested = _show_requested, False
        return {"id": ident, "show": asked}
    if op == "frame":
        return {"id": ident, "frame": dict(_frame) if _frame else None}
    if op == "locale":
        # The language the desktop application's interface is shown in: its one person is the default
        # Admin (it is always Standalone), so that account's own choice, else the installation's
        # default -- what the page shows. The tray and notices follow it while the application runs.
        from auth.accounts import DEFAULT_ADMIN_ID
        from services.account_appearance import load_locale
        return {"id": ident, "locale": (await load_locale(DEFAULT_ADMIN_ID))["locale"]}
    if op == "saves":
        waiting = [(number, entry) for number, entry in sorted(_saves.items()) if not entry[2]]
        for _number, entry in waiting:
            entry[2] = True                         # handed out once
        return {"id": ident, "saves": [{"save": number, "name": entry[0]} for number, entry in waiting]}
    if op == "saved":
        number, path = request.get("save"), request.get("path")
        if not isinstance(number, int) or isinstance(number, bool) or number not in _saves \
                or not (path is None or (isinstance(path, str) and 0 < len(path) <= MAX_PATH and os.path.isabs(path))):
            return {"id": ident, "error": "refused"}
        future = _saves[number][1]
        if not future.done():
            future.set_result(path)
        return {"id": ident, "ok": True}
    after, limit = request.get("after"), request.get("limit", MAX_EVENTS)
    if (after is not None and (not isinstance(after, int) or isinstance(after, bool) or after < 0)) \
            or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_EVENTS:
        return {"id": ident, "error": "refused"}
    from db import event_journal
    from db.database import get_db
    async with get_db() as db:
        if after is None:   # the cursor to start from: no history is replayed as notices
            return {"id": ident, "cursor": await event_journal.latest_id(db), "events": []}
        page = await event_journal.page(db, limit=limit, before=None, snapshot=None, category="transfer",
                                        severity=None, since=None, search=None, after=after)
    items = sorted(page["items"], key=lambda item: item["id"])
    cursor = max([after, *(item["id"] for item in items)])
    return {"id": ident, "cursor": cursor,
            "events": [_project_event(item) for item in items
                       if item["type"] in LIFECYCLE or item.get("outcome") == "resumed"]}


def serve(application, loop: asyncio.AbstractEventLoop, channel) -> threading.Thread:
    """Answer the supervisor until its pipe ends, then stop this backend."""
    inbound, outbound = channel

    def run() -> None:
        try:
            while True:
                line = inbound.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE or not line.endswith(b"\n"):
                    reply = {"id": None, "error": "refused"}
                    if not line.endswith(b"\n"):   # an oversized line: its rest is no request either
                        while (rest := inbound.readline(MAX_LINE + 1)) and not rest.endswith(b"\n"):
                            pass
                else:
                    try:
                        request = json.loads(line)
                    except ValueError:
                        request = None
                    try:
                        reply = asyncio.run_coroutine_threadsafe(answer(application, request), loop).result()
                    except Exception:  # noqa: BLE001 -- the owner's failure, never the channel's end
                        logger.exception("desktop control request failed")
                        reply = {"id": request.get("id") if isinstance(request, dict) else None, "error": "failed"}
                outbound.write(json.dumps(reply, separators=(",", ":")).encode("utf-8") + b"\n")
        except OSError:
            pass
        finally:
            for end in (inbound, outbound):
                try:
                    end.close()
                except OSError:
                    pass
            logger.info("Desktop supervisor channel closed; stopping")
            loop.call_soon_threadsafe(signal.raise_signal, signal.SIGINT)

    thread = threading.Thread(target=run, name="desktop-control", daemon=True)
    thread.start()
    return thread
