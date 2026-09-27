"""Provider-neutral request identity parsing."""
import hashlib
import base64
from dataclasses import dataclass
import re
from pathlib import PurePosixPath
from urllib.parse import urlparse, urlsplit, unquote
from typing import Optional, List, Set
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
# is the URL scheme; provider applicability decides who resolves it.
DIRECT_LINK_SCHEMES = frozenset({"http", "https", "ftp", "sftp", "scp", "ssh"})


# The authentication target scope of a URL-shaped resource. Scheme knowledge
# belongs here, with the request kinds themselves: the authentication-input
# owner only ever compares opaque scopes. SCP, SSH and SFTP are one family --
# the same server authenticates all three.
_AUTH_SCOPE_FAMILIES = {
    "http": ("http", 80), "https": ("https", 443), "ftp": ("ftp", 21),
    "sftp": ("ssh", 22), "scp": ("ssh", 22), "ssh": ("ssh", 22),
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
            raise ValueError("Every link must be an absolute HTTP, HTTPS, FTP, SFTP, SCP or SSH URL")
        if parsed.scheme.lower() not in DIRECT_LINK_SCHEMES or not parsed.hostname:
            raise ValueError("Every link must be an absolute HTTP, HTTPS, FTP, SFTP, SCP or SSH URL")
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    if not normalized:
        raise ValueError("At least one HTTP, HTTPS, FTP, SFTP, SCP or SSH link is required")
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
