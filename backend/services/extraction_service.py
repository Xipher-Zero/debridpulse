"""Post-download extraction orchestration for completed transfers."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from core.config import get_settings
from db.database import get_db
from services.event_bus import publish
from services.extractor import archive_paths_from_downloads
from services.extractor_secure import get_secure_extractor
from services.notifications import NotificationService


_PART_RAR_RE = re.compile(r"^(?P<base>.+)\.part(?P<part>\d+)\.rar$", re.IGNORECASE)
_OLD_RAR_RE = re.compile(r"^(?P<base>.+)\.r(?P<part>\d{2})$", re.IGNORECASE)


def _normalise_paths(paths: Iterable[str | Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        if not raw:
            continue
        path = Path(raw)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _canonical_archive_entries(paths: Iterable[str | Path]) -> list[Path]:
    """Return one extraction entry point per DB-known archive set."""
    known = _normalise_paths(paths)
    entries = archive_paths_from_downloads(known)

    # Traditional split RAR sets are named payload.rar, payload.r00, payload.r01...
    # The .rar file is the canonical entry when it exists; 7-Zip consumes the
    # numbered companions automatically. Keep .r00 as an entry only for sets
    # where no matching .rar was downloaded.
    traditional_roots = {
        (str(path.parent), path.name[:-4].casefold())
        for path in known
        if path.suffix.casefold() == ".rar" and not _PART_RAR_RE.fullmatch(path.name)
    }

    canonical: list[Path] = []
    for entry in entries:
        old_part = _OLD_RAR_RE.fullmatch(entry.name)
        if old_part and (
            str(entry.parent),
            old_part.group("base").casefold(),
        ) in traditional_roots:
            continue
        canonical.append(entry)
    return canonical


def _archive_source_paths(entry: Path, known_paths: Iterable[str | Path]) -> list[Path]:
    """Return only DB-known source volumes belonging to *entry*'s archive set."""
    entry = Path(entry)
    known = _normalise_paths(known_paths)

    part_match = _PART_RAR_RE.fullmatch(entry.name)
    if part_match:
        base = part_match.group("base").casefold()
        members: list[tuple[int, Path]] = []
        for path in known:
            match = _PART_RAR_RE.fullmatch(path.name)
            if (
                path.parent == entry.parent
                and match
                and match.group("base").casefold() == base
            ):
                members.append((int(match.group("part")), path))
        if members:
            return [path for _part, path in sorted(members, key=lambda item: item[0])]
        return [entry]

    old_match = _OLD_RAR_RE.fullmatch(entry.name)
    if old_match:
        base = old_match.group("base")
    elif entry.suffix.casefold() == ".rar":
        base = entry.name[:-4]
        has_numbered_companion = any(
            path.parent == entry.parent
            and (match := _OLD_RAR_RE.fullmatch(path.name)) is not None
            and match.group("base").casefold() == base.casefold()
            for path in known
        )
        if not has_numbered_companion:
            return [entry]
    else:
        return [entry]

    base_folded = base.casefold()
    root_name = f"{base}.rar".casefold()
    root: Path | None = None
    numbered: list[tuple[int, Path]] = []
    for path in known:
        if path.parent != entry.parent:
            continue
        if path.name.casefold() == root_name:
            root = path
            continue
        match = _OLD_RAR_RE.fullmatch(path.name)
        if match and match.group("base").casefold() == base_folded:
            numbered.append((int(match.group("part")), path))

    sources: list[Path] = []
    if root is not None:
        sources.append(root)
    sources.extend(path for _part, path in sorted(numbered, key=lambda item: item[0]))
    return sources or [entry]


def _cleanup_successful_sources(
    successful_entries: Iterable[Path],
    source_paths_by_entry: dict[str, list[Path]],
    existed_before: set[str],
) -> tuple[int, int, list[tuple[Path, str]]]:
    """Remove DB-owned source volumes for successfully extracted archive sets."""
    targets: list[Path] = []
    seen: set[str] = set()
    for entry in successful_entries:
        for path in source_paths_by_entry.get(str(entry), [Path(entry)]):
            key = str(path)
            if key in seen or key not in existed_before:
                continue
            seen.add(key)
            targets.append(path)

    removed = 0
    failures: list[tuple[Path, str]] = []
    for path in targets:
        if not path.exists():
            # The extractor owns deletion of its entry volume. Count that as part
            # of the same requested cleanup operation rather than deleting twice.
            removed += 1
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            failures.append((path, str(exc)))
    return removed, len(targets), failures


class ExtractionService:
    async def extract_archive(self, *args, **kwargs):
        return await get_secure_extractor().extract_archive(*args, **kwargs)

    async def _publish_state(
        self,
        torrent_id: int,
        name: str,
        extraction_status: str,
        extraction_error: str | None = None,
    ) -> None:
        try:
            await publish(
                "torrent_updated",
                {
                    "id": int(torrent_id),
                    "status": "completed",
                    "name": str(name or ""),
                    "extraction_status": extraction_status,
                    "extraction_error": extraction_error,
                },
            )
            await publish("stats_changed", {})
        except Exception:
            pass

    async def extract_completed_transfer(
        self,
        torrent_id: int,
        torrent_dict: dict | None = None,
    ) -> dict:
        """Extract known completed archive children without changing transport truth."""
        cfg = get_settings()
        if not bool(getattr(cfg, "extract_enabled", False)):
            return {"attempted": False, "reason": "disabled"}

        torrent_id = int(torrent_id)
        async with get_db() as db:
            parent = await db.fetchone(
                "SELECT * FROM torrents WHERE id=?",
                (torrent_id,),
            )
            rows = await db.fetchall(
                """SELECT local_path FROM download_files
                   WHERE torrent_id=? AND status='completed'
                     AND local_path IS NOT NULL""",
                (torrent_id,),
            )

        if not parent or str(parent.get("status") or "") != "completed":
            return {"attempted": False, "reason": "not-completed"}

        known_paths = _normalise_paths(
            row.get("local_path") for row in rows if row.get("local_path")
        )
        archives = _canonical_archive_entries(known_paths)
        if not archives:
            async with get_db() as db:
                await db.execute(
                    """UPDATE torrents
                       SET extraction_status='skipped', extraction_error=NULL,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='completed'""",
                    (torrent_id,),
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                    (torrent_id, "Auto-extract: Not attempted · no supported archive detected"),
                )
                await db.commit()
            return {"attempted": False, "reason": "no-archives"}

        cleanup_requested = bool(getattr(cfg, "extract_delete_archive", True))
        source_paths_by_entry = {
            str(archive): _archive_source_paths(archive, known_paths)
            for archive in archives
        }
        existed_before = {
            str(path)
            for sources in source_paths_by_entry.values()
            for path in sources
            if path.exists()
        }

        name = str(
            parent.get("name")
            or (torrent_dict or {}).get("name")
            or f"transfer {torrent_id}"
        )
        async with get_db() as db:
            await db.execute(
                """UPDATE torrents
                   SET extraction_status='extracting', extraction_error=NULL,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status='completed'""",
                (torrent_id,),
            )
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                (
                    torrent_id,
                    f"Auto-extract: Attempted · {len(archives)} archive(s) detected",
                ),
            )
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                (torrent_id, "Extraction status: Extracting"),
            )
            await db.commit()

        await self._publish_state(torrent_id, name, "extracting")

        extractor = get_secure_extractor()
        extractor.update_max_concurrent(
            max(1, int(getattr(cfg, "extract_max_concurrent", 1) or 1))
        )
        results = await extractor.extract_archives(
            archives,
            delete_after=cleanup_requested,
        )
        failures = [(path, msg) for path, ok, msg in results if not ok]
        successes = [(path, msg) for path, ok, msg in results if ok]

        if not results:
            failures = [
                (
                    archives[0],
                    "Auto-extract produced no result for a detected archive",
                )
            ]

        cleanup_removed = 0
        cleanup_total = 0
        cleanup_failures: list[tuple[Path, str]] = []
        cleanup_event: tuple[str, str] | None = None
        if cleanup_requested and successes:
            cleanup_removed, cleanup_total, cleanup_failures = _cleanup_successful_sources(
                (path for path, _msg in successes),
                source_paths_by_entry,
                existed_before,
            )
            if cleanup_failures:
                cleanup_detail = "; ".join(
                    f"{path.name}: {message}"
                    for path, message in cleanup_failures[:3]
                )
                if len(cleanup_failures) > 3:
                    cleanup_detail += (
                        f"; +{len(cleanup_failures) - 3} more cleanup failure(s)"
                    )
                cleanup_event = (
                    "warn",
                    (
                        "Archive cleanup: Failed · "
                        f"{cleanup_removed}/{cleanup_total} source file(s) removed · "
                        f"{cleanup_detail}"
                    )[:1200],
                )
            else:
                cleanup_event = (
                    "info",
                    (
                        "Archive cleanup: Completed · "
                        f"{cleanup_removed}/{cleanup_total} source file(s) removed"
                    ),
                )

        if failures:
            detail = "; ".join(str(msg) for _path, msg in failures[:3])
            if len(failures) > 3:
                detail += f"; +{len(failures) - 3} more extraction failure(s)"
            detail = detail[:1000]
            final_state = "error"
            event_level = "error"
            event_message = (
                f"Extraction status: Failed · {len(failures)}/{len(archives)} archive(s) failed · {detail}"
            )[:1200]
        else:
            detail = ""
            final_state = "completed"
            event_level = "info"
            event_message = (
                f"Extraction status: Completed · {len(successes)}/{len(archives)} archive(s) extracted"
            )

        async with get_db() as db:
            await db.execute(
                """UPDATE torrents
                   SET extraction_status=?, extraction_error=?,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status!='deleted'""",
                (final_state, detail or None, torrent_id),
            )
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                (torrent_id, event_level, event_message),
            )
            if cleanup_event is not None:
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                    (torrent_id, cleanup_event[0], cleanup_event[1]),
                )
            await db.commit()

        await self._publish_state(
            torrent_id,
            name,
            final_state,
            detail or None,
        )

        if bool(getattr(cfg, "discord_notify_extract", False)):
            notify = NotificationService(
                webhook_url=str(getattr(cfg, "discord_webhook_url", "") or ""),
                added_webhook_url=str(getattr(cfg, "discord_webhook_added", "") or ""),
            )
            if successes:
                await notify.send_extract_complete(
                    name,
                    archive_count=len(successes),
                    dest=str(Path(successes[0][0]).parent),
                )
            for _path, message in failures:
                await notify.send_extract_failed(name, reason=str(message))

        return {
            "attempted": True,
            "status": final_state,
            "archives": len(archives),
            "succeeded": len(successes),
            "failed": len(failures),
            "error": detail or None,
            "cleanup": {
                "requested": cleanup_requested,
                "removed": cleanup_removed,
                "total": cleanup_total,
                "failed": len(cleanup_failures),
            },
        }
