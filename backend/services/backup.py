"""
DebridPulse backups: the ONE owner of restore points.

A restore point is one DebridPulse backup -- the operator never manages its
member files. On disk it is a directory in the configured Backup Folder,
named by its identity (``YYYYmmdd_HHMMSS_<uuid32>``), holding an ownership
manifest, ``config.json``, the SQLite database and an optional avatar. Only
directories carrying that manifest are ever listed, removed, restored or
rotated, so a broad or shared backup root cannot cause unrelated data loss.

Everything that touches restore points lives here: creation (Run Backup, the
scheduler, and the mandatory pre-restore / pre-reset safety backups all use
``create_restore_point``), listing, validation, admission of a saved copy
(Add Backup), the portable single-file copy (Save Backup), removal, retention,
and the staged, journaled state swap a restore performs. Nothing is admitted,
created or swapped in before it has been validated completely.

Compatibility is exact: a restore point records ``schema_version`` -- the
fingerprint of its database's schema -- and is usable only while that equals
the fingerprint of the running installation's database. The one exception is
a schema named in ``_UPGRADABLE_SCHEMAS``: the canonical database bootstrap
upgrades a restore's private staged copy of it (never the restore point
itself), and that copy must then equal the running schema before anything is
swapped in.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import uuid
import zipfile
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger("debridpulse.backup")

_BACKUP_DIR_RE = re.compile(r"^\d{8}_\d{6}(?:_[0-9a-f]{8}|_[0-9a-f]{32})?$")
# DB_PATH is configurable, so the database member is any safe bare file name.
_DATABASE_MEMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
_BACKUP_RUN_LOCK = asyncio.Lock()
_MANIFEST_NAME = ".debridpulse-backup.json"
_MANIFEST_KIND = "debridpulse-backup"
_CONFIG_MEMBER = "config.json"
_AVATAR_EXTENSIONS = ("png", "jpg", "gif", "webp")
_AVATAR_MEMBERS = frozenset(f"avatar.{ext}" for ext in _AVATAR_EXTENSIONS)
# Private working names inside the Backup Folder and beside live state. None
# of them can match the restore-point pattern, so inventory and retention
# never see them.
_STAGING_PREFIX = ".dp-staging-"
_PACKAGE_PREFIX = ".dp-package-"
_RESTORE_PREFIX = ".dp-restore-"
_JOURNAL_NAME = ".dp-restore-journal.json"
_COPY_CHUNK = 1024 * 1024
# Schemas the canonical bootstrap (``db.database.init_db``) upgrades to the
# running one, each exactly as a pinned DebridPulse build recorded it:
# * the v1.0.13 schema before transfer-owned file-selection intent (6b691ef7),
#   which that bootstrap extends with the intent tables and the event journal;
# * the v1.0.13 schema immediately before the event journal (8b009d04), which
#   it extends with the journal alone -- an empty journal: legacy ``events``
#   rows are kept as they are and never converted into journal history.
_UPGRADABLE_SCHEMAS = frozenset({
    "sha256:4877a1c666288a4fbef8d6b8fdf9c92165cf7c153a246ce3cfbf3dd2727c7078",
    "sha256:8c1f182d8be7c67c4d3b7a471c0878e8dd9304e5b9b2a704a393a7d7cbc7dc1a",
})
_MAX_MANIFEST_BYTES = 64 * 1024
CONTENTS_LABEL = "DP State"

_HEADLINES = {
    "create": "Backup could not be created.",
    "add": "Backup could not be added.",
    "save": "Backup could not be saved.",
    "restore": "Backup could not be restored.",
    "remove": "Backup could not be removed.",
}
INVALID = "The selected file is not a valid DebridPulse backup."
UNSUPPORTED = "This backup was created by an unsupported version."
MISSING = "The selected backup no longer exists."


class BackupRejected(Exception):
    """An operator-facing refusal. ``message`` is safe to show as-is."""

    def __init__(self, action: str, detail: str = "", *, reason: str = "invalid"):
        self.action = action
        self.detail = detail
        self.reason = reason
        self.message = " ".join(part for part in (_HEADLINES.get(action, "Backup failed."), detail) if part)
        super().__init__(self.message)


def _invalid(action: str) -> BackupRejected:
    return BackupRejected(action, INVALID if action == "add" else "The backup is not a valid DebridPulse backup.")


def _no_space() -> BackupRejected:
    return BackupRejected("add", "There is not enough free space in the Backup Folder for this backup.",
                          reason="space")


def _unsupported(action: str) -> BackupRejected:
    return BackupRejected(action, UNSUPPORTED, reason="unsupported")


@dataclass(frozen=True)
class RestorePoint:
    id: str
    path: Path
    size_bytes: int

    @property
    def created_at(self) -> str:
        stamp = datetime.strptime(self.id[:15], "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
        return stamp.isoformat()

    def public(self) -> dict:
        return {"id": self.id, "created_at": self.created_at, "size_bytes": self.size_bytes,
                "contents": CONTENTS_LABEL}


def _chmod_private(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _sqlite_backup(source: Path, destination: Path) -> None:
    with closing(sqlite3.connect(str(source), timeout=30)) as src:
        with closing(sqlite3.connect(str(destination), timeout=30)) as dst:
            src.backup(dst)
            # A backup's database is one self-contained file: in rollback
            # journal mode an ordinary read of it creates no sidecar. The live
            # schema owner (init_db) returns a restored copy to WAL at start.
            dst.execute("PRAGMA journal_mode=DELETE")
    _chmod_private(destination, 0o600)


def _cfg():
    try:
        from core.config import get_settings
        return get_settings()
    except Exception as exc:
        logger.warning("backup: could not read config: %s", exc)
        return None


def _folder() -> Path:
    cfg = _cfg()
    return Path(getattr(cfg, "backup_folder", "/app/data/backups") or "/app/data/backups")


# ── schema identity ─────────────────────────────────────────────────────────

def _fingerprint(conn: sqlite3.Connection) -> str:
    """Schema identity: every (table, column) pair plus the DebridPulse schema
    markers, so two databases match only when the running installation would
    start on either without any migration."""
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    pairs = []
    for table in tables:
        quoted = table.replace('"', '""')
        pairs.extend(f"{table}.{row[1]}" for row in conn.execute(f'PRAGMA table_info("{quoted}")'))
    markers = sorted(str(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")) \
        if "schema_migrations" in tables else []
    identity = "\n".join(sorted(pairs)) + "\n--markers--\n" + "\n".join(markers)
    return "sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _open_frozen(path: Path) -> sqlite3.Connection:
    """Read a quiescent database file without creating any sidecar beside it."""
    return sqlite3.connect(f"file:{quote(str(path))}?immutable=1", uri=True)


def schema_version(db_path: Path) -> str:
    """The exact schema identity of a live (possibly WAL) database."""
    with closing(sqlite3.connect(str(db_path), timeout=30)) as conn:
        conn.execute("PRAGMA query_only=1")
        return _fingerprint(conn)


async def journal_backup(event_type: str, message: str, point_id: str, detail: str | None = None) -> bool:
    """A backup operation that has already succeeded, recorded after the fact
    through the journal's standalone path (a restore point is files, not a
    database transition). The event is not inside the restore point it names."""
    from db.event_journal import JournalEvent, record_now

    return await record_now(JournalEvent("administration", event_type, "info", message, "backup",
                                         subject_id=point_id, detail=detail))


# ── validation ──────────────────────────────────────────────────────────────

def _read_manifest(directory: Path, action: str) -> dict:
    path = directory / _MANIFEST_NAME
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise ValueError
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _invalid(action) from None
    if not isinstance(payload, dict) or payload.get("kind") != _MANIFEST_KIND:
        raise _invalid(action)
    return payload


def _manifest_facts(manifest: dict, action: str) -> dict:
    """The one reading of a manifest: its identity, its database member and
    the schema identity it records (``None`` when it records none).

    Manifests written before restore points carried ``database`` and
    ``schema_version`` (earlier v1.0.13 backups) are read by what the restore
    point itself holds: the database is its one member that is neither the
    configuration nor an avatar, and compatibility is decided from that
    database's own schema, exactly as for any restore point."""
    if not isinstance(manifest.get("timestamp"), str) or not _BACKUP_DIR_RE.fullmatch(manifest["timestamp"]):
        raise _invalid(action)
    recorded = manifest.get("schema_version")
    if recorded is not None and (not isinstance(recorded, str) or not recorded):
        raise _invalid(action)
    database = manifest.get("database")
    if database is None:
        files = manifest.get("files")
        if not isinstance(files, list):
            raise _invalid(action)
        candidates = [name for name in files if name not in {_CONFIG_MEMBER, *_AVATAR_MEMBERS}]
        database = candidates[0] if len(candidates) == 1 else None
    if (not isinstance(database, str) or not _DATABASE_MEMBER_RE.fullmatch(database)
            or database == _CONFIG_MEMBER or database in _AVATAR_MEMBERS):
        raise _invalid(action)
    return {**manifest, "database": database, "schema_version": recorded}


def _allowed_members(manifest: dict) -> frozenset[str]:
    """The member names a (read) manifest can legitimately own."""
    return frozenset({_MANIFEST_NAME, _CONFIG_MEMBER, manifest["database"], *_AVATAR_MEMBERS})


def _check_database(path: Path, recorded: str | None, live: str, action: str, *, upgradable: bool = False) -> None:
    try:
        with closing(_open_frozen(path)) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise _invalid(action)
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"schema_migrations", "torrents"} <= tables:
                raise _invalid(action)
            if conn.execute("SELECT 1 FROM schema_migrations LIMIT 1").fetchone() is None:
                raise _invalid(action)
            actual = _fingerprint(conn)
    except sqlite3.Error:
        raise _invalid(action) from None
    if recorded is not None and actual != recorded:
        raise _invalid(action)
    if actual != live and not (upgradable and actual in _UPGRADABLE_SCHEMAS):
        raise _unsupported(action)


def _check_config(path: Path, action: str) -> None:
    from core.config import LegacySettingsDocument, validate_settings_document

    try:
        validate_settings_document(json.loads(path.read_text(encoding="utf-8")))
    except LegacySettingsDocument:
        raise _unsupported(action) from None
    except Exception:
        raise _invalid(action) from None


def _validate_directory(directory: Path, live_schema: str, action: str) -> dict:
    """Validate one complete restore point; returns its manifest."""
    manifest = _manifest_facts(_read_manifest(directory, action), action)
    allowed = _allowed_members(manifest)
    names = set()
    for entry in directory.iterdir():
        if entry.is_symlink() or not entry.is_file():
            raise _invalid(action)
        names.add(entry.name)
    database = manifest["database"]
    if not names <= allowed or not {_MANIFEST_NAME, _CONFIG_MEMBER, database} <= names:
        raise _invalid(action)
    if len(names & _AVATAR_MEMBERS) > 1:
        raise _invalid(action)
    if sorted(manifest.get("files") or []) != sorted(names - {_MANIFEST_NAME}):
        raise _invalid(action)
    if manifest.get("errors"):
        raise _invalid(action)
    # A restore point may hold an upgradable schema; restoring it upgrades only
    # the staged copy (``stage_restore``), which must then match exactly.
    _check_database(directory / database, manifest["schema_version"], live_schema, action, upgradable=True)
    _check_config(directory / _CONFIG_MEMBER, action)
    return manifest


# ── inventory ───────────────────────────────────────────────────────────────

def _managed_backup_dir(path: Path) -> bool:
    """Return True only for directories explicitly owned by this backup service."""
    if path.is_symlink() or not path.is_dir() or not _BACKUP_DIR_RE.fullmatch(path.name):
        return False
    manifest = path / _MANIFEST_NAME
    if not manifest.is_file():
        return False
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("kind") == _MANIFEST_KIND


def _restore_point(path: Path) -> RestorePoint:
    size = sum(entry.stat().st_size for entry in path.iterdir() if entry.is_file())
    return RestorePoint(path.name, path, size)


def list_restore_points() -> list[RestorePoint]:
    """Every DebridPulse restore point in the Backup Folder, newest first."""
    folder = _folder()
    if not folder.is_dir():
        return []
    points = []
    for entry in sorted(folder.iterdir(), key=lambda item: item.name, reverse=True):
        if not _managed_backup_dir(entry):
            continue
        try:
            points.append(_restore_point(entry))
        except OSError as exc:
            logger.debug("Backup listing skipped %s: %s", entry.name, exc)
    return points


def restore_point(point_id: str, action: str = "restore") -> RestorePoint:
    if not isinstance(point_id, str) or not _BACKUP_DIR_RE.fullmatch(point_id):
        raise BackupRejected(action, MISSING, reason="not_found")
    path = _folder() / point_id
    if not _managed_backup_dir(path):
        raise BackupRejected(action, MISSING, reason="not_found")
    return _restore_point(path)


def remove_restore_point(point_id: str) -> None:
    """Remove exactly one restore point. Retention settings are never touched."""
    point = restore_point(point_id, "remove")
    try:
        shutil.rmtree(point.path)
    except OSError as exc:
        logger.warning("Could not remove backup %s: %s", point.id, exc)
        raise BackupRejected("remove") from None
    logger.info("Backup removed: %s", point.id)


# ── creation + retention ────────────────────────────────────────────────────

def _discard_private_residue(folder: Path) -> None:
    """Remove staging left by an interrupted run (callers hold the run lock),
    and Save Backup copies whose delivery was abandoned a day ago or more."""
    stale = datetime.now(timezone.utc).timestamp() - 86400
    for entry in folder.iterdir():
        if entry.is_symlink():
            continue
        if entry.name.startswith(_STAGING_PREFIX) and entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        elif entry.name.startswith(_PACKAGE_PREFIX) and entry.is_file():
            try:
                if entry.stat().st_mtime < stale:
                    entry.unlink()
            except OSError:
                pass


def _prepare_folder() -> Path:
    folder = _folder()
    folder.mkdir(parents=True, exist_ok=True)
    _chmod_private(folder, 0o700)
    _discard_private_residue(folder)
    return folder


def _write_manifest(directory: Path, payload: dict) -> None:
    manifest = directory / _MANIFEST_NAME
    manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _chmod_private(manifest, 0o600)


def _copy_private(source: Path, target: Path) -> None:
    shutil.copy2(source, target)
    _chmod_private(target, 0o600)


async def create_restore_point() -> RestorePoint:
    """THE creation of a restore point from current DebridPulse state.

    Run Backup, the scheduler, and the mandatory pre-restore and pre-reset
    safety backups all come here. The restore point is assembled privately,
    validated exactly as Add Backup would validate it, and only then renamed
    into place, so an incomplete backup is never visible. Raises
    ``BackupRejected`` on any failure."""
    async with _BACKUP_RUN_LOCK:
        return await _create_locked()


async def _create_locked() -> RestorePoint:
    from core.config import CONFIG_PATH, get_settings, save_settings
    from db.database import DB_PATH

    try:
        folder = _prepare_folder()
    except OSError as exc:
        logger.warning("Backup folder unavailable: %s", exc)
        raise BackupRejected("create") from None
    point_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}"
    staging = folder / f"{_STAGING_PREFIX}{uuid.uuid4().hex}"
    try:
        staging.mkdir(mode=0o700)
        if not CONFIG_PATH.exists():
            # A configuration that was never persisted is the effective one;
            # the canonical writer records it so the backup is complete.
            save_settings(get_settings())
        _copy_private(CONFIG_PATH, staging / _CONFIG_MEMBER)
        await asyncio.to_thread(_sqlite_backup, DB_PATH, staging / DB_PATH.name)
        files = [_CONFIG_MEMBER, DB_PATH.name]
        for ext in _AVATAR_EXTENSIONS:
            avatar = CONFIG_PATH.parent / f"avatar.{ext}"
            if avatar.exists():
                _copy_private(avatar, staging / avatar.name)
                files.append(avatar.name)
                break
        with closing(_open_frozen(staging / DB_PATH.name)) as conn:
            recorded = _fingerprint(conn)
        _write_manifest(staging, {
            "kind": _MANIFEST_KIND, "timestamp": point_id, "schema_version": recorded,
            "database": DB_PATH.name, "files": files, "errors": [],
        })
        await asyncio.to_thread(_validate_directory, staging, recorded, "create")
        target = folder / point_id
        os.rename(staging, target)
    except BackupRejected:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        logger.warning("Backup failed: %s", type(exc).__name__)
        raise BackupRejected("create") from None
    point = _restore_point(target)
    logger.info("Backup created: %s", point.id)
    return point


async def run_backup() -> dict:
    """Run Backup and the scheduled backup: honour the enable switch, create
    one restore point, then apply retention."""
    cfg = _cfg()
    if not cfg or not getattr(cfg, "backup_enabled", True):
        return {"skipped": True, "reason": "backup disabled"}
    point = await create_restore_point()
    keep_days = max(1, int(getattr(cfg, "backup_keep_days", 7)))
    async with _BACKUP_RUN_LOCK:
        removed = _rotate_backups(point.path.parent, keep_days)
    await journal_backup("administration.backup_created", "Backup created", point.id,
                   f"{removed} older backup(s) removed by retention" if removed else None)
    files = sorted(entry.name for entry in point.path.iterdir() if entry.name != _MANIFEST_NAME)
    return {
        "timestamp": point.id,
        "backup_dir": str(point.path),
        "backed_up": files,
        "errors": [],
        "rotated": removed,
        "restore_point": point.public(),
    }


def _rotate_backups(backup_folder: Path, keep_days: int) -> int:
    """Remove only managed DebridPulse backup directories older than keep_days."""
    removed = 0
    cutoff = datetime.now(timezone.utc).timestamp() - (keep_days * 86400)
    for entry in backup_folder.iterdir():
        if not _managed_backup_dir(entry):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry)
                removed += 1
                logger.debug("Rotated old backup: %s", entry.name)
        except Exception as e:
            logger.warning("Could not rotate backup %s: %s", entry.name, e)
    return removed


# ── the portable unit: Save Backup / Add Backup ─────────────────────────────

def package_name(point_id: str) -> str:
    return f"debridpulse-backup-{point_id}.zip"


def package_restore_point(point_id: str) -> Path:
    """Save Backup: the selected restore point as ONE portable file.

    The package is exactly the restore point's member files, unchanged, as the
    flat members of a zip. The caller streams it and deletes it."""
    point = restore_point(point_id, "save")
    target = point.path.parent / f"{_PACKAGE_PREFIX}{uuid.uuid4().hex}.zip"
    try:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as package:
            for entry in sorted(point.path.iterdir(), key=lambda item: item.name):
                if entry.is_file() and not entry.is_symlink():
                    package.write(entry, arcname=entry.name)
        _chmod_private(target, 0o600)
    except OSError as exc:
        target.unlink(missing_ok=True)
        logger.warning("Could not package backup %s: %s", point.id, exc)
        raise BackupRejected("save") from None
    return target


def _unpack(package: Path, destination: Path) -> None:
    """Copy a package's members into ``destination`` without trusting it.

    Nothing is extracted generically: every member must be a regular file with
    a bare name the manifest can own, and each member's CRC is verified as it
    is copied. There is no size limit of its own -- whatever Save Backup
    produced must be admissible again -- so the bound is the storage it lands
    on: the declared sizes must fit the Backup Folder's free space, and no
    member may yield more bytes than it declares."""
    try:
        archive = zipfile.ZipFile(package)
    except (zipfile.BadZipFile, OSError, ValueError):
        raise _invalid("add") from None
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if not infos or len(infos) > 8 or len(set(names)) != len(names) or _MANIFEST_NAME not in names:
            raise _invalid("add")
        total = 0
        for info in infos:
            name = info.filename
            mode = info.external_attr >> 16
            if (not name or "/" in name or "\\" in name or name in {".", ".."} or "\x00" in name
                    or info.is_dir() or info.flag_bits & 0x1
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                raise _invalid("add")
            total += info.file_size
        manifest_info = archive.getinfo(_MANIFEST_NAME)
        if manifest_info.file_size > _MAX_MANIFEST_BYTES:
            raise _invalid("add")
        try:
            manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
        except (ValueError, zipfile.BadZipFile, OSError):
            raise _invalid("add") from None
        if not isinstance(manifest, dict) or manifest.get("kind") != _MANIFEST_KIND:
            raise _invalid("add")
        if not set(names) <= _allowed_members(_manifest_facts(manifest, "add")):
            raise _invalid("add")
        if total > shutil.disk_usage(destination.parent).free:
            raise _no_space()
        destination.mkdir(mode=0o700)
        for info in infos:
            written = 0
            try:
                with archive.open(info) as source, open(destination / info.filename, "xb") as target:
                    while True:
                        chunk = source.read(_COPY_CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > info.file_size:
                            raise ValueError("member larger than declared")
                        target.write(chunk)
            except (zipfile.BadZipFile, OSError, ValueError, EOFError, RuntimeError):
                raise _invalid("add") from None
            _chmod_private(destination / info.filename, 0o600)


async def add_backup(chunks) -> RestorePoint:
    """Add Backup: validate a saved copy completely, then admit it.

    ``chunks`` is the offered package as an async iterable of bytes, taken
    straight from the request as it arrives. It is written to private staging
    only while it fits the Backup Folder's storage: the ingress budget is the
    free space there when the upload starts, and a chunk that would reach that
    budget -- or the space free at that moment -- is refused before it is
    written. The package is then unpacked member by member, validated exactly
    like every restore point, and only then renamed into the Backup Folder
    under its own identity. A rejected package leaves nothing behind."""
    from db.database import DB_PATH

    async with _BACKUP_RUN_LOCK:
        try:
            folder = _prepare_folder()
        except OSError as exc:
            logger.warning("Backup folder unavailable: %s", exc)
            raise BackupRejected("add") from None
        staging = folder / f"{_STAGING_PREFIX}{uuid.uuid4().hex}"
        try:
            staging.mkdir(mode=0o700)
            upload = staging / "package.zip"
            budget = shutil.disk_usage(staging).free
            written = 0
            with open(upload, "xb") as handle:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written >= budget or len(chunk) >= shutil.disk_usage(staging).free:
                        raise _no_space()
                    handle.write(chunk)
            unpacked = staging / "restore-point"
            await asyncio.to_thread(_unpack, upload, unpacked)
            live = await asyncio.to_thread(schema_version, DB_PATH)
            manifest = await asyncio.to_thread(_validate_directory, unpacked, live, "add")
            target = folder / manifest["timestamp"]
            if target.exists():
                raise BackupRejected("add", "This backup is already in Backups.", reason="duplicate")
            os.rename(unpacked, target)
        except BackupRejected:
            raise
        except Exception as exc:
            logger.warning("Add Backup failed: %s", type(exc).__name__)
            raise BackupRejected("add") from None
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    point = _restore_point(target)
    logger.info("Backup added: %s", point.id)
    await journal_backup("administration.backup_added", "Backup added", point.id)
    return point


# ── restore: stage, validate, journaled swap ────────────────────────────────

async def validate_restore_point(point_id: str) -> RestorePoint:
    """Validate a restore point for restore without disturbing anything."""
    from db.database import DB_PATH

    point = restore_point(point_id, "restore")
    live = await asyncio.to_thread(schema_version, DB_PATH)
    await asyncio.to_thread(_validate_directory, point.path, live, "restore")
    return point


def _retire_wal(db_path: Path) -> None:
    """Fold a database's write-ahead log into its main file and remove the
    sidecars, so the file alone is the complete database. Callers guarantee
    no other connection is open."""
    if db_path.exists():
        with closing(sqlite3.connect(str(db_path), timeout=30)) as conn:
            busy, _log, _checkpointed = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy:
                raise RuntimeError("database is still in use")
    for suffix in ("-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    from core.secure_files import fsync_parent_directory

    fsync_parent_directory(path / ".")


@dataclass
class StagedRestore:
    """A validated restored state waiting beside the live one.

    Every staged file sits in a private directory next to the live file it
    replaces, so each swap step is a same-filesystem rename. ``moves`` is the
    ordered rename plan; the journal records it before the first rename so an
    interrupted swap can always be reversed."""
    point: RestorePoint
    manifest: dict
    live_schema: str
    stage_dirs: tuple[Path, ...]
    database: Path
    config: Path
    avatar: Path | None
    moves: list[tuple[str, str]] = field(default_factory=list)
    # The staged copy was upgraded from the restore point's own schema.
    upgraded: bool = False

    def validate(self) -> None:
        # Exact, always: an upgraded copy is checked against the running schema
        # only -- the schema its restore point recorded is the one it left.
        _check_database(self.database, None if self.upgraded else self.manifest["schema_version"],
                        self.live_schema, "restore")
        _check_config(self.config, "restore")

    def _plan(self) -> list[tuple[str, str]]:
        from core.config import CONFIG_PATH
        from db.database import DB_PATH

        db_stage, config_stage = self.stage_dirs[0], self.stage_dirs[-1]
        displace, install = [], []
        live_avatars = [CONFIG_PATH.parent / f"avatar.{ext}" for ext in _AVATAR_EXTENSIONS]
        for live, stage in ((DB_PATH, db_stage), (CONFIG_PATH, config_stage),
                            *((avatar, config_stage) for avatar in live_avatars)):
            if live.exists():
                displace.append((str(live), str(stage / f"displaced-{live.name}")))
        install.append((str(self.database), str(DB_PATH)))
        install.append((str(self.config), str(CONFIG_PATH)))
        if self.avatar is not None:
            install.append((str(self.avatar), str(CONFIG_PATH.parent / self.avatar.name)))
        return displace + install

    def activate(self) -> None:
        """The swap. Callers hold database maintenance: no connection is open."""
        from core.secure_files import atomic_write_json
        from db.database import DB_PATH

        _retire_wal(DB_PATH)
        self.moves = self._plan()
        atomic_write_json(DB_PATH.parent / _JOURNAL_NAME, {
            "moves": self.moves, "stage_dirs": [str(path) for path in self.stage_dirs]})
        done = []
        try:
            for source, target in self.moves:
                os.replace(source, target)
                done.append((source, target))
            for directory in {Path(target).parent for _source, target in self.moves}:
                _fsync_directory(directory)
        except OSError:
            _reverse(done)
            self._discard()
            raise

    def rollback(self) -> None:
        """Reverse an activated swap. Callers hold database maintenance."""
        from db.database import DB_PATH

        _retire_wal(DB_PATH)
        _reverse(self.moves)
        self._discard()

    def commit(self) -> None:
        """The restored state stands: drop the displaced state and the journal."""
        self._discard()

    def discard(self) -> None:
        """Abandon a staged restore that was never activated."""
        self._discard()

    def _discard(self) -> None:
        from db.database import DB_PATH

        # The journal goes first: a journal must never outlive the staged and
        # displaced files its reversal would move back.
        (DB_PATH.parent / _JOURNAL_NAME).unlink(missing_ok=True)
        for directory in self.stage_dirs:
            shutil.rmtree(directory, ignore_errors=True)


def _reverse(moves) -> None:
    for source, target in reversed(list(moves)):
        if os.path.lexists(target) and not os.path.lexists(source):
            os.replace(target, source)


async def stage_restore(point_id: str) -> StagedRestore:
    """Revalidate a restore point and stage its complete state beside the
    live state. The Backup Folder location is not restored state: the staged
    configuration keeps the live one, so every restore point -- the safety
    backup included -- stays in Backups."""
    from core.config import CONFIG_PATH, get_settings
    from db.database import DB_PATH

    point = restore_point(point_id, "restore")
    live = await asyncio.to_thread(schema_version, DB_PATH)
    manifest = await asyncio.to_thread(_validate_directory, point.path, live, "restore")
    token = uuid.uuid4().hex
    db_stage = DB_PATH.parent / f"{_RESTORE_PREFIX}{token}"
    config_stage = CONFIG_PATH.parent / f"{_RESTORE_PREFIX}{token}"
    stage_dirs = (db_stage,) if db_stage == config_stage else (db_stage, config_stage)
    try:
        for directory in stage_dirs:
            directory.mkdir(mode=0o700)
        database = db_stage / f"restored-{DB_PATH.name}"
        await asyncio.to_thread(_copy_private, point.path / manifest["database"], database)
        upgraded = await asyncio.to_thread(_staged_schema, database) != live
        if upgraded:
            # The private copy only, through the one canonical bootstrap.
            from db import database as canonical
            await canonical.init_db(database)
            await asyncio.to_thread(_settle_staged, database)
        document = json.loads((point.path / _CONFIG_MEMBER).read_text(encoding="utf-8"))
        document["backup_folder"] = get_settings().backup_folder
        config = config_stage / "restored-config.json"
        config.write_text(json.dumps(document, indent=2), encoding="utf-8")
        _chmod_private(config, 0o600)
        avatar = None
        for name in manifest.get("files") or []:
            if name in _AVATAR_MEMBERS:
                avatar = config_stage / name
                _copy_private(point.path / name, avatar)
    except Exception as exc:
        for directory in stage_dirs:
            shutil.rmtree(directory, ignore_errors=True)
        logger.warning("Restore staging failed: %s", type(exc).__name__)
        raise BackupRejected("restore") from None
    return StagedRestore(point, manifest, live, stage_dirs, database, config, avatar, upgraded=upgraded)


def _staged_schema(path: Path) -> str:
    with closing(_open_frozen(path)) as conn:
        return _fingerprint(conn)


def _settle_staged(path: Path) -> None:
    """Fold an upgraded staged copy's write-ahead log into the file itself, so
    the one file that is validated and swapped in is complete."""
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def recover_interrupted_restore() -> bool:
    """Process start: reverse a swap whose restored startup never committed,
    and drop staging a restore abandoned before it swapped anything.

    Returns True when the pre-restore state was put back."""
    from core.config import CONFIG_PATH
    from db.database import DB_PATH

    journal = DB_PATH.parent / _JOURNAL_NAME
    reversed_swap = False
    if journal.exists():
        try:
            payload = json.loads(journal.read_text(encoding="utf-8"))
            moves = [(str(source), str(target)) for source, target in payload.get("moves") or []]
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError("An interrupted restore could not be reversed") from exc
        _retire_wal(DB_PATH)
        _reverse(moves)
        journal.unlink(missing_ok=True)
        reversed_swap = True
        logger.warning("An interrupted restore was reversed; the pre-restore state is active")
    for parent in {DB_PATH.parent, CONFIG_PATH.parent}:
        if parent.is_dir():
            for entry in parent.iterdir():
                if entry.name.startswith(_RESTORE_PREFIX) and entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
    return reversed_swap
