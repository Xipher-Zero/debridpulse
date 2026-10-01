"""Real-Debrid REST 1.0 and OAuth2 (open-source device flow) client.

One native operation per call. Retry, recovery and ambiguous outcomes belong to
the universal core; the only loops here are the ones the OAuth protocol itself
defines (a refused access token is refreshed once and the call replayed once).

Authentication is ``Authorization: Bearer <access token>`` only -- a token is
never placed in a query string. Tokens, client secrets, refresh tokens and
device codes are never logged and never appear in a raised message.

https://api.real-debrid.com/
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Mapping

import aiohttp

from providers.realdebrid.rate_limit import SlidingWindowRateLimiter

API = "https://api.real-debrid.com/rest/1.0"
OAUTH = "https://api.real-debrid.com/oauth/v2"
# Real-Debrid's published client for open-source applications. It may request
# only the scopes Real-Debrid grants it (unrestrict, torrents, downloads, user).
OPEN_SOURCE_CLIENT_ID = "X245A4XAIBGVM"
DEVICE_GRANT = "http://oauth.net/grant_type/device/1.0"

TIMEOUT = aiohttp.ClientTimeout(total=30)
UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120)
# A native response is decoded only up to this size: a complete 5000-entry
# torrent page is far smaller, so anything larger is malformed, not data.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# An access token is treated as expired this long before Real-Debrid says so,
# so a call never races the expiry boundary.
_EXPIRY_MARGIN_SECONDS = 60
INVENTORY_PAGE_LIMIT = 5000


class RealDebridAPIError(Exception):
    """A Real-Debrid refusal: the native numeric ``error_code`` (``None`` when
    the response carried none), the native ``error`` name and the HTTP status."""

    def __init__(self, error_code: int | None, error: str, status: int = 0):
        self.error_code = error_code
        self.error = str(error or "")
        self.status = int(status or 0)
        super().__init__(f"Real-Debrid [{error_code if error_code is not None else self.status}]: {self.error}")


class RealDebridProtocolError(Exception):
    """A Real-Debrid response that does not have the documented shape."""


# Not a native code: the token endpoint refused the stored grant. Definitive --
# the operator has to connect again -- and distinct from any numeric code.
OAUTH_GRANT_REJECTED = "oauth_grant_rejected"
CREDENTIAL_MISSING = "credential_missing"


@dataclass(frozen=True)
class Credential:
    """The user-bound open-source client credential and its refresh token."""
    client_id: str
    client_secret: str
    refresh_token: str

    @property
    def usable(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[..., Awaitable[RawResponse]]


async def aiohttp_transport(method: str, url: str, *, headers=None, params=None, data=None,
                            timeout=TIMEOUT) -> RawResponse:
    """One HTTP exchange with a fresh session, the body read up to the bound."""
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers or {}, params=params, data=data,
                                   timeout=timeout, allow_redirects=False) as response:
            body = await response.content.read(MAX_RESPONSE_BYTES + 1)
            return RawResponse(response.status, {key.casefold(): value for key, value in response.headers.items()},
                               body)


def _decode(response: RawResponse) -> Any:
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise RealDebridProtocolError("Real-Debrid returned an oversized response")
    try:
        text = response.body.decode("utf-8").strip() if response.body else ""
    except UnicodeDecodeError:
        raise RealDebridProtocolError("Real-Debrid returned a response that is not UTF-8") from None
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise RealDebridProtocolError("Real-Debrid returned invalid JSON") from None


def _refusal(response: RawResponse) -> RealDebridAPIError:
    """The native refusal a non-success response carries, as typed facts."""
    try:
        payload = _decode(response)
    except RealDebridProtocolError:
        payload = None
    code = None
    name = ""
    if isinstance(payload, dict):
        raw = payload.get("error_code")
        if isinstance(raw, int) and not isinstance(raw, bool):
            code = raw
        name = str(payload.get("error") or "")
    return RealDebridAPIError(code, name, response.status)


def _object(value: Any, what: str) -> dict:
    if not isinstance(value, dict):
        raise RealDebridProtocolError(f"Real-Debrid returned an unexpected {what}")
    return value


class RealDebridService:
    def __init__(self, credential: Credential | None = None, *, rate_limit_per_minute: int = 240,
                 rate_limiter=None, transport: Transport | None = None,
                 on_refresh: Callable[[Credential], Awaitable[None]] | None = None,
                 clock: Callable[[], float] = time.time):
        self.credential = credential
        self._rate_limiter = rate_limiter or SlidingWindowRateLimiter(rate_limit_per_minute)
        self._transport = transport or aiohttp_transport
        self._on_refresh = on_refresh
        self._clock = clock
        self._access_token = ""
        self._access_expires_at = 0.0
        self._refresh_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return self.credential is not None and self.credential.usable

    def secrets(self) -> tuple[str, ...]:
        """Every secret this client currently holds, for diagnostic redaction."""
        values = [self._access_token]
        if self.credential is not None:
            values += [self.credential.client_secret, self.credential.refresh_token]
        return tuple(value for value in values if value)

    async def _send(self, method, url, **kwargs) -> RawResponse:
        await self._rate_limiter.acquire()
        return await self._transport(method, url, **kwargs)

    # -- OAuth2 device flow ----------------------------------------------------

    async def device_code(self) -> dict:
        """Start an open-source device authorization."""
        response = await self._send("GET", f"{OAUTH}/device/code",
                                    params={"client_id": OPEN_SOURCE_CLIENT_ID, "new_credentials": "yes"})
        if response.status != 200:
            raise _refusal(response)
        native = _object(_decode(response), "device authorization")
        for field in ("device_code", "user_code", "verification_url"):
            if not isinstance(native.get(field), str) or not native[field].strip():
                raise RealDebridProtocolError("Real-Debrid returned an incomplete device authorization")
        for field in ("interval", "expires_in"):
            if not isinstance(native.get(field), int) or isinstance(native.get(field), bool) or native[field] <= 0:
                raise RealDebridProtocolError("Real-Debrid returned an incomplete device authorization")
        return native

    async def device_credentials(self, device_code: str) -> dict | None:
        """The user-bound client credential once the user has authorized this
        device, or ``None`` while authorization is still pending.

        Real-Debrid answers a device the user has not approved yet with exactly
        HTTP 403 and ``{"error": null, "error_code": null}`` (observed live,
        2026-10-01). Only that is pending. Any other refusal -- a rate limit
        included -- is a refusal, and a success without the documented
        credential (Real-Debrid's answer to an unknown device code) is a
        malformed response, never pending."""
        response = await self._send("GET", f"{OAUTH}/device/credentials",
                                    params={"client_id": OPEN_SOURCE_CLIENT_ID, "code": device_code})
        if response.status == 403:
            try:
                payload = _decode(response)
            except RealDebridProtocolError:
                payload = None
            if (isinstance(payload, dict) and "error" in payload and "error_code" in payload
                    and payload["error"] is None and payload["error_code"] is None):
                return None
        if response.status != 200:
            raise _refusal(response)
        native = _object(_decode(response), "device credential")
        if not all(isinstance(native.get(field), str) and native[field].strip()
                   for field in ("client_id", "client_secret")):
            raise RealDebridProtocolError("Real-Debrid returned a device credential without a client credential")
        return native

    async def token(self, client_id: str, client_secret: str, code: str) -> dict:
        """Exchange a device code -- or a refresh token -- for an access token."""
        response = await self._send("POST", f"{OAUTH}/token", data={
            "client_id": client_id, "client_secret": client_secret, "code": code, "grant_type": DEVICE_GRANT})
        if 400 <= response.status < 500 and response.status != 429:
            raise RealDebridAPIError(None, OAUTH_GRANT_REJECTED, response.status)
        if response.status != 200:
            raise _refusal(response)
        native = _object(_decode(response), "token response")
        if not isinstance(native.get("access_token"), str) or not native["access_token"].strip():
            raise RealDebridProtocolError("Real-Debrid returned a token response without an access token")
        if not isinstance(native.get("refresh_token"), str) or not native["refresh_token"].strip():
            raise RealDebridProtocolError("Real-Debrid returned a token response without a refresh token")
        expires_in = native.get("expires_in")
        if not isinstance(expires_in, int) or isinstance(expires_in, bool) or expires_in <= 0:
            raise RealDebridProtocolError("Real-Debrid returned a token response without a lifetime")
        return native

    def adopt_token(self, native: dict) -> None:
        """Hold an access token this client's own credential just obtained."""
        self._access_token = str(native["access_token"])
        self._access_expires_at = self._clock() + int(native["expires_in"]) - _EXPIRY_MARGIN_SECONDS

    async def _refresh(self, stale: str) -> str:
        async with self._refresh_lock:
            # Another caller already replaced the token this one found stale.
            if self._access_token and self._access_token != stale and self._clock() < self._access_expires_at:
                return self._access_token
            credential = self.credential
            if credential is None or not credential.usable:
                raise RealDebridAPIError(None, CREDENTIAL_MISSING, 0)
            native = await self.token(credential.client_id, credential.client_secret, credential.refresh_token)
            self.adopt_token(native)
            rotated = str(native["refresh_token"])
            if rotated != credential.refresh_token:
                self.credential = replace(credential, refresh_token=rotated)
                if self._on_refresh is not None:
                    await self._on_refresh(self.credential)
            return self._access_token

    async def _authorized(self, method: str, path: str, *, params=None, data=None,
                          timeout=TIMEOUT) -> RawResponse:
        token = self._access_token
        if not token or self._clock() >= self._access_expires_at:
            token = await self._refresh(token)
        response = await self._send(method, f"{API}/{path}", headers={"Authorization": f"Bearer {token}"},
                                    params=params, data=data, timeout=timeout)
        if response.status == 401:
            # The protocol's own recovery: a refused access token is refreshed
            # once and the refused call replayed once. Nothing was performed.
            token = await self._refresh(token)
            response = await self._send(method, f"{API}/{path}", headers={"Authorization": f"Bearer {token}"},
                                        params=params, data=data, timeout=timeout)
        if response.status >= 400:
            raise _refusal(response)
        return response

    # -- REST ------------------------------------------------------------------

    async def user(self) -> dict:
        return _object(_decode(await self._authorized("GET", "user")), "user")

    async def disable_access_token(self) -> None:
        await self._authorized("GET", "disable_access_token")

    async def unrestrict_link(self, link: str) -> dict:
        return _object(_decode(await self._authorized("POST", "unrestrict/link", data={"link": link})),
                       "unrestricted link")

    async def add_magnet(self, magnet: str) -> dict:
        return _object(_decode(await self._authorized("POST", "torrents/addMagnet", data={"magnet": magnet})),
                       "torrent creation")

    async def add_torrent(self, metainfo: bytes) -> dict:
        return _object(_decode(await self._authorized("PUT", "torrents/addTorrent", data=bytes(metainfo),
                                                      timeout=UPLOAD_TIMEOUT)), "torrent creation")

    async def select_files(self, native_id: str, files: str = "all") -> int:
        """Select files of a torrent; 204 selected it, 202 says it already was."""
        response = await self._authorized("POST", f"torrents/selectFiles/{native_id}", data={"files": files})
        return response.status

    async def torrent_info(self, native_id: str) -> dict:
        return _object(_decode(await self._authorized("GET", f"torrents/info/{native_id}")), "torrent")

    async def torrents_page(self, page: int, limit: int = INVENTORY_PAGE_LIMIT) -> tuple[list, int | None]:
        """One inventory page and the advertised total. An empty page is either
        an empty JSON list or 204 No Content; anything else malformed fails."""
        response = await self._authorized("GET", "torrents", params={"page": page, "limit": limit})
        payload = _decode(response) if response.status != 204 else []
        if payload is None:
            payload = []
        if not isinstance(payload, list):
            raise RealDebridProtocolError("Real-Debrid returned an unexpected torrent page")
        total = response.headers.get("x-total-count")
        try:
            advertised = int(total) if total is not None else None
        except ValueError:
            raise RealDebridProtocolError("Real-Debrid returned an invalid torrent total") from None
        return payload, advertised

    async def delete_torrent(self, native_id: str) -> None:
        await self._authorized("DELETE", f"torrents/delete/{native_id}")

    async def hosts_domains(self) -> Any:
        response = await self._send("GET", f"{API}/hosts/domains")
        if response.status != 200:
            raise _refusal(response)
        return _decode(response)

    async def hosts_regex(self) -> Any:
        response = await self._send("GET", f"{API}/hosts/regex")
        if response.status != 200:
            raise _refusal(response)
        return _decode(response)
