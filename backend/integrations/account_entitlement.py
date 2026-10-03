"""Neutral durable account-entitlement truth for account-backed providers.

One owner per provider instance keeps the provider's ``entitlements``
(``transfers.entitlement.ProviderEntitlements``) current from account truth,
independent of any browser status poll:

* it restores the last-known-good account facts persisted under the current
  credential's scope (``integrations.runtime_state.ScopedRuntimeStateStore``),
  so another credential or account never inherits them, and startup performs
  no fetch;
* its maintenance refreshes them on its own cadence, and a status probe or
  connection proof that already fetched them hands them over (``observe``),
  so there is one account truth, never a second cache;
* a failed refresh keeps the last-known-good facts -- but entitlement is
  DERIVED from them at the instant it is read, so a known expiry is binding:
  nothing premium survives its authoritative end because a refresh failed;
* an explicit operator act (a Test of the saved account, re-enabling the
  integration) fetches account truth NOW (``refresh_now``): passive freshness
  and background retry backoff never suppress it, and it changes nothing
  about enablement or credentials;
* a provider-proven definitive refusal contracts exactly the refused request
  classes for this account (``contract``) until account truth changes;
* every change of the derived truth wakes routing and the status presentation
  through the existing neutral seams.

Native account schemas, plan names and refusal codes stay in the provider's
``AccountTranslation``; this module stores opaque, provider-validated facts and
knows none of them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any, Awaitable, Callable, Mapping, Protocol

from core.logging_utils import sanitize_exception
from integrations.runtime_state import RuntimeStateConflict
from transfers.entitlement import CONNECTION_FAILED_ENTITLEMENTS, UNRESOLVED_ENTITLEMENTS, ProviderEntitlements

logger = logging.getLogger("integrations.account")

STATE_KEY = "account"
# Background freshness: account truth is a low-frequency control-plane fact,
# so an external plan change reaches routing within about five minutes. A known
# expiry never waits for this -- entitlement is derived at the instant it is read.
ACCOUNT_REFRESH_SECONDS = 5 * 60
ACCOUNT_RETRY_SECONDS = 5 * 60
_MAX_PAYLOAD_BYTES = 64 * 1024



class AccountTranslation(Protocol):
    """Provider-owned account semantics. ``facts`` validates a native account
    answer (or previously persisted facts) into a small JSON-safe mapping and
    raises ``ValueError`` for anything malformed; ``derive`` turns facts into
    neutral entitlement truth at ``now`` for the request classes ``offered``,
    with ``contracted`` classes removed."""

    schema_version: str

    def configured(self) -> bool: ...
    async def fetch(self) -> Any: ...
    def facts(self, native: Any) -> Mapping[str, Any]: ...
    def connection_failed(self, exc: BaseException) -> bool: ...
    def derive(self, facts: Mapping[str, Any], *, offered: frozenset[str], contracted: frozenset[str],
               now: float) -> ProviderEntitlements: ...


class AccountEntitlementMaintenance:
    """The one account-entitlement owner of one registered provider instance;
    reaches the application through the generic integration lifecycle seam."""

    def __init__(self, provider, translation: AccountTranslation, store, *, integration_id: str,
                 notify: Callable[[str], None] | None = None,
                 notify_status: Callable[[], Awaitable[None]] | None = None,
                 clock: Callable[[], float] = time.time,
                 refresh_seconds: float = ACCOUNT_REFRESH_SECONDS,
                 retry_seconds: float = ACCOUNT_RETRY_SECONDS) -> None:
        if not (math.isfinite(refresh_seconds) and refresh_seconds > 0
                and math.isfinite(retry_seconds) and retry_seconds > 0):
            raise ValueError("refresh and retry intervals must be positive")
        self._provider = provider
        self._translation = translation
        self._store = store
        self._integration_id = str(integration_id)
        self._notify = notify
        self._notify_status = notify_status
        self._clock = clock
        self._refresh_seconds = float(refresh_seconds)
        self._retry_seconds = float(retry_seconds)
        self._facts: dict | None = None
        self._contracted: frozenset[str] = frozenset()
        self._connection_failed = False
        self._generation = 0
        self._stale_after = 0.0
        self._next_retry_at = 0.0
        self._loaded = False
        self._published: ProviderEntitlements | None = None
        self._lock = asyncio.Lock()

    # -- the neutral truth ---------------------------------------------------------

    @property
    def entitlements(self) -> ProviderEntitlements:
        """Current truth, derived NOW from the last-known-good facts."""
        if self._facts is None:
            if self._connection_failed or not self._translation.configured():
                # The connection itself is failing (no credential, or the
                # provider refused it): a health question, never entitlement.
                return CONNECTION_FAILED_ENTITLEMENTS
            return UNRESOLVED_ENTITLEMENTS
        return self._translation.derive(
            self._facts, offered=frozenset(self._provider.descriptor.request_types),
            contracted=self._contracted, now=float(self._clock()))

    def connection_unusable(self) -> bool:
        """Whether account truth is absent because the connection is failing."""
        return self._facts is None and (self._connection_failed or not self._translation.configured())

    async def _announce(self) -> None:
        current = self.entitlements
        if current == self._published:
            return
        self._published = current
        if self._notify is not None:
            self._notify(self._integration_id)
        if self._notify_status is not None:
            try:
                await self._notify_status()
            except Exception as exc:  # presentation must never break routing truth
                logger.debug("account status invalidation failed: %s", sanitize_exception(exc))

    # -- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        """Restore the scoped last-known-good facts only; startup fetches nothing."""
        await self._ensure_loaded()

    async def stop(self) -> None:
        return None

    async def maintain(self) -> None:
        if not self._provider.descriptor.enabled:
            return
        async with self._lock:
            await self._ensure_loaded()
            now = float(self._clock())
            if self._translation.configured() and now >= self._next_retry_at and (
                    self._facts is None or now >= self._stale_after):
                await self._refresh(now)
        # A known expiry passing is a change of derived truth with no fetch at
        # all: this cadence is what tells routing and presentation about it.
        await self._announce()

    async def refresh_now(self) -> ProviderEntitlements:
        """Fetch authoritative account truth now -- the explicit operator mode.

        Bypasses passive freshness (a still-fresh last-known-good) and the
        background retry delay, because the operator asked DebridPulse to check
        now; works while the integration is administratively disabled, because
        reading account truth is proof, not participation. Persistence,
        derivation, contraction and announcement are exactly the ordinary
        refresh's. A failure keeps the last-known-good facts and schedules the
        ordinary background retry -- it never becomes a tight retry loop."""
        async with self._lock:
            await self._ensure_loaded(explicit=True)
            if self._translation.configured():
                await self._refresh(float(self._clock()))
        await self._announce()
        return self.entitlements

    async def observe(self, native: Any) -> ProviderEntitlements:
        """Adopt account facts another path already fetched (a status probe).
        Malformed facts change nothing."""
        async with self._lock:
            await self._ensure_loaded()
            now = float(self._clock())
            try:
                facts = dict(self._translation.facts(native))
            except (TypeError, ValueError) as exc:
                logger.warning("%s account facts are invalid: %s", self._integration_id, sanitize_exception(exc))
            else:
                await self._persist(facts, now)
        await self._announce()
        return self.entitlements

    async def contract(self, request_types) -> None:
        """Record a provider-proven definitive refusal: this account may not
        begin these request classes. Scoped to this account, durable, and
        lifted only by a change of its account truth."""
        kinds = frozenset(str(item) for item in request_types)
        async with self._lock:
            await self._ensure_loaded()
            if not kinds or kinds <= self._contracted:
                return
            self._contracted = self._contracted | kinds
            if self._facts is not None:
                await self._write(self._facts, float(self._clock()))
        logger.info("%s account refused %s; not offered for new work until account truth changes",
                    self._integration_id, ",".join(sorted(kinds)))
        await self._announce()

    # -- persistence -----------------------------------------------------------------

    async def _ensure_loaded(self, *, explicit: bool = False) -> None:
        # Passive paths never read state for an integration that does not
        # participate; an explicit operator refresh may.
        if self._loaded or not (explicit or self._provider.descriptor.enabled):
            return
        self._loaded = True
        try:
            record = await self._store.load(self._integration_id, STATE_KEY)
        except Exception as exc:
            logger.warning("%s account state could not be loaded: %s", self._integration_id, sanitize_exception(exc))
            return
        if record is None or record.schema_version != self._translation.schema_version:
            return
        try:
            if len(record.payload) > _MAX_PAYLOAD_BYTES:
                raise ValueError("account state is too large")
            envelope = json.loads(record.payload.decode("utf-8"))
            if not isinstance(envelope, dict) or set(envelope) != {"facts", "contracted"}:
                raise ValueError("account state envelope is malformed")
            contracted = envelope["contracted"]
            if not isinstance(contracted, list) or not all(isinstance(item, str) for item in contracted):
                raise ValueError("account state contraction is malformed")
            facts = dict(self._translation.facts(envelope["facts"]))
        except (UnicodeDecodeError, TypeError, ValueError) as exc:
            logger.warning("%s account state is invalid: %s", self._integration_id, sanitize_exception(exc))
            return
        self._facts, self._contracted = facts, frozenset(contracted)
        self._generation, self._stale_after = record.generation, float(record.stale_after or 0.0)

    async def _refresh(self, now: float) -> None:
        try:
            facts = dict(self._translation.facts(await self._translation.fetch()))
        except Exception as exc:
            self._next_retry_at = now + self._retry_seconds
            self._connection_failed = self._translation.connection_failed(exc)
            logger.warning("%s account refresh failed; retaining last-known-good state: %s",
                           self._integration_id, sanitize_exception(exc))
            return
        await self._persist(facts, now)

    async def _persist(self, facts: dict, now: float) -> None:
        self._connection_failed = False
        self._next_retry_at = 0.0
        if facts != self._facts:
            # Account truth changed: a refusal proven under the old truth no
            # longer binds -- capability may expand again.
            self._contracted = frozenset()
        self._facts = facts
        await self._write(facts, now)

    async def _write(self, facts: dict, now: float) -> None:
        payload = json.dumps({"facts": facts, "contracted": sorted(self._contracted)},
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
        try:
            record = await self._store.replace(
                self._integration_id, payload, schema_version=self._translation.schema_version,
                state_key=STATE_KEY, observed_at=now, successful_at=now,
                stale_after=now + self._refresh_seconds, expected_generation=self._generation)
        except RuntimeStateConflict:
            # Another writer of this scope won: its truth is as current as ours.
            self._loaded = False
            await self._ensure_loaded()
            return
        except Exception as exc:
            # In-memory truth still routes; durability is retried next refresh.
            self._stale_after = 0.0
            logger.warning("%s account state could not be persisted: %s",
                           self._integration_id, sanitize_exception(exc))
            return
        self._generation, self._stale_after = record.generation, float(record.stale_after or 0.0)
