from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable


@dataclass(slots=True)
class _FailureState:
    failures: int
    last_failure: float


class FailureThrottle:
    """Bounded in-memory increasing-delay throttle with automatic recovery."""

    def __init__(
        self,
        *,
        max_entries: int = 512,
        reset_after_seconds: float = 300.0,
        free_failures: int = 2,
        base_delay_seconds: float = 0.25,
        max_delay_seconds: float = 2.0,
    ):
        self.max_entries = max(1, int(max_entries))
        self.reset_after_seconds = max(1.0, float(reset_after_seconds))
        self.free_failures = max(0, int(free_failures))
        self.base_delay_seconds = max(0.0, float(base_delay_seconds))
        self.max_delay_seconds = max(self.base_delay_seconds, float(max_delay_seconds))
        self._entries: OrderedDict[str, _FailureState] = OrderedDict()

    def delay_for(self, key: str) -> float:
        now = time.monotonic()
        state = self._fresh_state(str(key or "unknown"), now)
        if state is None or state.failures <= self.free_failures:
            return 0.0
        exponent = state.failures - self.free_failures - 1
        return min(self.max_delay_seconds, self.base_delay_seconds * (2 ** exponent))

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        normalized = str(key or "unknown")
        state = self._fresh_state(normalized, now)
        failures = 1 if state is None else state.failures + 1
        self._entries[normalized] = _FailureState(failures=failures, last_failure=now)
        self._entries.move_to_end(normalized)
        self._prune(now)

    def record_success(self, key: str) -> None:
        self._entries.pop(str(key or "unknown"), None)

    def clear(self) -> None:
        self._entries.clear()

    def _fresh_state(self, key: str, now: float) -> _FailureState | None:
        state = self._entries.get(key)
        if state is None:
            return None
        if state.last_failure + self.reset_after_seconds <= now:
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        return state

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, state in self._entries.items()
            if state.last_failure + self.reset_after_seconds <= now
        ]
        for key in expired:
            self._entries.pop(key, None)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    @property
    def size(self) -> int:
        self._prune(time.monotonic())
        return len(self._entries)


class DualWindowRateLimiter:
    """Rolling-window limiter with both per-peer and process-wide budgets.

    Public login/OIDC-start routes must remain unauthenticated, but they allocate
    bounded server-side state and OIDC start also performs outbound discovery.
    A process-wide budget prevents a rotating-source flood from exhausting those
    stores while the per-peer budget stops one transport peer from monopolizing
    the global allowance.
    """

    def __init__(
        self,
        *,
        per_peer_limit: int,
        global_limit: int,
        window_seconds: float = 60.0,
        max_peers: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_peer_limit = max(1, int(per_peer_limit))
        self.global_limit = max(self.per_peer_limit, int(global_limit))
        self.window_seconds = max(1.0, float(window_seconds))
        self.max_peers = max(1, int(max_peers))
        self._clock = clock
        self._global: deque[float] = deque()
        self._peers: OrderedDict[str, deque[float]] = OrderedDict()

    @staticmethod
    def _trim(values: deque[float], cutoff: float) -> None:
        while values and values[0] <= cutoff:
            values.popleft()

    def allow(self, peer: str) -> bool:
        now = self._clock()
        cutoff = now - self.window_seconds
        self._trim(self._global, cutoff)

        key = str(peer or "unknown")
        peer_values = self._peers.get(key)
        if peer_values is None:
            peer_values = deque()
            self._peers[key] = peer_values
        else:
            self._trim(peer_values, cutoff)
            self._peers.move_to_end(key)

        if len(self._global) >= self.global_limit or len(peer_values) >= self.per_peer_limit:
            if not peer_values:
                self._peers.pop(key, None)
            return False

        self._global.append(now)
        peer_values.append(now)
        self._peers.move_to_end(key)
        self._prune_peers(cutoff)
        return True

    def _prune_peers(self, cutoff: float) -> None:
        empty = []
        for key, values in self._peers.items():
            self._trim(values, cutoff)
            if not values:
                empty.append(key)
        for key in empty:
            self._peers.pop(key, None)
        while len(self._peers) > self.max_peers:
            self._peers.popitem(last=False)

    def clear(self) -> None:
        self._global.clear()
        self._peers.clear()


password_failure_throttle = FailureThrottle()
# Login CSRF state lives for ten minutes with a 512-entry cap. At 40 starts per
# rolling minute globally, application-originated challenge issuance stays below
# capacity even before expired-state cleanup runs.
login_challenge_rate_limiter = DualWindowRateLimiter(
    per_peer_limit=20,
    global_limit=40,
)
# OIDC transactions live for ten minutes and are capped at 128 entries. Normal
# public starts and authenticated config-verification starts use independent
# budgets so a public flood cannot consume the operator's verification quota;
# their combined ten-minute maxima remain below transaction-store capacity.
oidc_start_rate_limiter = DualWindowRateLimiter(
    per_peer_limit=6,
    global_limit=8,
)
# Pending verified configurations have their own 32-entry store. Three starts
# per rolling minute keeps that ten-minute store below capacity too.
oidc_verify_rate_limiter = DualWindowRateLimiter(
    per_peer_limit=3,
    global_limit=3,
)
# A User's own notification Test and test report reach an outbound Discord
# webhook. Keyed by the durable account ID: a few per account per minute,
# with a process-wide ceiling so many accounts together cannot flood either.
personal_notification_test_rate_limiter = DualWindowRateLimiter(
    per_peer_limit=3,
    global_limit=30,
)
