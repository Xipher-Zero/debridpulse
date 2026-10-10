"""The durable event journal: THE append-only history of what DebridPulse did.

One row is one occurrence a canonical owner established -- a transfer
transition, a route attempt's end, an execution failure, an operator's file
choice, a backup restored. The owner writes it with ``record`` on its own open
connection, inside the transaction that commits the transition itself, so an
event exists exactly when its transition committed. An occurrence that commits
no database transition of its own (a settings save, a backup file created) goes
through the one standalone path, ``record_now``, after the action succeeded.

The journal decides nothing: nothing reads it to route, retry, select or clean
up. It holds compact facts -- typed by ``category`` (the broad operator filter),
``event_type`` (the precise occurrence), ``severity`` and an optional
``outcome`` -- correlated to the authoritative rows that hold the full truth,
never copies of them. It is never pruned: a transfer's deletion or
consolidation leaves its history, and only the explicit whole-database reset
clears it.

Text is sanitized here, before persistence, so neither the table nor its search
index can ever hold a URL, credential, header or native body.

Search is a DERIVED index (FTS5, trigram) maintained behind a durable
watermark: every journal id at or below ``event_journal_index.indexed_through``
is indexed. It is updated outside the authoritative transaction, so an index
failure can never refuse a transfer transition; ``catch_up`` indexes the tail
idempotently (FTS rows and the watermark commit together) and ``reset_index``
drops a broken index for a bounded rebuild. A search reports how much of the
journal it could not cover instead of presenting a partial answer as complete.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

logger = logging.getLogger("debridpulse.event_journal")

# ── taxonomy ────────────────────────────────────────────────────────────────

# The broad, integration-neutral operator filter ("Event Type"). Every
# ``event_type`` is ``<category>.<occurrence>``.
CATEGORIES = (
    "transfer", "routing", "resource", "selection", "consolidation", "execution",
    "recovery", "storage", "extraction", "input", "integration", "configuration",
    "administration",
)
SEVERITIES = ("info", "warning", "error")
PAGE_SIZES = (50, 100, 250)
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 250

MESSAGE_LIMIT = 200
DETAIL_LIMIT = 500
NAME_LIMIT = 255
SEARCH_MIN_TEXT = 3
SEARCH_MAX_TEXT = 200
# Derived-index catch-up batch: what one maintenance pass or one search indexes
# before answering. A larger backlog is reported, never hidden.
INDEX_BATCH = 2000

_TYPE = re.compile(r"[a-z]+\.[a-z0-9_.]{1,80}")
_TOKEN = re.compile(r"[a-z0-9_]{1,64}")
_TRANSFER_REF = re.compile(r"#?(\d{1,18})")

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS event_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        occurrence_key TEXT UNIQUE,
        occurred_at REAL NOT NULL,
        category TEXT NOT NULL,
        event_type TEXT NOT NULL,
        severity TEXT NOT NULL CHECK(severity IN ('info','warning','error')),
        outcome TEXT,
        subject_kind TEXT NOT NULL,
        subject_id TEXT,
        transfer_id INTEGER,
        related_transfer_id INTEGER,
        integration_id TEXT,
        subject_name TEXT,
        message TEXT NOT NULL,
        detail TEXT,
        error_domain TEXT,
        error_category TEXT,
        error_origin TEXT,
        error_mutation TEXT,
        provenance TEXT)""",
    # The derived search index's watermark, and when this database's journal
    # began (forward history only: nothing before it was ever imported).
    """CREATE TABLE IF NOT EXISTS event_journal_index (
        id INTEGER PRIMARY KEY CHECK(id = 1),
        indexed_through INTEGER NOT NULL DEFAULT 0,
        started_at REAL NOT NULL)""",
)
INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_event_journal_transfer ON event_journal(transfer_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_event_journal_related ON event_journal(related_transfer_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_event_journal_category ON event_journal(category, id)",
    "CREATE INDEX IF NOT EXISTS idx_event_journal_severity ON event_journal(severity, id)",
    "CREATE INDEX IF NOT EXISTS idx_event_journal_category_severity ON event_journal(category, severity, id)",
    "CREATE INDEX IF NOT EXISTS idx_event_journal_occurred ON event_journal(occurred_at)",
)
FTS_TABLE = "event_journal_fts"
FTS_SCHEMA = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
    "message, detail, subject_name, content='event_journal', content_rowid='id', tokenize='trigram')"
)
# SQLite's own clock at insert time, inside the writer's lock: commit order and
# occurrence time agree unless the wall clock itself steps back.
_NOW_SQL = "((julianday('now') - 2440587.5) * 86400.0)"


@lru_cache(maxsize=1)
def fts_supported() -> bool:
    """Whether this process's SQLite runtime provides FTS5 with the trigram
    tokenizer. Probed once on a private in-memory database."""
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE probe USING fts5(text, tokenize='trigram')")
        finally:
            conn.close()
        return True
    except sqlite3.Error:
        return False


async def ensure_schema(db) -> None:
    """Part of THE schema bootstrap (``db.database.init_db``): additive and
    idempotent. The search index is created empty when the runtime supports it
    and is never rebuilt here; startup does not wait on indexing."""
    for statement in SCHEMA:
        await db.execute(statement)
    await db.execute(
        f"INSERT OR IGNORE INTO event_journal_index(id, indexed_through, started_at) VALUES(1, 0, {_NOW_SQL})")
    for statement in INDEXES:
        await db.execute(statement)
    if fts_supported():
        await db.execute(FTS_SCHEMA)
    else:
        logger.warning("SQLite FTS5 trigram support is unavailable: Activity Log text search is disabled")


# ── writing ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class JournalEvent:
    category: str
    event_type: str
    severity: str
    message: str
    subject_kind: str
    subject_id: Any = None
    transfer_id: int | None = None
    # A second transfer the occurrence involves (consolidation: the canonical
    # owner of a contributor's artifacts). Searching either id finds it.
    related_transfer_id: int | None = None
    integration_id: str | None = None
    outcome: str | None = None
    detail: str | None = None
    # A ``transfers.errors.NormalizedError``; only its classification and its
    # already-sanitized diagnostic are kept, never the envelope.
    error: Any = None
    # ``<table>:<id>`` of the canonical row holding the full facts. It may
    # outlive that row; it is a reference, never a promise.
    provenance: str | None = None
    # Deterministic identity of an occurrence whose owner can be re-entered
    # without a guarding state change; a replay then finds the event already
    # recorded. ``None`` where the co-committed transition is itself the
    # once-only guard.
    occurrence_key: str | None = None
    subject_name: str | None = None


def _safe(value, limit: int) -> str | None:
    from transfers.errors import safe_diagnostic

    if value is None:
        return None
    text = " ".join(safe_diagnostic(str(value), limit=limit * 2).split())
    return text[:limit] or None


def _token(value) -> str | None:
    text = str(value or "").strip().lower()
    return text if _TOKEN.fullmatch(text) else None


def _validated(event: JournalEvent) -> JournalEvent:
    if event.category not in CATEGORIES:
        raise ValueError(f"unknown event category {event.category!r}")
    if not _TYPE.fullmatch(event.event_type) or not event.event_type.startswith(event.category + "."):
        raise ValueError(f"event type {event.event_type!r} is not in category {event.category!r}")
    if event.severity not in SEVERITIES:
        raise ValueError(f"unknown event severity {event.severity!r}")
    if event.outcome is not None and not _TOKEN.fullmatch(event.outcome):
        raise ValueError(f"invalid event outcome {event.outcome!r}")
    if not _TOKEN.fullmatch(event.subject_kind):
        raise ValueError(f"invalid event subject kind {event.subject_kind!r}")
    return event


async def record(db, event: JournalEvent) -> bool:
    """Append ``event`` on the caller's open connection, inside the caller's
    transaction: it commits or rolls back with the transition it describes.
    A failure raises, failing that transaction, exactly like any other row the
    transition writes. Returns False only when ``occurrence_key`` was already
    recorded."""
    event = _validated(event)
    name = event.subject_name
    if name is None and event.transfer_id is not None:
        row = await db.fetchone("SELECT name FROM torrents WHERE id=?", (int(event.transfer_id),))
        name = row["name"] if row else None
    error = event.error
    detail = event.detail
    if detail is None and error is not None and getattr(error, "diagnostic", ""):
        detail = error.diagnostic
    cursor = await db.execute(
        f"""INSERT INTO event_journal(occurrence_key, occurred_at, category, event_type, severity, outcome,
                subject_kind, subject_id, transfer_id, related_transfer_id, integration_id, subject_name,
                message, detail, error_domain, error_category, error_origin, error_mutation, provenance)
            VALUES(?, {_NOW_SQL}, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(occurrence_key) DO NOTHING""",
        (
            _safe(event.occurrence_key, 240), event.category, event.event_type, event.severity, event.outcome,
            event.subject_kind, _safe(event.subject_id, 128),
            None if event.transfer_id is None else int(event.transfer_id),
            None if event.related_transfer_id is None else int(event.related_transfer_id),
            _safe(event.integration_id, 128), _safe(name, NAME_LIMIT),
            _safe(event.message, MESSAGE_LIMIT) or event.event_type, _safe(detail, DETAIL_LIMIT),
            _token(getattr(error, "domain", None)), _token(getattr(error, "category", None)),
            _token(getattr(error, "origin", None)), _token(getattr(error, "mutation", None)),
            _safe(event.provenance, 240),
        ),
    )
    return bool(cursor.rowcount)


async def record_now(event: JournalEvent) -> bool:
    """THE standalone path, for an occurrence whose action commits no database
    transition of its own and has already succeeded: one short transaction of
    its own, after the fact. A failure is logged and reported as False; it
    never undoes or fails the action, which is already true."""
    from db.database import get_db

    try:
        async with get_db() as db:
            await record(db, event)
            await db.commit()
        return True
    except Exception as exc:
        logger.warning("Event journal write failed for %s: %s", event.event_type, type(exc).__name__)
        return False


# ── derived search index ────────────────────────────────────────────────────


async def _fts_present(db) -> bool:
    if not fts_supported():
        return False
    row = await db.fetchone("SELECT 1 AS present FROM sqlite_master WHERE name=?", (FTS_TABLE,))
    return bool(row)


async def catch_up(limit: int = INDEX_BATCH) -> int:
    """Index up to ``limit`` journal rows above the watermark and advance it,
    in one transaction: a crash leaves both or neither, so no row is ever
    indexed twice or skipped. Returns how many rows it indexed."""
    from db.database import get_db

    async with get_db() as db:
        if not await _fts_present(db):
            return 0
        await db.execute("BEGIN IMMEDIATE")
        mark = await db.fetchone("SELECT indexed_through FROM event_journal_index WHERE id=1")
        through = int(mark["indexed_through"]) if mark else 0
        rows = await db.fetchall(
            "SELECT id, message, detail, subject_name FROM event_journal WHERE id > ? ORDER BY id LIMIT ?",
            (through, int(limit)))
        if not rows:
            await db.rollback()
            return 0
        await db.executemany(
            f"INSERT INTO {FTS_TABLE}(rowid, message, detail, subject_name) VALUES(?, ?, ?, ?)",
            [(row["id"], row["message"], row["detail"], row["subject_name"]) for row in rows])
        await db.execute("UPDATE event_journal_index SET indexed_through=? WHERE id=1", (rows[-1]["id"],))
        await db.commit()
        return len(rows)


async def reset_index(db=None) -> None:
    """Drop the derived index's contents and its watermark so ``catch_up``
    rebuilds it in bounded batches. Used when the index is found broken; on
    the caller's connection (inside its transaction) when one is given."""
    from db.database import get_db

    async def _reset(conn) -> None:
        if await _fts_present(conn):
            await conn.execute(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES('delete-all')")
        await conn.execute("UPDATE event_journal_index SET indexed_through=0 WHERE id=1")

    if db is not None:
        await _reset(db)
        return
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await _reset(conn)
        await conn.commit()


async def verify_index() -> bool:
    """FTS5's own structural integrity check of the derived index (internal
    consistency only: rows above the watermark are legitimately absent). A
    corrupt index -- one SQLite may otherwise answer from as if it were
    merely empty -- is dropped for a bounded rebuild. Returns False when it
    was. The journal itself is never touched."""
    from db.database import get_db

    async with get_db() as db:
        if not await _fts_present(db):
            return True
        try:
            await db.execute(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}, rank) VALUES('integrity-check', 0)")
            await db.commit()
            return True
        except sqlite3.DatabaseError as exc:
            await db.rollback()
            logger.warning("Event search index failed its integrity check and will be rebuilt: %s", exc)
    await reset_index()
    return False


async def clear(db) -> None:
    """The explicit whole-database reset's share: every journal row and the
    whole derived index, in the reset's own transaction. Ids keep ascending
    afterwards (the sequence is kept), so no navigation cursor taken before the
    reset can ever alias an event recorded after it."""
    await db.execute("DELETE FROM event_journal")
    if await _fts_present(db):
        await db.execute(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES('delete-all')")
    # Nothing at or below the last id ever allocated remains to be indexed.
    await db.execute("""UPDATE event_journal_index SET indexed_through=COALESCE(
        (SELECT seq FROM sqlite_sequence WHERE name='event_journal'), 0) WHERE id=1""")


async def pending_index_rows(db, bound: int) -> int:
    """Rows not yet indexed, counted up to ``bound`` (an exact count below it)."""
    mark = await db.fetchone("SELECT indexed_through FROM event_journal_index WHERE id=1")
    through = int(mark["indexed_through"]) if mark else 0
    row = await db.fetchone(
        "SELECT COUNT(*) AS n FROM (SELECT 1 FROM event_journal WHERE id > ? LIMIT ?)", (through, int(bound)))
    return int(row["n"]) if row else 0


# ── reading ─────────────────────────────────────────────────────────────────


class SearchRejected(ValueError):
    """An operator search this journal cannot answer as asked; ``message`` is
    safe to show."""


class SearchUnavailable(RuntimeError):
    """Text search cannot run on this installation right now; ``message`` is
    safe to show."""


@dataclass(frozen=True)
class SearchTerms:
    text: str | None
    transfer_id: int | None


def search_terms(raw: str | None) -> SearchTerms | None:
    """Literal, case-insensitive substring search over the message, detail and
    transfer name, plus -- for a bare number or ``#number`` -- the exact
    transfer id (every event correlated to that transfer). Text shorter than
    three characters cannot be answered from the index; a short number still
    matches the transfer id alone."""
    query = " ".join(str(raw or "").split())
    if not query:
        return None
    if len(query) > SEARCH_MAX_TEXT:
        raise SearchRejected(f"Search text is limited to {SEARCH_MAX_TEXT} characters.")
    reference = _TRANSFER_REF.fullmatch(query)
    transfer_id = int(reference.group(1)) if reference else None
    text = query if len(query) >= SEARCH_MIN_TEXT else None
    if text is None and transfer_id is None:
        raise SearchRejected(f"Search needs at least {SEARCH_MIN_TEXT} characters, or an exact transfer ID.")
    return SearchTerms(text, transfer_id)


def _phrase(text: str) -> str:
    # One FTS5 string: with the trigram tokenizer a string matches as a
    # contiguous, case-insensitive substring -- punctuation, spaces and quotes
    # included -- never as query syntax.
    return '"' + text.replace('"', '""') + '"'


async def page(db, *, limit: int, before: int | None, snapshot: int | None, category: str | None,
               severity: str | None, since: float | None, terms: SearchTerms | None) -> dict:
    """One newest-first page of the journal at or below ``snapshot``.

    Navigation is by the unique, commit-ordered id: ``before`` is the
    exclusive upper bound of an older page and ``snapshot`` freezes the
    investigation's newest edge, so events committed meanwhile never shift,
    duplicate or skip a page already navigated. Every predicate -- search
    included -- applies before LIMIT, and ``limit + 1`` rows decide
    ``has_more`` without counting the journal."""
    limit = max(1, min(int(limit), MAX_PAGE_SIZE))
    if snapshot is None:
        top = await db.fetchone("SELECT COALESCE(MAX(id), 0) AS id FROM event_journal")
        snapshot = int(top["id"])
    # ONE inclusive upper bound: SQLite seeks a rowid range on a single
    # bound, so two would walk every row between them.
    upper = int(snapshot) if before is None else min(int(snapshot), int(before) - 1)
    clauses = ["j.id <= ?"]
    params: list = [upper]
    if category:
        clauses.append("j.category = ?")
        params.append(category)
    if severity:
        clauses.append("j.severity = ?")
        params.append(severity)
    if since is not None:
        # The first id at or after the window start bounds the walk, so a
        # window never scans the history older than it.
        clauses.append("""j.id >= COALESCE((SELECT w.id FROM event_journal w WHERE w.occurred_at >= ?
            ORDER BY w.occurred_at LIMIT 1), 9223372036854775807) AND j.occurred_at >= ?""")
        params.extend([float(since), float(since)])
    search = {"text": "none", "pending": 0, "complete": True}
    source, order = "event_journal j", "j.id DESC"
    if terms is not None:
        alternatives = []
        alternative_params: list = []
        if terms.transfer_id is not None:
            alternatives.append("j.transfer_id = ? OR j.related_transfer_id = ?")
            alternative_params.extend([terms.transfer_id, terms.transfer_id])
        if terms.text is not None:
            if not await _fts_present(db):
                if terms.transfer_id is None:
                    raise SearchUnavailable("Text search is unavailable on this installation "
                                            "(its SQLite runtime has no FTS5 trigram support).")
                search = {"text": "unavailable", "pending": 0, "complete": False}
            else:
                pending = await pending_index_rows(db, INDEX_BATCH + 1)
                search = {"text": "indexed", "pending": pending, "complete": pending == 0}
                if terms.transfer_id is None:
                    # Text alone: the index drives the page, newest match
                    # first from the cursor bound, and every other filter is
                    # checked per match -- the walk stops at the page instead
                    # of first collecting every match the history holds.
                    source = f"{FTS_TABLE} f CROSS JOIN event_journal j ON j.id = f.rowid"
                    order = "f.rowid DESC"
                    clauses.insert(0, f"f.{FTS_TABLE} MATCH ? AND f.rowid <= ?")
                    params[:0] = [_phrase(terms.text), upper]
                else:
                    alternatives.append(f"j.id IN (SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ? "
                                        "AND rowid <= ?)")
                    alternative_params.extend([_phrase(terms.text), upper])
        if alternatives:
            clauses.append("(" + " OR ".join(alternatives) + ")")
            params.extend(alternative_params)
    rows = await db.fetchall(
        f"""SELECT j.id, j.occurred_at, j.category, j.event_type, j.severity, j.outcome, j.subject_kind,
                   j.subject_id, j.transfer_id, j.related_transfer_id, j.integration_id, j.subject_name,
                   j.message, j.detail, j.error_category, j.error_domain,
                   strftime('%Y-%m-%dT%H:%M:%fZ', j.occurred_at, 'unixepoch') AS occurred_at_iso,
                   EXISTS(SELECT 1 FROM torrents t WHERE t.id = j.transfer_id AND t.status != 'deleted')
                       AS transfer_available
            FROM {source} WHERE {' AND '.join(clauses)}
            ORDER BY {order} LIMIT ?""",
        [*params, limit + 1],
    )
    newer = await db.fetchone("SELECT EXISTS(SELECT 1 FROM event_journal WHERE id > ?) AS newer", (int(snapshot),))
    started = await db.fetchone("SELECT started_at FROM event_journal_index WHERE id=1")
    items = rows[:limit]
    return {
        "items": [_public(row) for row in items],
        "limit": limit,
        "snapshot": int(snapshot),
        "has_more": len(rows) > limit,
        "next_before": int(items[-1]["id"]) if len(rows) > limit and items else None,
        "newer_available": bool(newer and newer["newer"]),
        "history_started_at": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(started["started_at"])))
                               if started else None),
        "search": search,
    }


def _public(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "occurred_at": row["occurred_at_iso"],
        "category": row["category"],
        "type": row["event_type"],
        "severity": row["severity"],
        "outcome": row["outcome"],
        "subject_kind": row["subject_kind"],
        "subject_id": row["subject_id"],
        "transfer_id": row["transfer_id"],
        "related_transfer_id": row["related_transfer_id"],
        "transfer_available": bool(row["transfer_available"]),
        "integration_id": row["integration_id"],
        "name": row["subject_name"],
        "message": row["message"],
        "detail": row["detail"],
        "error_category": row["error_category"],
        "error_domain": row["error_domain"],
    }


async def transfer_events(db, transfer_id: int, limit: int = 50) -> list[dict]:
    """The newest events correlated to one transfer (Transfer Details): as
    its subject or as the related transfer -- the same correlation as the
    Activity Log's exact transfer id search."""
    return await db.fetchall(
        """SELECT id, CASE severity WHEN 'warning' THEN 'warn' ELSE severity END AS level, message,
                  strftime('%Y-%m-%dT%H:%M:%fZ', occurred_at, 'unixepoch') AS created_at
           FROM event_journal WHERE transfer_id=? OR related_transfer_id=? ORDER BY id DESC LIMIT ?""",
        (int(transfer_id), int(transfer_id), int(limit)))


async def recorded_count(db) -> int:
    """Every committed journal record -- the whole retained history, not a
    page, a search, the index watermark or one transfer's share. One scan of
    the narrowest covering index (about 5 ms at a million records, measured);
    read on demand, never cached."""
    row = await db.fetchone("SELECT COUNT(*) AS n FROM event_journal")
    return int(row["n"]) if row else 0


async def severity_counts(db, since: float | None) -> dict[str, int]:
    """Journal events per severity, optionally since an instant (Statistics)."""
    if since is None:
        rows = await db.fetchall("SELECT severity, COUNT(*) AS n FROM event_journal GROUP BY severity")
    else:
        rows = await db.fetchall("""SELECT severity, COUNT(*) AS n FROM event_journal
            WHERE id >= COALESCE((SELECT id FROM event_journal WHERE occurred_at >= ? ORDER BY occurred_at LIMIT 1),
                                 9223372036854775807) GROUP BY severity""", (float(since),))
    return {str(row["severity"]): int(row["n"]) for row in rows}
