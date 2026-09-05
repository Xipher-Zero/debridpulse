"""Durable, privacy-safe consolidation summary events.

Canonical ownership proves and persists artifact consolidation.  This module
owns only the user-facing semantic summary: a committed canonical attach stages
one hidden marker, and the marker becomes one public event only after every
material leaf in the incoming logical submission has a stable disposition.
"""
from __future__ import annotations

import json
from typing import Any

from db.database import get_db

_PENDING_KIND = "duplicate_consolidation_pending"
_EVENT_KIND = "duplicate_consolidated"


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class ConsolidationEvents:
    """Promote committed consolidation facts into one safe logical event."""

    def __init__(self, repository):
        self.repository = repository

    async def stage(self, source_transfer_id: int) -> None:
        """Stage one hidden marker after a canonical attach has committed."""
        source_transfer_id = _positive_int(source_transfer_id)
        if source_transfer_id is None:
            return
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                existing = await db.fetchone(
                    """SELECT id FROM application_events
                       WHERE transfer_id = ? AND kind IN (?, ?)
                       ORDER BY id LIMIT 1""",
                    (source_transfer_id, _PENDING_KIND, _EVENT_KIND),
                )
                if existing is None:
                    # claimed=1 keeps this internal marker out of the existing
                    # observability queue until the whole logical submission is
                    # stable enough to summarize truthfully.
                    await db.execute(
                        """INSERT INTO application_events(transfer_id, kind, detail, claimed)
                           VALUES(?, ?, NULL, 1)""",
                        (source_transfer_id, _PENDING_KIND),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def finalize_pending(self) -> int:
        """Promote stable hidden markers to public claim-once events."""
        promoted = 0
        async with get_db() as db:
            markers = await db.fetchall(
                """SELECT id, transfer_id FROM application_events
                   WHERE kind = ? AND claimed = 1 ORDER BY id""",
                (_PENDING_KIND,),
            )
            for marker in markers:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    payload = await self._disposition(db, int(marker["transfer_id"]))
                    if payload is None:
                        await db.rollback()
                        continue
                    cursor = await db.execute(
                        """UPDATE application_events
                           SET kind = ?, detail = ?, claimed = 0
                           WHERE id = ? AND kind = ? AND claimed = 1""",
                        (
                            _EVENT_KIND,
                            json.dumps(payload, separators=(",", ":"), sort_keys=True),
                            int(marker["id"]),
                            _PENDING_KIND,
                        ),
                    )
                    await db.commit()
                    if cursor.rowcount == 1:
                        promoted += 1
                except Exception:
                    await db.rollback()
                    raise
        return promoted

    async def _disposition(self, db, source_transfer_id: int) -> dict[str, Any] | None:
        transfer = await db.fetchone("SELECT status FROM torrents WHERE id = ?", (source_transfer_id,))
        if transfer is None:
            return None
        rows = await db.fetchall(
            """SELECT r.id AS request_id,
                      r.state AS request_state,
                      f.id AS artifact_id,
                      COALESCE(f.blocked, 0) AS blocked,
                      COALESCE(f.mirror_state, '') AS mirror_state,
                      ac.canonical_artifact_id AS canonical_artifact_id,
                      canonical.torrent_id AS canonical_transfer_id
               FROM transfer_requests r
               LEFT JOIN transfer_requests child ON child.parent_id = r.id
               LEFT JOIN download_files f ON f.request_id = r.id
               LEFT JOIN artifact_consolidations ac
                 ON ac.source_transfer_id = r.transfer_id
                AND ac.source_request_id = r.id
               LEFT JOIN download_files canonical ON canonical.id = ac.canonical_artifact_id
               WHERE r.transfer_id = ? AND child.id IS NULL
               ORDER BY r.ordinal, r.id""",
            (source_transfer_id,),
        )

        material = 0
        matched = 0
        unmatched = 0
        canonical_transfer_ids: set[int] = set()
        for row in rows:
            if str(row.get("request_state") or "").lower() == "skipped":
                continue
            artifact_id = row.get("artifact_id")
            if artifact_id is None:
                # Resolution/materialization is still in flight; do not emit a
                # summary whose matched/unmatched counts could still change.
                return None
            if bool(row.get("blocked")):
                continue
            material += 1
            canonical_artifact_id = row.get("canonical_artifact_id")
            canonical_transfer_id = _positive_int(row.get("canonical_transfer_id"))
            if canonical_artifact_id is not None and canonical_transfer_id is not None:
                matched += 1
                canonical_transfer_ids.add(canonical_transfer_id)
                continue
            if str(row.get("mirror_state") or "").lower() == "standby":
                # A standby artifact without its durable canonical mapping is
                # not a stable user-facing disposition.
                return None
            unmatched += 1

        if material <= 0 or matched <= 0 or not canonical_transfer_ids:
            return None
        if matched + unmatched != material:
            return None

        status = str(transfer.get("status") or "").lower()
        if unmatched == 0:
            # Complete absorption is public only after canonical lifecycle has
            # durably transitioned the source transfer to CONSOLIDATED.
            if status != "consolidated":
                return None
        elif status == "consolidated":
            return None

        return {
            "source_transfer_id": source_transfer_id,
            "canonical_transfer_ids": sorted(canonical_transfer_ids),
            "matched_count": matched,
            "unmatched_count": unmatched,
        }

    @staticmethod
    def public_payload(detail: Any) -> dict[str, Any] | None:
        """Return only the strict safe payload shape used by SSE."""
        if not isinstance(detail, str) or not detail:
            return None
        try:
            raw = json.loads(detail)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None

        source_transfer_id = _positive_int(raw.get("source_transfer_id"))
        matched_count = _positive_int(raw.get("matched_count"))
        unmatched_count = _nonnegative_int(raw.get("unmatched_count"))
        raw_targets = raw.get("canonical_transfer_ids")
        if (
            source_transfer_id is None
            or matched_count is None
            or unmatched_count is None
            or not isinstance(raw_targets, list)
        ):
            return None
        targets: set[int] = set()
        for value in raw_targets:
            target = _positive_int(value)
            if target is None:
                return None
            targets.add(target)
        if not targets or len(targets) > matched_count:
            return None

        # Deliberately reconstruct rather than forward the stored JSON.  Even
        # malformed or future detail fields cannot leak URLs, headers, tokens,
        # cookies, provider payloads, or other capability material over SSE.
        return {
            "source_transfer_id": source_transfer_id,
            "canonical_transfer_ids": sorted(targets),
            "matched_count": matched_count,
            "unmatched_count": unmatched_count,
        }


class ConsolidationEventCanonical:
    """Decorate canonical ownership with post-commit semantic-event staging."""

    def __init__(self, canonical, events: ConsolidationEvents):
        object.__setattr__(self, "_canonical", canonical)
        object.__setattr__(self, "_events", events)

    def __getattr__(self, name):
        return getattr(self._canonical, name)

    def __setattr__(self, name, value):
        if name in {"_canonical", "_events"}:
            object.__setattr__(self, name, value)
            return
        setattr(self._canonical, name, value)

    async def attach(self, primary, source, incoming_candidates, size):
        attached = await self._canonical.attach(primary, source, incoming_candidates, size)
        if attached:
            # CanonicalOwnership.attach() returns True only after its durable
            # transaction commits. A failed/rolled-back attach cannot produce
            # a success marker or user-facing event.
            await self._events.stage(source.transfer_id)
        return attached
