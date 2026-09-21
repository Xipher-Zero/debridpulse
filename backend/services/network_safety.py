"""Network destination policy for provider-issued download capabilities.

One validator owns URL shape and public-destination policy for every transport
this application touches. Callers select which transports they accept by
passing a scheme set; the invariants (hostname required, malformed ports
rejected, embedded credentials rejected, private/local/link-local/non-global
destinations blocked, whole-DNS-answer rejection at connection time) are the
same for all of them.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Iterable
from urllib.parse import urlsplit

import aiohttp



class UnsafeDestinationError(ValueError):
    """An address violates public-destination policy."""


class DestinationLookupError(ConnectionError):
    """The destination could not be resolved; no policy authorization was granted."""


# The one canonical well-known-port table for every destination this
# application validates, resolves, or guards. Callers never carry their own
# per-scheme port arithmetic.
DEFAULT_DESTINATION_PORTS: dict[str, int] = {
    "http": 80,
    "https": 443,
    "ftp": 21,
    "sftp": 22,
}

# Transports a *provider-issued link* may name. Providers hand back web
# capabilities; widening this would widen every provider's link contract, which
# is a separate decision from what the downloader is able to execute.
PROVIDER_LINK_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Transports the downloader may be pointed at. This is the guarded-transport
# set: every scheme here is carried through the same hardened validation,
# connection-time resolution, and egress guard. It is kept equal to the aria2
# executor's positive claim by regression, not by importing across the layer.
PUBLIC_DESTINATION_SCHEMES: frozenset[str] = frozenset(DEFAULT_DESTINATION_PORTS)


def default_destination_port(scheme: str) -> int:
    """Return the well-known port for a guarded transport, or 0 when unknown."""
    return DEFAULT_DESTINATION_PORTS.get(str(scheme or "").casefold(), 0)


def validate_provider_download_url(
    value: object,
    *,
    context: str = "download link",
    schemes: frozenset[str] = PROVIDER_LINK_SCHEMES,
) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise UnsafeDestinationError(f"Provider returned an empty {context}")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeDestinationError(f"Provider returned an invalid {context}") from exc
    # The "non-HTTP(S)" wording is a load-bearing diagnostic marker consumed by
    # provider error translation. Keep it verbatim when changing this message.
    if parsed.scheme.casefold() not in schemes or not parsed.hostname:
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


async def validate_resolved_public_destination(
    uri: str, *, schemes: frozenset[str] = PUBLIC_DESTINATION_SCHEMES
) -> str:
    validated = validate_provider_download_url(uri, context="aria2 download link", schemes=schemes)
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
    port = int(parsed.port or default_destination_port(parsed.scheme))
    loop = asyncio.get_running_loop()
    try:
        answers = await loop.getaddrinfo(host, port, family=socket.AF_UNSPEC,
                                         type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise DestinationLookupError(f"Provider download host {host!r} could not be resolved") from exc
    reject_non_public_resolution((entry[4][0] for entry in answers if entry and len(entry) >= 5 and entry[4]), host=host)
    return validated


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
