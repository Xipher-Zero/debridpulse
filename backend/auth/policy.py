from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlparse, urlsplit

from auth.passwords import is_usable_password_hash


MUTATING_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PUBLIC_PATHS = frozenset(
    {
        "/api/health",
        "/api/version",
        "/api/avatar",
        "/api/auth/status",
        "/login",
        "/auth/oidc/start",
        "/auth/oidc/callback",
    }
)


def is_public_path(path: str) -> bool:
    return str(path or "") in PUBLIC_PATHS


def password_auth_enabled(settings) -> bool:
    return bool(getattr(settings, "auth_password_enabled", False))


def password_auth_ready(settings) -> bool:
    if not password_auth_enabled(settings):
        return False
    username = str(getattr(settings, "auth_username", "") or "").strip()
    password_hash = str(getattr(settings, "auth_password_hash", "") or "").strip()
    return bool(username and is_usable_password_hash(password_hash))


def password_auth_configured(settings) -> bool:
    """Compatibility name for callers asking whether password auth is usable."""
    return password_auth_ready(settings)


def oidc_auth_enabled(settings) -> bool:
    return bool(getattr(settings, "auth_oidc_enabled", False))


def interactive_auth_enabled(settings) -> bool:
    return password_auth_enabled(settings) or oidc_auth_enabled(settings)


def safe_return_path(value: str, *, default: str = "/") -> str:
    """Accept only a local absolute-path reference; never create an open redirect."""
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 2048:
        return default
    # Reject every network-path reference before URL parsing. Python's urlsplit
    # normalizes some 3+ slash inputs in ways browsers subsequently reinterpret.
    if not candidate.startswith("/") or candidate.startswith("//"):
        return default
    if any(ch in candidate for ch in ("\\", "\r", "\n", "\x00")):
        return default
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return default
    if parsed.scheme or parsed.netloc:
        return default
    if not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return default
    if parsed.path in {"/login", "/auth/oidc/start", "/auth/oidc/callback"}:
        return default
    return candidate


def normalized_origin(value: str) -> tuple[str, str, int] | None:
    """Return a canonical HTTP(S) origin tuple or ``None`` when malformed.

    Scheme is part of the browser origin. Treating ``http://host`` and
    ``https://host`` as equivalent would weaken the cross-site mutation boundary
    on deployments that expose both transports or have an HTTP downgrade path.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            return None
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, parsed.hostname.casefold(), int(port)


def normalized_host_authority(value: str) -> tuple[str, int | None] | None:
    """Parse an HTTP Host authority without letting it define a trusted scheme."""
    raw = str(value or "").strip()
    if not raw or any(ch.isspace() for ch in raw) or any(ch in raw for ch in "/\\?#"):
        return None
    try:
        parsed = urlsplit(f"//{raw}")
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        if parsed.path or parsed.query or parsed.fragment:
            return None
        port = parsed.port
    except ValueError:
        return None
    if port is not None and not (1 <= int(port) <= 65535):
        return None
    return parsed.hostname.casefold(), int(port) if port is not None else None


def authority_matches_origin(
    authority: tuple[str, int | None] | None,
    origin: tuple[str, str, int] | None,
) -> bool:
    """Compare Host authority to a configured origin with deliberate default ports."""
    if authority is None or origin is None:
        return False
    host, explicit_port = authority
    scheme, origin_host, origin_port = origin
    port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    return host == origin_host and int(port) == int(origin_port)


def configured_public_base_url(settings=None) -> str:
    """Return the operator-owned externally advertised base URL, if configured."""
    configured = (os.getenv("PUBLIC_BASE_URL", "") or "").strip()
    if configured:
        return configured
    if settings is None:
        try:
            from core.config import get_settings

            settings = get_settings()
        except Exception:  # noqa: BLE001 - authority resolution must fail closed
            return ""
    return str(getattr(settings, "public_base_url", "") or "").strip()


def configured_public_origin(settings=None) -> tuple[str, str, int] | None:
    """Return the valid HTTPS public application origin or ``None``.

    ``public_base_url`` already owns the externally advertised application
    authority for OIDC and secure-cookie behavior. Reuse it rather than deriving
    browser trust from client-selected Host values.
    """
    identity = normalized_origin(configured_public_base_url(settings))
    if identity is None or identity[0] != "https":
        return None
    return identity


def _direct_request_origin(request, authority: tuple[str, int | None]) -> tuple[str, str, int] | None:
    scheme = str(getattr(request.url, "scheme", "") or "").casefold()
    if scheme not in {"http", "https"}:
        return None
    host, explicit_port = authority
    port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    return scheme, host, int(port)


def _is_literal_direct_host(host: str) -> bool:
    if str(host or "").casefold() == "localhost":
        return True
    try:
        ipaddress.ip_address(str(host or ""))
    except ValueError:
        return False
    return True


def trusted_request_origin(request, settings=None) -> tuple[str, str, int] | None:
    """Resolve browser-visible request origin only from intentional authorities.

    A configured public base URL is the canonical reverse-proxy authority.
    Direct access deliberately permits localhost, literal IP authorities, and an
    authority that exactly matches ASGI's transport-owned ``scope['server']``.
    Arbitrary DNS Host values are never promoted to trust merely because Origin
    repeats the same value.
    """
    authority = normalized_host_authority(str(request.headers.get("Host", "") or ""))
    if authority is None:
        return None

    public_origin = configured_public_origin(settings)
    if authority_matches_origin(authority, public_origin):
        return public_origin

    direct_origin = _direct_request_origin(request, authority)
    if direct_origin is None:
        return None
    _scheme, host, port = direct_origin
    if _is_literal_direct_host(host):
        return direct_origin

    server = request.scope.get("server") if hasattr(request, "scope") else None
    if isinstance(server, (tuple, list)) and len(server) >= 2:
        server_host = str(server[0] or "").strip().casefold()
        try:
            server_port = int(server[1])
        except (TypeError, ValueError):
            server_port = -1
        if host == server_host and port == server_port:
            return direct_origin
    return None


def normalized_origin_host(origin: str) -> str:
    """Compatibility helper returning the case-folded origin authority."""
    try:
        return (urlparse(str(origin or "").strip()).netloc or "").casefold()
    except ValueError:
        return ""
