from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from fastapi import Request, Response

from auth.sessions import request_is_secure


HTTP_LOGIN_CSRF_COOKIE = "downloadpulse-login-csrf"
HTTPS_LOGIN_CSRF_COOKIE = "__Host-downloadpulse-login-csrf"


@dataclass(frozen=True, slots=True)
class _LoginCsrfRecord:
    token_digest: bytes
    expires_at: float


class LoginCsrfStore:
    """Short-lived, one-time login-CSRF challenges bound to a browser cookie."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 600.0,
        max_entries: int = 512,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = max(30.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._key = secrets.token_bytes(32)
        self._entries: OrderedDict[bytes, _LoginCsrfRecord] = OrderedDict()

    def _browser_key(self, browser_nonce: str) -> bytes:
        return hmac.new(
            self._key,
            str(browser_nonce or "").encode("utf-8", errors="surrogatepass"),
            hashlib.sha256,
        ).digest()

    @staticmethod
    def _token_digest(token: str) -> bytes:
        return hashlib.sha256(str(token or "").encode("utf-8", errors="surrogatepass")).digest()

    def issue(self) -> tuple[str, str]:
        now = self._clock()
        self.cleanup()
        browser_nonce = secrets.token_urlsafe(24)
        form_token = secrets.token_urlsafe(32)
        key = self._browser_key(browser_nonce)
        self._entries[key] = _LoginCsrfRecord(
            token_digest=self._token_digest(form_token),
            expires_at=now + self.ttl_seconds,
        )
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return browser_nonce, form_token

    def consume(self, browser_nonce: str, form_token: str) -> bool:
        if not browser_nonce or not form_token:
            return False
        key = self._browser_key(browser_nonce)
        record = self._entries.pop(key, None)
        if record is None or record.expires_at <= self._clock():
            return False
        return secrets.compare_digest(record.token_digest, self._token_digest(form_token))

    def cleanup(self) -> int:
        now = self._clock()
        expired = [key for key, record in self._entries.items() if record.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)
        return len(expired)

    def clear(self) -> None:
        self._entries.clear()

    @property
    def size(self) -> int:
        self.cleanup()
        return len(self._entries)


def login_csrf_cookie_name(request: Request) -> str:
    return HTTPS_LOGIN_CSRF_COOKIE if request_is_secure(request) else HTTP_LOGIN_CSRF_COOKIE


def set_login_csrf_cookie(response: Response, request: Request, browser_nonce: str) -> None:
    secure = request_is_secure(request)
    response.set_cookie(
        key=HTTPS_LOGIN_CSRF_COOKIE if secure else HTTP_LOGIN_CSRF_COOKIE,
        value=str(browser_nonce),
        max_age=600,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )


def clear_login_csrf_cookie(response: Response, request: Request) -> None:
    secure = request_is_secure(request)
    response.delete_cookie(
        key=HTTPS_LOGIN_CSRF_COOKIE if secure else HTTP_LOGIN_CSRF_COOKIE,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )


login_csrf_store = LoginCsrfStore()
