"""Premiumize-owned supported-host runtime state and maintenance.

Native Premiumize service semantics terminate in this module. ``services/list``
names the hoster services Premiumize can resolve immediately (``directdl``),
acquire into its cloud (``queue``) and look up in its cache (``cache``). A
service entry is a service NAME, not necessarily a hostname: its domains are
the name itself when that is a valid hostname and its ``aliases``; its
``regexpatterns`` add EXACT hosts only, and only from a pattern whose whole text
is the one shape that provably means "every path of this host" (see
``pattern_hosts``). A provider pattern is never executed, never read for
domain-looking text elsewhere (a path, a query), never widened to subdomains,
and one that is not that shape -- a path, port or dynamic host part, anything
else -- is ignored. Only ``directdl`` and ``queue`` services are claimed: a
cache lookup alone executes nothing.

Claims are positive inventory only: arbitrary HTTP(S) stays with the providers
that positively claim it. A failed refresh keeps the last-known-good snapshot,
and without any snapshot Premiumize is an unresolved specialized competitor --
a transient failure is never unsupportedness.

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

from core.logging_utils import sanitize_exception
from integrations.runtime_state import RuntimeStateConflict, RuntimeStateRecord
from providers.premiumize.client import API_HOST, parse_member_address
from transfers.applicability import (
    ApplicabilityReadiness, HostClaim, HostClaimScope, ProviderApplicability, parse_url_applicability,
)

logger = logging.getLogger("premiumize.hosts")

INTEGRATION_ID = "premiumize"
HOST_STATE_KEY = "supported-hosts"
HOST_SCHEMA_VERSION = "premiumize-supported-hosts-v1"
HOST_SOURCE = "api/services/list"
HOST_REFRESH_SECONDS = 24 * 60 * 60
HOST_REFRESH_RETRY_SECONDS = 15 * 60

# Provider-controlled applicability data is bounded before it can become
# durable LKG state.
_MAX_SERVICES = 4096
_MAX_DOMAINS = 8192
_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.ASCII)
# The one pattern shape that reduces to host-only applicability, matched
# against the pattern's TEXT: an optional ``^``, ``https?://`` (slashes escaped
# or not), an optional ``(www\.)?`` / ``(?:www\.)?``, a fully literal host
# (every dot escaped), then ``/.*`` -- any path at all -- and an optional
# ``$``. Nothing else of the pattern may remain, so its host is the URL
# authority and the whole host is supported.
_WHOLE_HOST_PATTERN = re.compile(
    r"\^?https\?:(?:\\/|/)(?:\\/|/)(?P<www>\((?:\?:)?www\\\.\)\?)?"
    r"(?P<host>(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\\\.)+[A-Za-z]{2,63})"
    r"(?:\\/|/)\.\*\$?", re.ASCII)
_MAX_PATTERNS = 64
_MAX_PATTERN_LENGTH = 1024
# Premiumize's own API address: claimed whatever the catalogue says, because
# the member addresses Premiumize results decompose into point at it.
_OWN_CLAIM = HostClaim(API_HOST, HostClaimScope.EXACT, frozenset({"https"}))
_GROUPS = ("directdl", "queue", "cache")


class PremiumizeHostSnapshotError(ValueError):
    """Native or persisted Premiumize service data is not safe to use."""


@dataclass(frozen=True)
class PremiumizeHostSnapshot:
    """Each capability group's DOMAINS (service names and aliases: the host
    and beneath it) and EXACT hosts (reduced from patterns: that host only)."""
    directdl: tuple[str, ...]
    queue: tuple[str, ...]
    cache: tuple[str, ...]
    directdl_hosts: tuple[str, ...] = ()
    queue_hosts: tuple[str, ...] = ()
    cache_hosts: tuple[str, ...] = ()
    source: str = HOST_SOURCE

    @property
    def claimed(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.directdl) | set(self.queue) | set(self.directdl_hosts) | set(self.queue_hosts)))

    def capable(self, group: str, host: str) -> bool:
        return host in getattr(self, f"{group}_hosts") or any(
            host == domain or host.endswith("." + domain) for domain in getattr(self, group))


def _normalize_domain(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().rstrip(".").casefold()
    if "://" in raw or "/" in raw or "@" in raw or ":" in raw:
        return None
    try:
        ip_address(raw)
        return None  # an address is not a service's name
    except ValueError:
        pass
    try:
        ascii_host = raw.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if len(ascii_host) > 253 or "." not in ascii_host or any(
            not label or not _DNS_LABEL_RE.fullmatch(label) for label in ascii_host.split(".")):
        return None
    return ascii_host if ascii_host != API_HOST else None


def pattern_hosts(pattern: Any) -> set[str]:
    """The EXACT hosts a provider pattern supports in whole, by reading its
    text -- never by executing it: the literal host of the one whole-host
    shape (``_WHOLE_HOST_PATTERN``), and its ``www.`` form when that is the
    pattern's optional prefix. Any other pattern names none."""
    if not isinstance(pattern, str) or len(pattern) > _MAX_PATTERN_LENGTH:
        return set()
    shape = _WHOLE_HOST_PATTERN.fullmatch(pattern)
    if shape is None:
        return set()
    host = _normalize_domain(shape.group("host").replace("\\.", "."))
    if host is None:
        return set()
    hosts = {host}
    if shape.group("www"):
        www = _normalize_domain("www." + host)
        if www:
            hosts.add(www)
    return hosts


def _snapshot(groups: dict[str, set[str]], exact: dict[str, set[str]]) -> PremiumizeHostSnapshot:
    if sum(len(values) for values in (*groups.values(), *exact.values())) > _MAX_DOMAINS:
        raise PremiumizeHostSnapshotError("service list has too many domains")
    return PremiumizeHostSnapshot(*(tuple(sorted(groups[group])) for group in _GROUPS),
                                  *(tuple(sorted(exact[group])) for group in _GROUPS))


def parse_native_host_snapshot(native: Any) -> PremiumizeHostSnapshot:
    """Validate one ``services/list`` answer into each group's hosts. A group
    that is not a list, a service none of whose name, aliases or patterns
    yields a valid hostname, is skipped rather than discarding the whole
    catalogue; an answer with no usable ``directdl`` or ``queue`` host at all
    is refused, so the previous good snapshot keeps routing."""
    if not isinstance(native, dict):
        raise PremiumizeHostSnapshotError("service list must be an object")
    aliases = native.get("aliases") if isinstance(native.get("aliases"), dict) else {}
    patterns = native.get("regexpatterns") if isinstance(native.get("regexpatterns"), dict) else {}
    groups: dict[str, set[str]] = {}
    exact: dict[str, set[str]] = {}
    for group in _GROUPS:
        services = native.get(group)
        services = services if isinstance(services, list) else []
        if len(services) > _MAX_SERVICES:
            raise PremiumizeHostSnapshotError("service list has too many services")
        domains, hosts = set(), set()
        for service in services:
            if not isinstance(service, str):
                continue
            named = aliases.get(service) if isinstance(aliases.get(service), list) else []
            domains |= {domain for domain in map(_normalize_domain, [service, *named]) if domain}
            written = patterns.get(service) if isinstance(patterns.get(service), list) else []
            for pattern in written[:_MAX_PATTERNS]:
                hosts |= pattern_hosts(pattern)
        groups[group], exact[group] = domains, hosts
    if not any(groups[group] or exact[group] for group in ("directdl", "queue")):
        raise PremiumizeHostSnapshotError("service list names no usable service")
    return _snapshot(groups, exact)


def encode_host_snapshot(snapshot: PremiumizeHostSnapshot) -> bytes:
    payload = json.dumps({"source": snapshot.source,
                          **{key: list(getattr(snapshot, key)) for group in _GROUPS
                             for key in (group, f"{group}_hosts")}},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(payload) > _MAX_SNAPSHOT_BYTES:
        raise PremiumizeHostSnapshotError("encoded host snapshot exceeds size limit")
    return payload


def decode_host_snapshot(payload: bytes) -> PremiumizeHostSnapshot:
    if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) > _MAX_SNAPSHOT_BYTES:
        raise PremiumizeHostSnapshotError("host snapshot payload is unusable")
    try:
        document = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PremiumizeHostSnapshotError("host snapshot payload is corrupt") from exc
    if not isinstance(document, dict) or document.get("source") != HOST_SOURCE:
        raise PremiumizeHostSnapshotError("host snapshot source is incompatible")
    decoded = {}
    for key in (key for group in _GROUPS for key in (group, f"{group}_hosts")):
        values = document.get(key)
        if not isinstance(values, list):
            raise PremiumizeHostSnapshotError("host snapshot domains are corrupt")
        domains = set(map(_normalize_domain, values))
        # A persisted snapshot was written from validated hosts; anything
        # else is corruption, never something to repair.
        if None in domains or len(domains) != len(values):
            raise PremiumizeHostSnapshotError("host snapshot domains are corrupt")
        decoded[key] = domains
    return _snapshot({group: decoded[group] for group in _GROUPS},
                     {group: decoded[f"{group}_hosts"] for group in _GROUPS})


def applicability_facts(snapshot: PremiumizeHostSnapshot | None) -> ProviderApplicability:
    claims = () if snapshot is None else tuple(
        HostClaim(domain, HostClaimScope.EXACT, frozenset({"http", "https"})) for domain in snapshot.claimed)
    return ProviderApplicability(
        specialized_hosts=(_OWN_CLAIM,) + claims, specialized=True,
        readiness=ApplicabilityReadiness.READY if snapshot is not None else ApplicabilityReadiness.UNRESOLVED,
    )


class PremiumizeRequestApplicability:
    """Emit only neutral facts for one request.

    A Premiumize member address is Premiumize's whatever the catalogue holds.
    Any other HTTP(S) request is Premiumize's only when its host is one of the
    ``directdl`` or ``queue`` services (or beneath one); without a catalogue
    Premiumize is an unresolved specialized claimant. Only the already matched
    hostname crosses the boundary."""

    def __init__(self, snapshot: PremiumizeHostSnapshot | None) -> None:
        self._snapshot = snapshot

    def _facts(self, claims=(), *, ready: bool | None = None) -> ProviderApplicability:
        resolved = self._snapshot is not None if ready is None else ready
        return ProviderApplicability(
            specialized_hosts=tuple(claims), specialized=True,
            readiness=ApplicabilityReadiness.READY if resolved else ApplicabilityReadiness.UNRESOLVED)

    def __call__(self, request) -> ProviderApplicability:
        if parse_member_address(getattr(request, "payload", None)) is not None:
            return self._facts((_OWN_CLAIM,), ready=True)
        if self._snapshot is None:
            return self._facts()
        view = parse_url_applicability(request)
        if view is None or view.scheme not in {"http", "https"}:
            return self._facts()
        if not (self._snapshot.capable("directdl", view.hostname) or self._snapshot.capable("queue", view.hostname)):
            return self._facts()
        return self._facts((HostClaim(view.hostname, HostClaimScope.EXACT, frozenset({view.scheme})),))


class PremiumizeHostMaintenance:
    """Maintenance-only refresh and durable LKG ownership for one registered
    Premiumize provider instance. Reaches the application through the generic
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
        self.snapshot: PremiumizeHostSnapshot | None = None
        self._next_retry_at = 0.0
        self._lock = asyncio.Lock()
        # A freshly built provider is unresolved until its snapshot is loaded.
        self._publish(None, announce=False)

    def _publish(self, snapshot: PremiumizeHostSnapshot | None, *, announce: bool = True) -> None:
        provider = self._provider
        previous = getattr(provider, "applicability", None)
        provider.applicability = applicability_facts(snapshot)
        provider.applicability_for = PremiumizeRequestApplicability(snapshot)
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
        return self.snapshot is None or self._record is None or self._record.is_stale(now=now)

    async def _ensure_loaded(self) -> None:
        if self._loaded or not self._provider.descriptor.enabled:
            return
        self._loaded = True
        try:
            record = await self._store.load(INTEGRATION_ID, HOST_STATE_KEY)
        except Exception as exc:
            logger.warning("Premiumize host runtime state could not be loaded: %s", sanitize_exception(exc))
            return
        if record is None or record.schema_version != HOST_SCHEMA_VERSION:
            return
        try:
            snapshot = decode_host_snapshot(record.payload)
        except PremiumizeHostSnapshotError as exc:
            logger.warning("Premiumize host runtime snapshot is invalid: %s", sanitize_exception(exc))
            return
        self._record, self.snapshot = record, snapshot
        self._publish(snapshot)

    async def _refresh(self, now: float) -> None:
        expected = self._record.generation if self._record is not None else 0
        try:
            # Timeout and native errors stay inside the provider's own client;
            # maintenance owns cadence, validation and persistence.
            snapshot = parse_native_host_snapshot(await self._provider.client.services())
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
            logger.warning("Premiumize supported-host refresh failed; retaining last-known-good state: %s",
                           sanitize_exception(exc))
            return
        self._record, self.snapshot, self._next_retry_at = record, snapshot, 0.0
        self._publish(snapshot)
        logger.info("Premiumize supported-host snapshot refreshed (%d claimed domains)", len(snapshot.claimed))
