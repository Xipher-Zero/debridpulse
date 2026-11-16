"""The installed desktop application's private control channel.

The native desktop supervisor (``desktop/supervisor.py``) starts this backend
as its own child and hands it the read end of one anonymous pipe and the write
end of another, inherited by the child alone: ``DEBRIDPULSE_DESKTOP_CONTROL``
names them (descriptors on POSIX, handles on Windows; the numbers are no
secret -- only a process that holds the other ends can speak). There is no
socket, port, URL or file: nothing a page, another account or another process
can connect to. The authority is the supervisor's ownership of this process
and of those pipe ends, never a DebridPulse account -- HTTP authentication,
roles and permissions are untouched, and nothing here signs a session in.

The tray asks four things, each answered by its existing owner:

* ``status`` -- the ``/api/stats`` counters (``api.routes.get_stats``),
  projected to five numbers;
* ``events`` -- the activity journal (``db.event_journal.page``) after a
  cursor, transfer lifecycle events only (a resume: the outcome
  ``resumed``), projected to identifiers, type, outcome and the transfer's
  name (no message, detail or error text);
* ``pause`` / ``resume`` -- ``ApplicationService.pause_all`` / ``resume_all``.

One JSON object per line in, ``{"id": <int>, "op": <name>, ...}``; one line
answers it, echoing ``id``. Anything else -- another operation, a malformed or
oversized line, an unexpected field -- is refused and changes nothing.

The end of the supervisor's pipe (an explicit Quit, or the supervisor gone)
stops this backend through its own shutdown: the interrupt uvicorn already
handles, so DebridPulse's lifespan stops every owned executor as it always
does. A desktop backend without a verified channel does not start.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import stat
import threading

logger = logging.getLogger("debridpulse.desktop")

ENVIRONMENT = "DEBRIDPULSE_DESKTOP_CONTROL"
MAX_LINE = 4096
MAX_EVENTS = 100
# The journal's transfer lifecycle: what the tray's notices are made of.
LIFECYCLE = frozenset({"transfer.accepted", "transfer.queued", "transfer.transferring", "transfer.paused",
                       "transfer.completed", "transfer.failed", "transfer.cancelled"})
_FIELDS = {"status": {"id", "op"}, "events": {"id", "op", "after", "limit"},
           "pause": {"id", "op"}, "resume": {"id", "op"}}


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
    """The two pipe ends named by ``DEBRIDPULSE_DESKTOP_CONTROL`` ("<in>,<out>"),
    removed from the environment so nothing this backend starts sees it; or
    ``ChannelRefused``."""
    named = os.environ.pop(ENVIRONMENT, "")
    try:
        inbound, outbound = named.split(",")
        return _pipe(inbound, "rb"), _pipe(outbound, "wb")
    except (ValueError, OSError) as exc:
        raise ChannelRefused(f"the desktop control channel cannot be used: {exc}") from None


def _project_status(stats: dict) -> dict:
    return {"active": int(stats.get("active_operations") or 0), "queued": int(stats.get("queued_downloads") or 0),
            "errors": int(stats.get("error_count") or 0), "completed": int(stats.get("completed_count") or 0),
            "paused": bool(stats.get("paused"))}


def _project_event(item: dict) -> dict:
    return {"id": int(item["id"]), "type": item["type"], "outcome": item.get("outcome"),
            "transfer_id": item.get("transfer_id"), "name": item.get("name"), "occurred_at": item.get("occurred_at")}


async def answer(application, request) -> dict:
    """The answer to one request; a refusal for anything outside the four."""
    if not isinstance(request, dict) or not isinstance(request.get("id"), int) or isinstance(request.get("id"), bool):
        return {"id": None, "error": "refused"}
    ident, op = request["id"], request.get("op")
    if not isinstance(op, str) or op not in _FIELDS or set(request) - _FIELDS[op]:
        return {"id": ident, "error": "refused"}
    if op == "status":
        from api.routes import get_stats
        return {"id": ident, "status": _project_status(await get_stats(application))}
    if op in {"pause", "resume"}:
        result = await (application.pause_all() if op == "pause" else application.resume_all())
        return {"id": ident, "ok": bool(result.get("ok")), "paused": bool(result.get("paused"))}
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
