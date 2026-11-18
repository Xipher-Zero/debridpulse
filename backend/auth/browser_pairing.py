"""Browser pairing: how a browser extension obtains its own browser token.

A device-authorization flow (after RFC 8628), held in this process's memory
only (a restart cancels a pending pairing; the extension simply connects
again):

1. The extension starts a pairing (no credential) and receives a secret
   ``device_code`` and a short ``user_code``.
2. It opens ``/?connect-browser=<user_code>``. A signed-in person who may
   connect browsers (``auth.accounts.BROWSER_ROLES``) sees the requesting
   client's name and when it asked, and approves or denies.
3. Approval issues the browser token for THAT person's account at once
   (``auth.accounts.issue_browser_token``); the extension, polling with its
   device code, receives the raw token exactly once.

The user code only identifies a pairing to the approving person; the token
goes only to the holder of the 256-bit device code. Device codes are kept as
SHA-256 keys, never in raw form. Nothing here is persisted or logged.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

# How long a pairing waits for approval, and then for its token to be collected.
PAIRING_SECONDS = 600
# The fewest seconds between two polls of one pairing (RFC 8628 ``interval``).
POLL_INTERVAL_SECONDS = 2
# Pending pairings, installation-wide. Starting is rate-limited per peer
# (``api.browser_routes``); this cap bounds memory whatever the sources.
MAX_PENDING = 64
CLIENT_NAME_MAX = 64
# No 0/O, 1/I/L, 2/Z, 5/S, 8/B: a code read aloud or retyped stays unambiguous.
_USER_ALPHABET = "ACDEFGHJKMNPQRTUVWXY34679"
_USER_CODE_LENGTH = 8


class PairingRefused(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


@dataclass
class Pairing:
    device_key: str
    user_code: str
    client_name: str
    requester: str
    created: float
    expires: float
    created_at: str
    status: str = "pending"            # pending | approved | denied
    token: str | None = None           # the raw token, between approval and collection
    token_id: str | None = None
    token_label: str | None = None
    account_id: str | None = None
    last_poll: float = field(default=-1e9)


def _device_key(device_code: str) -> str:
    return hashlib.sha256(str(device_code or "").encode("utf-8", errors="surrogatepass")).hexdigest()


def normalized_user_code(value: str) -> str:
    """``abcd-efgh``, ``ABCDEFGH`` and ``ABCD EFGH`` all name ``ABCD-EFGH``."""
    raw = "".join(ch for ch in str(value or "").upper() if ch.isalnum())
    return f"{raw[:4]}-{raw[4:]}" if len(raw) == _USER_CODE_LENGTH else ""


class PairingStore:
    """The one owner of pending pairings. Every method runs under one lock."""

    def __init__(self):
        self._by_device: dict[str, Pairing] = {}
        self._by_user: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def _expire(self, now: float, revoke: list[tuple[str, str]]) -> None:
        for key, pairing in list(self._by_device.items()):
            if pairing.expires <= now:
                if pairing.status == "approved" and pairing.token_id and pairing.account_id:
                    # Approved but never collected: the token must not outlive it.
                    revoke.append((pairing.account_id, pairing.token_id))
                self._drop(key)

    def _drop(self, key: str) -> None:
        pairing = self._by_device.pop(key, None)
        if pairing is not None:
            self._by_user.pop(pairing.user_code, None)

    async def _revoke(self, revoke: list[tuple[str, str]]) -> None:
        from auth.accounts import TOKEN_KIND_BROWSER, revoke_token

        for account_id, token_id in revoke:
            await revoke_token(account_id, token_id, kind=TOKEN_KIND_BROWSER, actor_id=account_id)

    async def start(self, client_name: str, requester: str) -> tuple[Pairing, str]:
        name = " ".join(str(client_name or "").split())[:CLIENT_NAME_MAX]
        if not name:
            raise PairingRefused("A browser pairing needs the browser's name.", code="browser.pairing.name_required")
        revoke: list[tuple[str, str]] = []
        async with self._lock:
            now = time.monotonic()
            self._expire(now, revoke)
            if len(self._by_device) >= MAX_PENDING:
                result = None
            else:
                device_code = secrets.token_urlsafe(32)
                while True:
                    raw = "".join(secrets.choice(_USER_ALPHABET) for _ in range(_USER_CODE_LENGTH))
                    user_code = f"{raw[:4]}-{raw[4:]}"
                    if user_code not in self._by_user:
                        break
                pairing = Pairing(
                    device_key=_device_key(device_code), user_code=user_code, client_name=name,
                    requester=str(requester or "")[:64], created=now, expires=now + PAIRING_SECONDS,
                    created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
                self._by_device[pairing.device_key] = pairing
                self._by_user[user_code] = pairing.device_key
                result = (pairing, device_code)
        await self._revoke(revoke)
        if result is None:
            raise PairingRefused("Too many browsers are waiting to be connected; try again shortly.",
                code="browser.pairing.busy")
        return result

    async def pending(self, user_code: str) -> Pairing:
        """The pending pairing a person is about to approve or deny."""
        revoke: list[tuple[str, str]] = []
        async with self._lock:
            self._expire(time.monotonic(), revoke)
            key = self._by_user.get(normalized_user_code(user_code))
            pairing = self._by_device.get(key) if key else None
            found = pairing if pairing is not None and pairing.status == "pending" else None
        await self._revoke(revoke)
        if found is None:
            raise PairingRefused("This connection request has expired or was already answered.",
                code="browser.pairing.not_found")
        return found

    async def approve(self, user_code: str, account_id: str, label: str) -> Pairing:
        """Issue the browser token for ``account_id`` -- the approving person's
        own account, never another -- and hold it for the poller. The token is
        created inside the lock, so a pairing is approved at most once."""
        from auth.accounts import issue_browser_token

        revoke: list[tuple[str, str]] = []
        async with self._lock:
            self._expire(time.monotonic(), revoke)
            key = self._by_user.get(normalized_user_code(user_code))
            pairing = self._by_device.get(key) if key else None
            if pairing is None or pairing.status != "pending":
                pairing = None
            else:
                record, raw = await issue_browser_token(account_id, label or pairing.client_name,
                                                        requester=pairing.requester)
                pairing.status, pairing.token, pairing.token_id = "approved", raw, record["id"]
                pairing.token_label, pairing.account_id = record["label"], str(account_id)
        await self._revoke(revoke)
        if pairing is None:
            raise PairingRefused("This connection request has expired or was already answered.",
                code="browser.pairing.not_found")
        return pairing

    async def deny(self, user_code: str) -> None:
        async with self._lock:
            key = self._by_user.get(normalized_user_code(user_code))
            pairing = self._by_device.get(key) if key else None
            if pairing is None or pairing.status != "pending":
                raise PairingRefused("This connection request has expired or was already answered.",
                code="browser.pairing.not_found")
            pairing.status = "denied"

    async def poll(self, device_code: str) -> dict:
        """What the extension holding ``device_code`` may know: still pending,
        too fast, denied, or approved -- with the raw token, once. A code that
        never existed and one that expired answer alike."""
        revoke: list[tuple[str, str]] = []
        async with self._lock:
            now = time.monotonic()
            self._expire(now, revoke)
            key = _device_key(device_code)
            pairing = self._by_device.get(key) if str(device_code or "") else None
            if pairing is None:
                answer = {"status": "expired"}
            elif now - pairing.last_poll < POLL_INTERVAL_SECONDS:
                pairing.last_poll = now
                answer = {"status": "slow_down", "interval": POLL_INTERVAL_SECONDS}
            else:
                pairing.last_poll = now
                if pairing.status == "pending":
                    answer = {"status": "pending", "interval": POLL_INTERVAL_SECONDS}
                elif pairing.status == "denied":
                    self._drop(key)
                    answer = {"status": "denied"}
                else:
                    answer = {"status": "approved", "token": pairing.token,
                              "token_id": pairing.token_id, "label": pairing.token_label}
                    self._drop(key)
        await self._revoke(revoke)
        return answer

pairing_store = PairingStore()
