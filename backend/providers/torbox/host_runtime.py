"""TorBox-owned supported-host runtime state and maintenance.

Native TorBox host semantics terminate in this module. ``/webdl/hosters`` is
TorBox's public catalogue of the hosts its web downloads support: the same for
every account, so it is fetched without a credential and the snapshot carries
no account truth that could cross from one connection to another. Each
hoster's ``status`` is TorBox's own statement of whether that hoster can be
used on TorBox now -- a JSON boolean -- and only a hoster whose status is
exactly ``true`` contributes claims: DebridPulse never positively claims a
host TorBox itself says is unavailable.

Claims are positive inventory only. The fact that TorBox might accept some
unlisted URL never makes it a claimant: arbitrary HTTP(S) stays with the
providers that positively claim it.

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
from providers.torbox.client import API_HOST, parse_member_address
from transfers.applicability import (
    ApplicabilityReadiness, HostClaim, HostClaimScope, ProviderApplicability, parse_url_applicability,
)

logger = logging.getLogger("torbox.hosts")

INTEGRATION_ID = "torbox"
HOST_STATE_KEY = "supported-hosts"
# v2: the persisted domains are the USABLE hosters only.
HOST_SCHEMA_VERSION = "torbox-supported-hosts-v2"
HOST_SOURCE = "v1/api/webdl/hosters"
HOST_REFRESH_SECONDS = 24 * 60 * 60
HOST_REFRESH_RETRY_SECONDS = 15 * 60

# Provider-controlled applicability data is bounded before it can become
# durable LKG state.
_MAX_HOSTERS = 4096
_MAX_DOMAINS = 8192
_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.ASCII)
# TorBox's own delivery address: claimed whatever the catalogue says, because
# the member addresses TorBox objects decompose into point at it.
_OWN_CLAIM = HostClaim(API_HOST, HostClaimScope.EXACT, frozenset({"https"}))


class TorBoxHostSnapshotError(ValueError):
    """Native or persisted TorBox host data is not safe to use."""


@dataclass(frozen=True)
class TorBoxHostSnapshot:
    domains: tuple[str, ...]
    source: str = HOST_SOURCE

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


def _snapshot(domains: set[str]) -> TorBoxHostSnapshot:
    if len(domains) > _MAX_DOMAINS:
        raise TorBoxHostSnapshotError("hoster list has too many domains")
    return TorBoxHostSnapshot(tuple(sorted(domains)))


def parse_native_host_snapshot(hosters: Any) -> TorBoxHostSnapshot:
    """Validate one ``/webdl/hosters`` answer into the domains of the hosters
    TorBox can use NOW.

    A record is well formed when it names at least one valid domain and its
    ``status`` is a JSON boolean. TorBox's catalogue is long and edited often,
    so a malformed record -- including one whose status is missing or not a
    boolean -- is skipped rather than discarding the whole catalogue (an
    unusable snapshot would leave TorBox an unresolved claimant that holds
    every HTTP(S) request back), and it is never claimed: an unreadable status
    is not a usable one. A well-formed record whose status is ``false`` is
    valid data that claims nothing, so a catalogue in which every hoster is
    currently unavailable is still a resolved snapshot with no hoster claims.
    A catalogue with no well-formed record at all is refused, so the previous
    good snapshot keeps routing."""
    if not isinstance(hosters, list) or not hosters:
        raise TorBoxHostSnapshotError("hoster list must be a non-empty list")
    if len(hosters) > _MAX_HOSTERS:
        raise TorBoxHostSnapshotError("hoster list has too many entries")
    usable, well_formed = set(), False
    for record in hosters:
        if (not isinstance(record, dict) or not isinstance(record.get("domains"), list)
                or not isinstance(record.get("status"), bool)):
            continue
        domains = {domain for domain in map(_normalize_domain, record["domains"])
                   if domain and domain != API_HOST}
        if not domains:
            continue
        well_formed = True
        if record["status"] is True:
            usable |= domains
    if not well_formed:
        raise TorBoxHostSnapshotError("hoster list holds no well-formed hoster")
    return _snapshot(usable)


def encode_host_snapshot(snapshot: TorBoxHostSnapshot) -> bytes:
    payload = json.dumps({"source": snapshot.source, "domains": list(snapshot.domains)},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(payload) > _MAX_SNAPSHOT_BYTES:
        raise TorBoxHostSnapshotError("encoded host snapshot exceeds size limit")
    return payload


def decode_host_snapshot(payload: bytes) -> TorBoxHostSnapshot:
    if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) > _MAX_SNAPSHOT_BYTES:
        raise TorBoxHostSnapshotError("host snapshot payload is unusable")
    try:
        document = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TorBoxHostSnapshotError("host snapshot payload is corrupt") from exc
    if not isinstance(document, dict) or document.get("source") != HOST_SOURCE:
        raise TorBoxHostSnapshotError("host snapshot source is incompatible")
    values = document.get("domains")
    if not isinstance(values, list):
        raise TorBoxHostSnapshotError("host snapshot domains are corrupt")
    domains = set(map(_normalize_domain, values))
    # A persisted snapshot was written from validated domains; anything else
    # is corruption, never something to repair.
    if None in domains or API_HOST in domains or len(domains) != len(values):
        raise TorBoxHostSnapshotError("host snapshot domains are corrupt")
    return _snapshot(domains)


def applicability_facts(snapshot: TorBoxHostSnapshot | None) -> ProviderApplicability:
    return ProviderApplicability(
        specialized_hosts=(_OWN_CLAIM,) + (() if snapshot is None else snapshot.claims), specialized=True,
        readiness=ApplicabilityReadiness.READY if snapshot is not None else ApplicabilityReadiness.UNRESOLVED,
    )


class TorBoxRequestApplicability:
    """Emit only neutral facts for one request.

    A TorBox member address is TorBox's whatever the catalogue holds. Any other
    HTTP(S) request is TorBox's only when its host is one of the catalogue's
    domains (or beneath one); without a catalogue TorBox is an unresolved
    specialized claimant. Only the already matched hostname crosses the
    boundary."""

    def __init__(self, snapshot: TorBoxHostSnapshot | None) -> None:
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
        if not any(view.hostname == domain or view.hostname.endswith("." + domain)
                   for domain in self._snapshot.domains):
            return self._facts()
        return self._facts((HostClaim(view.hostname, HostClaimScope.EXACT, frozenset({view.scheme})),))


class TorBoxHostMaintenance:
    """Maintenance-only refresh and durable LKG ownership for one registered
    TorBox provider instance. Reaches the application through the generic
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
        self._snapshot: TorBoxHostSnapshot | None = None
        self._next_retry_at = 0.0
        self._lock = asyncio.Lock()
        # A freshly built provider is unresolved until its snapshot is loaded.
        self._publish(None, announce=False)

    def _publish(self, snapshot: TorBoxHostSnapshot | None, *, announce: bool = True) -> None:
        provider = self._provider
        previous = getattr(provider, "applicability", None)
        provider.applicability = applicability_facts(snapshot)
        provider.applicability_for = TorBoxRequestApplicability(snapshot)
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
            logger.warning("TorBox host runtime state could not be loaded: %s", sanitize_exception(exc))
            return
        if record is None or record.schema_version != HOST_SCHEMA_VERSION:
            return
        try:
            snapshot = decode_host_snapshot(record.payload)
        except TorBoxHostSnapshotError as exc:
            logger.warning("TorBox host runtime snapshot is invalid: %s", sanitize_exception(exc))
            return
        self._record, self._snapshot = record, snapshot
        self._publish(snapshot)

    async def _refresh(self, now: float) -> None:
        expected = self._record.generation if self._record is not None else 0
        try:
            # Pacing, timeout and native errors stay inside the provider's own
            # client; maintenance owns cadence, validation and persistence.
            snapshot = parse_native_host_snapshot(await self._provider.client.hosters())
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
            logger.warning("TorBox supported-host refresh failed; retaining last-known-good state: %s",
                           sanitize_exception(exc))
            return
        self._record, self._snapshot, self._next_retry_at = record, snapshot, 0.0
        self._publish(snapshot)
        logger.info("TorBox supported-host snapshot refreshed (%d usable domains)", len(snapshot.domains))
