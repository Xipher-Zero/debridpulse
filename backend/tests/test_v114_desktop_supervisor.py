"""The desktop supervisor's own decisions (WS5): the native shell's isolation
settings, the tray menu and the lifecycle notices. The supervisor's process and
channel behaviour is proven against a real backend in
``test_v114_desktop_control``."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from desktop import supervisor  # noqa: E402

CONFIG = json.loads((ROOT / "desktop" / "shell" / "neutralino.config.json").read_text(encoding="utf-8"))


class Words:
    def __call__(self, key, **params):
        return key + (json.dumps(params, sort_keys=True) if params else "")


def test_the_page_gets_no_native_token_library_or_extension_and_close_only_hides():
    window = CONFIG["modes"]["window"]
    # The page is DownloadPulse's own loopback interface: nothing native is
    # injected into it, so it holds no Neutralino token to call anything with.
    assert window["injectGlobals"] is False and window["injectClientLibrary"] is False
    assert CONFIG["enableExtensions"] is False and "extensions" not in CONFIG and "globalVariables" not in CONFIG
    assert window["enableInspector"] is False and window["exitProcessOnClose"] is False
    # Even the supervisor's own credential reaches only the calls it makes.
    assert CONFIG["nativeAllowList"] == list(supervisor.NATIVE_METHODS)
    assert not any(method.startswith(("os.exec", "os.spawn", "filesystem.", "storage.", "computer.", "os.open"))
                   for method in CONFIG["nativeAllowList"])
    assert CONFIG["tokenSecurity"] == "one-time" and CONFIG["exportAuthInfo"] is True
    assert CONFIG["dataLocation"] == "system" and CONFIG["applicationId"] == supervisor.APP_ID


def test_the_supervisor_refuses_a_native_call_outside_its_list():
    shell = supervisor.Shell(session=None)
    import asyncio
    import pytest
    with pytest.raises(PermissionError):
        asyncio.run(shell.call("os.execCommand", {"command": "calc"}))


def test_the_tray_offers_status_open_the_one_control_that_applies_and_quit_and_no_settings():
    words = Words()
    running = supervisor.tray_menu({"active": 2, "queued": 3, "paused": False}, words, "linux")
    ids = [item.get("id") for item in running["menuItems"]]
    assert ids == ["status", None, "open", "pause", None, "quit"]
    assert running["menuItems"][0]["isDisabled"] is True
    assert running["menuItems"][0]["text"] == 'desktop.tray.status{"active": 2, "queued": 3}'
    paused = supervisor.tray_menu({"active": 0, "queued": 3, "paused": True}, words, "linux")
    assert [item.get("id") for item in paused["menuItems"]][3] == "resume"
    assert not any("settings" in str(item.get("id")) for item in running["menuItems"])   # C6 dropped
    assert running["icon"] == "/resources/icons/tray.png" and "useTemplateIcon" not in running
    assert supervisor.tray_menu(None, words, "win32")["icon"] == "/resources/icons/tray.png"
    # macOS: the menu bar's own template image, which it tints for its appearance.
    mac = supervisor.tray_menu(None, words, "darwin")
    assert mac["icon"] == "/resources/icons/trayTemplate.png" and mac["useTemplateIcon"] is True
    assert [item.get("id") for item in mac["menuItems"]] == ["status", None, "open", "pause", None, "quit"]


def test_notices_cover_each_lifecycle_transition_once_and_coalesce_bulk_pause_and_resume():
    words = Words()
    first = supervisor.notices([
        {"id": 1, "type": "transfer.accepted", "transfer_id": 1, "name": "a.mkv"},
        {"id": 2, "type": "transfer.transferring", "transfer_id": 1, "name": "a.mkv"},   # no notice
        {"id": 3, "type": "transfer.completed", "outcome": "succeeded", "transfer_id": 1, "name": "a.mkv"},
        {"id": 4, "type": "transfer.failed", "outcome": "failed", "transfer_id": 2, "name": "b.iso"},
        {"id": 5, "type": "transfer.cancelled", "outcome": "cancelled", "transfer_id": 3, "name": "c.zip"},
    ], words)
    assert first == [("desktop.notice.accepted", "a.mkv"), ("desktop.notice.completed", "a.mkv"),
                     ("error.category.transfer_failed", "b.iso"), ("file_selection.transfer_cancelled", "c.zip")]
    bulk = supervisor.notices([{"id": 10 + n, "type": "transfer.paused", "transfer_id": 20 + n, "name": f"{n}"}
                               for n in range(4)], words)
    assert bulk == [('desktop.notice.paused_many{"count": 4}', "")]
    # Resumed is the journal's own word for a transition out of Paused, into
    # whatever state the transfer goes on in -- also for a transfer paused
    # before this run began.
    resumed = supervisor.notices([{"id": 30 + n, "type": kind, "outcome": "resumed", "transfer_id": 20 + n,
                                   "name": f"{n}"}
                                  for n, kind in enumerate(["transfer.queued", "transfer.transferring",
                                                            "transfer.ready", "transfer.resolving"])], words)
    assert resumed == [('desktop.notice.resumed_many{"count": 4}', "")]
    # Queued or downloading without that outcome (a new transfer, a retry) is no "resumed".
    assert supervisor.notices([{"id": 40, "type": "transfer.queued", "transfer_id": 99, "name": "n"},
                               {"id": 41, "type": "transfer.transferring", "transfer_id": 99, "name": "n"}],
                              words) == []
    one = supervisor.notices([{"id": 42, "type": "transfer.paused", "transfer_id": 1, "name": "a.mkv"}], words)
    assert one == [("transfer.action.paused_toast", "a.mkv")]
    single = supervisor.notices([{"id": 43, "type": "transfer.queued", "outcome": "resumed", "transfer_id": 1,
                                  "name": "a.mkv"}], words)
    assert single == [("transfer.action.resumed_toast", "a.mkv")]


def test_the_journal_says_resumed_only_for_a_transition_out_of_paused():
    sys.path.insert(0, str(ROOT / "backend"))
    from transfers import journal_events
    from transfers.models import TransferState as S

    for target in (S.QUEUED, S.TRANSFERRING, S.READY, S.RESOLVING, S.ACCEPTED, S.POST_PROCESSING):
        event = journal_events.lifecycle(7, S.PAUSED, target)
        assert event.outcome == "resumed" and event.event_type == f"transfer.{journal_events._STATE_LABELS[target][0]}"
    # Leaving Paused for a stopped state is that stop, not a resume.
    assert journal_events.lifecycle(7, S.PAUSED, S.CANCELLED).outcome == "cancelled"
    assert journal_events.lifecycle(7, S.PAUSED, S.FAILED).outcome == "failed"
    assert journal_events.lifecycle(7, S.PAUSED, S.DELETED).outcome is None
    # The same states reached any other way say nothing of a resume.
    for previous in (S.ACCEPTED, S.QUEUED, S.TRANSFERRING, S.FAILED):
        assert journal_events.lifecycle(7, previous, S.QUEUED).outcome is None
        assert journal_events.lifecycle(7, previous, S.TRANSFERRING).outcome is None
    assert journal_events.lifecycle(7, S.TRANSFERRING, S.PAUSED).outcome is None


def test_every_tray_and_notice_word_is_in_the_catalog():
    english = json.loads((ROOT / "frontend" / "static" / "i18n" / "en.json").read_text(encoding="utf-8"))["messages"]
    used = set(supervisor.LIFECYCLE_NOTICES.values()) | {
        "desktop.tray.status", "desktop.tray.open", "desktop.tray.quit", "processing.pause_all",
        "processing.resume_all", "transfer.action.paused_toast", "transfer.action.resumed_toast",
        "desktop.notice.paused_many", "desktop.notice.resumed_many"}
    assert used <= set(english)


def test_the_windows_runtime_lock_keeps_every_canonical_pin():
    import re
    pins = lambda path: {name.lower().replace("_", "-"): version for name, version in re.findall(  # noqa: E731
        r"(?m)^([A-Za-z0-9_.-]+)==([^\s\;]+)", path.read_text(encoding="utf-8"))}
    canonical = pins(ROOT / "backend" / "requirements.txt")
    windows = pins(ROOT / "packaging" / "desktop" / "windows" / "requirements.txt")
    assert {name: version for name, version in windows.items() if name in canonical and canonical[name] != version} == {}
    assert set(windows) - set(canonical) == {"colorama", "pywin32", "truststore", "tzdata"}
    assert set(canonical) - set(windows) == {"uvloop"}
    assert "--hash=sha256:" in (ROOT / "packaging" / "desktop" / "windows" / "requirements.txt").read_text()


def test_the_macos_runtime_lock_keeps_every_canonical_pin_and_adds_only_pyobjc():
    import re
    lock = ROOT / "packaging" / "desktop" / "macos" / "requirements.txt"
    pins = lambda path: {name.lower().replace("_", "-"): version for name, version in re.findall(  # noqa: E731
        r"(?m)^([A-Za-z0-9_.-]+)==([^\s\;]+)", path.read_text(encoding="utf-8"))}
    canonical = pins(ROOT / "backend" / "requirements.txt")
    macos = pins(lock)
    assert {name: version for name, version in macos.items() if name in canonical and canonical[name] != version} == {}
    assert set(canonical) - set(macos) == set()
    assert {name: macos[name] for name in set(macos) - set(canonical)} == {
        "pyobjc-core": "12.2.1", "pyobjc-framework-cocoa": "12.2.1", "truststore": "0.10.4"}
    # Every package line carries its hashes (pip installs it --require-hashes).
    blocks = re.split(r"(?m)^(?=[A-Za-z0-9_.-]+==)", lock.read_text(encoding="utf-8"))[1:]
    assert len(blocks) == len(macos) and all("--hash=sha256:" in block for block in blocks)


def test_the_linux_runtime_lock_keeps_every_canonical_pin_and_adds_only_the_session_bus_client():
    import re
    lock = ROOT / "packaging" / "desktop" / "linux" / "requirements.txt"
    pins = lambda path: {name.lower().replace("_", "-"): version for name, version in re.findall(  # noqa: E731
        r"(?m)^([A-Za-z0-9_.-]+)==([^\s\;]+)", path.read_text(encoding="utf-8"))}
    canonical, linux = pins(ROOT / "backend" / "requirements.txt"), pins(lock)
    assert {name: version for name, version in linux.items() if name in canonical and canonical[name] != version} == {}
    assert set(canonical) - set(linux) == set()
    assert {name: linux[name] for name in set(linux) - set(canonical)} == {"jeepney": "0.9.0"}
    blocks = re.split(r"(?m)^(?=[A-Za-z0-9_.-]+==)", lock.read_text(encoding="utf-8"))[1:]
    assert len(blocks) == len(linux) and all("--hash=sha256:" in block for block in blocks)
    # The assembler installs it on Linux, as it installs each platform's own lock.
    sys.path.insert(0, str(ROOT / "packaging" / "desktop"))
    import assemble
    assert assemble.DESKTOP_LOCKS["Linux"] == "packaging/desktop/linux/requirements.txt"


def test_the_menu_bar_template_is_the_favicons_own_mark_in_black():
    sys.path.insert(0, str(ROOT / "packaging" / "desktop"))
    import assemble
    favicon = (ROOT / "frontend" / "static" / "favicon.svg").read_text(encoding="utf-8")
    template = assemble.template_svg(favicon)
    assert "<rect" not in template and "url(#mark)" not in template
    assert template.count("<path") == 2 and template.count('"#000"') == 2
    # The mark's own geometry, unchanged.
    import re
    assert re.findall(r' d="([^"]+)"', template) == re.findall(r' d="([^"]+)"', favicon)
    import pytest
    with pytest.raises(SystemExit):     # a favicon of another shape is not silently templated
        assemble.template_svg(favicon.replace("<rect", "<circle", 1))


def test_the_macos_usenet_patch_is_the_pinned_one_for_the_pinned_release_and_only_removes_the_menu_runner():
    import hashlib
    import importlib.util
    import re
    spec = importlib.util.spec_from_file_location("assemble", ROOT / "packaging" / "desktop" / "assemble.py")
    assemble = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(assemble)
    pin = assemble.USENET_MACOS_PATCH
    patch = (ROOT / pin["path"]).read_bytes()
    assert hashlib.sha256(patch).hexdigest() == pin["sha256"]
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"(?m)^ARG USENET_SERVICE_VERSION=(\S+)$", dockerfile).group(1) == pin["version"]
    text = patch.decode("utf-8")
    assert re.findall(r"(?m)^\+\+\+ (\S+)", text) == ["b/" + pin["file"]]
    removed = [line[1:] for line in text.splitlines() if line.startswith("-") and not line.startswith("---")]
    added = [line[1:] for line in text.splitlines() if line.startswith("+") and not line.startswith("+++")]
    assert removed[0] == "    elif sabnzbd.MACOS and sabnzbd.FOUNDATION:"
    assert "        AppHelper.runEventLoop()" in removed
    assert added and all(line.lstrip().startswith("#") for line in added)   # a notice, no new code
    assert "Modified by DownloadPulse" in added[0]


# -- B-1: Neutralino's exported credential is private before it exists ---------------------

import logging  # noqa: E402
import os  # noqa: E402

import pytest  # noqa: E402

POSIX = os.name != "nt"


@pytest.mark.only_on(POSIX, reason="POSIX modes; Windows is checked by its ACL below")
def test_the_credential_directory_is_made_private_before_the_shell_writes_into_it(tmp_path):
    home = tmp_path / "net.downloadpulse.desktop"
    (home / ".tmp").mkdir(parents=True, mode=0o755)
    home.chmod(0o755)
    (home / ".tmp").chmod(0o755)          # what Neutralino itself creates
    supervisor.private_directory(home / ".tmp")
    assert (home.stat().st_mode & 0o777, (home / ".tmp").stat().st_mode & 0o777) == (0o700, 0o700)
    fresh = tmp_path / "fresh" / "net.downloadpulse.desktop" / ".tmp"
    fresh.parent.parent.mkdir()
    supervisor.private_directory(fresh)
    assert fresh.stat().st_mode & 0o777 == 0o700 and fresh.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.only_on(POSIX, reason="POSIX link substitution")
def test_a_linked_credential_directory_or_file_fails_closed(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    home = tmp_path / "net.downloadpulse.desktop"
    home.mkdir(mode=0o700)
    (home / ".tmp").symlink_to(elsewhere)
    with pytest.raises(OSError):
        supervisor.private_directory(home / ".tmp")
    assert elsewhere.stat().st_mode & 0o777 == 0o755        # the link's target was not touched
    real = tmp_path / "real.json"
    real.write_text('{"nlToken": "x"}')
    (tmp_path / "auth_info.json").symlink_to(real)
    with pytest.raises(PermissionError):
        supervisor.read_credential(tmp_path / "auth_info.json")
    assert real.exists()


def test_the_credential_is_read_once_and_deleted(tmp_path):
    credential = tmp_path / "auth_info.json"
    credential.write_text('{"nlToken": "a", "nlConnectToken": "b", "nlPort": 1}')
    assert supervisor.read_credential(credential)["nlPort"] == 1
    assert not credential.exists()


# -- Windows: owner, reparse points and every ACE, checked by handle -------------------------

WINDOWS_ONLY = pytest.mark.only_on(not POSIX, reason="Windows: native owner, reparse-point and DACL checks")


def _icacls(*argv):
    import subprocess
    icacls = os.path.join(os.environ["SYSTEMROOT"], "System32", "icacls.exe")
    subprocess.run([icacls, *map(str, argv)], check=True, capture_output=True)


def _junction(link, target):
    import subprocess
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


def _home(tmp_path):
    """A data home inside this account's profile (its ACL: this account,
    SYSTEM, Administrators), as %APPDATA% is."""
    home = tmp_path / "roaming"
    home.mkdir()
    return home, home / supervisor.APP_ID / ".tmp"


@WINDOWS_ONLY
def test_a_fresh_and_an_existing_private_path_are_accepted(tmp_path):
    home, credential_dir = _home(tmp_path)
    supervisor.private_directory(credential_dir)          # created private
    supervisor.private_directory(credential_dir)          # existing and private: accepted again
    assert credential_dir.is_dir()


@WINDOWS_ONLY
@pytest.mark.parametrize("where,rights", [("data home", "(M)"), ("app directory", "(R)"), (".tmp", "(R)")])
def test_an_unexpected_explicit_trustee_fails_closed(tmp_path, where, rights):
    """Backup Operators -- no familiar "broad" name. Above the credential any
    right to change an entry is refused; on its own directories even read."""
    home, credential_dir = _home(tmp_path)
    supervisor.private_directory(credential_dir)
    target = {"data home": home, "app directory": credential_dir.parent, ".tmp": credential_dir}[where]
    _icacls(target, "/grant", f"*S-1-5-32-551:{rights}")
    with pytest.raises(PermissionError, match="S-1-5-32-551"):
        supervisor.private_directory(credential_dir)


@WINDOWS_ONLY
def test_read_and_traverse_for_someone_else_above_the_credential_is_accepted(tmp_path):
    """What Windows itself grants on a profile (traverse for a capability SID,
    read for Users): it cannot replace or redirect the path."""
    home, credential_dir = _home(tmp_path)
    _icacls(home, "/grant", "*S-1-5-32-551:(RX)")
    supervisor.private_directory(credential_dir)


@WINDOWS_ONLY
@pytest.mark.parametrize("where", ["app directory", ".tmp"])
def test_a_junction_in_the_credential_path_fails_closed(tmp_path, where):
    home, credential_dir = _home(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if where == "app directory":
        _junction(credential_dir.parent, elsewhere)
    else:
        credential_dir.parent.mkdir(mode=0o700)
        _junction(credential_dir, elsewhere)
    with pytest.raises(PermissionError, match="reparse point"):
        supervisor.private_directory(credential_dir)
    assert not (elsewhere / ".tmp").exists()              # nothing was created through the junction


@WINDOWS_ONLY
def test_the_credential_is_read_by_handle_and_deleted_and_a_linked_one_fails_closed(tmp_path):
    home, credential_dir = _home(tmp_path)
    supervisor.private_directory(credential_dir)
    credential = credential_dir / "auth_info.json"
    credential.write_text('{"nlToken": "a", "nlConnectToken": "b", "nlPort": 1}')
    assert supervisor.read_credential(credential)["nlPort"] == 1 and not credential.exists()
    decoy = tmp_path / "decoy.json"
    decoy.write_text('{"nlToken": "x", "nlConnectToken": "y", "nlPort": 2}')
    try:
        os.symlink(decoy, credential)
    except OSError as exc:   # without Developer Mode, Windows refuses the link before DownloadPulse sees it
        pytest.skip(f"Windows blocked creating the file symlink before DownloadPulse's check: {exc}")
    with pytest.raises(PermissionError, match="reparse point"):
        supervisor.read_credential(credential)
    assert decoy.exists()


@WINDOWS_ONLY
def test_a_credential_not_yet_written_is_waited_for_not_refused(tmp_path):
    home, credential_dir = _home(tmp_path)
    supervisor.private_directory(credential_dir)
    with pytest.raises(FileNotFoundError):
        supervisor.read_credential(credential_dir / "auth_info.json")


@WINDOWS_ONLY
def test_a_private_data_home_under_a_parent_another_account_could_replace_it_in_is_refused(tmp_path):
    parent = tmp_path / "open-parent"
    parent.mkdir()
    _icacls(parent, "/grant", "*S-1-5-32-551:(OI)(CI)(M)")   # an unexpected trustee may delete/rename children
    home = parent / "roaming"
    home.mkdir(mode=0o700)                                 # the data home itself: private (protected DACL)
    with pytest.raises(PermissionError, match="open-parent grants S-1-5-32-551 rights to change it"):
        supervisor.private_directory(home / supervisor.APP_ID / ".tmp")
    assert not (home / supervisor.APP_ID).exists()         # refused before anything was created


@WINDOWS_ONLY
def test_an_intermediate_reparse_point_above_the_data_home_is_refused(tmp_path):
    real = tmp_path / "real"
    (real / "roaming").mkdir(parents=True)
    _junction(tmp_path / "link", real)
    with pytest.raises(PermissionError, match="reparse point"):
        supervisor.private_directory(tmp_path / "link" / "roaming" / supervisor.APP_ID / ".tmp")
    assert not (real / "roaming" / supervisor.APP_ID).exists()


@WINDOWS_ONLY
def test_a_data_home_outside_the_accounts_profile_is_not_supported(tmp_path):
    outside = __import__("pathlib").Path(os.environ["SYSTEMROOT"])
    with pytest.raises(PermissionError, match="not inside this account's profile"):
        supervisor.private_directory(outside / supervisor.APP_ID / ".tmp")
    assert not (outside / supervisor.APP_ID).exists()


@WINDOWS_ONLY
def test_the_real_roaming_appdata_is_an_accepted_anchor_path():
    """Neutralino's own data home (the Roaming known folder) passes the full
    chain check on this account's ordinary profile."""
    home = supervisor.shell_data_home()
    profile = __import__("pathlib").Path(os.environ["USERPROFILE"])
    assert os.path.normcase(str(home)).startswith(os.path.normcase(str(profile)) + os.sep)


def _account_sid():
    import subprocess
    return subprocess.run(["powershell", "-NoProfile", "-Command",
                           "([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value"],
                          capture_output=True, text=True, check=True).stdout.strip()


def _advapi():
    import ctypes
    from ctypes import wintypes
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD)]
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    return ctypes, wintypes, adv


_OWNER_AND_DACL = 0x1 | 0x4                                # OWNER_ | DACL_SECURITY_INFORMATION


def _descriptor_text(descriptor) -> str:
    """A security descriptor's owner and DACL as Windows itself writes them in
    SDDL (well-known SIDs as their aliases, e.g. the built-in Administrator
    account as ``LA``)."""
    ctypes, wintypes, adv = _advapi()
    text = wintypes.LPWSTR()
    if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, _OWNER_AND_DACL,
                                                                    ctypes.byref(text), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return text.value
    finally:
        ctypes.windll.kernel32.LocalFree(text)


def _sddl(path) -> str:
    """``path``'s owner and DACL as SDDL, read straight from Windows (not
    through the supervisor), so a fixture can be checked independently of the
    policy under test."""
    ctypes, wintypes, adv = _advapi()
    needed = wintypes.DWORD()
    adv.GetFileSecurityW(str(path), _OWNER_AND_DACL, None, 0, ctypes.byref(needed))
    descriptor = ctypes.create_string_buffer(needed.value)
    if not adv.GetFileSecurityW(str(path), _OWNER_AND_DACL, descriptor, needed, ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _descriptor_text(descriptor)


def _install_fixture(path, sddl: str) -> None:
    """Replace ``path``'s owner and ENTIRE DACL with ``sddl`` in one call
    (``P``: inheritance from the parent blocked), then read it back and
    require exactly that descriptor -- compared in Windows' own SDDL form of
    ``sddl``, so only a different owner, ACE, flag or right counts. A failure
    here is a FIXTURE mismatch, never a policy result."""
    ctypes, wintypes, adv = _advapi()
    descriptor = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        expected = _descriptor_text(descriptor)
        # OWNER_ | DACL_ | PROTECTED_DACL_SECURITY_INFORMATION
        if not adv.SetFileSecurityW(str(path), _OWNER_AND_DACL | 0x80000000, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        ctypes.windll.kernel32.LocalFree(descriptor)
    observed = _sddl(path)
    assert observed == expected, (
        f"fixture not in place (elevated={_elevated()}): set {sddl} (Windows' form: {expected}), "
        f"read back {observed}; application directory {_sddl(path.parent)}")


def _elevated() -> bool:
    import ctypes
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


@WINDOWS_ONLY
def test_a_private_looking_tmp_that_hands_files_no_private_acl_is_refused(tmp_path):
    """Its own ACL names only this account, SYSTEM and Administrators, but
    nothing is file-inheritable: Windows would give the credential the
    creating token's default DACL instead. Refused under any token."""
    import subprocess
    home, credential_dir = _home(tmp_path)
    credential_dir.parent.mkdir(mode=0o700)
    credential_dir.mkdir()
    me = _account_sid()
    # The fixture: this owner and exactly these three ACEs, none inheritable (no OI/CI/IO flags),
    # inheritance from the application directory protected.
    fixture = f"O:{me}D:P(A;;FA;;;{me})(A;;FA;;;SY)(A;;FA;;;BA)"
    _install_fixture(credential_dir, fixture)
    with pytest.raises(PermissionError, match="does not hand its private ACL to the files created in it"):
        supervisor.private_directory(credential_dir)
    # Why it matters, as demonstrated for a standard (unelevated) token -- the desktop's own: a file
    # created there gets the token's default DACL, which names the logon session.
    if not _elevated():
        probe = credential_dir / "probe.txt"
        probe.write_text("x")
        listed = subprocess.run(["icacls", str(probe)], capture_output=True, text=True).stdout
        assert "LogonSessionId" in listed, listed


@WINDOWS_ONLY
def test_the_same_fixture_with_file_inheriting_aces_is_accepted(tmp_path):
    """The control for the case above: the same owner, trustees and protected
    DACL, set the same way, but handed to every file created in it."""
    home, credential_dir = _home(tmp_path)
    credential_dir.parent.mkdir(mode=0o700)
    credential_dir.mkdir()
    me = _account_sid()
    fixture = f"O:{me}D:P(A;OICI;FA;;;{me})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    _install_fixture(credential_dir, fixture)
    supervisor.private_directory(credential_dir)


@WINDOWS_ONLY
def test_a_credential_whose_own_dacl_names_someone_else_is_refused_before_it_is_read(tmp_path):
    home, credential_dir = _home(tmp_path)
    supervisor.private_directory(credential_dir)
    credential = credential_dir / "auth_info.json"
    credential.write_text('{"nlToken": "a", "nlConnectToken": "b", "nlPort": 1}')
    _icacls(credential, "/grant", "*S-1-5-32-551:(R)")
    with pytest.raises(PermissionError, match="grants access to S-1-5-32-551"):
        supervisor.read_credential(credential)
    assert credential.exists()                             # refused: not consumed


def test_every_desktop_notice_is_recorded_with_its_digest_and_the_assembler_ships_them():
    import hashlib
    import importlib.util
    import re
    notices = ROOT / "packaging" / "desktop" / "notices"
    readme = (notices / "README.md").read_text(encoding="utf-8")
    recorded = dict(re.findall(r"(?m)^\| `([^`]+)` \|(?: [^|]* \|)? `([0-9a-f]{64})` \|$", readme))
    present = {path.relative_to(notices).as_posix() for path in notices.rglob("*") if path.is_file()} - {"README.md"}
    assert present and set(recorded) == present
    for name, digest in recorded.items():
        assert hashlib.sha256((notices / name).read_bytes()).hexdigest() == digest, name
    spec = importlib.util.spec_from_file_location("assemble", ROOT / "packaging" / "desktop" / "assemble.py")
    assemble = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(assemble)
    assert "docs/DEPENDENCY_LICENSES.md" in assemble.LEGAL      # Help's third-party document
    assert '("packaging/desktop/notices", "licenses/desktop")' in (ROOT / "packaging" / "desktop" / "assemble.py").read_text()


# ── Linux: the tray, notices and activation on a real session bus ──────────

def _session_bus_available() -> bool:
    import shutil
    try:
        import jeepney  # noqa: F401
    except ImportError:
        return False
    return sys.platform.startswith("linux") and shutil.which("dbus-daemon") is not None


LINUX_BUS = pytest.mark.only_on(_session_bus_available(),
                               reason="Linux with the desktop runtime's jeepney and a dbus-daemon")


BARE_BUS = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><type>session</type><listen>unix:dir={dir}</listen><auth>EXTERNAL</auth>
<policy context="default"><allow send_destination="*" eavesdrop="true"/><allow eavesdrop="true"/><allow own="*"/></policy>
</busconfig>"""


class BareBus:
    """A private dbus-daemon with no service files (nothing of the host's can
    be started on it), as this process's session bus for the test."""

    def __init__(self, tmp_path):
        import subprocess
        self.config = tmp_path / "bus.conf"
        self.config.write_text(BARE_BUS.format(dir=tmp_path), encoding="utf-8")
        self.previous = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        self.daemon = subprocess.Popen(["dbus-daemon", f"--config-file={self.config}", "--nofork", "--print-address=1"],
                                       stdout=subprocess.PIPE, text=True)
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = self.daemon.stdout.readline().strip()

    def close(self):
        if self.previous is None:
            os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
        else:
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = self.previous
        self.daemon.terminate()
        self.daemon.wait(10)


class Watcher:
    """A StatusNotifierWatcher on the bare bus: it records registrations and
    says whether a host is registered (``hosted``), and can announce a host
    going and coming as the KDE watcher does."""

    NAME = ("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher")

    def __init__(self):
        self.registered, self.hosted, self.router, self.task = [], True, None, None

    async def start(self):
        from jeepney import MatchRule, message_bus, new_method_return
        from jeepney.io.asyncio import open_dbus_router
        from jeepney.low_level import HeaderFields
        self.context = open_dbus_router("SESSION")
        self.router = await self.context.__aenter__()
        await self.router.send_and_get_reply(message_bus.RequestName(self.NAME[0]))
        self.handle = self.router.filter(MatchRule(type="method_call"), bufsize=32)

        async def serve():
            while True:
                call = await self.handle.queue.get()
                member = call.header.fields.get(HeaderFields.member)
                if member == "RegisterStatusNotifierItem":
                    self.registered.append(call.body[0])
                    await self.router.send(new_method_return(call))
                elif member == "Get":
                    await self.router.send(new_method_return(call, "v", (("b", self.hosted),)))
        self.task = asyncio.ensure_future(serve())
        return self

    async def announce(self, hosted: bool):
        from jeepney import DBusAddress, new_signal
        self.hosted = hosted
        await self.router.send(new_signal(DBusAddress(self.NAME[1], interface=self.NAME[2]),
                                          "StatusNotifierHostRegistered" if hosted
                                          else "StatusNotifierHostUnregistered"))

    async def stop(self):
        self.task.cancel()
        self.handle.close()
        await self.context.__aexit__(None, None, None)


import asyncio  # noqa: E402


async def _peer(name: str | None = None):
    """Another connection to the bare bus, owning ``name`` if given."""
    from jeepney import message_bus
    from jeepney.io.asyncio import open_dbus_router
    context = open_dbus_router("SESSION")
    router = await context.__aenter__()
    if name:
        await router.send_and_get_reply(message_bus.RequestName(name))
    router.context = context
    return router


async def _click(peer, index: int, group: bool = False):
    """A menu click as a tray host sends it: the reply's type and body."""
    from jeepney import DBusAddress, new_method_call
    menu = DBusAddress(supervisor.SessionBus.MENU, bus_name=supervisor.APP_ID, interface="com.canonical.dbusmenu")
    message = new_method_call(menu, "EventGroup", "a(isvu)", ([(index, "clicked", ("i", 0), 0)],)) if group \
        else new_method_call(menu, "Event", "isvu", (index, "clicked", ("i", 0), 0))
    reply = await peer.send_and_get_reply(message)
    return reply.header.message_type.name, reply.body


async def _drain(events: asyncio.Queue) -> list:
    await asyncio.sleep(0.2)
    found = []
    while not events.empty():
        found.append(events.get_nowait())
    return found


def _records():
    records = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    supervisor.logger.addHandler(handler)
    supervisor.logger.setLevel(logging.INFO)          # its run() leaves INFO on; so does the test
    return records, handler


@LINUX_BUS
def test_the_tray_host_alone_takes_the_trays_actions_and_anyone_may_read_and_open(tmp_path):
    """The native control boundary, on a bare bus with a watcher standing in
    for the desktop's. Menu entries (by place): 1 status, 3 Open, 4 Pause All,
    6 Quit. A peer owning an org.kde.StatusNotifierHost-* name (the tray host)
    pauses and quits; a plain peer, or one owning another org.kde name, reads
    the item and menu and opens the window, and its Pause All, Resume All and
    Quit are refused with AccessDenied before anything reaches the supervisor;
    a watcher that is its own host acts; starting DownloadPulse again opens it."""
    bus = BareBus(tmp_path)
    icons = tmp_path / "icons"
    icons.mkdir()
    (icons / "tray-22.rgba").write_bytes(bytes([10, 20, 30, 255]) * 22 * 22)
    records, handler = _records()

    async def scenario():
        from jeepney import DBusAddress, Properties, new_method_call
        watcher = await Watcher().start()
        events: asyncio.Queue = asyncio.Queue()
        session = await supervisor.SessionBus.open(events, icons)
        host, stranger, imposter = await _peer("org.kde.StatusNotifierHost-4242"), await _peer(), \
            await _peer("org.kde.NotAHost")
        try:
            await session.tray(supervisor.tray_menu({"active": 1, "queued": 2, "paused": False}, Words(), "linux"))
            assert session.tray_shown and watcher.registered == [supervisor.APP_ID]
            assert await _drain(events) == [("trayAvailability", {"shown": True})]
            menu = DBusAddress(session.MENU, bus_name=supervisor.APP_ID, interface="com.canonical.dbusmenu")
            item = DBusAddress(session.ITEM, bus_name=supervisor.APP_ID, interface="org.kde.StatusNotifierItem")
            for peer in (host, stranger, imposter):             # reads: anyone
                layout = (await peer.send_and_get_reply(new_method_call(menu, "GetLayout", "iias", (0, -1, [])))).body[1]
                labels = [child[1][1].get("label", ("s", "-"))[1] for child in layout[2]]
                assert labels == ['desktop.tray.status{"active": 1, "queued": 2}', "-", "desktop.tray.open",
                                  "processing.pause_all", "-", "desktop.tray.quit"]
                pixmaps = (await peer.send_and_get_reply(Properties(item).get("IconPixmap"))).body[0][1]
                assert pixmaps == [(22, 22, bytes([255, 10, 20, 30]) * 22 * 22)]
            # The host: its actions are taken.
            assert await _click(host, 4) == ("method_return", ())
            assert await _click(host, 6, group=True) == ("method_return", ([],))
            assert await _drain(events) == [("trayMenuItemClicked", {"id": "pause"}),
                                            ("trayMenuItemClicked", {"id": "quit"})]
            # Anyone else: refused, and nothing reaches the supervisor.
            for peer in (stranger, imposter):
                for index, group in ((4, False), (6, False), (4, True), (6, True)):
                    kind, body = await _click(peer, index, group)
                    assert kind == "error", (index, group)
                assert await _drain(events) == []
            assert sum("not the tray host" in line for line in records) == 8
            # Reopening stays anyone's: the menu's Open, the item's activation, a second start.
            assert await _click(stranger, 3) == ("method_return", ())
            await stranger.send_and_get_reply(new_method_call(item, "Activate", "ii", (0, 0)))
            assert await supervisor.SessionBus.activate_running() is True
            assert await _drain(events) == [("trayMenuItemClicked", {"id": "open"})] * 3
            # A watcher that is its own host (GNOME's AppIndicator extension): it acts.
            await session.tray(supervisor.tray_menu({"paused": True}, Words(), "linux"))
            assert await _click(watcher.router, 4) == ("method_return", ())
            assert await _drain(events) == [("trayMenuItemClicked", {"id": "resume"})]
            # A notice without a notification portal on the bus: reported, not assumed shown.
            await session.notice("desktop.notice.completed", "a.mkv")
            assert any("a notice was not shown" in line for line in records)
        finally:
            for peer in (host, stranger, imposter):
                await peer.context.__aexit__(None, None, None)
            await session.close()
            await watcher.stop()

    try:
        asyncio.run(scenario())
    finally:
        supervisor.logger.removeHandler(handler)
        bus.close()


class Portal:
    """The notification portal on the bare bus: it answers its version and
    records each AddNotification's id and title, as the desktop's would."""

    NAME = ("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop")

    def __init__(self, chosen: str | None = None):
        self.added, self.asked, self.chosen = [], [], chosen      # chosen: a file URI, or None (cancel)

    async def start(self):
        from jeepney import MatchRule, message_bus, new_method_return
        from jeepney.io.asyncio import open_dbus_router
        from jeepney.low_level import HeaderFields
        self.context = open_dbus_router("SESSION")
        self.router = await self.context.__aenter__()
        await self.router.send_and_get_reply(message_bus.RequestName(self.NAME[0]))
        self.handle = self.router.filter(MatchRule(type="method_call"), bufsize=32)

        async def serve():
            while True:
                call = await self.handle.queue.get()
                member = call.header.fields.get(HeaderFields.member)
                if member == "AddNotification":
                    self.added.append((call.body[0], call.body[1]["title"][1]))
                    await self.router.send(new_method_return(call))
                elif member == "SaveFile":
                    # As the portal does: the request's handle, then its Response
                    # signal on that handle (0 and the URIs chosen; 1 cancelled).
                    from jeepney import DBusAddress, new_signal
                    _parent, title, options = call.body
                    self.asked.append((title, options["current_name"][1], options.get("modal", ("b", False))[1]))
                    sender = call.header.fields[HeaderFields.sender].lstrip(":").replace(".", "_")
                    handle = f"/org/freedesktop/portal/desktop/request/{sender}/{options['handle_token'][1]}"
                    await self.router.send(new_method_return(call, "o", (handle,)))
                    await self.router.send(new_signal(DBusAddress(handle, interface="org.freedesktop.portal.Request"),
                                                      "Response", "ua{sv}",
                                                      (0, {"uris": ("as", [self.chosen])}) if self.chosen else (1, {})))
                elif member == "Get":
                    await self.router.send(new_method_return(call, "v", (("u", 1),)))
        self.task = asyncio.ensure_future(serve())
        return self

    async def stop(self):
        self.task.cancel()
        self.handle.close()
        await self.context.__aexit__(None, None, None)


@LINUX_BUS
def test_every_notice_is_a_new_one_never_replacing_an_earlier_runs(tmp_path):
    """The portal replaces a notice whose id it already holds for the
    application, and Plasma shows such a replacement without a popup. Two runs
    (each its own SessionBus) send notices: every id differs, so each is a
    new notice. The per-run counter (transfer-1, transfer-2, ... again in
    every run) made a later run's notices silent replacements."""
    bus = BareBus(tmp_path)
    icons = tmp_path / "icons"
    icons.mkdir()
    (icons / "tray-22.rgba").write_bytes(bytes([10, 20, 30, 255]) * 22 * 22)

    async def scenario():
        portal = await Portal().start()
        try:
            for run in ("first run", "second run"):
                session = await supervisor.SessionBus.open(asyncio.Queue(), icons)
                try:
                    await session.notice(f"{run}: accepted", "a.mkv")
                    await session.notice(f"{run}: completed", "a.mkv")
                finally:
                    await session.close()
            await asyncio.sleep(0.1)
            assert [title for _, title in portal.added] == ["first run: accepted", "first run: completed",
                                                            "second run: accepted", "second run: completed"]
            ids = [ident for ident, _ in portal.added]
            assert len(set(ids)) == 4, ids
        finally:
            await portal.stop()

    try:
        asyncio.run(scenario())
    finally:
        bus.close()


@LINUX_BUS
def test_a_save_asks_the_desktops_own_save_dialog_through_the_portal(tmp_path):
    """Linux: the Save dialog is the desktop's own, through the file chooser
    portal (Neutralino's needs a zenity or kdialog the Flatpak has not got),
    offered under the file's name: the place chosen comes back as a path
    (a file URI's, percent-decoded), a cancel as None."""
    bus = BareBus(tmp_path)
    icons = tmp_path / "icons"
    icons.mkdir()

    async def scenario():
        for chosen, expected in (("file:///run/user/1000/doc/ab12/My%20Trace.json", "/run/user/1000/doc/ab12/My Trace.json"),
                                 (None, None)):
            portal = await Portal(chosen).start()
            session = await supervisor.SessionBus.open(asyncio.Queue(), icons)
            try:
                found = await asyncio.wait_for(session.save_destination("trace-1.json", "Save File"), 10)
            finally:
                await session.close()
                await portal.stop()
            assert portal.asked == [("Save File", "trace-1.json", True)]
            assert found == expected, (chosen, found)

    try:
        asyncio.run(scenario())
    finally:
        bus.close()


def test_the_shells_save_dialog_is_allowed_and_waits_for_the_person():
    """Everywhere but Linux the shell's own Save dialog (Neutralino's
    os.showSaveDialog) is used: allowed on both sides, and given the person's
    own time rather than a native call's usual 15 seconds."""
    assert "os.showSaveDialog" in supervisor.NATIVE_METHODS and "os.showSaveDialog" in CONFIG["nativeAllowList"]
    assert supervisor.DIALOG_SECONDS >= 300
    source = (ROOT / "desktop" / "supervisor.py").read_text(encoding="utf-8")
    assert 'await self.call("os.showSaveDialog", {"title": title, "defaultPath": str(start)},\n' \
           '                                     timeout=DIALOG_SECONDS)' in source


@LINUX_BUS
def test_the_tray_is_unavailable_while_no_watcher_or_host_and_returns_with_them(tmp_path):
    """The tray's availability follows the desktop: the last host going, or
    the watcher itself, takes it away at once (the shell's event, so close no
    longer hides); a host or watcher returning registers the item again and
    gives it back."""
    bus = BareBus(tmp_path)
    (tmp_path / "icons").mkdir()

    async def scenario():
        watcher = await Watcher().start()
        events: asyncio.Queue = asyncio.Queue()
        session = await supervisor.SessionBus.open(events, tmp_path / "icons")
        try:
            assert session.tray_shown and await _drain(events) == [("trayAvailability", {"shown": True})]
            await watcher.announce(False)                       # the last host went away
            assert await _drain(events) == [("trayAvailability", {"shown": False})] and not session.tray_shown
            await watcher.announce(True)                        # a host returned
            assert await _drain(events) == [("trayAvailability", {"shown": True})] and session.tray_shown
            assert watcher.registered.count(supervisor.APP_ID) >= 2   # registered again
            await watcher.stop()                                # the watcher itself went away
            assert await _drain(events) == [("trayAvailability", {"shown": False})] and not session.tray_shown
            returned = await Watcher().start()                  # a new watcher: registered with it, shown
            try:
                assert await _drain(events) == [("trayAvailability", {"shown": True})] and session.tray_shown
                assert returned.registered == [supervisor.APP_ID]
            finally:
                await returned.stop()
        finally:
            await session.close()

    try:
        asyncio.run(scenario())
    finally:
        bus.close()


def test_macos_quit_is_the_application_menus_cmd_q_event_never_the_shells_own_termination():
    menu = supervisor.main_menu(Words())
    assert menu == [{"id": "app", "text": "DownloadPulse", "menuItems": [
        {"id": "quit", "text": "desktop.tray.quit", "shortcut": "q"}]}]
    # No native action: Neutralino sends the click to the supervisor (mainMenuItemClicked).
    assert all("action" not in item for top in menu for item in top["menuItems"])
    assert "window.setMainMenu" in supervisor.NATIVE_METHODS and "window.setMainMenu" in CONFIG["nativeAllowList"]
    # macOS Quit asks the shell to exit itself (main-thread teardown in the shell DownloadPulse builds).
    assert "app.exit" in supervisor.NATIVE_METHODS and "app.exit" in CONFIG["nativeAllowList"]


def test_open_restores_a_hidden_window_and_asks_for_a_minimized_one():
    """Open by what the window is: hidden (closed to the tray) is shown and
    raised -- the shell maps it again; minimized is unminimized and raised,
    which on Wayland the compositor grants or not (the taskbar always
    restores it); shown is raised."""
    assert supervisor.open_methods(hidden=True, minimized=False) == ("window.show", "window.focus")
    assert supervisor.open_methods(hidden=True, minimized=True) == ("window.show", "window.focus")
    assert supervisor.open_methods(hidden=False, minimized=True) == ("window.unminimize", "window.focus")
    assert supervisor.open_methods(hidden=False, minimized=False) == ("window.focus",)


def test_quit_ends_the_shell_in_order_and_says_how(tmp_path):
    """_end_shell on a real child that, like Neutralino, answers SIGTERM with
    its own handler (_Exit 143): on Linux it ends through that handler; on
    macOS the shell is first asked to exit itself (app.exit) -- this child
    cannot answer, so after the bound the process owner's SIGTERM ends it,
    through its own handler, never SIGKILL; on Windows by its job. The log
    names which, and a shell that had already ended is recorded as such."""
    import asyncio
    sys.path.insert(0, str(ROOT / "backend"))
    from executors.process_ownership import ProcessOwnership
    child = ("import signal, os, sys, time\n"
             "signal.signal(signal.SIGTERM, lambda *_: (open(sys.argv[1], 'w').write('handler'), os._exit(143)))\n"
             "open(sys.argv[1] + '.ready', 'w').write('ready')\n"
             "while True: time.sleep(0.05)\n")
    records, handler = _records()

    async def scenario():
        owner = ProcessOwnership(tmp_path / "owner")
        mark = tmp_path / "mark"
        owned = await owner.spawn(supervisor.SHELL_ID, [sys.executable, "-c", child, str(mark)],
                                  env=owner.environment())
        while not (tmp_path / "mark.ready").exists():
            await asyncio.sleep(0.05)
        await supervisor._end_shell(supervisor.Shell(session=None), owned, owner)
        code = owned.process.returncode
        if os.name == "nt":
            assert code is not None and not mark.exists()
            assert any("ended by the supervisor: its job ended" in line for line in records)
        else:
            if sys.platform == "darwin":                # asked first, then the bounded fallback
                assert any("did not exit in" in line for line in records)
            assert code == 143 and mark.read_text() == "handler"
            assert any("ended by the supervisor: SIGTERM, through its own handler (143)" in line for line in records)
            assert not any("SIGKILL" in line for line in records)
        await supervisor._end_shell(supervisor.Shell(session=None), owned, owner)   # already gone: said so
        assert any(line.startswith("the native shell had ended by itself") for line in records)
        owner.forget(supervisor.SHELL_ID)

    try:
        asyncio.run(scenario())
    finally:
        supervisor.logger.removeHandler(handler)


def test_the_shell_composites_with_texturemapper_on_linux_and_a_persons_own_choice_stands(tmp_path):
    """WebKitGTK 2.54's default Skia compositor drew stale and blank frames as
    a page change ended; its TextureMapper compositor, with compositing still
    on, did not. The Linux shell gets it unless the person running DownloadPulse
    chose otherwise; Windows keeps its WebView2 profile in DownloadPulse's data
    root; macOS gets the environment as it is."""
    base = {"PATH": "/usr/bin", "HOME": "/home/someone"}
    linux = supervisor.shell_environment(tmp_path, base, "linux")
    assert linux == {**base, "WEBKIT_USE_SKIA_FOR_COMPOSITION": "0"}
    chosen = supervisor.shell_environment(tmp_path, {**base, "WEBKIT_USE_SKIA_FOR_COMPOSITION": "1"}, "linux")
    assert chosen["WEBKIT_USE_SKIA_FOR_COMPOSITION"] == "1"
    assert supervisor.shell_environment(tmp_path, base, "win32") == {**base, "WEBVIEW2_USER_DATA_FOLDER": str(tmp_path / "webview2")}
    assert supervisor.shell_environment(tmp_path, base, "darwin") == base
    assert "PATH" in supervisor.shell_environment(tmp_path, {}, "darwin")        # always passed on, empty or not
    assert base == {"PATH": "/usr/bin", "HOME": "/home/someone"}                  # the caller's mapping is not changed


@pytest.mark.asyncio
async def test_the_shells_save_dialog_answer_is_read_as_neutralino_sends_it(tmp_path):
    """Neutralino answers os.showSaveDialog with {"returnValue": <path>, "success":
    true} (api/os/os.cpp), "" for Cancel. The supervisor reads the path from it:
    before, it took the whole answer for a path, so every save on Windows and
    macOS was 'cancelled'. A real WebSocket answering exactly as Neutralino does."""
    import json as _json

    from aiohttp import ClientSession, web

    answers = iter(["C:/Users/person/Downloads/trace.json", ""])

    async def neutralino(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for message in socket:
            call = _json.loads(message.data)
            assert call["method"] == "os.showSaveDialog" and call["data"]["title"] == "Save File"
            await socket.send_str(_json.dumps({"id": call["id"], "method": call["method"],
                                               "data": {"returnValue": next(answers), "success": True}}))
        return socket

    app = web.Application()
    app.router.add_get("/", neutralino)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession() as session:
            shell = supervisor.Shell(session)
            shell.ws = await session.ws_connect(f"ws://127.0.0.1:{port}/")
            receiving = asyncio.ensure_future(shell._receive())
            assert await shell.save_destination("trace.json", "Save File") == "C:/Users/person/Downloads/trace.json"
            assert await shell.save_destination("trace.json", "Save File") is None      # Cancel: ""
            await shell.ws.close()
            await receiving
    finally:
        await runner.cleanup()
