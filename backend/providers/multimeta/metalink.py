"""Metalink4 (RFC 5854) descriptor interpretation: pure, bounded, no I/O.

What a descriptor says, read into neutral facts and nothing more: each
``<file>``'s safe relative path, declared size, whole-file hashes and its
ordinary ``<url>`` source references in the publisher's order of preference.
How those sources are routed, chosen between, retried, verified or executed is
never decided here.

Parsed with the standard library's expat under the same refusal the WebDAV
reader applies: no document type, no entity declaration and no external entity
at all -- a Metalink document needs none, so any of them makes it malformed
rather than something to resolve. Every count and every text is bounded, and
nothing is ever truncated into a result that looks complete: a document past a
bound is refused whole.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urljoin, urlsplit
from xml.parsers import expat

from transfers.file_selection import ManifestInvalid, normalize_relative_path

NAMESPACE = "urn:ietf:params:xml:ns:metalink"
# Metalink 3 -- an earlier, different format, never reinterpreted as 4.
LEGACY_NAMESPACE = "http://www.metalinker.org/"

# Hard safety bounds of one descriptor -- internal, never operator tuning. The
# document bound is the ceiling DP already applies to an uploaded torrent
# metafile, the other descriptor format it accepts.
MAX_DESCRIPTOR_BYTES = 16 * 1024 * 1024
MAX_FILES = 10_000
MAX_SOURCES_PER_FILE = 256
# Every source becomes one ordinary request: the fan-out is bounded too.
MAX_SOURCES = 20_000
MAX_HASHES_PER_FILE = 32
MAX_PIECE_HASHES = 1_000_000
MAX_TEXT = 64 * 1024
MAX_DEPTH = 16
_PRIORITY_RANGE = range(1, 1_000_000)
_MAX_SIZE = 2 ** 63 - 1

# RFC 5854 hash types (IANA "Hash Function Textual Names") whose whole-file
# digest DP's material verification computes, as the names it computes them by.
_HASH_TYPES = {"sha-256": ("sha256", 64), "sha-512": ("sha512", 128), "sha-1": ("sha1", 40), "md5": ("md5", 32)}
_HEX = re.compile(r"[0-9a-fA-F]+")
_DECIMAL = re.compile(r"[0-9]{1,19}")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")


class InvalidDescriptor(Exception):
    """A descriptor DP does not interpret. ``reason`` is one of ``malformed``,
    ``unsupported`` (not Metalink4), ``too_large`` (past a safety bound) or
    ``unsafe_path``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class DescribedFile:
    """One logical file a descriptor describes.

    ``sources``: the absolute addresses of its usable ``<url>`` references,
    in the publisher's order of preference. ``unusable``: why it has none --
    ``metaurl_only`` (only metadata references, never followed here) or
    ``no_usable_url``; ``""`` when it has sources."""
    path: str
    size: int = 0
    hashes: tuple[tuple[str, str], ...] = ()
    sources: tuple[str, ...] = ()
    unusable: str = ""


def _number(value: str, *, allowed=None) -> int:
    if not _DECIMAL.fullmatch(value):
        raise InvalidDescriptor("malformed")
    number = int(value)
    if number > _MAX_SIZE or (allowed is not None and number not in allowed):
        raise InvalidDescriptor("malformed")
    return number


def _source(value: str, base: str | None) -> str:
    """The absolute address one ``<url>`` names, or ``""`` when it names none
    DP can route: relative without a base, credentials written into it,
    another descriptor, or not an address at all. Whether its scheme is
    supported is routing's."""
    if not value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return ""
    try:
        address = urljoin(base, value) if base else value
        parts = urlsplit(address)
        hostname = parts.hostname
        parts.port  # noqa: B018 -- raises for a malformed port
    except ValueError:
        return ""
    if not _SCHEME.fullmatch(parts.scheme or "") or not hostname:
        return ""
    if parts.username is not None or parts.password is not None:
        # A credential has no place in a published descriptor, and none is
        # carried into a request on its behalf.
        return ""
    if parts.path.casefold().endswith(".meta4"):
        # Another descriptor is a metadata reference, never a source of this
        # file: it is not followed.
        return ""
    return address


def parse(document: bytes, *, base: str | None = None) -> tuple[DescribedFile, ...]:
    """Every ``<file>`` of one Metalink4 document.

    ``base``: the validated address the document was finally read from, the
    only base a relative ``<url>`` may resolve against; ``None`` for a document
    that has none (an upload), so a relative reference is unusable there."""
    if not isinstance(document, (bytes, bytearray)) or len(document) > MAX_DESCRIPTOR_BYTES:
        raise InvalidDescriptor("too_large")
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    def refuse(*_arguments):
        raise InvalidDescriptor("malformed")

    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    stack: list[str] = []
    text: list[list[str]] = []
    lengths: list[int] = []
    files: list[DescribedFile] = []
    current: dict | None = None
    counts = {"sources": 0, "pieces": 0}

    def start(name, attributes):
        nonlocal current
        if not stack:
            namespace = name.split(" ", 1)[0] if " " in name else ""
            if namespace == LEGACY_NAMESPACE or name != f"{NAMESPACE} metalink":
                raise InvalidDescriptor("unsupported")
        if len(stack) >= MAX_DEPTH:
            raise InvalidDescriptor("too_large")
        parent = stack[-1] if stack else ""
        stack.append(name)
        text.append([])
        lengths.append(0)
        if name == f"{NAMESPACE} file" and parent == f"{NAMESPACE} metalink":
            if len(files) >= MAX_FILES:
                raise InvalidDescriptor("too_large")
            current = {"name": attributes.get("name", ""), "size": None, "hashes": [], "sources": [],
                       "references": 0, "hash_elements": 0, "hash_type": None, "priority": None}
        elif current is not None and parent == f"{NAMESPACE} file":
            if name in {f"{NAMESPACE} url", f"{NAMESPACE} metaurl"}:
                current["references"] += 1
                if current["references"] > MAX_SOURCES_PER_FILE:
                    raise InvalidDescriptor("too_large")
                priority = attributes.get("priority")
                current["priority"] = None if priority is None else _number(priority.strip(), allowed=_PRIORITY_RANGE)
            elif name == f"{NAMESPACE} hash":
                current["hash_elements"] += 1
                if current["hash_elements"] > MAX_HASHES_PER_FILE:
                    raise InvalidDescriptor("too_large")
                current["hash_type"] = str(attributes.get("type", "")).strip().casefold()
        elif current is not None and name == f"{NAMESPACE} hash" and parent == f"{NAMESPACE} pieces":
            # Piece hashes are only bounded: lifecycle verification is whole-file.
            counts["pieces"] += 1
            if counts["pieces"] > MAX_PIECE_HASHES:
                raise InvalidDescriptor("too_large")

    def characters(value):
        lengths[-1] += len(value)
        if lengths[-1] > MAX_TEXT:
            raise InvalidDescriptor("too_large")
        text[-1].append(value)

    def end(name):
        nonlocal current
        value = "".join(text.pop()).strip()
        lengths.pop()
        stack.pop()
        parent = stack[-1] if stack else ""
        if current is None:
            return
        if parent == f"{NAMESPACE} file":
            if name == f"{NAMESPACE} url":
                counts["sources"] += 1
                if counts["sources"] > MAX_SOURCES:
                    raise InvalidDescriptor("too_large")
                # Ordered by declared priority (lower first, RFC 5854 4.2.16.1);
                # an undeclared one after every declared one; ties in document
                # order -- never shuffled.
                order = current["priority"] if current["priority"] is not None else _PRIORITY_RANGE.stop
                current["sources"].append((order, len(current["sources"]), _source(value, base)))
            elif name == f"{NAMESPACE} size":
                if current["size"] is not None:
                    raise InvalidDescriptor("malformed")
                current["size"] = _number(value)
            elif name == f"{NAMESPACE} hash":
                known = _HASH_TYPES.get(current["hash_type"] or "")
                if known is not None:
                    algorithm, length = known
                    if len(value) != length or not _HEX.fullmatch(value):
                        raise InvalidDescriptor("malformed")
                    current["hashes"].append((algorithm, value.lower()))
        elif name == f"{NAMESPACE} file" and parent == f"{NAMESPACE} metalink":
            files.append(_described(current))
            current = None

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    try:
        parser.Parse(bytes(document), True)
    except expat.ExpatError as exc:
        raise InvalidDescriptor("malformed") from exc
    if not files:
        raise InvalidDescriptor("malformed")
    if len({item.path.casefold() for item in files}) != len(files):
        # One name, one file (RFC 5854 4.1.2.1): two would race one target.
        raise InvalidDescriptor("malformed")
    return tuple(files)


def _described(current: dict) -> DescribedFile:
    name = str(current["name"] or "")
    if not name.strip():
        raise InvalidDescriptor("malformed")
    if name.startswith("/") or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise InvalidDescriptor("unsafe_path")
    try:
        path = normalize_relative_path(name)
    except ManifestInvalid as exc:
        raise InvalidDescriptor("unsafe_path" if str(exc) == "unsafe_path" else "malformed") from exc
    if not current["references"]:
        # Every file names at least one source (RFC 5854 4.1.2).
        raise InvalidDescriptor("malformed")
    ordered = [address for _order, _index, address in sorted(current["sources"]) if address]
    sources = tuple(dict.fromkeys(ordered))
    unusable = "" if sources else ("metaurl_only" if not current["sources"] else "no_usable_url")
    return DescribedFile(path, current["size"] or 0, tuple(dict.fromkeys(current["hashes"])), sources, unusable)
