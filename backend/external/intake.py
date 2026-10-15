"""What an external client may submit, normalized by the existing intake
owners before anything is reserved or admitted.

Every kind maps onto an existing submission seam (``ApplicationService``):
the same validation, network-safety and routing as an interactive submission.
An external submission never answers an operator-only question (private-LAN
consent, acquisition scope, interactive file selection): one that needs such
an answer is refused, typed, with nothing admitted.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from transfers.requests import normalize_direct_links, torrent_identity

MAX_LINKS = 100
MAX_LINK_LENGTH = 8192
# Decoded bytes of an inline .torrent: well inside the request body ceiling
# once base64-encoded.
MAX_TORRENT_BYTES = 8 * 1024 * 1024
# Decoded bytes of an uploaded NZB: the same native upload ceiling. The staged
# NZB owner's own ceiling is larger and still applies to what it writes.
MAX_NZB_BYTES = 8 * 1024 * 1024
MAX_FILENAME = 255


class IntakeRefused(Exception):
    """A submission refused before anything was reserved or admitted."""

    def __init__(self, code: str, message: str, *, status: int = 422, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.status, self.retryable = code, message, status, retryable


@dataclass(frozen=True)
class Intake:
    kind: str
    # The normalized source itself (never persisted by this module).
    links: tuple[str, ...] = ()
    magnet: str = ""
    url: str = ""
    torrent: bytes = b""
    nzb: bytes = b""
    filename: str = ""
    # What may be shown and persisted: no query, path, credential or payload.
    summary: dict | None = None
    # The canonical identity of the normalized source, for the intent digest.
    identity: str = ""


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def normalize(source: object) -> Intake:
    if not isinstance(source, dict):
        raise IntakeRefused("invalid_source", "source must be an object")
    kind = source.get("kind")
    if kind == "links":
        values = source.get("links")
        if not isinstance(values, list) or not values or len(values) > MAX_LINKS:
            raise IntakeRefused("invalid_source", f"links must be a list of 1-{MAX_LINKS} links")
        if any(not isinstance(item, str) or not item.strip() or len(item) > MAX_LINK_LENGTH for item in values):
            raise IntakeRefused("invalid_source", "every link must be a non-empty string")
        if any("\t" in item for item in values):
            raise IntakeRefused("unsupported_source", "alternate-source rows are not part of the external protocol")
        try:
            links = tuple(normalize_direct_links([item.strip() for item in values]))
        except ValueError as exc:
            raise IntakeRefused("invalid_source", str(exc)) from None
        if not links:
            raise IntakeRefused("invalid_source", "no usable link")
        hosts = sorted({_host(link) for link in links} - {""})
        return Intake("links", links=links, identity=json.dumps(list(links)),
                      summary={"kind": "links", "count": len(links), "hosts": hosts[:20]})
    if kind == "magnet":
        magnet = source.get("magnet")
        if not isinstance(magnet, str) or not magnet.strip() or len(magnet) > MAX_LINK_LENGTH:
            raise IntakeRefused("invalid_source", "magnet is required")
        from application.service import ApplicationService

        try:
            request = ApplicationService.magnet_request(magnet.strip())
        except ValueError as exc:
            raise IntakeRefused("invalid_source", str(exc)) from None
        return Intake("magnet", magnet=magnet.strip(), identity=f"btih:{request.fingerprint}",
                      summary={"kind": "magnet", "info_hash": request.fingerprint, "name": str(request.name)[:256]})
    if kind == "nzb_link":
        url = source.get("url")
        if not isinstance(url, str) or not url.strip() or len(url) > MAX_LINK_LENGTH:
            raise IntakeRefused("invalid_source", "url is required")
        try:
            address = normalize_direct_links([url.strip()])[0]
        except (ValueError, IndexError) as exc:
            raise IntakeRefused("invalid_source", str(exc) or "invalid link") from None
        if urlsplit(address).scheme.lower() not in {"http", "https"}:
            raise IntakeRefused("invalid_source", "an NZB link is an http(s) link")
        return Intake("nzb_link", url=address, identity=address,
                      summary={"kind": "nzb_link", "host": _host(address)})
    if kind == "nzb":
        filename = source.get("filename") or "upload.nzb"
        data = _uploaded(source.get("nzb_base64"), filename, field="nzb_base64", suffix=".nzb",
                         limit=MAX_NZB_BYTES, what="NZB")
        # The bytes are interpreted by the existing NZB reader at admission,
        # never here; the identity is the exact bytes.
        digest = hashlib.sha256(data).hexdigest()
        return Intake("nzb", nzb=data, filename=filename, identity=f"nzb:{digest}",
                      summary={"kind": "nzb", "name": filename[:256]})
    if kind == "torrent":
        filename = source.get("filename") or "upload.torrent"
        data = _uploaded(source.get("torrent_base64"), filename, field="torrent_base64", suffix=".torrent",
                         limit=MAX_TORRENT_BYTES, what="torrent")
        try:
            identity = torrent_identity(data)
        except ValueError as exc:
            raise IntakeRefused("invalid_source", str(exc)) from None
        return Intake("torrent", torrent=data, filename=filename,
                      identity=f"torrent:{hashlib.sha256(data).hexdigest()}",
                      summary={"kind": "torrent", "info_hash": identity.info_hash,
                               "name": str(identity.name or filename)[:256]})
    raise IntakeRefused("unsupported_source", "unsupported source kind")


def _uploaded(encoded, filename, *, field: str, suffix: str, limit: int, what: str) -> bytes:
    """One inline uploaded file: a plain ``*<suffix>`` name and non-empty
    base64 bytes within ``limit`` once decoded."""
    if not isinstance(encoded, str) or not encoded:
        raise IntakeRefused("invalid_source", f"{field} is required")
    if not isinstance(filename, str) or len(filename) > MAX_FILENAME or "/" in filename or "\\" in filename:
        raise IntakeRefused("invalid_source", "filename must be a plain file name")
    if not filename.lower().endswith(suffix):
        raise IntakeRefused("invalid_source", f"filename must end in {suffix}")
    if len(encoded) > (limit * 4) // 3 + 4:
        raise IntakeRefused("source_too_large", f"the {what} exceeds the {limit // (1024 * 1024)} MB external limit",
                            status=413)
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise IntakeRefused("invalid_source", f"{field} is not base64") from None
    if not data or len(data) > limit:
        raise IntakeRefused("invalid_source", f"the {what} is empty or too large")
    return data


def intent_digest(intake: Intake, *, profile_id: str, category_key: str, display_name: str,
                  client_reference: str | None) -> str:
    """The canonical digest of one submission's normalized intent: the same
    retry digests the same; anything that would change what is admitted, or
    how it is delivered, digests differently."""
    document = {"v": 1, "kind": intake.kind, "source": intake.identity, "profile": profile_id,
                "category": category_key, "display_name": display_name, "client_reference": client_reference}
    return "sha256:" + hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
