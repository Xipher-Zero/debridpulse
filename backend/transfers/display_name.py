"""Canonical human-facing transfer display name.

Presentation only: never rewrites persisted transfer identity, source/request
provenance, or acquisition candidate truth. Consumes bounded artifact-name
facts the caller already has in hand (current authoritative artifact
filenames) and produces one deterministic, conservative display string.

Historical precedent (DP 1.0.12 UI presentation pass, Section 15/28 audit):
``backend/services/manager_v2.py`` (commit ``4372cec2``, "Derive useful
direct-link batch names") introduced ``direct_link_collection_name`` /
``_direct_link_collection_base`` to build a useful parent label for a
direct-link submission batch from resolved filenames, with an explicit
conservative rule: recognize a small, fixed set of multipart/split-archive
filename patterns and collapse them to a shared stem; never invent a common
name across unrelated filenames. That logic survived the Universal Transfer
Core rewrite (``a1a33583``) as ``transfers.requests.direct_link_collection_name``
/ ``_direct_link_collection_base`` and remains live today, called once at
direct-link SUBMISSION time with an always-empty ``resolved_names`` list
(``application/service.py``) — so it can never reflect real artifact
filenames once they become known, and the raw root/request name (e.g.
``1fichier.com - <hash> + 23 more``) is what list surfaces have shown ever
since. This module reuses that same proven pattern-matching core (the three
multipart/split-archive regexes and the case-insensitive common-base check)
against the transfer's CURRENT authoritative artifact filenames instead, so
the display name reflects what is actually being downloaded. Only the final
wording changes, to match this task's explicit total-files preference
(Section 16) over the historical source-ingest "N links"/"N more" phrasing,
which was written for a link count known before resolution, not a file count
known after it.
"""
from __future__ import annotations

import re
from typing import Optional, Sequence

_UNNAMED = "(unnamed)"

_MULTIPART_BASE_PATTERNS = (
    re.compile(r"(?i)^(?P<base>.+)\.part\d+\.rar$"),
    re.compile(r"(?i)^(?P<base>.+)\.r\d{2,3}$"),
    re.compile(r"(?i)^(?P<base>.+)\.(?:7z|zip|rar)\.\d{3}$"),
)


def _collection_base(filename: str) -> str:
    """Return a conservative collection stem for a known multipart filename.

    Only the fixed set of multipart/split-archive suffixes above are ever
    collapsed; an unrecognized filename is returned unchanged so an unrelated
    name never gets silently folded into a fabricated common base.
    """
    name = str(filename or "").strip()
    for pattern in _MULTIPART_BASE_PATTERNS:
        match = pattern.match(name)
        if match:
            base = match.group("base").rstrip(" .-_")
            if base:
                return base
    return name


def normalized_transfer_display_name(
    artifact_filenames: Sequence[str],
    root_name: Optional[str] = None,
) -> str:
    """Derive the canonical human-facing transfer title.

    Preference order (Section 15 of the DP 1.0.12 presentation task):
    1. a normalized artifact-derived name (single file, or a safe common
       collection base across multiple related files);
    2. a representative artifact filename plus the remaining file count, when
       no safe common base can be derived without over-normalizing;
    3. the root/request ``name`` fallback, only when no artifact filenames
       are available at all;
    4. a final generic "(unnamed)" fallback.

    Deterministic and conservative: never strips words merely because they
    differ, never derives a name from source host/provider/candidate/request
    identity, never infers metadata absent from the filenames themselves.

    Count wording is form-dependent, by exactly what is visibly shown before
    the "+":

    - COLLECTION-IDENTITY form (a derived common base, e.g. "Example.Release"
      — not itself one of the N filenames): the count is the TOTAL file
      count, because none of the N files are separately named already.
      Example: 24 files ``Example.Release.part01.rar`` .. ``part24.rar`` ->
      ``"Example.Release + 24 files"``.
    - REPRESENTATIVE-FILENAME form (no safe common base; the first actual
      filename is shown as-is): the count is TOTAL MINUS ONE, because that
      shown filename already visibly accounts for one of the N files and
      counting it again would overstate the total. Example: a 24-file set
      whose members do not share a recognized common base, with
      ``Example.Release.part01.rar`` as the first filename -> exactly
      ``"Example.Release.part01.rar + 23 files"``.
    """
    names = [str(name).strip() for name in (artifact_filenames or []) if str(name or "").strip()]
    if not names:
        fallback = str(root_name or "").strip()
        return fallback or _UNNAMED

    if len(names) == 1:
        return names[0]

    total = len(names)
    bases = [_collection_base(name) for name in names]
    first_base = bases[0]
    if first_base and all(base.casefold() == first_base.casefold() for base in bases[1:]):
        # The derived base is not itself one of the N filenames: count every
        # file (Section 16's "Example.Release + 24 files" — total, not N-1).
        return f"{first_base} + {total} files"

    # No safe common base: a representative filename (itself one of the N)
    # plus how many OTHER files exist, rather than an aggressive/wrong guess
    # (Section 16's "Example.Release.part01.rar + 23 files" — total-1, since
    # the shown name already accounts for one of the files).
    return f"{names[0]} + {total - 1} files"
