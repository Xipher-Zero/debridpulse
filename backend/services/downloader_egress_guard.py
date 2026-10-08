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
connection the server chooses (a passive FTP data channel). Neither ever admits
another hostname. ``RouteScope.PUBLIC`` is the one scope that names no
hostname: it admits whatever PUBLIC destination its one live acquisition
discovers (a page, then its API, manifest and CDN hosts, each redirect a new
connection), for exactly as long as that acquisition holds it
(``public_route`` / ``revoke_public_route``), on the web ports only (80, 443
and unprivileged ports), and never under a private-LAN grant. No scope ever
skips this guard's own resolution and address policy.

A client that speaks plain HTTP through a proxy sends an absolute-form request
(``GET http://host/path``) rather than ``CONNECT``. Such a request is admitted
by exactly the same credential, resolution and address checks as a ``CONNECT``
to the same authority, and is then relayed to the approved address as one
origin-form request on its own connection.

A credential may additionally carry a private-LAN grant (signed into the
credential itself, domain-separated from ungranted credentials). Such a job may
reach RFC1918 addresses of its one authorized hostname -- and only while the
operator's global Local Network Connections policy is on at the moment of each
CONNECT (``configure_private_lan``). Loopback, link-local, metadata and every
other non-global class stay refused for every credential, and the whole DNS
answer set is still judged at connection time, so a mixed or rebinding answer
cannot smuggle one in. Core grants a job this only for an operator-submitted
LAN destination; a provider-returned endpoint never carries it.
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
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
import re
from urllib.parse import urlsplit

from services.network_safety import PUBLIC_DESTINATION_SCHEMES, default_destination_port
from services.network_safety import validate_provider_download_url
from services.network_safety import private_lan_address, reject_non_public_resolution

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
    PUBLIC = "public"


_SAME_HOST_USER = re.compile(
    re.escape(f"{_PROXY_USER}.") + r"(lan\.)?" + re.escape(f"{RouteScope.SAME_HOST.value}.") + r"([0-9]{1,5})"
)
# A public-destination route names its acquisition, never a host; it has no
# private-LAN form at all.
_PUBLIC_SCOPE = re.compile(r"[a-z0-9]{1,64}")
_PUBLIC_USER = re.compile(re.escape(f"{_PROXY_USER}.{RouteScope.PUBLIC.value}.") + r"([a-z0-9]{1,64})")
# The web ports a public-destination route may reach.
_PUBLIC_WELL_KNOWN_PORTS = frozenset({80, 443})
# The marker a private-LAN-granted credential's username carries.
_LAN = "lan"

Resolver = Callable[[str, int], Awaitable[list[tuple]]]
PublicCheck = Callable[[str], bool]


def _is_public(address: str) -> bool:
    normalized = str(address or "").split("%", 1)[0].strip()
    try:
        return bool(normalized) and ipaddress.ip_address(normalized).is_global
    except ValueError:
        return False


def _target(uri: str, private_lan: bool = False) -> tuple[str, int]:
    validated = validate_provider_download_url(
        uri, context="aria2 download link", schemes=PUBLIC_DESTINATION_SCHEMES, private_lan=private_lan)
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


def _absolute_target(target: str) -> tuple[str, int, str]:
    """``(host, port, origin-form path)`` of an absolute-form plain-HTTP target."""
    parsed = urlsplit(str(target or ""))
    host = str(parsed.hostname or "").rstrip(".").casefold()
    if parsed.scheme.casefold() != "http" or not host or parsed.username is not None or parsed.password is not None:
        raise ValueError("Unsupported proxy request target")
    port = int(parsed.port or default_destination_port("http"))
    path = parsed.path or "/"
    return host, port, path + (f"?{parsed.query}" if parsed.query else "")


# Hop-by-hop proxy headers never reach the origin; the relayed request always
# ends its connection, so one connection never carries a second authority.
_HOP_HEADERS = frozenset({"proxy-authorization", "proxy-connection", "connection", "keep-alive"})


def _origin_request(method: str, origin: str, version: str, headers: list[str]) -> bytes:
    kept = [line for line in headers if line and line.split(":", 1)[0].strip().casefold() not in _HOP_HEADERS]
    head = "\r\n".join([f"{method} {origin} {version}", *kept, "Connection: close", "", ""])
    return head.encode("iso-8859-1", errors="replace")


class _UpstreamRefused(OSError):
    """Every approved address of a destination actively refused the connection."""


_BUDGET_NAME = re.compile(r"[a-z0-9_-]{1,32}")


class EgressBudget:
    """One live download byte-rate budget shared by every relayed connection
    whose route names it (``0`` = unlimited).

    It enforces a rate it is given and owns no policy: DebridPulse's one
    aggregate owner (``transfers.runtime_coordination``) assigns an executor
    its share, and the executor sets it here. Pacing is on bytes delivered
    from the destination, so the sum over every connection of the budget never
    exceeds the rate by more than one relayed chunk, whatever the remote does;
    a connection that is paused or gone simply stops consuming it."""

    def __init__(self):
        self._rate = 0
        self._next = 0.0
        self._delivered = 0

    @property
    def rate(self) -> int:
        return self._rate

    @property
    def delivered(self) -> int:
        """Bytes delivered through this budget so far (paced or not)."""
        return self._delivered

    def set_rate(self, bytes_per_second: int) -> int:
        self._rate = max(0, int(bytes_per_second or 0))
        return self._rate

    async def consume(self, size: int) -> None:
        rate = self._rate
        if rate > 0 and size > 0:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + size / rate
            if start > now:
                await asyncio.sleep(start - now)
        self._delivered += max(0, size)


class _UpstreamTimeout(OSError):
    """No approved address of a destination answered within the route's bound."""


class TunnelTargetTimeout(TimeoutError):
    """``open_tunnel``: no approved address answered within the tunnel's own
    timeout. A ``TimeoutError`` -- exactly what a consumer already sees when
    that timeout elapses first."""


class TunnelTargetRefused(PermissionError):
    """``open_tunnel``: the approved server actively refused the connection.

    Still a ``PermissionError`` -- the tunnel was not opened -- so every
    consumer keeps its meaning; one that must tell "nothing serves that port"
    apart from a policy refusal catches this first."""


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
        # The operator's global Local Network Connections policy, read at every
        # CONNECT: turning it off stops granted jobs from reaching LAN too.
        self._private_lan = False
        self._budgets: dict[str, EgressBudget] = {}
        # The public-destination routes that exist right now, by acquisition.
        self._public_scopes: set[str] = set()

    def budget(self, name: str) -> EgressBudget:
        """The named download budget routes may carry (created unlimited)."""
        if not _BUDGET_NAME.fullmatch(str(name)):
            raise ValueError("Invalid egress budget name")
        return self._budgets.setdefault(str(name), EgressBudget())

    def configure_private_lan(self, enabled: bool) -> None:
        self._private_lan = bool(enabled)

    @property
    def private_lan_enabled(self) -> bool:
        return self._private_lan

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

    def _token(self, host: str, port: int, lan: bool = False) -> str:
        # A granted message is domain-separated ("lan|..."): a hostname never
        # contains "|", so an ungranted credential can never verify as granted.
        authority = f"{_LAN + '|' if lan else ''}{str(host).rstrip('.').casefold()}:{int(port)}".encode("utf-8")
        return hmac.new(self._secret, authority, hashlib.sha256).hexdigest()

    def _same_host_token(self, host: str, port: int, lan: bool = False) -> str:
        # Domain-separated from the endpoint token: a hostname never contains
        # "|", so no endpoint message can equal a same-host message.
        message = (f"{_LAN + '|' if lan else ''}{RouteScope.SAME_HOST.value}|"
                   f"{str(host).rstrip('.').casefold()}:{int(port)}")
        return hmac.new(self._secret, message.encode("utf-8"), hashlib.sha256).hexdigest()

    def _public_token(self, scope: str) -> str:
        # Domain-separated from every host-scoped message: a hostname never
        # contains "|", and no host-scoped message starts with this marker.
        message = f"{RouteScope.PUBLIC.value}|{scope}"
        return hmac.new(self._secret, message.encode("utf-8"), hashlib.sha256).hexdigest()

    def _terms(self, token: str, connect_timeout_seconds: float | None, budget: str | None) -> str:
        """A route token that also carries the route's own terms -- its connect
        bound (milliseconds, ``0`` = none) and the download budget it draws on
        -- signed with them; a route without terms keeps its token unchanged."""
        if connect_timeout_seconds is None and budget is None:
            return token
        bound = 0 if connect_timeout_seconds is None else max(1, int(round(float(connect_timeout_seconds) * 1000)))
        name = "" if budget is None else str(budget)
        if name and not _BUDGET_NAME.fullmatch(name):
            raise ValueError("Invalid egress budget name")
        signed = hmac.new(self._secret, f"{token}|connect={bound}|budget={name}".encode("utf-8"),
                          hashlib.sha256).hexdigest()
        return f"{signed}.{bound}" + (f".{name}" if name else "")

    def _credential(self, host: str, port: int, scope: RouteScope, lan: bool = False,
                    connect_timeout_seconds: float | None = None, budget: str | None = None) -> tuple[str, str]:
        user = f"{_PROXY_USER}.{_LAN}" if lan else _PROXY_USER
        if scope == RouteScope.ENDPOINT:
            return user, self._terms(self._token(host, port, lan), connect_timeout_seconds, budget)
        if scope == RouteScope.SAME_HOST:
            return (f"{user}.{scope.value}.{int(port)}",
                    self._terms(self._same_host_token(host, port, lan), connect_timeout_seconds, budget))
        raise ValueError("Unsupported egress route scope")

    def _verified(self, password: str, token: str) -> tuple[float | None, str | None] | None:
        """``None`` when ``password`` is not this route's credential; else the
        terms it carries: ``(connect bound in seconds, budget name)``."""
        parts = password.split(".")
        if len(parts) == 1:
            return (None, None) if hmac.compare_digest(password, token) else None
        if len(parts) > 3 or not parts[1].isdigit() or not 0 <= int(parts[1]) <= 3_600_000:
            return None
        name = parts[2] if len(parts) == 3 else None
        if name is not None and not _BUDGET_NAME.fullmatch(name):
            return None
        bound = int(parts[1]) / 1000 if int(parts[1]) else None
        if not hmac.compare_digest(password, self._terms(token, bound, name) if (bound or name) else token):
            return None
        return bound, name

    def _admits(self, username: str, password: str, host: str, port: int
                ) -> tuple[bool, float | None, str | None] | None:
        """Verify a CONNECT credential against the authority it names.
        ``None`` refuses; otherwise whether the credential carries a
        private-LAN grant, its connect bound (``None``: unbounded) and the
        download budget it draws on (``None``: none)."""
        for lan in (False, True):
            if username == (f"{_PROXY_USER}.{_LAN}" if lan else _PROXY_USER):
                terms = self._verified(password, self._token(host, port, lan))
                return None if terms is None else (lan, *terms)
        public = _PUBLIC_USER.fullmatch(username)
        if public is not None:
            # Any public destination of the one live acquisition this route
            # was issued to; never a private-LAN grant, whatever the setting.
            scope = public.group(1)
            if scope not in self._public_scopes:
                return None
            if port not in _PUBLIC_WELL_KNOWN_PORTS and port < _SERVER_SELECTED_PORT_FLOOR:
                return None
            terms = self._verified(password, self._public_token(scope))
            return None if terms is None else (False, *terms)
        match = _SAME_HOST_USER.fullmatch(username)
        if match is None:
            return None
        lan = bool(match.group(1))
        authorized = int(match.group(2))
        if not 0 < authorized <= 65535:
            return None
        if port != authorized and port < _SERVER_SELECTED_PORT_FLOOR:
            return None
        terms = self._verified(password, self._same_host_token(host, authorized, lan))
        return None if terms is None else (lan, *terms)

    def _proxy_url(self) -> str:
        if self._server is None or self._bound_port <= 0:
            raise RuntimeError("DebridPulse egress guard is not running")
        return f"http://{_LOOPBACK}:{self._bound_port}"

    def job_options(self, uri: str, *, scope: RouteScope = RouteScope.ENDPOINT,
                    private_lan: bool = False, budget: str | None = None) -> dict[str, str]:
        """Return per-addUri proxy policy that cannot inherit a daemon bypass.
        ``private_lan`` signs core's private-LAN grant into the credential;
        ``budget`` names the download budget the job's bytes draw on."""
        host, port = _target(uri, private_lan)
        proxy = self._proxy_url()
        user, token = self._credential(host, port, RouteScope(scope), bool(private_lan), budget=budget)
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

    def proxy_credential(self, uri: str, *, scope: RouteScope = RouteScope.ENDPOINT,
                         private_lan: bool = False, connect_timeout_seconds: float | None = None,
                         budget: str | None = None) -> tuple[str, int, str, str]:
        """``(proxy host, proxy port, user, token)`` for a native client that
        can only be pointed at an authenticated CONNECT proxy and cannot take a
        pre-opened connection (``open_tunnel``). The same signed route-scoped
        credential an aria2 job receives: it admits exactly the authorized
        hostname and port, and the guard still performs the final resolution
        and address policy at every CONNECT. ``connect_timeout_seconds`` is the
        route's own Connection Timeout, signed into the credential: the guard's
        connection to the destination is bounded by it (a destination that
        does not answer in time is ``504``). ``budget`` names the download
        budget (``budget()``) every byte this route delivers draws on."""
        host, port = _target(uri, private_lan)
        self._proxy_url()
        user, token = self._credential(host, port, RouteScope(scope), bool(private_lan), connect_timeout_seconds,
                                       budget)
        return _LOOPBACK, self._bound_port, user, token

    def public_route(self, scope: str, *, budget: str | None = None,
                     connect_timeout_seconds: float | None = None) -> tuple[str, int, str, str]:
        """``(proxy host, proxy port, user, token)`` of a public-destination
        route for one live acquisition ``scope``: it admits any destination
        whose WHOLE answer set this guard itself judges public at each
        connection -- every hop and redirect separately -- on the web ports,
        never a private-LAN address, and only until ``revoke_public_route``.
        ``budget`` and ``connect_timeout_seconds`` are signed route terms,
        exactly as for every other route."""
        scope = str(scope or "")
        if not _PUBLIC_SCOPE.fullmatch(scope):
            raise ValueError("Invalid public route scope")
        self._proxy_url()
        self._public_scopes.add(scope)
        user = f"{_PROXY_USER}.{RouteScope.PUBLIC.value}.{scope}"
        return _LOOPBACK, self._bound_port, user, self._terms(self._public_token(scope), connect_timeout_seconds,
                                                              budget)

    def revoke_public_route(self, scope: str) -> None:
        """End a public-destination route: its credential admits nothing more."""
        self._public_scopes.discard(str(scope or ""))

    async def open_tunnel(
        self, uri: str, *, scope: RouteScope = RouteScope.ENDPOINT, port: int | None = None,
        timeout_seconds: float = 10.0, private_lan: bool = False, budget: str | None = None,
    ) -> socket.socket:
        """Open one in-process connection through this guard's own CONNECT boundary.

        The application's bounded evidence readers reach a download origin
        exactly the way an owned aria2 job does: the same signed route-scoped
        credential, the same guard-owned resolution, public-address and
        mixed/private/rebinding checks, and the same admission rule for a
        server-selected port. ``port`` names that second connection (a passive
        FTP data channel) and is only admitted under ``RouteScope.SAME_HOST``.
        Returns a connected non-blocking socket carrying nothing but the
        tunnelled bytes; a refusal raises ``PermissionError`` -- the
        ``TunnelTargetRefused`` subclass when the approved server itself
        actively refused (nothing serves that port). ``timeout_seconds`` also
        bounds the guard's own connection to the destination; when that is
        what elapses, ``TunnelTargetTimeout`` (a ``TimeoutError``) is raised.
        ``budget`` names the download budget the tunnel's bytes draw on.
        """
        host, authorized = _target(uri, private_lan)
        await self.ensure_started()
        user, token = self._credential(host, authorized, RouteScope(scope), bool(private_lan), timeout_seconds,
                                       budget)
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
            if answer.startswith(b"HTTP/1.1 502 "):
                raise TunnelTargetRefused("The server refused the connection")
            if answer.startswith(b"HTTP/1.1 504 "):
                raise TunnelTargetTimeout("The server did not answer in time")
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

    def _admitted(self, address: str, lan: bool) -> bool:
        return self._public_check(address) or (lan and private_lan_address(address))

    async def _approved_endpoints(self, host: str, port: int, *, lan: bool = False) -> list[tuple[int, str, int]]:
        # The grant counts only while the operator's global policy is on NOW.
        lan = bool(lan) and self._private_lan
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            addresses = [str(literal)]
            if not self._admitted(str(literal), lan):
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
            reject_non_public_resolution(addresses, host=host, private_lan=lan)
        else:
            if not addresses:
                raise ValueError(f"Provider download host {host!r} did not resolve to an address")
            blocked = [address for address in addresses if not self._admitted(address, lan)]
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

    async def _connect_upstream(self, endpoints: list[tuple[int, str, int]], bound: float | None = None):
        """Connect to the first approved address that accepts, within the
        route's connect ``bound`` (all addresses together) when it has one."""
        errors: list[OSError] = []
        try:
            async with asyncio.timeout(bound):
                for family, address, port in endpoints:
                    try:
                        return await asyncio.open_connection(
                            address,
                            port,
                            family=family,
                            flags=socket.AI_NUMERICHOST,
                        )
                    except OSError as exc:
                        errors.append(exc)
        except TimeoutError as exc:
            raise _UpstreamTimeout("No approved address answered within the route's connect bound") from exc
        last_error = errors[-1] if errors else None
        if errors and all(isinstance(error, ConnectionRefusedError) for error in errors):
            raise _UpstreamRefused("Every approved address refused the connection") from last_error
        if any(isinstance(error, TimeoutError) for error in errors):
            raise _UpstreamTimeout("No approved address answered in time") from last_error
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
            origin = None
            if len(request) == 3 and request[0].upper() == "CONNECT":
                host, port = _authority_target(request[1])
            elif (len(request) == 3 and request[0].isalpha() and request[2].startswith("HTTP/1.")
                  and request[1][:7].casefold() == "http://"):
                # A plain-HTTP absolute-form request: the same authority checks
                # as a CONNECT to it, then one origin-form request upstream.
                host, port, origin = _absolute_target(request[1])
            else:
                writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            username, password = self._proxy_credentials(lines[1:])
            admitted = self._admits(username, password, host, port)
            if admitted is None:
                writer.write(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                    b"Proxy-Authenticate: Basic realm=\"DebridPulse\"\r\n"
                    b"Connection: close\r\n\r\n"
                )
                await writer.drain()
                return

            lan, bound, budget_name = admitted
            budget = self.budget(budget_name) if budget_name else None
            try:
                endpoints = await self._approved_endpoints(host, port, lan=lan)
                upstream_reader, upstream_writer = await self._connect_upstream(endpoints, bound)
            except _UpstreamTimeout:
                # No approved address answered within the route's own bound
                # (or the network's): uncertainty, never a refusal or a policy fact.
                writer.write(b"HTTP/1.1 504 Gateway Timeout\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            except _UpstreamRefused:
                # An approved destination that actively refused: definitive
                # evidence that nothing serves that port -- distinct from a
                # policy refusal or a network failure, which stay 403.
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            except (ValueError, OSError):
                # Proxy-Status (RFC 9209) says the refusal is this guard's own,
                # so a client can never mistake it for the origin's answer.
                writer.write(b"HTTP/1.1 403 Forbidden\r\n"
                             b"Proxy-Status: debridpulse; error=destination_ip_prohibited\r\n"
                             b"Connection: close\r\n\r\n")
                await writer.drain()
                return

            if origin is None:
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                upstream_writer.write(_origin_request(request[0], origin, request[2], lines[1:]))
                await upstream_writer.drain()

            async def relay(source: asyncio.StreamReader, destination: asyncio.StreamWriter,
                            paced: EgressBudget | None = None) -> None:
                while True:
                    chunk = await source.read(64 * 1024)
                    if not chunk:
                        return
                    if paced is not None:
                        await paced.consume(len(chunk))
                    destination.write(chunk)
                    await destination.drain()

            tasks = {
                asyncio.create_task(relay(reader, upstream_writer)),
                # Only what the destination delivers draws on a download budget.
                asyncio.create_task(relay(upstream_reader, writer, budget)),
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
