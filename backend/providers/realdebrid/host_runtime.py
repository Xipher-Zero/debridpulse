"""Real-Debrid-owned supported-host runtime state and maintenance.

Native Real-Debrid host semantics terminate in this module. ``/hosts/domains``
is the structural set of supported hosts and ``/hosts/regex`` the supported-link
validators; both are public inventory, not account state, so the snapshot is
not scoped to a credential. ``/hosts/status`` (whether a host is up right now)
is deliberately never consulted: a host that is down at Real-Debrid is still a
Real-Debrid route, and transient state must not hand it to a generic provider.

The neutral runtime-state store persists only opaque bytes; the neutral
applicability classifier receives only canonical host claims.
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
from typing import Any, Callable

import re2

from core.logging_utils import sanitize_exception
from integrations.runtime_state import RuntimeStateConflict, RuntimeStateRecord
from transfers.applicability import (
    ApplicabilityReadiness, HostClaim, HostClaimScope, ProviderApplicability, parse_url_applicability,
)

logger = logging.getLogger("realdebrid.hosts")

INTEGRATION_ID = "realdebrid"
HOST_STATE_KEY = "supported-hosts"
HOST_SCHEMA_VERSION = "realdebrid-supported-hosts-v1"
HOST_SOURCE = "rest/1.0/hosts/domains+regex"
HOST_REFRESH_SECONDS = 24 * 60 * 60
HOST_REFRESH_RETRY_SECONDS = 15 * 60

# Provider-controlled applicability data is bounded before it can become durable
# LKG state. RE2 bounds the work of each expression; these bound the snapshot.
_MAX_DOMAINS = 8192
_MAX_PATTERNS = 4096
_MAX_PATTERN_LENGTH = 8192
_MAX_TOTAL_PATTERN_BYTES = 512 * 1024
_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
_MAX_MATCH_URL_LENGTH = 8192
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.ASCII)
# Real-Debrid publishes each validator as a delimited literal, ``/<expression>/<flags>``.
_DELIMITED = re.compile(r"\A/(?P<body>.+)/(?P<flags>[a-z]*)\Z", re.DOTALL)


class RealDebridHostSnapshotError(ValueError):
    """Native or persisted Real-Debrid host data is not safe to use."""


@dataclass(frozen=True)
class RealDebridHostSnapshot:
    domains: tuple[str, ...]
    patterns: tuple[str, ...]
    source: str = HOST_SOURCE

    @property
    def claims(self) -> tuple[HostClaim, ...]:
        return tuple(HostClaim(domain, HostClaimScope.EXACT, frozenset({"http", "https"}))
                     for domain in self.domains)


def _normalize_domain(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RealDebridHostSnapshotError("domain must be a non-empty string")
    raw = value.strip().rstrip(".")
    if "://" in raw or "/" in raw or "@" in raw:
        raise RealDebridHostSnapshotError("domain must contain only a hostname")
    candidate = raw.strip("[]")
    try:
        return ip_address(candidate).compressed.casefold()
    except ValueError:
        pass
    try:
        ascii_host = candidate.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise RealDebridHostSnapshotError("domain is not valid IDNA") from exc
    if len(ascii_host) > 253 or any(not label or not _DNS_LABEL_RE.fullmatch(label)
                                    for label in ascii_host.split(".")):
        raise RealDebridHostSnapshotError("domain is not a valid DNS hostname")
    return ascii_host


def compile_pattern(pattern: str):
    """Compile one native delimited validator with the bounded RE2 engine."""
    match = _DELIMITED.match(pattern)
    if match is None:
        raise RealDebridHostSnapshotError("host regex is not a delimited expression")
    flags = match.group("flags")
    if set(flags) - {"i"}:
        raise RealDebridHostSnapshotError("host regex uses unsupported flags")
    try:
        return re2.compile(("(?i)" if "i" in flags else "") + match.group("body"))
    except Exception as exc:
        raise RealDebridHostSnapshotError("host regex is malformed or uses unsupported unsafe features") from exc


def parse_native_host_snapshot(domains: Any, patterns: Any) -> RealDebridHostSnapshot:
    """Validate one ``/hosts/domains`` and one ``/hosts/regex`` response.

    The whole replacement is bounded and validated before maintenance may
    persist it as LKG; one unusable entry rejects the replacement, so the
    previous good snapshot keeps routing."""
    if not isinstance(domains, list) or not domains:
        raise RealDebridHostSnapshotError("host domains must be a non-empty list")
    if not isinstance(patterns, list) or not patterns:
        raise RealDebridHostSnapshotError("host regexes must be a non-empty list")
    if len(domains) > _MAX_DOMAINS:
        raise RealDebridHostSnapshotError("host inventory has too many domains")
    if len(patterns) > _MAX_PATTERNS:
        raise RealDebridHostSnapshotError("host inventory has too many regexes")
    normalized = tuple(sorted({_normalize_domain(item) for item in domains}))
    total = 0
    validated = []
    for item in patterns:
        if not isinstance(item, str) or not item.strip():
            raise RealDebridHostSnapshotError("host regex must be a non-empty string")
        pattern = item.strip()
        size = len(pattern.encode("utf-8"))
        if size > _MAX_PATTERN_LENGTH:
            raise RealDebridHostSnapshotError("host regex is too long")
        total += size
        if total > _MAX_TOTAL_PATTERN_BYTES:
            raise RealDebridHostSnapshotError("host regex data is too large")
        compile_pattern(pattern)
        validated.append(pattern)
    return RealDebridHostSnapshot(normalized, tuple(validated))


def encode_host_snapshot(snapshot: RealDebridHostSnapshot) -> bytes:
    payload = json.dumps({"source": snapshot.source, "domains": list(snapshot.domains),
                          "patterns": list(snapshot.patterns)},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(payload) > _MAX_SNAPSHOT_BYTES:
        raise RealDebridHostSnapshotError("encoded host snapshot exceeds size limit")
    return payload


def decode_host_snapshot(payload: bytes) -> RealDebridHostSnapshot:
    if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) > _MAX_SNAPSHOT_BYTES:
        raise RealDebridHostSnapshotError("host snapshot payload is unusable")
    try:
        document = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RealDebridHostSnapshotError("host snapshot payload is corrupt") from exc
    if not isinstance(document, dict) or document.get("source") != HOST_SOURCE:
        raise RealDebridHostSnapshotError("host snapshot source is incompatible")
    return parse_native_host_snapshot(document.get("domains"), document.get("patterns"))


class RealDebridRequestApplicability:
    """Evaluate native link validators locally; emit only neutral facts.

    A request is Real-Debrid's when its host is one of Real-Debrid's supported
    domains AND one of Real-Debrid's validators accepts the link -- the
    validators carry path restrictions a host claim cannot. Only the already
    matched request hostname crosses the boundary."""

    def __init__(self, snapshot: RealDebridHostSnapshot | None) -> None:
        self._snapshot = snapshot
        self._compiled = () if snapshot is None else tuple(compile_pattern(item) for item in snapshot.patterns)

    def _facts(self, claims=()) -> ProviderApplicability:
        return ProviderApplicability(
            specialized_hosts=tuple(claims), specialized=True,
            readiness=ApplicabilityReadiness.READY if self._snapshot is not None else ApplicabilityReadiness.UNRESOLVED,
        )

    def __call__(self, request) -> ProviderApplicability:
        if self._snapshot is None:
            return self._facts()
        view = parse_url_applicability(request)
        if view is None or view.scheme not in {"http", "https"}:
            return self._facts()
        raw = request.payload if isinstance(request.payload, str) else ""
        if not raw or len(raw) > _MAX_MATCH_URL_LENGTH:
            return self._facts()
        if not any(view.hostname == domain or view.hostname.endswith("." + domain)
                   for domain in self._snapshot.domains):
            return self._facts()
        if not any(pattern.search(raw) for pattern in self._compiled):
            return self._facts()
        return self._facts((HostClaim(view.hostname, HostClaimScope.EXACT, frozenset({view.scheme})),))


def applicability_facts(snapshot: RealDebridHostSnapshot | None) -> ProviderApplicability:
    return ProviderApplicability(
        specialized_hosts=() if snapshot is None else snapshot.claims, specialized=True,
        readiness=ApplicabilityReadiness.READY if snapshot is not None else ApplicabilityReadiness.UNRESOLVED,
    )


class RealDebridHostMaintenance:
    """Maintenance-only refresh and durable LKG ownership for one registered
    Real-Debrid provider instance. Reaches the application through the generic
    integration lifecycle seam."""

    def __init__(self, provider, store, *, notify: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.time,
                 refresh_seconds: float = HOST_REFRESH_SECONDS,
                 retry_seconds: float = HOST_REFRESH_RETRY_SECONDS) -> None:
        if not (math.isfinite(refresh_seconds) and refresh_seconds > 0
                and math.isfinite(retry_seconds) and retry_seconds > 0):
            raise ValueError("refresh and retry intervals must be positive")
        self._provider = provider
        self._store = store
        self._notify = notify
        self._clock = clock
        self._refresh_seconds = float(refresh_seconds)
        self._retry_seconds = float(retry_seconds)
        self._loaded = False
        self._record: RuntimeStateRecord | None = None
        self._snapshot: RealDebridHostSnapshot | None = None
        self._next_retry_at = 0.0
        self._lock = asyncio.Lock()
        # A freshly built provider is unresolved until its snapshot is loaded.
        self._publish(None, announce=False)

    def _publish(self, snapshot: RealDebridHostSnapshot | None, *, announce: bool = True) -> None:
        provider = self._provider
        previous = getattr(provider, "applicability", None)
        provider.applicability = applicability_facts(snapshot)
        provider.applicability_for = RealDebridRequestApplicability(snapshot)
        if announce and previous != provider.applicability and self._notify is not None:
            self._notify(INTEGRATION_ID)

    async def start(self) -> None:
        """Restore the persisted snapshot only; startup fetches nothing."""
        await self._ensure_loaded()

    async def stop(self) -> None:
        return None

    async def maintain(self) -> None:
        if not self._provider.descriptor.enabled:
            return
        async with self._lock:
            await self._ensure_loaded()
            now = float(self._clock())
            if now < self._next_retry_at or not self._refresh_due(now):
                return
            await self._refresh(now)

    def _refresh_due(self, now: float) -> bool:
        return self._snapshot is None or self._record is None or self._record.is_stale(now=now)

    async def _ensure_loaded(self) -> None:
        if self._loaded or not self._provider.descriptor.enabled:
            return
        self._loaded = True
        try:
            record = await self._store.load(INTEGRATION_ID, HOST_STATE_KEY)
        except Exception as exc:
            logger.warning("Real-Debrid host runtime state could not be loaded: %s", sanitize_exception(exc))
            return
        if record is None or record.schema_version != HOST_SCHEMA_VERSION:
            return
        try:
            snapshot = decode_host_snapshot(record.payload)
        except RealDebridHostSnapshotError as exc:
            logger.warning("Real-Debrid host runtime snapshot is invalid: %s", sanitize_exception(exc))
            return
        self._record, self._snapshot = record, snapshot
        self._publish(snapshot)

    async def _refresh(self, now: float) -> None:
        expected = self._record.generation if self._record is not None else 0
        try:
            # Pacing, timeout and native errors stay inside the provider's own
            # client; maintenance owns cadence, validation and persistence.
            client = self._provider.client
            snapshot = parse_native_host_snapshot(await client.hosts_domains(), await client.hosts_regex())
            record = await self._store.replace(
                INTEGRATION_ID, encode_host_snapshot(snapshot), schema_version=HOST_SCHEMA_VERSION,
                state_key=HOST_STATE_KEY, observed_at=now, successful_at=now,
                stale_after=now + self._refresh_seconds, expected_generation=expected)
        except RuntimeStateConflict:
            # Another writer won; adopt its authoritative generation.
            self._loaded = False
            await self._ensure_loaded()
            return
        except Exception as exc:
            self._next_retry_at = now + self._retry_seconds
            logger.warning("Real-Debrid supported-host refresh failed; retaining last-known-good state: %s",
                           sanitize_exception(exc))
            return
        self._record, self._snapshot, self._next_retry_at = record, snapshot, 0.0
        self._publish(snapshot)
        logger.info("Real-Debrid supported-host snapshot refreshed (%d domains, %d validators)",
                    len(snapshot.domains), len(snapshot.patterns))
