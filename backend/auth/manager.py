from __future__ import annotations

import asyncio
import secrets
import threading

from fastapi import Request

from auth.accounts import get_account, local_credential
from auth.passwords import basic_verification_cache, verify_password_candidate_async
from auth.policy import password_auth_enabled
from auth.throttle import password_failure_throttle
from core.config import get_settings


class PasswordAuthenticationBusy(Exception):
    """Password verification admission is saturated; fail fast instead of queueing."""


class PasswordAttemptAdmission:
    """Bound expensive password attempts globally and per transport peer.

    Argon2 verification is intentionally expensive. The lower-level verifier
    limits simultaneous Argon2 workers, while this admission boundary also caps
    requests waiting through throttling or for a worker slot. Without it, a
    parallel unauthenticated burst could create an unbounded coroutine queue.
    """

    def __init__(self, *, max_pending: int = 16, max_per_peer: int = 4) -> None:
        self.max_pending = max(1, int(max_pending))
        self.max_per_peer = max(1, min(int(max_per_peer), self.max_pending))
        self._lock = threading.Lock()
        self._pending = 0
        self._peers: dict[str, int] = {}

    def acquire(self, peer: str) -> bool:
        key = str(peer or "unknown")
        with self._lock:
            peer_pending = self._peers.get(key, 0)
            if self._pending >= self.max_pending or peer_pending >= self.max_per_peer:
                return False
            self._pending += 1
            self._peers[key] = peer_pending + 1
            return True

    def release(self, peer: str) -> None:
        key = str(peer or "unknown")
        with self._lock:
            peer_pending = self._peers.get(key, 0)
            if peer_pending <= 0:
                return
            self._pending = max(0, self._pending - 1)
            if peer_pending == 1:
                self._peers.pop(key, None)
            else:
                self._peers[key] = peer_pending - 1

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending


password_attempt_admission = PasswordAttemptAdmission()


def peer_key(request: Request) -> str:
    """Use the transport peer, not untrusted forwarding headers, for throttling."""
    client = request.client
    return str(client.host if client else "unknown")


async def password_proof_current(proven, settings):
    """The account a completed password proof still signs in as, or ``None``.

    Argon2 verification runs outside the authentication configuration lock;
    under it, the proof is re-checked against live state: Username & Password
    is still enabled and the account still holds exactly the credential that
    was verified. The caller holds the lock."""
    if proven is None or not password_auth_enabled(settings):
        return None
    current = await get_account(proven.id)
    if current is None or not current.active or current.username != proven.username:
        return None
    if not (current.credential_version and secrets.compare_digest(current.credential_version, proven.credential_version)):
        return None
    return current


async def verify_local_credentials(
    request: Request,
    provided_user: str,
    provided_password: str,
    *,
    allow_basic_success_cache: bool = False,
    settings=None,
):
    """Verify a local credential with shared throttling/timing behavior.

    Returns the active account whose credential matched, or ``None``. An
    unknown or disabled username costs the same Argon2 work as a wrong
    password and is indistinguishable from it."""
    cfg = settings if settings is not None else get_settings()
    if not password_auth_enabled(cfg):
        return None

    peer = peer_key(request)
    candidate_user = str(provided_user or "")
    candidate_password = str(provided_password or "")
    # A Basic client presenting exactly the credential already proven against
    # this stored verifier needs no Argon2 work, so it takes no admission slot:
    # peers whose attempts fail cannot crowd it out. The stored verifier is
    # read for that only while the cache holds a proven credential, and that
    # read serves the cache hit alone; anything else -- a wrong password
    # included -- is admitted and throttled first, and reads the credential
    # afresh there.
    if allow_basic_success_cache and candidate_user and basic_verification_cache.size:
        cached = await local_credential(candidate_user)
        if cached is not None and basic_verification_cache.contains(cached[0].username, candidate_password, cached[1]):
            password_failure_throttle.record_success(peer, candidate_user)
            return cached[0]

    if not password_attempt_admission.acquire(peer):
        raise PasswordAuthenticationBusy
    try:
        delay = password_failure_throttle.delay_for(peer)
        if delay:
            await asyncio.sleep(delay)

        found = await local_credential(candidate_user) if candidate_user else None
        account, password_hash = found if found is not None else (None, "")

        verified = await verify_password_candidate_async(
            password_hash,
            candidate_password,
            use_configured_hash=account is not None,
        )
        if verified and allow_basic_success_cache:
            basic_verification_cache.remember(
                account.username,
                candidate_password,
                password_hash,
            )

        if verified:
            password_failure_throttle.record_success(peer, candidate_user)
            return account

        password_failure_throttle.record_failure(peer, candidate_user)
        return None
    finally:
        password_attempt_admission.release(peer)
