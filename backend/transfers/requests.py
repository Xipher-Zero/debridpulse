"""Provider-neutral request identity parsing."""
import codecs
import csv
import hashlib
import base64
from dataclasses import dataclass
import io
import json
import re
from pathlib import PurePosixPath
from urllib.parse import quote, urlparse, urlsplit, unquote
from typing import Optional, List, Set
from xml.parsers import expat

import yaml

from transfers.filesystem import safe_name

MAX_DIRECT_LINKS_PER_BATCH = 100
import bencode2


class TorrentMetainfoRejected(ValueError):
    """A ``.torrent`` metainfo DebridPulse does not accept, with an
    operator-actionable message."""


V2_ONLY_TORRENT_MESSAGE = ("This .torrent is BitTorrent v2-only, which is not supported. "
                           "Use a v1 or hybrid .torrent, or the torrent's magnet link.")
INVALID_TORRENT_MESSAGE = "Invalid torrent metainfo"


def _bencode_element_end(data: bytes, start: int) -> int:
    """The offset just past the one bencoded element starting at ``start``,
    scanned without decoding (iteratively, so nesting depth costs no stack).
    Structural only: ``bencode2`` validates the element afterwards."""
    depth, index, size = 0, start, len(data)
    while True:
        if index >= size:
            raise ValueError("truncated bencode")
        token = data[index]
        if token in b"dl":
            depth, index = depth + 1, index + 1
        elif token == ord("e"):
            if depth == 0:
                raise ValueError("unexpected end marker")
            depth, index = depth - 1, index + 1
        elif token == ord("i"):
            index = data.index(b"e", index) + 1
        elif 48 <= token <= 57:
            colon = data.index(b":", index)
            index = colon + 1 + int(data[index:colon])
            if index > size:
                raise ValueError("truncated string")
        else:
            raise ValueError("invalid bencode token")
        if depth == 0:
            return index


def _raw_info(data: bytes) -> bytes:
    """The ``info`` value's ORIGINAL encoded bytes, sliced from the metainfo
    exactly as submitted -- never re-encoded -- after the whole metainfo has
    been validated by the strict decoder."""
    if not isinstance(data, (bytes, bytearray)) or not data[:1] == b"d":
        raise ValueError("metainfo is not a dictionary")
    data = bytes(data)
    if not isinstance(bencode2.bdecode(data), dict):
        raise ValueError("metainfo is not a dictionary")
    index, raw = 1, None
    while data[index:index + 1] != b"e":
        key_end = _bencode_element_end(data, index)
        key = bencode2.bdecode(data[index:key_end])
        value_end = _bencode_element_end(data, key_end)
        if key == b"info":
            raw = data[key_end:value_end]
        index = value_end
    if index + 1 != len(data) or raw is None:
        raise ValueError("metainfo has no info dictionary")
    return raw


@dataclass(frozen=True)
class TorrentIdentity:
    """A validated torrent metainfo: its v1 info-hash -- SHA-1 over the
    ``info`` dictionary's original encoded bytes -- whether it is a hybrid
    (v1 + v2) torrent, whose v1 side is what DebridPulse uses, and the
    torrent's own declared ``info.name`` when that is one safe, strict UTF-8
    name (``None`` otherwise: no naming authority)."""
    info_hash: str
    hybrid: bool
    raw_info: bytes
    name: str | None = None


def torrent_identity(data: bytes) -> TorrentIdentity:
    """THE validation of an uploaded ``.torrent``: raises
    :class:`TorrentMetainfoRejected` for anything DebridPulse cannot identify.

    The info-hash is SHA-1 over the ``info`` value's original bytes. The
    strict decoder refuses non-canonical encodings (unsorted or duplicate
    keys, leading zeros), and the raw bytes must re-encode to themselves, so
    the hash can never be of a reconstructed structure. A v2-only torrent
    (``meta version`` 2 without the v1 ``pieces``) has no v1 info-hash at all
    and is refused with an actionable message rather than given an invented
    one; a hybrid keeps its v1 identity."""
    try:
        raw = _raw_info(data)
        info = bencode2.bdecode(raw)
        canonical = isinstance(info, dict) and bencode2.bencode(info) == raw
    except Exception:
        raise TorrentMetainfoRejected(INVALID_TORRENT_MESSAGE) from None
    if not canonical:
        raise TorrentMetainfoRejected(INVALID_TORRENT_MESSAGE)
    if info.get(b"meta version") == 2 and b"pieces" not in info:
        raise TorrentMetainfoRejected(V2_ONLY_TORRENT_MESSAGE)
    # SHA-1 is mandated by the BitTorrent v1 info-hash protocol and is not
    # used here for a security decision.
    return TorrentIdentity(hashlib.sha1(raw, usedforsecurity=False).hexdigest(),
                           info.get(b"meta version") == 2, raw, _utf8_segment(info.get(b"name")))


def extract_hash_from_torrent(data: bytes) -> str:
    """The BitTorrent v1 info-hash of a validated metainfo payload
    (``torrent_identity``), or ``""`` for anything it refuses -- including a
    v2-only torrent, which has no v1 info-hash to report."""
    try:
        return torrent_identity(data).info_hash
    except TorrentMetainfoRejected:
        return ""


@dataclass(frozen=True)
class TorrentMemberTree:
    """An identified torrent's own declared member tree (v1 side): its
    ``info.name`` and every file as ``(path, exact size)``. For a multi-file
    torrent the paths are inside the collection named ``name``; a single-file
    torrent is the one member ``(name, length)``."""
    info_hash: str
    name: str
    members: tuple[tuple[str, int], ...]
    multi_file: bool


def _utf8_segment(value) -> str | None:
    if not isinstance(value, bytes):
        return None
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return None if text in {"", ".", ".."} or "/" in text or "\\" in text or "\x00" in text else text


def torrent_member_tree(data: bytes) -> TorrentMemberTree | None:
    """The declared member tree of a validated ``.torrent`` (``torrent_identity``),
    read only when identity evidence is needed; ``None`` when the metainfo is
    not identifiable or its tree is not unambiguous text.

    Names and path segments must be strict UTF-8 and individually safe; they
    are taken exactly (no Unicode normalization). BEP 47 padding files
    (``attr`` containing ``p``) are alignment filler, not content, and are
    left out. A member of size zero is kept, so a proof that needs exact
    positive sizes fails closed on it."""
    try:
        identity = torrent_identity(data)
        info = bencode2.bdecode(identity.raw_info)
    except Exception:
        return None
    name = identity.name
    if name is None:
        return None
    files = info.get(b"files")
    if files is None:
        length = info.get(b"length")
        if isinstance(length, bool) or not isinstance(length, int) or length < 0:
            return None
        return TorrentMemberTree(identity.info_hash, name, ((name, length),), False)
    if not isinstance(files, list) or not files:
        return None
    members = []
    for item in files:
        if not isinstance(item, dict):
            return None
        attr = item.get(b"attr", b"")
        if isinstance(attr, bytes) and b"p" in attr:
            continue
        length, path = item.get(b"length"), item.get(b"path")
        if isinstance(length, bool) or not isinstance(length, int) or length < 0:
            return None
        if not isinstance(path, list) or not path:
            return None
        segments = [_utf8_segment(segment) for segment in path]
        if any(segment is None for segment in segments):
            return None
        members.append(("/".join(segments), length))
    if not members or len({path for path, _size in members}) != len(members):
        return None
    return TorrentMemberTree(identity.info_hash, name, tuple(members), True)


def bittorrent_root_hash(request) -> str:
    """The info-hash a BitTorrent root request is DURABLY bound to, or ``""``:
    the fingerprint recorded at admission AND the same hash derived again from
    the request's own persisted payload -- a magnet's ``btih``, or an uploaded
    torrent's original ``info`` bytes. A recorded fingerprint that its payload
    does not reproduce binds nothing."""
    from transfers.models import BITTORRENT_REQUEST_KINDS, TORRENT_FILE_REQUEST_KINDS

    recorded = str(getattr(request, "fingerprint", "") or "").strip().casefold()
    kind = str(getattr(request, "kind", "") or "").strip().lower()
    if kind not in BITTORRENT_REQUEST_KINDS or not re.fullmatch(r"[0-9a-f]{40}", recorded):
        return ""
    payload = getattr(request, "payload", None)
    if kind in TORRENT_FILE_REQUEST_KINDS:
        derived = extract_hash_from_torrent(payload) if isinstance(payload, (bytes, bytearray)) else ""
    else:
        derived = (extract_hash(payload) or "") if isinstance(payload, str) else ""
    return recorded if derived == recorded else ""



def extract_hash(magnet: str) -> Optional[str]:
    match = re.search(r"xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet, re.I)
    if not match:
        return None
    value = match.group(1)
    if len(value) == 32:
        try:
            value = base64.b32decode(value.upper()).hex()
        except Exception:
            return None
    return value.lower()



# The direct-source transports one link submission may carry. The request kind
# is the URL scheme; provider applicability decides who resolves it. The WebDAV
# aliases are request syntax for HTTP(S) WebDAV semantics, not wire transports.
DIRECT_LINK_SCHEMES = frozenset({"http", "https", "ftp", "sftp", "scp", "ssh", "rsync", "rsync+ssh",
                                 "webdav", "webdavs", "dav", "davs"})


# The authentication target scope of a URL-shaped resource. Scheme knowledge
# belongs here, with the request kinds themselves: the authentication-input
# owner only ever compares opaque scopes. SCP, SSH, SFTP and rsync over SSH
# are one family -- the same server authenticates all four. An rsync daemon is
# its own service with its own accounts. A WebDAV alias names the HTTP(S)
# server it is spoken over, so an answer given for ``davs://host/`` is the
# answer for ``https://host/`` members of the same authority.
_AUTH_SCOPE_FAMILIES = {
    "http": ("http", 80), "https": ("https", 443), "ftp": ("ftp", 21),
    "webdav": ("http", 80), "dav": ("http", 80), "webdavs": ("https", 443), "davs": ("https", 443),
    "sftp": ("ssh", 22), "scp": ("ssh", 22), "ssh": ("ssh", 22), "rsync+ssh": ("ssh", 22),
    "rsync": ("rsync", 873),
}


@dataclass(frozen=True)
class AuthScope:
    """The target an authentication answer is valid for. Never a cache key on
    its own: material is reused only inside one request lineage AND scope."""
    family: str
    host: str
    port: int | None


def auth_scope(address) -> AuthScope | None:
    """The authentication target scope of a URL-shaped address, or ``None``."""
    try:
        parts = urlsplit(str(address or ""))
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.casefold()
    host = str(parts.hostname or "").rstrip(".").casefold()
    if not scheme or not host:
        return None
    family, default = _AUTH_SCOPE_FAMILIES.get(scheme, (scheme, None))
    return AuthScope(family, host, port if port is not None else default)


# The remote-file transports whose resources a provider can address by one
# canonical coordinate within a server scope (``remote_object_coordinate``).
_REMOTE_OBJECT_FAMILIES = frozenset({"ftp", "ssh", "rsync"})


def remote_object_coordinate(address) -> str:
    """The canonical remote coordinate of the file ``address`` reaches on its
    server, or ``""`` when the address does not determine one. An address, not
    immutable identity: the server may replace the contents at one path.

    Server scope is the authentication scope (transport family, host, port, so
    ``scp``/``ssh``/``sftp`` share one), and the object is its absolute path as
    the transports decode it. Anything that would need guessing yields no
    coordinate: a home-relative path (the server resolves it), dot or empty
    segments, an encoded separator, a directory, a query or fragment. A
    provider states this at candidate construction; nothing reconstructs it
    later, and it never names an object across two server scopes."""
    scope = auth_scope(address)
    if scope is None or scope.family not in _REMOTE_OBJECT_FAMILIES or scope.port is None:
        return ""
    parts = urlsplit(str(address))
    raw = parts.path
    if parts.query or parts.fragment or not raw.startswith("/") or raw.endswith("/") or "%2f" in raw.casefold():
        return ""
    segments = unquote(raw).split("/")[1:]
    if not segments or segments[0].startswith("~") or any(item in {"", ".", ".."} for item in segments):
        return ""
    host = f"[{scope.host}]" if ":" in scope.host else scope.host
    return f"{scope.family}://{host}:{scope.port}/" + "/".join(quote(item, safe="") for item in segments)


def direct_link_host(address) -> str:
    """The exact host an operator-submitted direct link names ('' when the
    input is not a direct link) -- the only host a local-network consent for
    that submission can ever cover."""
    if not isinstance(address, str):
        return ""
    try:
        parsed = urlsplit(address.strip())
    except ValueError:
        return ""
    if parsed.scheme.lower() not in DIRECT_LINK_SCHEMES:
        return ""
    return str(parsed.hostname or "").rstrip(".").casefold()


_EVERY_LINK = "Every link must be an absolute HTTP, HTTPS, FTP, SFTP, SCP, SSH or rsync URL, or a WebDAV URL"


def normalize_direct_links(values: List[str]) -> List[str]:
    """Validate and de-duplicate direct-source links without fetching them.

    Credentials a link carries are not refused here: the core admission
    boundary (``TransferEngine.submit``) splits them out as USER_SUPPLIED
    authentication before anything is persisted."""
    normalized: List[str] = []
    seen: Set[str] = set()
    for raw in values or []:
        value = str(raw or "").strip()
        if not value:
            continue
        parsed = urlparse(value)
        try:
            malformed_port = parsed.port == 0  # urlparse raises on a non-numeric or out-of-range port
        except ValueError:
            malformed_port = True
        if malformed_port:
            raise ValueError(_EVERY_LINK)
        if parsed.scheme.lower() not in DIRECT_LINK_SCHEMES or not parsed.hostname:
            raise ValueError(_EVERY_LINK)
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    if not normalized:
        raise ValueError("At least one HTTP, HTTPS, FTP, SFTP, SCP, SSH or rsync link, or WebDAV link, is required")
    if len(normalized) > MAX_DIRECT_LINKS_PER_BATCH:
        raise ValueError(
            f"A maximum of {MAX_DIRECT_LINKS_PER_BATCH} links may be submitted at once"
        )
    return normalized


# The ordinary multi-link text surface's one grouping delimiter: within one
# row, TAB separates alternate sources for ONE item; a new row is another item.
# Spaces are never a delimiter.
ALTERNATIVE_SOURCE_DELIMITER = "\t"


def direct_link_rows(values: List[str]) -> List[tuple[str, ...]]:
    """The rows of one text submission, each the ordered alternate sources the
    operator declared for one item -- left to right, empty cells (trailing or
    repeated TABs) dropped and a link repeated within its row kept once. An
    identical row repeated is one row, exactly as a repeated line is one link.
    A link in two different rows would make it an alternative of two items:
    that is refused, never guessed. Links themselves are validated by
    ``normalize_direct_links``, unchanged."""
    rows: List[tuple[str, ...]] = []
    owner: dict[str, tuple[str, ...]] = {}
    for raw in values or []:
        cells: List[str] = []
        for cell in str(raw or "").split(ALTERNATIVE_SOURCE_DELIMITER):
            cell = cell.strip()
            if cell and cell not in cells:
                cells.append(cell)
        row = tuple(cells)
        if not row or row in rows:
            continue
        if any(owner.get(cell, row) != row for cell in row):
            raise ValueError("A link may be listed in only one row")
        owner.update((cell, row) for cell in row)
        rows.append(row)
    return rows



def direct_link_filename(url: str, fallback_index: int = 1) -> str:
    """Return a safe initial filename for a direct-link transaction."""
    parsed = urlparse(str(url or ""))
    candidate = unquote(PurePosixPath(parsed.path or "").name).strip()
    if not candidate:
        # Query-only hosters such as 1fichier encode the opaque file identity
        # in the leading bare query component, sometimes followed by ordinary
        # parameters (for example: ?<token>&af=...). Retain only that leading
        # opaque component and never expose key=value query parameters.
        raw_query = str(parsed.query or "").strip()
        leading_query_part = raw_query.split("&", 1)[0].strip()
        query_token = unquote(leading_query_part).strip()
        if query_token and "=" not in query_token and "&" not in query_token:
            candidate = f"{parsed.hostname or 'debrid-link'} - {query_token}"
        else:
            candidate = parsed.hostname or f"debrid-link-{fallback_index}"
    candidate = safe_name(candidate)
    return candidate or f"debrid-link-{fallback_index}"



def _direct_link_collection_base(filename: str) -> str:
    """Return a conservative collection stem for known multipart filenames."""
    name = safe_name(str(filename or "").strip())
    patterns = (
        r"(?i)^(?P<base>.+)\.part\d+\.rar$",
        r"(?i)^(?P<base>.+)\.r\d{2,3}$",
        r"(?i)^(?P<base>.+)\.(?:7z|zip|rar)\.\d{3}$",
    )
    for pattern in patterns:
        match = re.match(pattern, name)
        if match:
            base = match.group("base").rstrip(" .-_")
            if base:
                return base
    return name



def direct_link_collection_name(
    resolved_names: List[str], source_urls: List[str]
) -> str:
    """Build a useful parent label without inventing unavailable filenames."""
    urls = list(source_urls or [])
    total = len(urls)
    resolved = [
        safe_name(str(name))
        for name in (resolved_names or [])
        if str(name or "").strip()
    ]

    if total <= 0:
        return "Debrid links"

    if total == 1:
        return (
            resolved[0]
            if resolved
            else direct_link_filename(urls[0], 1)
        )

    if resolved:
        bases = [_direct_link_collection_base(name) for name in resolved]
        first_base = bases[0]
        if all(base.casefold() == first_base.casefold() for base in bases[1:]):
            return safe_name(f"{first_base} ({total} links)")

        return safe_name(f"{resolved[0]} + {total - 1} more")

    fallback = direct_link_filename(urls[0], 1)
    return safe_name(f"{fallback} + {total - 1} more")



# A submitted file no structured upload owner claims is read here as a
# document only: decoded as text, then recognized as one small coherent grammar
# whose structure itself says which values are links. Whether a value is a
# supported link stays with the submission owners -- nothing here knows a
# scheme. A document that would yield links only by guessing -- prose, markup,
# a table or record with two link fields, a row listing several links (an
# equivalence nothing here may assert) -- is refused whole.

# Internal safety bounds, never operator tuning. A link list is small: a
# submission admits at most MAX_DIRECT_LINKS_PER_BATCH links.
MAX_LINK_FILE_BYTES = 1024 * 1024
_MAX_LINK_FILE_VALUE = 64 * 1024
_MAX_XML_DEPTH = 3
# The one field-name vocabulary that marks a record's link.
_LINK_FIELDS = frozenset({"url", "uri", "link"})
# An absolute reference's shape (RFC 3986 scheme, then anything without
# whitespace): grammar only, never support. Two scheme characters at least, so
# a drive letter (C:\...) is not one.
_LINK_SHAPE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]+:\S+")
_NOT_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def link_file_entries(data: bytes) -> tuple[tuple[str, str], ...]:
    """``(location, value)`` for every link one submitted file lists, in order
    and without exact repeats. ``location`` names where the value was found
    ("Line 4", "Entry 2") so a refusal never has to echo the value itself.
    Raises ``ValueError`` when the file is not such a list."""
    if not data:
        raise ValueError("The file is empty")
    if len(data) > MAX_LINK_FILE_BYTES:
        raise ValueError("The file exceeds the 1 MB link file limit")
    text = _link_file_text(bytes(data))
    meaningful = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    if not meaningful:
        raise ValueError("The file contains no links")
    first = meaningful[0]
    if first.startswith(("[", "{")):
        entries = _listed_entries(_json_document(text))
    elif first.startswith("<"):
        entries = _xml_entries(text)
    elif first in {"-", "---"} or first.startswith("- "):
        entries = _listed_entries(_yaml_document(text))
    else:
        entries = _delimited_entries(text, first)
    values: dict[str, str] = {}
    for location, raw in entries:
        value = raw.strip()
        if not value:
            raise ValueError(f"{location} has no link")
        if len(value) > _MAX_LINK_FILE_VALUE:
            raise ValueError(f"{location} is too long")
        values.setdefault(value, location)
    if not values:
        raise ValueError("The file contains no links")
    if len(values) > MAX_DIRECT_LINKS_PER_BATCH:
        raise ValueError(f"A maximum of {MAX_DIRECT_LINKS_PER_BATCH} links may be submitted at once")
    return tuple((location, value) for value, location in values.items())


def _link_file_text(data: bytes) -> str:
    """Unicode text of a UTF-8/16/32 document, or refusal. UTF-16 without a
    byte-order mark only when every code unit is unambiguous (one zero byte,
    one non-zero byte, the same side throughout); nothing else is guessed."""
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = "utf-32"
    elif data.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    elif len(data) % 2 == 0 and all(data[0::2]) and not any(data[1::2]):
        encoding = "utf-16-le"
    elif len(data) % 2 == 0 and all(data[1::2]) and not any(data[0::2]):
        encoding = "utf-16-be"
    else:
        encoding = "utf-8"
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        raise ValueError("The file is not a text file") from None
    if _NOT_TEXT.search(text):
        raise ValueError("The file is not a text file")
    return text


def _link_shaped(value) -> bool:
    return isinstance(value, str) and _LINK_SHAPE.fullmatch(value.strip()) is not None


def _link_field(names) -> int:
    """The position of the one link field among ``names``."""
    found = [index for index, name in enumerate(names) if str(name).strip().casefold() in _LINK_FIELDS]
    if not found:
        raise ValueError("The file has no link field")
    if len(found) > 1:
        raise ValueError("The file has more than one link field")
    return found[0]


def _delimited_entries(text: str, first: str):
    """A line list, or a CSV/TSV table whose header names its one link column.
    Headerless rows carry exactly one link each."""
    for number, line in enumerate(text.splitlines(), 1):
        if len(line) > _MAX_LINK_FILE_VALUE:
            raise ValueError(f"Line {number} is too long")
    delimiter = "\t" if "\t" in first else ","
    header = next(csv.reader([first], delimiter=delimiter))
    if not any(_link_shaped(cell) for cell in header) and (
            len(header) > 1 or header[0].strip().casefold() in _LINK_FIELDS):
        return _table_entries(text, delimiter)
    entries = []
    for number, line in enumerate(text.splitlines(), 1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        cells = value.split() if any(char.isspace() for char in value) else next(csv.reader([value]))
        if sum(_link_shaped(cell) for cell in cells) > 1:
            raise ValueError(f"Line {number} lists more than one link")
        if any(char.isspace() for char in value):
            raise ValueError(f"Line {number} is not a single link")
        entries.append((f"Line {number}", value))
    return entries


def _table_entries(text: str, delimiter: str):
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    rows = []
    try:
        for row in reader:
            if any(cell.strip() for cell in row) and not row[0].lstrip().startswith("#"):
                rows.append((reader.line_num, row))
    except csv.Error:
        raise ValueError(f"Line {reader.line_num} is not a well-formed table row") from None
    if not rows:
        raise ValueError("The file contains no links")
    (_line, header), body = rows[0], rows[1:]
    column = _link_field(header)
    entries = []
    for number, row in body:
        if len(row) != len(header):
            raise ValueError(f"Line {number} does not match the table header")
        if any(_link_shaped(cell) for index, cell in enumerate(row) if index != column):
            raise ValueError(f"Line {number} has more than one link")
        entries.append((f"Line {number}", row[column]))
    if not entries:
        raise ValueError("The file contains no links")
    return entries


def _json_document(text: str):
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        raise ValueError("The file is not valid JSON") from None


def _yaml_document(text: str):
    try:
        return yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError):
        raise ValueError("The file is not valid YAML") from None


def _listed_entries(document):
    """A top-level list of links, or of records of one shape that each name
    one link field. Nothing nested is searched."""
    if not isinstance(document, list) or not document:
        raise ValueError("The file is not a list of links")
    if all(isinstance(item, str) for item in document):
        return [(f"Entry {index}", item) for index, item in enumerate(document, 1)]
    if not all(isinstance(item, dict) for item in document):
        raise ValueError("The file mixes links and records")
    names = list(document[0])
    if any(set(item) != set(names) for item in document):
        raise ValueError("The records in the file do not share one shape")
    key = names[_link_field(names)]
    entries = []
    for index, item in enumerate(document, 1):
        others = [value for name, value in item.items() if name != key]
        if not isinstance(item[key], str) or any(isinstance(value, (list, dict)) for value in others):
            raise ValueError(f"Entry {index} is not a link record")
        if any(_link_shaped(value) for value in others):
            raise ValueError(f"Entry {index} has more than one link")
        entries.append((f"Entry {index}", item[key]))
    return entries


def _xml_entries(text: str):
    """A root of repeated link elements, or of repeated flat records that each
    name one link field. Parsed with no document type, no entity declaration,
    no external entity and no namespace: a document in a declared vocabulary
    (Metalink, XHTML, a feed) belongs to that vocabulary's owner."""
    parser = expat.ParserCreate(encoding="utf-8", namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    def refuse(*_arguments):
        raise ValueError("The file is not a link list")

    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    parser.StartNamespaceDeclHandler = refuse
    root = {"name": "", "children": [], "text": []}
    stack = [root]

    def start(name, _attributes):
        if len(stack) > _MAX_XML_DEPTH:
            refuse()
        element = {"name": name, "children": [], "text": []}
        stack[-1]["children"].append(element)
        stack.append(element)

    def characters(value):
        stack[-1]["text"].append(value)

    parser.StartElementHandler = start
    parser.EndElementHandler = lambda _name: stack.pop()
    parser.CharacterDataHandler = characters
    try:
        parser.Parse(text.encode("utf-8"), True)
    except expat.ExpatError:
        raise ValueError("The file is not well-formed XML") from None
    (document,) = root["children"]
    records = document["children"]
    if not records or "".join(document["text"]).strip() or len({item["name"] for item in records}) != 1:
        refuse()
    if all(not item["children"] for item in records):
        if records[0]["name"].casefold() not in _LINK_FIELDS:
            refuse()
        return [(f"Entry {index}", "".join(item["text"])) for index, item in enumerate(records, 1)]
    names = [field["name"] for field in records[0]["children"]]
    column = _link_field(names)
    entries = []
    for index, item in enumerate(records, 1):
        fields = item["children"]
        if ([field["name"] for field in fields] != names or "".join(item["text"]).strip()
                or any(field["children"] for field in fields)):
            raise ValueError(f"Entry {index} is not a link record")
        values = ["".join(field["text"]) for field in fields]
        if any(_link_shaped(value) for position, value in enumerate(values) if position != column):
            raise ValueError(f"Entry {index} has more than one link")
        entries.append((f"Entry {index}", values[column]))
    return entries
