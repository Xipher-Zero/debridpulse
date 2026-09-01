"""Network destination policy for provider-issued download capabilities."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
from typing import Iterable
from urllib.parse import urlsplit

import aiohttp


class UnsafeDestinationError(ValueError):
    """An address violates public-destination policy."""


class DestinationLookupError(ConnectionError):
    """The destination could not be resolved; no policy authorization was granted."""


def validate_provider_download_url(value: object, *, context: str = "download link") -> str:
    """Validate a provider-issued URL before handing it to the local downloader.

    A provider may broker the remote object, but the returned capability
    URL still crosses a network trust boundary: aria2 will resolve and connect to
    it from the DebridPulse host. Keep that boundary explicit and reject schemes,
    credentials, and literal/local destinations that should never be necessary
    for a public download URL.
    """
    raw = str(value or "").strip()
    if not raw:
        raise UnsafeDestinationError(f"Provider returned an empty {context}")
    try:
        parsed = urlsplit(raw)
        port = parsed.port  # force validation of malformed/out-of-range ports
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
        # inet_aton also recognizes legacy numeric IPv4 spellings such as
        # 2130706433 or 0x7f000001 that strict ipaddress intentionally rejects
        # but some network stacks still resolve as loopback/private addresses.
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
    normalized = {
        str(address or "").strip()
        for address in addresses
        if str(address or "").strip()
    }
    if not normalized:
        raise UnsafeDestinationError(f"Provider download host {host!r} did not resolve to an address")
    blocked = sorted(address for address in normalized if not _public_ip(address))
    if blocked:
        raise UnsafeDestinationError(
            f"Provider download host {host!r} resolved to non-public address(es): "
            + ", ".join(blocked[:4])
        )


async def validate_resolved_public_destination(uri: str) -> str:
    """Validate syntax and current DNS answers before any provider capability use."""
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
        answers = await loop.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise DestinationLookupError(f"Provider download host {host!r} could not be resolved") from exc

    reject_non_public_resolution(
        (entry[4][0] for entry in answers if entry and len(entry) >= 5 and entry[4]),
        host=host,
    )
    return validated


def _content_range_total(value: str) -> int:
    match = re.fullmatch(r"bytes\s+\d+-\d+/(\d+)", str(value or "").strip(), re.I)
    return int(match.group(1)) if match else 0


class PublicDestinationResolver(aiohttp.abc.AbstractResolver):
    """Authorize the exact DNS answers supplied to an HTTP connection."""
    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        answers = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        reject_non_public_resolution((entry[4][0] for entry in answers), host=host)
        return [{"hostname": host, "host": entry[4][0], "port": port,
                 "family": entry[0], "proto": entry[2], "flags": socket.AI_NUMERICHOST}
                for entry in answers]

    async def close(self):
        pass


async def sampled_public_artifact_fingerprint(
    uri: str,
    *,
    sample_bytes: int = 64 * 1024,
    timeout_seconds: float = 20.0,
    headers: dict | None = None,
) -> tuple[int, str] | None:
    """Return a bounded first+last-range fingerprint without following redirects.

    This is used only to strengthen near-size cross-hoster mirror identity. A
    server that redirects, ignores Range for a large object, changes total size
    between samples, or otherwise cannot be sampled safely returns ``None`` and
    the candidates remain independent physical downloads.
    """
    validated = await validate_resolved_public_destination(uri)
    sample_bytes = max(4096, int(sample_bytes))
    timeout = aiohttp.ClientTimeout(total=max(5.0, float(timeout_seconds)))

    connector = aiohttp.TCPConnector(resolver=PublicDestinationResolver(), use_dns_cache=False)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async with session.get(
            validated,
            headers={**(headers or {}), "Range": f"bytes=0-{sample_bytes - 1}", "Accept-Encoding": "identity"},
            allow_redirects=False,
        ) as response:
            if 300 <= response.status < 400:
                return None
            if response.status == 200:
                length = int(response.headers.get("Content-Length") or 0)
                if length <= 0 or length > sample_bytes:
                    return None
                body = await response.read()
                if len(body) != length:
                    return None
                digest = hashlib.sha256()
                digest.update(str(length).encode("ascii"))
                digest.update(b"\0")
                digest.update(body)
                return length, digest.hexdigest()
            if response.status != 206:
                return None
            total = _content_range_total(response.headers.get("Content-Range", ""))
            if total <= 0:
                return None
            first = await response.content.read(sample_bytes + 1)
            if len(first) != min(total, sample_bytes):
                return None
            if str(response.headers.get("Content-Range", "")).strip().lower() != f"bytes 0-{len(first)-1}/{total}":
                return None

        if total <= len(first):
            digest = hashlib.sha256()
            digest.update(str(total).encode("ascii"))
            digest.update(b"\0")
            digest.update(first[:total])
            return total, digest.hexdigest()

        last_start = max(0, total - sample_bytes)
        async with session.get(
            validated,
            headers={**(headers or {}), "Range": f"bytes={last_start}-{total - 1}", "Accept-Encoding": "identity"},
            allow_redirects=False,
        ) as response:
            if response.status != 206:
                return None
            if _content_range_total(response.headers.get("Content-Range", "")) != total:
                return None
            last = await response.content.read(sample_bytes + 1)
            if len(last) != total - last_start:
                return None
            if str(response.headers.get("Content-Range", "")).strip().lower() != f"bytes {last_start}-{total-1}/{total}":
                return None

    digest = hashlib.sha256()
    digest.update(str(total).encode("ascii"))
    digest.update(b"\0")
    digest.update(first)
    digest.update(b"\0")
    digest.update(last)
    return total, digest.hexdigest()
