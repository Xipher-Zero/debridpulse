"""Premiumize API client.

One native operation per call. Retry, recovery and ambiguous outcomes belong to
the universal core; nothing here loops or retries.

Premiumize authenticates with ``Authorization: Bearer <API key>`` (the
operator's API key). The key is sent in that header only -- never in a query
string or a body -- and never appears in a raised message or a log line.

Every answer states its own outcome in ``status`` (``"success"`` or
``"error"``, with a stable ``code``), on HTTP 200 as on an error status, so the
envelope is read before the status. Premiumize publishes ``rate_limit_reached``
but no request-rate ceiling, so this client paces nothing.

Request encoding: ``src`` operations are ``application/x-www-form-urlencoded``
forms; a torrent or NZB file is uploaded as the ``src`` field of a
``multipart/form-data`` body. Never JSON.

https://www.premiumize.me/api
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp

from transfers.errors import safe_diagnostic

API_HOST = "www.premiumize.me"
API = f"https://{API_HOST}/api"

DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_UPLOAD_TIMEOUT_SECONDS = 120
# A native response is decoded only up to this size: a whole transfer list or
# folder page is far smaller, so anything larger is malformed, not data.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# The most one read of a response body asks for.
_READ_CHUNK_BYTES = 64 * 1024
# Premiumize's object ids (transfers, files, folders) are opaque: any
# non-empty string up to this bound, kept exactly as Premiumize stated it.
MAX_NATIVE_ID_LENGTH = 1024


def native_id(value: Any) -> str | None:
    """A Premiumize object id exactly as stated, or ``None`` when ``value`` is
    not one. No grammar is assumed and nothing is trimmed or rewritten."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_NATIVE_ID_LENGTH:
        return None
    return value


class PremiumizeAPIError(Exception):
    """A Premiumize refusal: its stable ``code`` (``""`` when the answer
    carried none), its human ``message`` and the HTTP status.

    ``structured`` is whether this is a complete, structurally valid
    ``status: "error"`` answer with a stable ``code`` (or a request never
    sent). An error status whose body is not that answer (empty, truncated,
    HTML, other JSON) is not. What a structured code proves about a
    productive request is the translation's decision, never this client's."""

    def __init__(self, code: str, message: str = "", status: int = 0, *, structured: bool = False):
        self.code = str(code or "")
        self.message = str(message or "")
        self.status = int(status or 0)
        self.structured = bool(structured)
        super().__init__(f"Premiumize [{self.code or self.status}]: {self.message}")


class PremiumizeProtocolError(Exception):
    """A Premiumize response that does not have the documented shape."""


# Not a native code: no API key is saved.
CREDENTIAL_MISSING = "credential_missing"


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    # The request this answers: method and endpoint path only, never a query.
    method: str = ""
    path: str = ""


Transport = Callable[..., Awaitable[RawResponse]]


async def aiohttp_transport(method: str, url: str, *, headers=None, params=None, data=None,
                            timeout=None) -> RawResponse:
    """One HTTP exchange with a fresh session, never following a redirect;
    the request's total timeout bounds the exchange and every read."""
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers or {}, params=params, data=data,
                                   timeout=timeout, allow_redirects=False) as response:
            body = await _bounded_body(response.content)
            return RawResponse(response.status, {key.casefold(): value for key, value in response.headers.items()},
                               body)


async def _bounded_body(stream: aiohttp.StreamReader) -> bytes:
    """The whole body: read until it ends, or until it is past
    ``MAX_RESPONSE_BYTES`` -- all an oversized answer needs to show. The
    declared length is never trusted; one read returns only what has arrived.
    A failure before the end raises, so a partial body is never an answer."""
    body = bytearray()
    while len(body) <= MAX_RESPONSE_BYTES:
        chunk = await stream.read(min(_READ_CHUNK_BYTES, MAX_RESPONSE_BYTES + 1 - len(body)))
        if not chunk:
            break
        body += chunk
    return bytes(body)


# What an unreadable answer may keep: enough to tell JSON, HTML, a CDN page and
# an empty or garbled body apart, never the body itself.
_EVIDENCE_BODY_BYTES = 96


def _unreadable(response: RawResponse, what: str) -> PremiumizeProtocolError:
    """A Premiumize answer that is not the documented one, named by its safe,
    bounded HTTP facts: method, endpoint path, status, media type, length and a
    short body prefix. Never a header but those, a query or a credential."""
    facts = [f"Premiumize {response.method or '?'} {response.path or '?'} {what}: HTTP {response.status}",
             f"content-type={safe_diagnostic(response.headers.get('content-type'), limit=64) or 'none'}",
             f"length={len(response.body)}"]
    if response.body:
        prefix = response.body[:_EVIDENCE_BODY_BYTES].decode("utf-8", "replace")
        facts.append("body-prefix=" + json.dumps(safe_diagnostic(prefix, limit=_EVIDENCE_BODY_BYTES)))
    return PremiumizeProtocolError("; ".join(facts))


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


def _envelope(response: RawResponse) -> dict:
    """The whole success answer, or the refusal it carries.

    ``status`` decides, not the HTTP status: Premiumize states a business
    refusal as ``status: "error"`` with a stable ``code``, on HTTP 200 too. An
    error status whose body is not Premiumize's answer is that status's own
    refusal. A redirect is never followed and never decoded."""
    if 300 <= response.status < 400:
        raise _unreadable(response, "answered a redirect")
    try:
        payload = _decode(response)
    except PremiumizeProtocolError as exc:
        if response.status >= 400:
            raise PremiumizeAPIError("", str(exc), response.status) from None
        raise
    if not isinstance(payload, dict):
        raise _unreadable(response, "returned an unexpected answer")
    status = payload.get("status")
    if status == "success" and response.status < 400:
        return payload
    code, message = payload.get("code"), payload.get("message")
    structured = status == "error" and isinstance(code, str) and bool(code.strip())
    if status == "error" or response.status >= 400:
        raise PremiumizeAPIError(code if isinstance(code, str) else "", message if isinstance(message, str) else "",
                                 response.status, structured=structured)
    raise _unreadable(response, "returned an answer without a status")


def _list(payload: dict, field: str, what: str) -> list:
    value = payload.get(field)
    if not isinstance(value, list):
        raise PremiumizeProtocolError(f"Premiumize returned {what} without {field}")
    return value


# -- durable member addresses -----------------------------------------------------
#
# A member of a Premiumize collection is addressed by a credential-free
# Premiumize API URL, so it can be a member request's payload -- persisted,
# shown and routed back to this provider -- while the download link it yields
# is generated on demand and is never durable truth.
#
# * a cloud file: ``item/details?id=<file id>`` (Premiumize's stable file id);
# * a member of an immediate result: ``transfer/directdl?src=<source>&path=
#   <member path>&size=<bytes>`` -- no file id exists, so the member is the
#   source's exact collection path and exact size, proven again on each use.
CLOUD_MEMBER, IMMEDIATE_MEMBER = "cloud", "immediate"
_CLOUD_PATH, _IMMEDIATE_PATH = "/api/item/details", "/api/transfer/directdl"


def cloud_member_address(file_id: str) -> str:
    if native_id(file_id) != file_id:
        raise ValueError("a cloud member is addressed by a Premiumize file id")
    return f"{API}/item/details?" + urlencode({"id": file_id})


def immediate_member_address(source: str, path: str, size: int) -> str:
    if not source or not path or isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("an immediate member is its source, exact path and exact size")
    return f"{API}/transfer/directdl?" + urlencode({"src": source, "path": path, "size": str(size)})


def parse_member_address(value: object) -> tuple[str, ...] | None:
    """``(CLOUD_MEMBER, file id)`` or ``(IMMEDIATE_MEMBER, source, path,
    size)`` when ``value`` is exactly a member address, else ``None``."""
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme != "https" or parts.hostname != API_HOST or port is not None or parts.username
            or parts.fragment):
        return None
    query = parse_qs(parts.query, keep_blank_values=True)
    if any(len(values) != 1 for values in query.values()):
        return None
    fields = {key: values[0] for key, values in query.items()}
    if parts.path == _CLOUD_PATH and set(fields) == {"id"}:
        file_id = native_id(fields["id"])
        return (CLOUD_MEMBER, file_id) if file_id is not None else None
    if parts.path == _IMMEDIATE_PATH and set(fields) == {"src", "path", "size"}:
        size = fields["size"]
        if not fields["src"] or not fields["path"] or not size.isdigit():
            return None
        return IMMEDIATE_MEMBER, fields["src"], fields["path"], str(int(size))
    return None


class PremiumizeService:
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

    async def _send(self, method, path, *, timeout=None, **kwargs) -> dict:
        if not self.api_key:
            # Never sent: nothing was done.
            raise PremiumizeAPIError(CREDENTIAL_MISSING, structured=True)
        url = f"{API}/{path}"
        response = await self._transport(method, url, headers={"Authorization": f"Bearer {self.api_key}"},
                                         timeout=timeout or self.request_timeout, **kwargs)
        return _envelope(replace(response, method=method, path=urlsplit(url).path))

    # -- account and services --------------------------------------------------------

    async def account_info(self) -> dict:
        return await self._send("GET", "account/info")

    async def services(self) -> dict:
        return await self._send("GET", "services/list")

    async def cache_check(self, items: tuple[str, ...]) -> list[bool]:
        """Whether Premiumize's cache holds each item (a link or an
        info-hash), in order. A pure read that creates nothing."""
        if not items:
            return []
        answer = await self._send("POST", "cache/check", data=[("items[]", item) for item in items])
        held = _list(answer, "response", "a cache answer")
        if len(held) != len(items) or any(not isinstance(value, bool) for value in held):
            raise PremiumizeProtocolError("Premiumize returned a cache answer that does not match its items")
        return held

    # -- immediate resolution and cloud acquisition ---------------------------------

    async def directdl(self, source: str) -> list:
        """Premiumize's immediate result for ``source``: every member of
        ``content[]`` exactly as answered. Creates no cloud transfer."""
        return _list(await self._send("POST", "transfer/directdl", data={"src": source}),
                     "content", "an immediate result")

    async def create_transfer(self, *, source: str = "", upload: Any = None, name: str = "") -> str:
        """Create a cloud transfer -- a productive mutation with no idempotency
        key. ``source`` (a link or magnet) is a form field; ``upload`` (torrent
        or NZB bytes, or a binary stream) is the ``src`` file of a multipart
        body, under the upload timeout. Returns the transfer id."""
        if upload is not None:
            form = aiohttp.FormData()
            form.add_field("src", upload, filename=name or "upload", content_type="application/octet-stream")
            answer = await self._send("POST", "transfer/create", data=form, timeout=self.upload_timeout)
        else:
            answer = await self._send("POST", "transfer/create", data={"src": source})
        transfer_id = native_id(answer.get("id"))
        if transfer_id is None:
            # Premiumize may have created it, yet nothing here can be bound.
            raise PremiumizeProtocolError("Premiumize returned a creation answer without a transfer id")
        return transfer_id

    # -- observation -------------------------------------------------------------------

    async def list_transfers(self) -> list:
        return _list(await self._send("GET", "transfer/list"), "transfers", "a transfer list")

    async def folder_list(self, folder_id: str) -> dict:
        answer = await self._send("GET", "folder/list", params={"id": folder_id})
        _list(answer, "content", "a folder")
        return answer

    async def item_details(self, file_id: str) -> dict:
        return await self._send("GET", "item/details", params={"id": file_id})

    async def delete_transfer(self, transfer_id: str) -> None:
        await self._send("POST", "transfer/delete", data={"id": transfer_id})
