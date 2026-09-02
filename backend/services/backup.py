"""
Automatic backup service for DebridPulse.

Backs up config.json and the SQLite database to a configurable folder.
Rotation only removes directories that contain a DebridPulse ownership
manifest, so a broad or shared backup root cannot cause unrelated data loss.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("debridpulse.backup")

_BACKUP_DIR_RE = re.compile(r"^\d{8}_\d{6}(?:_[0-9a-f]{8}|_[0-9a-f]{32})?$")
_BACKUP_RUN_LOCK = asyncio.Lock()
_MANIFEST_NAME = ".debridpulse-backup.json"
_MANIFEST_KIND = "debridpulse-backup"


def _chmod_private(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _sqlite_backup(source: Path, destination: Path) -> None:
    with sqlite3.connect(str(source), timeout=30) as src:
        with sqlite3.connect(str(destination), timeout=30) as dst:
            src.backup(dst)
    _chmod_private(destination, 0o600)


def _cfg():
    try:
        from core.config import get_settings
        return get_settings()
    except Exception as exc:
        logger.warning("backup: could not read config: %s", exc)
        return None


def _write_manifest(backup_dir: Path, *, timestamp: str, backed_up: list[str], errors: list[str]) -> None:
    manifest = backup_dir / _MANIFEST_NAME
    payload = {
        "kind": _MANIFEST_KIND,
        "timestamp": timestamp,
        "files": list(backed_up),
        "errors": list(errors),
    }
    manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _chmod_private(manifest, 0o600)


def _managed_backup_dir(path: Path) -> bool:
    """Return True only for directories explicitly owned by this backup service."""
    if not path.is_dir() or not _BACKUP_DIR_RE.fullmatch(path.name):
        return False
    manifest = path / _MANIFEST_NAME
    if not manifest.is_file():
        return False
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("kind") == _MANIFEST_KIND


async def run_backup() -> dict:
    async with _BACKUP_RUN_LOCK:
        return await _run_backup_locked()


async def _run_backup_locked() -> dict:
    """
    Performs a single backup run. Returns a summary dict.
    Backup folder default: /app/data/backups
    """
    cfg = _cfg()
    if not cfg or not getattr(cfg, "backup_enabled", True):
        return {"skipped": True, "reason": "backup disabled"}

    backup_folder = Path(getattr(cfg, "backup_folder", "/app/data/backups"))
    keep_days = max(1, int(getattr(cfg, "backup_keep_days", 7)))

    backup_folder.mkdir(parents=True, exist_ok=True)
    _chmod_private(backup_folder, 0o700)
    ts = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}"
    backup_dir = backup_folder / ts
    backup_dir.mkdir(parents=True, exist_ok=False)
    _chmod_private(backup_dir, 0o700)

    backed_up: list[str] = []
    errors: list[str] = []

    # 1. config.json
    try:
        from core.config import CONFIG_PATH
        if CONFIG_PATH.exists():
            config_backup = backup_dir / "config.json"
            shutil.copy2(CONFIG_PATH, config_backup)
            _chmod_private(config_backup, 0o600)
            backed_up.append("config.json")
    except Exception as e:
        errors.append(f"config: {e}")

    # 2. SQLite database
    try:
        from db.database import DB_PATH
        if DB_PATH.exists():
            await asyncio.to_thread(_sqlite_backup, DB_PATH, backup_dir / DB_PATH.name)
            backed_up.append(DB_PATH.name)
    except Exception as e:
        errors.append(f"database: {e}")

    # 3. Uploaded avatar
    try:
        from core.config import CONFIG_PATH
        config_dir = CONFIG_PATH.parent
        for ext in ("png", "jpg", "gif", "webp"):
            p = config_dir / f"avatar.{ext}"
            if p.exists():
                target = backup_dir / p.name
                shutil.copy2(p, target)
                _chmod_private(target, 0o600)
                backed_up.append(p.name)
                break
    except Exception as e:
        errors.append(f"avatar: {e}")

    # The ownership manifest is written before rotation. Only directories with
    # this marker are ever eligible for recursive deletion.
    try:
        _write_manifest(backup_dir, timestamp=ts, backed_up=backed_up, errors=errors)
    except Exception as e:
        errors.append(f"manifest: {e}")

    # 4. Rotate old DebridPulse-owned backups only.
    removed = _rotate_backups(backup_folder, keep_days)

    result = {
        "timestamp": ts,
        "backup_dir": str(backup_dir),
        "backed_up": backed_up,
        "errors": errors,
        "rotated": removed,
    }
    if errors:
        logger.warning("Backup completed with errors: %s", errors)
    else:
        logger.info("Backup completed: %s (%d files)", ts, len(backed_up))
    return result


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


def list_backups() -> list:
    """Return managed DebridPulse backup entries, newest first."""
    cfg = _cfg()
    if not cfg:
        return []
    folder = Path(getattr(cfg, "backup_folder", "/app/data/backups"))
    if not folder.exists():
        return []
    entries = []
    for d in sorted(folder.iterdir(), reverse=True):
        if not _managed_backup_dir(d):
            continue
        try:
            files = [f.name for f in d.iterdir() if f.is_file() and f.name != _MANIFEST_NAME]
            size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
            entries.append({"name": d.name, "files": files, "size_bytes": size})
        except Exception as exc:
            logger.debug("Backup dir listing failed: %s", exc)
    return entries
