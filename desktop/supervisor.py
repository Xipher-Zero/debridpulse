"""The DownloadPulse desktop supervisor: the thin native shim around the one
DownloadPulse backend of an installed desktop application.

It owns, and only owns, what a desktop process needs and DownloadPulse does not:

* the per-user data root (``data_root``) the backend's ``DB_PATH`` and
  ``CONFIG_PATH`` name, and one supervisor per data root (an exclusive lock);
* the backend process: started through DownloadPulse's own process ownership
  (``executors.process_ownership``, the WS2 owner) on a free loopback port, in
  desktop mode (``DOWNLOADPULSE_DESKTOP=1``), with the backend's private control
  channel (``application.desktop_control``): two anonymous pipes only the two
  processes hold;
* the native window, tray and notices: a Neutralino process whose page is the
  backend's own loopback interface, with no native token or library injected
  into it; the supervisor alone holds Neutralino's exported credential and
  calls its allow-listed native methods. On Linux the tray and the notices are
  the desktop's own session-bus interfaces instead (``SessionBus``), and so is
  opening the running instance again;
* Quit: the native shell ends first, in order (``_end_shell``: its own
  ``app.exit`` on macOS, SIGTERM on Linux, its job on Windows; a stuck shell
  is stopped by the process owner), and its window and every connection its
  page holds go with it; then
  closing the control channel makes the backend stop itself through its own
  shutdown, bounded by uvicorn's graceful-shutdown timeout for any other open
  request; only a backend that does not end in time is ended forcibly (its
  process group or job), the crash backstop, and the log says so.

It reads no attempt marker, keeps no transfer state of its own, and calls no
DownloadPulse HTTP API but the public health check: the tray's status, events
and Pause/Resume All travel on the private channel to their canonical owners.
Nothing it does signs a session in or unlocks the interface. What the native
side refuses or fails to do is logged (``logs/desktop.log``), never assumed
done.
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
APP_ID = "net.downloadpulse.desktop"
BACKEND_ID = "desktop-backend"
SHELL_ID = "desktop-shell"
READY_SECONDS = 120
QUIT_SECONDS = 60
SHELL_EXIT_SECONDS = 10
# How long the backend's shutdown waits for a request still open (an event
# stream of a page that is not the shell's) before uvicorn cancels it and goes
# on with DownloadPulse's own shutdown.
GRACEFUL_SECONDS = 5
POLL_SECONDS = 3.0
# How soon a connected browser's Open DownloadPulse brings the window forward:
# the backend's ``show`` is asked this often (one short line each way).
SHOW_POLL_SECONDS = 0.5
NATIVE_METHODS = ("os.setTray", "os.showNotification", "window.show", "window.hide", "window.focus",
                  "window.unminimize", "window.minimize", "window.setMainMenu", "os.showSaveDialog", "app.exit",
                  "window.setFrameTheme")
# How long a Save dialog may stay open: the person's own time.
DIALOG_SECONDS = 600
logger = logging.getLogger("downloadpulse.desktop.supervisor")


# ── layout ─────────────────────────────────────────────────────────────────

def layout() -> dict[str, Path]:
    """Where the bundled pieces are, relative to this file: the application
    (``backend``, ``frontend/static``), the bundled tools (``tools``) and the
    native shell (``shell``). ``DOWNLOADPULSE_DESKTOP_LAYOUT`` names another
    root for a development run."""
    root = Path(os.environ.get("DOWNLOADPULSE_DESKTOP_LAYOUT") or HERE.parent).resolve()
    return {"root": root, "backend": root / "backend", "static": root / "frontend" / "static",
            "tools": root / "tools", "shell": root / "desktop" / "shell"}


def data_root() -> Path:
    """The per-user application data root, outside the program files:
    Windows LocalAppData, macOS Application Support, XDG data (Flatpak's own
    writable data) elsewhere."""
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "DownloadPulse"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "DownloadPulse"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "downloadpulse"


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
    """The tray's and notices' words, from DownloadPulse's own catalogs: at
    start the installation's default language (English when none is chosen),
    then the language the interface is shown in (op ``locale``), followed
    while the application runs (``choose``)."""

    def __init__(self, config_path: Path):
        from core import localization
        self._message = localization.message
        self._tags = localization.locale_tags()
        try:
            tag = json.loads(config_path.read_text(encoding="utf-8")).get("default_locale")
        except (OSError, ValueError):
            tag = None
        self.tag = tag if tag in self._tags else localization.DEFAULT_LOCALE

    def choose(self, tag) -> bool:
        """Show the words in ``tag`` from now on; True when that is a change
        (an unknown tag changes nothing)."""
        if tag not in self._tags or tag == self.tag:
            return False
        self.tag = tag
        return True

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

    async def call(self, method: str, data: dict | list | None = None, timeout: float = 15):
        """One native call's answer; ``NativeError`` when Neutralino says it
        failed (its answer's ``error``), never taken for done."""
        if method not in NATIVE_METHODS:
            raise PermissionError(method)
        self._next += 1
        ident = f"dp-{self._next}"
        self._pending[ident] = asyncio.get_running_loop().create_future()
        await self.ws.send_str(json.dumps({"id": ident, "method": method, "accessToken": self.token,
                                           "data": data or {}}))
        answer = await asyncio.wait_for(self._pending[ident], timeout)
        if isinstance(answer, dict) and answer.get("error"):
            raise NativeError(method, answer["error"])
        return answer

    # Where the tray and notices are shown, everywhere but Linux (SessionBus).
    tray_shown = False
    # The shell's own executable (run()), whose windows get the frame on Windows.
    binary = ""

    async def tray(self, menu: dict) -> None:
        try:
            await self.call("os.setTray", menu)
            self.tray_shown = True
        except NativeError as exc:
            if self.tray_shown or not getattr(self, "_tray_reported", False):
                logger.error("the tray could not be shown: %s", exc)
            self._tray_reported, self.tray_shown = True, False

    async def notice(self, title: str, body: str) -> None:
        try:
            await self.call("os.showNotification", {"title": title, "content": body or title})
        except NativeError as exc:
            logger.error("a notice was not shown: %s", exc)

    async def save_destination(self, name: str, title: str) -> str | None:
        """Where the person saves ``name``: the platform's own Save dialog
        (Neutralino's, everywhere but Linux), starting in their Downloads
        folder; None when they cancel."""
        downloads = Path.home() / "Downloads"
        start = downloads / name if downloads.is_dir() else Path(name)
        try:
            chosen = await self.call("os.showSaveDialog", {"title": title, "defaultPath": str(start)},
                                     timeout=DIALOG_SECONDS)
        except (NativeError, TimeoutError) as exc:
            logger.error("the Save dialog failed: %s", exc)
            return None
        # Neutralino answers {"returnValue": <path, "" when cancelled>, "success": true}
        # (api/os/os.cpp showSaveDialog), never the bare path.
        if isinstance(chosen, dict):
            chosen = chosen.get("returnValue")
        return str(chosen) if isinstance(chosen, str) and chosen else None


    async def exit(self) -> None:
        """Neutralino's own exit (macOS, ``_end_shell``); it answers nothing,
        so only the process ending says it happened."""
        if self.ws is not None and not self.ws.closed:
            self._next += 1
            await self.ws.send_str(json.dumps({"id": f"dp-{self._next}", "method": "app.exit",
                                               "accessToken": self.token, "data": {}}))


class NativeError(RuntimeError):
    def __init__(self, method: str, error):
        detail = error.get("message") or error.get("code") if isinstance(error, dict) else error
        super().__init__(f"{method}: {detail}")


# ── Linux: the tray, notices and activation on the session bus ─────────────

class SessionBus:
    """Linux: the tray, the notices and opening the running instance, through
    the desktop's own standard interfaces on the session bus. Neutralino's
    tray needs an AppIndicator library no Flatpak runtime provides (and fails
    without it), and its notices go to a helper that can report success
    without showing anything; these do not depend on either.

    * The tray is a StatusNotifierItem (``/StatusNotifierItem``) with its
      ``com.canonical.dbusmenu`` menu (``/MenuBar``), registered with
      ``org.kde.StatusNotifierWatcher`` -- again whenever a watcher appears --
      showing the favicon-derived pixmaps; its menu is the supervisor's own
      (``tray_menu``). It is shown (``tray_shown``) only while the watcher
      accepted it and says a host displays items.
    * A notice is ``org.freedesktop.portal.Notification.AddNotification``.
    * The running instance owns its application ID on the bus and answers
      ``org.freedesktop.Application.Activate`` (``activate_running``): starting
      DownloadPulse again opens its window.

    A menu click, the item's own activation and Activate become the shell's
    events (``trayMenuItemClicked``), handled where every click is. Nothing
    here talks to the backend.

    **The native control boundary.** Anyone on the session bus may read the
    item and its menu and may open the window (the menu's Open, the item's
    activation, Activate: the window, password-locked or not, is no action).
    The actions -- Pause All, Resume All, Quit -- are taken only from the tray
    host: a caller that owns an ``org.kde.StatusNotifierHost-*`` name (the
    name every StatusNotifier host registers under: Plasma, waybar, ...) or
    the watcher itself (a host that is its own watcher, as GNOME's
    AppIndicator extension is). Anyone else is refused with AccessDenied
    before anything reaches the backend's channel -- a peer inside this
    sandbox above all (a confined Media Downloads helper can reach the bus,
    but xdg-dbus-proxy lets nothing in the sandbox own such a name). The
    Flatpak may *see* (never talk to) the ``org.kde.*`` names for this check.
    An unconfined process of the same account could still register a host
    name of its own; it could equally read this account's DownloadPulse files,
    so that is not a boundary the bus could add.

    **Availability.** The tray is shown (``tray_shown``) only while a watcher
    holds the item and says a host is registered; the watcher leaving, or its
    last host, takes it away at once, and it is registered again as soon as a
    watcher or host returns. Each change is the shell's event
    (``trayAvailability``), so the window is never left hidden where nothing
    can open it."""

    # The menu entries that act, and so are the tray host's alone.
    ACTIONS = frozenset({"pause", "resume", "quit"})
    HOSTS = "org.kde.StatusNotifierHost-"

    ITEM, MENU = "/StatusNotifierItem", "/MenuBar"
    APPLICATION = "/" + APP_ID.replace(".", "/")
    WATCHER = ("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher")
    PORTAL = ("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
              "org.freedesktop.portal.Notification")
    CHOOSER = "org.freedesktop.portal.FileChooser"
    SIZES = (22, 32, 48, 64)

    def __init__(self, router, events: asyncio.Queue, pixmaps: list):
        self.router, self.events, self.pixmaps = router, events, pixmaps
        self.items: list[dict] = []
        self.revision, self.notices_sent, self.tray_shown = 0, 0, False
        self.status_line = ""
        self.tasks: list[asyncio.Task] = []
        self.filters: list = []

    # -- starting and stopping --

    @classmethod
    async def open(cls, events: asyncio.Queue, icons: Path) -> SessionBus | None:
        from jeepney.io.asyncio import open_dbus_router
        try:
            context = open_dbus_router("SESSION")
            router = await context.__aenter__()
        except (OSError, KeyError, ValueError) as exc:   # no session bus at all
            logger.error("the session bus is unavailable; no tray or notices: %s", exc)
            return None
        bus = cls(router, events, cls._pixmaps(icons))
        bus.context = context
        await bus._start()
        return bus

    @classmethod
    def _pixmaps(cls, icons: Path) -> list:
        """The tray icon at each size, as StatusNotifierItem pixmaps: ARGB32 in
        network byte order (the files are the favicon's RGBA, assemble.py)."""
        pixmaps = []
        for size in cls.SIZES:
            try:
                rgba = (icons / f"tray-{size}.rgba").read_bytes()
            except OSError:
                continue
            if len(rgba) != size * size * 4:
                continue
            argb = bytearray(len(rgba))
            argb[0::4], argb[1::4], argb[2::4], argb[3::4] = rgba[3::4], rgba[0::4], rgba[1::4], rgba[2::4]
            pixmaps.append((size, size, bytes(argb)))
        if not pixmaps:
            logger.error("no tray icon pixmaps in %s", icons)
        return pixmaps

    async def _start(self) -> None:
        from jeepney import MatchRule, message_bus
        calls = self.router.filter(MatchRule(type="method_call"), bufsize=64)
        owners = self.router.filter(MatchRule(type="signal", sender="org.freedesktop.DBus",
                                              interface="org.freedesktop.DBus", member="NameOwnerChanged",
                                              path="/org/freedesktop/DBus"), bufsize=16)
        hosts = self.router.filter(MatchRule(type="signal", interface=self.WATCHER[2]), bufsize=16)
        self.filters = [calls, owners, hosts]
        self.tasks = [asyncio.ensure_future(self._answer(calls.queue)),
                      asyncio.ensure_future(self._watch(owners.queue)),
                      asyncio.ensure_future(self._hosts(hosts.queue))]
        rule = MatchRule(type="signal", sender="org.freedesktop.DBus", interface="org.freedesktop.DBus",
                         member="NameOwnerChanged", path="/org/freedesktop/DBus")
        rule.add_arg_condition(0, self.WATCHER[0])
        await self._ask(message_bus.AddMatch(rule), "watching for a tray watcher")
        await self._ask(message_bus.AddMatch(MatchRule(type="signal", sender=self.WATCHER[0],
                                                       interface=self.WATCHER[2])), "watching for tray hosts")
        owned = await self._ask(message_bus.RequestName(APP_ID, 0x4), "owning the application ID")   # DO_NOT_QUEUE
        if owned is not None and owned.body[0] not in (1, 4):
            logger.error("the application ID %s is owned by another process; DownloadPulse cannot be "
                         "opened by starting it again", APP_ID)
        await self._register()
        await self._check_portal()

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        for handle in self.filters:
            handle.close()
        try:
            await self.context.__aexit__(None, None, None)
        except (OSError, RuntimeError):
            pass

    @staticmethod
    async def activate_running() -> bool:
        """Ask the instance running for this account to open its window."""
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.asyncio import open_dbus_router
        try:
            async with open_dbus_router("SESSION") as router:
                reply = await asyncio.wait_for(router.send_and_get_reply(new_method_call(
                    DBusAddress(SessionBus.APPLICATION, bus_name=APP_ID, interface="org.freedesktop.Application"),
                    "Activate", "a{sv}", ({},))), 10)
        except (OSError, KeyError, ValueError, TimeoutError) as exc:
            logger.error("the running DownloadPulse could not be asked to open: %s", exc)
            return False
        if reply.header.message_type.name == "error":
            logger.error("the running DownloadPulse did not open: %s", reply.body)
            return False
        return True

    # -- what the supervisor shows --

    async def tray(self, menu: dict) -> None:
        self.items = [item for item in menu["menuItems"]]
        self.status_line = next((item["text"] for item in self.items if item.get("id") == "status"), "")
        self.revision += 1
        await self._signal(self.MENU, "com.canonical.dbusmenu", "LayoutUpdated", "ui", (self.revision, 0))
        await self._signal(self.ITEM, "org.kde.StatusNotifierItem", "NewToolTip", "", ())
        if not self.tray_shown:
            await self._register()

    async def notice(self, title: str, body: str) -> None:
        """One new notice. Its id is never one this application used before,
        in this run or an earlier one: the portal replaces a notice whose id
        it already holds for the application, and the desktop shows such a
        replacement quietly (Plasma: no popup) -- a per-run counter restarting
        at 1 turned every later run's notices into silent replacements."""
        from jeepney import DBusAddress, new_method_call
        self.notices_sent += 1
        name, path, interface = self.PORTAL
        reply = await self._ask(new_method_call(
            DBusAddress(path, bus_name=name, interface=interface), "AddNotification", "sa{sv}",
            (notice_id(), {"title": ("s", title), "body": ("s", body or title),
                           "priority": ("s", "normal")})), "showing a notice")
        if reply is None:
            logger.error("a notice was not shown (%s)", title)

    async def save_destination(self, name: str, title: str) -> str | None:
        """Where the person saves ``name``: the desktop's own Save dialog
        through the file chooser portal (inside the Flatpak it also grants
        this application the place chosen; Neutralino's own dialog needs a
        zenity or kdialog the sandbox has not got); None when they cancel."""
        import secrets
        from urllib.parse import unquote, urlparse
        from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
        token = "dp" + secrets.token_hex(8)
        sender = (self.router.unique_name or "").lstrip(":").replace(".", "_")
        request = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        # The answer arrives as the request's Response signal: listened for
        # before asking, so it cannot come first.
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request", member="Response", path=request)
        with self.router.filter(rule, bufsize=1) as answers:
            await self._ask(message_bus.AddMatch(rule), "listening for the Save dialog's answer")
            name_, path, _interface = self.PORTAL
            asked = await self._ask(new_method_call(
                DBusAddress(path, bus_name=name_, interface=self.CHOOSER), "SaveFile", "ssa{sv}",
                ("", title, {"handle_token": ("s", token), "current_name": ("s", name), "modal": ("b", True)})),
                "showing the Save dialog")
            if asked is None:
                return None
            try:
                answer = await asyncio.wait_for(answers.get(), DIALOG_SECONDS)
            except TimeoutError:
                logger.error("the Save dialog gave no answer in %ss", DIALOG_SECONDS)
                return None
            finally:
                await self._ask(message_bus.RemoveMatch(rule), "no longer listening for the Save dialog")
        response, results = answer.body
        uris = results.get("uris", ("as", []))[1] if response == 0 else []
        if not uris:
            return None                             # cancelled (or nothing chosen)
        location = urlparse(uris[0])
        return unquote(location.path) if location.scheme == "file" and location.path else None

    # -- the bus side --

    async def _ask(self, message, doing: str):
        """A call's reply, or None (logged) when it failed or was refused."""
        try:
            reply = await asyncio.wait_for(self.router.send_and_get_reply(message), 10)
        except (OSError, TimeoutError, RuntimeError) as exc:
            logger.error("session bus: %s failed: %s", doing, exc)
            return None
        if reply.header.message_type.name == "error":
            from jeepney.low_level import HeaderFields
            logger.error("session bus: %s was refused: %s %s", doing,
                         reply.header.fields.get(HeaderFields.error_name), reply.body)
            return None
        return reply

    async def _register(self) -> None:
        from jeepney import DBusAddress, Properties, new_method_call
        name, path, interface = self.WATCHER
        watcher = DBusAddress(path, bus_name=name, interface=interface)
        registered = await self._ask(new_method_call(watcher, "RegisterStatusNotifierItem", "s", (APP_ID,)),
                                     "registering the tray icon")
        hosted = await self._ask(Properties(watcher).get("IsStatusNotifierHostRegistered"),
                                 "asking whether a tray host is shown") if registered else None
        self._shown(bool(registered and hosted and hosted.body[0][1]))

    def _shown(self, shown: bool) -> None:
        """The tray's availability, and the shell's event when it changes."""
        if shown == self.tray_shown:
            return
        self.tray_shown = shown
        logger.info("tray icon %s", "shown" if shown else "not shown: no tray host to open the window from")
        self.events.put_nowait(("trayAvailability", {"shown": shown}))

    async def _owner(self, name: str) -> str | None:
        from jeepney import message_bus
        try:
            reply = await asyncio.wait_for(self.router.send_and_get_reply(message_bus.GetNameOwner(name)), 10)
        except (OSError, TimeoutError, RuntimeError):
            return None
        return reply.body[0] if reply.header.message_type.name != "error" else None

    async def _host(self, caller: str | None) -> bool:
        """Whether ``caller`` is the tray host (see the class's boundary)."""
        if not caller:
            return False
        from jeepney import message_bus
        listed = await self._ask(message_bus.ListNames(), "listing the tray hosts")
        names = [self.WATCHER[0], *(name for name in (listed.body[0] if listed else ())
                                    if name.startswith(self.HOSTS))]
        for name in names:
            if await self._owner(name) == caller:
                return True
        return False

    async def _check_portal(self) -> None:
        from jeepney import DBusAddress, Properties
        name, path, interface = self.PORTAL
        version = await self._ask(Properties(DBusAddress(path, bus_name=name, interface=interface)).get("version"),
                                  "finding the notification portal")
        if version is not None:
            logger.info("notification portal version %s", version.body[0][1])

    async def _signal(self, path: str, interface: str, member: str, signature: str, body: tuple) -> None:
        from jeepney import DBusAddress, new_signal
        try:
            await self.router.send(new_signal(DBusAddress(path, interface=interface), member,
                                              signature or None, body))
        except OSError as exc:
            logger.error("session bus: %s.%s not sent: %s", interface, member, exc)

    async def _watch(self, queue: asyncio.Queue) -> None:
        while True:
            message = await queue.get()
            watched, _old, new = message.body
            if watched != self.WATCHER[0]:
                continue
            if new:
                logger.info("a tray watcher appeared")
                await self._register()
            else:
                logger.warning("the tray watcher went away")
                self._shown(False)

    async def _hosts(self, queue: asyncio.Queue) -> None:
        """A host registered or gone (the watcher's own signals): asked anew."""
        from jeepney.low_level import HeaderFields
        while True:
            message = await queue.get()
            if message.header.fields.get(HeaderFields.member) in {"StatusNotifierHostRegistered",
                                                                   "StatusNotifierHostUnregistered"}:
                await self._register()

    async def _answer(self, queue: asyncio.Queue) -> None:
        from jeepney import new_error, new_method_return
        from jeepney.low_level import HeaderFields
        while True:
            message = await queue.get()
            fields = message.header.fields
            path, interface, member = (fields.get(HeaderFields.path), fields.get(HeaderFields.interface),
                                       fields.get(HeaderFields.member))
            caller = fields.get(HeaderFields.sender)
            try:
                answer = await self._method(caller, path, interface, member, message.body)
            except PermissionError as refusal:
                logger.warning("refused %s from %s: not the tray host", refusal, caller)
                reply = new_error(message, "org.freedesktop.DBus.Error.AccessDenied", "s",
                                  (f"{refusal} is the tray host's",))
            except LookupError:
                reply = new_error(message, "org.freedesktop.DBus.Error.UnknownMethod", "s",
                                  (f"{interface}.{member} on {path}",))
            else:
                reply = new_method_return(message, *answer) if answer else new_method_return(message)
            try:
                await self.router.send(reply)
            except OSError as exc:
                logger.error("session bus: no reply sent to %s.%s: %s", interface, member, exc)

    async def _method(self, caller, path, interface, member, body):
        """(signature, body) of the reply, () for none; LookupError: not ours;
        PermissionError: an action from someone other than the tray host."""
        if interface == "org.freedesktop.DBus.Introspectable" and member == "Introspect":
            return "s", (self._introspection(path),)
        if interface == "org.freedesktop.DBus.Peer" and member == "Ping":
            return ()
        if interface == "org.freedesktop.DBus.Properties" and member in {"Get", "GetAll"}:
            properties = self._properties(path, body[0])
            if member == "GetAll":
                return "a{sv}", (properties,)
            if body[1] not in properties:
                raise LookupError(body[1])
            return "v", (properties[body[1]],)
        if path == self.ITEM and interface == "org.kde.StatusNotifierItem":
            if member in {"Activate", "SecondaryActivate"}:
                await self.events.put(("trayMenuItemClicked", {"id": "open"}))
            elif member not in {"ContextMenu", "Scroll", "ProvideXdgActivationToken"}:
                raise LookupError(member)
            return ()
        if path == self.MENU and interface == "com.canonical.dbusmenu":
            return await self._menu(caller, member, body)
        if path == self.APPLICATION and interface == "org.freedesktop.Application" \
                and member in {"Activate", "Open", "ActivateAction"}:
            await self.events.put(("trayMenuItemClicked", {"id": "open"}))
            return ()
        raise LookupError(member)

    async def _menu(self, caller, member, body):
        entries = {index: item for index, item in enumerate(self.items, 1)}
        if member == "GetLayout":
            children = [("(ia{sv}av)", (index, self._entry(item), [])) for index, item in entries.items()]
            return "u(ia{sv}av)", (self.revision, (0, {"children-display": ("s", "submenu")}, children))
        if member == "GetGroupProperties":
            wanted = body[0] or list(entries)
            return "a(ia{sv})", ([(index, self._entry(entries[index])) for index in wanted if index in entries],)
        if member == "GetProperty":
            entry = self._entry(entries[body[0]]) if body[0] in entries else {}
            if body[1] not in entry:
                raise LookupError(body[1])
            return "v", (entry[body[1]],)
        if member in {"Event", "EventGroup"}:
            unknown, chosen = [], []
            for index, event, _data, _time in ([body] if member == "Event" else body[0]):
                item = entries.get(index)
                if item is None:
                    unknown.append(index)
                elif event == "clicked" and item.get("id") and not item.get("isDisabled"):
                    chosen.append(item["id"])
            # The boundary: an action is the tray host's, checked before any of
            # the group is taken.
            refused = [choice for choice in chosen if choice in self.ACTIONS]
            if refused and not await self._host(caller):
                raise PermissionError(", ".join(refused))
            for choice in chosen:
                await self.events.put(("trayMenuItemClicked", {"id": choice}))
            return ("ai", (unknown,)) if member == "EventGroup" else ()
        if member == "AboutToShow":
            return "b", (False,)
        if member == "AboutToShowGroup":
            return "aiai", ([], [])
        raise LookupError(member)

    @staticmethod
    def _entry(item: dict) -> dict:
        if item.get("text") == "-":
            return {"type": ("s", "separator")}
        return {"label": ("s", item.get("text", "")), "enabled": ("b", not item.get("isDisabled"))}

    def _properties(self, path: str, interface: str) -> dict:
        if path == self.ITEM and interface in {"org.kde.StatusNotifierItem", ""}:
            return {"Category": ("s", "ApplicationStatus"), "Id": ("s", APP_ID), "Title": ("s", "DownloadPulse"),
                    "Status": ("s", "Active"), "WindowId": ("i", 0), "IconName": ("s", ""),
                    "IconPixmap": ("a(iiay)", self.pixmaps), "IconThemePath": ("s", ""),
                    "OverlayIconName": ("s", ""), "OverlayIconPixmap": ("a(iiay)", []),
                    "AttentionIconName": ("s", ""), "AttentionIconPixmap": ("a(iiay)", []),
                    "AttentionMovieName": ("s", ""),
                    "ToolTip": ("(sa(iiay)ss)", ("", [], "DownloadPulse", self.status_line)),
                    "ItemIsMenu": ("b", False), "Menu": ("o", self.MENU)}
        if path == self.MENU and interface in {"com.canonical.dbusmenu", ""}:
            return {"Version": ("u", 3), "TextDirection": ("s", "ltr"), "Status": ("s", "normal"),
                    "IconThemePath": ("as", [])}
        raise LookupError(interface)

    def _introspection(self, path: str) -> str:
        interfaces = {self.ITEM: ["org.kde.StatusNotifierItem"], self.MENU: ["com.canonical.dbusmenu"],
                      self.APPLICATION: ["org.freedesktop.Application"]}.get(path, [])
        children = sorted({part.split("/")[1] for part in (self.ITEM, self.MENU, self.APPLICATION)}) \
            if path == "/" else []
        standard = ["org.freedesktop.DBus.Introspectable", "org.freedesktop.DBus.Peer",
                    "org.freedesktop.DBus.Properties"] if interfaces else ["org.freedesktop.DBus.Introspectable"]
        return ("<node>" + "".join(f'<interface name="{name}"/>' for name in standard + interfaces)
                + "".join(f'<node name="{child}"/>' for child in children) + "</node>")


# ── the run ────────────────────────────────────────────────────────────────

LIFECYCLE_NOTICES = {"transfer.accepted": "desktop.notice.accepted", "transfer.completed": "desktop.notice.completed",
                     "transfer.failed": "error.category.transfer_failed",
                     "transfer.cancelled": "file_selection.transfer_cancelled",
                     "transfer.paused": "transfer.action.paused_toast"}


def shell_environment(root: Path, environ=None, platform: str = sys.platform) -> dict[str, str]:
    """The native shell's environment: this process's own, plus
      - Windows: its WebView2 profile inside DownloadPulse's data root
        (Microsoft's documented override, not Neutralino's default
        %APPDATA%\\<executable name>);
      - Linux: WebKitGTK's TextureMapper compositor rather than Skia, which
        WebKitGTK 2.54 made the default. Composited with Skia, the window
        drew stale and blank frames as a page change ended (measured in the
        GNOME 49 runtime's WebKitGTK 2.54.1 on KDE Wayland; compositing stays
        on). Where the person running DownloadPulse has set it, their value
        stands."""
    environ = os.environ if environ is None else environ
    env = {**environ, "PATH": environ.get("PATH", "")}
    if platform == "win32":
        env["WEBVIEW2_USER_DATA_FOLDER"] = str(root / "webview2")
    elif platform.startswith("linux"):
        env.setdefault("WEBKIT_USE_SKIA_FOR_COMPOSITION", "0")
    return env


def notice_id() -> str:
    """A notice's portal id, unique across runs (SessionBus.notice): a random
    128-bit value, so no notice ever replaces an earlier one."""
    import uuid
    return f"notice-{uuid.uuid4().hex}"


def notices(events: list[dict], words: Words) -> list[tuple[str, str]]:
    """The notices for one batch of journal events: one per eligible
    transition, a bulk Pause or Resume coalesced into one. Resumed is what the
    journal itself says resumed (a transition out of Paused, ``outcome``
    ``resumed``), never inferred here."""
    shown, paused, resumed = [], [], []
    for event in events:
        kind, name = event.get("type"), str(event.get("name") or "")
        if kind == "transfer.paused":
            paused.append(name)
        elif event.get("outcome") == "resumed":
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


def local_port_notice(status: dict | None, words: Words) -> tuple[str, str] | None:
    """The one notice of a run whose Local Port is not listening (another
    program holds it): external clients cannot reach DownloadPulse until it is
    free or changed in Settings. None while it listens."""
    local = (status or {}).get("local_port") or {}
    if local.get("listening") is not False:
        return None
    return words("desktop.local_port.unavailable_title"), words("desktop.local_port.unavailable", port=str(int(local.get("port") or 0)))


def tray_menu(status: dict | None, words: Words, platform: str = sys.platform) -> dict:
    """The tray: its icon (macOS: a template image the menu bar tints for its
    appearance), the status line, Open, the one of Pause All / Resume All
    that applies, Quit."""
    line = words("desktop.tray.status", active=int((status or {}).get("active", 0)),
                 queued=int((status or {}).get("queued", 0)))
    paused = bool((status or {}).get("paused"))
    icon = {"icon": "/resources/icons/trayTemplate.png", "useTemplateIcon": True} if platform == "darwin" \
        else {"icon": "/resources/icons/tray.png"}
    return {**icon, "menuItems": [
        {"id": "status", "text": line, "isDisabled": True},
        {"text": "-"},
        {"id": "open", "text": words("desktop.tray.open")},
        {"id": "resume" if paused else "pause", "text": words("processing.resume_all" if paused
                                                               else "processing.pause_all")},
        {"text": "-"},
        {"id": "quit", "text": words("desktop.tray.quit")},
    ]}


def main_menu(words: Words) -> list:
    """macOS: the application menu (Neutralino's ``window.setMainMenu`` takes
    the menu list itself), with Quit on Cmd+Q, chosen as the loop's own event
    (``mainMenuItemClicked``, an item without a native ``action``), never
    NSApp's termination."""
    return [{"id": "app", "text": "DownloadPulse", "menuItems": [
        {"id": "quit", "text": words("desktop.tray.quit"), "shortcut": "q"}]}]


def open_methods(*, hidden: bool, minimized: bool) -> tuple[str, ...]:
    """Open's native calls for the window as it is. Hidden (closed to the
    tray): shown again and raised. Minimized: unminimized and raised -- on
    Wayland a client cannot unminimize itself, so the compositor decides and
    the taskbar always restores it. Shown: raised."""
    if hidden:
        return ("window.show", "window.focus")
    if minimized:
        return ("window.unminimize", "window.focus")
    return ("window.focus",)


def backend_argv(paths: dict[str, Path], port: int) -> list[str]:
    return [sys.executable, "-I", "-m", "uvicorn", "main:app", "--app-dir", str(paths["backend"]),
            "--host", "127.0.0.1", "--port", str(port), "--workers", "1", "--no-access-log",
            "--timeout-graceful-shutdown", str(GRACEFUL_SECONDS)]


async def run() -> int:
    paths = layout()
    root = data_root()
    lock = single_instance(root)
    if lock is None:
        if sys.platform.startswith("linux") and await SessionBus.activate_running():
            return 0      # the running instance opens its window
        logger.error("DownloadPulse is already running for this account")
        return 3
    (root / "logs").mkdir(parents=True, exist_ok=True)
    diagnostics = logging.FileHandler(root / "logs" / "desktop.log", encoding="utf-8")
    diagnostics.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(diagnostics)
    sys.path.insert(0, str(paths["backend"]))
    os.environ["STATIC_DIR"] = str(paths["static"])
    import aiohttp
    from executors.process_ownership import ProcessOwnership

    db_path, config_path = root / "data" / "downloadpulse.db", root / "config" / "config.json"
    for directory in (db_path.parent, config_path.parent):
        directory.mkdir(parents=True, exist_ok=True)
    processes = ProcessOwnership(root / "desktop-runtime")
    port = free_port()
    channel = Channel()
    request_end, answer_end = channel.child_ends()
    env = processes.environment({
        "DB_PATH": str(db_path), "CONFIG_PATH": str(config_path), "STATIC_DIR": str(paths["static"]),
        "DOWNLOADPULSE_DESKTOP": "1", "DOWNLOADPULSE_DESKTOP_CONTROL": f"{request_end},{answer_end}",
        # The window's own port, which Local Port relays to (application.desktop_listener).
        "DOWNLOADPULSE_DESKTOP_PORT": str(port),
        # The bundled tools, and the packaged rsync helper's own directory
        # (its MSYS runtime layout on Windows), before the system's.
        "PATH": os.pathsep.join([*(str(path) for path in (paths["tools"], paths["tools"] / "rsync" / "usr" / "bin")
                                   if path.is_dir()), os.environ.get("PATH", "")]),
        "HOME" if os.name != "nt" else "USERPROFILE": str(Path.home()),
    })
    for key in ("APPDATA", "LOCALAPPDATA", "TEMP", "TMP") if os.name == "nt" \
            else ("TMPDIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    for stale in (BACKEND_ID, SHELL_ID):   # a previous run that crashed: its groups end before ours start
        if processes.alive(stale):
            await processes.terminate(None, stale, grace=10)
        processes.forget(stale)
    backend = await processes.spawn(BACKEND_ID, backend_argv(paths, port), env=env,
                                    pass_fds=(request_end, answer_end))
    channel.started()
    drains = [asyncio.ensure_future(_drain(stream, root / "logs" / "backend.log"))
              for stream in (backend.process.stdout, backend.process.stderr)]
    shell = shell_process = bus = None
    try:
        async with aiohttp.ClientSession() as session:
            await _ready(session, port, backend)
            words = Words(config_path)
            auth_file = shell_data_home() / APP_ID / ".tmp" / "auth_info.json"
            private_directory(auth_file.parent)   # before the shell writes its credential; or no shell
            auth_file.unlink(missing_ok=True)
            # The shell runs under DownloadPulse's own name (its webview keys its
            # data on it), in the environment shell_environment() gives it.
            shell_binary = paths["shell"] / ("downloadpulse-shell.exe" if os.name == "nt" else "downloadpulse-shell")
            shell_process = await processes.spawn(SHELL_ID, [
                str(shell_binary), f"--path={paths['shell']}", "--load-dir-res",
                f"--url=http://127.0.0.1:{port}/"], env=shell_environment(root))
            drains += [asyncio.ensure_future(_drain(stream, root / "logs" / "shell.log"))
                       for stream in (shell_process.process.stdout, shell_process.process.stderr)]
            shell = Shell(session)
            shell.binary = str(shell_binary)
            await shell.connect(auth_file, time.monotonic() + READY_SECONDS)
            if sys.platform.startswith("linux"):
                bus = await SessionBus.open(shell.events, paths["shell"] / "resources" / "icons")
            try:
                return await _serve(shell, channel, words, backend, bus or shell)
            finally:
                # Quit, in order: the shell and its page first, so no request
                # of the page is left open to hold the backend's shutdown.
                await _end_shell(shell, shell_process, processes)
    finally:
        if bus is not None:
            await bus.close()
        channel.close()
        if not await _ended(backend, QUIT_SECONDS):
            logger.error("the DownloadPulse backend did not stop in %ss; ending it forcibly", QUIT_SECONDS)
            await processes.terminate(backend, BACKEND_ID, grace=5)
        logger.info("the DownloadPulse backend ended %s (%s)", "through its own shutdown"
                    if backend.process.returncode == 0 else "abnormally", backend.process.returncode)
        processes.forget(BACKEND_ID)
        if processes.alive(SHELL_ID):
            await processes.terminate(None, SHELL_ID, grace=5)
        processes.forget(SHELL_ID)
        for task in drains:
            task.cancel()
        lock.close()


async def _serve(shell: Shell, channel: Channel, words: Words, backend, native) -> int:
    """One run's loop. ``native`` shows the tray and the notices (the shell,
    or Linux's ``SessionBus``); the window is always the shell's."""
    cursor = (await channel.ask("events"))["cursor"]
    status = (await channel.ask("status")).get("status")
    words.choose((await channel.ask("locale")).get("locale"))       # the interface's language from the first draw
    await native.tray(tray_menu(status, words))
    # Local Port taken at launch: said once, as the window opens.
    if (unavailable := local_port_notice(status, words)) is not None:
        await native.notice(*unavailable)
    if sys.platform == "darwin":
        # The application menu: its Quit (Cmd+Q) is this loop's Quit, never
        # the shell's own termination (_end_shell).
        await _window(shell, "window.setMainMenu", main_menu(words))
    hidden = False                              # this loop hid the window (close-to-tray)
    applied_frame = None                        # the frame last given to the window (apply_frame)
    minimized = False                           # the shell says it is minimized (its own events)
    next_poll = time.monotonic() + POLL_SECONDS
    next_show = time.monotonic() + SHOW_POLL_SECONDS

    async def bring_forward(why: str) -> None:
        # What Open can do depends on what the window is: a window this
        # loop hid is shown again (the shell maps it); a minimized one
        # is asked to come back, which on Wayland is the compositor's
        # to grant (the taskbar always can); a shown one is raised.
        nonlocal hidden
        methods = open_methods(hidden=hidden, minimized=minimized)
        logger.info("%s: the window was %s; %s", why, "hidden" if hidden else "minimized" if minimized
                    else "shown", ", ".join(methods))
        hidden = False
        for method in methods:
            await _window(shell, method)

    while backend.process.returncode is None:
        try:
            event, data = await asyncio.wait_for(shell.events.get(),
                                                 max(0.1, min(next_poll, next_show) - time.monotonic()))
        except TimeoutError:
            event, data = None, None
        if event in {"windowMinimize", "windowRestore"}:
            minimized = event == "windowMinimize"
        elif event == "windowClose":
            # Hidden only where the tray can open it again; otherwise the
            # window stays one click away (and DownloadPulse keeps running).
            hidden = native.tray_shown
            await _window(shell, "window.hide" if hidden else "window.minimize")
            minimized = minimized or not hidden
        elif event == "trayAvailability" and not (data or {}).get("shown") and hidden:
            # The tray went away while it was the only way back: the window
            # returns, minimized, where the desktop can still open it.
            logger.warning("the tray went away while the window was hidden; showing it minimized")
            hidden, minimized = False, True
            for method in ("window.show", "window.minimize"):
                await _window(shell, method)
        elif event == "mainMenuItemClicked" and (data or {}).get("id") == "quit":
            logger.info("Quit (the application menu, Cmd+Q)")
            return 0
        elif event == "trayMenuItemClicked":
            chosen = (data or {}).get("id")
            if chosen == "open":
                await bring_forward("Open")
            elif chosen in {"pause", "resume"}:
                await channel.ask(chosen)
                next_poll = 0
            elif chosen == "quit":
                logger.info("Quit (the tray)")
                return 0
        elif event == "shellClosed":
            # The shell ended by itself (macOS: its own termination, the Dock's
            # Quit); its exit status is recorded where it ends (_end_shell).
            logger.info("the native shell ended by itself")
            return 0
        if time.monotonic() >= next_show:
            # A connected browser's Open DownloadPulse: the tray's Open.
            if (await channel.ask("show")).get("show"):
                await bring_forward("Open (a connected browser)")
            next_show = time.monotonic() + SHOW_POLL_SECONDS
        if time.monotonic() >= next_poll:
            batch = await channel.ask("events", after=cursor)
            cursor = batch.get("cursor", cursor)
            for title, body in notices(batch.get("events") or [], words):
                await native.notice(title, body)
            current = (await channel.ask("status")).get("status")
            # The tray in the language the interface is shown in: a change there is redrawn now.
            relabel = words.choose((await channel.ask("locale")).get("locale"))
            if current != status or relabel or not native.tray_shown:
                status = current
                await native.tray(tray_menu(status, words))
            if relabel and sys.platform == "darwin":
                # macOS: the application menu (its Quit) speaks the new language too.
                await _window(shell, "window.setMainMenu", main_menu(words))
            # The window frame in the page's theme, whenever the page says a new one.
            frame = (await channel.ask("frame")).get("frame")
            if frame and frame != applied_frame:
                applied_frame = frame
                await apply_frame(shell, frame)
            # Desktop saves waiting for a destination: each in its own task, so
            # the window and tray stay live while its dialog is open.
            for waiting in (await channel.ask("saves")).get("saves") or []:
                asyncio.ensure_future(_save(channel, native, words, waiting))
            next_poll = time.monotonic() + POLL_SECONDS
    logger.error("the DownloadPulse backend ended on its own (%s)", backend.process.returncode)
    return 1


async def apply_frame(shell: "Shell", frame: dict) -> None:
    """The window's own frame in the page's theme (``frame``: caption and text
    ``#rrggbb``, dark). macOS: the shell's ``window.setFrameTheme``
    (packaging/desktop/neutralino/macos-frame-theme.patch). Windows: the title
    bar of every window of the shell (``windows_frame``). Linux keeps its
    desktop's frame. A frame that cannot be applied is logged, never fatal."""
    try:
        if sys.platform == "darwin":
            await shell.call("window.setFrameTheme", {"caption": frame["caption"], "dark": bool(frame["dark"])})
        elif os.name == "nt":
            coloured = await asyncio.to_thread(windows_frame, shell.binary, frame["caption"], frame["text"],
                                               bool(frame["dark"]))
            if not coloured:
                logger.warning("no window of the shell to give the frame")
    except (NativeError, OSError, TimeoutError, KeyError) as exc:
        logger.warning("the window frame could not be applied: %s", exc)


def colorref(value: str) -> int:
    """``#rrggbb`` as Windows' COLORREF (0x00bbggrr)."""
    red, green, blue = (int(value[index:index + 2], 16) for index in (1, 3, 5))
    return blue << 16 | green << 8 | red


def windows_frame(binary: str, caption: str, text: str, dark: bool) -> int:
    """Windows: every top-level window of the process running ``binary`` (the
    shell, which ProcessOwnership starts behind its gate) gets the frame:
    dark mode for a dark theme (Windows 10 20H1+), and on Windows 11 the
    caption and its text in the theme's colours (DwmSetWindowAttribute
    DWMWA_USE_IMMERSIVE_DARK_MODE 20, DWMWA_CAPTION_COLOR 35,
    DWMWA_TEXT_COLOR 36; an attribute Windows lacks is simply refused). How
    many windows were coloured."""
    import ctypes
    from ctypes import wintypes as w
    user32, kernel32, dwm = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32"), ctypes.WinDLL("dwmapi")
    callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user32.EnumWindows.argtypes = [callback, w.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    kernel32.OpenProcess.argtypes, kernel32.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    kernel32.CloseHandle.argtypes = [w.HANDLE]
    dwm.DwmSetWindowAttribute.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD]
    target = os.path.normcase(os.path.realpath(binary))
    found = []

    def visit(hwnd, _):
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = kernel32.OpenProcess(0x1000, False, pid.value)          # PROCESS_QUERY_LIMITED_INFORMATION
        if process:
            image, size = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
            if kernel32.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size)) \
                    and os.path.normcase(os.path.realpath(image.value)) == target:
                found.append(hwnd)
            kernel32.CloseHandle(process)
        return True

    user32.EnumWindows(callback(visit), 0)
    for hwnd in found:
        for attribute, value in ((20, int(dark)), (35, colorref(caption)), (36, colorref(text))):
            data = w.DWORD(value)
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))
    return len(found)


async def _save(channel, native, words, waiting: dict) -> None:
    """One desktop save: the person's Save dialog, then its answer to the
    backend (a path, or None for cancelled), which writes the file."""
    path = None
    try:
        path = await native.save_destination(str(waiting.get("name") or "downloadpulse-file"), words("desktop.save.title"))
    finally:
        try:
            await channel.ask("saved", save=waiting["save"], path=path)
        except (ConnectionError, OSError, ValueError) as exc:
            logger.error("a desktop save's answer did not reach the backend: %s", exc)


async def _end_shell(shell: Shell, owned, processes) -> None:
    """The native shell's end at Quit, in order, recorded as what it is -- the
    shell's end, never the backend's.

    * macOS: its own ``app.exit``. The shell DownloadPulse builds
      (packaging/desktop/neutralino/macos-main-thread-shutdown.patch) tears its
      status item and window down on the main thread, and a termination
      signal is an event on its main queue that ends it the same way.
    * Linux: SIGTERM; its handler runs ``cleanupTray`` (a no-op: the tray is
      the supervisor's) and ``_Exit``.
    * Windows: its job, as the process owner ends one.

    Only a shell that does not end in time is stopped by the process owner
    (SIGTERM, then SIGKILL: the bounded fallback for a stuck process), and
    the log says so. The shell keeps nothing to save."""
    if owned.process.returncode is not None or await _ended(owned, 0.5):
        code = owned.process.returncode
        logger.info("the native shell had ended by itself (%s%s)", code,
                    f": signal {-code}, abnormal" if code is not None and code < 0 else "")
        return
    if sys.platform == "darwin":
        try:
            await shell.exit()
        except (OSError, RuntimeError) as exc:
            logger.warning("the native shell was not asked to exit: %s", exc)
        if await _ended(owned, SHELL_EXIT_SECONDS):
            logger.info("the native shell exited by its own app.exit (%s)", owned.process.returncode)
            return
        logger.error("the native shell did not exit in %ss; stopping it", SHELL_EXIT_SECONDS)
    ended = await processes.terminate(owned, SHELL_ID, grace=SHELL_EXIT_SECONDS)
    code = owned.process.returncode
    how = ("its job ended" if os.name == "nt"
           else "SIGTERM, through its own handler" if code == 128 + 15
           else "SIGTERM" if code == -15 else "forcibly, SIGKILL after an unanswered SIGTERM")
    logger.info("the native shell was ended by the supervisor: %s%s (%s)", how,
                "" if ended else " -- NOT proven gone", code)


async def _window(shell: Shell, method: str, data: dict | list | None = None) -> None:
    try:
        await shell.call(method, data)
    except NativeError as exc:
        logger.error("the window did not respond: %s", exc)


async def _ready(session, port: int, backend) -> None:
    deadline = time.monotonic() + READY_SECONDS
    while time.monotonic() < deadline:
        if backend.process.returncode is not None:
            raise RuntimeError(f"the DownloadPulse backend ended during start ({backend.process.returncode})")
        try:
            async with session.get(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, asyncio.TimeoutError, Exception):  # noqa: BLE001 -- not listening yet
            pass
        await asyncio.sleep(0.25)
    raise TimeoutError("the DownloadPulse backend did not become ready")


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
