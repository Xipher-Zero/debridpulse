"""TorBox API v1 client and device authorization.

One native operation per call. Retry, recovery and ambiguous outcomes belong to
the universal core; nothing here loops or retries.

TorBox authenticates with ``Authorization: Bearer <API token>``. The one
exception is ``requestdl``, which TorBox defines with the token as a query
parameter; that request is built here and nowhere else, and the token never
appears in a raised message or a log line.

TorBox's three acquisition families -- torrents, web downloads and Usenet
downloads -- are parallel APIs whose object ids are unique only within their
family. The family table below is the one place their native names live.

https://api.torbox.app/openapi.json
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp

from core.presentation_safety import safe_public_host
from providers.torbox.rate_limit import SlidingWindowRateLimiter

API_HOST = "api.torbox.app"
API = f"https://{API_HOST}/v1/api"
# The name TorBox shows the operator on its device-authorization page.
DEVICE_APP_NAME = "DebridPulse"

DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_UPLOAD_TIMEOUT_SECONDS = 120
# A native response is decoded only up to this size: a full page of objects is
# far smaller, so anything larger is malformed, not data.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
LIST_PAGE_LIMIT = 1000

TORRENT, WEBDL, USENET = "torrent", "webdl", "usenet"


@dataclass(frozen=True)
class Family:
    """One TorBox acquisition family's native endpoints and field names."""
    path: str          # list/requestdl path segment
    control: str       # delete endpoint
    control_id: str    # id field of the control body
    requestdl_id: str  # id query parameter of requestdl
    created_id: str    # id field of a creation answer


FAMILIES: Mapping[str, Family] = {
    TORRENT: Family("torrents", "torrents/controltorrent", "torrent_id", "torrent_id", "torrent_id"),
    WEBDL: Family("webdl", "webdl/controlwebdownload", "webdl_id", "web_id", "webdownload_id"),
    USENET: Family("usenet", "usenet/controlusenetdownload", "usenet_id", "usenet_id", "usenetdownload_id"),
}


class TorBoxAPIError(Exception):
    """A TorBox refusal: its native ``error`` code (``""`` when the answer
    carried none), its user-facing ``detail`` and the HTTP status."""

    def __init__(self, error: str, detail: str = "", status: int = 0):
        self.error = str(error or "")
        self.detail = str(detail or "")
        self.status = int(status or 0)
        super().__init__(f"TorBox [{self.error or self.status}]: {self.detail}")


class TorBoxProtocolError(Exception):
    """A TorBox response that does not have the documented shape."""


# Not a native code: no credential is saved.
CREDENTIAL_MISSING = "credential_missing"


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[..., Awaitable[RawResponse]]


async def aiohttp_transport(method: str, url: str, *, headers=None, params=None, data=None,
                            timeout=None) -> RawResponse:
    """One HTTP exchange with a fresh session, the body read up to the bound."""
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers or {}, params=params, data=data,
                                   timeout=timeout, allow_redirects=False) as response:
            body = await response.content.read(MAX_RESPONSE_BYTES + 1)
            return RawResponse(response.status, {key.casefold(): value for key, value in response.headers.items()},
                               body)


def _decode(response: RawResponse) -> Any:
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise TorBoxProtocolError("TorBox returned an oversized response")
    try:
        text = response.body.decode("utf-8").strip() if response.body else ""
    except UnicodeDecodeError:
        raise TorBoxProtocolError("TorBox returned a response that is not UTF-8") from None
    if not text:
        raise TorBoxProtocolError("TorBox returned an empty response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise TorBoxProtocolError("TorBox returned invalid JSON") from None


def _envelope(response: RawResponse) -> Any:
    """``data`` of TorBox's standard answer, or the refusal it carries.

    TorBox states success in its ``success`` flag and the reason in its
    ``error`` code; HTTP status alone is not authoritative (it answers some
    refusals 500 and some 400), so both are read."""
    try:
        payload = _decode(response)
    except TorBoxProtocolError:
        if response.status >= 400:
            raise TorBoxAPIError("", "", response.status) from None
        raise
    if not isinstance(payload, dict):
        raise TorBoxProtocolError("TorBox returned an unexpected answer")
    error = payload.get("error")
    if payload.get("success") is True and not error and response.status < 400:
        return payload.get("data")
    raise TorBoxAPIError(str(error or ""), str(payload.get("detail") or ""), response.status)


def _object(value: Any, what: str) -> dict:
    if not isinstance(value, dict):
        raise TorBoxProtocolError(f"TorBox returned an unexpected {what}")
    return value


# The one DebridPulse-local field of a member address: the safe hostname of
# the hoster a web download was submitted for. It is provenance only -- never
# sent to TorBox, never part of the member's identity.
SOURCE_HOST_FIELD = "source_host"


def member_address(family: str, native_id: str, file_id: str, *, source_host: str | None = None) -> str:
    """The durable, credential-free address of one file of one TorBox object.

    It is TorBox's own ``requestdl`` endpoint WITHOUT the token, so it can be a
    member request's payload: persisted, shown and routed back to TorBox, yet
    useless without the account. The download link it yields is generated on
    demand (``TorBoxService.requestdl``) and is never durable truth.

    A web download's member may also carry ``source_host``: the bare,
    already-safe hostname of the hoster TorBox fetched it from, so every link
    generated for the member -- first or refreshed -- names that hoster as its
    source. Only the hostname: never the hoster URL's path, query or secrets."""
    spec = FAMILIES[family]
    fields = {spec.requestdl_id: native_id, "file_id": file_id}
    if source_host is not None:
        if family != WEBDL or safe_public_host(source_host) != source_host:
            raise ValueError("source_host is a web download's safe hostname")
        fields[SOURCE_HOST_FIELD] = source_host
    return f"{API}/{spec.path}/requestdl?" + urlencode(fields)


def _member_fields(value: object) -> tuple[str, str, str, str | None] | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if (parts.scheme != "https" or parts.hostname != API_HOST or parts.port is not None or parts.username
            or parts.fragment):
        return None
    query = parse_qs(parts.query, keep_blank_values=True)
    for family, spec in FAMILIES.items():
        allowed = {spec.requestdl_id, "file_id"}
        if family == WEBDL and SOURCE_HOST_FIELD in query:
            allowed.add(SOURCE_HOST_FIELD)
        if parts.path != f"/v1/api/{spec.path}/requestdl" or set(query) != allowed:
            continue
        if any(len(values) != 1 for values in query.values()):
            return None
        native_id, file_id = query[spec.requestdl_id][0], query["file_id"][0]
        host = query.get(SOURCE_HOST_FIELD, [None])[0]
        if not (native_id.isdigit() and file_id.isdigit()) or (host is not None and safe_public_host(host) != host):
            return None
        return family, native_id, file_id, host
    return None


def parse_member_address(value: object) -> tuple[str, str, str] | None:
    """``(family, object id, file id)`` when ``value`` is exactly a
    ``member_address``, else ``None``. Nothing else is read as one."""
    fields = _member_fields(value)
    return None if fields is None else fields[:3]


def member_source_host(value: object) -> str | None:
    """The web-download hoster a member address names, if any."""
    fields = _member_fields(value)
    return None if fields is None else fields[3]


class TorBoxService:
    def __init__(self, token: str = "", *, rate_limit_per_minute: int = 240,
                 request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
                 upload_timeout_seconds: float = DEFAULT_UPLOAD_TIMEOUT_SECONDS,
                 rate_limiter=None, transport: Transport | None = None):
        self.token = str(token or "")
        self.request_timeout = aiohttp.ClientTimeout(total=float(request_timeout_seconds))
        self.upload_timeout = aiohttp.ClientTimeout(total=float(upload_timeout_seconds))
        self._rate_limiter = rate_limiter or SlidingWindowRateLimiter(rate_limit_per_minute)
        self._transport = transport or aiohttp_transport

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def secrets(self) -> tuple[str, ...]:
        """Every secret this client holds, for diagnostic redaction."""
        return (self.token,) if self.token else ()

    async def _send(self, method, path, *, authorized=True, timeout=None, headers=None, **kwargs) -> RawResponse:
        if authorized and not self.token:
            raise TorBoxAPIError(CREDENTIAL_MISSING)
        sent = dict(headers or {})
        if authorized:
            sent["Authorization"] = f"Bearer {self.token}"
        await self._rate_limiter.acquire()
        return await self._transport(method, f"{API}/{path}", headers=sent, timeout=timeout or self.request_timeout,
                                     **kwargs)

    async def _json(self, method, path, body: dict) -> Any:
        return _envelope(await self._send(method, path, headers={"Content-Type": "application/json"},
                                          data=json.dumps(body)))

    # -- device authorization -------------------------------------------------

    async def device_start(self) -> dict:
        native = _object(_envelope(await self._send(
            "GET", "user/auth/device/start", authorized=False, params={"app": DEVICE_APP_NAME})),
            "device authorization")
        for field in ("device_code", "code", "verification_url", "expires_at"):
            if not isinstance(native.get(field), str) or not native[field].strip():
                raise TorBoxProtocolError("TorBox returned an incomplete device authorization")
        return native

    async def device_token(self, device_code: str) -> str | None:
        """The API token once the operator has approved this device, or
        ``None`` while TorBox still reports ``DEVICE_CODE_NOT_USED`` -- the one
        documented pending answer. Every other refusal is a refusal."""
        response = await self._send("POST", "user/auth/device/token", authorized=False,
                                    headers={"Content-Type": "application/json"},
                                    data=json.dumps({"device_code": device_code}))
        try:
            native = _object(_envelope(response), "device token")
        except TorBoxAPIError as exc:
            if exc.error == "DEVICE_CODE_NOT_USED":
                return None
            raise
        token = native.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise TorBoxProtocolError("TorBox returned a device token answer without a token")
        return token.strip()

    # -- account ------------------------------------------------------------------

    async def user(self) -> dict:
        return _object(_envelope(await self._send("GET", "user/me", params={"settings": "false"})), "user")

    async def hosters(self) -> Any:
        """The public supported-host inventory. Fetched without the token: the
        list is the same for every account, so it carries no account truth."""
        return _envelope(await self._send("GET", "webdl/hosters", authorized=False))

    # -- creation -----------------------------------------------------------------

    def _created(self, family: str, native: Any) -> str:
        value = _object(native, "creation answer").get(FAMILIES[family].created_id)
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip().isdigit():
            raise TorBoxProtocolError("TorBox returned a creation answer without an object id")
        return str(value).strip()

    async def create_torrent(self, *, magnet: str = "", metainfo: bytes | None = None, name: str = "") -> str:
        form = aiohttp.FormData()
        if metainfo is not None:
            form.add_field("file", bytes(metainfo), filename=name or "upload.torrent",
                           content_type="application/x-bittorrent")
        else:
            form.add_field("magnet", magnet)
        # TorBox zips a torrent of 100 files or more unless told not to; DP
        # materializes and places every member itself.
        form.add_field("allow_zip", "false")
        timeout = self.upload_timeout if metainfo is not None else None
        return self._created(TORRENT, _envelope(await self._send(
            "POST", "torrents/createtorrent", data=form, timeout=timeout)))

    async def create_webdl(self, link: str) -> str:
        return self._created(WEBDL, _envelope(await self._send(
            "POST", "webdl/createwebdownload", data={"link": link})))

    async def create_usenet(self, posting, *, name: str) -> str:
        """Submit one NZB posting as a file. ``posting`` is bytes or a binary
        stream, sent as it is read."""
        form = aiohttp.FormData()
        form.add_field("file", posting, filename=name or "upload.nzb", content_type="application/x-nzb")
        return self._created(USENET, _envelope(await self._send(
            "POST", "usenet/createusenetdownload", data=form, timeout=self.upload_timeout)))

    # -- observation ----------------------------------------------------------------

    async def item(self, family: str, native_id: str) -> dict:
        """One object, read fresh rather than from TorBox's list cache."""
        return _object(_envelope(await self._send(
            "GET", f"{FAMILIES[family].path}/mylist", params={"id": native_id, "bypass_cache": "true"})),
            f"{family} object")

    async def items(self, family: str, offset: int, limit: int = LIST_PAGE_LIMIT) -> list:
        """One page of the account's objects of ``family``. TorBox answers an
        empty account ``ITEM_NOT_FOUND``; that is an empty page."""
        try:
            payload = _envelope(await self._send(
                "GET", f"{FAMILIES[family].path}/mylist",
                params={"offset": offset, "limit": limit, "bypass_cache": "true"}))
        except TorBoxAPIError as exc:
            if exc.error == "ITEM_NOT_FOUND":
                return []
            raise
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise TorBoxProtocolError(f"TorBox returned an unexpected {family} page")
        return payload

    async def requestdl(self, family: str, native_id: str, file_id: str) -> str:
        """A fresh download link for one file. TorBox defines this endpoint
        with the token in its query; the link it answers is short-lived
        execution material."""
        if not self.token:
            raise TorBoxAPIError(CREDENTIAL_MISSING)
        spec = FAMILIES[family]
        link = _envelope(await self._send(
            "GET", f"{spec.path}/requestdl", authorized=False,
            params={"token": self.token, spec.requestdl_id: native_id, "file_id": file_id}))
        if not isinstance(link, str) or not link.strip():
            raise TorBoxProtocolError("TorBox returned no download link")
        return link.strip()

    async def delete(self, family: str, native_id: str) -> None:
        spec = FAMILIES[family]
        await self._json("POST", spec.control, {spec.control_id: int(native_id), "operation": "delete"})
