from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from fastapi import Request, Response

from auth.models import Principal


HTTP_SESSION_COOKIE = "debridpulse-session"
HTTPS_SESSION_COOKIE = "__Host-debridpulse-session"
CSRF_HEADER = "X-CSRF-Token"


@dataclass(frozen=True, slots=True)
class SessionRecord:
    principal: Principal
    created_at: float
    expires_at: float
    credential_version: str = ""


class SessionStore:
    """Bounded, in-memory application sessions for the single-process appliance.

    Raw bearer session IDs are never retained server-side. The store is keyed by
    HMAC fingerprints under a process-random secret, so a process-memory dump of
    the mapping does not directly reveal reusable browser cookies.
    """

    def __init__(
        self,
        *,
        max_entries: int = 512,
        cleanup_interval_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_entries = max(1, int(max_entries))
        self.cleanup_interval_seconds = max(1.0, float(cleanup_interval_seconds))
        self._clock = clock
        self._session_key = secrets.token_bytes(32)
        self._csrf_key = secrets.token_bytes(32)
        self._entries: OrderedDict[bytes, SessionRecord] = OrderedDict()
        self._last_cleanup = self._clock()
        self._cleanup_task: asyncio.Task | None = None
        self._stop_cleanup: asyncio.Event | None = None
        self._revocation_listeners: list[Callable[[frozenset[bytes], str | None], None]] = []

    def on_revocation(self, listener: Callable[[frozenset[bytes], str | None], None]) -> None:
        """Be told, synchronously, which sessions ended (by ``fingerprint``)
        and, for an account change, which account -- so a long-lived
        connection opened under a session ends with it."""
        self._revocation_listeners.append(listener)

    def _announce(self, fingerprints, account_id: str | None = None) -> None:
        ended = frozenset(fingerprints)
        if not ended and not account_id:
            return
        for listener in tuple(self._revocation_listeners):
            listener(ended, account_id)

    def fingerprint(self, token: str) -> bytes:
        """The non-secret address of one session (its store key)."""
        return self._fingerprint(token)

    def _fingerprint(self, token: str) -> bytes:
        return hmac.new(
            self._session_key,
            str(token or "").encode("utf-8", errors="surrogatepass"),
            hashlib.sha256,
        ).digest()

    def create(
        self,
        principal: Principal,
        *,
        lifetime_seconds: float,
        credential_version: str = "",
    ) -> tuple[str, SessionRecord]:
        now = self._clock()
        self.cleanup(force=True)
        lifetime = max(60.0, float(lifetime_seconds))
        # Prefer the version attached to the actual authentication proof. Never
        # infer an OIDC proof from whatever configuration happens to be current
        # at session-creation time; doing so creates a policy-change race.
        resolved_version = str(credential_version or principal.credential_version or "")
        while True:
            token = secrets.token_urlsafe(32)
            fingerprint = self._fingerprint(token)
            if fingerprint not in self._entries:
                break
        record = SessionRecord(
            principal=principal,
            created_at=now,
            expires_at=now + lifetime,
            credential_version=resolved_version,
        )
        self._entries[fingerprint] = record
        self._entries.move_to_end(fingerprint)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return token, record

    def resolve(self, token: str) -> SessionRecord | None:
        if not token:
            return None
        now = self._clock()
        self.cleanup()
        fingerprint = self._fingerprint(token)
        record = self._entries.get(fingerprint)
        if record is None:
            return None
        if record.expires_at <= now:
            self._entries.pop(fingerprint, None)
            return None
        return record

    def csrf_token(self, token: str) -> str:
        if not token:
            return ""
        return hmac.new(
            self._csrf_key,
            str(token).encode("utf-8", errors="surrogatepass"),
            hashlib.sha256,
        ).hexdigest()

    def verify_csrf(self, token: str, candidate: str) -> bool:
        if not token or not candidate:
            return False
        expected = self.csrf_token(token)
        return secrets.compare_digest(expected, str(candidate))

    def revoke(self, token: str) -> bool:
        if not token:
            return False
        fingerprint = self._fingerprint(token)
        removed = self._entries.pop(fingerprint, None) is not None
        if removed:
            self._announce({fingerprint})
        return removed

    def revoke_mechanism(self, mechanism) -> int:
        targets = [
            key
            for key, record in self._entries.items()
            if record.principal.mechanism is mechanism
        ]
        for key in targets:
            self._entries.pop(key, None)
        self._announce(targets)
        return len(targets)

    def revoke_account(self, account_id: str) -> int:
        """Every session of one account, whatever mechanism issued it. The
        account owner calls this as part of every change to the account, so no
        session outlives the role, status or credential it was issued under."""
        account_id = str(account_id or "")
        if not account_id:
            return 0
        targets = [
            key
            for key, record in self._entries.items()
            if record.principal.account_id == account_id
        ]
        for key in targets:
            self._entries.pop(key, None)
        self._announce(targets, account_id)
        return len(targets)

    def clear(self) -> None:
        ended = list(self._entries)
        self._entries.clear()
        self._announce(ended)

    def cleanup(self, *, force: bool = False) -> int:
        now = self._clock()
        if not force and now - self._last_cleanup < self.cleanup_interval_seconds:
            return 0
        self._last_cleanup = now
        expired = [key for key, record in self._entries.items() if record.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)
        return len(expired)

    async def _cleanup_loop(self, stop_event: asyncio.Event) -> None:
        try:
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self.cleanup_interval_seconds,
                    )
                except TimeoutError:
                    self.cleanup(force=True)
        finally:
            self.cleanup(force=True)

    def start_cleanup(self) -> None:
        if self._cleanup_task is None or self._cleanup_task.done():
            stop_event = asyncio.Event()
            self._stop_cleanup = stop_event
            self._cleanup_task = asyncio.create_task(self._cleanup_loop(stop_event))

    async def stop_cleanup(self) -> None:
        task = self._cleanup_task
        stop_event = self._stop_cleanup
        if task is None:
            return
        if stop_event is not None:
            stop_event.set()
        try:
            await task
        finally:
            self._cleanup_task = None
            self._stop_cleanup = None

    @property
    def size(self) -> int:
        self.cleanup(force=True)
        return len(self._entries)


def _authority(value: str, *, default_scheme: str) -> tuple[str, int] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw if "://" in raw else f"{default_scheme}://{raw}")
        if not parsed.hostname:
            return None
        if parsed.port is not None:
            port = parsed.port
        else:
            port = 443 if parsed.scheme.casefold() == "https" else 80
        return parsed.hostname.casefold(), port
    except ValueError:
        return None


def _configured_https_origin_matches(request: Request) -> bool:
    """Use only operator-owned canonical origin state, never raw proxy headers."""
    configured = (os.getenv("PUBLIC_BASE_URL", "") or "").strip()
    if not configured:
        try:
            from core.config import get_settings

            configured = str(getattr(get_settings(), "public_base_url", "") or "").strip()
        except Exception:  # noqa: BLE001 - cookie classification must remain conservative
            return False
    try:
        parsed = urlsplit(configured)
    except ValueError:
        return False
    if parsed.scheme.casefold() != "https":
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        return False
    configured_authority = _authority(configured, default_scheme="https")
    request_authority = _authority(
        str(request.headers.get("Host", "") or ""),
        default_scheme="https",
    )
    return bool(configured_authority and configured_authority == request_authority)


def request_is_secure(request: Request) -> bool:
    if str(request.url.scheme or "").casefold() == "https":
        return True
    # Uvicorn may already translate trusted proxy headers into scope['scheme'].
    # Do not separately trust arbitrary X-Forwarded-Proto supplied by a client.
    return _configured_https_origin_matches(request)


def session_cookie_name(request: Request) -> str:
    return HTTPS_SESSION_COOKIE if request_is_secure(request) else HTTP_SESSION_COOKIE


def session_cookie_token(request: Request) -> str:
    # Prefer the secure cookie regardless of proxy-internal request scheme. OIDC
    # always issues the __Host- cookie and callback/session admission must not
    # depend on trusting forwarded headers to locate it.
    return str(
        request.cookies.get(HTTPS_SESSION_COOKIE)
        or request.cookies.get(HTTP_SESSION_COOKIE)
        or ""
    )


def set_session_cookie(
    response: Response,
    request: Request,
    token: str,
    *,
    max_age: int,
    force_secure: bool = False,
) -> None:
    secure = bool(force_secure or request_is_secure(request))
    response.set_cookie(
        key=HTTPS_SESSION_COOKIE if secure else HTTP_SESSION_COOKIE,
        value=str(token),
        max_age=max(60, int(max_age)),
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    # Clear both names. A secure OIDC session may arrive through a proxy whose
    # internal hop is HTTP and must still be removable without scheme inference.
    response.delete_cookie(
        key=HTTPS_SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        key=HTTP_SESSION_COOKIE,
        path="/",
        secure=False,
        httponly=True,
        samesite="lax",
    )


session_store = SessionStore()
