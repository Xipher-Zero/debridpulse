"""Connection-boundary egress guard for DebridPulse-owned downloads.

Download URLs retain their original hostname all the way through aria2 so HTTPS
SNI and certificate hostname verification remain end-to-end.  Each owned aria2
job is forced through this CONNECT proxy.  The proxy performs the final DNS
resolution itself, rejects the entire answer set if any address is non-global,
and then opens the upstream socket to an approved numeric address.  aria2 never
gets a second opportunity to resolve the provider hostname for the target
connection.

aria2 reaches the guard over loopback; the guard listens on loopback only.  Its
port is controlled by DEBRIDPULSE_EGRESS_GUARD_PORT.

Each job credential is signed for one route scope. ``RouteScope.ENDPOINT`` (the
default) admits exactly the authorized hostname and port. ``RouteScope.SAME_HOST``
admits the same hostname on the authorized port plus server-selected
unprivileged ports, for native transports whose one job opens a second
connection the server chooses (a passive FTP data channel). No scope ever admits
another hostname or skips this guard's own resolution and address policy.
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
from enum import StrEnum
import re
from urllib.parse import urlsplit

from services.network_safety import PUBLIC_DESTINATION_SCHEMES, default_destination_port
from services.network_safety import validate_provider_download_url
from services.network_safety import reject_non_public_resolution

logger = logging.getLogger("debridpulse.downloader_egress_guard")

# aria2's per-protocol proxy preferences, which take precedence over --all-proxy.
_PER_PROTOCOL_PROXY_PREFERENCES = ("http", "https", "ftp")

_PROXY_USER = "debridpulse"
_LOOPBACK = "127.0.0.1"
# Lowest port a same-host scope admits besides the authorized one. Servers
# select passive data ports from their unprivileged range; privileged service
# ports on the same host stay outside the grant.
_SERVER_SELECTED_PORT_FLOOR = 1024


class RouteScope(StrEnum):
    """Which CONNECT authorities one signed job credential admits."""
    ENDPOINT = "endpoint"
    SAME_HOST = "same-host"


_SAME_HOST_USER = re.compile(
    re.escape(f"{_PROXY_USER}.{RouteScope.SAME_HOST.value}.") + r"([0-9]{1,5})"
)

Resolver = Callable[[str, int], Awaitable[list[tuple]]]
PublicCheck = Callable[[str], bool]


def _is_public(address: str) -> bool:
    normalized = str(address or "").split("%", 1)[0].strip()
    try:
        return bool(normalized) and ipaddress.ip_address(normalized).is_global
    except ValueError:
        return False


def _target(uri: str) -> tuple[str, int]:
    validated = validate_provider_download_url(
        uri, context="aria2 download link", schemes=PUBLIC_DESTINATION_SCHEMES)
    parsed = urlsplit(validated)
    host = str(parsed.hostname or "").rstrip(".").casefold()
    if not host:
        raise ValueError("Provider download URL has no hostname")
    # aria2 derives the same well-known port when the URL omits one, so the
    # guard credential is scoped to exactly the authority aria2 will CONNECT to.
    port = int(parsed.port or default_destination_port(parsed.scheme))
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
        bind_port: int | None = None,
    ) -> None:
        self._resolver = resolver
        self._public_check = public_check or _is_public
        self._configured_port = bind_port
        self._secret = secrets.token_bytes(32)
        self._server: asyncio.AbstractServer | None = None
        self._lock = asyncio.Lock()
        self._bound_port = 0

    @property
    def bound_port(self) -> int:
        return int(self._bound_port)

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
            host = _LOOPBACK
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

    def _same_host_token(self, host: str, port: int) -> str:
        # Domain-separated from the endpoint token: a hostname never contains
        # "|", so no endpoint message can equal a same-host message.
        message = f"{RouteScope.SAME_HOST.value}|{str(host).rstrip('.').casefold()}:{int(port)}"
        return hmac.new(self._secret, message.encode("utf-8"), hashlib.sha256).hexdigest()

    def _credential(self, host: str, port: int, scope: RouteScope) -> tuple[str, str]:
        if scope == RouteScope.ENDPOINT:
            return _PROXY_USER, self._token(host, port)
        if scope == RouteScope.SAME_HOST:
            return f"{_PROXY_USER}.{scope.value}.{int(port)}", self._same_host_token(host, port)
        raise ValueError("Unsupported egress route scope")

    def _admits(self, username: str, password: str, host: str, port: int) -> bool:
        """Verify a CONNECT credential against the authority it names."""
        if username == _PROXY_USER:
            return hmac.compare_digest(password, self._token(host, port))
        match = _SAME_HOST_USER.fullmatch(username)
        if match is None:
            return False
        authorized = int(match.group(1))
        if not 0 < authorized <= 65535:
            return False
        if port != authorized and port < _SERVER_SELECTED_PORT_FLOOR:
            return False
        return hmac.compare_digest(password, self._same_host_token(host, authorized))

    def _proxy_url(self) -> str:
        if self._server is None or self._bound_port <= 0:
            raise RuntimeError("DebridPulse egress guard is not running")
        return f"http://{_LOOPBACK}:{self._bound_port}"

    def job_options(self, uri: str, *, scope: RouteScope = RouteScope.ENDPOINT) -> dict[str, str]:
        """Return per-addUri proxy policy that cannot inherit a daemon bypass."""
        host, port = _target(uri)
        proxy = self._proxy_url()
        user, token = self._credential(host, port, RouteScope(scope))
        options = {
            "all-proxy": proxy,
            "all-proxy-user": user,
            "all-proxy-passwd": token,
            # Empty is the explicit per-job override, so no daemon-global
            # no-proxy list can ever bypass the guard for an owned job.
            "no-proxy": "",
            # Force CONNECT for ordinary HTTP too. HTTPS tunnels regardless, but
            # setting this explicitly gives every guarded scheme the same target
            # resolution boundary and keeps TLS end-to-end inside the tunnel.
            "proxy-method": "tunnel",
        }
        # aria2 resolves a per-protocol proxy preference ahead of --all-proxy, so
        # a daemon-global --http-proxy/--https-proxy/--ftp-proxy would route
        # that protocol around the guard.  Every per-protocol preference aria2
        # recognises is therefore pinned to the guard per job.
        # SFTP has no per-protocol preference and resolves through --all-proxy.
        for protocol in _PER_PROTOCOL_PROXY_PREFERENCES:
            options[f"{protocol}-proxy"] = proxy
            options[f"{protocol}-proxy-user"] = user
            options[f"{protocol}-proxy-passwd"] = token
        return options

    async def open_tunnel(
        self, uri: str, *, scope: RouteScope = RouteScope.ENDPOINT, port: int | None = None,
        timeout_seconds: float = 10.0,
    ) -> socket.socket:
        """Open one in-process connection through this guard's own CONNECT boundary.

        The application's bounded evidence readers reach a download origin
        exactly the way an owned aria2 job does: the same signed route-scoped
        credential, the same guard-owned resolution, public-address and
        mixed/private/rebinding checks, and the same admission rule for a
        server-selected port. ``port`` names that second connection (a passive
        FTP data channel) and is only admitted under ``RouteScope.SAME_HOST``.
        Returns a connected non-blocking socket carrying nothing but the
        tunnelled bytes; a refusal raises ``PermissionError``.
        """
        host, authorized = _target(uri)
        await self.ensure_started()
        user, token = self._credential(host, authorized, RouteScope(scope))
        server = self._server
        if server is None or not server.sockets:
            raise RuntimeError("DebridPulse egress guard is not running")
        listener = server.sockets[0].getsockname()
        family = server.sockets[0].family
        address = listener[0]
        authority = f"{host}:{int(port) if port is not None else authorized}"
        credential = base64.b64encode(f"{user}:{token}".encode("utf-8")).decode("ascii")
        loop = asyncio.get_running_loop()
        tunnel = socket.socket(family, socket.SOCK_STREAM)
        tunnel.setblocking(False)
        try:
            async with asyncio.timeout(timeout_seconds):
                await loop.sock_connect(tunnel, (address, listener[1]))
                await loop.sock_sendall(tunnel, (
                    f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
                    f"Proxy-Authorization: Basic {credential}\r\n\r\n"
                ).encode("ascii"))
                # Read the proxy answer byte by byte: the origin may speak first
                # (an FTP greeting, an SSH banner) and none of its bytes may be
                # consumed here.
                answer = b""
                while not answer.endswith(b"\r\n\r\n"):
                    chunk = await loop.sock_recv(tunnel, 1)
                    if not chunk or len(answer) >= 1024:
                        raise PermissionError("DebridPulse egress guard refused the connection")
                    answer += chunk
        except BaseException:
            tunnel.close()
            raise
        if not answer.startswith(b"HTTP/1.1 200 "):
            tunnel.close()
            raise PermissionError("DebridPulse egress guard refused the connection")
        return tunnel

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
            if not self._admits(username, password, host, port):
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
