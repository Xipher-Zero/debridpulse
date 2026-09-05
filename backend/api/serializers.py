"""Browser-facing serializers for canonical transfer records.

Persistence rows intentionally retain provider/materialization capabilities such
as magnets and unlocked URLs. Ordinary API responses must not expose those
capabilities merely because a database row contains them.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import re
from typing import Any
import json

from transfers.errors import NormalizedError


_OPAQUE_FIELDS = frozenset({"payload", "context", "handle", "candidate", "candidates", "candidate_source", "candidate_bindings", "endpoints", "request", "resource", "redactions", "headers", "normalized_error"})
_TORRENT_PRIVATE_FIELDS = frozenset({"magnet", "download_url"}) | _OPAQUE_FIELDS
_FILE_PRIVATE_FIELDS = frozenset({"source_url", "download_url"}) | _OPAQUE_FIELDS
_CAPABILITY_FIELDS = _TORRENT_PRIVATE_FIELDS | _FILE_PRIVATE_FIELDS
_NAIVE_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$")
_PRE_MATERIALIZATION_STATUSES = frozenset({
    "pending",
    "uploading",
    "processing",
    "ready",
})


def _public_timestamp(value: Any) -> Any:
    """Serialize SQLite UTC timestamp values with an explicit UTC designator."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str):
        stripped = value.strip()
        if _NAIVE_UTC_RE.fullmatch(stripped):
            return stripped.replace(" ", "T") + "Z"
    return value


def _public_field(key: str, value: Any) -> Any:
    return _public_timestamp(value) if key.endswith("_at") else value


def _without_fields(value: Mapping[str, Any], private_fields: frozenset[str]) -> dict[str, Any]:
    result = {
        key: _public_field(key, item)
        for key, item in dict(value).items()
        if key not in private_fields
    }
    error = value.get("normalized_error")
    if error:
        try:
            normalized = NormalizedError.from_dict(json.loads(error) if isinstance(error, str) else error)
        except (ValueError, TypeError, KeyError):
            pass
        else:
            result["error"] = normalized.as_dict()
            result["error_message"] = normalized.message
    return result


def public_torrent(value: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize a torrent row using browser-facing transfer semantics.

    Provider readiness does not prove local possession. Display local progress
    only once materialization or execution has established it.
    """
    payload = _without_fields(value, _TORRENT_PRIVATE_FIELDS)
    status = str(payload.get("status") or "").strip().lower()
    if status in _PRE_MATERIALIZATION_STATUSES:
        payload["progress"] = 0.0
    return payload


def public_download_file(value: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize a download_files row without source/unlocked URLs."""
    return _without_fields(value, _FILE_PRIVATE_FIELDS)


def public_payload(value: Any) -> Any:
    """Recursively remove known capability-bearing persistence fields."""
    if isinstance(value, Mapping):
        return {
            key: _public_field(key, public_payload(item))
            for key, item in value.items()
            if key not in _CAPABILITY_FIELDS
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [public_payload(item) for item in value]
    return value
