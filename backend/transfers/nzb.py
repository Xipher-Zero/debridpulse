"""THE NZB source-fact owner: validation, normalization and naming facts.

Neutral: any route an NZB can take (native Usenet, a provider that acquires it
remotely) reads submitted NZB bytes through this one reader, and no route
owns it. Knows nothing about any executor, SAB, NNTP transport, provider or
the download root: it turns an NZB manifest into neutral facts (a name, a
declared byte total, the file and segment counts) or rejects it. Parsing is
defensive -- an NZB is untrusted operator input -- and never resolves
external entities.

It also owns the NZB naming facts a remotely acquired posting needs to be
named as native Usenet names it (``useful_name``, ``dominant_member``,
``is_probably_obfuscated``): the narrow subset of SABnzbd 5.1.3's
deobfuscation -- the version DebridPulse bundles -- that decides when one
dominant, obfuscated payload takes the posting's useful name.

Parsing is bounded. A real posting's manifest is routinely tens or hundreds of
megabytes of XML, and building a document tree for one costs multiples of its
size in resident memory: 1721 MiB measured for a 256 MiB manifest. The reader
below streams instead, discarding every element as soon as its contribution has
been validated and counted, which measured 17.8 MiB for the same input --
effectively constant in manifest size, and faster. The canonical facts it
produces are byte-identical to those the document-tree reader produced.
"""
from __future__ import annotations

from dataclasses import dataclass
import io
import os
import re
from xml.etree import ElementTree

from transfers.filesystem import safe_name

# Structural ceilings. A manifest may legitimately be very large, so size alone
# is a poor guard; these bound the shapes an abusive document can take without
# constraining any real posting. The byte ceiling for a submitted input belongs
# to its one owner, ``transfers.staged_input.MAX_STAGED_INPUT_BYTES``, and is
# deliberately NOT restated here.
MAX_NZB_FILES = 1_000_000
MAX_NZB_SEGMENTS = 50_000_000

# yEnc subjects conventionally quote the posted filename. Element names are
# matched without their namespace, so a manifest is accepted with or without the
# canonical NZB namespace declaration, exactly as before.
_QUOTED_NAME = re.compile(r'"([^"]{1,255})"')


@dataclass(frozen=True)
class NzbManifest:
    """Neutral facts a validated NZB asserts about the posted material."""
    name: str
    declared_bytes: int
    file_count: int
    segment_count: int


class InvalidNzb(ValueError):
    """The payload is not a usable NZB manifest."""


def _local(tag: str) -> str:
    """An element's name without its namespace, if it has one."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _posted_name(subject: str) -> str:
    match = _QUOTED_NAME.search(subject or "")
    return match.group(1).strip() if match else ""


def _segment_bytes(element) -> int:
    try:
        size = int(str(element.get("bytes") or "0").strip())
    except ValueError as exc:
        raise InvalidNzb("NZB segment declares a non-numeric size") from exc
    if size < 0:
        raise InvalidNzb("NZB segment declares a negative size")
    if not (element.text or "").strip():
        raise InvalidNzb("NZB segment declares no message identifier")
    return size


def read(stream, *, fallback_name: str = "") -> NzbManifest:
    """Validate an NZB read from ``stream`` and return its neutral facts.

    Bounded: the reader holds one element at a time. A ``segment`` is cleared
    once its size and message identifier are accounted for, and a ``file`` once
    its segments have been counted, so nothing accumulates however large the
    manifest is.

    Raises ``InvalidNzb`` for anything that is not a structurally complete NZB
    describing at least one file with at least one segment.
    """
    declared = 0
    files = 0
    segments = 0
    file_segments = 0
    first_name = ""
    root_seen = False
    in_file = False

    # The same parser the document-tree reader used: it resolves no external
    # entity and performs no network access.
    try:
        for event, element in ElementTree.iterparse(stream, events=("start", "end")):
            name = _local(element.tag)
            if event == "start":
                if not root_seen:
                    root_seen = True
                    if name != "nzb":
                        raise InvalidNzb("Document root is not an NZB manifest")
                elif name == "file":
                    in_file = True
                    file_segments = 0
                continue
            if name == "segment":
                declared += _segment_bytes(element)
                segments += 1
                file_segments += 1
                if segments > MAX_NZB_SEGMENTS:
                    raise InvalidNzb("NZB manifest declares too many segments")
                element.clear()
            elif name == "file":
                if not file_segments:
                    raise InvalidNzb("NZB manifest declares a file with no segments")
                files += 1
                if files > MAX_NZB_FILES:
                    raise InvalidNzb("NZB manifest declares too many files")
                if not first_name:
                    first_name = _posted_name(element.get("subject") or "")
                in_file = False
                element.clear()
            elif name == "nzb":
                element.clear()
    except ElementTree.ParseError as exc:
        raise InvalidNzb("NZB payload is not well-formed XML") from exc

    if not root_seen:
        raise InvalidNzb("NZB payload is not well-formed XML")
    if in_file:
        raise InvalidNzb("NZB payload is not well-formed XML")
    if not files:
        raise InvalidNzb("NZB manifest declares no files")

    name = first_name or str(fallback_name or "").strip()
    if name.lower().endswith(".nzb"):
        name = name[: -len(".nzb")]
    if not name:
        raise InvalidNzb("NZB manifest asserts no usable name")
    return NzbManifest(name=name, declared_bytes=declared, file_count=files,
                       segment_count=segments)


def parse(payload: bytes, *, fallback_name: str = "") -> NzbManifest:
    """Validate an in-memory NZB. One implementation: it reads the same way."""
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        raise InvalidNzb("NZB payload is empty")
    return read(io.BytesIO(bytes(payload)), fallback_name=fallback_name)


# -- naming facts: SABnzbd 5.1.3 ``sabnzbd/deobfuscate_filenames.py`` and
# ``sabnzbd/filesystem.py``, as bundled -- exactly this subset, nothing else of
# SAB's post-processing (no content sniffing, PAR2, subtitles or lookalikes).

# A work name ends in none of these (``filesystem.strip_extensions``).
_WORK_NAME_SUFFIXES = (".nzb", ".par", ".par2")
# A dominant payload with one of these extensions is never renamed
# (``deobfuscate_filenames.EXCLUDED_FILE_EXTS``).
DEOBFUSCATION_EXCLUDED_EXTENSIONS = frozenset({
    ".vob", ".rar", ".par2", ".mts", ".m2ts", ".cpi", ".clpi", ".mpl", ".mpls", ".bdm", ".bdmv"})
# The dominant payload is "much bigger" than the next only beyond this ratio,
# strictly (``deobfuscate_filenames.get_biggest_file``: ``factor > 3``).
DOMINANCE_RATIO = 3


def useful_name(name: str) -> str:
    """The posting's useful work name: ``name`` with every terminal ``.nzb``,
    ``.par`` or ``.par2`` removed (case-insensitive), made safe as one path
    component; ``""`` when nothing useful remains."""
    stem = str(name or "").strip()
    base, extension = os.path.splitext(stem)
    while extension.lower() in _WORK_NAME_SUFFIXES:
        stem = base
        base, extension = os.path.splitext(stem)
    stem = stem.strip()
    return safe_name(stem) if stem else ""


def dominant_member(sizes) -> int | None:
    """The index of the dominant payload among exact positive ``sizes``: the
    only one, or the largest when it is more than ``DOMINANCE_RATIO`` times
    the next largest. ``None`` when none is dominant -- order never decides."""
    sizes = list(sizes)
    if len(sizes) == 1:
        return 0
    ranked = sorted(range(len(sizes)), key=lambda index: sizes[index], reverse=True)
    if len(ranked) < 2 or sizes[ranked[1]] <= 0:
        return None
    return ranked[0] if sizes[ranked[0]] / sizes[ranked[1]] > DOMINANCE_RATIO else None


def is_probably_obfuscated(basename: str) -> bool:
    """SABnzbd 5.1.3 ``is_probably_obfuscated`` for a basename WITHOUT its
    extension: certain obfuscation patterns first, then the human-readable
    signals that establish a meaningful name, obfuscated by default."""
    name = str(basename or "")
    if not name:
        return True
    if re.findall(r"^[a-f0-9]{32}$", name):
        return True
    if re.findall(r"^[a-f0-9.]{40,}$", name):
        return True
    if re.findall(r"[a-f0-9]{30}", name) and len(re.findall(r"\[\w+\]", name)) >= 2:
        return True
    if re.findall(r"^abc\.xyz", name):
        return True
    decimals = sum(1 for c in name if c.isnumeric())
    upperchars = sum(1 for c in name if c.isupper())
    lowerchars = sum(1 for c in name if c.islower())
    spacesdots = sum(1 for c in name if c in " ._")
    if upperchars >= 2 and lowerchars >= 2 and spacesdots >= 1:
        return False
    if spacesdots >= 3:
        return False
    if (upperchars + lowerchars >= 4) and decimals >= 4 and spacesdots >= 1:
        return False
    if name[0].isupper() and lowerchars > 2 and upperchars / lowerchars <= 0.25:
        return False
    return True


def deobfuscated_name(work_name: str, filename: str) -> str | None:
    """The name a dominant payload ``filename`` takes from the posting's
    ``work_name``: the useful work name plus the payload's own (lower-cased)
    extension -- only when that extension is not excluded and the payload's
    basename is probably obfuscated. ``None`` keeps ``filename``."""
    useful = useful_name(work_name)
    base, extension = os.path.splitext(str(filename or ""))
    extension = extension.lower()
    if not useful or extension in DEOBFUSCATION_EXCLUDED_EXTENSIONS or not is_probably_obfuscated(base):
        return None
    renamed = useful + extension
    return renamed if renamed != filename else None
