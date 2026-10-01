"""The one canonical bounded content-evidence owner: HTTP(S), FTP and SFTP.

It is also the one read-only remote discovery reader of those transports
(``ftp_discovery``, ``sftp_discovery``, and ``webdav_discovery`` -- HTTP(S)'s
own collection listing), under exactly the same destination, redirect and
credential decisions.

Every transport that can read an object's first and last bytes proves the same
neutral fact: the object's total length plus a bounded first window and a
bounded last window, hashed exactly one way (``digest_full``/``digest_prefix``).
Identical bytes therefore yield the identical fingerprint whether they were
read over HTTP(S), FTP or SFTP.

This module owns window sizing, bounded acquisition, the digest and the typed
transport facts it observed. It never decides equivalence, persists anything,
routes providers or admits writers, and it never authorizes a destination
itself: HTTP(S) reads ask ``services.network_safety`` for every destination,
redirect and public-address decision (its hardened validators and
``PublicDestinationResolver``), and FTP/SFTP readers receive a ``connect``
coroutine returning a socket already authorized by the one downloader egress
guard. Nothing here writes local material; sampled bytes live only long enough
to be hashed.
"""
from __future__ import annotations

import asyncio
import base64
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import logging
import re
from typing import Awaitable, Callable
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from xml.parsers import expat

import aiohttp
import asyncssh

from services import network_safety
from transfers.models import DiscoveryDepth, FingerprintKind
from transfers.requests import AuthScope, auth_scope


# asyncssh narrates connections at INFO, naming the submitted username and the
# origin. Evidence reads must not put operator input in application logs.
logging.getLogger("asyncssh").setLevel(logging.WARNING)

SAMPLE_BYTES = 64 * 1024
# Transports the bounded HTTP Range reader below speaks; redirect targets stay
# confined to them.
SAMPLED_FINGERPRINT_SCHEMES: frozenset[str] = frozenset({"http", "https"})
_MINIMUM_SAMPLE_BYTES = 4096
DEFAULT_TIMEOUT_SECONDS = 20.0

Sample = tuple[int, str, FingerprintKind, str, str]
# Returns a connected socket through the egress guard; ``None`` selects the
# authorized endpoint port, an integer a server-selected port on the same host.
Connect = Callable[[int | None], Awaitable[object]]
# Called once, at the moment the server definitively accepted the supplied
# credential -- before the read it authenticated for (the transport verdict).
Accepted = Callable[[], None] | None


@dataclass(frozen=True)
class AccessRequired:
    """The transport definitively requires access input before it yields evidence.

    ``server_identity`` is empty for plain authentication, or the SHA-1 of the
    host key the server presented, which must be confirmed before any
    credential is offered to it. ``address`` is where an HTTP(S) read was
    finally asked (the subject's own address, or one it was redirected to):
    the authority that asked is that address's.
    """
    server_identity: str = ""
    address: str = ""


# An operator credential for one HTTP(S) authority: ``(AuthScope, header
# value)``. It is attached only to requests whose ``auth_scope`` is exactly
# that authority (``_guarded_request``).
Credential = tuple[AuthScope, str] | None


def sample_size(requested: int = SAMPLE_BYTES) -> int:
    return max(_MINIMUM_SAMPLE_BYTES, int(requested))


def last_window_start(total: int, size: int) -> int:
    return max(0, int(total) - int(size))


def digest_prefix(total: int, body: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(str(total).encode("ascii"))
    digest.update(b"\0prefix\0")
    digest.update(body)
    return digest.hexdigest()


def digest_full(total: int, first: bytes, last: bytes | None = None) -> str:
    digest = hashlib.sha256()
    digest.update(str(total).encode("ascii"))
    digest.update(b"\0")
    digest.update(first)
    if last is not None:
        digest.update(b"\0")
        digest.update(last)
    return digest.hexdigest()


def sample(total: int, signature: str, kind: FingerprintKind, reason: str = "", prefix_signature: str = "") -> Sample:
    return total, signature, kind, reason, prefix_signature


def unavailable(reason: str) -> Sample:
    return sample(0, "", FingerprintKind.UNAVAILABLE, reason)


class _WindowUnavailable(Exception):
    """A window read returned an ordinary transport refusal, not bytes."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def _offset_windows(total: int, read: Callable[[int, int], Awaitable[bytes | None]], size: int) -> Sample:
    """The one offset-read proof: first window, then last window, exact lengths.

    Mirrors the HTTP 206 semantics exactly, so an object whose whole body fits
    the first window keeps FULL_CONTENT_SAMPLE strength, and a last window that
    cannot be read leaves the prefix-only evidence the HTTP sampler reports."""
    if total <= 0:
        return unavailable("range_ignored")
    count = min(size, total)
    first = await read(0, count)
    if first is None or len(first) != count:
        return unavailable("sampler_unavailable")
    prefix = digest_prefix(total, first)
    if total <= count:
        return sample(total, digest_full(total, first), FingerprintKind.FULL_CONTENT_SAMPLE, "", prefix)
    start = last_window_start(total, size)
    try:
        last = await read(start, total - start)
    except _WindowUnavailable as exc:
        return sample(total, prefix, FingerprintKind.PREFIX_CONTENT_SAMPLE, exc.reason, prefix)
    if last is None or len(last) != total - start:
        return sample(total, prefix, FingerprintKind.PREFIX_CONTENT_SAMPLE, "sampler_unavailable", prefix)
    return sample(total, digest_full(total, first, last), FingerprintKind.FULL_CONTENT_SAMPLE, "", prefix)


# ── HTTP(S) ────────────────────────────────────────────────────────────────

def _content_range(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"\s*bytes\s+(\d+)\s*-\s*(\d+)\s*/\s*(\d+)\s*", str(value or ""), re.I)
    if not match:
        return None
    start, end, total = (int(match.group(index)) for index in (1, 2, 3))
    if total <= 0 or start < 0 or end < start or end >= total:
        return None
    return start, end, total


def _basic_challenge(response) -> bool:
    """A 401 is authentication evidence only when the server offers Basic
    credentials -- the one scheme the downloader can answer with operator
    input. Any other status or challenge scheme keeps its existing meaning."""
    if response.status != 401:
        return False
    return any(str(value).strip().split(" ", 1)[0].casefold() == "basic"
               for value in response.headers.getall("WWW-Authenticate", ()))


def _plausible_as_complete_representation(discovered_length: int, expected_bytes: int) -> bool:
    """Negative certainty guard only -- never artifact-identity policy.

    Decides whether a short, Range-ignoring 200 response can credibly be
    treated as the complete representation of an object the caller reported
    at a known positive size. A positive ``expected_bytes`` may only rule out
    a claim of completeness here; it is never compared for exact equality and
    never used to decide that two candidates are the same or different
    artifact -- that remains Universal Core policy.
    """
    return expected_bytes <= 0 or discovered_length * 2 >= expected_bytes


async def _read_exactly(response, count: int) -> bytes | None:
    """Read exactly one bounded sample; never consume past its declared region."""
    try:
        return await response.content.readexactly(count)
    except asyncio.IncompleteReadError:
        return None


def _granted(lan: bool) -> dict:
    """The private-LAN keyword only for a granted origin: every ungranted call
    is exactly the public-destination call it always was."""
    return {"private_lan": True} if lan else {}


def _origin(uri: str) -> tuple[str, str, int]:
    parsed = urlsplit(uri)
    return parsed.scheme.casefold(), str(parsed.hostname or "").casefold(), int(
        parsed.port or network_safety.default_destination_port(parsed.scheme))


# Request headers that describe the read itself and therefore survive a move
# to another origin; every other header -- above all any credential -- stays
# with the origin it was addressed to.
_READ_HEADERS = frozenset({"range", "accept-encoding"})


async def _guarded_request(session, uri: str, headers: dict, *, method: str = "GET", data: bytes | None = None,
                           carried: frozenset[str] = _READ_HEADERS, max_redirects: int = 3,
                           private_lan: bool = False, credential: Credential = None):
    """THE one in-process HTTP(S) request owner: ``(response, reason, uri)``.

    Every hop's destination is validated by ``network_safety``; redirects are
    followed here, bounded, never by the client library. A move to another
    origin (scheme, host or port -- so HTTPS to HTTP always counts) keeps only
    the ``carried`` read-describing headers: a capability header never follows
    it, and is never restored even if a later hop returns. An operator
    ``credential`` is attached to exactly the hops whose authentication scope
    is the one it was given for -- never another host or port, never HTTP for
    an HTTPS answer. ``uri`` is the address the returned response answered for."""
    current = uri
    current_headers = dict(headers)
    prior_origin = _origin(uri)
    granted_host = prior_origin[1] if private_lan else ""
    redirected = False
    for hop in range(max_redirects + 1):
        # A private-LAN grant covers the operator's own host only: a redirect
        # to any other name never inherits it.
        lan = bool(granted_host) and _origin(current)[1] == granted_host
        try:
            validated = await (network_safety.validate_resolved_public_destination(current, private_lan=True) if lan
                               else network_safety.validate_resolved_public_destination(current))
        except network_safety.DestinationLookupError:
            return None, "dns_failure", current
        except network_safety.UnsafeDestinationError:
            return None, "destination_rejected", current
        sent = dict(current_headers)
        if credential is not None and auth_scope(validated) == credential[0]:
            sent["Authorization"] = credential[1]
        response = await session.request(method, validated, headers=sent, data=data, allow_redirects=False)
        if not (300 <= response.status < 400):
            return response, "redirect" if redirected else "", validated
        location = str(response.headers.get("Location") or "").strip()
        response.release()
        if not location or hop >= max_redirects:
            return None, "redirect", validated
        next_uri = urljoin(validated, location)
        try:
            network_safety.validate_provider_download_url(
                next_uri, context="redirect target", schemes=SAMPLED_FINGERPRINT_SCHEMES,
                **_granted(bool(granted_host) and _origin(next_uri)[1] == granted_host))
        except network_safety.UnsafeDestinationError:
            return None, "destination_rejected", next_uri
        next_origin = _origin(next_uri)
        if next_origin != prior_origin:
            current_headers = {key: value for key, value in current_headers.items()
                               if key.casefold() in carried}
        prior_origin = next_origin
        current = next_uri
        redirected = True
    return None, "redirect", current


async def sampled_public_artifact_fingerprint(
    uri: str,
    *,
    sample_bytes: int = SAMPLE_BYTES,
    timeout_seconds: float = 20.0,
    headers: dict | None = None,
    expected_bytes: int = 0,
    private_lan: bool = False,
    credential: Credential = None,
    on_authenticated: Accepted = None,
) -> Sample | AccessRequired:
    """Return bounded structured content evidence for a public HTTP(S) capability.

    ``expected_bytes`` is retained for caller compatibility but is deliberately
    not an identity gate. The sampler reports the payload size it discovers;
    the Universal Core owns reported-size plausibility policy. Windows and
    digests are the shared ``services.artifact_sampling`` definition, so the
    same bytes fingerprint identically over every transport. A definitive
    Basic authentication challenge on the first window is reported as the
    typed ``AccessRequired`` fact (naming the address that asked) for the
    executor to translate. ``credential`` reaches only its own authority.
    """
    # The sampler speaks HTTP(S) only. A transport it cannot sample is refused
    # here, at its own boundary, so no other caller has to know that.
    if urlsplit(str(uri or "")).scheme.casefold() not in SAMPLED_FINGERPRINT_SCHEMES:
        return unavailable("destination_rejected")
    try:
        validated = await network_safety.validate_resolved_public_destination(uri, **_granted(private_lan))
    except network_safety.DestinationLookupError:
        return unavailable("dns_failure")
    except network_safety.UnsafeDestinationError:
        return unavailable("destination_rejected")
    granted_host = _origin(validated)[1] if private_lan else ""

    sample_bytes = sample_size(sample_bytes)
    timeout = aiohttp.ClientTimeout(total=max(5.0, float(timeout_seconds)))
    base_headers = {**(headers or {}), "Accept-Encoding": "identity"}
    connector = aiohttp.TCPConnector(
        resolver=network_safety.PublicDestinationResolver(**({"private_lan_host": granted_host} if granted_host else {})),
        use_dns_cache=False)
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            first_headers = {**base_headers, "Range": f"bytes=0-{sample_bytes - 1}"}
            response, redirect_reason, answered = await _guarded_request(session, validated, first_headers,
                                                                         private_lan=bool(granted_host),
                                                                         credential=credential)
            if response is None:
                return unavailable(redirect_reason or "sampler_unavailable")
            try:
                if _basic_challenge(response):
                    return AccessRequired(address=answered)
                if (credential is not None and on_authenticated is not None and response.status != 401
                        and auth_scope(answered) == credential[0]):
                    on_authenticated()
                if response.status == 200:
                    try:
                        length = int(response.headers.get("Content-Length") or 0)
                    except (TypeError, ValueError):
                        length = 0
                    if length <= 0:
                        return unavailable("range_ignored")
                    count = min(sample_bytes, length)
                    first = await _read_exactly(response, count)
                    if first is None:
                        return unavailable("sampler_unavailable")
                    prefix = digest_prefix(length, first)
                    if length <= sample_bytes:
                        if not _plausible_as_complete_representation(length, expected_bytes):
                            return unavailable("incomplete_representation")
                        return sample(length, digest_full(length, first), FingerprintKind.FULL_CONTENT_SAMPLE,
                                       redirect_reason, prefix)
                    return sample(length, prefix, FingerprintKind.PREFIX_CONTENT_SAMPLE,
                                   "range_ignored", prefix)
                if response.status != 206:
                    return unavailable("range_unsupported")
                parsed = _content_range(response.headers.get("Content-Range", ""))
                if parsed is None:
                    return unavailable("invalid_content_range")
                start, end, total = parsed
                expected_end = min(total - 1, sample_bytes - 1)
                if start != 0 or end != expected_end:
                    return unavailable("invalid_content_range")
                first = await _read_exactly(response, end - start + 1)
                if first is None:
                    return unavailable("sampler_unavailable")
                prefix = digest_prefix(total, first)
            finally:
                response.release()

            if total <= len(first):
                return sample(total, digest_full(total, first[:total]), FingerprintKind.FULL_CONTENT_SAMPLE,
                               redirect_reason, prefix)

            last_start = last_window_start(total, sample_bytes)
            last_headers = {**base_headers, "Range": f"bytes={last_start}-{total - 1}"}
            response, last_redirect_reason, _answered = await _guarded_request(session, validated, last_headers,
                                                                               private_lan=bool(granted_host),
                                                                               credential=credential)
            if response is None:
                return sample(total, prefix, FingerprintKind.PREFIX_CONTENT_SAMPLE,
                               last_redirect_reason or "sampler_unavailable", prefix)
            try:
                if response.status != 206:
                    reason = "range_ignored" if response.status == 200 else "range_unsupported"
                    return sample(total, prefix, FingerprintKind.PREFIX_CONTENT_SAMPLE, reason, prefix)
                parsed = _content_range(response.headers.get("Content-Range", ""))
                if parsed is None:
                    return sample(total, prefix, FingerprintKind.PREFIX_CONTENT_SAMPLE,
                                   "invalid_content_range", prefix)
                start, end, repeated_total = parsed
                if start != last_start or end != total - 1 or repeated_total != total:
                    return sample(total, prefix, FingerprintKind.PREFIX_CONTENT_SAMPLE,
                                   "size_disagreement" if repeated_total != total else "invalid_content_range", prefix)
                last = await _read_exactly(response, end - start + 1)
                if last is None:
                    return sample(total, prefix, FingerprintKind.PREFIX_CONTENT_SAMPLE,
                                   "sampler_unavailable", prefix)
            finally:
                response.release()

        return sample(total, digest_full(total, first, last), FingerprintKind.FULL_CONTENT_SAMPLE,
                       redirect_reason or last_redirect_reason, prefix)
    except asyncio.TimeoutError:
        return unavailable("timeout")
    except network_safety.DestinationLookupError:
        return unavailable("dns_failure")
    except network_safety.UnsafeDestinationError:
        return unavailable("destination_rejected")
    except (aiohttp.ClientError, OSError, ValueError):
        return unavailable("sampler_unavailable")


# ── Remote discovery facts (shared by every transport) ─────────────────────

class _SessionRefused(Exception):
    """A session ended in a typed fact rather than a usable session."""

    def __init__(self, outcome):
        super().__init__("session refused")
        self.outcome = outcome


@dataclass(frozen=True)
class Listing:
    """The regular files of one directory: ``(name, size)`` pairs, listed in
    ``directory``, the server's concrete absolute path for it. A name holds
    ``/`` only for a file a deeper listing found below the directory (its
    path relative to it). ``location`` is the address the directory was
    finally listed at when the server moved it (``""``: where it was asked)."""
    entries: tuple[tuple[str, int], ...]
    directory: str = ""
    location: str = ""


@dataclass(frozen=True)
class RemoteFile:
    """The discovered path is one regular file of ``size`` bytes (``location``
    as for ``Listing``)."""
    size: int
    path: str = ""
    location: str = ""


@dataclass(frozen=True)
class Opaque:
    """The server answered definitively, but describes the path through no
    listing protocol at all: neither a file nor a directory as far as
    discovery can prove. A positive protocol fact, never a refusal."""


@dataclass(frozen=True)
class ListingRefused:
    """A definitive refusal of the discovered path itself."""
    reason: str


MAX_LISTED_ENTRIES = 10_000


# ── FTP ────────────────────────────────────────────────────────────────────

class _FtpControl:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    async def reply(self) -> tuple[int, str]:
        line = (await self.reader.readline()).decode("latin-1").rstrip("\r\n")
        if len(line) < 3 or not line[:3].isdigit():
            raise ConnectionError("Malformed FTP reply")
        code, text = int(line[:3]), line
        if len(line) > 3 and line[3] == "-":
            while True:
                more = await self.reader.readline()
                if not more:
                    raise ConnectionError("Truncated FTP reply")
                text = more.decode("latin-1").rstrip("\r\n")
                if text.startswith(f"{code} "):
                    break
        return code, text

    async def command(self, verb: str, argument: str = "") -> tuple[int, str]:
        if any(char in argument for char in "\r\n\x00"):
            raise ValueError("FTP arguments must be single-line text")
        self.writer.write(((f"{verb} {argument}" if argument else verb) + "\r\n").encode("latin-1"))
        await self.writer.drain()
        return await self.reply()


def _ftp_segments(address: str) -> list[str]:
    return [unquote(part) for part in urlsplit(address).path.split("/") if part]


def _ftp_path(address: str) -> tuple[list[str], str]:
    """aria2's own FTP path semantics: login directory, then each decoded directory segment, then the file."""
    segments = _ftp_segments(address)
    if not segments:
        raise ValueError("FTP evidence needs a file path")
    return segments[:-1], segments[-1]


def _passive_port(code: int, text: str) -> int | None:
    if code == 229:
        match = re.search(r"\(\|\|\|(\d{1,5})\|\)", text)
        return int(match.group(1)) if match else None
    if code == 227:
        match = re.search(r"(\d{1,3}),(\d{1,3}),(\d{1,3}),(\d{1,3}),(\d{1,3}),(\d{1,3})", text)
        return int(match.group(5)) * 256 + int(match.group(6)) if match else None
    return None


@asynccontextmanager
async def _ftp_session(connect: Connect, username: str, password: str, on_authenticated: Accepted = None):
    """THE one FTP login: control connection through the egress guard, then
    USER/PASS, binary type and the login directory -- exactly aria2's own
    sequence. Only a 530 answer to the login itself is access evidence;
    anything else is an ordinary unavailable fact. A 230 to the login is the
    server's acceptance (``on_authenticated``)."""
    writer = None
    try:
        reader, writer = await asyncio.open_connection(sock=await connect(None))
        control = _FtpControl(reader, writer)
        if (await control.reply())[0] != 220:
            raise _SessionRefused(unavailable("sampler_unavailable"))
        code, _ = await control.command("USER", username)
        if code == 331:
            code, _ = await control.command("PASS", password)
        if code == 530:
            raise _SessionRefused(AccessRequired())
        if code != 230:
            raise _SessionRefused(unavailable("range_unsupported"))
        if on_authenticated is not None:
            on_authenticated()
        if (await control.command("TYPE", "I"))[0] != 200:
            raise _SessionRefused(unavailable("range_unsupported"))
        code, text = await control.command("PWD")
        home = re.match(r'257 "((?:[^"]|"")*)"', text) if code == 257 else None
        if home is not None and (await control.command("CWD", home.group(1).replace('""', '"')))[0] != 250:
            raise _SessionRefused(unavailable("range_unsupported"))
        yield control
    finally:
        if writer is not None:
            writer.close()


async def _ftp_data(control: _FtpControl, connect: Connect) -> tuple[object, object]:
    """One passive data connection on the same authorized host: the server's
    advertised data address is ignored and only its port is used."""
    code, text = await control.command("PASV")
    port = _passive_port(code, text)
    if port is None:
        code, text = await control.command("EPSV")
        port = _passive_port(code, text)
    if port is None:
        raise _WindowUnavailable("range_unsupported")
    return await asyncio.open_connection(sock=await connect(port))


async def ftp_fingerprint(address: str, *, connect: Connect, username: str, password: str,
                          sample_bytes: int = SAMPLE_BYTES,
                          timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                          on_authenticated: Accepted = None) -> Sample | AccessRequired:
    """Bounded FTP evidence: binary type, SIZE, then REST/RETR offset windows.

    Only a 530 answer to the login itself is authentication evidence; a
    missing path, a permission refusal or any other reply is an ordinary
    unavailable fact. Control and every passive data connection come from
    ``connect`` (the egress guard); the server's advertised data address is
    ignored and only its port is used, on the same authorized host."""
    size = sample_size(sample_bytes)
    try:
        directories, filename = _ftp_path(address)
        async with asyncio.timeout(max(5.0, float(timeout_seconds))):
            async with _ftp_session(connect, username, password, on_authenticated) as control:
                for directory in directories:
                    if (await control.command("CWD", directory))[0] != 250:
                        return unavailable("range_unsupported")
                code, text = await control.command("SIZE", filename)
                if code != 213:
                    return unavailable("range_unsupported")
                try:
                    total = int(text[4:].strip())
                except ValueError:
                    return unavailable("range_unsupported")

                async def window(offset: int, count: int) -> bytes | None:
                    data_reader, data_writer = await _ftp_data(control, connect)
                    try:
                        if offset and (await control.command("REST", str(offset)))[0] != 350:
                            raise _WindowUnavailable("range_unsupported")
                        code, _ = await control.command("RETR", filename)
                        if code not in {125, 150}:
                            raise _WindowUnavailable("range_unsupported")
                        try:
                            body = await data_reader.readexactly(count)
                        except asyncio.IncompleteReadError:
                            return None
                    finally:
                        data_writer.close()
                    # A window closed before end of file is answered 426/451 by the
                    # server; either final reply leaves the session usable.
                    await control.reply()
                    return body

                try:
                    return await _offset_windows(total, window, size)
                except _WindowUnavailable as exc:
                    return unavailable(exc.reason)
    except _SessionRefused as refused:
        return refused.outcome
    except TimeoutError:
        return unavailable("timeout")
    except PermissionError:
        return unavailable("destination_rejected")
    except (ConnectionError, OSError, ValueError, asyncio.IncompleteReadError):
        return unavailable("sampler_unavailable")


_MAX_LISTING_BYTES = 4 * 1024 * 1024


async def _ftp_listing_lines(control: _FtpControl, connect: Connect, verb: str) -> list[str] | None:
    """One bounded listing transfer of the current directory, or ``None``
    when the server does not implement ``verb``."""
    data_reader, data_writer = await _ftp_data(control, connect)
    try:
        code, _ = await control.command(verb)
        if code in {500, 501, 502, 504}:
            return None
        if code not in {125, 150}:
            raise _SessionRefused(ListingRefused("not_a_directory" if code == 550 else "unsupported_listing"))
        body = await data_reader.read(_MAX_LISTING_BYTES + 1)
        chunk = body
        while chunk and len(body) <= _MAX_LISTING_BYTES:
            chunk = await data_reader.read(_MAX_LISTING_BYTES + 1 - len(body))
            body += chunk
    finally:
        data_writer.close()
    await control.reply()
    if len(body) > _MAX_LISTING_BYTES:
        raise _SessionRefused(ListingRefused("too_many_entries"))
    return [line for line in body.decode("utf-8", "replace").splitlines() if line.strip()]


async def ftp_discovery(address: str, *, connect: Connect, username: str, password: str,
                        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, on_authenticated: Accepted = None,
                        ) -> Listing | RemoteFile | ListingRefused | AccessRequired | Sample:
    """Classify one FTP path from the server's own answers, read-only.

    A trailing ``/`` (or the login directory itself) is directory intent. Any
    other path is changed into (CWD) -- a server changes only into a
    directory -- and otherwise sized (SIZE): a regular file. A directory's
    immediate regular files come from MLSD, or, where the server has no MLSD,
    from NLST with each name sized (SIZE answers only for regular files).
    Nothing is recursive and nothing is retrieved."""
    segments = _ftp_segments(address)
    directory_intent = urlsplit(address).path.endswith("/") or not segments
    parents = segments if directory_intent else segments[:-1]
    try:
        async with asyncio.timeout(max(5.0, float(timeout_seconds))):
            async with _ftp_session(connect, username, password, on_authenticated) as control:
                for directory in parents:
                    if (await control.command("CWD", directory))[0] != 250:
                        return ListingRefused("not_found")
                if not directory_intent:
                    final = segments[-1]
                    if (await control.command("CWD", final))[0] != 250:
                        code, text = await control.command("SIZE", final)
                        if code != 213:
                            return ListingRefused("not_found")
                        try:
                            return RemoteFile(int(text[4:].strip()), "/".join(segments))
                        except ValueError:
                            return ListingRefused("unsupported_listing")
                code, text = await control.command("PWD")
                listed = re.match(r'257 "((?:[^"]|"")*)"', text) if code == 257 else None
                directory = listed.group(1).replace('""', '"') if listed else ""
                entries = []
                lines = await _ftp_listing_lines(control, connect, "MLSD")
                if lines is not None:
                    for line in lines:
                        facts, _, name = line.partition(" ")
                        named = dict(item.split("=", 1) for item in facts.lower().split(";") if "=" in item)
                        if named.get("type") == "file" and name and "/" not in name:
                            entries.append((name, int(named.get("size") or 0)))
                else:
                    lines = await _ftp_listing_lines(control, connect, "NLST")
                    if lines is None:
                        return ListingRefused("unsupported_listing")
                    names = [line.rsplit("/", 1)[-1] for line in lines]
                    if len(names) > MAX_LISTED_ENTRIES:
                        return ListingRefused("too_many_entries")
                    for name in names:
                        if name in {".", ".."} or not name:
                            continue
                        code, text = await control.command("SIZE", name)
                        if code == 213:
                            try:
                                entries.append((name, int(text[4:].strip())))
                            except ValueError:
                                continue
                if len(entries) > MAX_LISTED_ENTRIES:
                    return ListingRefused("too_many_entries")
                return Listing(tuple(sorted(entries)), directory)
    except _SessionRefused as refused:
        return refused.outcome
    except _WindowUnavailable:
        return ListingRefused("unsupported_listing")
    except TimeoutError:
        return unavailable("timeout")
    except PermissionError:
        return unavailable("destination_rejected")
    except (ConnectionError, OSError, ValueError, asyncio.IncompleteReadError):
        return unavailable("sampler_unavailable")


# ── WebDAV: HTTP(S)'s own collection listing ───────────────────────────────

# The only two properties discovery asks for: whether a member is a
# collection, and its size where the server states one. Entity tags and
# modification times are never requested -- nothing here is content evidence.
_PROPFIND_BODY = (b'<?xml version="1.0" encoding="utf-8"?>'
                  b'<propfind xmlns="DAV:"><prop><resourcetype/><getcontentlength/></prop></propfind>')
# The body bound below applies to the decoded listing, whatever encoding the
# server chose, so no transfer encoding is pinned here.
_PROPFIND_HEADERS = {"Depth": "1", "Content-Type": 'application/xml; charset="utf-8"'}
# A listing request moved to another origin is still a listing request; its
# credential is not (``_guarded_request``).
_PROPFIND_CARRIED = frozenset({"depth", "content-type"})
_DAV = "DAV: "
_HTTP_STATUS = re.compile(r"HTTP/\d(?:\.\d)?\s+(\d{3})(?:\s|$)")
_DECIMAL = re.compile(r"[0-9]{1,19}")


class _Malformed(Exception):
    """A listing answer DP will not interpret."""


def _multistatus(body: bytes) -> list[tuple[str, int, bool, int | None]]:
    """``(href, status, collection, size)`` for every response of one
    ``207 Multi-Status`` body.

    Parsed with no document type, no entity declaration and no external
    entity at all -- a WebDAV answer needs none, so any of them makes the
    answer malformed rather than something to resolve. A response carries its
    own status, or the status of the property block that describes it."""
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    def refuse(*_arguments):
        raise _Malformed("declarations are not accepted")

    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    stack: list[str] = []
    text: list[list[str]] = []
    responses: list[tuple[str, int, bool, int | None]] = []
    response: dict | None = None
    block: dict | None = None

    def start(name, _attributes):
        nonlocal response, block
        stack.append(name)
        text.append([])
        if name == _DAV + "response":
            response = {"href": None, "status": None, "blocks": []}
        elif name == _DAV + "propstat" and response is not None:
            block = {"status": None, "collection": False, "size": None}
        elif (name == _DAV + "collection" and block is not None
              and len(stack) >= 2 and stack[-2] == _DAV + "resourcetype"):
            block["collection"] = True

    def characters(value):
        text[-1].append(value)

    def end(name):
        nonlocal response, block
        value = "".join(text.pop()).strip()
        stack.pop()
        parent = stack[-1] if stack else ""
        if response is None:
            return
        if name == _DAV + "href" and parent == _DAV + "response":
            if response["href"] is not None:
                raise _Malformed("a response names one resource")
            response["href"] = value
        elif name == _DAV + "status" and parent in {_DAV + "response", _DAV + "propstat"}:
            matched = _HTTP_STATUS.match(value)
            if matched is None:
                raise _Malformed("unreadable status")
            (block if parent == _DAV + "propstat" else response)["status"] = int(matched.group(1))
        elif name == _DAV + "getcontentlength" and block is not None and parent == _DAV + "prop":
            block["size"] = int(value) if _DECIMAL.fullmatch(value) else None
        elif name == _DAV + "propstat" and block is not None:
            response["blocks"].append(block)
            block = None
        elif name == _DAV + "response":
            described = [item for item in response["blocks"] if 200 <= (item["status"] or 0) < 300]
            status = response["status"] or (200 if described else 0)
            if not response["href"] or not status:
                raise _Malformed("a response names a resource and its status")
            responses.append((response["href"], status, any(item["collection"] for item in described),
                              next((item["size"] for item in described if item["size"] is not None), None)))
            response = None
            if len(responses) > MAX_LISTED_ENTRIES + 1:
                raise ListingTooLarge()

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    try:
        parser.Parse(body, True)
    except expat.ExpatError as exc:
        raise _Malformed("not well-formed") from exc
    return responses


class ListingTooLarge(Exception):
    """A listing past the neutral entry bound."""


def _dav_segments(base: str, href: str) -> tuple[str, ...] | None:
    """The decoded path segments one ``href`` names, resolved against the
    address that listed it -- or ``None`` when it cannot name a member there:
    another origin, a query or fragment, a percent-escape that is not UTF-8,
    an encoded separator, an empty, ``.`` or ``..`` segment, or a control
    character. A trailing ``/`` is collection syntax, never part of a name."""
    target = urljoin(base, href)
    parts = urlsplit(target)
    if parts.query or parts.fragment or _origin(target) != _origin(base) or not parts.path.startswith("/"):
        return None
    pieces = parts.path.split("/")[1:]
    if pieces and pieces[-1] == "":
        pieces.pop()
    segments = []
    for piece in pieces:
        try:
            value = unquote(piece, errors="strict")
        except UnicodeDecodeError:
            return None
        if value in {"", ".", ".."} or "/" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            return None
        segments.append(value)
    return tuple(segments)


def _collection_address(base: str, segments: tuple[str, ...]) -> str:
    parts = urlsplit(base)
    path = "/" + "".join(quote(item, safe="") + "/" for item in segments)
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _listing_refusal(status: int) -> str:
    if status == 403:
        return "permission_denied"
    if status in {404, 410}:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if 500 <= status < 600 and status != 501:
        return "server_error"
    return "unsupported_listing"


async def webdav_discovery(address: str, *, depth: DiscoveryDepth, username: str = "", password: str = "",
                           credential_scope: AuthScope | None = None,
                           timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, on_authenticated: Accepted = None,
                           private_lan: bool = False, max_files: int | None = None,
                           scan_timeout_seconds: float | None = None,
                           ) -> Listing | RemoteFile | Opaque | ListingRefused | AccessRequired:
    """Classify one HTTP(S) path through WebDAV, read-only, and list it.

    Every request is one ``PROPFIND`` with ``Depth: 1`` -- the depth every
    WebDAV server must support -- so the neutral ``depth`` is enforced here,
    by repeated one-level listings, and never depends on the server offering
    ``Depth: infinity``: ``CURRENT`` lists the collection once, ``N`` descends
    into at most N levels of subcollections, ``UNLIMITED`` until none remain.
    A collection is entered at most once (the visited set is its canonical
    decoded path), and every member must be an immediate child of the
    collection that listed it, so no answer can make the traversal loop. Only
    a complete listing is a result: a refused, malformed or oversized member
    fails the whole discovery; nothing is ever truncated. ``max_files`` is the
    most regular files the caller accepts -- one more fails the discovery
    (``too_many_entries``) -- and can only tighten the neutral entry bound,
    never raise it; ``scan_timeout_seconds`` bounds the whole enumeration (every
    request keeps its own ``timeout_seconds`` bound too) and an enumeration
    still running at it fails as ``timeout``.

    The path's own answer classifies it: a collection lists its regular files;
    anything else described is one regular file of its stated size. A server
    that answers without WebDAV -- a success that is not a ``207``, or ``405``
    or ``501`` for the method itself -- is ``Opaque``; every other answer keeps
    its ordinary meaning. Destinations, redirects and the private-LAN grant
    are ``_guarded_request``'s: the credential is sent only to the authority
    it was given for (``credential_scope``, by default ``address``'s own), and
    an authentication challenge names the address that asked
    (``AccessRequired.address``) -- possibly a server the path moved to. No
    content is ever read."""
    if urlsplit(str(address or "")).scheme.casefold() not in SAMPLED_FINGERPRINT_SCHEMES:
        return ListingRefused("destination_rejected")
    try:
        validated = await network_safety.validate_resolved_public_destination(address, **_granted(private_lan))
    except network_safety.DestinationLookupError:
        return ListingRefused("dns_failure")
    except network_safety.UnsafeDestinationError:
        return ListingRefused("destination_rejected")
    granted_host = _origin(validated)[1] if private_lan else ""
    credential: Credential = ((credential_scope or auth_scope(validated)),
                              "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()) if username else None
    accepted = False

    async def propfind(session, uri: str):
        """``(answered address, 207 body or status)``; a challenge or a
        refusal of the request itself ends the discovery (``_SessionRefused``)."""
        nonlocal accepted
        response, reason, answered = await _guarded_request(
            session, uri, dict(_PROPFIND_HEADERS), method="PROPFIND", data=_PROPFIND_BODY, carried=_PROPFIND_CARRIED,
            private_lan=bool(granted_host) and _origin(uri)[1] == granted_host, credential=credential)
        if response is None:
            raise _SessionRefused(ListingRefused(
                reason if reason in {"dns_failure", "destination_rejected"} else "unsupported_listing"))
        try:
            if response.status == 401:
                if not _basic_challenge(response):
                    raise _SessionRefused(ListingRefused("auth_method_unsupported"))
                # Whichever authority asked -- this path's own, or one it was
                # moved to -- the question names it; an answer for another
                # authority is never offered here.
                raise _SessionRefused(AccessRequired(address=answered))
            if (credential is not None and auth_scope(answered) == credential[0] and not accepted
                    and on_authenticated):
                accepted = True
                on_authenticated()
            if response.status != 207:
                return answered, response.status
            body = await response.content.read(_MAX_LISTING_BYTES + 1)
            chunk = body
            while chunk and len(body) <= _MAX_LISTING_BYTES:
                chunk = await response.content.read(_MAX_LISTING_BYTES + 1 - len(body))
                body += chunk
            if len(body) > _MAX_LISTING_BYTES:
                raise _SessionRefused(ListingRefused("too_many_entries"))
            return answered, body
        finally:
            response.release()

    file_limit = MAX_LISTED_ENTRIES if max_files is None else min(int(max_files), MAX_LISTED_ENTRIES)
    timeout = aiohttp.ClientTimeout(total=max(5.0, float(timeout_seconds)))
    connector = aiohttp.TCPConnector(
        resolver=network_safety.PublicDestinationResolver(**({"private_lan_host": granted_host} if granted_host else {})),
        use_dns_cache=False)
    try:
        async with asyncio.timeout(scan_timeout_seconds), aiohttp.ClientSession(
                timeout=timeout, connector=connector) as session:
            answered, outcome = await propfind(session, validated)
            if isinstance(outcome, int):
                if 200 <= outcome < 300 or outcome in {405, 501}:
                    return Opaque()
                return ListingRefused(_listing_refusal(outcome))
            root = _dav_segments(answered, answered)
            listing = _multistatus(outcome)
            own = [item for item in listing if _dav_segments(answered, item[0]) == root]
            if root is None or len(own) != 1 or not 200 <= own[0][1] < 300:
                return ListingRefused("unsupported_listing")
            location = answered if answered != validated else ""
            if not own[0][2]:
                if len(listing) != 1:
                    return ListingRefused("unsupported_listing")
                return RemoteFile(max(0, own[0][3] or 0), location=location)
            files: list[tuple[str, int]] = []
            listed = 0
            visited = {root}
            # Members resolve against the collection's own address, which a
            # server may have answered without its trailing slash.
            pending = deque([(_collection_address(answered, root), root, (), 0, listing)])
            while pending:
                base, segments, relative, level, members = pending.popleft()
                if members is None:
                    moved, outcome = await propfind(session, base)
                    if isinstance(outcome, int):
                        return ListingRefused(_listing_refusal(outcome))
                    members = _multistatus(outcome)
                    if _dav_segments(moved, moved) != segments or _origin(moved) != _origin(base):
                        return ListingRefused("unsupported_listing")
                    base = moved
                names = set()
                own_seen = False
                for href, status, collection, size in members:
                    path = _dav_segments(base, href)
                    if path == segments:
                        own_seen = own_seen or collection
                        continue
                    if path is None or len(path) != len(segments) + 1 or path[:-1] != segments or path in names:
                        return ListingRefused("unsupported_listing")
                    if not 200 <= status < 300:
                        return ListingRefused(_listing_refusal(status))
                    names.add(path)
                    listed += 1
                    if listed > MAX_LISTED_ENTRIES:
                        return ListingRefused("too_many_entries")
                    member = relative + (path[-1],)
                    if not collection:
                        files.append(("/".join(member), max(0, size or 0)))
                        if len(files) > file_limit:
                            return ListingRefused("too_many_entries")
                    elif depth.descends(level) and path not in visited:
                        visited.add(path)
                        pending.append((_collection_address(base, path), path, member, level + 1, None))
                if not own_seen:
                    return ListingRefused("unsupported_listing")
            directory = "/" + "".join(item + "/" for item in root)
            return Listing(tuple(sorted(files)), directory, location=location)
    except _SessionRefused as refused:
        return refused.outcome
    except (_Malformed, ValueError):
        return ListingRefused("unsupported_listing")
    except ListingTooLarge:
        return ListingRefused("too_many_entries")
    except asyncio.TimeoutError:
        return ListingRefused("timeout")
    except network_safety.DestinationLookupError:
        return ListingRefused("dns_failure")
    except network_safety.UnsafeDestinationError:
        return ListingRefused("destination_rejected")
    except aiohttp.ClientSSLError:
        return ListingRefused("tls_failure")
    except aiohttp.ClientConnectorError as exc:
        refused = isinstance(getattr(exc, "os_error", None), ConnectionRefusedError)
        return ListingRefused("connection_refused" if refused else "connection_failed")
    except (aiohttp.ClientError, OSError):
        return ListingRefused("connection_failed")


# ── HTTP(S) download location ──────────────────────────────────────────────

@dataclass(frozen=True)
class Located:
    """Where an HTTP(S) read of an address is finally answered (``uri``,
    reached through the guarded redirect owner) and what it answered."""
    uri: str
    status: int


async def resolve_location(uri: str, *, headers: dict | None = None, credential: Credential = None,
                           timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, private_lan: bool = False,
                           on_authenticated: Accepted = None) -> Located | AccessRequired | None:
    """Follow one address's redirects the way every in-process HTTP(S) read
    does (``_guarded_request``) and report where it is finally answered.

    One ranged request for at most a single byte, whose body is never read:
    a download writer can then be pointed at the answering address itself,
    with the operator credential only when that address is the credential's
    own authority. A Basic challenge is ``AccessRequired`` naming the address
    that asked. ``None`` when nothing answered (the address stays as it is,
    and the writer reports its own failure)."""
    if urlsplit(str(uri or "")).scheme.casefold() not in SAMPLED_FINGERPRINT_SCHEMES:
        return None
    try:
        validated = await network_safety.validate_resolved_public_destination(uri, **_granted(private_lan))
    except (network_safety.DestinationLookupError, network_safety.UnsafeDestinationError):
        return None
    granted_host = _origin(validated)[1] if private_lan else ""
    timeout = aiohttp.ClientTimeout(total=max(5.0, float(timeout_seconds)))
    connector = aiohttp.TCPConnector(
        resolver=network_safety.PublicDestinationResolver(**({"private_lan_host": granted_host} if granted_host else {})),
        use_dns_cache=False)
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            response, _reason, answered = await _guarded_request(
                session, validated, {**(headers or {}), "Range": "bytes=0-0"}, private_lan=bool(granted_host),
                credential=credential)
            if response is None:
                return None
            try:
                if _basic_challenge(response):
                    return AccessRequired(address=answered)
                if (credential is not None and on_authenticated is not None and response.status != 401
                        and auth_scope(answered) == credential[0]):
                    on_authenticated()
                return Located(answered, response.status)
            finally:
                response.release()
    except (asyncio.TimeoutError, network_safety.DestinationLookupError, network_safety.UnsafeDestinationError,
            aiohttp.ClientError, OSError, ValueError):
        return None


# ── SFTP ───────────────────────────────────────────────────────────────────

# The host-key preference of every SSH consumer of one authentication scope.
# A server with several host keys presents the one its client negotiates, so
# the identity an operator confirms for a scope is only the identity each of
# its consumers verifies if they all ask in this order -- the packaged libssh2
# 1.11.1 preference, which the aria2 SFTP executor cannot change.
SSH_HOST_KEY_ALGORITHMS = (
    "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
    "ssh-ed25519", "rsa-sha2-512", "rsa-sha2-256", "ssh-rsa",
)


class _IdentityCheck(asyncssh.SSHClient):
    """Host identity is decided during key exchange, before any authentication."""

    def __init__(self, expected: str | None, client_key=None):
        self.expected = expected
        self.observed = ""
        self.client_key = client_key
        self.offered = False

    def validate_host_public_key(self, host, addr, port, key) -> bool:
        # SHA-1 of the host-key blob is the identity format the native executor
        # pins (its host-key option accepts only SHA-1 or MD5) and the canonical
        # challenge fact the operator confirmed; pinning an already confirmed
        # key relies on second-preimage resistance, which SHA-1 keeps.
        self.observed = hashlib.sha1(key.public_data).hexdigest()  # nosec B324
        return bool(self.expected) and self.observed == self.expected

    def public_key_auth_requested(self):
        # The one supplied key, offered once; never a local or agent key.
        if self.client_key is None or self.offered:
            return None
        self.offered = True
        return self.client_key


def _ssh_options(host_key_algorithms, timeout: float, *, key: bool = False) -> dict:
    # Nothing from the local account participates: no config files, no
    # known_hosts file (never read, never written), no agent, no local client
    # keys, no GSS, no X.509 trust store -- only the explicit identity decision
    # above. Exactly the supplied mechanism is offered: a password is never
    # used to answer keyboard-interactive or any other mechanism, and a
    # supplied key is only ever offered as that one public key.
    return dict(
        known_hosts=lambda _host, _addr, _port: ([], [], []), config=None, client_keys=None,
        agent_path=None, gss_host=None, x509_trusted_certs=None,
        preferred_auth=["publickey"] if key else ["password"],
        server_host_key_algs=list(host_key_algorithms), connect_timeout=timeout, login_timeout=timeout,
    )


def client_key(private_key: str, passphrase: str = ""):
    """THE one import of a supplied private key: OpenSSH (encrypted or not,
    via bcrypt's KDF), PKCS#8 (encrypted or not) and the traditional PEM
    formats. Raises ``ValueError`` for material that cannot sign -- a wrong or
    missing passphrase, or not a private key -- with no key text in it."""
    try:
        return asyncssh.import_private_key(private_key, passphrase or None)
    except (asyncssh.KeyImportError, asyncssh.KeyEncryptionError, ValueError, TypeError):
        raise ValueError("unusable private key") from None


@asynccontextmanager
async def ssh_connection(host: str, *, sock, host_key_algorithms, host_identity: str | None,
                         username: str, password: str = "", private_key: str = "", passphrase: str = "",
                         timeout: float, on_authenticated: Accepted = None):
    """THE one SSH identity-then-authentication step, over an already
    connected egress-guarded socket.

    Without ``host_identity`` the server's host key is only observed and
    refused as ``AccessRequired(observed)``; no credential is sent. With it, a
    presented key that differs fails closed during key exchange -- before
    authentication -- and only then is the password (or, with ``private_key``,
    that one key, imported by ``client_key``) offered. Key material that cannot
    sign is refused as ``unavailable("key_unusable")`` before any connection
    work. A server that never asks for the supplied mechanism
    is an unsupported method, never an access requirement: the supplied
    material cannot answer it. Every SSH consumer -- SFTP evidence and
    discovery, and a subprocess transport's SSH channel -- runs through this
    one step, so none of them can trust or authenticate differently.

    The connection exists only once user authentication succeeded: that is
    the server's definitive acceptance of the credential (``on_authenticated``),
    reported before any channel, listing or read."""
    key = None
    if private_key:
        try:
            key = client_key(private_key, passphrase)
        except ValueError:
            raise _SessionRefused(unavailable("key_unusable")) from None
    client = _IdentityCheck(host_identity, key)
    offered = []

    def supply_password():
        offered.append(True)
        return password or None

    credential = {} if key is not None else {"password": supply_password}
    try:
        connection, _ = await asyncssh.create_connection(
            lambda: client, host, sock=sock, username=username or "evidence", **credential,
            **_ssh_options(host_key_algorithms, timeout, key=key is not None))
    except asyncssh.HostKeyNotVerifiable:
        if not host_identity and client.observed:
            raise _SessionRefused(AccessRequired(client.observed)) from None
        raise _SessionRefused(unavailable("destination_rejected")) from None
    except asyncssh.PermissionDenied:
        if not offered and not client.offered:
            raise _SessionRefused(unavailable("auth_method_unsupported")) from None
        raise _SessionRefused(AccessRequired(host_identity or client.observed)) from None
    async with connection:
        if on_authenticated is not None:
            on_authenticated()
        yield connection


@asynccontextmanager
async def _sftp_session(address: str, *, connect: Connect, host_key_algorithms, host_identity: str | None,
                        username: str, password: str, timeout: float, on_authenticated: Accepted = None):
    """THE one SSH/SFTP session primitive: identity, then authentication
    (``ssh_connection``), then SFTP. Evidence and discovery both run on this
    one session, so they cannot trust differently."""
    host = str(urlsplit(address).hostname or "")
    sock = await connect(None)
    async with ssh_connection(host, sock=sock, host_key_algorithms=host_key_algorithms,
                              host_identity=host_identity, username=username, password=password,
                              timeout=timeout, on_authenticated=on_authenticated) as connection:
        try:
            sftp = await connection.start_sftp_client()
        except (asyncssh.ChannelOpenError, asyncssh.SFTPError):
            raise _SessionRefused(unavailable("sftp_unavailable")) from None
        async with sftp:
            yield sftp


async def sftp_fingerprint(address: str, *, connect: Connect, host_key_algorithms, host_identity: str | None = None,
                           username: str = "", password: str = "", sample_bytes: int = SAMPLE_BYTES,
                           timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                           on_authenticated: Accepted = None) -> Sample | AccessRequired:
    """Bounded SFTP evidence behind a confirmed server identity: STAT, then two
    bounded offset windows. ``host_key_algorithms`` is the caller's executor
    preference order, so the key observed here is the key execution verifies."""
    size = sample_size(sample_bytes)
    timeout = max(5.0, float(timeout_seconds))
    path = unquote(urlsplit(address).path)
    try:
        async with asyncio.timeout(timeout):
            async with _sftp_session(address, connect=connect, host_key_algorithms=host_key_algorithms,
                                     host_identity=host_identity, username=username, password=password,
                                     timeout=timeout, on_authenticated=on_authenticated) as sftp:
                try:
                    attributes = await sftp.stat(path)
                except asyncssh.SFTPError:
                    return unavailable("range_unsupported")
                if attributes.type != asyncssh.FILEXFER_TYPE_REGULAR or attributes.size is None:
                    return unavailable("range_unsupported")
                async with sftp.open(path, "rb") as handle:
                    async def window(offset: int, count: int) -> bytes:
                        return await handle.read(count, offset)
                    return await _offset_windows(int(attributes.size), window, size)
    except _SessionRefused as refused:
        return refused.outcome
    except TimeoutError:
        return unavailable("timeout")
    except PermissionError:
        return unavailable("destination_rejected")
    except (asyncssh.Error, ConnectionError, OSError, ValueError):
        return unavailable("sampler_unavailable")


def _listed_path(address: str) -> tuple[str, bool]:
    """``(path, home_relative)`` for one SFTP directory address.

    ``/~/`` is the SSH URI form for the login (home) directory. It is sent to
    the server as a RELATIVE path, which SFTP resolves against that directory
    by protocol definition (``realpath``) -- never expanded here, never by a
    shell, and never for another user's home."""
    path = unquote(urlsplit(address).path) or "/"
    if path == "/~" or path.startswith("/~/"):
        return path[3:] or ".", True
    return path, False


# SFTP status codes that name a definitive refusal of the discovered path.
_LISTING_STATUS = {
    asyncssh.FX_NO_SUCH_FILE: "not_found", asyncssh.FX_NO_SUCH_PATH: "not_found",
    asyncssh.FX_PERMISSION_DENIED: "permission_denied", asyncssh.FX_NOT_A_DIRECTORY: "not_a_directory",
}


async def sftp_discovery(address: str, *, connect: Connect, host_key_algorithms, host_identity: str | None = None,
                         username: str = "", password: str = "",
                         timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, on_authenticated: Accepted = None,
                         ) -> Listing | RemoteFile | ListingRefused | AccessRequired | Sample:
    """Classify one SFTP path, on the one session primitive, read-only.

    The server's own STAT answers what the path is: a regular file is
    reported with its size; a directory's immediate entries are read and only
    regular files returned -- a subdirectory is never entered and a symbolic
    link (to a file or a directory) is never followed. Returns
    ``AccessRequired`` exactly as evidence does, a typed ``ListingRefused``
    for a definitive refusal of the path, or an unavailable ``Sample`` fact
    for anything transient."""
    timeout = max(5.0, float(timeout_seconds))
    path, home_relative = _listed_path(address)
    try:
        async with asyncio.timeout(timeout):
            async with _sftp_session(address, connect=connect, host_key_algorithms=host_key_algorithms,
                                     host_identity=host_identity, username=username, password=password,
                                     timeout=timeout, on_authenticated=on_authenticated) as sftp:
                try:
                    if home_relative:
                        path = await sftp.realpath(path)
                    attributes = await sftp.stat(path)
                    if attributes.type == asyncssh.FILEXFER_TYPE_REGULAR:
                        return RemoteFile(int(attributes.size or 0), path if isinstance(path, str) else path.decode())
                    if attributes.type != asyncssh.FILEXFER_TYPE_DIRECTORY:
                        return ListingRefused("unsupported_type")
                    names = await sftp.readdir(path)
                except asyncssh.SFTPError as exc:
                    reason = _LISTING_STATUS.get(exc.code)
                    return ListingRefused(reason) if reason else unavailable("sampler_unavailable")
                if len(names) > MAX_LISTED_ENTRIES:
                    return ListingRefused("too_many_entries")
                entries = []
                for item in names:
                    name = item.filename if isinstance(item.filename, str) else item.filename.decode("utf-8", "replace")
                    # READDIR attributes describe the entry itself (lstat), so a
                    # symbolic link is reported as a link and never as its target.
                    if name in {".", ".."} or "/" in name or item.attrs.type != asyncssh.FILEXFER_TYPE_REGULAR:
                        continue
                    entries.append((name, int(item.attrs.size or 0)))
                return Listing(tuple(sorted(entries)), path if isinstance(path, str) else path.decode())
    except _SessionRefused as refused:
        return refused.outcome
    except TimeoutError:
        return unavailable("timeout")
    except PermissionError:
        return unavailable("destination_rejected")
    except (asyncssh.Error, ConnectionError, OSError, ValueError):
        return unavailable("sampler_unavailable")
