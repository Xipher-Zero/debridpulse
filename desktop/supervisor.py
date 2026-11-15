"""The DebridPulse desktop supervisor: the thin native shim around the one
DebridPulse backend of an installed desktop application.

It owns, and only owns, what a desktop process needs and DebridPulse does not:

* the per-user data root (``data_root``) the backend's ``DB_PATH`` and
  ``CONFIG_PATH`` name, and one supervisor per data root (an exclusive lock);
* the backend process: started through DebridPulse's own process ownership
  (``executors.process_ownership``, the WS2 owner) on a free loopback port, in
  desktop mode (``DEBRIDPULSE_DESKTOP=1``), with the backend's private control
  channel (``application.desktop_control``): two anonymous pipes only the two
  processes hold;
* the native window, tray and notices: a Neutralino process whose page is the
  backend's own loopback interface, with no native token or library injected
  into it; the supervisor alone holds Neutralino's exported credential and
  calls its allow-listed native methods;
* Quit: closing the control channel makes the backend stop itself through its
  own shutdown; only a backend that does not end in time is ended forcibly
  (its process group or job), the crash backstop.

It reads no attempt marker, keeps no transfer state of its own beyond what one
run's notices need, and calls no DebridPulse HTTP API but the public health
check: the tray's status, events and Pause/Resume All travel on the private
channel to their canonical owners. Nothing it does signs a session in or
unlocks the interface.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_ID = "org.debridpulse.desktop"
BACKEND_ID = "desktop-backend"
SHELL_ID = "desktop-shell"
READY_SECONDS = 120
QUIT_SECONDS = 60
POLL_SECONDS = 3.0
NATIVE_METHODS = ("os.setTray", "os.showNotification", "window.show", "window.hide", "window.focus",
                  "window.unminimize")
logger = logging.getLogger("debridpulse.desktop.supervisor")


# ── layout ─────────────────────────────────────────────────────────────────

def layout() -> dict[str, Path]:
    """Where the bundled pieces are, relative to this file: the application
    (``backend``, ``frontend/static``), the bundled tools (``tools``) and the
    native shell (``shell``). ``DEBRIDPULSE_DESKTOP_LAYOUT`` names another
    root for a development run."""
    root = Path(os.environ.get("DEBRIDPULSE_DESKTOP_LAYOUT") or HERE.parent).resolve()
    return {"root": root, "backend": root / "backend", "static": root / "frontend" / "static",
            "tools": root / "tools", "shell": root / "desktop" / "shell"}


def data_root() -> Path:
    """The per-user application data root, outside the program files:
    Windows LocalAppData, macOS Application Support, XDG data (Flatpak's own
    writable data) elsewhere."""
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "DebridPulse"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "DebridPulse"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "debridpulse"


def shell_data_home() -> Path:
    """Neutralino's own data home for ``dataLocation: system`` (its
    ``sago::getDataHome``): where it exports its native credential. On
    Windows, the Roaming AppData known folder, asked of Windows itself
    (``SHGetKnownFolderPath``) as Neutralino does -- not an environment
    variable."""
    if os.name == "nt":
        return Path(_win32_api().known_folders()[1])
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


# Windows: the trustees a checked object's ACEs may name -- this account,
# SYSTEM, Administrators, OWNER RIGHTS (the owner, verified to be one of
# them) and, on inherit-only ACEs, CREATOR OWNER (whoever creates a child:
# inside a private directory, a trusted account).
_SYSTEM, _ADMINISTRATORS, _OWNER_RIGHTS, _CREATOR_OWNER = "S-1-5-18", "S-1-5-32-544", "S-1-3-4", "S-1-3-0"
# The only rights anyone else may hold on a directory ABOVE the credential's
# own (the profile's parent, the profile, every directory down to the data
# home): list/read, read extended attributes, traverse, read attributes, read
# the security descriptor, synchronize, generic read and execute. Any other
# right -- to add, delete or rename an entry, write data or attributes (a
# reparse point), change the DACL or the owner -- could replace or redirect
# the path, and fails closed.
_READ_ONLY = 0x1 | 0x8 | 0x20 | 0x80 | 0x00020000 | 0x00100000 | 0x80000000 | 0x20000000
# Above the profiles root (the volume root down to the profiles root's
# parent), Windows lets ordinary accounts add entries (e.g. Authenticated
# Users: Modify on C:\). What would replace or rename an EXISTING entry of the
# path is: on the entry, DELETE (moot on a volume root), WRITE_DAC,
# WRITE_OWNER, GENERIC_ALL; on its parent, FILE_DELETE_CHILD or GENERIC_ALL.
_DELETE, _REPLACE_SELF, _REPLACE_CHILD = 0x00010000, 0x00010000 | 0x00040000 | 0x00080000 | 0x10000000, 0x40 | 0x10000000
_TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"


def private_directory(path: Path) -> None:
    """Make ``path`` -- the directory Neutralino exports its native credential
    into -- and its parent (the application's own data directory) private to
    this OS account BEFORE the credential is written; or raise
    ``PermissionError`` (the shell is then not started). Neutralino itself
    creates them world-readable (0755, the file 0644), leaving the
    credential's privacy to whatever the home directory happens to be.

    POSIX: each is a real directory (never a link) owned by this account, mode
    0700.

    Windows -- the one supported layout: this account's profile
    (``GetUserProfileDirectoryW``) is a direct child of the system's profiles
    root (``GetProfilesDirectoryW``, e.g. ``C:\\Users``), and the data home
    (the Roaming known folder) lies inside the profile. Starting from the
    profiles root's volume root, nothing on the path may be replaceable or
    redirectable by another account:

    * the volume root down to the profiles root's parent: real directories,
      never reparse points, each resolving to itself, owned by this account,
      SYSTEM, Administrators or TrustedInstaller, on which nobody else holds a
      right that could replace or rename the next entry (``_REPLACE_SELF`` on
      it, ``_REPLACE_CHILD`` on its parent);
    * the profiles root, the profile and every directory below it down to the
      data home: real directories, never reparse points, owned by this
      account, SYSTEM or Administrators (TrustedInstaller above the profile),
      on which nobody else holds any right beyond reading and traversing
      (``_READ_ONLY``) -- Windows itself grants such rights there (Users,
      Everyone, capability SIDs); the profiles root and the profile resolve to
      themselves;
    * the application directory and ``.tmp``: real directories, never reparse
      points, owned by this account, whose every allow ACE names this account,
      SYSTEM, Administrators or OWNER RIGHTS (CREATOR OWNER on inherit-only
      ACEs); ``.tmp`` must also hand those to every file created in it (at
      least one file-inheriting allow ACE) -- otherwise Windows gives a new file
      the creating token's default DACL, which names the logon session too.

    A nested or relocated profile, a data home outside the profile, a
    redirected path, an intermediate junction or mount point, or an ACL that
    names anyone else is refused, not repaired."""
    if os.name != "nt":
        for directory in (path.parent, path):
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            import stat
            info = os.lstat(directory)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise PermissionError(f"{directory} is not a directory of this account")
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fchmod(descriptor, 0o700)
                if os.fstat(descriptor).st_mode & 0o077:
                    raise PermissionError(f"{directory} cannot be made private")
            finally:
                os.close(descriptor)
        return
    api = _win32_api()
    account = api.account_sid()
    trusted = {account, _SYSTEM, _ADMINISTRATORS}
    profile = Path(api.known_folders()[0])
    profiles = Path(api.profiles_directory())
    if os.path.normcase(str(profile.parent)) != os.path.normcase(str(profiles)):
        raise PermissionError(f"{profile} is not a direct child of the profiles root ({profiles}): "
                              f"a nested or relocated profile is not supported")
    application, tmp = path.parent, path
    chain = [Path(os.path.abspath(application.parent))]   # the data home ...
    if os.path.normcase(str(chain[0])) != os.path.normcase(str(profile)):
        if not os.path.normcase(str(chain[0])).startswith(os.path.normcase(str(profile)) + os.sep):
            raise PermissionError(f"{chain[0]} is not inside this account's profile ({profile}): not supported")
        while os.path.normcase(str(chain[-1].parent)) != os.path.normcase(str(profile)):
            chain.append(chain[-1].parent)                # ... and every directory up to the profile
        chain.append(profile)
    chain.reverse()                                       # profile first, data home last

    def facts(target: Path, *, directory: bool = True):
        handle = api.open(target, directory=directory)
        try:
            attributes = handle.attributes()
            owner, aces = handle.security()
            final = handle.final_path()
        finally:
            handle.close()
        if attributes & 0x400:                            # FILE_ATTRIBUTE_REPARSE_POINT
            raise PermissionError(f"{target} is a reparse point")
        if not attributes & 0x10:
            raise PermissionError(f"{target} is not a directory")
        if aces is None:
            raise PermissionError(f"{target} has no DACL (open to everyone)")
        return owner, aces, final

    def checked(target: Path, owners: set[str], rule: str) -> str:
        """``target``'s facts against ``rule`` ("unreplaceable", "read-only" or
        "private"); its final path."""
        owner, aces, final = facts(target)
        if owner not in owners:
            raise PermissionError(f"{target} is owned by {owner}")
        if rule != "private" and os.path.normcase(final) != os.path.normcase(str(target)):
            raise PermissionError(f"{target} resolves to {final}: a redirected path is not supported")
        root = target.parent == target
        for kind, flags, mask, trustee in aces:
            if kind not in (0, 1):                        # only ACCESS_ALLOWED / ACCESS_DENIED
                raise PermissionError(f"{target} has an ACE of type {kind}")
            if kind == 1 or trustee in owners or trustee in trusted or trustee == _OWNER_RIGHTS:
                continue
            if rule == "private":
                if not (trustee == _CREATOR_OWNER and flags & 0x08):   # INHERIT_ONLY
                    raise PermissionError(f"{target} grants access to {trustee}")
            elif flags & 0x08:                            # an inherit-only ACE grants nothing on this object
                continue
            elif rule == "read-only" and mask & ~_READ_ONLY:
                raise PermissionError(f"{target} grants {trustee} rights to change it (0x{mask:08x})")
            elif rule == "unreplaceable" and mask & ((_REPLACE_SELF & ~(_DELETE if root else 0)) | _REPLACE_CHILD):
                raise PermissionError(f"{target} lets {trustee} replace or rename an entry of the path (0x{mask:08x})")
        if rule == "private" and target == tmp and not any(
                kind == 0 and flags & 0x01 for kind, flags, _mask, _trustee in aces):   # OBJECT_INHERIT
            raise PermissionError(f"{target} does not hand its private ACL to the files created in it")
        return final

    # The trusted starting point: the profiles root's volume root, down to it.
    for ancestor in (*reversed(profiles.parents), ):
        checked(ancestor, trusted | {_TRUSTED_INSTALLER}, "unreplaceable")
    checked(profiles, trusted | {_TRUSTED_INSTALLER}, "read-only")
    # The profile and everything down to the data home.
    for directory in chain:
        checked(directory, trusted, "read-only")
    for directory in (application, tmp):                 # the credential's own: private
        try:
            directory.mkdir(mode=0o700)   # CPython: an owner-only, protected DACL
        except FileExistsError:
            pass
        checked(directory, {account}, "private")


def read_credential(path: Path) -> dict:
    """Neutralino's exported credential: a regular file of this account in the
    protected directory, opened without following a link (POSIX) or a reparse
    point (Windows), read once and deleted -- on Windows through the very
    handle that was verified, after the whole path is verified again
    (``private_directory``)."""
    if os.name == "nt":
        private_directory(path.parent)
        api = _win32_api()
        folder = api.open(path.parent, directory=True)
        try:
            expected = folder.final_path()
        finally:
            folder.close()
        credential = api.open(path, directory=False, delete=True)
        try:
            attributes = credential.attributes()
            owner, _aces = credential.security()
            if attributes & 0x400:
                raise PermissionError(f"{path} is a reparse point")
            if attributes & 0x10:
                raise PermissionError(f"{path} is not a regular file")
            if owner != api.account_sid():
                raise PermissionError(f"{path} is owned by {owner}")
            # The credential's own DACL, on the handle, before it is read.
            _owner, aces = credential.security()
            if aces is None:
                raise PermissionError(f"{path} has no DACL (open to everyone)")
            for kind, _flags, _mask, trustee in aces:
                if kind not in (0, 1) or (kind == 0 and trustee not in {
                        owner, _SYSTEM, _ADMINISTRATORS, _OWNER_RIGHTS}):
                    raise PermissionError(f"{path} grants access to {trustee or f'an ACE of type {kind}'}")
            if os.path.normcase(os.path.dirname(credential.final_path())) != os.path.normcase(expected):
                raise PermissionError(f"{path} is not inside its protected directory")
            value = json.loads(credential.read().decode("utf-8"))
            credential.delete()
        finally:
            credential.close()
        return value
    import errno
    import stat
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PermissionError(f"{path} is a link") from None
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise PermissionError(f"{path} is not this account's file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            value = json.loads(handle.read().decode("utf-8"))
    finally:
        os.close(descriptor)
    path.unlink(missing_ok=True)
    return value


# ── raw Windows bindings (facts only; the policy is above) ─────────────────

class _Win32Handle:
    """One file-system object opened WITHOUT following a reparse point."""

    def __init__(self, api, path: Path, handle):
        self.api, self.path, self.handle = api, path, handle

    def attributes(self) -> int:
        info = (self.api.wintypes.DWORD * 13)()            # BY_HANDLE_FILE_INFORMATION
        if not self.api.k32.GetFileInformationByHandle(self.handle, info):
            raise PermissionError(f"{self.path} cannot be inspected")
        return info[0]

    def security(self) -> tuple[str, list | None]:
        """(owner SID, [(ACE type, ACE flags, access mask, trustee SID)] or
        None for a NULL DACL)."""
        api, ctypes = self.api, self.api.ctypes
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        if api.adv.GetSecurityInfo(self.handle, 1, 0x1 | 0x4, ctypes.byref(owner), None, ctypes.byref(dacl), None,
                                   ctypes.byref(descriptor)) != 0:
            raise PermissionError(f"the security descriptor of {self.path} cannot be read")
        try:
            owner_sid = api.sid_text(owner)
            if not dacl:
                return owner_sid, None
            size = (api.wintypes.DWORD * 3)()               # ACL_SIZE_INFORMATION
            if not api.adv.GetAclInformation(dacl, size, ctypes.sizeof(size), 2):
                raise PermissionError(f"the DACL of {self.path} cannot be read")
            aces = []
            for index in range(size[0]):
                ace = ctypes.c_void_p()
                if not api.adv.GetAce(dacl, index, ctypes.byref(ace)):
                    raise PermissionError(f"an ACE of {self.path} cannot be read")
                kind, flags = ctypes.c_ubyte.from_address(ace.value).value, ctypes.c_ubyte.from_address(ace.value + 1).value
                mask = ctypes.c_uint32.from_address(ace.value + 4).value
                trustee = api.sid_text(ctypes.c_void_p(ace.value + 8)) if kind in (0, 1) else ""
                aces.append((kind, flags, mask, trustee))
            return owner_sid, aces
        finally:
            api.k32.LocalFree(descriptor)

    def final_path(self) -> str:
        buffer = self.api.ctypes.create_unicode_buffer(32768)
        length = self.api.k32.GetFinalPathNameByHandleW(self.handle, buffer, 32768, 0)
        if not 0 < length < 32768:
            raise PermissionError(f"the final path of {self.path} cannot be read")
        return buffer.value.removeprefix("\\\\?\\")

    def read(self) -> bytes:
        chunks, buffer, got = [], self.api.ctypes.create_string_buffer(65536), self.api.wintypes.DWORD()
        while True:
            if not self.api.k32.ReadFile(self.handle, buffer, 65536, self.api.ctypes.byref(got), None):
                raise PermissionError(f"{self.path} cannot be read")
            if not got.value:
                return b"".join(chunks)
            chunks.append(buffer.raw[:got.value])

    def delete(self) -> None:
        flag = self.api.ctypes.c_ubyte(1)                  # FILE_DISPOSITION_INFO.DeleteFile
        if not self.api.k32.SetFileInformationByHandle(self.handle, 4, self.api.ctypes.byref(flag), 1):
            raise PermissionError(f"{self.path} cannot be deleted")

    def close(self) -> None:
        if self.handle:
            self.api.k32.CloseHandle(self.handle)
            self.handle = None


def _win32_api():
    """The raw kernel32/advapi32/userenv/shell32 bindings the policy above uses."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    env = ctypes.WinDLL("userenv", use_last_error=True)
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32")
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    k32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    adv.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    adv.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
    adv.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.c_void_p]
    env.GetUserProfileDirectoryW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    env.GetProfilesDirectoryW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.c_void_p]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    class Api:
        pass

    api = Api()
    api.ctypes, api.wintypes, api.k32, api.adv = ctypes, wintypes, k32, adv
    invalid = wintypes.HANDLE(-1).value

    def open(path: Path, *, directory: bool, delete: bool = False) -> _Win32Handle:
        access = 0x80000000 | 0x00020000 | 0x80 | (0x00010000 if delete else 0)  # GENERIC_READ|READ_CONTROL|READ_ATTRIBUTES|DELETE
        flags = 0x00200000 | (0x02000000 if directory else 0)    # FILE_FLAG_OPEN_REPARSE_POINT | BACKUP_SEMANTICS
        handle = k32.CreateFileW(str(path), access, 0x1 | 0x4 | (0x2 if directory else 0), None, 3, flags, None)
        if handle in (None, 0, invalid):
            error = ctypes.get_last_error()
            if error in (2, 3):                   # not there (yet): ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND
                raise FileNotFoundError(error, "not found", str(path))
            raise PermissionError(f"{path} cannot be opened ({error})")
        return _Win32Handle(api, path, handle)

    def sid_text(sid) -> str:
        value = ctypes.c_void_p()
        if not adv.ConvertSidToStringSidW(sid, ctypes.byref(value)):
            raise PermissionError("a SID cannot be read")
        try:
            return ctypes.wstring_at(value.value)
        finally:
            k32.LocalFree(value)                  # ConvertSidToStringSidW allocates with LocalAlloc

    def token():
        handle = wintypes.HANDLE()
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x8, ctypes.byref(handle)):   # TOKEN_QUERY
            raise PermissionError("this process's token cannot be read")
        return handle

    def account_sid() -> str:
        handle, user = token(), ctypes.create_string_buffer(512)
        try:
            if not adv.GetTokenInformation(handle, 1, user, 512, ctypes.byref(wintypes.DWORD())):   # TokenUser
                raise PermissionError("this account's SID cannot be read")
        finally:
            k32.CloseHandle(handle)
        return sid_text(ctypes.c_void_p(ctypes.c_void_p.from_buffer(user).value))

    def known_folders() -> tuple[str, str]:
        """(this account's profile directory, its Roaming AppData folder)."""
        handle, size = token(), wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(32768)
        try:
            if not env.GetUserProfileDirectoryW(handle, buffer, ctypes.byref(size)):
                raise PermissionError("this account's profile directory cannot be read")
        finally:
            k32.CloseHandle(handle)
        roaming_id = (ctypes.c_ubyte * 16).from_buffer_copy(   # FOLDERID_RoamingAppData {3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}
            bytes.fromhex("db85b63ef965f64ca03ae3ef65729f3d"))
        value = ctypes.c_void_p()
        if shell.SHGetKnownFolderPath(ctypes.byref(roaming_id), 0, None, ctypes.byref(value)) != 0:
            raise PermissionError("the Roaming AppData folder cannot be read")
        try:
            roaming = ctypes.wstring_at(value.value)
        finally:
            ole.CoTaskMemFree(value)              # SHGetKnownFolderPath allocates with CoTaskMemAlloc
        return buffer.value, roaming

    def profiles_directory() -> str:
        """The system's profiles root (``GetProfilesDirectoryW``)."""
        size, buffer = wintypes.DWORD(32768), ctypes.create_unicode_buffer(32768)
        if not env.GetProfilesDirectoryW(buffer, ctypes.byref(size)):
            raise PermissionError("the profiles directory cannot be read")
        return buffer.value

    api.open, api.sid_text, api.account_sid, api.known_folders = open, sid_text, account_sid, known_folders
    api.profiles_directory = profiles_directory
    return api


def single_instance(root: Path):
    """The supervisor's exclusive lock on its data root, or ``None`` when
    another supervisor holds it. Held as long as the returned file is open."""
    root.mkdir(parents=True, exist_ok=True)
    handle = open(root / "desktop.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# ── the private channel (supervisor end) ───────────────────────────────────

class Channel:
    """The supervisor's ends of the backend's control pipes: one request at a
    time, answered in order (``application.desktop_control``)."""

    def __init__(self):
        self.request_read, self.request_write = os.pipe()     # supervisor writes, backend reads
        self.answer_read, self.answer_write = os.pipe()       # backend writes, supervisor reads
        self._lock = asyncio.Lock()
        self._next = 0
        self._out = self._in = None

    def child_ends(self) -> tuple[int, int]:
        """The backend's two ends, made inheritable (POSIX descriptors, or the
        Windows handles behind them) for that one child."""
        for fd in (self.request_read, self.answer_write):
            os.set_inheritable(fd, True)
        if os.name == "nt":
            import msvcrt
            return msvcrt.get_osfhandle(self.request_read), msvcrt.get_osfhandle(self.answer_write)
        return self.request_read, self.answer_write

    def started(self) -> None:
        """The backend holds its ends: the supervisor's copies are closed, so
        the pipes end exactly when one of the two processes lets go."""
        os.close(self.request_read)
        os.close(self.answer_write)
        self._out = os.fdopen(self.request_write, "wb", buffering=0)
        self._in = os.fdopen(self.answer_read, "rb", buffering=0)

    async def ask(self, op: str, **fields) -> dict:
        async with self._lock:
            self._next += 1
            line = json.dumps({"id": self._next, "op": op, **fields}, separators=(",", ":")).encode() + b"\n"
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._out.write, line)
            answer = await loop.run_in_executor(None, self._in.readline)
            if not answer:
                raise ConnectionError("the backend's control channel ended")
            return json.loads(answer)

    def close(self) -> None:
        """Quit: the backend reads the end of its input and stops itself."""
        for end in (self._out, self._in):
            try:
                if end is not None:
                    end.close()
            except OSError:
                pass


# ── words ──────────────────────────────────────────────────────────────────

class Words:
    """The tray's and notices' words, from DebridPulse's own catalogs in the
    installation's default language (English when none is chosen)."""

    def __init__(self, config_path: Path):
        from core import localization
        self._message = localization.message
        try:
            tag = json.loads(config_path.read_text(encoding="utf-8")).get("default_locale")
        except (OSError, ValueError):
            tag = None
        self.tag = tag if tag in localization.locale_tags() else localization.DEFAULT_LOCALE

    def __call__(self, key: str, **params) -> str:
        return self._message(key, self.tag, **params)


# ── the native shell ───────────────────────────────────────────────────────

class Shell:
    """Neutralino, driven over its own loopback WebSocket with the credential
    it exported for this supervisor (never given to the page)."""

    def __init__(self, session):
        self.session, self.ws, self.token, self._next = session, None, "", 0
        self.events: asyncio.Queue = asyncio.Queue()
        self._pending: dict[str, asyncio.Future] = {}

    async def connect(self, auth_file: Path, deadline: float) -> None:
        while True:
            try:
                info = read_credential(auth_file)   # read once, then deleted; it is nobody else's
                break
            except PermissionError:
                raise
            except (OSError, ValueError):
                if time.monotonic() > deadline:
                    raise TimeoutError("the native shell exported no credential") from None
                await asyncio.sleep(0.2)
        self.token = str(info["nlToken"])
        self.ws = await self.session.ws_connect(
            f"ws://127.0.0.1:{int(info['nlPort'])}?connectToken={info['nlConnectToken']}")
        asyncio.ensure_future(self._receive())

    async def _receive(self) -> None:
        async for message in self.ws:
            try:
                value = json.loads(message.data)
            except (TypeError, ValueError):
                continue
            future = self._pending.pop(str(value.get("id")), None) if value.get("id") else None
            if future is not None and not future.done():
                future.set_result(value.get("data"))
            elif value.get("event"):
                await self.events.put((value["event"], value.get("data")))
        await self.events.put(("shellClosed", None))

    async def call(self, method: str, data: dict | None = None):
        if method not in NATIVE_METHODS:
            raise PermissionError(method)
        self._next += 1
        ident = f"dp-{self._next}"
        self._pending[ident] = asyncio.get_running_loop().create_future()
        await self.ws.send_str(json.dumps({"id": ident, "method": method, "accessToken": self.token,
                                           "data": data or {}}))
        return await asyncio.wait_for(self._pending[ident], 15)


# ── the run ────────────────────────────────────────────────────────────────

LIFECYCLE_NOTICES = {"transfer.accepted": "desktop.notice.accepted", "transfer.completed": "desktop.notice.completed",
                     "transfer.failed": "error.category.transfer_failed",
                     "transfer.cancelled": "file_selection.transfer_cancelled",
                     "transfer.paused": "transfer.action.paused_toast"}


def notices(events: list[dict], previous: dict[int, str], words: Words) -> list[tuple[str, str]]:
    """The notices for one batch of journal events: one per eligible
    transition, a bulk Pause or Resume coalesced into one. Resumed is a paused
    transfer seen moving again in this run, never invented."""
    shown, paused, resumed = [], [], []
    for event in events:
        transfer, kind = event.get("transfer_id"), event.get("type")
        before = previous.get(transfer) if transfer is not None else None
        if transfer is not None:
            previous[transfer] = kind
        name = str(event.get("name") or "")
        if kind == "transfer.paused":
            paused.append(name)
        elif kind in {"transfer.queued", "transfer.transferring"} and before == "transfer.paused":
            resumed.append(name)
        elif kind in LIFECYCLE_NOTICES:
            shown.append((words(LIFECYCLE_NOTICES[kind]), name))
    for names, single, many in ((paused, "transfer.action.paused_toast", "desktop.notice.paused_many"),
                                (resumed, "transfer.action.resumed_toast", "desktop.notice.resumed_many")):
        if len(names) == 1:
            shown.append((words(single), names[0]))
        elif names:
            shown.append((words(many, count=len(names)), ""))
    return shown


def tray_menu(status: dict | None, words: Words) -> dict:
    line = words("desktop.tray.status", active=int((status or {}).get("active", 0)),
                 queued=int((status or {}).get("queued", 0)))
    paused = bool((status or {}).get("paused"))
    return {"icon": "/resources/icons/tray.png", "menuItems": [
        {"id": "status", "text": line, "isDisabled": True},
        {"text": "-"},
        {"id": "open", "text": words("desktop.tray.open")},
        {"id": "resume" if paused else "pause", "text": words("processing.resume_all" if paused
                                                               else "processing.pause_all")},
        {"text": "-"},
        {"id": "quit", "text": words("desktop.tray.quit")},
    ]}


async def run() -> int:
    paths = layout()
    root = data_root()
    lock = single_instance(root)
    if lock is None:
        logger.error("DebridPulse is already running for this account")
        return 3
    sys.path.insert(0, str(paths["backend"]))
    os.environ["STATIC_DIR"] = str(paths["static"])
    from executors.process_ownership import ProcessOwnership

    import aiohttp

    db_path, config_path = root / "data" / "debridpulse.db", root / "config" / "config.json"
    for directory in (db_path.parent, config_path.parent):
        directory.mkdir(parents=True, exist_ok=True)
    processes = ProcessOwnership(root / "desktop-runtime")
    port = free_port()
    channel = Channel()
    request_end, answer_end = channel.child_ends()
    env = processes.environment({
        "DB_PATH": str(db_path), "CONFIG_PATH": str(config_path), "STATIC_DIR": str(paths["static"]),
        "DEBRIDPULSE_DESKTOP": "1", "DEBRIDPULSE_DESKTOP_CONTROL": f"{request_end},{answer_end}",
        # The bundled tools, and the packaged rsync helper's own directory
        # (its MSYS runtime layout on Windows), before the system's.
        "PATH": os.pathsep.join([*(str(path) for path in (paths["tools"], paths["tools"] / "rsync" / "usr" / "bin")
                                   if path.is_dir()), os.environ.get("PATH", "")]),
        "HOME" if os.name != "nt" else "USERPROFILE": str(Path.home()),
    })
    for key in ("APPDATA", "LOCALAPPDATA", "TEMP", "TMP") if os.name == "nt" else ("TMPDIR", "XDG_DATA_HOME"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    argv = [sys.executable, "-I", "-m", "uvicorn", "main:app", "--app-dir", str(paths["backend"]),
            "--host", "127.0.0.1", "--port", str(port), "--workers", "1", "--no-access-log"]
    for stale in (BACKEND_ID, SHELL_ID):   # a previous run that crashed: its groups end before ours start
        if processes.alive(stale):
            await processes.terminate(None, stale, grace=10)
        processes.forget(stale)
    backend = await processes.spawn(BACKEND_ID, argv, env=env, pass_fds=(request_end, answer_end))
    channel.started()
    drains = [asyncio.ensure_future(_drain(stream, root / "logs" / "backend.log"))
              for stream in (backend.process.stdout, backend.process.stderr)]
    shell = None
    try:
        async with aiohttp.ClientSession() as session:
            await _ready(session, port, backend)
            words = Words(config_path)
            auth_file = shell_data_home() / APP_ID / ".tmp" / "auth_info.json"
            private_directory(auth_file.parent)   # before the shell writes its credential; or no shell
            auth_file.unlink(missing_ok=True)
            # The shell runs under DebridPulse's own name (its webview keys its
            # data on it), and on Windows keeps its WebView2 profile inside
            # DebridPulse's data root: Microsoft's documented override, not
            # Neutralino's default %APPDATA%\<executable name>.
            shell_binary = paths["shell"] / ("debridpulse-shell.exe" if os.name == "nt" else "debridpulse-shell")
            shell_env = {**os.environ, "PATH": os.environ.get("PATH", "")}
            if os.name == "nt":
                shell_env["WEBVIEW2_USER_DATA_FOLDER"] = str(root / "webview2")
            shell_process = await processes.spawn(SHELL_ID, [
                str(shell_binary), f"--path={paths['shell']}", "--load-dir-res",
                f"--url=http://127.0.0.1:{port}/"], env=shell_env)
            drains += [asyncio.ensure_future(_drain(stream, root / "logs" / "shell.log"))
                       for stream in (shell_process.process.stdout, shell_process.process.stderr)]
            shell = Shell(session)
            await shell.connect(auth_file, time.monotonic() + READY_SECONDS)
            return await _serve(shell, channel, words, backend)
    finally:
        channel.close()
        if not await _ended(backend, QUIT_SECONDS):
            await processes.terminate(backend, BACKEND_ID, grace=5)
        processes.forget(BACKEND_ID)
        if processes.alive(SHELL_ID):
            await processes.terminate(None, SHELL_ID, grace=5)
        processes.forget(SHELL_ID)
        for task in drains:
            task.cancel()
        lock.close()


async def _serve(shell: Shell, channel: Channel, words: Words, backend) -> int:
    cursor = (await channel.ask("events"))["cursor"]
    status = (await channel.ask("status")).get("status")
    await shell.call("os.setTray", tray_menu(status, words))
    previous: dict[int, str] = {}
    next_poll = time.monotonic() + POLL_SECONDS
    while backend.process.returncode is None:
        try:
            event, data = await asyncio.wait_for(shell.events.get(), max(0.1, next_poll - time.monotonic()))
        except TimeoutError:
            event, data = None, None
        if event == "windowClose":
            await shell.call("window.hide")
        elif event == "trayMenuItemClicked":
            chosen = (data or {}).get("id")
            if chosen == "open":
                await shell.call("window.show")
                await shell.call("window.unminimize")
                await shell.call("window.focus")
            elif chosen in {"pause", "resume"}:
                await channel.ask(chosen)
                next_poll = 0
            elif chosen == "quit":
                return 0
        elif event == "shellClosed":
            return 0
        if time.monotonic() >= next_poll:
            batch = await channel.ask("events", after=cursor)
            cursor = batch.get("cursor", cursor)
            for title, body in notices(batch.get("events") or [], previous, words):
                await shell.call("os.showNotification", {"title": title, "content": body or title})
            current = (await channel.ask("status")).get("status")
            if current != status:
                status = current
                await shell.call("os.setTray", tray_menu(status, words))
            next_poll = time.monotonic() + POLL_SECONDS
    return 1   # the backend ended on its own


async def _ready(session, port: int, backend) -> None:
    deadline = time.monotonic() + READY_SECONDS
    while time.monotonic() < deadline:
        if backend.process.returncode is not None:
            raise RuntimeError(f"the DebridPulse backend ended during start ({backend.process.returncode})")
        try:
            async with session.get(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, asyncio.TimeoutError, Exception):  # noqa: BLE001 -- not listening yet
            pass
        await asyncio.sleep(0.25)
    raise TimeoutError("the DebridPulse backend did not become ready")


async def _ended(owned, seconds: float) -> bool:
    try:
        await asyncio.wait_for(owned.process.wait(), seconds)
        return True
    except TimeoutError:
        return False


async def _drain(stream, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as log:
        while chunk := await stream.read(65536):
            log.write(chunk)
            log.flush()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
