"""Provider-neutral request identity parsing."""
import hashlib
import base64
import re
from pathlib import PurePosixPath
from urllib.parse import urlparse, unquote
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



def normalize_direct_links(values: List[str]) -> List[str]:
    """Validate and de-duplicate ordinary HTTP(S) links without fetching them."""
    normalized: List[str] = []
    seen: Set[str] = set()
    for raw in values or []:
        value = str(raw or "").strip()
        if not value:
            continue
        parsed = urlparse(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Every link must be an absolute HTTP or HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Credentials embedded in URLs are not supported")
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    if not normalized:
        raise ValueError("At least one HTTP or HTTPS link is required")
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
