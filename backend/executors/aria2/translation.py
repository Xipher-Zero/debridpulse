"""aria2-native state and factual failure translation.

The numeric meanings are documented at
https://aria2.github.io/manual/en/html/aria2c.html#exit-status . Specific native
codes are semantic evidence. Generic code 1 is interpreted only through a small,
auditable diagnostic table; HTTP status evidence from code 22 is interpreted
only through its strict numeric ``status=NNN`` field. Recovery and lifecycle
policy are universal-core responsibilities.
"""
from __future__ import annotations

import asyncio
import re
import ssl

import aiohttp

from executors.aria2.client import Aria2ConnectionError, Aria2RPCError
from transfers.errors import (
    Category as C, Confidence as CF, Domain as D, EvidenceBasis as E,
    NormalizedError, Origin as O, Permanence as P, Retryability as T, Stage,
    TransferError, safe_diagnostic,
)
from transfers.models import ExecutionHandle, ExecutionObservation, ExecutionState, TransferProgress


# Values are facts only: semantic domain/category, factual retryability legacy
# property, provenance, and permanence. Recovery actions intentionally do not
# exist in this adapter table.
_ERRORS = {
    "2": (D.NETWORK, C.READ_TIMEOUT, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY),
    "3": (D.RESOLUTION, C.SOURCE_NOT_FOUND, T.AFTER_RERESOLUTION, O.REMOTE_SOURCE, P.UNKNOWN),
    "4": (D.RESOLUTION, C.SOURCE_NOT_FOUND, T.AFTER_RERESOLUTION, O.REMOTE_SOURCE, P.UNKNOWN),
    "5": (D.EXECUTOR, C.TRANSFER_STALLED, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY),
    "6": (D.NETWORK, C.CONNECTION_FAILED, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY),
    "7": (D.EXECUTOR, C.TRANSFER_INTERRUPTED, T.BACKOFF, O.EXECUTOR, P.UNKNOWN),
    "8": (D.NETWORK, C.REMOTE_READ_FAILED, T.AFTER_RERESOLUTION, O.REMOTE_SOURCE, P.UNKNOWN),
    "9": (D.LOCAL_RESOURCE, C.DISK_FULL, T.AFTER_RESOURCE_CHANGE, O.LOCAL_SYSTEM, P.PERMANENT),
    "10": (D.INTEGRITY, C.CONTENT_INVALID, T.AFTER_RESOURCE_CHANGE, O.LOCAL_SYSTEM, P.PERMANENT),
    "11": (D.LIFECYCLE, C.LOCAL_PATH_CONFLICT, T.AFTER_RESOURCE_CHANGE, O.EXECUTOR, P.PERMANENT),
    "12": (D.LIFECYCLE, C.RESOURCE_STATE_CONFLICT, T.AFTER_RESOURCE_CHANGE, O.EXECUTOR, P.PERMANENT),
    "13": (D.LOCAL_RESOURCE, C.LOCAL_PATH_CONFLICT, T.AFTER_RESOURCE_CHANGE, O.LOCAL_SYSTEM, P.PERMANENT),
    "14": (D.LOCAL_RESOURCE, C.LOCAL_IO_FAILURE, T.AFTER_RESOURCE_CHANGE, O.LOCAL_SYSTEM, P.PERMANENT),
    "15": (D.LOCAL_RESOURCE, C.PATH_UNAVAILABLE, T.AFTER_RESOURCE_CHANGE, O.LOCAL_SYSTEM, P.PERMANENT),
    "16": (D.LOCAL_RESOURCE, C.LOCAL_IO_FAILURE, T.AFTER_RESOURCE_CHANGE, O.LOCAL_SYSTEM, P.PERMANENT),
    "17": (D.LOCAL_RESOURCE, C.LOCAL_IO_FAILURE, T.AFTER_RESOURCE_CHANGE, O.LOCAL_SYSTEM, P.PERMANENT),
    "18": (D.LOCAL_RESOURCE, C.PATH_UNAVAILABLE, T.AFTER_RESOURCE_CHANGE, O.LOCAL_SYSTEM, P.PERMANENT),
    "19": (D.NETWORK, C.DNS_FAILURE, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY),
    "20": (D.INTEGRITY, C.CONTENT_INVALID, T.NEVER, O.REMOTE_SOURCE, P.PERMANENT),
    "21": (D.NETWORK, C.REMOTE_READ_FAILED, T.AFTER_RERESOLUTION, O.REMOTE_SOURCE, P.UNKNOWN),
    "22": (D.NETWORK, C.PROTOCOL_ERROR, T.UNKNOWN, O.REMOTE_SOURCE, P.UNKNOWN),
    "23": (D.SECURITY, C.UNSAFE_REDIRECT, T.NEVER, O.SECURITY_POLICY, P.PERMANENT),
    "24": (D.RESOLUTION, C.CANDIDATE_EXPIRED, T.AFTER_RERESOLUTION, O.REMOTE_SOURCE, P.PERMANENT),
    "25": (D.INTEGRITY, C.CONTENT_INVALID, T.NEVER, O.REMOTE_SOURCE, P.PERMANENT),
    "26": (D.INTEGRITY, C.CONTENT_INVALID, T.NEVER, O.REMOTE_SOURCE, P.PERMANENT),
    "27": (D.REQUEST, C.INVALID_REQUEST, T.NEVER, O.USER, P.PERMANENT),
    "28": (D.EXECUTOR, C.INVALID_CONFIGURATION, T.NEVER, O.EXECUTOR, P.PERMANENT),
    "29": (D.NETWORK, C.SOURCE_TEMPORARILY_UNAVAILABLE, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY),
    "30": (D.INTERNAL, C.EXECUTOR_PROTOCOL_VIOLATION, T.UNKNOWN, O.EXECUTOR, P.UNKNOWN),
    "32": (D.INTEGRITY, C.CHECKSUM_MISMATCH, T.AFTER_RERESOLUTION, O.REMOTE_SOURCE, P.PERMANENT),
}

_CODE1_DIAGNOSTICS = (
    (re.compile(r"\bFailed to receive data\b[\s\S]{0,160}\bError decoding the received TLS packet\b", re.I),
     D.NETWORK, C.TLS_FAILURE, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY),
    (re.compile(r"\bSSL routines::unexpected eof while reading\b", re.I),
     D.NETWORK, C.TLS_FAILURE, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY),
    (re.compile(r"\b(?:TLS|SSL)(?:/SSL)?\b.{0,80}\b(?:receive|read|record|decode)(?:d|ing)?\b.{0,80}\b(?:error|fail(?:ed|ure)?|unexpected eof)\b", re.I),
     D.NETWORK, C.TLS_FAILURE, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY),
    (re.compile(r"\bconnection reset by peer\b|\bECONNRESET\b", re.I),
     D.NETWORK, C.REMOTE_RESET, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY),
    (re.compile(r"\b(?:premature|unexpected) EOF\b", re.I),
     D.NETWORK, C.REMOTE_READ_FAILED, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY),
)
_HTTP_STATUS = re.compile(r"\bstatus\s*=\s*(\d{3})\b", re.I)

_STATES = {
    "active": ExecutionState.TRANSFERRING, "waiting": ExecutionState.QUEUED,
    "paused": ExecutionState.PAUSED, "complete": ExecutionState.SUCCEEDED,
    "error": ExecutionState.FAILED, "removed": ExecutionState.CANCELLED,
}


def _code1_failure(message: object):
    text = str(message or "")
    for pattern, domain, category, retryability, origin, permanence in _CODE1_DIAGNOSTICS:
        if pattern.search(text):
            return domain, category, retryability, origin, permanence
    return None


def _http_status_failure(message: object):
    """Extract only strict numeric HTTP status evidence; never recovery policy."""
    match = _HTTP_STATUS.search(str(message or ""))
    if match is None:
        return None
    status = int(match.group(1))
    if status == 429:
        return D.NETWORK, C.RATE_LIMITED, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY
    if 500 <= status <= 599:
        return D.NETWORK, C.SOURCE_TEMPORARILY_UNAVAILABLE, T.BACKOFF, O.REMOTE_SOURCE, P.TEMPORARY
    return None


def native_failure(code: object, message: object = "", *, stage=Stage.EXECUTION, secrets=()) -> NormalizedError:
    native = str(code or "")
    spec = _ERRORS.get(native)
    confidence = CF.HIGH
    evidence = E.NATIVE_CODE
    if native == "1":
        spec = _code1_failure(message)
        if spec is not None:
            confidence = CF.MEDIUM
            evidence = E.DIAGNOSTIC
    elif native == "22":
        status_spec = _http_status_failure(message)
        if status_spec is not None:
            spec = status_spec
            confidence = CF.HIGH
            evidence = E.DIAGNOSTIC
    if spec is None:
        spec = (D.EXECUTOR, C.UNMAPPED_EXECUTOR_ERROR, T.UNKNOWN, O.EXECUTOR, P.UNKNOWN)
        confidence = CF.UNKNOWN
        evidence = E.UNKNOWN
    domain, category, retryability, origin, permanence = spec
    return NormalizedError(
        domain, category, stage, retryability=retryability,
        origin=origin, permanence=permanence,
        integration_id="aria2", native_code=native,
        diagnostic=safe_diagnostic(message, secrets=tuple(secrets)),
        confidence=confidence, evidence_basis=evidence,
    )


def exception_failure(exc: Exception, *, stage=Stage.EXECUTION, secrets=()) -> NormalizedError:
    if isinstance(exc, TransferError):
        return exc.error
    if isinstance(exc, (ssl.SSLCertVerificationError, aiohttp.ClientConnectorCertificateError)):
        domain, category, retryability, origin, permanence, confidence = (
            D.SECURITY, C.TLS_IDENTITY_FAILURE, T.NEVER, O.SECURITY_POLICY, P.PERMANENT, CF.HIGH,
        )
    elif isinstance(exc, (asyncio.TimeoutError, Aria2ConnectionError, aiohttp.ClientConnectionError)):
        domain, category, retryability, origin, permanence, confidence = (
            D.EXECUTOR, C.EXECUTOR_UNAVAILABLE, T.BACKOFF, O.EXECUTOR, P.TEMPORARY, CF.HIGH,
        )
    else:
        domain, category, retryability, origin, permanence, confidence = (
            D.EXECUTOR, C.UNMAPPED_EXECUTOR_ERROR, T.UNKNOWN, O.EXECUTOR, P.UNKNOWN, CF.LOW,
        )
    return NormalizedError(
        domain, category, stage, retryability=retryability,
        origin=origin, permanence=permanence, integration_id="aria2",
        native_code=str(getattr(exc, "code", "") or ""),
        diagnostic=safe_diagnostic(exc, secrets=tuple(secrets)),
        confidence=confidence, evidence_basis=E.TYPED_EXCEPTION,
    )


def is_missing(exc: Exception, gid: str) -> bool:
    """Only aria2's explicit response for this exact GID proves absence."""
    if isinstance(exc, Aria2ConnectionError) or not isinstance(exc, Aria2RPCError):
        return False
    return bool(re.search(r"\bGID\s+" + re.escape(gid) + r"\s+is not found\b", str(exc), re.I))


def observation(handle: ExecutionHandle, native, *, secrets=()) -> ExecutionObservation:
    state = _STATES.get(str(native.status), ExecutionState.UNKNOWN)
    error = None
    if state == ExecutionState.FAILED:
        error = native_failure(native.error_code, native.error_message, secrets=secrets)
    elif state == ExecutionState.UNKNOWN:
        error = native_failure("", "Unrecognized executor state", secrets=secrets)
    return ExecutionObservation(
        handle, state,
        TransferProgress(max(0, int(native.total_length)), max(0, int(native.completed_length)), max(0, int(native.download_speed))),
        tuple(str(item["path"]) for item in (native.files or []) if item.get("path")), error,
    )
