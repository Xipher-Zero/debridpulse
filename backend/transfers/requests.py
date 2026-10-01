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


def extract_hash_from_torrent(data: bytes) -> str:
    """
    Return the BitTorrent v1 info-hash from a validated metainfo payload.

    BitTorrent defines the v1 info-hash as SHA-1 over the bencoded ``info``
    dictionary. ``bencode2`` preserves byte strings and validates the complete
    metainfo structure before the dictionary is encoded for hashing. Invalid or
    incomplete payloads return an empty string and are never approximated with
    a byte-slicing fallback.
    """
    try:
        metainfo = bencode2.bdecode(data)
        if not isinstance(metainfo, dict):
            return ""
        info = metainfo.get(b"info")
        if not isinstance(info, dict):
            return ""
        info_bytes = bencode2.bencode(info)
        # SHA-1 is mandated by the BitTorrent v1 info-hash protocol and is not
        # used here for a security decision.
        return hashlib.sha1(info_bytes, usedforsecurity=False).hexdigest()
    except Exception:
        return ""



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
