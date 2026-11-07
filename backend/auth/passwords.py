from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import time
from collections import OrderedDict
from dataclasses import dataclass

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type


_PASSWORD_HASHER = PasswordHasher()
# Valid Argon2id verifier used only to equalize verification work when the
# supplied username is wrong. The source text is deliberately non-secret; the
# result of this verification is never accepted for authentication.
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$LXpi21Jmn0Nsy9XXdJ5h2Q$"
    "tfoUhl3T7DNb5V/ogeM/1EEljjAfw61Bvtwn26tLbZI"
)
# The default Argon2id parameters use substantial memory by design. Bound
# simultaneous unauthenticated verification work rather than letting the default
# worker pool multiply that memory cost under attack.
_PASSWORD_VERIFY_SLOTS = asyncio.Semaphore(2)


def hash_password(password: str) -> str:
    value = str(password or "")
    if not value:
        raise ValueError("password must not be empty")
    return _PASSWORD_HASHER.hash(value)


def verify_password(password_hash: str, password: str) -> bool:
    encoded = str(password_hash or "").strip()
    if not encoded:
        return False
    try:
        return bool(_PASSWORD_HASHER.verify(encoded, str(password or "")))
    except (InvalidHashError, VerificationError):
        return False


def verify_password_candidate(
    password_hash: str,
    password: str,
    *,
    use_configured_hash: bool,
) -> bool:
    """Verify a password while doing Argon2 work even for a wrong username."""
    target = str(password_hash or "").strip() if use_configured_hash else _DUMMY_PASSWORD_HASH
    verified = verify_password(target, password)
    return bool(use_configured_hash and verified)


async def verify_password_candidate_async(
    password_hash: str,
    password: str,
    *,
    use_configured_hash: bool,
) -> bool:
    """Run one real-or-dummy Argon2 verification off the request event loop."""
    await _PASSWORD_VERIFY_SLOTS.acquire()
    try:
        worker = asyncio.create_task(
            asyncio.to_thread(
                verify_password_candidate,
                password_hash,
                password,
                use_configured_hash=use_configured_hash,
            )
        )
    except BaseException:
        _PASSWORD_VERIFY_SLOTS.release()
        raise

    # asyncio.to_thread cannot cancel the underlying thread. Release the slot
    # only when the worker itself finishes, never when its request task is
    # cancelled or disconnected. The worker task is shielded so caller
    # cancellation cannot mark it cancelled before the thread returns.
    worker.add_done_callback(lambda _task: _PASSWORD_VERIFY_SLOTS.release())
    return await asyncio.shield(worker)


async def hash_password_async(password: str, *, finished=None) -> str:
    """Generate one Argon2 verifier off the request event loop, in the same
    bounded worker slots as verification (``_PASSWORD_VERIFY_SLOTS``).

    As there, the thread cannot be cancelled, so its slot is released only
    when the worker itself finishes -- never when the caller is cancelled or
    disconnects. ``finished`` (optional) is called exactly once at that same
    moment, or at once if no worker was started, so a caller can hold its own
    admission for exactly as long as the work it admitted."""
    try:
        await _PASSWORD_VERIFY_SLOTS.acquire()
    except BaseException:
        if finished is not None:
            finished()
        raise
    try:
        worker = asyncio.create_task(asyncio.to_thread(hash_password, password))
    except BaseException:
        _PASSWORD_VERIFY_SLOTS.release()
        if finished is not None:
            finished()
        raise

    def complete(_task) -> None:
        _PASSWORD_VERIFY_SLOTS.release()
        if finished is not None:
            finished()

    worker.add_done_callback(complete)
    return await asyncio.shield(worker)


def password_credential_version(password_hash: str) -> str:
    """Stable non-secret version marker used to invalidate password sessions."""
    encoded = str(password_hash or "").strip().encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(encoded).hexdigest() if encoded else ""


def is_usable_password_hash(password_hash: str) -> bool:
    """True only for a parseable Argon2id verifier."""
    encoded = str(password_hash or "").strip()
    if not encoded:
        return False
    try:
        return extract_parameters(encoded).type is Type.ID
    except InvalidHashError:
        return False


def password_needs_rehash(password_hash: str) -> bool:
    encoded = str(password_hash or "").strip()
    if not encoded:
        return False
    try:
        return bool(_PASSWORD_HASHER.check_needs_rehash(encoded))
    except InvalidHashError:
        return False


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float


class BasicVerificationCache:
    """Bounded, short-lived cache of successful HTTP Basic verifications.

    Cache keys are HMAC-SHA256 fingerprints under a process-random key. Raw
    credentials are never retained. The configured password hash participates in
    the fingerprint so a credential replacement cannot reuse an old cache entry.
    """

    def __init__(self, *, max_entries: int = 256, ttl_seconds: float = 30.0):
        self.max_entries = max(1, int(max_entries))
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self._key = os.urandom(32)
        self._entries: OrderedDict[bytes, _CacheEntry] = OrderedDict()

    def _fingerprint(self, username: str, password: str, password_hash: str) -> bytes:
        message = b"\0".join(
            (
                str(username or "").encode("utf-8", errors="surrogatepass"),
                str(password or "").encode("utf-8", errors="surrogatepass"),
                str(password_hash or "").encode("utf-8", errors="surrogatepass"),
            )
        )
        return hmac.new(self._key, message, hashlib.sha256).digest()

    def contains(self, username: str, password: str, password_hash: str) -> bool:
        now = time.monotonic()
        fingerprint = self._fingerprint(username, password, password_hash)
        entry = self._entries.get(fingerprint)
        if entry is None:
            return False
        if entry.expires_at <= now:
            self._entries.pop(fingerprint, None)
            return False
        self._entries.move_to_end(fingerprint)
        return True

    def remember(self, username: str, password: str, password_hash: str) -> None:
        now = time.monotonic()
        fingerprint = self._fingerprint(username, password, password_hash)
        self._entries[fingerprint] = _CacheEntry(expires_at=now + self.ttl_seconds)
        self._entries.move_to_end(fingerprint)
        self._prune(now)

    def clear(self) -> None:
        self._entries.clear()

    def _prune(self, now: float) -> None:
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    @property
    def size(self) -> int:
        self._prune(time.monotonic())
        return len(self._entries)


basic_verification_cache = BasicVerificationCache()
