"""
Database maintenance helpers for explicit database backups and wipe operations.

Backups are exported as JSON snapshots of the authoritative SQLite database.
Rotation only removes directories carrying a DebridPulse database-backup
ownership manifest so a shared backup root cannot delete unrelated data.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import uuid
from datetime import date, datetime, time, timezone
from pathlib import Path

from core.config import get_settings
from db.database import get_db

logger = logging.getLogger("debridpulse.db_maintenance")

TABLES = [
    "torrents",
    "download_files",
    "events",
    "stats_snapshots",
    "transfer_pause_intents",
    "deferred_provider_submissions",
    "debridpulse_aria2_owned_gids",
    "transfer_controls",
    "transfer_requests",
    "provider_resources",
    "resolution_attempts",
    "execution_attempts",
    "transfer_outcomes",
    "postprocess_attempts",
    "application_events",
    "integration_runtime_state",
    "transfer_input_challenges",
    "schema_migrations",
]

_TABLE_ORDER = {
    "torrents": "id",
    "download_files": "id",
    "events": "id",
    "stats_snapshots": "id",
    "transfer_pause_intents": "torrent_id",
    "deferred_provider_submissions": "torrent_id",
    "debridpulse_aria2_owned_gids": "gid",
    "transfer_controls": "key",
    "transfer_requests": "id",
    "provider_resources": "id",
    "resolution_attempts": "id",
    "execution_attempts": "id",
    "transfer_outcomes": "id",
    "postprocess_attempts": "transfer_id,processor_id",
    "application_events": "id",
    "integration_runtime_state": "integration_id,state_key",
    "transfer_input_challenges": "transfer_id",
    "schema_migrations": "version",
}

_BACKUP_DIR_RE = re.compile(r"^\d{8}_\d{6}(?:_[0-9a-f]{8}|_[0-9a-f]{32})?$")
_BACKUP_RUN_LOCK = asyncio.Lock()
_MANIFEST_NAME = ".debridpulse-db-backup.json"
_MANIFEST_KIND = "debridpulse-database-backup"


def _chmod_private(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _folder() -> Path:
    cfg = get_settings()
    return Path(getattr(cfg, "db_backup_folder", "/app/data/db-backups"))


def _keep_days() -> int:
    cfg = get_settings()
    return max(1, int(getattr(cfg, "db_backup_keep_days", 7) or 7))


def _json_default(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"__base64__": base64.b64encode(value).decode("ascii")}
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _write_manifest(backup_dir: Path, *, timestamp: str, errors: list[str]) -> None:
    manifest = backup_dir / _MANIFEST_NAME
    manifest.write_text(
        json.dumps(
            {
                "kind": _MANIFEST_KIND,
                "timestamp": timestamp,
                "errors": list(errors),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _chmod_private(manifest, 0o600)


def _managed_backup_dir(path: Path) -> bool:
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


async def run_database_backup() -> dict:
    async with _BACKUP_RUN_LOCK:
        return await _run_database_backup_locked()


async def _run_database_backup_locked() -> dict:
    cfg = get_settings()
    if not getattr(cfg, "db_backup_enabled", True):
        return {"skipped": True, "reason": "database backup disabled"}

    backup_folder = _folder()
    backup_folder.mkdir(parents=True, exist_ok=True)
    _chmod_private(backup_folder, 0o700)
    ts = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}"
    backup_dir = backup_folder / ts
    backup_dir.mkdir(parents=True, exist_ok=False)
    _chmod_private(backup_dir, 0o700)

    payload = {
        "timestamp": ts,
        "db_type": "sqlite",
        "tables": {},
    }
    errors: list[str] = []

    try:
        async with get_db() as db:
            await db.execute("BEGIN")
            present = {row["name"] for row in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in TABLES:
                if table not in present:
                    continue  # A pre-upgrade backup contains only its actual schema.
                order_key = _TABLE_ORDER[table]
                rows = await db.fetchall(f"SELECT * FROM {table} ORDER BY {order_key}")
                payload["tables"][table] = rows
    except Exception as exc:
        errors.append(f"export: {exc}")

    json_path = backup_dir / "database.json"
    if not errors:
        try:
            json_path.write_text(
                json.dumps(payload, indent=2, default=_json_default), encoding="utf-8"
            )
            _chmod_private(json_path, 0o600)
        except Exception as exc:
            errors.append(f"write: {exc}")

    try:
        _write_manifest(backup_dir, timestamp=ts, errors=errors)
    except Exception as exc:
        errors.append(f"manifest: {exc}")

    removed = _rotate_old_backups(backup_folder, _keep_days())
    result = {
        "timestamp": ts,
        "backup_dir": str(backup_dir),
        "file": str(json_path),
        "tables": {table: len(payload["tables"].get(table, [])) for table in TABLES},
        "errors": errors,
        "rotated": removed,
    }
    if errors:
        logger.warning("Database backup completed with errors: %s", errors)
    else:
        logger.info("Database backup completed: %s", ts)
    return result


def _rotate_old_backups(folder: Path, keep_days: int) -> int:
    removed = 0
    cutoff = datetime.now(timezone.utc).timestamp() - (keep_days * 86400)
    for entry in folder.iterdir():
        if not _managed_backup_dir(entry):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry)
                removed += 1
        except Exception as exc:
            logger.warning("Could not rotate DB backup %s: %s", entry.name, exc)
    return removed


def list_database_backups() -> list[dict]:
    folder = _folder()
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
            logger.debug("Database backup dir listing failed for %s: %s", d.name, exc)
    return entries


async def wipe_database(*, verified_quiesced: bool = False) -> dict:
    if not verified_quiesced:
        raise RuntimeError("Database wipe requires verified quiesced transfer state")
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        # Operational integration state is application database state, not user
        # configuration. An explicit whole-database wipe deliberately purges it;
        # ordinary integration disablement never reaches this path.
        await db.execute("DELETE FROM integration_runtime_state")
        await db.execute("DELETE FROM transfer_input_challenges")
        for table in ("application_events", "postprocess_attempts", "transfer_outcomes", "execution_attempts", "resolution_attempts", "provider_resources"):
            await db.execute(f"DELETE FROM {table}")
        await db.execute("DELETE FROM transfer_requests WHERE parent_id IS NOT NULL")
        await db.execute("DELETE FROM transfer_requests")
        await db.execute("DELETE FROM debridpulse_aria2_owned_gids")
        await db.execute("DELETE FROM transfer_pause_intents")
        await db.execute("DELETE FROM deferred_provider_submissions")
        await db.execute("DELETE FROM download_files")
        await db.execute("DELETE FROM events")
        await db.execute("DELETE FROM stats_snapshots")
        await db.execute("DELETE FROM torrents")
        try:
            await db.execute(
                "DELETE FROM sqlite_sequence WHERE name IN ('torrents','download_files','events','stats_snapshots')"
            )
        except Exception as exc:
            logger.debug("sqlite_sequence reset skipped: %s", exc)
        await db.commit()

    logger.warning("Database wipe completed")
    return {"ok": True, "wiped_tables": [table for table in TABLES if table not in {"transfer_controls", "schema_migrations"}]}


async def cleanup_old_events(keep_days: int = 30) -> dict:
    """Delete events older than ``keep_days`` days.

    IMPORTANT: Only the *events* table is pruned — torrents and download_files
    are never touched. Old events are audit-log entries; their removal does not
    affect torrent state, duplicate-prevention logic, or download tracking.
    """
    keep_days = max(1, int(keep_days))
    cutoff_expr = f"datetime('now', '-{keep_days} days')"

    async with get_db() as db:
        result = await db.execute(
            f"DELETE FROM events WHERE created_at < {cutoff_expr}"
        )
        try:
            deleted = result.rowcount if hasattr(result, "rowcount") else -1
        except Exception:
            deleted = -1
        await db.commit()

    if deleted > 0:
        logger.info("Events TTL cleanup: deleted %d event(s) older than %d days", deleted, keep_days)
    else:
        logger.debug("Events TTL cleanup: no events older than %d days", keep_days)

    return {"deleted": deleted, "keep_days": keep_days}
