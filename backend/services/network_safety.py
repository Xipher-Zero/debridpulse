"""Network destination policy for provider-issued download capabilities."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
from typing import Iterable
from urllib.parse import urljoin, urlsplit

import aiohttp

from transfers.models import FingerprintKind


class UnsafeDestinationError(ValueError):
    """An address violates public-destination policy."""


class DestinationLookupError(ConnectionError):
    """The destination could not be resolved; no policy authorization was granted."""


def validate_provider_download_url(value: object, *, context: str = "download link") -> str:
    raw = str(value or "").strip()
    if not raw:
        raise UnsafeDestinationError(f"Provider returned an empty {context}")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeDestinationError(f"Provider returned an invalid {context}") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise UnsafeDestinationError(f"Provider returned a non-HTTP(S) {context}")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeDestinationError(f"Provider returned a credential-bearing {context}")
    if port is not None and not (1 <= port <= 65535):
        raise UnsafeDestinationError(f"Provider returned an invalid {context}")
    host = parsed.hostname.rstrip(".").casefold()
    if not host or "%" in host:
        raise UnsafeDestinationError(f"Provider returned an invalid {context} host")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise UnsafeDestinationError(f"Provider returned a local {context} host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            address = None
    if address is not None and not address.is_global:
        raise UnsafeDestinationError(f"Provider returned a non-public {context} address")
    return raw


def _public_ip(address: str) -> bool:
    normalized = str(address or "").split("%", 1)[0].strip()
    try:
        return bool(normalized) and ipaddress.ip_address(normalized).is_global
    except ValueError:
        return False


def reject_non_public_resolution(addresses: Iterable[str], *, host: str) -> None:
    normalized = {str(address or "").strip() for address in addresses if str(address or "").strip()}
    if not normalized:
        raise UnsafeDestinationError(f"Provider download host {host!r} did not resolve to an address")
    blocked = sorted(address for address in normalized if not _public_ip(address))
    if blocked:
        raise UnsafeDestinationError(
            f"Provider download host {host!r} resolved to non-public address(es): " + ", ".join(blocked[:4])
        )


async def validate_resolved_public_destination(uri: str) -> str:
    validated = validate_provider_download_url(uri, context="aria2 download link")
    parsed = urlsplit(validated)
    host = str(parsed.hostname or "").rstrip(".").casefold()
    if not host:
        raise UnsafeDestinationError("Provider download URL has no hostname")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise UnsafeDestinationError(f"Provider download host {host!r} is not public")
        return validated
    port = int(parsed.port or (443 if parsed.scheme.casefold() == "https" else 80))
    loop = asyncio.get_running_loop()
    try:
        answers = await loop.getaddrinfo(host, port, family=socket.AF_UNSPEC,
                                         type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise DestinationLookupError(f"Provider download host {host!r} could not be resolved") from exc
    reject_non_public_resolution((entry[4][0] for entry in answers if entry and len(entry) >= 5 and entry[4]), host=host)
    return validated


def _content_range(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"\s*bytes\s+(\d+)\s*-\s*(\d+)\s*/\s*(\d+)\s*", str(value or ""), re.I)
    if not match:
        return None
    start, end, total = (int(match.group(index)) for index in (1, 2, 3))
    if total <= 0 or start < 0 or end < start or end >= total:
        return None
    return start, end, total


def _digest_prefix(total: int, body: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(str(total).encode("ascii"))
    digest.update(b"\0prefix\0")
    digest.update(body)
    return digest.hexdigest()


def _digest_full(total: int, first: bytes, last: bytes | None = None) -> str:
    digest = hashlib.sha256()
    digest.update(str(total).encode("ascii"))
    digest.update(b"\0")
    digest.update(first)
    if last is not None:
        digest.update(b"\0")
        digest.update(last)
    return digest.hexdigest()


def _sample(total: int, signature: str, kind: FingerprintKind, reason: str = "",
            prefix_signature: str = "") -> tuple[int, str, FingerprintKind, str, str]:
    return total, signature, kind, reason, prefix_signature


def _unavailable(reason: str) -> tuple[int, str, FingerprintKind, str, str]:
    return _sample(0, "", FingerprintKind.UNAVAILABLE, reason)


async def _read_exactly(response, count: int) -> bytes | None:
    """Read exactly one bounded sample; never consume past its declared region."""
    try:
        return await response.content.readexactly(count)
    except asyncio.IncompleteReadError:
        return None


class PublicDestinationResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        answers = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        reject_non_public_resolution((entry[4][0] for entry in answers), host=host)
        return [{"hostname": host, "host": entry[4][0], "port": port,
                 "family": entry[0], "proto": entry[2], "flags": socket.AI_NUMERICHOST}
                for entry in answers]

    async def close(self):
        pass


def _origin(uri: str) -> tuple[str, str, int]:
    parsed = urlsplit(uri)
    return parsed.scheme.casefold(), str(parsed.hostname or "").casefold(), int(
        parsed.port or (443 if parsed.scheme.casefold() == "https" else 80))


async def _range_request(session, uri: str, headers: dict, *, max_redirects: int = 3):
    current = uri
    current_headers = dict(headers)
    prior_origin = _origin(uri)
    redirected = False
    for hop in range(max_redirects + 1):
        try:
            validated = await validate_resolved_public_destination(current)
        except DestinationLookupError:
            return None, "dns_failure"
        except UnsafeDestinationError:
            return None, "destination_rejected"
        response = await session.get(validated, headers=current_headers, allow_redirects=False)
        if not (300 <= response.status < 400):
            return response, "redirect" if redirected else ""
        location = str(response.headers.get("Location") or "").strip()
        response.release()
        if not location or hop >= max_redirects:
            return None, "redirect"
        next_uri = urljoin(validated, location)
        try:
            validate_provider_download_url(next_uri, context="redirect target")
        except UnsafeDestinationError:
            return None, "destination_rejected"
        next_origin = _origin(next_uri)
        if next_origin != prior_origin:
            current_headers = {key: value for key, value in current_headers.items()
                               if key.casefold() in {"range", "accept-encoding"}}
        prior_origin = next_origin
        current = next_uri
        redirected = True
    return None, "redirect"


async def sampled_public_artifact_fingerprint(
    uri: str,
    *,
    sample_bytes: int = 64 * 1024,
    timeout_seconds: float = 20.0,
    headers: dict | None = None,
    expected_bytes: int = 0,
) -> tuple[int, str, FingerprintKind, str, str]:
    """Return bounded structured content evidence for a public HTTP(S) capability.

    ``expected_bytes`` is retained for caller compatibility but is deliberately
    not an identity gate. The sampler reports the payload size it discovers;
    the Universal Core owns reported-size plausibility policy.
    """
    try:
        validated = await validate_resolved_public_destination(uri)
    except DestinationLookupError:
        return _unavailable("dns_failure")
    except UnsafeDestinationError:
        return _unavailable("destination_rejected")

    sample_bytes = max(4096, int(sample_bytes))
    timeout = aiohttp.ClientTimeout(total=max(5.0, float(timeout_seconds)))
    base_headers = {**(headers or {}), "Accept-Encoding": "identity"}
    connector = aiohttp.TCPConnector(resolver=PublicDestinationResolver(), use_dns_cache=False)
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            first_headers = {**base_headers, "Range": f"bytes=0-{sample_bytes - 1}"}
            response, redirect_reason = await _range_request(session, validated, first_headers)
            if response is None:
                return _unavailable(redirect_reason or "sampler_unavailable")
            try:
                if response.status == 200:
                    try:
                        length = int(response.headers.get("Content-Length") or 0)
                    except (TypeError, ValueError):
                        length = 0
                    if length <= 0:
                        return _unavailable("range_ignored")
                    count = min(sample_bytes, length)
                    first = await _read_exactly(response, count)
                    if first is None:
                        return _unavailable("sampler_unavailable")
                    prefix = _digest_prefix(length, first)
                    if length <= sample_bytes:
                        return _sample(length, _digest_full(length, first), FingerprintKind.FULL_CONTENT_SAMPLE,
                                       redirect_reason, prefix)
                    return _sample(length, prefix, FingerprintKind.PREFIX_CONTENT_SAMPLE,
                                   "range_ignored", prefix)
                if response.status != 206:
                    return _unavailable("range_unsupported")
                parsed = _content_range(response.headers.get("Content-Range", ""))
                if parsed is None:
                    return _unavailable("invalid_content_range")
                start, end, total = parsed
                expected_end = min(total - 1, sample_bytes - 1)
                if start != 0 or end != expected_end:
                    return _unavailable("invalid_content_range")
                first = await _read_exactly(response, end - start + 1)
                if first is None:
                    return _unavailable("sampler_unavailable")
                prefix = _digest_prefix(total, first)
            finally:
                response.release()

            if total <= len(first):
                return _sample(total, _digest_full(total, first[:total]), FingerprintKind.FULL_CONTENT_SAMPLE,
                               redirect_reason, prefix)

            last_start = max(0, total - sample_bytes)
            last_headers = {**base_headers, "Range": f"bytes={last_start}-{total - 1}"}
            response, last_redirect_reason = await _range_request(session, validated, last_headers)
            if response is None:
                return _sample(total, prefix, FingerprintKind.PREFIX_CONTENT_SAMPLE,
                               last_redirect_reason or "sampler_unavailable", prefix)
            try:
                if response.status != 206:
                    reason = "range_ignored" if response.status == 200 else "range_unsupported"
                    return _sample(total, prefix, FingerprintKind.PREFIX_CONTENT_SAMPLE, reason, prefix)
                parsed = _content_range(response.headers.get("Content-Range", ""))
                if parsed is None:
                    return _sample(total, prefix, FingerprintKind.PREFIX_CONTENT_SAMPLE,
                                   "invalid_content_range", prefix)
                start, end, repeated_total = parsed
                if start != last_start or end != total - 1 or repeated_total != total:
                    return _sample(total, prefix, FingerprintKind.PREFIX_CONTENT_SAMPLE,
                                   "size_disagreement" if repeated_total != total else "invalid_content_range", prefix)
                last = await _read_exactly(response, end - start + 1)
                if last is None:
                    return _sample(total, prefix, FingerprintKind.PREFIX_CONTENT_SAMPLE,
                                   "sampler_unavailable", prefix)
            finally:
                response.release()

        return _sample(total, _digest_full(total, first, last), FingerprintKind.FULL_CONTENT_SAMPLE,
                       redirect_reason or last_redirect_reason, prefix)
    except asyncio.TimeoutError:
        return _unavailable("timeout")
    except DestinationLookupError:
        return _unavailable("dns_failure")
    except UnsafeDestinationError:
        return _unavailable("destination_rejected")
    except (aiohttp.ClientError, OSError, ValueError):
        return _unavailable("sampler_unavailable")
