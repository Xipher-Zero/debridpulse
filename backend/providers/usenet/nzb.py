"""Provider-local NZB validation and normalization.

Knows nothing about any executor, SAB, NNTP transport or the download root: it
turns an NZB manifest into neutral facts (a name, a declared byte total, the
file and segment counts) or rejects it. Parsing is defensive -- an NZB is
untrusted operator input -- and never resolves external entities.

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
import re
from xml.etree import ElementTree

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
