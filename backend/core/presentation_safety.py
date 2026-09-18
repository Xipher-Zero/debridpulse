"""Secret-safe user-facing source presentation helpers.

These helpers are deliberately separate from logging sanitization. Browser-facing
provenance may preserve useful identity from the durable user request, while log
sanitization remains free to redact long URLs wholesale.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


def _middle_ellipsis(value: str, max_length: int) -> str:
    """Bound presentation text while retaining both source and filename identity."""
    limit = max(24, int(max_length))
    if len(value) <= limit:
        return value
    marker = "…"
    tail = max(12, min(64, limit // 3))
    head = limit - tail - len(marker)
    return value[:head] + marker + value[-tail:]


_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def safe_public_host(value: object) -> str | None:
    """Return a bare, browser-safe DNS host name, or ``None``.

    The one host-only normalizer for browser-facing source identity: trimmed,
    lower-cased, a leading ``www.`` and any trailing dot removed, at most 253
    characters, and every label strictly ``[a-z0-9-]`` (no leading/trailing
    hyphen, at most 63 characters). Userinfo, ports, paths, queries,
    fragments, whitespace, and non-ASCII/malformed hosts all fail the label
    check, so they are rejected outright rather than repaired -- the caller
    must never fall back to the unsanitized input.
    """
    host = str(value or "").strip().lower().removeprefix("www.").rstrip(".")
    if not host or len(host) > 253:
        return None
    if any(not label or not _HOST_LABEL_RE.fullmatch(label) for label in host.split(".")):
        return None
    return host


_ROUTE_SCHEMES = frozenset({"http", "https", "ftp", "sftp", "scp"})
_ROUTE_DEFAULT_PORTS = {"http": 80, "https": 443, "ftp": 21, "sftp": 22, "scp": 22}


def safe_route_endpoint(value: object, *, max_length: int = 180) -> tuple[str | None, str | None]:
    """Return ``(route_origin, route_location)`` for a durable historical route
    endpoint, or ``(None, None)`` when it cannot be safely represented.

    Provider-neutral generalization of ``safe_original_http_resource``'s same
    parsing/redaction discipline across every URI-like transport DP presents
    through generic routing (DP 1.0.12 Route History identity correction):
    ``route_origin`` is bare ``scheme://hostname[:non-default-port]``;
    ``route_location`` additionally carries the safe path. An explicit port
    equal to the scheme's well-known default (Gate 9 revision 2) is dropped
    so ``https://host`` and ``https://host:443`` identify the SAME origin for
    both display and same-origin collision detection; a genuinely
    non-default port always remains visible. Userinfo, query, and fragment
    never survive in either. An unparseable or unsupported-scheme value
    fails closed to ``(None, None)`` rather than leaking the raw endpoint --
    the caller must never fall back to the unsanitized input.
    """
    raw = str(value or "").strip()
    if not raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return None, None
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.casefold()
        if scheme not in _ROUTE_SCHEMES or not parsed.hostname:
            return None, None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None and parsed.port != _ROUTE_DEFAULT_PORTS.get(scheme):
            host = f"{host}:{parsed.port}"
    except ValueError:
        return None, None

    origin = urlunsplit((scheme, host, "", "", ""))
    path = parsed.path
    location = urlunsplit((scheme, host, path, "", "")) if path and path != "/" else origin
    return _middle_ellipsis(origin, max_length), _middle_ellipsis(location, max_length)


def safe_original_http_resource(value: object, *, max_length: int = 180) -> str | None:
    """Return a useful HTTP(S) source label without userinfo, query values, or fragments.

    The caller must supply the durable original user request, never a resolved or
    provider-issued capability. Query presence is retained only as an ellipsis so
    signed/token-bearing values cannot enter browser-facing provenance.
    """
    raw = str(value or "").strip()
    if not raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return None
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
    except ValueError:
        return None

    path = parsed.path or "/"
    safe = urlunsplit((scheme, host, path, "", ""))
    if parsed.query:
        safe += "?…"
    return _middle_ellipsis(safe, max_length)
