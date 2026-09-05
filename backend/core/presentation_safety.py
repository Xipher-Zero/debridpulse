"""Secret-safe user-facing source presentation helpers.

These helpers are deliberately separate from logging sanitization. Browser-facing
provenance may preserve useful identity from the durable user request, while log
sanitization remains free to redact long URLs wholesale.
"""
from __future__ import annotations

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
