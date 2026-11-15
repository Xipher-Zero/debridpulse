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
    # The page is DebridPulse's own loopback interface: nothing native is
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
    running = supervisor.tray_menu({"active": 2, "queued": 3, "paused": False}, words)
    ids = [item.get("id") for item in running["menuItems"]]
    assert ids == ["status", None, "open", "pause", None, "quit"]
    assert running["menuItems"][0]["isDisabled"] is True
    assert running["menuItems"][0]["text"] == 'desktop.tray.status{"active": 2, "queued": 3}'
    paused = supervisor.tray_menu({"active": 0, "queued": 3, "paused": True}, words)
    assert [item.get("id") for item in paused["menuItems"]][3] == "resume"
    assert not any("settings" in str(item.get("id")) for item in running["menuItems"])   # C6 dropped
    assert running["icon"] == "/resources/icons/tray.png"


def test_notices_cover_each_lifecycle_transition_once_and_coalesce_bulk_pause_and_resume():
    words, previous = Words(), {}
    first = supervisor.notices([
        {"id": 1, "type": "transfer.accepted", "transfer_id": 1, "name": "a.mkv"},
        {"id": 2, "type": "transfer.transferring", "transfer_id": 1, "name": "a.mkv"},   # no notice
        {"id": 3, "type": "transfer.completed", "transfer_id": 1, "name": "a.mkv"},
        {"id": 4, "type": "transfer.failed", "transfer_id": 2, "name": "b.iso"},
        {"id": 5, "type": "transfer.cancelled", "transfer_id": 3, "name": "c.zip"},
    ], previous, words)
    assert first == [("desktop.notice.accepted", "a.mkv"), ("desktop.notice.completed", "a.mkv"),
                     ("error.category.transfer_failed", "b.iso"), ("file_selection.transfer_cancelled", "c.zip")]
    bulk = supervisor.notices([{"id": 10 + n, "type": "transfer.paused", "transfer_id": 20 + n, "name": f"{n}"}
                               for n in range(4)], previous, words)
    assert bulk == [('desktop.notice.paused_many{"count": 4}', "")]
    resumed = supervisor.notices([{"id": 30 + n, "type": "transfer.queued", "transfer_id": 20 + n, "name": f"{n}"}
                                  for n in range(4)], previous, words)
    assert resumed == [('desktop.notice.resumed_many{"count": 4}', "")]
    # Queued without a pause before it (a new transfer) is no "resumed".
    assert supervisor.notices([{"id": 40, "type": "transfer.queued", "transfer_id": 99, "name": "n"}],
                              previous, words) == []
    one = supervisor.notices([{"id": 41, "type": "transfer.paused", "transfer_id": 1, "name": "a.mkv"}],
                             previous, words)
    assert one == [("transfer.action.paused_toast", "a.mkv")]


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
    assert set(windows) - set(canonical) == {"colorama", "tzdata"}
    assert set(canonical) - set(windows) == {"uvloop"}
    assert "--hash=sha256:" in (ROOT / "packaging" / "desktop" / "windows" / "requirements.txt").read_text()


# -- B-1: Neutralino's exported credential is private before it exists ---------------------

import os  # noqa: E402

import pytest  # noqa: E402

POSIX = os.name != "nt"


@pytest.mark.skipif(not POSIX, reason="POSIX modes; Windows is checked by its ACL below")
def test_the_credential_directory_is_made_private_before_the_shell_writes_into_it(tmp_path):
    home = tmp_path / "org.debridpulse.desktop"
    (home / ".tmp").mkdir(parents=True, mode=0o755)
    home.chmod(0o755)
    (home / ".tmp").chmod(0o755)          # what Neutralino itself creates
    supervisor.private_directory(home / ".tmp")
    assert (home.stat().st_mode & 0o777, (home / ".tmp").stat().st_mode & 0o777) == (0o700, 0o700)
    fresh = tmp_path / "fresh" / "org.debridpulse.desktop" / ".tmp"
    fresh.parent.parent.mkdir()
    supervisor.private_directory(fresh)
    assert fresh.stat().st_mode & 0o777 == 0o700 and fresh.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(not POSIX, reason="POSIX link substitution")
def test_a_linked_credential_directory_or_file_fails_closed(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    home = tmp_path / "org.debridpulse.desktop"
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

WINDOWS_ONLY = pytest.mark.skipif(POSIX, reason="Windows: native owner, reparse-point and DACL checks")


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
    except OSError as exc:   # without Developer Mode, Windows refuses the link before DebridPulse sees it
        pytest.skip(f"Windows blocked creating the file symlink before DebridPulse's check: {exc}")
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


@WINDOWS_ONLY
def test_a_private_looking_tmp_that_hands_files_no_private_acl_is_refused(tmp_path):
    """Its own ACL names only this account, SYSTEM and Administrators, but
    nothing is inheritable: Windows would give the credential the creating
    token's default DACL, which names the logon session too."""
    import subprocess
    home, credential_dir = _home(tmp_path)
    credential_dir.parent.mkdir(mode=0o700)
    credential_dir.mkdir()
    me = _account_sid()
    _icacls(credential_dir, "/inheritance:r", "/grant:r", f"*{me}:(F)", "*S-1-5-18:(F)", "*S-1-5-32-544:(F)")
    with pytest.raises(PermissionError, match="does not hand its private ACL to the files created in it"):
        supervisor.private_directory(credential_dir)
    # Why it matters: a file created there names the logon session.
    probe = credential_dir / "probe.txt"
    probe.write_text("x")
    listed = subprocess.run(["icacls", str(probe)], capture_output=True, text=True).stdout
    assert "LogonSessionId" in listed, listed


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
