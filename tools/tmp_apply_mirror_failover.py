from pathlib import Path


def replace_once(path: str, old: str, new: str, label: str) -> None:
    p = Path(path)
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    p.write_text(text.replace(old, new, 1))


# ---- database: persist logical mirror group + source lifecycle ----
replace_once(
    "backend/db/database.py",
    '''_SCHEMA_COLUMNS_FILES = [
    ("source_url", "TEXT"),
    ("download_id", "TEXT"),
    ("download_client", "TEXT DEFAULT 'aria2'"),
    ("retry_count", "INTEGER DEFAULT 0"),
    ("updated_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
]
''',
    '''_SCHEMA_COLUMNS_FILES = [
    ("source_url", "TEXT"),
    ("download_id", "TEXT"),
    ("download_client", "TEXT DEFAULT 'aria2'"),
    ("retry_count", "INTEGER DEFAULT 0"),
    ("mirror_group_id", "INTEGER"),
    ("mirror_state", "TEXT DEFAULT ''"),
    ("updated_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
]
''',
    "download_files migration columns",
)
replace_once(
    "backend/db/database.py",
    '''                block_reason TEXT,
                retry_count INTEGER DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
''',
    '''                block_reason TEXT,
                retry_count INTEGER DEFAULT 0,
                mirror_group_id INTEGER,
                mirror_state TEXT DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
''',
    "download_files create columns",
)
replace_once(
    "backend/db/database.py",
    '''            "CREATE INDEX IF NOT EXISTS idx_dlfiles_download_id ON download_files (download_id)",
            "CREATE INDEX IF NOT EXISTS idx_torrents_alldebrid_id ON torrents (alldebrid_id)",
''',
    '''            "CREATE INDEX IF NOT EXISTS idx_dlfiles_download_id ON download_files (download_id)",
            "CREATE INDEX IF NOT EXISTS idx_dlfiles_mirror_group ON download_files (torrent_id, mirror_group_id, mirror_state, status)",
            "CREATE INDEX IF NOT EXISTS idx_torrents_alldebrid_id ON torrents (alldebrid_id)",
''',
    "mirror group index",
)


# ---- mirror collapse: duplicates are persisted failover standbys ----
replace_once(
    "backend/services/dispatch_coordinator.py",
    '''            """SELECT f.id AS file_id, f.torrent_id, f.filename,
                      f.size_bytes, f.source_url, f.status, f.download_id
''',
    '''            """SELECT f.id AS file_id, f.torrent_id, f.filename,
                      f.size_bytes, f.source_url, f.status, f.download_id,
                      f.mirror_group_id, f.mirror_state
''',
    "mirror query lifecycle fields",
)
replace_once(
    "backend/services/dispatch_coordinator.py",
    '''            cursor = await db.execute(
                """UPDATE download_files
                      SET status='duplicate', blocked=NULL, block_reason=?,
                          download_url=NULL, local_path=NULL,
                          updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                      AND status IN ('pending','queued','paused')
                      AND blocked=0 AND download_id IS NULL""",
                (reason, int(duplicate["file_id"])),
            )
''',
    '''            group_id = int(primary.get("mirror_group_id") or primary["file_id"])
            await db.execute(
                """UPDATE download_files
                      SET mirror_group_id=?,
                          mirror_state=CASE
                              WHEN COALESCE(mirror_state, '') IN ('', 'standby') THEN 'active'
                              ELSE mirror_state
                          END,
                          updated_at=CURRENT_TIMESTAMP
                    WHERE id=?""",
                (group_id, int(primary["file_id"])),
            )
            cursor = await db.execute(
                """UPDATE download_files
                      SET status='duplicate', blocked=NULL, block_reason=?,
                          mirror_group_id=?, mirror_state='standby',
                          download_url=NULL, local_path=NULL,
                          updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                      AND status IN ('pending','queued','paused')
                      AND blocked=0 AND download_id IS NULL""",
                (reason, group_id, int(duplicate["file_id"])),
            )
''',
    "persist mirror standby relationship",
)
replace_once(
    "backend/services/dispatch_coordinator.py",
    '''                    f"Classified {classified} cross-hoster mirror link(s) as "
                    f"duplicates for {logical_files} logical file(s); one copy will be downloaded",
''',
    '''                    f"Classified {classified} cross-hoster mirror link(s) as "
                    f"duplicates for {logical_files} logical file(s); one copy will be downloaded; "
                    "alternates retained as automatic failover standbys",
''',
    "mirror classification event",
)


# ---- dispatcher: tag source-unlock failures and revalidate failover metadata ----
replace_once(
    "backend/services/manager_v2.py",
    '''                        """SELECT f.id AS file_id, f.torrent_id, f.filename,
                                  f.source_url, f.download_url, f.local_path,
                                  t.name AS torrent_name, t.source AS transfer_source
''',
    '''                        """SELECT f.id AS file_id, f.torrent_id, f.filename,
                                  f.size_bytes, f.source_url, f.download_url, f.local_path,
                                  f.mirror_group_id, f.mirror_state,
                                  t.name AS torrent_name, t.source AS transfer_source
''',
    "dispatch mirror metadata query",
)
replace_once(
    "backend/services/manager_v2.py",
    '''                        result = await _retry_async(self.ad().unlock_link, sl)
                        dl_url = result.get("link", "")
                        if not dl_url:
                            raise Exception("Empty download URL from unlock")
                        return {**row_, "_dl_url": dl_url, "_err": None}
''',
    '''                        result = await _retry_async(self.ad().unlock_link, sl)
                        dl_url = result.get("link", "")
                        if not dl_url:
                            raise Exception("Empty download URL from unlock")
                        if (
                            row_.get("transfer_source") == DIRECT_LINK_SOURCE
                            and row_.get("mirror_group_id") is not None
                            and str(row_.get("mirror_state") or "") == "active"
                        ):
                            from services.dispatch_coordinator import _mirror_sizes_match

                            resolved_name = safe_name(str(
                                result.get("filename")
                                or result.get("name")
                                or row_.get("filename")
                                or ""
                            ))
                            resolved_size = int(
                                result.get("filesize")
                                or result.get("size")
                                or row_.get("size_bytes")
                                or 0
                            )
                            if (
                                resolved_name.casefold()
                                != str(row_.get("filename") or "").strip().casefold()
                                or not _mirror_sizes_match(
                                    resolved_size,
                                    int(row_.get("size_bytes") or 0),
                                )
                            ):
                                raise Exception(
                                    "Failover source no longer matches the validated mirror artifact"
                                )
                        return {**row_, "_dl_url": dl_url, "_err": None}
''',
    "dispatch mirror revalidation",
)
replace_once(
    "backend/services/manager_v2.py",
    '''                    if (
                        provider_code == "LINK_HOST_NOT_SUPPORTED"
                        or "LINK_HOST_NOT_SUPPORTED" in error_text
                    ):
                        logger.warning(
                            "aria2 dispatch blocked unsupported provider file [%s]: %s",
                            row["filename"],
                            error_text,
                        )
                        async with get_db() as db:
                            await db.execute(
                                """UPDATE download_files
                                   SET status='blocked', blocked=1, block_reason=?,
                                       download_id=NULL, updated_at=CURRENT_TIMESTAMP
                                   WHERE id=?""",
                                (error_text, row["file_id"]),
                            )
                            await db.commit()
                    else:
                        logger.error(
                            "aria2 dispatch failed [%s]: %s",
                            row["filename"],
                            error_text,
                        )
                        await self._update_file_state(
                            row["file_id"],
                            "error",
                            row["local_path"],
                            reason=error_text,
                        )
''',
    '''                    if row.get("transfer_source") == DIRECT_LINK_SOURCE:
                        logger.warning(
                            "direct-link source unlock failed [%s]: %s",
                            row["filename"],
                            error_text,
                        )
                        await self._update_file_state(
                            row["file_id"],
                            "error",
                            row["local_path"],
                            reason=f"source-unlock: {error_text}",
                        )
                    elif (
                        provider_code == "LINK_HOST_NOT_SUPPORTED"
                        or "LINK_HOST_NOT_SUPPORTED" in error_text
                    ):
                        logger.warning(
                            "aria2 dispatch blocked unsupported provider file [%s]: %s",
                            row["filename"],
                            error_text,
                        )
                        async with get_db() as db:
                            await db.execute(
                                """UPDATE download_files
                                   SET status='blocked', blocked=1, block_reason=?,
                                       download_id=NULL, updated_at=CURRENT_TIMESTAMP
                                   WHERE id=?""",
                                (error_text, row["file_id"]),
                            )
                            await db.commit()
                    else:
                        logger.error(
                            "aria2 dispatch failed [%s]: %s",
                            row["filename"],
                            error_text,
                        )
                        await self._update_file_state(
                            row["file_id"],
                            "error",
                            row["local_path"],
                            reason=error_text,
                        )
''',
    "direct-link unlock failure tagging",
)
replace_once(
    "backend/services/manager_v2.py",
    '''                    await self._update_file_state(
                        row["file_id"], "error", row["local_path"], reason=safe_error
                    )
''',
    '''                    await self._update_file_state(
                        row["file_id"],
                        "error",
                        row["local_path"],
                        reason=(
                            f"aria2-dispatch: {safe_error}"
                            if row.get("transfer_source") == DIRECT_LINK_SOURCE
                            else safe_error
                        ),
                    )
''',
    "direct-link local dispatch failure tagging",
)


# ---- result authority: promote standby sources before parent failure ----
replace_once(
    "backend/services/direct_link_result_guard.py",
    '''from core.config import get_settings
from db.database import get_db
''',
    '''from core.config import get_settings
from core.logging_utils import sanitize_log_value
from db.database import get_db
''',
    "result guard log sanitizer import",
)
replace_once(
    "backend/services/direct_link_result_guard.py",
    '''_FALSE_ARIA2_FAILURE = "One or more aria2 transfers failed"
_ACTIVE_DIRECT_LINK_PATHS: ContextVar[frozenset[str]] = ContextVar(
''',
    '''_FALSE_ARIA2_FAILURE = "One or more aria2 transfers failed"
_FAILOVER_ARIA2_ERROR_CODES = frozenset({
    "2",   # timeout
    "3",   # resource not found
    "4",   # max resource-not-found threshold
    "5",   # remote source too slow
    "6",   # network problem
    "8",   # remote server cannot resume
    "19",  # name resolution failure
    "21",  # FTP command failure
    "22",  # bad/unexpected HTTP response header
    "23",  # redirect loop
    "24",  # HTTP authorization failure / expired capability
    "29",  # remote overload / maintenance
    "32",  # checksum validation failure
})
_ACTIVE_DIRECT_LINK_PATHS: ContextVar[frozenset[str]] = ContextVar(
''',
    "failover aria2 codes",
)
insert_anchor = '''    async def _direct_link_completion_state(self, torrent_id: int):
'''
insert_code = '''    @staticmethod
    def _persisted_mirror_failure_code(reason: object) -> str:
        prefix = str(reason or "").strip().split(":", 1)[0].strip()
        return prefix if prefix.isdigit() else ""

    async def _mirror_failure_is_failover_eligible(
        self, row: dict
    ) -> tuple[bool, str]:
        """Classify only failures that another upstream source can plausibly fix."""
        persisted = str(row.get("block_reason") or "").strip()
        if persisted.startswith("source-unlock:"):
            reason = persisted.split(":", 1)[1].strip() or "source unlock failed"
            return True, reason
        if persisted.startswith("aria2-dispatch:"):
            reason = persisted.split(":", 1)[1].strip() or "local aria2 dispatch failed"
            return False, reason

        code = self._persisted_mirror_failure_code(persisted)
        if code:
            return code in _FAILOVER_ARIA2_ERROR_CODES, persisted

        gid = str(row.get("download_id") or "").strip()
        if not gid:
            return False, persisted or "unclassified pre-dispatch failure"
        try:
            state = await self.aria2().tell_status(gid)
        except Exception as exc:
            return False, sanitize_log_value(
                f"Could not confirm aria2 source failure: {exc}",
                max_length=220,
            )
        if str(getattr(state, "status", "") or "") != "error":
            return False, persisted or f"aria2 state is {getattr(state, 'status', '')}"

        code = str(getattr(state, "error_code", "") or "").strip()
        raw_reason = f"{code}: {getattr(state, 'error_message', '')}".strip(": ")
        capability = str(row.get("download_url") or "").strip()
        if capability:
            raw_reason = raw_reason.replace(capability, "<capability-url>")
        safe_reason = sanitize_log_value(raw_reason, max_length=220)
        return code in _FAILOVER_ARIA2_ERROR_CODES, safe_reason

    async def _clear_failed_mirror_target(self, row: dict) -> tuple[bool, str]:
        """Discard partial state before switching hosters; never mix source bytes."""
        gid = str(row.get("download_id") or "").strip()
        if gid and is_builtin_mode():
            try:
                await self._remove_owned_aria2_gid(gid)
            except Exception as exc:
                return False, sanitize_log_value(
                    f"Could not retire failed aria2 job {gid}: {exc}",
                    max_length=220,
                )

        local_path = str(row.get("local_path") or "").strip()
        if not local_path:
            return True, ""
        target = Path(local_path)
        for candidate in (Path(f"{target}.aria2"), target):
            try:
                if not candidate.exists() and not candidate.is_symlink():
                    continue
                if candidate.is_dir() and not candidate.is_symlink():
                    return False, f"Refusing to remove directory during mirror failover: {candidate}"
                candidate.unlink()
            except OSError as exc:
                return False, sanitize_log_value(
                    f"Could not clear failed mirror target {candidate}: {exc}",
                    max_length=220,
                )
        return True, ""

    async def _promote_direct_link_mirror_failover(self, torrent_id: int) -> bool:
        """Promote one standby per failed logical artifact before parent failure."""
        torrent_id = int(torrent_id)
        async with get_db() as db:
            failures = await db.fetchall(
                """SELECT f.id AS file_id, f.torrent_id, f.filename, f.size_bytes,
                          f.source_url, f.download_url, f.local_path, f.download_id,
                          f.block_reason, f.mirror_group_id, f.mirror_state,
                          t.name AS torrent_name, t.progress AS torrent_progress
                     FROM download_files f
                     JOIN torrents t ON t.id=f.torrent_id
                    WHERE t.id=? AND t.source=?
                      AND f.blocked=0 AND f.status='error'
                      AND f.mirror_group_id IS NOT NULL
                      AND f.mirror_state='active'
                    ORDER BY f.id ASC""",
                (torrent_id, DIRECT_LINK_SOURCE),
            )

        if not failures:
            return False

        from services.dispatch_coordinator import _source_host

        promoted_any = False
        for failed in failures:
            group_id = int(failed.get("mirror_group_id") or failed["file_id"])
            async with get_db() as db:
                standbys = await db.fetchall(
                    """SELECT id AS file_id, filename, size_bytes, source_url,
                              mirror_group_id, mirror_state
                         FROM download_files
                        WHERE torrent_id=? AND mirror_group_id=?
                          AND mirror_state='standby' AND status='duplicate'
                        ORDER BY id ASC""",
                    (torrent_id, group_id),
                )
            if not standbys:
                await self._log_event(
                    torrent_id,
                    "error",
                    f"Mirror failover exhausted for {failed['filename']!r}; no standby source remains",
                )
                continue

            eligible, failure_reason = await self._mirror_failure_is_failover_eligible(failed)
            failed_host = _source_host(failed.get("source_url")) or "active source"
            if not eligible:
                await self._log_event(
                    torrent_id,
                    "warn",
                    f"Mirror failover not attempted for {failed_host}: local/system failure · {failure_reason}",
                )
                continue

            cleared, clear_reason = await self._clear_failed_mirror_target(failed)
            if not cleared:
                await self._log_event(
                    torrent_id,
                    "error",
                    f"Mirror failover could not prepare a clean target for {failed['filename']!r}: {clear_reason}",
                )
                continue

            promoted = standbys[0]
            promoted_host = _source_host(promoted.get("source_url")) or "standby source"
            async with get_db() as db:
                candidate_update = await db.execute(
                    """UPDATE download_files
                          SET status='pending', blocked=0, block_reason=NULL,
                              download_url=NULL, local_path=?, download_id=NULL,
                              retry_count=0, mirror_state='active',
                              updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND torrent_id=? AND mirror_group_id=?
                          AND mirror_state='standby' AND status='duplicate'""",
                    (
                        str(failed.get("local_path") or ""),
                        int(promoted["file_id"]),
                        torrent_id,
                        group_id,
                    ),
                )
                if int(getattr(candidate_update, "rowcount", 0) or 0) <= 0:
                    await db.commit()
                    continue

                await db.execute(
                    """UPDATE download_files
                          SET blocked=NULL, mirror_state='exhausted',
                              download_url=NULL, block_reason=?,
                              updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND torrent_id=?
                          AND mirror_group_id=? AND mirror_state='active'
                          AND status='error'""",
                    (failure_reason, int(failed["file_id"]), torrent_id, group_id),
                )
                await db.execute(
                    """UPDATE torrents
                          SET status='queued',
                              error_message=CASE
                                  WHEN error_message=? THEN NULL
                                  ELSE error_message
                              END,
                              updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status!='deleted'""",
                    (_FALSE_ARIA2_FAILURE, torrent_id),
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'warn', ?)",
                    (
                        torrent_id,
                        f"Mirror source exhausted: {failed_host} · {failure_reason}",
                    ),
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                    (
                        torrent_id,
                        f"Mirror failover: promoted {promoted_host} standby for {failed['filename']}",
                    ),
                )
                await db.commit()

            logger.info(
                "direct-link mirror failover: transfer=%s file=%s failed_host=%s promoted_host=%s",
                torrent_id,
                str(failed.get("filename") or "")[:120],
                failed_host,
                promoted_host,
            )
            await self._broadcast_direct_link_update(
                torrent_id,
                "queued",
                str(failed.get("torrent_name") or "Debrid links"),
                float(failed.get("torrent_progress") or 0.0),
            )
            promoted_any = True

        if promoted_any:
            self._track_maintenance_task(
                self.advance_aria2_queue(),
                label=f"mirror-failover-dispatch-{torrent_id}",
            )
        return promoted_any

'''
replace_once(
    "backend/services/direct_link_result_guard.py",
    insert_anchor,
    insert_code + insert_anchor,
    "mirror failover methods",
)
replace_once(
    "backend/services/direct_link_result_guard.py",
    '''                       SUM(CASE WHEN local_path IS NULL AND download_id IS NULL AND status IN ('error','missing') THEN 1 ELSE 0 END) AS source_failure_count,
                       SUM(CASE WHEN status='duplicate' THEN 1 ELSE 0 END) AS duplicate_count
''',
    '''                       SUM(CASE WHEN local_path IS NULL AND download_id IS NULL
                                           AND status IN ('error','missing')
                                           AND COALESCE(mirror_state, '')!='exhausted'
                                      THEN 1 ELSE 0 END) AS source_failure_count,
                       SUM(CASE WHEN status='duplicate' THEN 1 ELSE 0 END) AS duplicate_count,
                       SUM(CASE WHEN mirror_state='exhausted' THEN 1 ELSE 0 END) AS mirror_exhausted_count,
                       SUM(CASE WHEN mirror_state='standby' AND status='duplicate' THEN 1 ELSE 0 END) AS mirror_standby_count
''',
    "mirror-aware result counts",
)
replace_once(
    "backend/services/direct_link_result_guard.py",
    '''            duplicates = int(counts.get("duplicate_count") or 0)

            current_error = str(parent.get("error_message") or "").strip()
''',
    '''            duplicates = int(counts.get("duplicate_count") or 0)
            mirror_exhausted = int(counts.get("mirror_exhausted_count") or 0)
            mirror_standbys = int(counts.get("mirror_standby_count") or 0)

            current_error = str(parent.get("error_message") or "").strip()
''',
    "mirror completion counters",
)
replace_once(
    "backend/services/direct_link_result_guard.py",
    '''                details = [f"aria2 completed {completed_count} logical file(s)"]
                if source_failures:
                    details.append(f"{source_failures} source link(s) unavailable")
                if duplicates:
                    details.append(f"{duplicates} duplicate mirror(s) suppressed")
                await db.execute(
''',
    '''                await db.execute(
                    """UPDATE download_files
                          SET mirror_state='unused', updated_at=CURRENT_TIMESTAMP
                        WHERE torrent_id=? AND mirror_state='standby'
                          AND status='duplicate'""",
                    (torrent_id,),
                )
                details = [f"aria2 completed {completed_count} logical file(s)"]
                if source_failures:
                    details.append(f"{source_failures} source link(s) unavailable")
                if mirror_exhausted:
                    details.append(
                        f"{mirror_exhausted} mirror source(s) exhausted during failover"
                    )
                if duplicates:
                    details.append(f"{duplicates} duplicate mirror(s) suppressed")
                if mirror_standbys:
                    details.append(f"{mirror_standbys} unused standby mirror(s)")
                await db.execute(
''',
    "finalize mirror lifecycle",
)
replace_once(
    "backend/services/direct_link_result_guard.py",
    '''        if await self._complete_direct_link_result(torrent_id):
            return

        async with get_db() as db:
''',
    '''        if await self._complete_direct_link_result(torrent_id):
            return

        # A failed active mirror is a source attempt, not yet a failed artifact.
        # Promote a validated standby before the inherited parent-error path runs.
        if await self._promote_direct_link_mirror_failover(torrent_id):
            return

        async with get_db() as db:
''',
    "failover before terminal parent failure",
)
replace_once(
    "backend/services/direct_link_result_guard.py",
    '''        await self._normalize_direct_link_source_outcomes()
        result = await super().reconcile_aria2_on_startup()
''',
    '''        await self._normalize_direct_link_source_outcomes()
        async with get_db() as db:
            failover_candidates = await db.fetchall(
                """SELECT DISTINCT t.id
                     FROM torrents t
                     JOIN download_files f ON f.torrent_id=t.id
                    WHERE t.source=? AND t.status!='deleted'
                      AND f.blocked=0 AND f.status='error'
                      AND f.mirror_group_id IS NOT NULL
                      AND f.mirror_state='active'""",
                (DIRECT_LINK_SOURCE,),
            )
        for row in failover_candidates:
            await self._promote_direct_link_mirror_failover(int(row["id"]))
        result = await super().reconcile_aria2_on_startup()
''',
    "startup mirror failover recovery",
)


# ---- focused regressions ----
Path("backend/tests/test_direct_link_mirror_failover.py").write_text(r'''import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db.database as database
import services.direct_link_result_guard as result_guard
from services.direct_link_result_guard import DirectLinkResultGuardManager


async def _prepare_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "mirror-failover.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()
    return db_path


def _read_one(db_path: Path, sql: str, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _read_all(db_path: Path, sql: str, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _insert_failover_group(db_path: Path, *, reason="3: Resource not found", extra_standby=True):
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO torrents
               (hash, name, status, source, download_client, provider_status,
                size_bytes, progress)
               VALUES ('direct:failover', 'archive.rar (3 links)', 'downloading',
                       'direct_link', 'aria2', 'ready', 1000, 61.0)"""
        )
        torrent_id = int(cur.lastrowid)
        primary = conn.execute(
            """INSERT INTO download_files
               (torrent_id, filename, size_bytes, source_url, download_url,
                local_path, status, download_id, download_client, blocked,
                block_reason, mirror_group_id, mirror_state)
               VALUES (?, 'archive.rar', 1000, 'https://one.example/a',
                       'https://capability.invalid/a', ?, 'error', 'gid-a',
                       'aria2', 0, ?, NULL, 'active')""",
            (torrent_id, str(Path(db_path).parent / "archive.rar"), reason),
        )
        primary_id = int(primary.lastrowid)
        conn.execute(
            "UPDATE download_files SET mirror_group_id=? WHERE id=?",
            (primary_id, primary_id),
        )
        standby_ids = []
        for host in (["two.example", "three.example"] if extra_standby else ["two.example"]):
            row = conn.execute(
                """INSERT INTO download_files
                   (torrent_id, filename, size_bytes, source_url, status,
                    download_client, blocked, block_reason,
                    mirror_group_id, mirror_state)
                   VALUES (?, 'archive.rar', 1000, ?, 'duplicate',
                           'aria2', NULL, 'validated standby', ?, 'standby')""",
                (torrent_id, f"https://{host}/a", primary_id),
            )
            standby_ids.append(int(row.lastrowid))
        conn.commit()
        return torrent_id, primary_id, standby_ids
    finally:
        conn.close()


def _disable_async_dispatch(manager):
    def discard(coro, *, label):
        coro.close()
        return None
    manager._track_maintenance_task = discard


@pytest.mark.asyncio
async def test_source_specific_failure_promotes_first_standby(tmp_path, monkeypatch):
    db_path = await _prepare_db(tmp_path, monkeypatch)
    torrent_id, primary_id, standby_ids = _insert_failover_group(db_path)
    target = Path(db_path).parent / "archive.rar"
    target.write_bytes(b"partial")
    Path(f"{target}.aria2").write_bytes(b"control")

    manager = DirectLinkResultGuardManager()
    _disable_async_dispatch(manager)
    manager._broadcast_direct_link_update = AsyncMock()
    monkeypatch.setattr(result_guard, "is_builtin_mode", lambda: False)

    promoted = await manager._promote_direct_link_mirror_failover(torrent_id)

    assert promoted is True
    assert not target.exists()
    assert not Path(f"{target}.aria2").exists()

    primary = _read_one(
        db_path,
        "SELECT status, blocked, download_url, mirror_state FROM download_files WHERE id=?",
        (primary_id,),
    )
    assert primary == {
        "status": "error",
        "blocked": None,
        "download_url": None,
        "mirror_state": "exhausted",
    }
    promoted_row = _read_one(
        db_path,
        "SELECT status, blocked, local_path, download_id, mirror_state FROM download_files WHERE id=?",
        (standby_ids[0],),
    )
    assert promoted_row == {
        "status": "pending",
        "blocked": 0,
        "local_path": str(target),
        "download_id": None,
        "mirror_state": "active",
    }
    untouched = _read_one(
        db_path,
        "SELECT status, blocked, mirror_state FROM download_files WHERE id=?",
        (standby_ids[1],),
    )
    assert untouched == {
        "status": "duplicate",
        "blocked": None,
        "mirror_state": "standby",
    }
    parent = _read_one(
        db_path,
        "SELECT status, error_message FROM torrents WHERE id=?",
        (torrent_id,),
    )
    assert parent == {"status": "queued", "error_message": None}
    events = _read_all(
        db_path,
        "SELECT level, message FROM events WHERE torrent_id=? ORDER BY id",
        (torrent_id,),
    )
    assert any("Mirror source exhausted: one.example" in row["message"] for row in events)
    assert any("Mirror failover: promoted two.example standby" in row["message"] for row in events)
    manager._broadcast_direct_link_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_disk_failure_does_not_cycle_standbys(tmp_path, monkeypatch):
    db_path = await _prepare_db(tmp_path, monkeypatch)
    torrent_id, primary_id, standby_ids = _insert_failover_group(
        db_path,
        reason="9: Not enough disk space available",
        extra_standby=False,
    )
    manager = DirectLinkResultGuardManager()
    _disable_async_dispatch(manager)
    monkeypatch.setattr(result_guard, "is_builtin_mode", lambda: False)

    promoted = await manager._promote_direct_link_mirror_failover(torrent_id)

    assert promoted is False
    primary = _read_one(
        db_path,
        "SELECT blocked, mirror_state FROM download_files WHERE id=?",
        (primary_id,),
    )
    standby = _read_one(
        db_path,
        "SELECT status, mirror_state FROM download_files WHERE id=?",
        (standby_ids[0],),
    )
    assert primary == {"blocked": 0, "mirror_state": "active"}
    assert standby == {"status": "duplicate", "mirror_state": "standby"}
    events = _read_all(
        db_path,
        "SELECT message FROM events WHERE torrent_id=?",
        (torrent_id,),
    )
    assert any("local/system failure" in row["message"] for row in events)


@pytest.mark.asyncio
async def test_source_unlock_failure_without_gid_is_failover_eligible():
    manager = DirectLinkResultGuardManager()
    eligible, reason = await manager._mirror_failure_is_failover_eligible(
        {
            "download_id": None,
            "block_reason": "source-unlock: LINK_DOWN",
            "download_url": None,
        }
    )
    assert eligible is True
    assert reason == "LINK_DOWN"


@pytest.mark.asyncio
async def test_local_aria2_dispatch_failure_without_gid_is_not_failover_eligible():
    manager = DirectLinkResultGuardManager()
    eligible, reason = await manager._mirror_failure_is_failover_eligible(
        {
            "download_id": None,
            "block_reason": "aria2-dispatch: Unable to queue aria2 download",
            "download_url": None,
        }
    )
    assert eligible is False
    assert reason == "Unable to queue aria2 download"


@pytest.mark.asyncio
async def test_success_after_failover_is_plain_done_and_unused_standby_is_retained(tmp_path, monkeypatch):
    db_path = await _prepare_db(tmp_path, monkeypatch)
    torrent_id, primary_id, standby_ids = _insert_failover_group(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE download_files SET blocked=NULL, mirror_state='exhausted', download_url=NULL WHERE id=?",
            (primary_id,),
        )
        conn.execute(
            """UPDATE download_files
               SET status='completed', blocked=0, local_path=?, download_id='gid-b',
                   mirror_state='active'
               WHERE id=?""",
            (str(Path(db_path).parent / "archive.rar"), standby_ids[0]),
        )
        conn.commit()
    finally:
        conn.close()

    manager = DirectLinkResultGuardManager()
    _disable_async_dispatch(manager)
    manager._mark_finished = AsyncMock()
    monkeypatch.setattr(
        result_guard,
        "get_settings",
        lambda: SimpleNamespace(discord_notify_finished=False),
    )
    monkeypatch.setattr(result_guard, "is_builtin_mode", lambda: False)

    completed = await manager._complete_direct_link_result(torrent_id)

    assert completed is True
    parent = _read_one(
        db_path,
        "SELECT status, error_message, progress FROM torrents WHERE id=?",
        (torrent_id,),
    )
    assert parent == {"status": "completed", "error_message": None, "progress": 100.0}
    unused = _read_one(
        db_path,
        "SELECT status, mirror_state FROM download_files WHERE id=?",
        (standby_ids[1],),
    )
    assert unused == {"status": "duplicate", "mirror_state": "unused"}
    events = _read_all(
        db_path,
        "SELECT message FROM events WHERE torrent_id=? ORDER BY id",
        (torrent_id,),
    )
    assert any("mirror source(s) exhausted during failover" in row["message"] for row in events)
    assert any("unused standby mirror(s)" in row["message"] for row in events)


def test_failover_schema_and_dispatch_contract_are_persisted():
    root = Path(__file__).resolve().parents[2]
    db_source = (root / "backend/db/database.py").read_text()
    dispatch_source = (root / "backend/services/dispatch_coordinator.py").read_text()
    manager_source = (root / "backend/services/manager_v2.py").read_text()
    result_source = (root / "backend/services/direct_link_result_guard.py").read_text()

    assert '("mirror_group_id", "INTEGER")' in db_source
    assert '("mirror_state", "TEXT DEFAULT \'\'")' in db_source
    assert "mirror_state='standby'" in dispatch_source
    assert "alternates retained as automatic failover standbys" in dispatch_source
    assert 'reason=f"source-unlock: {error_text}"' in manager_source
    assert "Failover source no longer matches the validated mirror artifact" in manager_source
    assert "_promote_direct_link_mirror_failover" in result_source
    assert "mirror_state='exhausted'" in result_source
    assert "mirror_state='unused'" in result_source
''')

print("Mirror failover patch applied")
