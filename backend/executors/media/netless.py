"""Run one native Media Downloads helper with no network capability at all.

The sandboxed worker (``executors.media.worker``) is the only process of an
acquisition that may reach the network, and only through the egress guard; its
Python audit hook governs the worker's own code. The native helpers it may
start -- ffmpeg/ffprobe (the lossless finalizer, version probes) and deno
(yt-dlp's JavaScript challenge solver) -- are native code that hook cannot
see. So each is started through this shim, which asks the KERNEL to take their
network away before the helper's first instruction, with each system's own
mechanism:

* Linux: ``PR_SET_NO_NEW_PRIVS`` and a seccomp-BPF filter that refuses
  (``EACCES``) every ``socket()`` outside ``AF_UNIX`` (no IPv4, IPv6, netlink
  or packet socket can exist, so nothing can connect, send or resolve over a
  network) and ``io_uring_setup`` (whose operations could create sockets
  without ``socket()``); any foreign-ABI system call kills the process. A
  seccomp filter is inherited by every child across ``fork``/``exec`` and can
  never be removed, so nothing the helper starts regains a network.
* macOS: a Seatbelt sandbox (``sandbox_init``) that denies every network
  operation (IP and unix-domain alike, so no resolver daemon either), every
  Mach service lookup (no system service -- LaunchServices opening a URL in an
  unconfined browser, a URL-session daemon -- can reach the network on the
  helper's behalf), Apple Events, and every file write outside the attempt's
  workspace (its ``TMPDIR``), so nothing can be planted for an unconfined
  process to run later. A sandbox survives ``exec`` and binds every
  descendant, and is never lifted.
* Windows: the tool runs in an AppContainer with no capability at all --
  Windows admits no network for it, internet, private network or loopback
  (the egress guard included) -- that may create no child process
  (``PROCESS_CREATION_CHILD_PROCESS_RESTRICTED``), on a private desktop, in a
  job that ends it with this shim. It reaches only the files granted to that
  AppContainer (``permit``): its own program directory and the workspace.
  Windows cannot replace a process image, so this shim waits for the tool and
  exits with its code; a packaged launcher (``netless-launcher.exe``) is the
  executable the worker starts in place of the tool and runs this shim.

Then the real tool runs with its arguments unchanged. If the confinement cannot
be established, the helper is not run at all.

Standard library only: it is executed by its own wrapper scripts
(``executors.media.sandbox.MediaSandbox``) as ``python -I``.
"""
from __future__ import annotations

import ctypes
import os
import platform
import struct
import subprocess
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


def _confine_linux() -> None:
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


# The Seatbelt profile: everything a helper does locally stays possible (it
# reads its inputs and libraries, runs as any program does); what it could use
# to reach a network, directly or through another process, is denied.
_SEATBELT = ('(version 1)(allow default)'
             '(deny network*)(deny mach-lookup)(deny appleevent-send)'
             '(deny file-write*)'
             '(allow file-write* (subpath "{workspace}") (literal "/dev/null") (literal "/dev/dtracehelper"))')


def seatbelt(workspace: str) -> str:
    """The macOS sandbox profile of a helper whose only writable tree is
    ``workspace`` (resolved, absolute; a path the profile language would have
    to quote is refused)."""
    if not os.path.isabs(workspace) or any(char in workspace for char in '"\\\n\0'):
        raise ValueError("the helper workspace cannot be named in a sandbox profile")
    return _SEATBELT.format(workspace=workspace)


def _confine_macos() -> None:
    named = os.environ.get("TMPDIR") or ""
    if not os.path.isabs(named):
        raise ValueError("the helper has no absolute workspace (TMPDIR)")
    profile = seatbelt(os.path.realpath(named)).encode("utf-8")
    libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libsystem.sandbox_init.argtypes = [ctypes.c_char_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_char_p)]
    error = ctypes.c_char_p()
    if libsystem.sandbox_init(profile, 0, ctypes.byref(error)) != 0:
        raise OSError(f"sandbox refused: {(error.value or b'').decode('utf-8', 'replace')}")


def confine() -> None:
    """Take this process's (and every future child's) network away, or raise."""
    if sys.platform.startswith("linux"):
        _confine_linux()
    elif sys.platform == "darwin":
        _confine_macos()
    else:
        raise OSError(f"no network confinement for {sys.platform}")


# ── Windows: AppContainer ────────────────────────────────────────────────
CONTAINER = "DownloadPulse.MediaHelpers"
DESKTOP = "DownloadPulse-MediaHelpers"
_SECURITY_CAPABILITIES = 0x00020009
_CHILD_PROCESS_POLICY = 0x0002000E
_HANDLE_LIST = 0x00020002
_CHILD_PROCESS_RESTRICTED = 0x1
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_SUSPENDED = 0x00000004
_STARTF_USESTDHANDLES = 0x00000100
_KILL_ON_JOB_CLOSE = 0x00002000
_SE_WINDOW_OBJECT = 7
_DACL = 0x4
_GENERIC_READ_EXECUTE = 0x80000000 | 0x20000000
_GENERIC_ALL = 0x10000000


def _win32():
    from ctypes import wintypes as w
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    env = ctypes.WinDLL("userenv", use_last_error=True)
    env.CreateAppContainerProfile.argtypes = [w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, w.DWORD,
                                              ctypes.POINTER(ctypes.c_void_p)]
    env.CreateAppContainerProfile.restype = ctypes.c_long
    env.DeriveAppContainerSidFromAppContainerName.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    env.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
    adv.GetSecurityInfo.argtypes = [w.HANDLE, ctypes.c_int, w.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.SetEntriesInAclW.argtypes = [w.ULONG, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.SetSecurityInfo.argtypes = [w.HANDLE, ctypes.c_int, w.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_void_p, ctypes.c_void_p]
    u32.GetProcessWindowStation.restype = w.HANDLE
    u32.GetUserObjectInformationW.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD,
                                              ctypes.POINTER(w.DWORD)]
    u32.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p]
    u32.CreateDesktopW.restype = w.HANDLE
    k32.GetStdHandle.restype = w.HANDLE
    k32.SetHandleInformation.argtypes = [w.HANDLE, w.DWORD, w.DWORD]
    k32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, w.DWORD, w.DWORD,
                                                      ctypes.POINTER(ctypes.c_size_t)]
    k32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                              ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    k32.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL, w.DWORD,
                                   ctypes.c_void_p, w.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p]
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    k32.CreateJobObjectW.restype = w.HANDLE
    k32.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    k32.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    k32.ResumeThread.argtypes = [w.HANDLE]
    k32.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    k32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    k32.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    return w, k32, adv, u32, env


_ALREADY_EXISTS = 0x800700B7   # HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)


def _container_sid(env) -> ctypes.c_void_p:
    """The helpers' AppContainer SID, its per-user profile created first if
    it is missing: Windows starts no process in an AppContainer that has no
    profile."""
    sid = ctypes.c_void_p()
    result = env.CreateAppContainerProfile(CONTAINER, "DownloadPulse media helpers",
                                           "DownloadPulse Media Downloads helpers: no network, no children",
                                           None, 0, ctypes.byref(sid))
    if result & 0xFFFFFFFF == _ALREADY_EXISTS:
        result = env.DeriveAppContainerSidFromAppContainerName(CONTAINER, ctypes.byref(sid))
    if result != 0 or not sid:
        raise OSError(f"no AppContainer identity (0x{result & 0xFFFFFFFF:08x})")
    return sid


def container_sid() -> str:
    """The AppContainer's SID, as text (what ``permit`` grants access to)."""
    w, _k32, adv, _u32, env = _win32()
    text = w.LPWSTR()
    if not adv.ConvertSidToStringSidW(_container_sid(env), ctypes.byref(text)):
        raise ctypes.WinError(ctypes.get_last_error())
    return text.value


def permit(path: str, *, write: bool) -> None:
    """Let the helpers' AppContainer read and run (``write``: also change)
    ``path`` and, for a directory, everything in it -- the only files a
    confined helper can open besides what Windows opens to every
    AppContainer."""
    rights = "M" if write else "RX"
    grant = f"*{container_sid()}:" + (f"(OI)(CI)({rights})" if os.path.isdir(path) else f"({rights})")
    icacls = os.path.join(os.environ.get("SYSTEMROOT") or r"C:\Windows", "System32", "icacls.exe")
    # This module is loaded on its own (the helpers' wrappers put only its own
    # directory on the path), so it names the Windows flag itself rather than
    # importing core.processes.WINDOWLESS: no console window for icacls.
    result = subprocess.run([icacls, path, "/grant", grant, "/Q"], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=60,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        raise OSError(f"cannot grant the media helpers access to {path}: "
                      f"{(result.stdout + result.stderr).strip()[:200]}")


def _grant_object(w, adv, handle, sid, rights: int) -> None:
    """Add one ACE for ``sid`` to a window station or desktop."""
    class Trustee(ctypes.Structure):
        _fields_ = [("multiple", ctypes.c_void_p), ("operation", ctypes.c_int), ("form", ctypes.c_int),
                    ("type", ctypes.c_int), ("name", ctypes.c_void_p)]

    class Access(ctypes.Structure):
        _fields_ = [("rights", w.DWORD), ("mode", ctypes.c_int), ("inheritance", w.DWORD), ("trustee", Trustee)]

    dacl, descriptor, merged = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    if adv.GetSecurityInfo(handle, _SE_WINDOW_OBJECT, _DACL, None, None, ctypes.byref(dacl), None,
                           ctypes.byref(descriptor)) != 0:
        raise OSError("cannot read a window object's access list")
    entry = Access(rights, 1, 0, Trustee(None, 0, 0, 5, sid))   # GRANT_ACCESS, TRUSTEE_IS_SID
    if adv.SetEntriesInAclW(1, ctypes.byref(entry), dacl, ctypes.byref(merged)) != 0 \
            or adv.SetSecurityInfo(handle, _SE_WINDOW_OBJECT, _DACL, None, None, merged, None) != 0:
        raise OSError("cannot grant the helpers' window object")


def _run_contained(tool: str, arguments: list[str]) -> int:
    w, k32, adv, u32, env = _win32()
    sid = _container_sid(env)

    class Capabilities(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("capabilities", ctypes.c_void_p), ("count", w.DWORD),
                    ("reserved", w.DWORD)]

    class StartupInfo(ctypes.Structure):
        _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR), ("title", w.LPWSTR),
                    ("geometry", w.DWORD * 8), ("flags", w.DWORD), ("show", w.WORD), ("reserved2", w.WORD),
                    ("reserved3", ctypes.c_void_p), ("stdin", w.HANDLE), ("stdout", w.HANDLE),
                    ("stderr", w.HANDLE), ("attributes", ctypes.c_void_p)]

    class ProcessInformation(ctypes.Structure):
        _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]

    # A private desktop of this window station: the helper sees no window,
    # input or clipboard of the user's.
    station = u32.GetProcessWindowStation()
    name, size = ctypes.create_unicode_buffer(256), w.DWORD()
    if not station or not u32.GetUserObjectInformationW(station, 2, name, ctypes.sizeof(name), ctypes.byref(size)):
        raise OSError("no window station")
    desktop = u32.CreateDesktopW(DESKTOP, None, None, 0, _GENERIC_ALL, None)
    if not desktop:
        raise ctypes.WinError(ctypes.get_last_error())
    _grant_object(w, adv, station, sid, _GENERIC_READ_EXECUTE)
    _grant_object(w, adv, desktop, sid, _GENERIC_ALL)

    capabilities = Capabilities(sid, None, 0, 0)
    policy = w.DWORD(_CHILD_PROCESS_RESTRICTED)
    handles = []
    for which in (-10, -11, -12):
        handle = k32.GetStdHandle(which)
        if handle and handle != w.HANDLE(-1).value and handle not in handles:
            k32.SetHandleInformation(handle, 1, 1)   # HANDLE_FLAG_INHERIT: the tool's standard streams
            handles.append(handle)
    inherited = (w.HANDLE * max(len(handles), 1))(*handles)
    size = ctypes.c_size_t()
    k32.InitializeProcThreadAttributeList(None, 3, 0, ctypes.byref(size))
    attributes = ctypes.create_string_buffer(size.value)
    if not k32.InitializeProcThreadAttributeList(attributes, 3, 0, ctypes.byref(size)) \
            or not k32.UpdateProcThreadAttribute(attributes, 0, _SECURITY_CAPABILITIES, ctypes.byref(capabilities),
                                                 ctypes.sizeof(capabilities), None, None) \
            or not k32.UpdateProcThreadAttribute(attributes, 0, _CHILD_PROCESS_POLICY, ctypes.byref(policy),
                                                 ctypes.sizeof(policy), None, None) \
            or (handles and not k32.UpdateProcThreadAttribute(attributes, 0, _HANDLE_LIST, inherited,
                                                              ctypes.sizeof(w.HANDLE) * len(handles), None, None)):
        raise ctypes.WinError(ctypes.get_last_error())
    startup = StartupInfo()
    startup.cb = ctypes.sizeof(StartupInfo)
    startup.desktop = f"{name.value}\\{DESKTOP}"
    startup.attributes = ctypes.cast(attributes, ctypes.c_void_p)
    if handles:
        startup.flags = _STARTF_USESTDHANDLES
        startup.stdin, startup.stdout, startup.stderr = (k32.GetStdHandle(which) for which in (-10, -11, -12))
    job = k32.CreateJobObjectW(None, None)
    limits = (ctypes.c_ubyte * 144)()          # JOBOBJECT_EXTENDED_LIMIT_INFORMATION (x64)
    ctypes.c_uint32.from_buffer(limits, 16).value = _KILL_ON_JOB_CLOSE   # BasicLimitInformation.LimitFlags
    if not job or not k32.SetInformationJobObject(job, 9, limits, ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())
    command = ctypes.create_unicode_buffer(subprocess.list2cmdline([tool, *arguments]))
    information = ProcessInformation()
    if not k32.CreateProcessW(tool, command, None, None, bool(handles),
                              _EXTENDED_STARTUPINFO_PRESENT | _CREATE_SUSPENDED, None, None,
                              ctypes.byref(startup), ctypes.byref(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not k32.AssignProcessToJobObject(job, information.process):
        k32.TerminateProcess(information.process, 126)
        raise ctypes.WinError(ctypes.get_last_error())
    k32.ResumeThread(information.thread)
    k32.WaitForSingleObject(information.process, 0xFFFFFFFF)
    code = w.DWORD()
    k32.GetExitCodeProcess(information.process, ctypes.byref(code))
    return code.value


def run(tool: str) -> None:
    """Run ``tool`` with this process's arguments, confined first: on POSIX
    by ``exec`` in place, on Windows as the AppContainer process this shim
    waits for."""
    try:
        if os.name == "nt":
            code = _run_contained(tool, sys.argv[1:])
        else:
            confine()
    except (OSError, KeyError, AttributeError, ValueError) as exc:
        sys.stderr.write(f"DownloadPulse: refusing to run {os.path.basename(tool)} without network confinement "
                         f"({exc})\n")
        sys.exit(126)
    if os.name == "nt":
        # The tool's exit code is a DWORD (a crash or a refused start leaves an
        # NTSTATUS above 2**31, e.g. 0xC0000142); sys.exit takes a C int, so it
        # is passed as the same 32 bits -- never collapsed to -1.
        sys.exit(code - (1 << 32) if code >= 1 << 31 else code)
    os.execv(tool, [tool, *sys.argv[1:]])
