"""Debrid-Link-owned supported-host runtime state and maintenance.

Native Debrid-Link host semantics terminate in this module. ``/downloader/hosts``
(file hosters only) is Debrid-Link's public catalogue: for each hoster its
domains and the link validators (``regexs``) that say which of its URLs it can
generate. It is the same for every account, so it is fetched without the key
and the snapshot carries no account truth.

A request is Debrid-Link's when its host is one of a hoster's domains (or
beneath one) AND -- when that hoster publishes validators -- one of them
accepts the link: a validator carries path restrictions a host claim cannot.
A validator the bounded RE2 engine cannot compile is dropped; a hoster left
with none of the validators it published claims nothing. Debrid-Link's
per-hoster ``status`` (up right now) is deliberately never consulted: a hoster
that is down at Debrid-Link is still a Debrid-Link route, and transient state
must not hand it to a generic provider. Claims are positive inventory only.

Each hoster also keeps Debrid-Link's ``isFree`` -- whether a free account may
use it. It never decides whether a link is Debrid-Link's; the provider's
``entitlement_for`` reads it (``host_free``) to narrow what a free account may
begin. Only a JSON ``true`` is free: a missing or malformed flag is not.

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
from providers.debridlink.client import API_HOST, parse_member_address
from transfers.applicability import (
    ApplicabilityReadiness, HostClaim, HostClaimScope, ProviderApplicability, parse_url_applicability,
)

logger = logging.getLogger("debridlink.hosts")

INTEGRATION_ID = "debridlink"
HOST_STATE_KEY = "supported-hosts"
HOST_SCHEMA_VERSION = "debridlink-supported-hosts-v1"
HOST_SOURCE = "api/v2/downloader/hosts?types=host"
HOST_REFRESH_SECONDS = 24 * 60 * 60
HOST_REFRESH_RETRY_SECONDS = 15 * 60

# Provider-controlled applicability data is bounded before it can become
# durable LKG state. RE2 bounds the work of each expression; these bound the
# snapshot.
_MAX_HOSTERS = 4096
_MAX_DOMAINS = 8192
_MAX_PATTERNS = 8192
_MAX_PATTERN_LENGTH = 8192
_MAX_TOTAL_PATTERN_BYTES = 1024 * 1024
_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
_MAX_MATCH_URL_LENGTH = 8192
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.ASCII)
# Debrid-Link's own address: claimed whatever the catalogue says, because the
# member addresses a torrent decomposes into point at it.
_OWN_CLAIM = HostClaim(API_HOST, HostClaimScope.EXACT, frozenset({"https"}))


class DebridLinkHostSnapshotError(ValueError):
    """Native or persisted Debrid-Link host data is not safe to use."""


@dataclass(frozen=True)
class Hoster:
    domains: tuple[str, ...]
    patterns: tuple[str, ...]
    free: bool = False


@dataclass(frozen=True)
class DebridLinkHostSnapshot:
    hosters: tuple[Hoster, ...]
    source: str = HOST_SOURCE

    @property
    def domains(self) -> tuple[str, ...]:
        return tuple(sorted({domain for hoster in self.hosters for domain in hoster.domains}))

    @property
    def claims(self) -> tuple[HostClaim, ...]:
        return tuple(HostClaim(domain, HostClaimScope.EXACT, frozenset({"http", "https"}))
                     for domain in self.domains)


def _normalize_domain(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().rstrip(".").casefold()
    if "://" in raw or "/" in raw or "@" in raw or ":" in raw:
        return None
    try:
        ip_address(raw)
        return None  # an address is not a hoster's name
    except ValueError:
        pass
    try:
        ascii_host = raw.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if len(ascii_host) > 253 or "." not in ascii_host or any(
            not label or not _DNS_LABEL_RE.fullmatch(label) for label in ascii_host.split(".")):
        return None
    return ascii_host


def compile_pattern(pattern: str):
    """Compile one native validator with the bounded RE2 engine, or ``None``."""
    try:
        return re2.compile(pattern)
    except Exception:
        return None


def parse_native_host_snapshot(hosters: Any) -> DebridLinkHostSnapshot:
    """Validate one ``/downloader/hosts`` answer.

    A record is used when it names at least one valid domain and every
    validator it keeps compiles; a malformed record is skipped rather than
    discarding the whole catalogue (an unusable snapshot would leave
    Debrid-Link an unresolved claimant that holds every HTTP(S) request back),
    and it is never claimed. A catalogue with no usable record at all is
    refused, so the previous good snapshot keeps routing."""
    if not isinstance(hosters, list) or not hosters:
        raise DebridLinkHostSnapshotError("hoster list must be a non-empty list")
    if len(hosters) > _MAX_HOSTERS:
        raise DebridLinkHostSnapshotError("hoster list has too many entries")
    usable, domain_count, pattern_count, pattern_bytes = [], 0, 0, 0
    for record in hosters:
        if not isinstance(record, dict):
            continue
        if record.get("type", "host") != "host":
            continue
        domains = record.get("domains")
        if not isinstance(domains, list):
            continue
        normalized = tuple(sorted({domain for domain in map(_normalize_domain, domains)
                                   if domain is not None and domain != API_HOST}))
        if not normalized:
            continue
        published = record.get("regexs", [])
        if not isinstance(published, list):
            continue
        patterns = []
        for item in published:
            if not isinstance(item, str) or not item.strip() or len(item.encode("utf-8")) > _MAX_PATTERN_LENGTH:
                continue
            if compile_pattern(item.strip()) is not None:
                patterns.append(item.strip())
        if published and not patterns:
            continue
        domain_count += len(normalized)
        pattern_count += len(patterns)
        pattern_bytes += sum(len(item.encode("utf-8")) for item in patterns)
        if domain_count > _MAX_DOMAINS or pattern_count > _MAX_PATTERNS or pattern_bytes > _MAX_TOTAL_PATTERN_BYTES:
            raise DebridLinkHostSnapshotError("host inventory is too large")
        usable.append(Hoster(normalized, tuple(patterns), record.get("isFree") is True))
    if not usable:
        raise DebridLinkHostSnapshotError("hoster list has no usable hoster")
    return DebridLinkHostSnapshot(tuple(sorted(usable, key=lambda hoster: hoster.domains)))


def encode_host_snapshot(snapshot: DebridLinkHostSnapshot) -> bytes:
    payload = json.dumps({"source": snapshot.source,
                          "hosters": [{"domains": list(hoster.domains), "regexs": list(hoster.patterns),
                                       "isFree": hoster.free} for hoster in snapshot.hosters]},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(payload) > _MAX_SNAPSHOT_BYTES:
        raise DebridLinkHostSnapshotError("encoded host snapshot exceeds size limit")
    return payload


def decode_host_snapshot(payload: bytes) -> DebridLinkHostSnapshot:
    if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) > _MAX_SNAPSHOT_BYTES:
        raise DebridLinkHostSnapshotError("host snapshot payload is unusable")
    try:
        document = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DebridLinkHostSnapshotError("host snapshot payload is corrupt") from exc
    if not isinstance(document, dict) or document.get("source") != HOST_SOURCE:
        raise DebridLinkHostSnapshotError("host snapshot source is incompatible")
    hosters = document.get("hosters")
    snapshot = parse_native_host_snapshot(hosters)
    if len(snapshot.hosters) != len(hosters if isinstance(hosters, list) else ()):
        raise DebridLinkHostSnapshotError("host snapshot hosters are corrupt")
    return snapshot


def applicability_facts(snapshot: DebridLinkHostSnapshot | None) -> ProviderApplicability:
    return ProviderApplicability(
        specialized_hosts=(_OWN_CLAIM,) + (() if snapshot is None else snapshot.claims), specialized=True,
        readiness=ApplicabilityReadiness.READY if snapshot is not None else ApplicabilityReadiness.UNRESOLVED,
    )


class DebridLinkRequestApplicability:
    """Evaluate native link validators locally; emit only neutral facts.

    A Debrid-Link member address is Debrid-Link's whatever the catalogue
    holds. Any other HTTP(S) request is Debrid-Link's only when a hoster of
    the catalogue claims both its host and (when it publishes validators) the
    link itself; without a catalogue Debrid-Link is an unresolved specialized
    claimant. Only the already matched hostname crosses the boundary."""

    def __init__(self, snapshot: DebridLinkHostSnapshot | None) -> None:
        self._snapshot = snapshot
        self._hosters = () if snapshot is None else tuple(
            (hoster, tuple(compile_pattern(item) for item in hoster.patterns)) for hoster in snapshot.hosters)

    def _facts(self, claims=(), *, ready: bool | None = None) -> ProviderApplicability:
        resolved = self._snapshot is not None if ready is None else ready
        return ProviderApplicability(
            specialized_hosts=tuple(claims), specialized=True,
            readiness=ApplicabilityReadiness.READY if resolved else ApplicabilityReadiness.UNRESOLVED)

    def _matched(self, request):
        """``(hoster, url view)`` of the catalogue hoster that claims
        ``request``, or ``None``."""
        if self._snapshot is None:
            return None
        view = parse_url_applicability(request)
        if view is None or view.scheme not in {"http", "https"}:
            return None
        raw = request.payload if isinstance(request.payload, str) else ""
        if not raw or len(raw) > _MAX_MATCH_URL_LENGTH:
            return None
        for hoster, patterns in self._hosters:
            if not any(view.hostname == domain or view.hostname.endswith("." + domain) for domain in hoster.domains):
                continue
            if patterns and not any(pattern is not None and pattern.search(raw) for pattern in patterns):
                continue
            return hoster, view
        return None

    def __call__(self, request) -> ProviderApplicability:
        if parse_member_address(getattr(request, "payload", None)) is not None:
            return self._facts((_OWN_CLAIM,), ready=True)
        matched = self._matched(request)
        if matched is None:
            return self._facts()
        _hoster, view = matched
        return self._facts((HostClaim(view.hostname, HostClaimScope.EXACT, frozenset({view.scheme})),))

    def host_free(self, request) -> bool | None:
        """Whether Debrid-Link marks the hoster ``request`` belongs to usable
        by a free account, or ``None`` when no catalogue hoster claims it.
        Structural host truth only: what the account may do with it is
        account entitlement's question (``providers.debridlink.account``)."""
        matched = self._matched(request)
        return matched[0].free if matched is not None else None


class DebridLinkHostMaintenance:
    """Maintenance-only refresh and durable LKG ownership for one registered
    Debrid-Link provider instance. Reaches the application through the generic
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
        self._snapshot: DebridLinkHostSnapshot | None = None
        self._next_retry_at = 0.0
        self._lock = asyncio.Lock()
        # A freshly built provider is unresolved until its snapshot is loaded.
        self._publish(None, announce=False)

    def _publish(self, snapshot: DebridLinkHostSnapshot | None, *, announce: bool = True) -> None:
        provider = self._provider
        previous = getattr(provider, "applicability", None)
        provider.applicability = applicability_facts(snapshot)
        provider.applicability_for = DebridLinkRequestApplicability(snapshot)
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
            logger.warning("Debrid-Link host runtime state could not be loaded: %s", sanitize_exception(exc))
            return
        if record is None or record.schema_version != HOST_SCHEMA_VERSION:
            return
        try:
            snapshot = decode_host_snapshot(record.payload)
        except DebridLinkHostSnapshotError as exc:
            logger.warning("Debrid-Link host runtime snapshot is invalid: %s", sanitize_exception(exc))
            return
        self._record, self._snapshot = record, snapshot
        self._publish(snapshot)

    async def _refresh(self, now: float) -> None:
        expected = self._record.generation if self._record is not None else 0
        try:
            # Timeout and native errors stay inside the provider's own client;
            # maintenance owns cadence, validation and persistence.
            snapshot = parse_native_host_snapshot(await self._provider.client.hosts())
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
            logger.warning("Debrid-Link supported-host refresh failed; retaining last-known-good state: %s",
                           sanitize_exception(exc))
            return
        self._record, self._snapshot, self._next_retry_at = record, snapshot, 0.0
        self._publish(snapshot)
        logger.info("Debrid-Link supported-host snapshot refreshed (%d hosters, %d domains)",
                    len(snapshot.hosters), len(snapshot.domains))
