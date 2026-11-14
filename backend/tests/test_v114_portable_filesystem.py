"""Portable filesystem, configuration and path safety (1.0.14 desktop WS1).

The folder-path rules and lexical containment (``core.settings_constraints``),
the secure atomic writer (``core.secure_files``), the materialization owner's
no-follow open, positional read, link predicate, file naming and empty-folder
retirement (``transfers.filesystem``) and the data-root defaults (C1) behave
the same on every host where the host allows it, and by the host's own rules
where it does not.

* The Windows path rules are pure functions of ``ntpath`` and run on every
  host; on POSIX the rules are proved byte-identical to the ones they replace.
* Everything that touches the filesystem runs on the host it is collected on.
  A junction exists only on Windows, so junction cases are generated only
  there: nothing here is ever skipped.
"""
from __future__ import annotations

import hashlib
import ntpath
import os
import posixpath
import subprocess
import sys
from pathlib import Path

import pytest

from core import secure_files
from core.settings_constraints import AbsolutePath, native_absolute, path_within, windows_reserved_name
from external import profiles
from transfers import filesystem
from transfers.models import IntegrityMetadata

WINDOWS = os.name == "nt"
LINK_KINDS = ["symlink", *(["junction"] if WINDOWS else [])]


_JUNCTIONS: list[Path] = []


def _link(kind: str, target: Path, link: Path) -> None:
    if kind == "symlink":
        os.symlink(target, link, target_is_directory=target.is_dir())
    else:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
        _JUNCTIONS.append(link)


@pytest.fixture(autouse=True)
def _remove_junctions():
    """A junction a test made is removed as the junction itself (``rmdir``),
    never through it, before pytest's temporary-directory cleanup (which
    cannot traverse one on Windows 11: ``WinError 448``)."""
    yield
    while _JUNCTIONS:
        junction = _JUNCTIONS.pop()
        if junction.is_junction():
            os.rmdir(junction)


# ── Folder path rules ─────────────────────────────────────────────────────────

WINDOWS_ACCEPTED = [
    "C:\\", "C:\\Downloads", "c:\\Users\\me\\Downloads", "D:\\Media\\TV Shows",
    "\\\\nas\\share", "\\\\nas\\share\\", "\\\\nas\\share\\Downloads",
]
WINDOWS_REFUSED = [
    "/Downloads",                                # rooted, no drive: whatever drive is current
    "\\Downloads",
    "/Downloads/x\\..\\..\\Windows",             # the known mixed-separator traversal
    "C:\\Downloads\\x\\..\\..\\Windows",         # traversal, unresolved
    "C:\\Downloads/x",                           # a second separator spelling
    "C:\\Downloads\\",                           # trailing separator
    "C:\\Downloads\\\\x",                        # doubled separator
    "C:\\Downloads\\.\\x",
    "C:Downloads",                               # drive-relative
    "C:",
    "Downloads", "",
    "\\\\?\\C:\\Downloads",                      # extended-length namespace
    "\\\\.\\PhysicalDrive0",                     # device namespace
    "\\\\?\\UNC\\nas\\share",
    "\\\\nas", "\\\\nas\\",
    "C:\\Downloads\\NUL", "C:\\Downloads\\con.txt", "C:\\COM1\\x", "C:\\Downloads\\LPT9.log",
    "C:\\Downloads\\name.", "C:\\Downloads\\name ",  # Windows would silently rewrite them
    "C:\\Downloads\\a:stream",                   # alternate data stream
    "C:\\Down<loads", "C:\\Down|loads", "C:\\Down\x01loads",
    "1:\\Downloads",
]


@pytest.mark.parametrize("value", WINDOWS_ACCEPTED)
def test_windows_fully_qualified_folders_are_accepted(value):
    assert native_absolute(value, ntpath)


@pytest.mark.parametrize("value", WINDOWS_REFUSED)
def test_windows_ambiguous_device_reserved_and_traversal_forms_are_refused(value):
    assert not native_absolute(value, ntpath)


POSIX_SAMPLES = ["/", "/download", "/a/../b", "/a//b", "/a/", "relative", "", "C:\\x", "\\\\nas\\share",
                 "/x\\..\\..\\etc", "~/x", "./x", "/NUL", "/con.txt"]


@pytest.mark.parametrize("value", POSIX_SAMPLES)
def test_posix_folder_rule_is_exactly_the_rule_it_replaces(value):
    assert native_absolute(value, posixpath) is value.startswith("/")


@pytest.mark.parametrize("base,path", [
    ("/download", "/download"), ("/download", "/download/tv"), ("/download", "/download2"),
    ("/download/", "/download/tv"), ("/", "/x"), ("/download", "/download/../etc"),
    ("/download", "/Download/tv"), ("/download", "/download/x\\..\\..\\etc"), ("/download", ""),
])
def test_posix_containment_is_exactly_the_comparison_it_replaces(base, path):
    before_base, before_root = posixpath.normpath(base), posixpath.normpath(path)
    before = before_root == before_base or before_root.startswith(before_base.rstrip("/") + "/")
    assert path_within(base, path, posixpath) is before


@pytest.mark.parametrize("base,path,within", [
    ("/Downloads", "/Downloads/x\\..\\..\\Windows", False),        # the known bypass
    ("C:\\Downloads", "C:\\Downloads/x\\..\\..\\Windows", False),
    ("C:\\Downloads", "C:\\Downloads\\..\\Downloads2", False),
    ("C:\\Downloads", "C:\\Downloads2", False),
    ("C:\\Downloads", "D:\\Downloads\\tv", False),
    ("C:\\Downloads", "C:\\downloads\\TV", True),                 # Windows compares case-insensitively
    ("C:\\Downloads", "C:\\Downloads/tv", True),                  # and treats / as \
    ("C:\\Downloads", "C:\\Downloads", True),
    ("C:\\", "C:\\Downloads", True),
    ("\\\\nas\\share", "\\\\nas\\share\\tv", True),
    ("\\\\nas\\share", "\\\\nas\\sharex\\tv", False),
])
def test_windows_containment_uses_windows_component_and_case_rules(base, path, within):
    assert path_within(base, path, ntpath) is within


def test_the_mixed_separator_bypass_is_refused_where_it_is_written_and_where_it_is_compared():
    bypass = "/Downloads/x\\..\\..\\Windows"
    # As it was: a POSIX string test calls the root normalized and beneath
    # the Download Folder, while Windows resolves it to \Windows.
    assert posixpath.normpath(bypass) == bypass
    assert bypass.startswith("/Downloads/")
    assert ntpath.normpath(bypass) == "\\Windows"
    # Now: refused at the write ...
    with pytest.raises(ValueError):
        profiles._absolute(bypass, "local_root", ntpath)
    with pytest.raises(ValueError):
        profiles._absolute("C:\\Downloads/x\\..\\..\\Windows", "local_root", ntpath)
    # ... and outside the Download Folder however it got stored.
    assert not path_within("/Downloads", bypass, ntpath)
    assert profiles._absolute("C:\\Downloads\\tv", "local_root", ntpath) == "C:\\Downloads\\tv"
    for root in ("C:\\", "\\\\nas\\share", "\\\\nas\\share\\"):      # drive and share roots
        assert profiles._absolute(root, "local_root", ntpath) == root
    # POSIX keeps its exact former rule: "/" is a root, "//" and "/a/." are not normal.
    assert profiles._absolute("/", "local_root", posixpath) == "/"
    assert profiles._absolute("/a/", "local_root", posixpath) == "/a"
    for unnormal in ("//", "/a/.", "/a//b"):
        with pytest.raises(ValueError):
            profiles._absolute(unnormal, "local_root", posixpath)


def test_profile_roots_keep_their_codes_on_each_host_rule():
    for flavor, normal, unnormal in ((posixpath, "/download/tv", "/download/tv/../x"),
                                     (ntpath, "C:\\Downloads\\tv", "C:\\Downloads\\tv\\..\\x")):
        assert profiles._absolute(normal, "local_root", flavor) == normal
        with pytest.raises(ValueError) as refused:
            profiles._absolute(unnormal, "local_root", flavor)
        assert refused.value.display_code == "profiles.path_not_normalized"
        with pytest.raises(ValueError) as refused:
            profiles._absolute("relative", "local_root", flavor)
        assert refused.value.display_code == "profiles.path_not_absolute"


def test_an_import_root_is_judged_in_the_external_clients_namespace():
    # A Linux or container client's root is a POSIX path wherever DebridPulse runs.
    assert profiles.client_flavor("/imports/tv") is posixpath
    assert profiles.client_flavor("C:\\Imports") is os.path
    assert profiles._absolute("/imports/tv", "import_root", profiles.client_flavor("/imports/tv")) == "/imports/tv"


def test_a_written_folder_follows_this_hosts_rule():
    rule = AbsolutePath()
    native = "C:\\Downloads" if WINDOWS else "/download"
    foreign = "/download" if WINDOWS else "C:\\Downloads"
    rule.check(native)
    with pytest.raises(ValueError):
        rule.check(foreign)


@pytest.mark.parametrize("name,reserved", [
    ("NUL", True), ("nul", True), ("con.txt", True), ("COM1 .log", True), ("LPT\u00b9", True),
    ("name.", True), ("name ", True),
    ("NULL", False), ("console.txt", False), ("COM0", False), ("COM10", False), ("normal.mkv", False),
])
def test_windows_reserved_names(name, reserved):
    assert windows_reserved_name(name) is reserved


# ── File names DebridPulse writes ─────────────────────────────────────────────

@pytest.mark.parametrize("value,windows,posix", [
    ("NUL", "_NUL", "NUL"), ("con.txt", "_con.txt", "con.txt"), ("Episode.", "Episode", "Episode."),
    ("Track ", "Track", "Track"), ("a:b", "a_b", "a_b"), ("movie.mkv", "movie.mkv", "movie.mkv"),
    ("...", "download", "download"),
])
def test_a_written_name_never_names_a_device_or_a_rewritten_file_on_windows(monkeypatch, value, windows, posix):
    assert filesystem.safe_name(value) == (windows if WINDOWS else posix)
    monkeypatch.setattr(filesystem, "_WINDOWS", True)
    assert filesystem.safe_name(value) == windows
    monkeypatch.setattr(filesystem, "_WINDOWS", False)
    assert filesystem.safe_name(value) == posix


# ── Secure atomic write ───────────────────────────────────────────────────────

def test_a_secure_write_succeeds_where_the_platform_has_no_fchmod(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "fchmod", raising=False)
    target = tmp_path / "config" / "config.json"
    secure_files.atomic_write_json(target, {"secret": "value"}, indent=2)
    secure_files.atomic_write_json(target, {"secret": "replaced"}, indent=2)
    assert target.read_text(encoding="utf-8").replace("\r\n", "\n") == '{\n  "secret": "replaced"\n}'
    assert sorted(path.name for path in target.parent.iterdir()) == ["config.json"]


# ── Completion read, no-follow open, durability ───────────────────────────────

# Bytes a text-mode descriptor would corrupt: CR/LF pairs, and 0x1A, at which a
# Windows text-mode read stops.
PAYLOAD = b"a\nb\r\nc\x1ad" + bytes(range(256)) * 41


def _payload(tmp_path: Path) -> Path:
    path = tmp_path / "payload.bin"
    path.write_bytes(PAYLOAD)
    return path


@pytest.mark.parametrize("pread", ["native", "absent"])
def test_completion_reads_the_exact_bytes_with_or_without_pread(tmp_path, monkeypatch, pread):
    if pread == "absent":
        monkeypatch.delattr(os, "pread", raising=False)
    path = _payload(tmp_path)
    digest = IntegrityMetadata("sha256", hashlib.sha256(PAYLOAD).hexdigest())
    assert filesystem.payload_matches(str(path), len(PAYLOAD), integrity=(digest,))
    assert not filesystem.payload_matches(str(path), len(PAYLOAD) + 1)
    wrong = IntegrityMetadata("sha256", hashlib.sha256(PAYLOAD + b"x").hexdigest())
    assert not filesystem.payload_matches(str(path), len(PAYLOAD), integrity=(wrong,))


@pytest.mark.parametrize("kind", LINK_KINDS)
def test_a_link_where_the_payload_belongs_is_never_followed(tmp_path, kind):
    real = _payload(tmp_path) if kind == "symlink" else tmp_path / "real"
    if kind == "junction":
        real.mkdir()
    link = tmp_path / "link"
    _link(kind, real, link)
    with pytest.raises(OSError) as refused:
        os.close(filesystem.open_no_follow(link, os.O_RDONLY))
    assert refused.value.errno == __import__("errno").ELOOP
    assert filesystem.payload_facts(str(link)) == filesystem.PayloadFacts(True, False)
    assert filesystem.flush_payload(str(link)) is None
    assert not filesystem.payload_matches(str(link), len(PAYLOAD))


def test_a_no_follow_write_never_writes_through_a_link(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"keep")
    link = tmp_path / "member.txt"
    os.symlink(outside, link)
    with pytest.raises(OSError):
        os.close(filesystem.open_no_follow(link, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644))
    assert outside.read_bytes() == b"keep"
    created = tmp_path / "new.bin"
    fd = filesystem.open_no_follow(created, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(PAYLOAD)
    fd = filesystem.open_no_follow(created, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(b"short")
    assert created.read_bytes() == b"short"


def test_a_checkpoint_flushes_a_regular_payload_and_reports_its_identity(tmp_path):
    path = _payload(tmp_path)
    facts = filesystem.flush_payload(str(path))
    assert facts is not None and facts.available and facts.exists and facts.size == len(PAYLOAD)
    assert facts == filesystem.payload_facts(str(path))
    assert path.read_bytes() == PAYLOAD


@pytest.mark.parametrize("kind", LINK_KINDS)
def test_a_linked_directory_is_never_part_of_a_collection_member_path(tmp_path, kind):
    root, elsewhere = tmp_path / "root", tmp_path / "elsewhere"
    root.mkdir(), elsewhere.mkdir()
    (elsewhere / "file.bin").write_bytes(b"x")
    _link(kind, elsewhere, root / "linked")
    assert filesystem.is_link(root / "linked")
    assert filesystem.member_payload(str(root), "linked/file.bin") is None
    with pytest.raises(filesystem.TransferError):
        filesystem.validate_target(str(tmp_path), str(root / "linked"))


# ── Empty delivered-folder retirement ─────────────────────────────────────────

def test_retirement_removes_only_an_empty_owned_directory_beneath_the_root(tmp_path):
    root = tmp_path / "downloads"
    (root / "tv" / "Show" / "Season 1").mkdir(parents=True)
    (root / "tv" / "Busy").mkdir()
    (root / "tv" / "Busy" / "keep.mkv").write_bytes(b"x")
    remove = filesystem.remove_empty_directory
    assert remove(str(root), str(root / "tv" / "Show" / "Season 1")) == filesystem.DIRECTORY_REMOVED
    assert not (root / "tv" / "Show" / "Season 1").exists() and (root / "tv" / "Show").is_dir()
    assert remove(str(root), str(root / "tv" / "Busy")) == filesystem.DIRECTORY_NOT_EMPTY
    assert (root / "tv" / "Busy" / "keep.mkv").read_bytes() == b"x"
    assert remove(str(root), str(root / "tv" / "Gone")) == filesystem.DIRECTORY_ABSENT
    assert remove(str(root), str(root / "tv" / "Gone" / "Deeper")) == filesystem.DIRECTORY_ABSENT
    assert remove(str(root), str(root / "tv" / "Busy" / "keep.mkv")) == filesystem.DIRECTORY_REFUSED
    assert remove(str(root), str(root)) == filesystem.DIRECTORY_REFUSED
    assert remove(str(root), str(tmp_path)) == filesystem.DIRECTORY_REFUSED
    assert remove(str(root), str(root / "tv" / ".." / "tv" / "Show")) == filesystem.DIRECTORY_REFUSED
    assert remove(str(root), str(root / "tv" / "Show")) == filesystem.DIRECTORY_REMOVED


@pytest.mark.parametrize("kind", LINK_KINDS)
@pytest.mark.parametrize("position", ["final", "intermediate"])
def test_retirement_never_follows_or_removes_a_link(tmp_path, kind, position):
    root, elsewhere = tmp_path / "downloads", tmp_path / "elsewhere"
    root.mkdir()
    (elsewhere / "empty").mkdir(parents=True)
    _link(kind, elsewhere, root / "linked")
    target = root / "linked" if position == "final" else root / "linked" / "empty"
    assert filesystem.remove_empty_directory(str(root), str(target)) == filesystem.DIRECTORY_REFUSED
    assert filesystem.is_link(root / "linked") and (elsewhere / "empty").is_dir()


if WINDOWS:
    # The root itself is a link only here: on POSIX the root is opened as a
    # path (unchanged), so these cases are generated only where they apply.
    @pytest.mark.parametrize("kind", ["junction", "symlink"])
    def test_a_root_that_is_itself_a_link_is_refused_by_its_own_handle(tmp_path, monkeypatch, kind):
        real = tmp_path / "real"
        (real / "empty").mkdir(parents=True)
        root = tmp_path / "linked-root"
        _link(kind, real, root)
        native = filesystem._win32()
        seen: list[int] = []
        facts = native._facts

        def recording(handle):
            result = facts(handle)
            seen.append(result[0] if result else -1)
            return result

        monkeypatch.setattr(native, "_facts", recording)
        assert filesystem.remove_empty_directory(str(root), str(root / "empty")) == filesystem.DIRECTORY_REFUSED
        # DebridPulse's refusal, not the OS's: the root was opened as the entry
        # itself and refused on its reparse attribute, before any traversal.
        assert seen == [seen[0]] and seen[0] & native.ATTRIBUTE_REPARSE_POINT
        assert filesystem.is_link(root) and (real / "empty").is_dir()
        seen.clear()
        assert filesystem.remove_empty_directory(str(real), str(real / "empty")) == filesystem.DIRECTORY_REMOVED
        assert not seen[0] & native.ATTRIBUTE_REPARSE_POINT and not (real / "empty").exists()


# ── Configuration defaults beside the database (C1) ──────────────────────────

def _defaults(db_path: str) -> list[str]:
    script = ("import core.config as c, executors.aria2.definition as d;"
              "s, a = c.AppSettings(), d.Aria2Options();"
              "print(s.backup_folder); print(s.db_backup_folder); print(a.log_file); print(a.session_file)")
    environment = {**os.environ, "DB_PATH": db_path}
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                            env=environment, capture_output=True, text=True, timeout=120, check=True)
    return result.stdout.splitlines()[-4:]


def test_data_folders_default_beside_the_database(tmp_path):
    data = tmp_path / "Data Root"
    expected = [str(data / "backups"), str(data / "db-backups"),
                str(data / "aria2" / "aria2.log"), str(data / "aria2" / "aria2.session")]
    assert _defaults(str(data / "debridpulse.db")) == expected
    # Each is a folder value the Settings write rule and the aria2 schema accept.
    for value in expected[:2]:
        AbsolutePath().check(value)
    from executors.aria2.definition import Aria2Options
    Aria2Options(log_file=expected[2], session_file=expected[3])


def test_the_container_defaults_are_unchanged():
    container = ["/app/data/backups", "/app/data/db-backups", "/app/data/aria2/aria2.log",
                 "/app/data/aria2/aria2.session"]
    observed = _defaults("/app/data/debridpulse.db")
    assert observed == ([str(Path(value)) for value in container] if WINDOWS else container)
