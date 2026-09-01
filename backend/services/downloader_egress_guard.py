"""Connection-boundary egress guard for DebridPulse-owned HTTP(S) downloads.

Provider download URLs retain their original hostname all the way through aria2 so
HTTPS SNI and certificate hostname verification remain end-to-end.  Each owned
aria2 job is forced through this CONNECT proxy.  The proxy performs the final DNS
resolution itself, rejects the entire answer set if any address is non-global,
and then opens the upstream socket to an approved numeric address.  aria2 never
gets a second opportunity to resolve the provider hostname for the target
connection.

Built-in aria2 reaches the guard over loopback.  A shared/external daemon cannot
safely be assumed to reach the application's loopback namespace, so external mode
fails closed unless the operator explicitly advertises a route to this guard with
DEBRIDPULSE_EXTERNAL_ARIA2_EGRESS_PROXY.  The guard bind address/port are
controlled by DEBRIDPULSE_EGRESS_GUARD_BIND and DEBRIDPULSE_EGRESS_GUARD_PORT.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from services.network_safety import validate_provider_download_url
from services.network_safety import reject_non_public_resolution

logger = logging.getLogger("debridpulse.downloader_egress_guard")

Resolver = Callable[[str, int], Awaitable[list[tuple]]]
PublicCheck = Callable[[str], bool]


def _is_public(address: str) -> bool:
    normalized = str(address or "").split("%", 1)[0].strip()
    try:
        return bool(normalized) and ipaddress.ip_address(normalized).is_global
    except ValueError:
        return False


def _target(uri: str) -> tuple[str, int]:
    validated = validate_provider_download_url(uri, context="aria2 download link")
    parsed = urlsplit(validated)
    host = str(parsed.hostname or "").rstrip(".").casefold()
    if not host:
        raise ValueError("Provider download URL has no hostname")
    port = int(parsed.port or (443 if parsed.scheme.casefold() == "https" else 80))
    return host, port


def _authority_target(authority: str) -> tuple[str, int]:
    parsed = urlsplit("//" + str(authority or "").strip())
    host = str(parsed.hostname or "").rstrip(".").casefold()
    if not host or parsed.port is None:
        raise ValueError("CONNECT target must include host and port")
    return host, int(parsed.port)


class DownloaderEgressGuard:
    """Authenticated target-scoped CONNECT proxy with connection-time DNS policy."""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        public_check: PublicCheck | None = None,
        bind_host: str | None = None,
        bind_port: int | None = None,
    ) -> None:
        self._resolver = resolver
        self._public_check = public_check or _is_public
        self._configured_host = bind_host
        self._configured_port = bind_port
        self._secret = secrets.token_bytes(32)
        self._server: asyncio.AbstractServer | None = None
        self._lock = asyncio.Lock()
        self._bound_port = 0

    @property
    def bound_port(self) -> int:
        return int(self._bound_port)

    def _bind_host(self) -> str:
        return str(
            self._configured_host
            if self._configured_host is not None
            else os.getenv("DEBRIDPULSE_EGRESS_GUARD_BIND", "127.0.0.1")
        ).strip() or "127.0.0.1"

    def _bind_port(self) -> int:
        raw = (
            self._configured_port
            if self._configured_port is not None
            else os.getenv("DEBRIDPULSE_EGRESS_GUARD_PORT", "6811")
        )
        try:
            port = int(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Invalid DebridPulse egress guard port") from exc
        if port < 0 or port > 65535:
            raise RuntimeError("Invalid DebridPulse egress guard port")
        return port

    async def ensure_started(self) -> None:
        if self._server is not None:
            return
        async with self._lock:
            if self._server is not None:
                return
            host = self._bind_host()
            port = self._bind_port()
            self._server = await asyncio.start_server(
                self._handle_client,
                host=host,
                port=port,
                limit=16 * 1024,
            )
            sockets = list(self._server.sockets or ())
            if not sockets:
                self._server.close()
                await self._server.wait_closed()
                self._server = None
                raise RuntimeError("DebridPulse egress guard failed to bind")
            self._bound_port = int(sockets[0].getsockname()[1])
            logger.info("Downloader egress guard listening on %s:%s", host, self._bound_port)

    async def stop(self) -> None:
        async with self._lock:
            server = self._server
            self._server = None
            self._bound_port = 0
            if server is not None:
                server.close()
                await server.wait_closed()

    def _token(self, host: str, port: int) -> str:
        authority = f"{str(host).rstrip('.').casefold()}:{int(port)}".encode("utf-8")
        return hmac.new(self._secret, authority, hashlib.sha256).hexdigest()

    def _proxy_url(self, *, external: bool) -> str:
        if not external:
            if self._server is None or self._bound_port <= 0:
                raise RuntimeError("DebridPulse egress guard is not running")
            return f"http://127.0.0.1:{self._bound_port}"

        raw = os.getenv("DEBRIDPULSE_EXTERNAL_ARIA2_EGRESS_PROXY", "").strip()
        if not raw:
            raise RuntimeError(
                "External aria2 is fail-closed until "
                "DEBRIDPULSE_EXTERNAL_ARIA2_EGRESS_PROXY advertises a route "
                "from the external daemon to the DebridPulse egress guard"
            )
        parsed = urlsplit(raw)
        if (
            parsed.scheme.casefold() != "http"
            or not parsed.hostname
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError(
                "DEBRIDPULSE_EXTERNAL_ARIA2_EGRESS_PROXY must be an http://host:port URL"
            )
        return f"http://{parsed.hostname}:{parsed.port}"

    def job_options(self, uri: str, *, external: bool) -> dict[str, str]:
        """Return per-addUri proxy policy that cannot inherit a daemon bypass."""
        host, port = _target(uri)
        proxy = self._proxy_url(external=external)
        token = self._token(host, port)
        options = {
            "all-proxy": proxy,
            "all-proxy-user": "debridpulse",
            "all-proxy-passwd": token,
            "http-proxy": proxy,
            "http-proxy-user": "debridpulse",
            "http-proxy-passwd": token,
            "https-proxy": proxy,
            "https-proxy-user": "debridpulse",
            "https-proxy-passwd": token,
            # A shared daemon may have a global no-proxy list.  Empty is the
            # explicit per-job override, preventing that list from bypassing the
            # guard for DebridPulse-owned jobs.
            "no-proxy": "",
            # Force CONNECT for ordinary HTTP too. HTTPS tunnels regardless, but
            # setting this explicitly gives both schemes the same guarded target
            # resolution boundary and keeps TLS end-to-end inside the tunnel.
            "proxy-method": "tunnel",
        }
        return options

    async def _resolve(self, host: str, port: int) -> list[tuple]:
        if self._resolver is not None:
            answers = await self._resolver(host, port)
        else:
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
                raise ValueError(f"Provider download host {host!r} could not be resolved") from exc
        return list(answers or ())

    async def _approved_endpoints(self, host: str, port: int) -> list[tuple[int, str, int]]:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            addresses = [str(literal)]
            if not self._public_check(str(literal)):
                raise ValueError(f"Provider download host {host!r} is not public")
            family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
            return [(family, str(literal), port)]

        answers = await self._resolve(host, port)
        endpoints: list[tuple[int, str, int]] = []
        addresses: list[str] = []
        for entry in answers:
            if not entry or len(entry) < 5 or not entry[4]:
                continue
            address = str(entry[4][0]).split("%", 1)[0]
            addresses.append(address)
            endpoints.append((int(entry[0]), address, port))

        # Preserve the shared validator's all-answers rule in production while
        # allowing a deterministic injected classifier for tunnel-path tests.
        if self._public_check is _is_public:
            reject_non_public_resolution(addresses, host=host)
        else:
            if not addresses:
                raise ValueError(f"Provider download host {host!r} did not resolve to an address")
            blocked = [address for address in addresses if not self._public_check(address)]
            if blocked:
                raise ValueError(
                    f"Provider download host {host!r} resolved to non-public address(es): "
                    + ", ".join(sorted(blocked)[:4])
                )
        return endpoints

    @staticmethod
    def _proxy_credentials(headers: list[str]) -> tuple[str, str]:
        value = ""
        for line in headers:
            if ":" not in line:
                continue
            key, candidate = line.split(":", 1)
            if key.strip().casefold() == "proxy-authorization":
                value = candidate.strip()
                break
        if not value.lower().startswith("basic "):
            return "", ""
        try:
            decoded = base64.b64decode(value.split(None, 1)[1], validate=True).decode("utf-8")
        except Exception:
            return "", ""
        if ":" not in decoded:
            return "", ""
        return tuple(decoded.split(":", 1))  # type: ignore[return-value]

    async def _connect_upstream(self, endpoints: list[tuple[int, str, int]]):
        last_error: Exception | None = None
        for family, address, port in endpoints:
            try:
                return await asyncio.open_connection(
                    address,
                    port,
                    family=family,
                    flags=socket.AI_NUMERICHOST,
                )
            except OSError as exc:
                last_error = exc
        raise OSError("No approved provider address accepted the connection") from last_error

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                return
            if len(raw) > 16 * 1024:
                return
            lines = raw.decode("iso-8859-1", errors="replace").split("\r\n")
            request = lines[0].split()
            if len(request) != 3 or request[0].upper() != "CONNECT":
                writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            host, port = _authority_target(request[1])
            username, password = self._proxy_credentials(lines[1:])
            expected = self._token(host, port)
            if username != "debridpulse" or not hmac.compare_digest(password, expected):
                writer.write(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                    b"Proxy-Authenticate: Basic realm=\"DebridPulse\"\r\n"
                    b"Connection: close\r\n\r\n"
                )
                await writer.drain()
                return

            try:
                endpoints = await self._approved_endpoints(host, port)
                upstream_reader, upstream_writer = await self._connect_upstream(endpoints)
            except (ValueError, OSError):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return

            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()

            async def relay(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
                while True:
                    chunk = await source.read(64 * 1024)
                    if not chunk:
                        return
                    destination.write(chunk)
                    await destination.drain()

            tasks = {
                asyncio.create_task(relay(reader, upstream_writer)),
                asyncio.create_task(relay(upstream_reader, writer)),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        except Exception as exc:
            logger.debug("Downloader egress guard connection closed: %s", exc)
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                try:
                    await upstream_writer.wait_closed()
                except Exception:
                    pass
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


downloader_egress_guard = DownloaderEgressGuard()
