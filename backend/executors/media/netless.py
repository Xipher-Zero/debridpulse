"""Run one native Media Downloads helper with no network capability at all.

The sandboxed worker (``executors.media.worker``) is the only process of an
acquisition that may reach the network, and only through the egress guard; its
Python audit hook governs the worker's own code. The native helpers it may
start -- ffmpeg/ffprobe (local finalization, version probes), mkvmerge (the
MKV finalizer) and deno (yt-dlp's JavaScript challenge solver) -- are native
code that hook cannot see. So each is started through this shim, which asks
the KERNEL to take their network away before the helper's first instruction:

* ``PR_SET_NO_NEW_PRIVS`` and a seccomp-BPF filter that refuses (``EACCES``)
  every ``socket()`` outside ``AF_UNIX`` (no IPv4, IPv6, netlink or packet
  socket can exist, so nothing can connect, send or resolve over a network)
  and ``io_uring_setup`` (whose operations could create sockets without
  ``socket()``); any foreign-ABI system call kills the process;
* a seccomp filter is inherited by every child across ``fork``/``exec`` and
  can never be removed, so nothing the helper starts regains a network.

Then it ``exec``s the real tool with the arguments unchanged. If the filter
cannot be installed, the helper is not run at all.

Standard library only: it is executed by its own wrapper scripts
(``executors.media.sandbox.MediaSandbox``) as ``python -I``.
"""
from __future__ import annotations

import ctypes
import os
import platform
import struct
import sys

_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_RET_ALLOW = 0x7FFF0000
_RET_KILL_PROCESS = 0x80000000
_RET_EACCES = 0x00050000 | 13
_AF_UNIX = 1
_LD_ABS_W = 0x20
_JEQ = 0x15
_JGE = 0x35
_RET = 0x06
# (audit architecture, socket, io_uring_setup, first foreign-ABI number)
_ARCHITECTURES = {
    "x86_64": (0xC000003E, 41, 425, 0x40000000),   # x32 numbers start at 0x40000000
    "aarch64": (0xC00000B7, 198, 425, 0x40000000),  # no foreign ABI; harmless bound
}


def program(machine: str | None = None) -> bytes:
    """The BPF filter for this machine, as ``struct sock_filter`` bytes."""
    arch, socket_nr, uring_nr, foreign = _ARCHITECTURES[machine or platform.machine()]
    # Jump offsets count from the next instruction.
    code = [
        (_LD_ABS_W, 0, 0, 4),            # 0: A = seccomp_data.arch
        (_JEQ, 0, 8, arch),              # 1: another ABI -> 10 (kill)
        (_LD_ABS_W, 0, 0, 0),            # 2: A = seccomp_data.nr
        (_JGE, 6, 0, foreign),           # 3: a foreign-ABI number -> 10 (kill)
        (_JEQ, 4, 0, uring_nr),          # 4: io_uring_setup -> 9 (refuse)
        (_JEQ, 0, 2, socket_nr),         # 5: anything but socket() -> 8 (allow)
        (_LD_ABS_W, 0, 0, 16),           # 6: A = low word of args[0], the domain
        (_JEQ, 0, 1, _AF_UNIX),          # 7: AF_UNIX -> 8 (allow), else -> 9 (refuse)
        (_RET, 0, 0, _RET_ALLOW),        # 8
        (_RET, 0, 0, _RET_EACCES),       # 9
        (_RET, 0, 0, _RET_KILL_PROCESS),  # 10
    ]
    return b"".join(struct.pack("=HBBI", *item) for item in code)


def confine() -> None:
    """Take this process's (and every future child's) network away, or raise."""
    filters = program()
    buffer = ctypes.create_string_buffer(filters)

    class _Program(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    fprog = _Program(len(filters) // 8, ctypes.cast(buffer, ctypes.c_void_p))
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "no_new_privs refused")
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.addressof(fprog), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "seccomp filter refused")


def run(tool: str) -> None:
    """``exec`` ``tool`` with this process's arguments, confined first."""
    try:
        confine()
    except (OSError, KeyError, AttributeError) as exc:
        sys.stderr.write(f"DebridPulse: refusing to run {os.path.basename(tool)} without network confinement "
                         f"({exc})\n")
        sys.exit(126)
    os.execv(tool, [tool, *sys.argv[1:]])
