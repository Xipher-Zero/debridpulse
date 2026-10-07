"""Debrid-Link API v2 client.

One native operation per call. Retry, recovery and ambiguous outcomes belong to
the universal core; nothing here loops or retries.

Debrid-Link authenticates with ``Authorization: Bearer <API key>`` (the
operator's private API key). The key is sent in that header only -- never in a
query string -- and never appears in a raised message or a log line.

Debrid-Link documents no finite request rate, only the ``floodDetected``
refusal, so this client paces nothing: a refusal is translated like any other,
with the server's own ``Retry-After`` carried when it states one.

https://debrid-link.com/api_doc/v2/introduction (machine-readable form:
``/api/v2/api_doc/infos`` and ``/api/v2/api_doc/errors``).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import aiohttp

from transfers.errors import safe_diagnostic

API_HOST = "debrid-link.com"
API = f"https://{API_HOST}/api/v2"

DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_UPLOAD_TIMEOUT_SECONDS = 120
TIMEOUT = aiohttp.ClientTimeout(total=DEFAULT_REQUEST_TIMEOUT_SECONDS)
# A native response is decoded only up to this size: a full page of torrents
# is far smaller, so anything larger is malformed, not data.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# The most one read of a response body asks for.
_READ_CHUNK_BYTES = 64 * 1024
# Debrid-Link's documented page bounds (``perPage``: min 20, max 100) and the
# most ids one ``ids`` query may name.
PAGE_SIZE = 100
MAX_IDS = 100

# Native object ids: a torrent id is an opaque token, a torrent file id is
# ``<torrent id>-<n>``; anything else is never treated as one.
_NATIVE_ID = re.compile(r"\A[A-Za-z0-9]{1,128}\Z")
_FILE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,191}\Z")


class DebridLinkAPIError(Exception):
    """A Debrid-Link refusal: its native ``error`` code (``""`` when the answer
    carried none), the HTTP status, the delay the server asked for, and -- for
    an error status whose body was not Debrid-Link's answer -- that body's
    safe facts (``detail``)."""

    def __init__(self, error: str, status: int = 0, *, retry_after: float | None = None, detail: str = ""):
        self.error = str(error or "")
        self.status = int(status or 0)
        self.retry_after = retry_after
        self.detail = str(detail or "")
        super().__init__(f"Debrid-Link [{self.error or self.status}]")


class DebridLinkProtocolError(Exception):
    """A Debrid-Link response that does not have the documented shape."""


# Not a native code: no API key is saved.
CREDENTIAL_MISSING = "credential_missing"


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    # The request this answers, as ``DebridLinkService._send`` made it: method
    # and endpoint path only -- never its query.
    method: str = ""
    path: str = ""


Transport = Callable[..., Awaitable[RawResponse]]


async def aiohttp_transport(method: str, url: str, *, headers=None, params=None, data=None,
                            timeout=TIMEOUT) -> RawResponse:
    """One HTTP exchange with a fresh session, the body read up to the bound."""
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers or {}, params=params, data=data,
                                   timeout=timeout, allow_redirects=False) as response:
            body = await _bounded_body(response.content)
            return RawResponse(response.status, {key.casefold(): value for key, value in response.headers.items()},
                               body)


async def _bounded_body(stream: aiohttp.StreamReader) -> bytes:
    """The whole body: read until it ends, or until it is past
    ``MAX_RESPONSE_BYTES`` -- all an oversized answer needs to show. One read
    returns only what has arrived so far, never the body. A failure before
    the end raises, so a partial body is never taken for an answer; the
    request's total timeout bounds every read here."""
    body = bytearray()
    while len(body) <= MAX_RESPONSE_BYTES:
        chunk = await stream.read(min(_READ_CHUNK_BYTES, MAX_RESPONSE_BYTES + 1 - len(body)))
        if not chunk:
            break
        body += chunk
    return bytes(body)


# What an unreadable answer may keep: enough to tell JSON, HTML, a CDN or WAF
# page and an empty or garbled body apart, never the body itself.
_EVIDENCE_BODY_BYTES = 96
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


def _unreadable(response: RawResponse, what: str) -> DebridLinkProtocolError:
    """A Debrid-Link answer that is not the documented one, described by its
    safe, bounded HTTP facts so it can be named: method, endpoint path,
    status, media type, length, a redirect's target and a short body prefix.
    Never a header but those, a query, a credential or the full body."""
    facts = [f"Debrid-Link {response.method or '?'} {response.path or '?'} {what}: HTTP {response.status}",
             f"content-type={safe_diagnostic(response.headers.get('content-type'), limit=64) or 'none'}",
             f"length={len(response.body)}"]
    if response.headers.get("location") is not None:
        facts.append(_location(str(response.headers["location"])))
    if response.body:
        prefix = response.body[:_EVIDENCE_BODY_BYTES].decode("utf-8", "replace")
        facts.append("body-prefix=" + json.dumps(safe_diagnostic(prefix, limit=_EVIDENCE_BODY_BYTES)))
    return DebridLinkProtocolError("; ".join(facts))


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
    except json.JSONDecodeError:
        raise _unreadable(response, "returned an answer that is not JSON") from None


def _retry_after(response: RawResponse) -> float | None:
    value = str(response.headers.get("retry-after") or "").strip()
    return float(value) if value.isdigit() else None


def _envelope(response: RawResponse) -> tuple[Any, dict]:
    """``(value, payload)`` of Debrid-Link's v2 answer, or the refusal it carries.

    v2 states success as ``{"success": true, "value": ...}`` and a refusal as
    ``{"success": false, "error": "<code>"}`` with a 4xx/5xx status; both the
    flag and the status are read, so neither alone can pass a refusal. A
    redirect is never followed and never decoded: it is a protocol fact of its
    own."""
    if 300 <= response.status < 400:
        raise _unreadable(response, "answered a redirect")
    try:
        payload = _decode(response)
    except DebridLinkProtocolError as exc:
        if response.status >= 400:
            raise DebridLinkAPIError("", response.status, retry_after=_retry_after(response),
                                     detail=str(exc)) from None
        raise
    if not isinstance(payload, dict):
        raise _unreadable(response, "returned an unexpected answer")
    if payload.get("success") is True and response.status < 400:
        return payload.get("value"), payload
    raise DebridLinkAPIError(str(payload.get("error") or ""), response.status, retry_after=_retry_after(response))


def _object(value: Any, what: str) -> dict:
    if not isinstance(value, dict):
        raise DebridLinkProtocolError(f"Debrid-Link returned an unexpected {what}")
    return value


def _list(value: Any, what: str) -> list:
    if not isinstance(value, list):
        raise DebridLinkProtocolError(f"Debrid-Link returned an unexpected {what}")
    return value


def native_id(value: object) -> str | None:
    text = str(value) if isinstance(value, str) else ""
    return text if _NATIVE_ID.match(text) else None


def native_file_id(value: object) -> str | None:
    text = str(value) if isinstance(value, str) else ""
    return text if _FILE_ID.match(text) else None


# -- the durable member address -----------------------------------------------------

# A seedbox file's durable, credential-free address. It is Debrid-Link's own
# ``/seedbox/list`` read for that torrent WITHOUT the key, plus the file id,
# so it can be a member request's payload: persisted, shown and routed back to
# Debrid-Link, yet useless without the account. The link Debrid-Link answers
# for the file is generated on demand and is never durable truth.
_MEMBER_PATH = "/api/v2/seedbox/list"


def member_address(torrent_id: str, file_id: str) -> str:
    if native_id(torrent_id) is None or native_file_id(file_id) is None:
        raise ValueError("member address needs a native torrent id and file id")
    return f"https://{API_HOST}{_MEMBER_PATH}?" + urlencode({"ids": torrent_id, "file_id": file_id})


def parse_member_address(value: object) -> tuple[str, str] | None:
    """``(torrent id, file id)`` when ``value`` is exactly a ``member_address``,
    else ``None``. Nothing else is read as one."""
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if (parts.scheme != "https" or parts.hostname != API_HOST or parts.port is not None or parts.username
            or parts.fragment or parts.path != _MEMBER_PATH):
        return None
    query = parse_qs(parts.query, keep_blank_values=True)
    if set(query) != {"ids", "file_id"} or any(len(values) != 1 for values in query.values()):
        return None
    torrent, file_id = native_id(query["ids"][0]), native_file_id(query["file_id"][0])
    if torrent is None or file_id is None:
        return None
    return torrent, file_id


class DebridLinkService:
    def __init__(self, api_key: str = "", *,
                 request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
                 upload_timeout_seconds: float = DEFAULT_UPLOAD_TIMEOUT_SECONDS,
                 transport: Transport | None = None):
        self.api_key = str(api_key or "").strip()
        self.request_timeout = aiohttp.ClientTimeout(total=float(request_timeout_seconds))
        self.upload_timeout = aiohttp.ClientTimeout(total=float(upload_timeout_seconds))
        self._transport = transport or aiohttp_transport

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def secrets(self) -> tuple[str, ...]:
        """Every secret this client holds, for diagnostic redaction."""
        return (self.api_key,) if self.api_key else ()

    async def _send(self, method, path, *, authorized=True, timeout=None, headers=None, **kwargs) -> RawResponse:
        if authorized and not self.api_key:
            raise DebridLinkAPIError(CREDENTIAL_MISSING)
        sent = dict(headers or {})
        if authorized:
            sent["Authorization"] = f"Bearer {self.api_key}"
        url = f"{API}/{path}"
        response = await self._transport(method, url, headers=sent, timeout=timeout or self.request_timeout, **kwargs)
        return replace(response, method=method, path=urlsplit(url).path)

    async def _json(self, method, path, body: dict) -> RawResponse:
        return await self._send(method, path, headers={"Content-Type": "application/json"}, data=json.dumps(body))

    # -- account and host inventory -------------------------------------------------

    async def account(self) -> dict:
        return _object(_envelope(await self._send("GET", "account/infos"))[0], "account")

    async def hosts(self) -> Any:
        """The public supported-host inventory (file hosters only). Fetched
        without the key: the list is the same for every account, so it carries
        no account truth."""
        return _envelope(await self._send(
            "GET", "downloader/hosts", authorized=False,
            params={"types": "host", "keys": "name,type,domains,regexs,isFree"}))[0]

    # -- direct hoster links -----------------------------------------------------------

    async def add_link(self, url: str) -> dict | list:
        """Generate a download for one hoster URL. Debrid-Link answers one link
        object, or a list of them when the URL is a folder of several files."""
        value = _envelope(await self._json("POST", "downloader/add", {"url": url}))[0]
        if not isinstance(value, (dict, list)):
            raise DebridLinkProtocolError("Debrid-Link returned an unexpected link answer")
        return value

    async def links(self, ids: tuple[str, ...]) -> list:
        """The account's downloader links named by ``ids``."""
        if not ids or len(ids) > MAX_IDS:
            raise ValueError("a link read names between one and 100 ids")
        return _list(_envelope(await self._send("GET", "downloader/list",
                                                params={"ids": ",".join(ids), "perPage": PAGE_SIZE}))[0],
                     "link list")

    async def remove_links(self, ids: tuple[str, ...]) -> list:
        joined = quote(",".join(ids), safe=",")
        return _list(_envelope(await self._send("DELETE", f"downloader/{joined}/remove"))[0], "removal answer")

    # -- seedbox -----------------------------------------------------------------------

    async def add_torrent(self, *, magnet: str = "", metainfo: bytes | None = None, name: str = "") -> dict:
        """Add a torrent. It always starts with every file wanted
        (``wait`` false): DebridPulse's own file selection decides what it
        materializes and is never pushed back. seedbox/add takes form fields
        -- a magnet as an ordinary form, a torrent file as multipart -- never
        JSON."""
        if metainfo is not None:
            form = aiohttp.FormData()
            form.add_field("file", bytes(metainfo), filename=name or "upload.torrent",
                           content_type="application/x-bittorrent")
            form.add_field("wait", "false")
            value = _envelope(await self._send("POST", "seedbox/add", data=form, timeout=self.upload_timeout))[0]
        else:
            value = _envelope(await self._send("POST", "seedbox/add", data={"url": magnet, "wait": "false"}))[0]
        return _object(value, "torrent creation")

    async def torrent(self, torrent_id: str) -> dict | None:
        """One torrent with its COMPLETE file list (an ``ids`` read never
        collapses many files into a zip entry), or ``None`` when the account
        holds no such torrent."""
        found = _list(_envelope(await self._send("GET", "seedbox/list",
                                                 params={"ids": torrent_id, "perPage": PAGE_SIZE}))[0],
                      "torrent list")
        if not found:
            return None
        if len(found) != 1:
            raise DebridLinkProtocolError("Debrid-Link answered one torrent read with several torrents")
        return _object(found[0], "torrent")

    async def torrents_page(self, page: int) -> tuple[list, int]:
        """One page of the account's torrents and the next page number
        (``-1`` once there is none)."""
        value, payload = _envelope(await self._send("GET", "seedbox/list",
                                                    params={"page": page, "perPage": PAGE_SIZE}))
        records = _list(value, "torrent page")
        pagination = payload.get("pagination")
        following = pagination.get("next") if isinstance(pagination, dict) else None
        if isinstance(following, bool) or not isinstance(following, int):
            raise DebridLinkProtocolError("Debrid-Link returned a torrent page without pagination")
        return records, following

    async def remove_torrent(self, torrent_id: str) -> list:
        return _list(_envelope(await self._send("DELETE", f"seedbox/{quote(torrent_id, safe='')}/remove"))[0],
                     "removal answer")
