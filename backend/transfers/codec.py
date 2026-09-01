"""Versionable persistence encoding; opaque adapter contexts are not interpreted."""
from __future__ import annotations

import base64
from dataclasses import fields, is_dataclass
from enum import Enum
import json
from typing import Mapping

from transfers.errors import NormalizedError
from transfers.models import (
    Endpoint, ExecutionHandle, IntegrityMetadata, Ownership, ProviderResource,
    TransferCandidate, TransferRequest, SourceEntry,
)


def _value(value):
    if isinstance(value, NormalizedError):
        return _value(value.as_dict(diagnostics=True))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if is_dataclass(value):
        return {item.name: _value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_value(item) for item in value]
    return value


def dump(value) -> str:
    return json.dumps(_value(value), separators=(",", ":"), sort_keys=True, allow_nan=False)


def load(value: str | None, default=None):
    return json.loads(value) if value else default


def request(value: dict) -> TransferRequest:
    data = dict(value)
    payload = data["payload"]
    if isinstance(payload, dict) and "$bytes" in payload:
        data["payload"] = base64.b64decode(payload["$bytes"], validate=True)
    return TransferRequest(**data)


def resource(value: dict | None) -> ProviderResource | None:
    if value is None:
        return None
    data = dict(value)
    data["ownership"] = Ownership(data["ownership"])
    return ProviderResource(**data)


def candidate(value: dict) -> TransferCandidate:
    data = dict(value)
    data["endpoints"] = tuple(Endpoint(**item) for item in data["endpoints"])
    data["integrity"] = tuple(IntegrityMetadata(**item) for item in data.get("integrity", []))
    data["resource"] = resource(data.get("resource"))
    if data.get("refresh_request"):
        data["refresh_request"] = request(data["refresh_request"])
    return TransferCandidate(**data)


def handle(value: dict | None) -> ExecutionHandle | None:
    return ExecutionHandle(**value) if value else None


def entry(value: dict | None) -> SourceEntry | None:
    return SourceEntry(value["name"], value["expected_bytes"], value["relative_path"], request(value["request"])) if value else None


def error(value: str | None) -> NormalizedError | None:
    return NormalizedError.from_dict(load(value)) if value else None
