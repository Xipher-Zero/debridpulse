"""THE operator search language of the Downloads and Activity Log lists.

One interpreter turns what an operator typed into a validated ``SearchPlan``;
each list's own query executor (``api.operational_downloads`` over the current
transfers, ``db.event_journal`` over the durable history) consumes that plan
and never inspects the raw text itself. Every result reports the same search
metadata (``SearchPlan.describe``), so both lists say the same thing about
what was searched and how complete the answer is.

The language:

- Ordinary text is a literal, case-insensitive substring, matched within one
  field at a time: no operators, wildcards, word splitting, fuzzy matching or
  ranking. ``100%``, ``file_name``, quotes, backslashes and brackets are just
  characters. Leading and trailing whitespace is ignored and every run of
  whitespace counts as one space, in the query and in the searched text alike.
- Text matches case-insensitively by the database engine's own case fold: the
  SQLite FTS5 trigram fold the Activity Log index applies. ``case_fold`` holds
  that same fold, read from the running SQLite engine, for matching done
  outside the index, so a character folds the same way on both lists. There is
  no Unicode normalization (precomposed and decomposed accents differ) and no
  diacritic folding.
- Text needs at least ``MIN_TEXT`` characters (the trigram index cannot answer
  less) and at most ``MAX_TEXT``; longer input is rejected, never truncated.
- ``#580`` is exactly transfer 580 and nothing else. A bare ``580`` is transfer
  580 OR the text "580"; a bare number shorter than ``MIN_TEXT`` digits is the
  transfer id alone, and the metadata says so. Ids are ASCII decimal digits:
  leading zeros are allowed (``#0580`` is 580), ``#0``, a sign, a non-digit or
  more than ``MAX_ID_DIGITS`` digits after ``#`` is invalid, and a bare number
  too large to be an id is searched as text only.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

MIN_TEXT = 3
MAX_TEXT = 200
MAX_ID_DIGITS = 18

_DIGITS = re.compile(r"[0-9]+")

# Search completeness: whether every eligible record was searched.
COMPLETE = "complete"
INDEX_INCOMPLETE = "index_incomplete"
UNAVAILABLE = "unavailable"
# Pagination finality, a separate fact: whether the list's last page and
# count are known exactly, can be found by one bounded count, or cannot be
# proven right now.
FINAL_EXACT = "exact"
FINAL_BOUNDED = "bounded"
FINAL_UNPROVEN = "unproven"


class InvalidSearch(ValueError):
    """What the operator typed is not a search this language can answer;
    ``str()`` is safe to show."""


@dataclass(frozen=True)
class SearchPlan:
    query: str
    text: str | None
    transfer_id: int | None
    notice: str | None = None

    @property
    def mode(self) -> str:
        if self.text is not None and self.transfer_id is not None:
            return "id_or_text"
        return "text" if self.text is not None else "id"

    def describe(self, status: str = COMPLETE, finality: str = FINAL_EXACT, pending: int = 0) -> dict:
        return describe(self, status, finality, pending)


def describe(plan: SearchPlan | None, status: str = COMPLETE, finality: str = FINAL_EXACT,
             pending: int = 0) -> dict:
    """The search metadata every list result carries."""
    return {
        "query": plan.query if plan else None,
        "mode": plan.mode if plan else "none",
        "transfer_id": plan.transfer_id if plan else None,
        "status": status,
        "pending": int(pending),
        "finality": finality,
        "notice": plan.notice if plan else None,
    }


def _reference(digits: str) -> int:
    if len(digits) > MAX_ID_DIGITS:
        raise InvalidSearch(f"A transfer ID has at most {MAX_ID_DIGITS} digits.")
    value = int(digits)
    if value < 1:
        raise InvalidSearch("Transfer IDs start at #1.")
    return value


def plan(raw: str | None) -> SearchPlan | None:
    """Interpret ``raw``: None for no search, else a validated plan.
    Raises ``InvalidSearch`` for anything this language cannot answer."""
    query = " ".join(str(raw or "").split())
    if not query:
        return None
    if len(query) > MAX_TEXT:
        raise InvalidSearch(f"Search is limited to {MAX_TEXT} characters.")
    if any(unicodedata.category(ch).startswith("C") for ch in query):
        raise InvalidSearch("Search can't contain control or invisible formatting characters.")
    if query.startswith("#"):
        digits = query[1:]
        if not _DIGITS.fullmatch(digits):
            raise InvalidSearch("Use # followed only by a transfer number, like #580.")
        return SearchPlan(query, None, _reference(digits))
    transfer_id = None
    if _DIGITS.fullmatch(query) and len(query) <= MAX_ID_DIGITS and int(query) > 0:
        transfer_id = int(query)
    if len(query) >= MIN_TEXT:
        return SearchPlan(query, query, transfer_id)
    if transfer_id is not None:
        return SearchPlan(query, None, transfer_id,
                          f"Showing transfer #{transfer_id} only: text search needs at least {MIN_TEXT} characters.")
    raise InvalidSearch(f"Search needs at least {MIN_TEXT} characters, or a transfer ID like #580.")


@lru_cache(maxsize=1)
def case_fold() -> dict[int, str]:
    """The running SQLite engine's FTS5 trigram case fold, as a translation
    table: each cased character the engine folds, mapped to what it folds to.
    Read once from the engine itself: each character is indexed between two
    fixed neighbours and its folded form is read back from the index's own
    vocabulary. Without FTS5 trigram the engine's only case-insensitive
    comparison is ASCII, and so is this."""
    ascii_fold = {code: chr(code + 32) for code in range(ord("A"), ord("Z") + 1)}
    cased = [char for char in map(chr, range(0x80, 0x30000))
             if (char.lower() != char or char.casefold() != char)
             and unicodedata.category(char) not in ("Cs", "Co", "Cn")]
    try:
        conn = sqlite3.connect(":memory:")
    except sqlite3.Error:
        return ascii_fold
    try:
        conn.execute("CREATE VIRTUAL TABLE probe USING fts5(text, tokenize='trigram')")
        conn.execute("CREATE VIRTUAL TABLE probe_terms USING fts5vocab(probe, 'instance')")
        conn.executemany("INSERT INTO probe(rowid, text) VALUES(?, ?)",
                         [(index, f"\x01{char}\x02") for index, char in enumerate(cased, start=1)])
        fold = dict(ascii_fold)
        for row, term in conn.execute("SELECT doc, term FROM probe_terms"):
            char = cased[int(row) - 1]
            if len(term) == 3 and term[1] != char:
                fold[ord(char)] = term[1]
        return fold
    except sqlite3.Error:
        return ascii_fold
    finally:
        conn.close()


def fold(value) -> str:
    """``value`` as the language compares it: whitespace runs collapsed to one
    space, then case-folded by ``case_fold``."""
    return " ".join(str(value or "").split()).translate(case_fold())
