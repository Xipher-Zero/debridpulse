import asyncio
import random
import time
import logging
from core.branding import REPOSITORY_API_URL
from core.config import get_settings
from core.logging_utils import sanitize_exception
from core.performance import async_timer
from core.version import is_version_newer, normalize_version_tag
from services.transfer_service import transfer_service

logger = logging.getLogger("alldebrid.scheduler")
_tasks = []


async def _jitter_sleep(base_seconds: float, jitter_fraction: float = 0.25) -> None:
    """Sleep for base_seconds ± jitter_fraction*base_seconds.

    Spreads startup spikes across the configured interval so all loops
    don't fire simultaneously on container start, reducing burst API load.
    Minimum sleep: 1 second.
    """
    jitter = base_seconds * jitter_fraction * (2 * random.random() - 1)
    await asyncio.sleep(max(1.0, base_seconds + jitter))


def _coerce_int_setting(value, default: int) -> int:
    try:
        if value is None:
            return int(default)
        return int(value)
    except Exception:
        return int(default)


def _has_reporting_webhook(cfg) -> bool:
    """Return True when reporting can send to either the dedicated or Discord webhook."""
    stats_webhook = (getattr(cfg, "stats_report_webhook_url", "") or "").strip()
    discord_webhook = (getattr(cfg, "discord_webhook_url", "") or "").strip()
    return bool(stats_webhook or discord_webhook)


def _stats_report_window_hours(cfg) -> int:
    """Return the configured report window in hours for webhook reporting."""
    return max(1, _coerce_int_setting(getattr(cfg, "stats_report_window_hours", 24), 24))


async def sync_status_loop():
    """
    Regular AllDebrid poll: syncs active (non-terminal) torrents every poll_interval_seconds.
    Also runs cleanup tasks each cycle and enforces Smart Scheduler night-mode limits.
    """
    await _jitter_sleep(get_settings().poll_interval_seconds)
    while True:
        try:
            async with async_timer("scheduler.provider_poll"):
                await transfer_service.sync_alldebrid_status()
        except Exception as e:
            logger.error("Status sync error: %s", sanitize_exception(e))
        try:
            await transfer_service.cleanup_no_peer_errors()
        except Exception as e:
            logger.error("No-peer cleanup error: %s", sanitize_exception(e))
        try:
            await transfer_service.cleanup_alldebrid_orphans()
        except Exception as e:
            logger.debug("AllDebrid orphan cleanup error: %s", sanitize_exception(e))
        try:
            await transfer_service.cleanup_stuck_downloads()
        except Exception as e:
            logger.error("Stuck download cleanup error: %s", sanitize_exception(e))
        await asyncio.sleep(get_settings().poll_interval_seconds)


async def full_sync_loop():
    """
    Full AllDebrid reconciliation: runs every full_sync_interval_minutes (default 5).
    Catches torrents in 'error'/'queued' that are actually 'ready' on AllDebrid,
    and any status drift between local DB and AllDebrid.
    Also imports new magnets added directly on AllDebrid.
    """
    cfg = get_settings()
    interval = max(1, _coerce_int_setting(getattr(cfg, "full_sync_interval_minutes", 5), 5))
    await _jitter_sleep(interval * 60)  # spread startup across the full interval
    while True:
        cfg = get_settings()
        interval = max(0, _coerce_int_setting(getattr(cfg, "full_sync_interval_minutes", 5), 5))
        if interval <= 0:
            await asyncio.sleep(60)
            continue
        try:
            async with async_timer("scheduler.provider_inventory"):
                result = await transfer_service.reconcile_provider_inventory()
            if result.get("imported") or result.get("updated"):
                logger.info(
                    "Provider inventory: %d imported, %d reconciled from %d item(s)",
                    int(result.get("imported") or 0),
                    int(result.get("updated") or 0),
                    int(result.get("snapshot_count") or 0),
                )
        except Exception as e:
            logger.error("Provider inventory sync failed: %s", sanitize_exception(e))
        await asyncio.sleep(interval * 60)


async def sync_download_clients_loop():
    await _jitter_sleep(max(2, get_settings().aria2_poll_interval_seconds))
    while True:
        try:
            async with async_timer("scheduler.download_client_sync"):
                await transfer_service.reconciliation.reconcile()
        except Exception as e:
            logger.error("Download client sync error: %s", sanitize_exception(e))
        await asyncio.sleep(max(2, get_settings().aria2_poll_interval_seconds))


async def deep_sync_loop():
    """Run a slower supplemental pass through canonical aria2 recovery.

    Normal reconciliation owns retry timing/accounting on every aria2 poll.
    ``aria2_deep_sync_interval_minutes=0`` disables this compatibility cadence.
    """
    while True:
        cfg = get_settings()
        interval_min = max(0, _coerce_int_setting(getattr(cfg, "aria2_deep_sync_interval_minutes", 10), 10))
        if interval_min <= 0:
            await asyncio.sleep(60)
            continue
        await asyncio.sleep(interval_min * 60)
        try:
            await transfer_service.deep_sync_aria2_finished()
        except Exception as e:
            logger.error("Deep aria2 sync error: %s", sanitize_exception(e))


async def backup_loop():
    """Runs periodic backups based on backup_interval_hours setting."""
    await asyncio.sleep(60)  # Initial delay
    while True:
        try:
            from services.backup import run_backup
            await run_backup()
        except Exception as e:
            logger.error("Backup error: %s", sanitize_exception(e))
        cfg = get_settings()
        interval_h = max(1, getattr(cfg, "backup_interval_hours", 24))
        await asyncio.sleep(interval_h * 3600)


async def aria2_housekeeping_loop():
    """Periodically reapply built-in aria2 memory tuning without deleting result history."""
    await asyncio.sleep(90)
    while True:
        cfg = get_settings()
        interval_min = max(0, _coerce_int_setting(getattr(cfg, "aria2_purge_interval_minutes", 60), 60))
        if interval_min <= 0:
            await asyncio.sleep(300)
            continue
        await asyncio.sleep(interval_min * 60)
        try:
            await transfer_service.run_aria2_housekeeping()
        except Exception as e:
            logger.error("aria2 housekeeping error: %s", sanitize_exception(e))


async def aria2_log_rotation_loop():
    """Rotate the built-in aria2 log file before it grows without bound."""
    from services.aria2_runtime import runtime, is_builtin_mode

    await asyncio.sleep(180)
    while True:
        try:
            cfg = get_settings()
            if is_builtin_mode(cfg):
                result = await runtime.ensure_log_rotation()
                if result.get("rotated"):
                    logger.info("aria2 log rotation completed")
        except Exception as e:
            logger.error("aria2 log rotation error: %s", sanitize_exception(e))
        await asyncio.sleep(900)


async def aria2_restart_loop():
    """
    Periodically restarts the built-in aria2 process to reclaim memory.

    aria2 uses glibc malloc. Even with MALLOC_ARENA_MAX=1 the process heap
    grows over time as malloc retains pages after freeing them. A full process
    restart is the only guaranteed way to return that memory to the OS.

    The restart is deferred until aria2 has no active downloads to avoid
    interrupting in-progress transfers. After restart, _dispatch re-queues
    all pending files from the DB within one poll cycle (normally ≤2 seconds).

    Controlled by aria2_restart_interval_hours (0 = disabled).
    """
    from services.aria2_runtime import runtime, is_builtin_mode

    while True:
        await asyncio.sleep(300)  # check every 5 minutes
        try:
            cfg = get_settings()
            if not is_builtin_mode(cfg):
                continue
            interval_h = float(getattr(cfg, "aria2_restart_interval_hours", 0) or 0)
            if interval_h <= 0:
                continue
            uptime_s = runtime._started_at
            if uptime_s <= 0:
                continue
            age_h = (time.time() - uptime_s) / 3600
            if age_h < interval_h:
                continue

            # Wait until no active downloads to avoid interruption
            try:
                from services.aria2 import Aria2Service
                from services.aria2_runtime import effective_rpc_config
                url, secret = effective_rpc_config(cfg)
                svc = Aria2Service(url, secret, 10)
                all_dl = await svc.get_all()
                active = [d for d in all_dl if d.status == "active"]
                if active:
                    logger.debug(
                        "aria2 restart deferred: %d active downloads", len(active)
                    )
                    continue
            except Exception:
                continue

            logger.info(
                "aria2 periodic restart after %.1f hours (memory reclaim)", age_h
            )
            await runtime.restart()
            logger.info("aria2 restarted successfully")
        except Exception as e:
            logger.error("aria2_restart_loop error: %s", sanitize_exception(e))


async def update_check_loop() -> None:
    """Check GitHub for new releases every N hours and send a Discord webhook if enabled."""
    await asyncio.sleep(300)  # 5 min initial delay
    _last_notified: str = ""
    while True:
        # Keep a valid backoff even if settings retrieval itself fails.
        interval_h = 12
        try:
            cfg = get_settings()
            interval_h = max(0, _coerce_int_setting(
                getattr(cfg, "update_check_interval_hours", 12), 12
            ))
            if interval_h <= 0:
                await asyncio.sleep(3600)
                continue

            from core.version import read_version
            import aiohttp as _aiohttp

            current = read_version()
            timeout = _aiohttp.ClientTimeout(total=10)
            async with _aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{REPOSITORY_API_URL}/releases/latest",
                    headers={"Accept": "application/vnd.github.v3+json"},
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"GitHub API returned {resp.status}")
                    rel = await resp.json()

            latest = normalize_version_tag(rel.get("tag_name") or "")

            if latest and is_version_newer(latest, current) and latest != _last_notified:
                logger.info("Update available: %s → %s", current, latest)
                await transfer_service.notifications.client().send_update(
                    current_version=current,
                    latest_version=latest,
                    release_url=rel.get("html_url", ""),
                    release_notes=(rel.get("body") or "").strip(),
                )
                _last_notified = latest

        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("update_check_loop error: %s", sanitize_exception(exc))

        await asyncio.sleep(max(3600, interval_h * 3600))


async def events_ttl_loop() -> None:
    """Prune old event log entries once per day.

    Only the ``events`` table is pruned — torrents and download_files are never
    touched, so duplicate-download prevention (based on the torrent hash and
    status columns) is not affected.
    """
    await asyncio.sleep(3600)  # 1-hour initial delay so startup isn't noisy
    while True:
        try:
            cfg = get_settings()
            keep_days = int(getattr(cfg, "events_keep_days", 30) or 30)
            if keep_days > 0:
                from services.db_maintenance import cleanup_old_events
                result = await cleanup_old_events(keep_days=keep_days)
                if result.get("deleted", 0) > 0:
                    logger.info(
                        "events_ttl_loop: pruned %d event(s) older than %d days",
                        result["deleted"], keep_days,
                    )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("events_ttl_loop error: %s", sanitize_exception(exc))
        await asyncio.sleep(86400)  # run once every 24 hours



async def disk_guard_loop():
    """
    Periodic disk-space guard: checks free space every disk_guard_interval_seconds.

    Runs independently of sync_status_loop so disk checks never pile up on the
    main poll cycle (which may be as fast as 1 s) — the default interval is 60 s.

    Compatible with all filesystems: ext4, XFS, ZFS, Btrfs, FUSE/shfs (Unraid),
    NFS, and Windows (via shutil fallback).
    """
    await asyncio.sleep(10)  # brief startup delay
    while True:
        cfg = get_settings()
        min_gb = float(getattr(cfg, "min_free_disk_gb", 0) or 0)
        interval = max(10, int(getattr(cfg, "disk_guard_interval_seconds", 60) or 60))
        if min_gb > 0:
            try:
                await transfer_service.check_disk_space_guard()
            except Exception as e:
                logger.debug("disk_guard check error: %s", sanitize_exception(e))
        await asyncio.sleep(interval)


def scheduler_running() -> bool:
    return any(not task.done() for task in _tasks)


async def start_scheduler():
    if scheduler_running():
        logger.debug("Scheduler already running")
        return
    _tasks.clear()
    _tasks.append(asyncio.create_task(sync_status_loop()))
    _tasks.append(asyncio.create_task(full_sync_loop()))
    _tasks.append(asyncio.create_task(sync_download_clients_loop()))
    _tasks.append(asyncio.create_task(deep_sync_loop()))
    _tasks.append(asyncio.create_task(aria2_housekeeping_loop()))
    _tasks.append(asyncio.create_task(aria2_log_rotation_loop()))
    _tasks.append(asyncio.create_task(backup_loop()))
    _tasks.append(asyncio.create_task(stats_snapshot_loop()))
    _tasks.append(asyncio.create_task(stats_report_loop()))
    _tasks.append(asyncio.create_task(aria2_restart_loop()))
    _tasks.append(asyncio.create_task(update_check_loop()))
    _tasks.append(asyncio.create_task(events_ttl_loop()))
    _tasks.append(asyncio.create_task(disk_guard_loop()))
    logger.info("Scheduler started")


async def stop_scheduler():
    tasks = list(_tasks)
    _tasks.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        waiter = asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            # Finish draining cancelled scheduler tasks before propagating caller
            # cancellation; the wipe route can then safely restart the scheduler.
            await waiter
            raise


async def stats_snapshot_loop():
    """Periodically takes a stats snapshot."""
    await asyncio.sleep(120)  # initial delay
    while True:
        cfg = get_settings()
        interval_min = max(0, _coerce_int_setting(getattr(cfg, "stats_snapshot_interval_minutes", 60), 60))
        if interval_min <= 0:
            await asyncio.sleep(300)
            continue
        await asyncio.sleep(interval_min * 60)
        try:
            from services.stats import take_stats_snapshot
            await take_stats_snapshot()
        except Exception as e:
            logger.error("Stats snapshot error: %s", sanitize_exception(e))


async def stats_report_loop():
    """Periodically sends a reporting webhook for the configured time window."""
    await asyncio.sleep(180)
    while True:
        cfg = get_settings()
        interval_h = max(0, _coerce_int_setting(getattr(cfg, "stats_report_interval_hours", 0), 0))
        window_h = _stats_report_window_hours(cfg)
        if interval_h <= 0 or not _has_reporting_webhook(cfg):
            await asyncio.sleep(300)
            continue
        await asyncio.sleep(max(300, interval_h * 3600))
        try:
            from services.stats import send_stats_report
            await send_stats_report(hours=window_h, triggered_by="schedule")
        except Exception as e:
            logger.error("Stats report error: %s", sanitize_exception(e))
