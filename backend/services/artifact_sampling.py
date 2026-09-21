"""The one canonical bounded content-evidence owner: HTTP(S), FTP and SFTP.

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
from dataclasses import dataclass
import hashlib
import logging
import re
from typing import Awaitable, Callable
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp
import asyncssh

from services import network_safety
from transfers.models import FingerprintKind


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


@dataclass(frozen=True)
class AccessRequired:
    """The transport definitively requires access input before it yields evidence.

    ``server_identity`` is empty for plain authentication, or the SHA-1 of the
    host key the server presented, which must be confirmed before any
    credential is offered to it.
    """
    server_identity: str = ""


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


def _origin(uri: str) -> tuple[str, str, int]:
    parsed = urlsplit(uri)
    return parsed.scheme.casefold(), str(parsed.hostname or "").casefold(), int(
        parsed.port or network_safety.default_destination_port(parsed.scheme))


async def _range_request(session, uri: str, headers: dict, *, max_redirects: int = 3):
    current = uri
    current_headers = dict(headers)
    prior_origin = _origin(uri)
    redirected = False
    for hop in range(max_redirects + 1):
        try:
            validated = await network_safety.validate_resolved_public_destination(current)
        except network_safety.DestinationLookupError:
            return None, "dns_failure"
        except network_safety.UnsafeDestinationError:
            return None, "destination_rejected"
        response = await session.get(validated, headers=current_headers, allow_redirects=False)
        if not (300 <= response.status < 400):
            return response, "redirect" if redirected else ""
        location = str(response.headers.get("Location") or "").strip()
        response.release()
        if not location or hop >= max_redirects:
            return None, "redirect"
        next_uri = urljoin(validated, location)
        try:
            network_safety.validate_provider_download_url(next_uri, context="redirect target",
                                                          schemes=SAMPLED_FINGERPRINT_SCHEMES)
        except network_safety.UnsafeDestinationError:
            return None, "destination_rejected"
        next_origin = _origin(next_uri)
        if next_origin != prior_origin:
            current_headers = {key: value for key, value in current_headers.items()
                               if key.casefold() in {"range", "accept-encoding"}}
        prior_origin = next_origin
        current = next_uri
        redirected = True
    return None, "redirect"


async def sampled_public_artifact_fingerprint(
    uri: str,
    *,
    sample_bytes: int = SAMPLE_BYTES,
    timeout_seconds: float = 20.0,
    headers: dict | None = None,
    expected_bytes: int = 0,
) -> Sample | AccessRequired:
    """Return bounded structured content evidence for a public HTTP(S) capability.

    ``expected_bytes`` is retained for caller compatibility but is deliberately
    not an identity gate. The sampler reports the payload size it discovers;
    the Universal Core owns reported-size plausibility policy. Windows and
    digests are the shared ``services.artifact_sampling`` definition, so the
    same bytes fingerprint identically over every transport. A definitive
    Basic authentication challenge on the first window is reported as the
    typed ``AccessRequired`` fact for the executor to translate.
    """
    # The sampler speaks HTTP(S) only. A transport it cannot sample is refused
    # here, at its own boundary, so no other caller has to know that.
    if urlsplit(str(uri or "")).scheme.casefold() not in SAMPLED_FINGERPRINT_SCHEMES:
        return unavailable("destination_rejected")
    try:
        validated = await network_safety.validate_resolved_public_destination(uri)
    except network_safety.DestinationLookupError:
        return unavailable("dns_failure")
    except network_safety.UnsafeDestinationError:
        return unavailable("destination_rejected")

    sample_bytes = sample_size(sample_bytes)
    timeout = aiohttp.ClientTimeout(total=max(5.0, float(timeout_seconds)))
    base_headers = {**(headers or {}), "Accept-Encoding": "identity"}
    connector = aiohttp.TCPConnector(resolver=network_safety.PublicDestinationResolver(), use_dns_cache=False)
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            first_headers = {**base_headers, "Range": f"bytes=0-{sample_bytes - 1}"}
            response, redirect_reason = await _range_request(session, validated, first_headers)
            if response is None:
                return unavailable(redirect_reason or "sampler_unavailable")
            try:
                if _basic_challenge(response):
                    return AccessRequired()
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
            response, last_redirect_reason = await _range_request(session, validated, last_headers)
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


def _ftp_path(address: str) -> tuple[list[str], str]:
    """aria2's own FTP path semantics: login directory, then each decoded directory segment, then the file."""
    segments = [unquote(part) for part in urlsplit(address).path.split("/") if part]
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


async def ftp_fingerprint(address: str, *, connect: Connect, username: str, password: str,
                          sample_bytes: int = SAMPLE_BYTES,
                          timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> Sample | AccessRequired:
    """Bounded FTP evidence: binary type, SIZE, then REST/RETR offset windows.

    Only a 530 answer to the login itself is authentication evidence; a
    missing path, a permission refusal or any other reply is an ordinary
    unavailable fact. Control and every passive data connection come from
    ``connect`` (the egress guard); the server's advertised data address is
    ignored and only its port is used, on the same authorized host."""
    size = sample_size(sample_bytes)
    writer = None
    try:
        directories, filename = _ftp_path(address)
        async with asyncio.timeout(max(5.0, float(timeout_seconds))):
            reader, writer = await asyncio.open_connection(sock=await connect(None))
            control = _FtpControl(reader, writer)
            if (await control.reply())[0] != 220:
                return unavailable("sampler_unavailable")
            code, _ = await control.command("USER", username)
            if code == 331:
                code, _ = await control.command("PASS", password)
            if code == 530:
                return AccessRequired()
            if code != 230:
                return unavailable("range_unsupported")
            if (await control.command("TYPE", "I"))[0] != 200:
                return unavailable("range_unsupported")
            code, text = await control.command("PWD")
            home = re.match(r'257 "((?:[^"]|"")*)"', text) if code == 257 else None
            if home is not None and (await control.command("CWD", home.group(1).replace('""', '"')))[0] != 250:
                return unavailable("range_unsupported")
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
                code, text = await control.command("PASV")
                port = _passive_port(code, text)
                if port is None:
                    code, text = await control.command("EPSV")
                    port = _passive_port(code, text)
                if port is None:
                    raise _WindowUnavailable("range_unsupported")
                data_reader, data_writer = await asyncio.open_connection(sock=await connect(port))
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
    except TimeoutError:
        return unavailable("timeout")
    except PermissionError:
        return unavailable("destination_rejected")
    except (ConnectionError, OSError, ValueError, asyncio.IncompleteReadError):
        return unavailable("sampler_unavailable")
    finally:
        if writer is not None:
            writer.close()


# ── SFTP ───────────────────────────────────────────────────────────────────

class _IdentityCheck(asyncssh.SSHClient):
    """Host identity is decided during key exchange, before any authentication."""

    def __init__(self, expected: str | None):
        self.expected = expected
        self.observed = ""

    def validate_host_public_key(self, host, addr, port, key) -> bool:
        # SHA-1 of the host-key blob is the identity format the native executor
        # pins (its host-key option accepts only SHA-1 or MD5) and the canonical
        # challenge fact the operator confirmed; pinning an already confirmed
        # key relies on second-preimage resistance, which SHA-1 keeps.
        self.observed = hashlib.sha1(key.public_data).hexdigest()  # nosec B324
        return bool(self.expected) and self.observed == self.expected


def _ssh_options(host_key_algorithms, timeout: float) -> dict:
    # Nothing from the local account participates: no config files, no
    # known_hosts file (never read, never written), no agent, no client keys,
    # no GSS, no X.509 trust store -- only the explicit identity decision above.
    return dict(
        known_hosts=lambda _host, _addr, _port: ([], [], []), config=None, client_keys=None,
        agent_path=None, gss_host=None, x509_trusted_certs=None, preferred_auth=["password"],
        server_host_key_algs=list(host_key_algorithms), connect_timeout=timeout, login_timeout=timeout,
    )


async def sftp_fingerprint(address: str, *, connect: Connect, host_key_algorithms, host_identity: str | None = None,
                           username: str = "", password: str = "", sample_bytes: int = SAMPLE_BYTES,
                           timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> Sample | AccessRequired:
    """Bounded SFTP evidence behind a confirmed server identity.

    Without ``host_identity`` the server's host key is only observed and
    returned for confirmation; no credential is sent. With it, a presented key
    that differs fails closed during key exchange -- before authentication --
    and only then are the credentials offered, the file STATed and two bounded
    offset windows read. ``host_key_algorithms`` is the caller's executor
    preference order, so the key observed here is the key execution verifies."""
    size = sample_size(sample_bytes)
    timeout = max(5.0, float(timeout_seconds))
    path = unquote(urlsplit(address).path)
    host = str(urlsplit(address).hostname or "")
    client = _IdentityCheck(host_identity)
    try:
        async with asyncio.timeout(timeout):
            sock = await connect(None)
            try:
                connection, _ = await asyncssh.create_connection(
                    lambda: client, host, sock=sock, username=username or "evidence", password=password or None,
                    **_ssh_options(host_key_algorithms, timeout))
            except asyncssh.HostKeyNotVerifiable:
                if not host_identity and client.observed:
                    return AccessRequired(client.observed)
                return unavailable("destination_rejected")
            except asyncssh.PermissionDenied:
                return AccessRequired(host_identity or client.observed)
            async with connection, connection.start_sftp_client() as sftp:
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
    except TimeoutError:
        return unavailable("timeout")
    except PermissionError:
        return unavailable("destination_rejected")
    except (asyncssh.Error, ConnectionError, OSError, ValueError):
        return unavailable("sampler_unavailable")
