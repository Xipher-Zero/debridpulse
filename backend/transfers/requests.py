"""Provider-neutral request identity parsing."""
import hashlib
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

