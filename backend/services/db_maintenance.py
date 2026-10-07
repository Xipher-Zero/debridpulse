"""
Database maintenance helpers for the explicit database wipe and event-log retention.

Backups of any kind -- the pre-wipe safety backup included -- belong to the one
restore-point owner, ``services.backup``.
"""
from __future__ import annotations

import logging

from db.database import get_db

logger = logging.getLogger("debridpulse.db_maintenance")

TABLES = [
    "torrents",
    "download_files",
    "events",
    "stats_snapshots",
    "transfer_pause_intents",
    "deferred_provider_submissions",
    "transfer_controls",
    "transfer_requests",
    "provider_resources",
    "standby_resources",
    "transfer_file_manifests",
    "transfer_file_manifest_entries",
    "transfer_file_selections",
    "transfer_file_selection_entries",
    "transfer_file_selection_intents",
    "transfer_file_selection_intent_entries",
    "resolution_attempts",
    "route_attempt_provenance",
    "execution_attempts",
    "execution_attempt_provenance",
    "canonical_candidate_bindings",
    "canonical_candidate_origins",
    "artifact_consolidations",
    "transfer_outcomes",
    "postprocess_attempts",
    "application_events",
    "artifact_recovery_state",
    "artifact_material_state",
    "integration_runtime_state",
    "transfer_input_challenges",
    "schema_migrations",
]


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
        # File-selection intent and provenance are deleted child-first so no
        # orphan can outlive an explicit whole-database wipe: an intent's
        # entries, then the intent (which names its originating generation),
        # then the generations' own records.
        await db.execute("DELETE FROM transfer_file_selection_intent_entries")
        await db.execute("DELETE FROM transfer_file_selection_intents")
        await db.execute("DELETE FROM transfer_file_selection_entries")
        await db.execute("DELETE FROM transfer_file_selections")
        await db.execute("DELETE FROM transfer_file_manifest_entries")
        await db.execute("DELETE FROM transfer_file_manifests")
        # Canonical candidate/consolidation state, deleted child-first. Derived
        # from the real foreign keys, not assumed: an origin row references its
        # binding, plus download_files/torrents/transfer_requests/resolution_
        # attempts; a consolidation row references download_files/torrents/
        # transfer_requests; a binding row references download_files. All three
        # must therefore precede the resolution_attempts sweep below and the
        # request/artifact/transfer deletes that follow it. Foreign keys stay
        # enforced for the whole wipe; nothing here relies on ON DELETE CASCADE.
        await db.execute("DELETE FROM canonical_candidate_origins")
        await db.execute("DELETE FROM artifact_consolidations")
        await db.execute("DELETE FROM canonical_candidate_bindings")
        for table in (
            "application_events", "artifact_recovery_state", "artifact_material_state", "postprocess_attempts",
            "transfer_outcomes",
            "execution_attempt_provenance", "route_attempt_provenance",
            "execution_attempts", "resolution_attempts",
            # A standby preparation references its root request and its
            # provider_resources binding: it goes first.
            "standby_resources", "provider_resources",
        ):
            await db.execute(f"DELETE FROM {table}")
        await db.execute("DELETE FROM transfer_requests WHERE parent_id IS NOT NULL")
        await db.execute("DELETE FROM transfer_requests")
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
