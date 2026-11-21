"""The ports of DownloadPulse's private loopback listeners.

DownloadPulse runs listeners only it talks to -- the downloader egress guard and
the services executors run for themselves (a native daemon's RPC, a bundled
acquisition service). Each prefers its usual port, but a port is never
DownloadPulse's to reserve: another program on the same computer (a person's own
download daemon, a second DownloadPulse, a development server) may already
listen on it. A taken port must not stop an executor, so each listener takes
its usual port when it is free and otherwise one the operating system
chooses, and says so. Every deployment follows the same rule; a container's
own network almost always leaves the usual ports free.

Nothing outside DownloadPulse depends on these ports: the address in use is read
from whoever owns the listener, never assumed. A port people configure for
others to reach (the desktop application's Local Port) is not one of these: it
is never moved silently.
"""
from __future__ import annotations

import errno
import logging
import socket

logger = logging.getLogger("downloadpulse.ports")

LOOPBACK = "127.0.0.1"

# EADDRINUSE everywhere, and Winsock's WSAEADDRINUSE (10048) where Python
# reports the Windows error number.
_IN_USE = {errno.EADDRINUSE, 10048}


def address_in_use(exc: BaseException) -> bool:
    """True when ``exc`` says the address is already in use."""
    return isinstance(exc, OSError) and (exc.errno in _IN_USE or getattr(exc, "winerror", None) == 10048)


def _probe() -> socket.socket:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        # Windows: SO_REUSEADDR there would let this bind over a live listener.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        # POSIX, as the listeners themselves bind: a port left in TIME_WAIT by
        # a listener that just stopped is free; one with a live listener is not.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return probe


def available(port: int, host: str = LOOPBACK) -> bool:
    """Whether a listener could bind ``host:port`` now."""
    with _probe() as probe:
        try:
            probe.bind((host, int(port)))
        except OSError:
            return False
    return True


def free_port(host: str = LOOPBACK) -> int:
    """A port the operating system has free on ``host`` now."""
    with _probe() as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def loopback_port(preferred: int, *, name: str, host: str = LOOPBACK) -> int:
    """``preferred`` when it is free on ``host``, else a free one, said once.

    A child process binds the port itself after this answers, so another
    program can still take it in between: its owner treats an address-in-use
    failure at start as a reason to choose again, never as a broken executor."""
    if preferred and available(preferred, host):
        return int(preferred)
    chosen = free_port(host)
    logger.warning("%s: port %s is in use on %s by another program; using port %s", name, preferred, host, chosen)
    return chosen
