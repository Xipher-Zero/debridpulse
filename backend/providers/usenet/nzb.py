"""Provider-local NZB validation and normalization.

Knows nothing about any executor, SAB, NNTP transport or the download root: it
turns NZB bytes into neutral facts (a name, a declared byte total, the file
count) or rejects them. Parsing is defensive -- an NZB is untrusted operator
input -- and never resolves external entities.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from xml.etree import ElementTree

# The NZB payload ceiling. An NZB is a small XML manifest; anything larger is
# not a manifest DebridPulse will parse.
MAX_NZB_BYTES = 16 * 1024 * 1024

_NAMESPACE = "{http://www.newzbin.com/DTD/2003/nzb}"
# yEnc subjects conventionally quote the posted filename.
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


def _files(root):
    """Every ``file`` element, with or without the canonical NZB namespace."""
    found = root.findall(f"{_NAMESPACE}file") or root.findall("file")
    if root.tag not in (f"{_NAMESPACE}nzb", "nzb"):
        raise InvalidNzb("Document root is not an NZB manifest")
    return found


def _segments(element):
    groups = element.find(f"{_NAMESPACE}segments")
    if groups is None:
        groups = element.find("segments")
    if groups is None:
        return []
    return list(groups.findall(f"{_NAMESPACE}segment")) or list(groups.findall("segment"))


def _posted_name(subject: str) -> str:
    match = _QUOTED_NAME.search(subject or "")
    return match.group(1).strip() if match else ""


def parse(payload: bytes, *, fallback_name: str = "") -> NzbManifest:
    """Validate ``payload`` and return its neutral manifest facts.

    Raises ``InvalidNzb`` for anything that is not a structurally complete NZB
    describing at least one file with at least one segment.
    """
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        raise InvalidNzb("NZB payload is empty")
    if len(payload) > MAX_NZB_BYTES:
        raise InvalidNzb("NZB payload exceeds the supported size")
    # A defused parser: entity expansion and external entities are refused by
    # ElementTree's default parser, which never resolves them.
    try:
        root = ElementTree.fromstring(bytes(payload))
    except ElementTree.ParseError as exc:
        raise InvalidNzb("NZB payload is not well-formed XML") from exc

    files = _files(root)
    if not files:
        raise InvalidNzb("NZB manifest declares no files")

    declared = 0
    segments = 0
    first_name = ""
    for element in files:
        parts = _segments(element)
        if not parts:
            raise InvalidNzb("NZB manifest declares a file with no segments")
        for part in parts:
            try:
                size = int(str(part.get("bytes") or "0").strip())
            except ValueError as exc:
                raise InvalidNzb("NZB segment declares a non-numeric size") from exc
            if size < 0:
                raise InvalidNzb("NZB segment declares a negative size")
            if not (part.text or "").strip():
                raise InvalidNzb("NZB segment declares no message identifier")
            declared += size
            segments += 1
        if not first_name:
            first_name = _posted_name(element.get("subject") or "")

    name = first_name or str(fallback_name or "").strip()
    if name.lower().endswith(".nzb"):
        name = name[: -len(".nzb")]
    if not name:
        raise InvalidNzb("NZB manifest asserts no usable name")
    return NzbManifest(name=name, declared_bytes=declared, file_count=len(files),
                       segment_count=segments)
