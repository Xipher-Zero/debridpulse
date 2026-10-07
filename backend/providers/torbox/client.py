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

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp

from core.presentation_safety import safe_public_host
from providers.torbox.rate_limit import SlidingWindowRateLimiter
from transfers.errors import safe_diagnostic

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
# TorBox answers at most about this many info-hashes per torrent cache query.
TORRENT_CACHE_BATCH = 100

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


# TorBox's refusal of an ``add_only_if_cached`` creation whose source it does
# not hold: nothing was added.
NOT_CACHED = "DOWNLOAD_NOT_CACHED"


def webdl_cache_key(link: str) -> str:
    """TorBox's web-download cache key for ``link``: the MD5 of the link as
    submitted. It identifies an ADDRESS TorBox has fetched before -- never the
    content behind it, so it is never integrity evidence."""
    return hashlib.md5(str(link).encode("utf-8"), usedforsecurity=False).hexdigest()


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
    # The request this answers, as ``TorBoxService._send`` made it: method and
    # endpoint path only -- never its query, which can carry the token.
    method: str = ""
    path: str = ""


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


# What an unreadable answer may keep: enough to tell JSON, HTML, a CDN or WAF
# page and an empty or garbled body apart, never the body itself.
_EVIDENCE_BODY_BYTES = 96
# How much of an unparseable answer around the parser's failure is kept.
_EVIDENCE_JSON_CONTEXT = 64
_EVIDENCE_HOST = re.compile(r"[a-z0-9.-]{1,253}")


def _location(value: str) -> str:
    """A redirect target's scheme, host, port and path -- never its userinfo,
    query or fragment."""
    try:
        target = urlsplit(value.strip())
        port = target.port
    except ValueError:
        return "location=unreadable"
    host = target.hostname or ""
    facts = [f"location-scheme={target.scheme or 'none'}",
             f"location-host={host if _EVIDENCE_HOST.fullmatch(host) else 'unreadable' if host else 'none'}"]
    if port is not None:
        facts.append(f"location-port={port}")
    facts.append(f"location-path={safe_diagnostic(target.path, limit=96) or '/'}")
    return " ".join(facts)


def _unreadable(response: RawResponse, what: str, *, parse: json.JSONDecodeError | None = None
                ) -> TorBoxProtocolError:
    """A TorBox answer that is not the documented one, described by its safe,
    bounded HTTP facts so it can be named: method, endpoint path, status,
    media type, length, where JSON parsing failed with a short escaped window
    around it, a redirect's target and a short body prefix. Never a header but
    those, a query, a credential or the full body."""
    facts = [f"TorBox {response.method or '?'} {response.path or '?'} {what}: HTTP {response.status}",
             f"content-type={safe_diagnostic(response.headers.get('content-type'), limit=64) or 'none'}",
             f"length={len(response.body)}"]
    if parse is not None:
        window = parse.doc[max(0, parse.pos - _EVIDENCE_JSON_CONTEXT):parse.pos + _EVIDENCE_JSON_CONTEXT]
        facts.append(f"json-error={safe_diagnostic(parse.msg, limit=64)} at pos {parse.pos} line {parse.lineno} "
                     f"col {parse.colno}")
        # ``json.dumps`` escapes every control character, so none is emitted literally.
        facts.append("json-context=" + safe_diagnostic(json.dumps(window), limit=4 * _EVIDENCE_JSON_CONTEXT))
    if response.headers.get("location") is not None:
        facts.append(_location(str(response.headers["location"])))
    if response.body:
        prefix = response.body[:_EVIDENCE_BODY_BYTES].decode("utf-8", "replace")
        facts.append("body-prefix=" + json.dumps(safe_diagnostic(prefix, limit=_EVIDENCE_BODY_BYTES)))
    return TorBoxProtocolError("; ".join(facts))


def _decode(response: RawResponse) -> Any:
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise _unreadable(response, "returned an oversized response")
    try:
        text = response.body.decode("utf-8").strip() if response.body else ""
    except UnicodeDecodeError:
        raise _unreadable(response, "returned a response that is not UTF-8") from None
    if not text:
        raise _unreadable(response, "returned an empty response")
    try:
        return json.loads(text)
    except json.JSONDecodeError as strict:
        # TorBox may send a literal control character inside a JSON string,
        # which its other clients' parsers accept and Python's strict parser
        # refuses. Only that is tolerated: the structure must still be JSON,
        # and every check after decoding still applies.
        try:
            return json.loads(text, strict=False)
        except json.JSONDecodeError:
            raise _unreadable(response, "returned an answer that is not JSON", parse=strict) from None


def _envelope(response: RawResponse) -> Any:
    """``data`` of TorBox's standard answer, or the refusal it carries.

    TorBox states success in its ``success`` flag and the reason in its
    ``error`` code; HTTP status alone is not authoritative (it answers some
    refusals 500 and some 400), so both are read. A redirect is never
    followed and never decoded: it is a protocol fact of its own."""
    if 300 <= response.status < 400:
        raise _unreadable(response, "answered a redirect")
    try:
        payload = _decode(response)
    except TorBoxProtocolError as exc:
        # An error status whose body is not TorBox's answer is still that
        # status's refusal; the body's safe facts are its only detail.
        if response.status >= 400:
            raise TorBoxAPIError("", str(exc), response.status) from None
        raise
    if not isinstance(payload, dict):
        raise _unreadable(response, "returned an unexpected answer")
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
        url = f"{API}/{path}"
        response = await self._transport(method, url, headers=sent, timeout=timeout or self.request_timeout, **kwargs)
        return replace(response, method=method, path=urlsplit(url).path)

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
        answer = _object(native, "creation answer")
        value = answer.get(FAMILIES[family].created_id)
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip().isdigit():
            # TorBox may accept a torrent into its queue of submissions it has
            # not started: it answers a ``queued_id`` -- the queue's own id,
            # never a torrent id -- and the torrent has none until it starts.
            # The creation happened, yet nothing here can be bound to it.
            if family == TORRENT and answer.get("queued_id") is not None:
                raise TorBoxProtocolError("TorBox accepted the torrent into its queue without a current torrent_id")
            raise TorBoxProtocolError("TorBox returned a creation answer without an object id")
        return str(value).strip()

    async def create_torrent(self, *, magnet: str = "", metainfo: bytes | None = None, name: str = "") -> str:
        # createtorrent is documented multipart/form-data; a form of plain
        # strings (a magnet) is not multipart unless asked to be.
        form = aiohttp.FormData(default_to_multipart=True)
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

    async def create_webdl(self, link: str, *, cached_only: bool = False) -> str | None:
        """Create a web download. ``cached_only`` asks TorBox to add it only
        if it already holds the source (``add_only_if_cached``) -- no hoster
        acquisition at all; ``None`` then means it did not, and added nothing."""
        form = {"link": link}
        if cached_only:
            form["add_only_if_cached"] = "true"
        try:
            native = _envelope(await self._send("POST", "webdl/createwebdownload", data=form))
        except TorBoxAPIError as exc:
            if cached_only and exc.error.upper() == NOT_CACHED:
                return None
            raise
        return self._created(WEBDL, native)

    async def webdl_cached(self, links: tuple[str, ...]) -> dict[str, dict]:
        """TorBox's cached web-download entries (``name``/``size``/``files``)
        for ``links``, keyed by link; a link it does not hold is absent. One
        batched read that creates nothing."""
        keys = {webdl_cache_key(link): link for link in links}
        if not keys:
            return {}
        native = _envelope(await self._send(
            "POST", "webdl/checkcached", params={"format": "object", "list_files": "true"},
            headers={"Content-Type": "application/json"}, data=json.dumps({"hashes": list(keys)})))
        if native is None:
            return {}
        if not isinstance(native, dict):
            raise TorBoxProtocolError("TorBox returned an unexpected cache answer")
        found = {}
        for key, entry in native.items():
            if entry is None:
                continue
            if not isinstance(entry, dict):
                raise TorBoxProtocolError("TorBox returned an unexpected cache entry")
            link = keys.get(str(key).casefold())
            if link is not None:
                found[link] = entry
        return found

    async def torrents_cached(self, hashes: tuple[str, ...]) -> frozenset[str]:
        """The info-hashes among ``hashes`` that TorBox's torrent cache holds;
        one that it does not hold is absent. Batched reads (``torrents/checkcached``,
        at most ``TORRENT_CACHE_BATCH`` hashes each) that create nothing."""
        wanted = sorted({str(value).casefold() for value in hashes if value})
        found = set()
        for start in range(0, len(wanted), TORRENT_CACHE_BATCH):
            chunk = wanted[start:start + TORRENT_CACHE_BATCH]
            native = _envelope(await self._send(
                "GET", "torrents/checkcached", params={"hash": ",".join(chunk), "format": "object"}))
            if native is None:
                continue
            if not isinstance(native, dict):
                raise TorBoxProtocolError("TorBox returned an unexpected cache answer")
            for key, entry in native.items():
                if entry is None:
                    continue
                if not isinstance(entry, dict):
                    raise TorBoxProtocolError("TorBox returned an unexpected cache entry")
                if str(key).casefold() in chunk:
                    found.add(str(key).casefold())
        return frozenset(found)

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

    async def queued_torrents(self, offset: int, limit: int = LIST_PAGE_LIMIT) -> list:
        """One page of TorBox's queue of torrent submissions it has not
        started yet, read fresh. An empty queue is an empty page. Each entry's
        ``id`` is a ``queued_id``, never a torrent id."""
        try:
            payload = _envelope(await self._send(
                "GET", "queued/getqueued",
                params={"type": "torrent", "offset": offset, "limit": limit, "bypass_cache": "true"}))
        except TorBoxAPIError as exc:
            if exc.error == "ITEM_NOT_FOUND":
                return []
            raise
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise TorBoxProtocolError("TorBox returned an unexpected queued page")
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
