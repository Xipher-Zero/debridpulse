"""rsync-native facts and their neutral translation.

Every mapping here was characterized against the packaged rsync 3.4.1
(protocol 32) client with a real 3.4.1 daemon and a real rsync over SSH, not
taken from folklore. A native exit code is evidence only together with the
diagnostic that accompanies it: rsync reports many different outcomes under
code 5 (daemon refusals) and code 23 (per-file errors), and its ``[sender]`` /
``[receiver]`` prefix is what says whether a failure was remote or local.
Recovery and lifecycle policy are universal-core responsibilities.
"""
from __future__ import annotations

import re

from transfers.errors import (
    Category as C, Confidence as CF, Domain as D, EvidenceBasis as E, NormalizedError, Origin as O,
    Permanence as P, Retryability as T, Stage, safe_diagnostic,
)

# rsync expands wildcards in a source argument, both in a daemon and in the
# server it starts over SSH: "x[1].txt" silently selects "x1.txt". A backslash
# makes each of these characters (and itself) literal.
_GLOB = re.compile(r"([\\*?\[\]])")


def literal_path(path: str) -> str:
    """A remote path exactly as named, never a pattern."""
    return _GLOB.sub(r"\\\1", path)


# ── listings (``--list-only --no-h -8``) ──────────────────────────────────────

_LISTING = re.compile(r"^([-dlpcbs])[-rwxsStT]{9} +([0-9]+) [0-9]{4}/[0-9]{2}/[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} (.+)$")
_ESCAPE = re.compile(rb"\\#([0-7]{3})")


class ListingUnusable(ValueError):
    """A listing line or name that cannot be read back exactly."""


def listed_name(raw: bytes) -> str:
    """Decode one listed name: rsync writes ``\\#ooo`` for each byte it escapes
    and escapes a literal backslash only where it precedes ``#ddd``, so this
    is exact. A name that is not UTF-8 text is refused, never approximated."""
    value = _ESCAPE.sub(lambda match: bytes((int(match.group(1), 8),)), raw)
    try:
        name = value.decode("utf-8")
    except UnicodeDecodeError:
        raise ListingUnusable("undecodable name") from None
    if not name or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ListingUnusable("unsupported name")
    return name


def listing_entries(output: bytes) -> list[tuple[str, int, str]]:
    """``(type, size, name)`` per listed object; type ``-`` is a regular file,
    ``d`` a directory, anything else (links, devices, FIFOs, sockets) a
    special object that is never a member."""
    entries = []
    for line in output.split(b"\n"):
        if not line:
            continue
        # latin-1 maps every byte to one character, so the name's bytes survive
        # the match exactly and are decoded once, by ``listed_name``.
        text = line.decode("latin-1")
        match = _LISTING.match(text)
        if match is None:
            raise ListingUnusable("unrecognized listing line")
        kind, size, name = match.group(1), int(match.group(2)), match.group(3)
        entries.append((kind, size, listed_name(name.encode("latin-1"))))
    return entries


_ROOT_LINE = re.compile(rb"^([^\t ][^\t]*?)( *)\t")


def listed_roots(output: bytes) -> list[str]:
    """The named roots (modules) a daemon advertises. The module list cannot be
    requested without the daemon's free-form message of the day in front of it
    (``--no-motd`` suppresses both), so only lines of the list's exact native
    shape -- the name left-justified to 15 columns, then a tab and the
    comment -- are roots; anything else is the server's message."""
    roots = []
    for line in output.split(b"\n"):
        match = _ROOT_LINE.match(line)
        if match is None:
            continue
        name, padding = match.group(1), match.group(2)
        if len(name) + len(padding) != max(15, len(name)) or name.endswith(b" "):
            continue
        roots.append(listed_name(name))
    return roots


def depth_options(depth) -> tuple[str, ...]:
    """The listing options that make rsync itself enumerate exactly the
    neutral ``DiscoveryDepth`` below the listed directory.

    ``CURRENT`` is the directory's own listing (no recursion). ``UNLIMITED``
    is the whole tree (``-r``). A finite depth of N levels recurses with ONE
    anchored exclude of every directory N+1 levels down: a pattern ending in
    ``/`` matches only directories, ``*`` never crosses a ``/``, and the
    leading ``/`` anchors it at the transfer root -- so the files of levels
    0..N are listed and each deeper directory is excluded by the sender before
    it is opened (rsync never descends into an excluded directory). Nothing is
    listed for DebridPulse to discard afterwards."""
    if depth.unlimited:
        return ("-r",)
    if not depth.levels:
        return ()
    return ("-r", "--exclude=/" + "*/" * (depth.levels + 1))


# ── outcomes ──────────────────────────────────────────────────────────────────

_ERROR_LINE = re.compile(r"@ERROR: (.+)")

# Daemon refusals (exit 5): the "@ERROR:" line is the fact.
_DAEMON = (
    (re.compile(r"^max connections \(\d+\) reached"),
     # The server's own connection limit: an external capacity fact (the daemon
     # refuses at once; it never queues or waits), not a failure of the source.
     (D.NETWORK, C.CONCURRENCY_LIMITED, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY)),
    (re.compile(r"^auth failed on module "),
     (D.RESOLUTION, C.AUTHENTICATION_FAILED, T.AFTER_REAUTH, O.REMOTE_SOURCE, P.UNKNOWN)),
    (re.compile(r"^Unknown module "),
     (D.RESOLUTION, C.SOURCE_NOT_FOUND, T.NEVER, O.REMOTE_SOURCE, P.PERMANENT)),
    (re.compile(r"^access denied to "),
     (D.RESOLUTION, C.AUTHORIZATION_FAILED, T.NEVER, O.REMOTE_SOURCE, P.PERMANENT)),
)

# Per-file errors (exit 23): the side and the errno decide.
_SENDER_MISSING = re.compile(r"\[sender\] (?:link_stat|change_dir) .* failed: No such file or directory \(2\)")
_SENDER_DENIED = re.compile(r"\[sender\] .*: Permission denied \(13\)")
_RECEIVER = re.compile(r"\[receiver\] .*\((\d+)\)")
_LOCAL_ERRNO = {
    "28": (D.LOCAL_RESOURCE, C.DISK_FULL, T.AFTER_RESOURCE_CHANGE, O.LOCAL_SYSTEM, P.TEMPORARY),
    "122": (D.LOCAL_RESOURCE, C.DISK_FULL, T.AFTER_RESOURCE_CHANGE, O.LOCAL_SYSTEM, P.TEMPORARY),
    "13": (D.LOCAL_RESOURCE, C.PERMISSION_DENIED, T.AFTER_RESOURCE_CHANGE, O.LOCAL_SYSTEM, P.TEMPORARY),
    "30": (D.LOCAL_RESOURCE, C.DOWNLOAD_STORAGE_READ_ONLY, T.AFTER_RESOURCE_CHANGE, O.LOCAL_SYSTEM, P.TEMPORARY),
}
_PROXY_REFUSED = re.compile(r"bad response from proxy -- HTTP/1\.[01] (\d{3})")
_CONNECT_FAILED = re.compile(r"failed to connect to .*\((\d+)\)")

_EXIT = {
    "1": (D.EXECUTOR, C.INVALID_CONFIGURATION, T.NEVER, O.EXECUTOR, P.PERMANENT),
    "2": (D.NETWORK, C.PROTOCOL_ERROR, T.NEVER, O.REMOTE_SOURCE, P.PERMANENT),
    "3": (D.LOCAL_RESOURCE, C.LOCAL_IO_FAILURE, T.AFTER_RESOURCE_CHANGE, O.LOCAL_SYSTEM, P.UNKNOWN),
    "4": (D.EXECUTOR, C.UNSUPPORTED_CAPABILITY, T.NEVER, O.EXECUTOR, P.PERMANENT),
    "5": (D.NETWORK, C.PROTOCOL_ERROR, T.BACKOFF, O.REMOTE_SOURCE, P.UNKNOWN),
    "10": (D.NETWORK, C.CONNECTION_FAILED, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY),
    "11": (D.LOCAL_RESOURCE, C.LOCAL_IO_FAILURE, T.AFTER_RESOURCE_CHANGE, O.LOCAL_SYSTEM, P.UNKNOWN),
    "12": (D.NETWORK, C.REMOTE_RESET, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY),
    "13": (D.EXECUTOR, C.UNMAPPED_EXECUTOR_ERROR, T.UNKNOWN, O.EXECUTOR, P.UNKNOWN),
    "14": (D.EXECUTOR, C.UNMAPPED_EXECUTOR_ERROR, T.BACKOFF, O.EXECUTOR, P.TEMPORARY),
    "15": (D.EXECUTOR, C.TRANSFER_INTERRUPTED, T.BACKOFF, O.EXECUTOR, P.TEMPORARY),
    "16": (D.EXECUTOR, C.TRANSFER_INTERRUPTED, T.BACKOFF, O.EXECUTOR, P.TEMPORARY),
    "19": (D.EXECUTOR, C.TRANSFER_INTERRUPTED, T.BACKOFF, O.EXECUTOR, P.TEMPORARY),
    "20": (D.EXECUTOR, C.TRANSFER_INTERRUPTED, T.BACKOFF, O.EXECUTOR, P.TEMPORARY),
    "21": (D.EXECUTOR, C.UNMAPPED_EXECUTOR_ERROR, T.BACKOFF, O.EXECUTOR, P.TEMPORARY),
    "22": (D.LOCAL_RESOURCE, C.LOCAL_RESOURCE_EXHAUSTED, T.AFTER_RESOURCE_CHANGE, O.LOCAL_SYSTEM, P.TEMPORARY),
    "23": (D.EXECUTOR, C.TRANSFER_FAILED, T.BACKOFF, O.REMOTE_SOURCE, P.UNKNOWN),
    # The file vanished between listing and transfer.
    "24": (D.RESOLUTION, C.SOURCE_NOT_FOUND, T.AFTER_RERESOLUTION, O.REMOTE_SOURCE, P.UNKNOWN),
    "30": (D.NETWORK, C.READ_TIMEOUT, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY),
    "35": (D.NETWORK, C.CONNECTION_TIMEOUT, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY),
    # The remote shell could not run rsync (rsync over SSH, relayed status).
    "127": (D.NETWORK, C.PROTOCOL_ERROR, T.NEVER, O.REMOTE_SOURCE, P.PERMANENT),
}


# A daemon module that requires a login makes the client read the password
# (``--password-file=-``); an explicitly empty one is refused before anything
# is sent. Only an authenticating module ever asks, so this is the definitive
# "a login is required and none was supplied" fact.
_PASSWORD_REQUIRED = re.compile(r"ERROR: failed to read a password from -")


def _spec_for(code: str, diagnostic: str):
    if code == "1" and _PASSWORD_REQUIRED.search(diagnostic):
        return (D.RESOLUTION, C.CREDENTIAL_MISSING, T.AFTER_REAUTH, O.REMOTE_SOURCE, P.UNKNOWN), E.DIAGNOSTIC
    if code == "5":
        match = _ERROR_LINE.search(diagnostic)
        if match:
            for pattern, spec in _DAEMON:
                if pattern.search(match.group(1)):
                    return spec, E.DIAGNOSTIC
    if code == "23":
        if _SENDER_MISSING.search(diagnostic):
            return (D.RESOLUTION, C.SOURCE_NOT_FOUND, T.NEVER, O.REMOTE_SOURCE, P.PERMANENT), E.DIAGNOSTIC
        if _SENDER_DENIED.search(diagnostic):
            return (D.RESOLUTION, C.AUTHORIZATION_FAILED, T.NEVER, O.REMOTE_SOURCE, P.PERMANENT), E.DIAGNOSTIC
        local = _RECEIVER.search(diagnostic)
        if local:
            return _LOCAL_ERRNO.get(local.group(1), (D.LOCAL_RESOURCE, C.LOCAL_IO_FAILURE, T.AFTER_RESOURCE_CHANGE,
                                                     O.LOCAL_SYSTEM, P.UNKNOWN)), E.DIAGNOSTIC
    if code == "10":
        proxy = _PROXY_REFUSED.search(diagnostic)
        if proxy:
            # The egress guard refused the route: 407 is a credential the guard
            # itself did not sign, 403 its own address policy (or an upstream
            # it could not reach), 502 an approved server that actively
            # refused -- nothing serves that port -- and 504 one that did not
            # answer within the route's Connection Timeout.
            if proxy.group(1) == "407":
                return (D.SECURITY, C.EGRESS_POLICY_VIOLATION, T.NEVER, O.SECURITY_POLICY, P.PERMANENT), E.DIAGNOSTIC
            if proxy.group(1) == "502":
                return (D.NETWORK, C.CONNECTION_REFUSED, T.BACKOFF, O.REMOTE_SOURCE, P.UNKNOWN), E.DIAGNOSTIC
            if proxy.group(1) == "504":
                return (D.NETWORK, C.CONNECTION_TIMEOUT, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY), E.DIAGNOSTIC
            return (D.NETWORK, C.CONNECTION_FAILED, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY), E.DIAGNOSTIC
        refused = _CONNECT_FAILED.search(diagnostic)
        if refused and refused.group(1) == "110":
            return (D.NETWORK, C.CONNECTION_TIMEOUT, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY), E.DIAGNOSTIC
    spec = _EXIT.get(code)
    return (spec, E.NATIVE_CODE) if spec else (None, E.UNKNOWN)


def native_failure(code: object, diagnostic: object = "", *, stage=Stage.EXECUTION, secrets=()) -> NormalizedError:
    native = str(code or "")
    text = str(diagnostic or "")
    spec, evidence = _spec_for(native, text)
    confidence = CF.HIGH if spec is not None else CF.UNKNOWN
    if spec is None:
        spec = (D.EXECUTOR, C.UNMAPPED_EXECUTOR_ERROR, T.UNKNOWN, O.EXECUTOR, P.UNKNOWN)
    domain, category, retryability, origin, permanence = spec
    return NormalizedError(
        domain, category, stage, retryability=retryability, origin=origin, permanence=permanence,
        integration_id="rsync", native_code=native, diagnostic=safe_diagnostic(text, secrets=tuple(secrets)),
        confidence=confidence, evidence_basis=evidence,
    )


# ── SSH channel outcomes (typed status records, never free text) ─────────────

CHANNEL_FAILURES = {
    "identity_changed": (D.SECURITY, C.HOST_KEY_FAILURE, T.NEVER, O.SECURITY_POLICY, P.PERMANENT),
    "method_unsupported": (D.REQUEST, C.UNSUPPORTED_CAPABILITY, T.NEVER, O.REMOTE_SOURCE, P.PERMANENT),
    "key_unusable": (D.REQUEST, C.CREDENTIAL_INVALID, T.AFTER_REAUTH, O.USER, P.UNKNOWN),
    "authentication_rejected": (D.RESOLUTION, C.AUTHENTICATION_FAILED, T.AFTER_REAUTH, O.REMOTE_SOURCE, P.UNKNOWN),
    "identity_required": (D.SECURITY, C.HOST_KEY_FAILURE, T.AFTER_REAUTH, O.SECURITY_POLICY, P.UNKNOWN),
    "destination_rejected": (D.SECURITY, C.DESTINATION_BLOCKED, T.NEVER, O.SECURITY_POLICY, P.PERMANENT),
    "timeout": (D.NETWORK, C.CONNECTION_TIMEOUT, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY),
    "connection_failed": (D.NETWORK, C.CONNECTION_FAILED, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY),
}


def channel_failure(outcome: str, *, stage=Stage.EXECUTION, diagnostic: str = "") -> NormalizedError:
    spec = CHANNEL_FAILURES.get(outcome, (D.NETWORK, C.CONNECTION_FAILED, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY))
    domain, category, retryability, origin, permanence = spec
    return NormalizedError(domain, category, stage, retryability=retryability, origin=origin, permanence=permanence,
                           integration_id="rsync", native_code=f"ssh:{outcome}", diagnostic=diagnostic,
                           confidence=CF.HIGH, evidence_basis=E.STRUCTURED)
