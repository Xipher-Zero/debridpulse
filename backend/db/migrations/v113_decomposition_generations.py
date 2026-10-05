"""1.0.13 upgrade: universal decomposition generations and frozen collection roots.

Run once, inside database initialisation, at the boundary where decomposition
generation authority (TASK3d-3a0) first meets an existing database; its
``transfer_controls`` marker (runtime state, never a forged
``schema_migrations`` version) makes every later start a no-op, so nothing it
grants is ever granted again. It never moves, deletes or retargets anything: it
only records, from durable evidence already present, facts the generation model
reads.

* A generation committed under the earlier (selection-only) model was the
  generation its root fanned out under: it is ``proven``.
* A still-undecided generation records its immediate committed predecessor,
  derived exactly as the earlier model derived it at use time.
* Every decomposition already persisted here is governed by the generation of
  its root's CURRENT binding -- the existing one, or, for a root that owns no
  generation at all, one created here (decided ALL, committed, proven, never
  offered) whose unstamped members are stamped with it -- and that generation is
  marked ``legacy_established``.

  ``legacy_established`` is a COMPATIBILITY LINEAGE, deliberately not a
  historical-version claim. It says only that the decomposition predates
  durable decomposition-generation authority. Which coordinate model actually
  produced it (for example, before or after the collection-root correction) is
  NOT durably recoverable -- no neutral fact ever recorded it -- so nothing here
  inspects a path, a name, a provider or provider metadata to guess. This
  pre-3a0 boundary is the finest the database can support. Such a decomposition
  may cross into the 3a0 model by one compatibility reconstruction, and only on
  a terminal reacquisition (``TransferRepository._continuity``).
* A decomposed transfer's collection folder is frozen from where its members
  were actually placed. Targets that do not imply exactly one folder freeze
  nothing: the transfer is marked conflicted and its current generations held,
  so nothing is guessed and nothing moves. A decomposed transfer with no placed
  member at all freezes the folder its current name already derives.
"""
from __future__ import annotations

import json
import posixpath
import time

from transfers import file_selection as fs
from transfers.filesystem import safe_name

MARKER = "decomposition_generations"
CONFLICT_REASON = "collection_root_conflict"


async def _rows(db, sql: str, params=()) -> list[dict]:
    cursor = await db.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in await cursor.fetchall()]


def _json(value) -> dict:
    try:
        loaded = json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _placed_folder(local_path: str, relative_path: str) -> str | None:
    """The collection folder one placed member's target implies, or ``None``
    when the target does not end with that member's own sanitized path."""
    member = "/".join(safe_name(part) for part in str(relative_path).replace("\\", "/").split("/") if part)
    target = str(local_path).replace("\\", "/")
    if not member or not target.endswith("/" + member):
        return None
    folder = posixpath.basename(target[: -(len(member) + 1)])
    return folder or None


async def _predecessor(db, request_id: str, binding_id: str) -> str | None:
    """Same derivation as ``TransferRepository._immediate_predecessor``."""
    rows = await _rows(db, """SELECT id, manifest_committed_at FROM transfer_file_selections
        WHERE request_id=? AND provider_resource_id!=? AND manifest_committed_at IS NOT NULL
        ORDER BY manifest_committed_at DESC LIMIT 2""", (request_id, binding_id))
    if not rows or (len(rows) > 1 and rows[0]["manifest_committed_at"] == rows[1]["manifest_committed_at"]):
        return None
    return rows[0]["id"]


async def _binding(db, transfer_id: int, resource) -> dict | None:
    resource_key = str(_json(resource).get("id") or "")
    if not resource_key:
        return None
    rows = await _rows(db, """SELECT id, provider_id FROM provider_resources WHERE transfer_id=?
        AND (resource_key=? OR (resource_key IS NULL AND id=?))""", (transfer_id, resource_key, resource_key))
    return rows[0] if rows else None


async def backfill_decomposition_generations(db) -> None:
    if await _rows(db, "SELECT 1 FROM transfer_controls WHERE key=?", (MARKER,)):
        return
    now = time.time()
    await db.execute("""UPDATE transfer_file_selections SET continuity='proven'
        WHERE continuity IS NULL AND manifest_committed_at IS NOT NULL""")
    for row in await _rows(db, """SELECT id, request_id, provider_resource_id FROM transfer_file_selections
            WHERE predecessor_id IS NULL AND manifest_committed_at IS NULL"""):
        predecessor = await _predecessor(db, row["request_id"], row["provider_resource_id"])
        if predecessor:
            await db.execute("UPDATE transfer_file_selections SET predecessor_id=? WHERE id=?", (predecessor, row["id"]))

    for root in await _rows(db, """SELECT r.id, r.transfer_id, r.resource FROM transfer_requests r
            JOIN torrents t ON t.id=r.transfer_id
            WHERE r.parent_id IS NULL AND r.resource IS NOT NULL AND t.status!='deleted'
              AND EXISTS(SELECT 1 FROM transfer_requests c WHERE c.parent_id=r.id)"""):
        binding = await _binding(db, root["transfer_id"], root["resource"])
        if binding is None:
            continue
        generation = fs.selection_identity(root["id"], binding["id"])
        if not await _rows(db, "SELECT 1 FROM transfer_file_selections WHERE request_id=?", (root["id"],)):
            await db.execute("""INSERT OR IGNORE INTO transfer_file_selections(
                    id, request_id, transfer_id, provider_resource_id, provider_id, initially_available,
                    manifest_wait_until, decision, decision_reason, decision_at, manifest_committed_at,
                    interactive, continuity, created_at, updated_at)
                VALUES(?,?,?,?,?,1,0.0,'all',?,?,?,0,'proven',?,?)""",
                             (generation, root["id"], root["transfer_id"], binding["id"], binding["provider_id"],
                              str(fs.DecisionReason.DEFAULT_MATERIALIZATION), now, now, now, now))
            await db.execute("""UPDATE transfer_requests SET materialized_selection_id=?
                WHERE parent_id=? AND materialized_selection_id IS NULL""", (generation, root["id"]))
        # The decomposition persisted before generation authority existed:
        # its governing generation carries the compatibility lineage.
        await db.execute("""UPDATE transfer_file_selections SET legacy_established=1
            WHERE id=? AND manifest_committed_at IS NOT NULL""", (generation,))

    for transfer in await _rows(db, """SELECT t.id, t.name FROM torrents t
            WHERE t.collection_root IS NULL AND COALESCE(t.collection_root_conflict,0)=0 AND t.status!='deleted'
              AND EXISTS(SELECT 1 FROM transfer_requests c JOIN transfer_requests r ON r.id=c.parent_id
                         WHERE r.transfer_id=t.id AND r.parent_id IS NULL)"""):
        folders, unreadable = set(), False
        for member in await _rows(db, """SELECT c.metadata, f.local_path FROM transfer_requests c
                JOIN transfer_requests r ON r.id=c.parent_id AND r.parent_id IS NULL
                JOIN download_files f ON f.request_id=c.id
                WHERE c.transfer_id=? AND COALESCE(f.local_path,'')!=''""", (transfer["id"],)):
            entry = _json(member["metadata"])
            if entry.get("whole_resource"):
                continue
            folder = _placed_folder(member["local_path"], entry.get("relative_path") or "")
            if folder is None:
                unreadable = True
            else:
                folders.add(folder)
        if unreadable or len(folders) > 1:
            await db.execute("UPDATE torrents SET collection_root_conflict=1 WHERE id=?", (transfer["id"],))
            await db.execute("""UPDATE transfer_file_selections SET continuity='held', continuity_reason=?, updated_at=?
                WHERE id IN (SELECT s.id FROM transfer_file_selections s WHERE s.transfer_id=?
                             AND s.created_at=(SELECT MAX(o.created_at) FROM transfer_file_selections o
                                               WHERE o.request_id=s.request_id))""",
                             (CONFLICT_REASON, now, transfer["id"]))
        else:
            frozen = next(iter(folders)) if folders else safe_name(transfer["name"] or "")
            await db.execute("UPDATE torrents SET collection_root=? WHERE id=?", (frozen, transfer["id"]))
    await db.execute("INSERT OR IGNORE INTO transfer_controls(key,value) VALUES(?,'established')", (MARKER,))
    await db.commit()
