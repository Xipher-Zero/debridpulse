"""aria2-native state and failure translation.

The numeric meanings are documented at
https://aria2.github.io/manual/en/html/aria2c.html#exit-status . Native messages
are diagnostics only except for the protocol's explicit missing-GID response.
"""
from __future__ import annotations

import asyncio
import re
import ssl

import aiohttp

from executors.aria2.client import Aria2ConnectionError, Aria2RPCError
from transfers.errors import (
    Category as C, Domain as D, NormalizedError, Origin as O, Permanence as P,
    Recovery as R, Retryability as T, Stage, TransferError, safe_diagnostic,
)
from transfers.models import ExecutionHandle, ExecutionObservation, ExecutionState, TransferProgress


# Each value explicitly identifies what can change the outcome. Unknown codes
# are deliberately absent and never inherit a transient network default.
_ERRORS = {
    "2": (D.NETWORK, C.READ_TIMEOUT, T.BACKOFF, R.BACKOFF, O.REMOTE_SOURCE),
    "3": (D.EXECUTOR, C.SOURCE_NOT_FOUND, T.AFTER_RERESOLUTION, R.RERESOLVE, O.REMOTE_SOURCE),
    "4": (D.EXECUTOR, C.SOURCE_NOT_FOUND, T.AFTER_RERESOLUTION, R.RERESOLVE, O.REMOTE_SOURCE),
    "5": (D.EXECUTOR, C.TRANSFER_STALLED, T.BACKOFF, R.TRY_ALTERNATE_CANDIDATE, O.REMOTE_SOURCE),
    "6": (D.NETWORK, C.CONNECTION_FAILED, T.BACKOFF, R.BACKOFF, O.REMOTE_SOURCE),
    "7": (D.EXECUTOR, C.TRANSFER_INTERRUPTED, T.BACKOFF, R.RECONCILE, O.EXECUTOR),
    "8": (D.EXECUTOR, C.REMOTE_READ_FAILED, T.AFTER_RERESOLUTION, R.TRY_ALTERNATE_CANDIDATE, O.REMOTE_SOURCE),
    "9": (D.LOCAL_RESOURCE, C.DISK_FULL, T.AFTER_RESOURCE_CHANGE, R.REQUIRE_OPERATOR, O.LOCAL_SYSTEM),
    "10": (D.INTEGRITY, C.CONTENT_INVALID, T.AFTER_RESOURCE_CHANGE, R.REQUIRE_OPERATOR, O.LOCAL_SYSTEM),
    "11": (D.LIFECYCLE, C.LOCAL_PATH_CONFLICT, T.AFTER_RESOURCE_CHANGE, R.RECONCILE, O.EXECUTOR),
    "12": (D.LIFECYCLE, C.RESOURCE_STATE_CONFLICT, T.AFTER_RESOURCE_CHANGE, R.RECONCILE, O.EXECUTOR),
    "13": (D.LOCAL_RESOURCE, C.LOCAL_PATH_CONFLICT, T.AFTER_RESOURCE_CHANGE, R.REQUIRE_OPERATOR, O.LOCAL_SYSTEM),
    "14": (D.LOCAL_RESOURCE, C.LOCAL_IO_FAILURE, T.AFTER_RESOURCE_CHANGE, R.REQUIRE_OPERATOR, O.LOCAL_SYSTEM),
    "15": (D.LOCAL_RESOURCE, C.PATH_UNAVAILABLE, T.AFTER_RESOURCE_CHANGE, R.REQUIRE_OPERATOR, O.LOCAL_SYSTEM),
    "16": (D.LOCAL_RESOURCE, C.LOCAL_IO_FAILURE, T.AFTER_RESOURCE_CHANGE, R.REQUIRE_OPERATOR, O.LOCAL_SYSTEM),
    "17": (D.LOCAL_RESOURCE, C.LOCAL_IO_FAILURE, T.AFTER_RESOURCE_CHANGE, R.REQUIRE_OPERATOR, O.LOCAL_SYSTEM),
    "18": (D.LOCAL_RESOURCE, C.PATH_UNAVAILABLE, T.AFTER_RESOURCE_CHANGE, R.REQUIRE_OPERATOR, O.LOCAL_SYSTEM),
    "19": (D.NETWORK, C.DNS_FAILURE, T.BACKOFF, R.BACKOFF, O.REMOTE_SOURCE),
    "20": (D.EXECUTOR, C.CONTENT_INVALID, T.NEVER, R.FAIL, O.REMOTE_SOURCE),
    "21": (D.NETWORK, C.REMOTE_READ_FAILED, T.AFTER_RERESOLUTION, R.TRY_ALTERNATE_CANDIDATE, O.REMOTE_SOURCE),
    "22": (D.NETWORK, C.PROTOCOL_ERROR, T.UNKNOWN, R.REQUIRE_OPERATOR, O.REMOTE_SOURCE),
    "23": (D.SECURITY, C.UNSAFE_REDIRECT, T.NEVER, R.FAIL, O.SECURITY_POLICY),
    "24": (D.EXECUTOR, C.CANDIDATE_EXPIRED, T.AFTER_RERESOLUTION, R.RERESOLVE, O.REMOTE_SOURCE),
    "25": (D.EXECUTOR, C.CONTENT_INVALID, T.NEVER, R.FAIL, O.REMOTE_SOURCE),
    "26": (D.EXECUTOR, C.CONTENT_INVALID, T.NEVER, R.FAIL, O.REMOTE_SOURCE),
    "27": (D.REQUEST, C.INVALID_REQUEST, T.NEVER, R.FAIL, O.USER),
    "28": (D.EXECUTOR, C.INVALID_CONFIGURATION, T.NEVER, R.REQUIRE_OPERATOR, O.EXECUTOR),
    "29": (D.NETWORK, C.SOURCE_TEMPORARILY_UNAVAILABLE, T.BACKOFF, R.BACKOFF, O.REMOTE_SOURCE),
    "30": (D.INTERNAL, C.EXECUTOR_PROTOCOL_VIOLATION, T.UNKNOWN, R.REQUIRE_OPERATOR, O.EXECUTOR),
    "32": (D.INTEGRITY, C.CHECKSUM_MISMATCH, T.AFTER_RERESOLUTION, R.TRY_ALTERNATE_CANDIDATE, O.REMOTE_SOURCE),
}
_STATES = {
    "active": ExecutionState.TRANSFERRING, "waiting": ExecutionState.QUEUED,
    "paused": ExecutionState.PAUSED, "complete": ExecutionState.SUCCEEDED,
    "error": ExecutionState.FAILED, "removed": ExecutionState.CANCELLED,
}


def native_failure(code: object, message: object = "", *, stage=Stage.EXECUTION, secrets=()) -> NormalizedError:
    native = str(code or "")
    spec = _ERRORS.get(native)
    if spec is None:
        spec = (D.EXECUTOR, C.UNMAPPED_EXECUTOR_ERROR, T.UNKNOWN, R.REQUIRE_OPERATOR, O.EXECUTOR)
    domain, category, retryability, recovery, origin = spec
    return NormalizedError(
        domain, category, stage, retryability=retryability, recovery=recovery,
        origin=origin, permanence=P.PERMANENT if retryability == T.NEVER else P.UNKNOWN,
        operator_action_required=recovery == R.REQUIRE_OPERATOR,
        integration_id="aria2", native_code=native,
        diagnostic=safe_diagnostic(message, secrets=tuple(secrets)),
    )


def exception_failure(exc: Exception, *, stage=Stage.EXECUTION, secrets=()) -> NormalizedError:
    if isinstance(exc, TransferError):
        return exc.error
    if isinstance(exc, (ssl.SSLCertVerificationError, aiohttp.ClientConnectorCertificateError)):
        domain, category, retryability, recovery = D.SECURITY, C.TLS_IDENTITY_FAILURE, T.NEVER, R.FAIL
    elif isinstance(exc, (asyncio.TimeoutError, Aria2ConnectionError, aiohttp.ClientConnectionError)):
        domain, category, retryability, recovery = D.EXECUTOR, C.EXECUTOR_UNAVAILABLE, T.BACKOFF, R.RECONCILE
    else:
        domain, category, retryability, recovery = D.EXECUTOR, C.UNMAPPED_EXECUTOR_ERROR, T.UNKNOWN, R.REQUIRE_OPERATOR
    return NormalizedError(
        domain, category, stage, retryability=retryability, recovery=recovery,
        origin=O.EXECUTOR, integration_id="aria2", native_code=str(getattr(exc, "code", "") or ""),
        diagnostic=safe_diagnostic(exc, secrets=tuple(secrets)),
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
