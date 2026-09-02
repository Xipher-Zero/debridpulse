"""AllDebrid-owned supported-host runtime state and maintenance.

Native AllDebrid host/account payload semantics terminate in this module.  The
neutral runtime-state store persists only opaque bytes and timestamps; the
neutral applicability classifier receives only canonical host claims.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from ipaddress import ip_address
import json
import logging
import math
import re
import time
from typing import Any, Callable, Mapping

from core.logging_utils import sanitize_exception
from integrations.runtime_state import RuntimeStateConflict, RuntimeStateRecord
from transfers.applicability import (
    HostClaim, HostClaimScope, ProviderApplicability, parse_url_applicability,
)

logger = logging.getLogger("alldebrid.hosts")

INTEGRATION_ID = "alldebrid"
HOST_STATE_KEY = "supported-hosts"
HOST_SCHEMA_VERSION = "alldebrid-supported-hosts-v1"
HOST_SOURCE = "v4.1/user/hosts"
HOST_REFRESH_SECONDS = 24 * 60 * 60
HOST_REFRESH_RETRY_SECONDS = 15 * 60
_MAX_PATTERN_LENGTH = 8192
_MAX_PATTERNS_PER_HOST = 128
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.ASCII)


class AllDebridHostSnapshotError(ValueError):
    """Native or persisted AllDebrid host data is not safe to use."""


@dataclass(frozen=True)
class AllDebridHost:
    service_id: str
    name: str
    service_type: str
    domains: tuple[str, ...]
    regexps: tuple[str, ...]
    available: bool | None = None
    quota: int | None = None
    quota_max: int | None = None
    quota_type: str | None = None
    simultaneous_downloads_remaining: int | None = None


@dataclass(frozen=True)
class AllDebridHostSnapshot:
    hosts: tuple[AllDebridHost, ...]
    source: str = HOST_SOURCE

    @property
    def claims(self) -> tuple[HostClaim, ...]:
        """Translate structural host support into neutral Item 6 facts only."""
        domains = sorted({domain for host in self.hosts for domain in host.domains})
        return tuple(
            HostClaim(
                domain,
                HostClaimScope.EXACT,
                frozenset({"http", "https"}),
            )
            for domain in domains
        )


class AllDebridRequestApplicability:
    """Evaluate native URL regexps locally and emit only neutral Item 6 facts.

    AllDebrid documents ``regexps`` as the supported-link validators. Host-only
    claims cannot express path/query restrictions, so native expressions stay
    here and only an already-matched request hostname crosses the boundary.
    """

    def __init__(self, snapshot: AllDebridHostSnapshot | None) -> None:
        self._compiled = tuple(
            (host, tuple(re.compile(pattern) for pattern in host.regexps))
            for host in (() if snapshot is None else snapshot.hosts)
        )

    @staticmethod
    def _within_domain(hostname: str, domain: str) -> bool:
        return hostname == domain or hostname.endswith("." + domain)

    def __call__(self, request) -> ProviderApplicability:
        view = parse_url_applicability(request)
        if view is None or view.scheme not in {"http", "https"}:
            return ProviderApplicability()
        raw = request.payload if isinstance(request.payload, str) else ""
        if not raw or len(raw) > _MAX_PATTERN_LENGTH:
            return ProviderApplicability()

        for host, patterns in self._compiled:
            if not any(self._within_domain(view.hostname, domain) for domain in host.domains):
                continue
            if not any(pattern.search(raw) for pattern in patterns):
                continue
            return ProviderApplicability(
                specialized_hosts=(
                    HostClaim(view.hostname, HostClaimScope.EXACT, frozenset({view.scheme})),
                )
            )
        return ProviderApplicability()


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AllDebridHostSnapshotError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_nonnegative_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise AllDebridHostSnapshotError(f"{field} must be an integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AllDebridHostSnapshotError(f"{field} must be an integer") from exc
    if normalized < 0:
        raise AllDebridHostSnapshotError(f"{field} must not be negative")
    return normalized


def _normalize_domain(value: Any) -> str:
    raw = _text(value, field="domain").rstrip(".")
    if "://" in raw or "/" in raw or "@" in raw:
        raise AllDebridHostSnapshotError("domain must contain only a hostname")
    candidate = raw.strip("[]")
    try:
        address = ip_address(candidate)
    except ValueError:
        try:
            ascii_host = candidate.encode("idna").decode("ascii").casefold()
        except UnicodeError as exc:
            raise AllDebridHostSnapshotError("domain is not valid IDNA") from exc
        if len(ascii_host) > 253:
            raise AllDebridHostSnapshotError("domain is too long")
        labels = ascii_host.split(".")
        if any(
            not label or len(label) > 63 or not _DNS_LABEL_RE.fullmatch(label)
            for label in labels
        ):
            raise AllDebridHostSnapshotError("domain is not a valid DNS hostname")
        return ascii_host
    return address.compressed.casefold()


def _native_regexps(record: Mapping[str, Any]) -> tuple[str, ...]:
    # Current v4.1 documentation names this field ``regexps``.  The service
    # examples have historically also shown singular ``regexp``; accept that
    # native compatibility form without exposing either spelling universally.
    if "regexps" in record:
        raw = record["regexps"]
    elif "regexp" in record:
        raw = record["regexp"]
    else:
        raise AllDebridHostSnapshotError("host regexps are missing")

    if isinstance(raw, str):
        values = (raw,)
    elif isinstance(raw, list):
        values = tuple(raw)
    else:
        raise AllDebridHostSnapshotError("host regexps must be a string or list")

    if len(values) > _MAX_PATTERNS_PER_HOST:
        raise AllDebridHostSnapshotError("host has too many regexps")

    patterns: list[str] = []
    for item in values:
        pattern = _text(item, field="regexp")
        if len(pattern) > _MAX_PATTERN_LENGTH:
            raise AllDebridHostSnapshotError("host regexp is too long")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise AllDebridHostSnapshotError("host regexp is malformed") from exc
        patterns.append(pattern)
    return tuple(patterns)


def _native_host(service_id: str, record: Any) -> AllDebridHost:
    if not isinstance(record, Mapping):
        raise AllDebridHostSnapshotError("host record must be an object")
    service_id = _text(service_id, field="service id")
    name = _text(record.get("name"), field="host name")
    service_type = _text(record.get("type"), field="host type").casefold()
    if service_type not in {"free", "premium"}:
        raise AllDebridHostSnapshotError("host type is unsupported")

    raw_domains = record.get("domains")
    if not isinstance(raw_domains, list) or not raw_domains:
        raise AllDebridHostSnapshotError("host domains must be a non-empty list")
    domains = tuple(dict.fromkeys(_normalize_domain(item) for item in raw_domains))
    regexps = _native_regexps(record)

    available = record.get("status")
    if available is not None and not isinstance(available, bool):
        raise AllDebridHostSnapshotError("host status must be boolean when present")

    quota_type = record.get("quotaType")
    if quota_type is not None:
        quota_type = _text(quota_type, field="quota type")
        if quota_type not in {"traffic", "nb_download"}:
            raise AllDebridHostSnapshotError("quota type is unsupported")

    return AllDebridHost(
        service_id=service_id,
        name=name,
        service_type=service_type,
        domains=domains,
        regexps=regexps,
        available=available,
        quota=_optional_nonnegative_int(record.get("quota"), field="quota"),
        quota_max=_optional_nonnegative_int(record.get("quotaMax"), field="quotaMax"),
        quota_type=quota_type,
        simultaneous_downloads_remaining=_optional_nonnegative_int(
            record.get("limitSimuDl"), field="limitSimuDl"
        ),
    )


def parse_native_host_snapshot(data: Any) -> AllDebridHostSnapshot:
    """Validate one successful ``/v4.1/user/hosts`` data object.

    Structural support is the presence of a validated host/domain entry.
    ``status`` and account-limit fields are retained separately and never decide
    whether a structural applicability claim exists.
    """
    if not isinstance(data, Mapping):
        raise AllDebridHostSnapshotError("host inventory data must be an object")
    raw_hosts = data.get("hosts")
    if not isinstance(raw_hosts, Mapping) or not raw_hosts:
        raise AllDebridHostSnapshotError("host inventory must contain hosts")
    hosts = tuple(
        _native_host(str(service_id), record)
        for service_id, record in sorted(raw_hosts.items(), key=lambda item: str(item[0]))
    )
    if not hosts:
        raise AllDebridHostSnapshotError("host inventory is empty")
    return AllDebridHostSnapshot(hosts)


def encode_host_snapshot(snapshot: AllDebridHostSnapshot) -> bytes:
    if not isinstance(snapshot, AllDebridHostSnapshot) or not snapshot.hosts:
        raise AllDebridHostSnapshotError("cannot persist an empty host snapshot")
    document = {
        "source": snapshot.source,
        "hosts": [
            {
                "service_id": host.service_id,
                "name": host.name,
                "service_type": host.service_type,
                "domains": list(host.domains),
                "regexps": list(host.regexps),
                "available": host.available,
                "quota": host.quota,
                "quota_max": host.quota_max,
                "quota_type": host.quota_type,
                "simultaneous_downloads_remaining": host.simultaneous_downloads_remaining,
            }
            for host in snapshot.hosts
        ],
    }
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def decode_host_snapshot(payload: bytes) -> AllDebridHostSnapshot:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise AllDebridHostSnapshotError("host snapshot payload must be bytes")
    try:
        document = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AllDebridHostSnapshotError("host snapshot payload is corrupt") from exc
    if not isinstance(document, Mapping) or document.get("source") != HOST_SOURCE:
        raise AllDebridHostSnapshotError("host snapshot source is incompatible")
    raw_hosts = document.get("hosts")
    if not isinstance(raw_hosts, list) or not raw_hosts:
        raise AllDebridHostSnapshotError("host snapshot hosts are missing")

    native_hosts: dict[str, dict[str, Any]] = {}
    for raw in raw_hosts:
        if not isinstance(raw, Mapping):
            raise AllDebridHostSnapshotError("persisted host record must be an object")
        service_id = _text(raw.get("service_id"), field="service id")
        if service_id in native_hosts:
            raise AllDebridHostSnapshotError("persisted host service id is duplicated")
        native_hosts[service_id] = {
            "name": raw.get("name"),
            "type": raw.get("service_type"),
            "domains": raw.get("domains"),
            "regexps": raw.get("regexps"),
            "status": raw.get("available"),
            "quota": raw.get("quota"),
            "quotaMax": raw.get("quota_max"),
            "quotaType": raw.get("quota_type"),
            "limitSimuDl": raw.get("simultaneous_downloads_remaining"),
        }
    return parse_native_host_snapshot({"hosts": native_hosts})


class AllDebridHostMaintenance:
    """Maintenance-only refresh and durable LKG ownership for AllDebrid hosts."""

    def __init__(
        self,
        store,
        *,
        clock: Callable[[], float] = time.time,
        refresh_seconds: float = HOST_REFRESH_SECONDS,
        retry_seconds: float = HOST_REFRESH_RETRY_SECONDS,
    ) -> None:
        self._store = store
        self._clock = clock
        self._refresh_seconds = float(refresh_seconds)
        self._retry_seconds = float(retry_seconds)
        if (
            not math.isfinite(self._refresh_seconds)
            or self._refresh_seconds <= 0
            or not math.isfinite(self._retry_seconds)
            or self._retry_seconds <= 0
        ):
            raise ValueError("refresh and retry intervals must be positive")
        self._provider = None
        self._loaded_provider = None
        self._record: RuntimeStateRecord | None = None
        self._snapshot: AllDebridHostSnapshot | None = None
        self._refresh_on_enable = False
        self._next_retry_at = 0.0
        self._refresh_lock = asyncio.Lock()

    def bind(self, provider, *, initial: bool = False) -> None:
        """Bind the current registry instance without owning registry policy."""
        previous_enabled = bool(
            self._provider is not None and self._provider.descriptor.enabled
        )
        self._provider = provider
        self._loaded_provider = None
        self._record = None
        self._snapshot = None
        if provider is not None:
            self._apply_snapshot(provider, None)
        current_enabled = bool(provider is not None and provider.descriptor.enabled)
        if not initial and current_enabled and not previous_enabled:
            self._refresh_on_enable = True
        elif not current_enabled:
            self._refresh_on_enable = False
        self._next_retry_at = 0.0

    @staticmethod
    def _apply_snapshot(provider, snapshot: AllDebridHostSnapshot | None) -> None:
        claims = snapshot.claims if snapshot is not None else ()
        provider.applicability = ProviderApplicability(
            specialized_hosts=tuple(claims)
        )
        provider.applicability_for = AllDebridRequestApplicability(snapshot)

    @staticmethod
    async def _fetch_native_hosts(provider):
        # Authentication, pacing, timeout and native error handling stay inside
        # the canonical AllDebrid client method; maintenance owns only cadence,
        # validation, persistence and applicability publication.
        return await provider.client.get_user_hosts()

    async def start(self) -> None:
        """Restore persisted claims only; startup itself performs no host fetch."""
        await self._ensure_loaded()

    async def stop(self) -> None:
        return None

    async def maintain(self) -> None:
        provider = self._provider
        if provider is None or not provider.descriptor.enabled:
            return
        await self._ensure_loaded()
        now = float(self._clock())
        if not math.isfinite(now):
            raise ValueError("clock must return a finite UTC epoch timestamp")
        if now < self._next_retry_at:
            return
        if self._refresh_due(now):
            await self._refresh()

    def _refresh_due(self, now: float) -> bool:
        if self._refresh_on_enable or self._snapshot is None or self._record is None:
            return True
        if self._record.stale_after is None:
            return True
        return self._record.is_stale(now=now)

    async def _ensure_loaded(self, *, force: bool = False) -> None:
        provider = self._provider
        if provider is None or not provider.descriptor.enabled:
            return
        if not force and self._loaded_provider is provider:
            return

        self._loaded_provider = provider
        self._apply_snapshot(provider, None)
        self._record = None
        self._snapshot = None
        try:
            record = await self._store.load(INTEGRATION_ID, HOST_STATE_KEY)
        except Exception as exc:
            logger.warning(
                "AllDebrid host runtime state could not be loaded: %s",
                sanitize_exception(exc),
            )
            return
        self._record = record
        if record is None:
            return
        if record.schema_version != HOST_SCHEMA_VERSION:
            logger.warning(
                "AllDebrid host runtime snapshot is incompatible; maintenance refresh required"
            )
            return
        try:
            snapshot = decode_host_snapshot(record.payload)
        except AllDebridHostSnapshotError as exc:
            logger.warning(
                "AllDebrid host runtime snapshot is invalid: %s",
                sanitize_exception(exc),
            )
            return
        self._snapshot = snapshot
        self._apply_snapshot(provider, snapshot)

    async def _refresh(self) -> None:
        async with self._refresh_lock:
            provider = self._provider
            if provider is None or not provider.descriptor.enabled:
                return
            await self._ensure_loaded()
            now = float(self._clock())
            if now < self._next_retry_at or not self._refresh_due(now):
                return
            expected_generation = self._record.generation if self._record else 0

            try:
                native = await self._fetch_native_hosts(provider)
                snapshot = parse_native_host_snapshot(native)
                # A configuration transition during network I/O invalidates this
                # response for the newly bound provider/account.
                if provider is not self._provider or not provider.descriptor.enabled:
                    return
                record = await self._store.replace(
                    INTEGRATION_ID,
                    encode_host_snapshot(snapshot),
                    schema_version=HOST_SCHEMA_VERSION,
                    state_key=HOST_STATE_KEY,
                    observed_at=now,
                    successful_at=now,
                    stale_after=now + self._refresh_seconds,
                    expected_generation=expected_generation,
                )
            except RuntimeStateConflict:
                # Another successful writer won. Consume that authoritative
                # generation rather than allowing this refresh to overwrite it.
                await self._ensure_loaded(force=True)
                self._refresh_on_enable = False
                self._next_retry_at = 0.0
                return
            except Exception as exc:
                self._next_retry_at = now + self._retry_seconds
                logger.warning(
                    "AllDebrid supported-host refresh failed; retaining last-known-good state: %s",
                    sanitize_exception(exc),
                )
                return

            self._record = record
            self._snapshot = snapshot
            self._loaded_provider = provider
            self._refresh_on_enable = False
            self._next_retry_at = 0.0
            self._apply_snapshot(provider, snapshot)
            logger.info(
                "AllDebrid supported-host snapshot refreshed (%d services, %d host claims)",
                len(snapshot.hosts),
                len(snapshot.claims),
            )
