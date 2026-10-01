"""Canonical aria2 execution boundary with durable identity and scoped mutation.

Core persists a prepared handle before submitting. A lost response is recovered
by observing that same handle, never by a second uncorrelated addUri. Authorization
is injected by the application repository and checked before every native action.
The executor reports factual observations only; recovery policy is core-owned.
"""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import time
from typing import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

from executors.aria2.client import Aria2ResponseError, Aria2Service
from executors.aria2.translation import exception_failure, is_missing, observation
from services.artifact_sampling import (
    SAMPLED_FINGERPRINT_SCHEMES, SSH_HOST_KEY_ALGORITHMS, AccessRequired, Listing, ListingRefused, Located, Opaque,
    RemoteFile, ftp_discovery, ftp_fingerprint, http_content, resolve_location, sampled_public_artifact_fingerprint,
    sftp_discovery, sftp_fingerprint, webdav_discovery,
)
from services.downloader_egress_guard import RouteScope, downloader_egress_guard
from services.network_safety import DestinationLookupError, validate_resolved_public_destination
from transfers import material as mat
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.input_required import SubmittedInput, auth_required, server_identity_required, username_password
from transfers.requests import auth_scope
from transfers.models import (
    ArtifactFingerprint, DiscoveredEntry, DiscoveryDepth, DiscoveryLimits, DiscoveryResult, ExecutionActivity, RemoteObjectKind, ExecutionControl, ExecutionFootprint, ExecutionHandle, ExecutionObservation, ExecutionRequest,
    ExecutionState, ExecutionSnapshot, ExecutorCapabilities, ExecutorClaim, ExecutorHealth,
    ExecutorRuntimeCapability, ExecutorRuntimeControlResult, FingerprintKind, InputFactName, InputField,
    InputMethod, InputReason, InputRequirement, IntegrationDescriptor, MaterializationKind, MaterializationResult,
    MaterializedEntry, ContinuationCapability, ContinuationStrategy,
)


# Native input evidence, each characterized against the packaged aria2 1.37.0 /
# libssh2 1.11.1. A native code alone is never authentication evidence; only the
# exact deterministic diagnostic on the matching transport is.
_FTP_LOGIN_REJECTED = "The response status is not successful. status=530"
_SSH_PASSWORD_REJECTED = "SSH authentication failure: Authentication failed (username/password)"
_SSH_HOST_KEY_MISMATCH = re.compile(r"Unexpected SSH host key: expected ([0-9a-f]{40}), actual ([0-9a-f]{40})")
_SSH_HOST_KEY_OPTION = re.compile(r"sha-1=([0-9a-f]{40})")
# The expected SHA-1 of a first SFTP attempt. It is never a trusted identity:
# the SSH handshake observes the real server key, then fails on it before any
# authentication, so an SFTP job never runs without host-key verification.
_HOST_KEY_SENTINEL = "0" * 40
# aria2's own anonymous defaults, pinned per job so no daemon-global FTP
# credentials are ever inherited by an owned job.
_ANONYMOUS_LOGIN = {"ftp-user": "anonymous", "ftp-passwd": "ARIA2USER@"}
# The packaged libssh2 1.11.1 host-key preference (characterized against servers
# restricted to subsets of ECDSA/Ed25519/RSA keys). It is the one preference of
# the whole SSH authentication scope, owned beside the one SSH step, so evidence
# acquisition -- and every other consumer of that scope -- asks for the same
# order and the identity an operator confirms is the one aria2 verifies.
_NATIVE_HOST_KEY_ORDER = SSH_HOST_KEY_ALGORITHMS
_SHA1_IDENTITY = re.compile(r"[0-9a-f]{40}")
# Positive transport claim only: a transport aria2 actually delivers and that
# the canonical destination validator and egress guard cover. Everything else
# -- scp, rsync, ftps, webdav, metalink, magnet, native torrent -- is
# unsupported by absence, never by a denial list. Executor-private: core never
# routes by it.
SUPPORTED_SCHEMES = frozenset({"http", "https", "ftp", "sftp"})
_OVERALL_DOWNLOAD_LIMIT = "max-overall-download-limit"
# A job given sparse DP material pieces its payload in exactly the DP geometry
# grain, so every whole DP chunk is one aria2 piece (characterized 1.37.0: a
# control file whose piece length or total length differs from the job's is
# refused before anything is fetched).
_IMPORT_PIECE_BYTES = mat.CHUNK_BYTES
_IMPORT_PIECE_OPTION = "1M"


@dataclass(frozen=True)
class Aria2Configuration:
    local_root: str
    split: int = 1
    minimum_split_size: str = "10M"
    connections_per_server: int = 1
    continue_downloads: bool = True
    confirmation_delay: float = 0.05
    control_confirmation_timeout: float = 3.0
    waiting_window: int = 100
    stopped_window: int = 100
    secrets: tuple[str, ...] = field(default=(), repr=False)


def _acceptance(submitted: SubmittedInput | None):
    """The transport verdict notice of supplied input (none for anonymous
    access: aria2's own default login proves nothing about an operator)."""
    return submitted.transport_accepted if submitted is not None else None


# The admission-confirmation window (``Aria2Executor._admitted``): long enough
# to cover the immediate native deaths characterized on a real SSH server
# (~250 ms), never a multi-second wait, and never longer than the executor's
# own control-confirmation bound.
_ADMISSION_CONFIRMATION_SECONDS = 1.0

# How long an observed move of an HTTP(S) address to another authority is
# remembered, so input answered for that authority starts there instead of at
# the original one (which would refuse it, or need its own input again). A
# small process-local memo of an observation, never durable truth.
_MOVED_SECONDS = 300.0
_MOVED_LIMIT = 256
# The executor's own diagnostic for a download whose address moved to another
# authority that asks for input (``_download_location``); the requirement it
# raised is kept per attempt, never carried through a sanitized diagnostic.
_AUTHORITY_DIAGNOSTIC = "redirected_authority"


class _AdmissionDeferred(Exception):
    """Owned execution remains parked by a newer core control intent."""


def execution_binding(local_root, url):
    """Bind authority to one daemon and download root, never merely a GID.

    The digest is durable handle identity: every persisted handle carries it.
    The serialized layout is therefore fixed, including its two constant
    members, so existing handles keep matching after an upgrade.
    """
    payload = [str(url).strip(), False, str(Path(local_root).resolve()), ""]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


class Aria2Executor:
    descriptor = IntegrationDescriptor("aria2", "aria2", frozenset())
    # Semantic guarantees only. The daemon-wide pause is NOT offered as an
    # acquisition gate: unpausing it would also unpause jobs a transfer-level
    # intent keeps paused, so per-execution controls converge global pause.
    #
    # Continuation: a FRESH aria2 job continues a FILE exactly at a
    # DebridPulse-authorized offset (the payload is cut to the plan boundary,
    # the ``.aria2`` control file is discarded and the job resumes there); it
    # cannot import arbitrary sparse ranges. aria2 exports completed pieces as
    # exact final-file ranges and pauses gracefully. A natively paused job
    # keeps its own in-memory piece map and ``aria2.unpause`` continues it
    # (native private resume) -- for the same source only: another source is
    # always a fresh job, prepared by ``start`` like any other. Private resume
    # neither reads nor writes the control file, and is never DebridPulse
    # material truth.
    #
    # Sparse import (characterized 1.37.0, HTTP/FTP/SFTP): a FRESH job whose
    # control file marks whole pieces complete keeps them untouched and
    # fetches only the rest; it verifies nothing it is told, and refuses a
    # piece or total length that is not its own. So the control file is only
    # ever WRITTEN, from a core plan, stating exactly the DP-valid whole pieces
    # the plan retains (``IMPORT_SPARSE_MATERIAL``) -- the same trust the
    # retained prefix of a contiguous plan receives -- and never read.
    capabilities = ExecutorCapabilities(
        candidate_sampling=True, per_execution_pause=True, aggregate_bandwidth_ceiling=True,
        transient_input=True, remote_discovery=True, materialization_kinds=frozenset({MaterializationKind.FILE}),
        continuation=frozenset({
            ContinuationCapability.FULL_RESTART, ContinuationCapability.CONTIGUOUS_FROM_OFFSET,
            ContinuationCapability.IMPORT_EXISTING_MATERIAL, ContinuationCapability.EXPORT_MATERIAL_RANGES,
            ContinuationCapability.NATIVE_QUIESCE, ContinuationCapability.NATIVE_PRIVATE_RESUME,
            ContinuationCapability.IMPORT_SPARSE_MATERIAL,
        }),
    )

    def __init__(self, client: Aria2Service, configuration: Aria2Configuration,
                 authorize: Callable[[ExecutionHandle, str], Awaitable[bool]], *, egress=None, runtime=None):
        self.client = client
        self.configuration = configuration
        self.authorize = authorize
        self.egress = egress or downloader_egress_guard
        self.runtime = runtime
        self._redactions = runtime.redactions if runtime is not None else OrderedDict()
        self.binding = execution_binding(configuration.local_root, getattr(client, "url", ""))
        # address -> (expiry, the address it was last observed answering at
        # under another authority); see ``_start_address``.
        self._moved: OrderedDict[str, tuple[float, str]] = OrderedDict()
        # attempt id -> the requirement a redirected download raised for it.
        self._asked: OrderedDict[str, InputRequirement] = OrderedDict()
        if not configuration.continue_downloads:
            # The operator disabled continuing partial downloads with aria2:
            # declare it, so core plans restarts for it rather than offsets.
            self.capabilities = replace(type(self).capabilities, continuation=type(self).capabilities.continuation - {
                ContinuationCapability.CONTIGUOUS_FROM_OFFSET, ContinuationCapability.IMPORT_EXISTING_MATERIAL,
                ContinuationCapability.IMPORT_SPARSE_MATERIAL})

    def claim(self, subject) -> ExecutorClaim:
        """Pure: a subject is claimed when one of its candidate endpoints uses a
        transport this executor delivers."""
        return ExecutorClaim(self._endpoint(subject.candidate) is not None)

    @staticmethod
    def _gid(attempt_id: str) -> str:
        """The deterministic native identity of a DP attempt."""
        gid = hashlib.sha256(str(attempt_id).encode()).hexdigest()[:16]
        return "1" + gid[1:] if gid == "0" * 16 else gid

    @staticmethod
    def _handle_gid(handle: ExecutionHandle) -> str:
        """The native job of a handle: its bound native identity, or -- for a
        pre-binding historical handle only -- the identity its historical
        correlation recorded."""
        if handle.native is not None:
            return str(handle.native.get("gid") or "")
        return str(handle.correlation.get("gid") or "")

    def _bound(self, handle: ExecutionHandle) -> ExecutionHandle:
        """The one legal native binding of a historical unbound handle."""
        if handle.native is not None:
            return handle
        return ExecutionHandle(handle.executor_id, handle.attempt_id, handle.correlation,
                               {"gid": self._handle_gid(handle)})

    def _failure(self, category: Category, stage=Stage.EXECUTION, *, domain=Domain.EXECUTOR) -> TransferError:
        return TransferError(NormalizedError(domain, category, stage, retryability=Retryability.NEVER,
                                            integration_id=self.descriptor.id))

    def _target(self, target: str) -> Path:
        root = Path(self.configuration.local_root).resolve()
        path = Path(target)
        if not path.is_absolute() or path.is_symlink():
            raise self._failure(Category.PATH_POLICY_VIOLATION, domain=Domain.SECURITY)
        # resolve() also rejects escaping through an existing parent symlink.
        resolved = path.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise self._failure(Category.PATH_POLICY_VIOLATION, domain=Domain.SECURITY)
        return resolved

    @staticmethod
    def _plan_target(request: ExecutionRequest) -> str:
        plan = request.work.materialization
        if plan.kind != MaterializationKind.FILE or plan.target is None:
            raise TransferError(NormalizedError(Domain.REQUEST, Category.UNSUPPORTED_CAPABILITY, Stage.QUEUE,
                                                retryability=Retryability.NEVER, integration_id="aria2"))
        return plan.target

    def footprint(self, work) -> ExecutionFootprint:
        """aria2's control file beside the planned target is its only transient path."""
        return ExecutionFootprint((str(self._target(work.materialization.target)) + ".aria2",))

    def _apply_continuation(self, request: ExecutionRequest, target: Path) -> dict[str, str]:
        """Put the payload in exactly the state the core plan authorizes and
        return aria2's per-job continuation options.

        The private control file never outlives a writer: whatever it claims
        (possibly more than DebridPulse ever committed) is discarded, so aria2
        cannot promote bytes DebridPulse did not authorize. A contiguous plan
        cuts the payload to its boundary -- aria2 then continues exactly there
        -- and fails closed when the retained prefix is not physically present.
        A sparse-import plan keeps the payload and states exactly its retained
        whole pieces in a fresh control file (``_import_sparse``). Any other
        plan retains nothing and aria2 starts from zero -- except a
        ``NATIVE_STATE_HANDOFF`` plan: its retained ranges belong to the
        inherited job's own state, so a fresh job fails closed on it rather
        than overwrite material DebridPulse holds valid."""
        plan = request.continuation
        if plan is not None and plan.strategy == ContinuationStrategy.NATIVE_STATE_HANDOFF:
            raise self._failure(Category.RESOURCE_STATE_CONFLICT, Stage.QUEUE, domain=Domain.LIFECYCLE)
        Path(str(target) + ".aria2").unlink(missing_ok=True)
        if (plan is not None and plan.strategy == ContinuationStrategy.SPARSE_IMPORT
                and ContinuationCapability.IMPORT_SPARSE_MATERIAL in self.capabilities.continuation):
            return self._import_sparse(plan, target)
        if (plan is None or plan.strategy != ContinuationStrategy.CONTIGUOUS_FROM_OFFSET or plan.boundary <= 0
                or ContinuationCapability.CONTIGUOUS_FROM_OFFSET not in self.capabilities.continuation):
            return {"continue": "false"}
        try:
            info = target.lstat()
        except FileNotFoundError:
            info = None
        if info is None or not stat.S_ISREG(info.st_mode) or info.st_size < plan.boundary:
            raise self._failure(Category.RESOURCE_STATE_CONFLICT, Stage.QUEUE, domain=Domain.LIFECYCLE)
        os.truncate(target, plan.boundary)
        return {"continue": "true"}

    def _import_sparse(self, plan, target: Path) -> dict[str, str]:
        """Hand a fresh job exactly the plan's retained whole pieces.

        Fails closed -- before anything is written -- unless the total is
        known, every retained range is whole pieces of it (the last one may
        end at the end of file), and the payload physically holds every one of
        them as a regular file. Bytes past the end of file are no material and
        are cut. Then one version-1 control file (aria2's documented format:
        no info hash, the job's piece length, the total, the completed-piece
        bitfield, no in-flight piece) states those pieces and nothing else;
        the job itself pieces in that grain."""
        total = int(plan.expected_size or 0)
        retained = mat.normalize(plan.retained)
        piece = _IMPORT_PIECE_BYTES
        if (total <= 0 or not retained or retained[-1][1] > total
                or any(start % piece or (end % piece and end != total) for start, end in retained)):
            raise self._failure(Category.RESOURCE_STATE_CONFLICT, Stage.QUEUE, domain=Domain.LIFECYCLE)
        try:
            info = target.lstat()
        except FileNotFoundError:
            info = None
        if info is None or not stat.S_ISREG(info.st_mode) or info.st_size < retained[-1][1]:
            raise self._failure(Category.RESOURCE_STATE_CONFLICT, Stage.QUEUE, domain=Domain.LIFECYCLE)
        if info.st_size > total:
            os.truncate(target, total)
        pieces = -(-total // piece)
        bitfield = bytearray(-(-pieces // 8))
        for start, end in retained:
            for index in range(start // piece, -(-end // piece)):
                bitfield[index // 8] |= 0x80 >> (index % 8)
        Path(str(target) + ".aria2").write_bytes(
            struct.pack(">HIIIQQI", 1, 0, 0, piece, total, 0, len(bitfield)) + bytes(bitfield) + struct.pack(">I", 0))
        return {"continue": "true", "piece-length": _IMPORT_PIECE_OPTION}

    def prepare(self, request: ExecutionRequest) -> ExecutionHandle:
        target = self._target(self._plan_target(request))
        if not request.attempt_id:
            raise self._failure(Category.INVALID_REQUEST)
        # Durable identity only: no endpoint address or header value (signed
        # URLs, cookies, bearer capabilities) is ever copied into the handle;
        # exact-value redaction is remembered in process memory only.
        self._remember_redactions(request)
        return ExecutionHandle(self.descriptor.id, request.attempt_id,
                               {"target": str(target), "binding": self.binding},
                               {"gid": self._gid(request.attempt_id)})

    def prepare_with_input(self, request: ExecutionRequest, submitted: SubmittedInput) -> ExecutionHandle:
        """aria2 never needs operator input to prepare: transport credentials
        apply only to a native start after an observed challenge
        (``start_with_input``), so preparation with input is preparation."""
        return self.prepare(request)

    _REDACTION_MEMORY = 4096

    def _remember(self, attempt_id: str, values) -> None:
        self._redactions[attempt_id] = tuple(values)
        self._redactions.move_to_end(attempt_id)
        while len(self._redactions) > self._REDACTION_MEMORY:
            self._redactions.popitem(last=False)

    def _remember_redactions(self, request: ExecutionRequest) -> None:
        self._remember(request.attempt_id, (value for endpoint in request.work.subject.candidate.endpoints
                                            for value in (endpoint.address, *endpoint.headers.values()) if value))

    async def _recover_redactions(self, handle: ExecutionHandle, native) -> bool:
        """Whether exact redaction facts are available for this job's native
        diagnostic. Restart- and eviction-safe: when process memory holds no
        values for this attempt, they are recovered from the authoritative
        native job itself -- its per-job ``header`` values and transport
        passwords -- without ever persisting them; the full option map is
        cleared at once. A failed recovery caches nothing (the next
        observation retries) and answers ``False``: the caller must then not
        admit the native diagnostic text at all (fail closed)."""
        if native is None or str(native.status) != "error" or handle.attempt_id in self._redactions:
            return True
        try:
            options = await self.client._call("aria2.getOption", [self._handle_gid(handle)])
        except Exception:
            return False
        if not isinstance(options, dict):
            return False
        try:
            values = []
            headers = options.get("header") or ()
            for line in (headers.splitlines() if isinstance(headers, str) else headers):
                name, separator, value = str(line).partition(":")
                if separator and value.strip():
                    values.append(value.strip())
            for key in ("http-passwd", "ftp-passwd"):
                value = str(options.get(key) or "")
                if value and value != _ANONYMOUS_LOGIN.get(key):
                    values.append(value)
        finally:
            options.clear()
        self._remember(handle.attempt_id, values)
        return True

    def _secrets(self, handle: ExecutionHandle, *, request: ExecutionRequest | None = None,
                 native=None) -> tuple[str, ...]:
        """Exact values to redact from native diagnostics, taken only from what
        is in hand -- the request being executed and the native job's own
        reported URIs -- never from durable handle state. Everything else is
        covered by the generic URL/credential sanitizer. (A pre-upgrade handle
        may still carry its historical ``redactions`` list; it is only read.)"""
        values = [*self.configuration.secrets, *(str(item) for item in handle.correlation.get("redactions", ())),
                  *self._redactions.get(handle.attempt_id, ())]
        if request is not None:
            values += [value for endpoint in request.work.subject.candidate.endpoints
                       for value in (endpoint.address, *endpoint.headers.values()) if value]
        if native is not None:
            for item in native.files or []:
                for uri in item.get("uris") or ():
                    value = uri.get("uri") if isinstance(uri, dict) else uri
                    if value:
                        values.append(str(value))
        return tuple(values)

    async def _check(self, handle: ExecutionHandle, action: str) -> str:
        if handle.executor_id != self.descriptor.id or not await self.authorize(handle, "observe"):
            raise self._failure(Category.OWNERSHIP_CONFLICT, domain=Domain.LIFECYCLE)
        if handle.correlation.get("binding") != self.binding:
            raise self._failure(Category.EXECUTOR_UNAVAILABLE)
        if action != "observe" and not await self.authorize(handle, action):
            raise _AdmissionDeferred()
        gid = self._handle_gid(handle)
        if len(gid) != 16 or any(ch not in "0123456789abcdef" for ch in gid):
            raise self._failure(Category.INVALID_ADAPTER_RESPONSE)
        self._target(str(handle.correlation.get("target") or ""))
        return gid

    async def fingerprint(self, subject):
        return await self._evidence(subject.candidate)

    async def fingerprint_with_input(self, subject, submitted: SubmittedInput):
        return await self._evidence(subject.candidate, submitted)

    # ── HTTP(S) authority, credential and redirect policy ──────────────────

    @staticmethod
    def _credential(address: str, submitted: SubmittedInput | None):
        """The operator credential of ``submitted`` for exactly the authority
        it was answered for (its stamped scope, else ``address``'s own), or
        ``None``. The guarded redirect owner attaches it to that authority's
        requests only."""
        if submitted is None or submitted.method != InputMethod.USERNAME_PASSWORD:
            return None
        username, password = submitted.value(InputField.USERNAME), submitted.value(InputField.PASSWORD)
        if not username or not password:
            return None
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        return (submitted.scope or auth_scope(address)), "Basic " + token

    def _start_address(self, address: str, submitted: SubmittedInput | None) -> str:
        """Where an HTTP(S) read of ``address`` begins: the address it was
        recently observed moving to under another authority, when
        ``submitted`` was answered for exactly that authority -- its own
        server would only refuse that input -- else ``address`` itself."""
        now = time.monotonic()
        for key in [key for key, (expires, _location) in self._moved.items() if expires <= now]:
            del self._moved[key]
        entry = self._moved.get(address)
        if (entry is not None and submitted is not None and submitted.scope is not None
                and submitted.scope == auth_scope(entry[1])):
            return entry[1]
        return address

    def _authority_requirement(self, address: str, accessed: AccessRequired) -> InputRequirement:
        """The neutral requirement of an HTTP(S) Basic challenge: of
        ``address``'s own authority, or -- when the read had moved -- of the
        authority that asked (its bare origin; never a path or token), whose
        move is remembered for the answer (``_start_address``)."""
        asked = str(accessed.address or "")
        if not asked or auth_scope(asked) == auth_scope(address):
            return auth_required(username_password())
        parts = urlsplit(asked)
        host = str(parts.hostname or "")
        netloc = (f"[{host}]" if ":" in host else host) + (f":{parts.port}" if parts.port else "")
        self._moved[address] = (time.monotonic() + _MOVED_SECONDS, asked)
        self._moved.move_to_end(address)
        while len(self._moved) > _MOVED_LIMIT:
            self._moved.popitem(last=False)
        return auth_required(username_password(), authority=urlunsplit((parts.scheme, netloc, "", "", "")))

    def _granted_at(self, candidate, address: str, start: str) -> dict:
        """The private-LAN grant for a read that starts at ``start``: only
        ever the candidate's own consented host, never one it moved to."""
        same_host = str(urlsplit(start).hostname or "").casefold() == str(urlsplit(address).hostname or "").casefold()
        return self._granted(self._private_lan(candidate) and same_host)

    async def _download_location(self, candidate, endpoint, submitted: SubmittedInput | None,
                                 attempt_id: str = ""):
        """THE executor redirect policy for an HTTP(S) download: where the
        writer is pointed, resolved in-process by the guarded redirect owner
        (aria2 itself never follows a redirect). Returns the answering
        address, or a ``NormalizedError`` when another authority the address
        moved to asked for input of its own. The operator credential reaches
        only its own authority on the way."""
        if endpoint.scheme not in SAMPLED_FINGERPRINT_SCHEMES:
            return endpoint.address
        headers = dict(endpoint.headers)
        if any(any(char in str(key) + str(value) for char in "\r\n\x00") for key, value in headers.items()):
            return endpoint.address  # refused by the one header check in ``_options``
        provider_authorization = any(str(key).lower() == "authorization" for key in headers)
        credential = None if provider_authorization else self._credential(endpoint.address, submitted)
        start = self._start_address(endpoint.address, submitted)
        result = await resolve_location(start, headers=headers if start == endpoint.address else {},
                                        credential=credential, on_authenticated=_acceptance(submitted),
                                        **self._granted_at(candidate, endpoint.address, start))
        if isinstance(result, AccessRequired):
            accepts_input = InputMethod.USERNAME_PASSWORD in candidate.accepted_input_methods
            requirement = self._authority_requirement(endpoint.address, result)
            if requirement.authority and accepts_input and not provider_authorization:
                self._asked[attempt_id] = requirement
                self._asked.move_to_end(attempt_id)
                while len(self._asked) > _MOVED_LIMIT:
                    self._asked.popitem(last=False)
                return NormalizedError(Domain.RESOLUTION, Category.CANDIDATE_EXPIRED, Stage.QUEUE,
                                       retryability=Retryability.AFTER_RERESOLUTION, integration_id=self.descriptor.id,
                                       native_code="24", diagnostic=_AUTHORITY_DIAGNOSTIC)
            return start
        if isinstance(result, Located):
            return result.uri
        return start

    async def _evidence(self, candidate, submitted: SubmittedInput | None = None):
        """One neutral CandidateSampling over the endpoint execution would use.

        Transport dispatch lives here, at the executor boundary; byte windows
        and the digest are the one shared definition, so the same bytes are the
        same fingerprint whatever the transport. Only definitive, characterized
        access evidence becomes an ``InputRequirement``, and only for a
        candidate that advertises transient username/password input."""
        endpoint = self._endpoint(candidate)
        if endpoint is None:
            return None
        for key, value in endpoint.headers.items():
            if any(char in str(key) + str(value) for char in "\r\n\x00") or str(key).lower() in {"host", "proxy-authorization"}:
                return None
        accepts_input = InputMethod.USERNAME_PASSWORD in candidate.accepted_input_methods
        credentials = None
        if submitted is not None:
            credentials = submitted.value(InputField.USERNAME), submitted.value(InputField.PASSWORD)
            if not accepts_input or submitted.method != InputMethod.USERNAME_PASSWORD or not all(credentials):
                return None
        refused = ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "range_unsupported")
        if endpoint.scheme in SAMPLED_FINGERPRINT_SCHEMES:
            headers = dict(endpoint.headers)
            # A provider-issued Authorization capability is the candidate's own
            # access; operator credentials never replace it.
            provider_authorization = any(str(key).lower() == "authorization" for key in headers)
            if credentials is not None and provider_authorization:
                return None
            start = self._start_address(endpoint.address, submitted)
            result = await sampled_public_artifact_fingerprint(
                start, headers=headers if start == endpoint.address else {},
                expected_bytes=max(0, int(candidate.expected_bytes or 0)),
                credential=self._credential(endpoint.address, submitted), on_authenticated=_acceptance(submitted),
                **self._granted_at(candidate, endpoint.address, start),
            )
            if isinstance(result, AccessRequired):
                if not accepts_input or provider_authorization:
                    return refused
                return self._authority_requirement(endpoint.address, result)
            return ArtifactFingerprint(*result) if result else None
        # FTP/SFTP evidence reaches its origin only through the egress guard,
        # after the same destination validation execution applies.
        lan = self._private_lan(candidate)
        try:
            await validate_resolved_public_destination(endpoint.address, **self._granted(lan))
        except DestinationLookupError:
            return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "dns_failure")
        except ValueError:
            return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "destination_rejected")
        if endpoint.scheme == "ftp":
            username, password = self._ftp_login(submitted)
            result = await ftp_fingerprint(
                endpoint.address, username=username, password=password,
                connect=lambda port=None: self.egress.open_tunnel(endpoint.address, scope=RouteScope.SAME_HOST,
                                                                  port=port, **self._granted(lan)),
                on_authenticated=_acceptance(submitted),
            )
            if isinstance(result, AccessRequired):
                return auth_required(username_password()) if accepts_input else refused
            return ArtifactFingerprint(*result)
        if endpoint.scheme == "sftp":
            # SFTP evidence always needs the operator: a confirmed identity and
            # a credential. Without advertised input there is no sample.
            if not accepts_input:
                return None
            access = self._sftp_access(endpoint.address, submitted)
            if access is None:
                return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "destination_rejected")
            host, identity, username, password = access
            result = await sftp_fingerprint(
                endpoint.address, connect=lambda port=None: self.egress.open_tunnel(endpoint.address,
                                                                                    **self._granted(lan)),
                host_key_algorithms=_NATIVE_HOST_KEY_ORDER, host_identity=identity,
                username=username, password=password, on_authenticated=_acceptance(submitted),
            )
            if isinstance(result, AccessRequired):
                requirement = self._sftp_requirement(host, result.server_identity)
                if requirement is None:
                    return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "destination_rejected")
                return requirement
            return ArtifactFingerprint(*result)
        return None

    def _sftp_access(self, address: str, submitted: SubmittedInput | None):
        """THE one SFTP trust and credential decision, for evidence and discovery.

        ``(host, confirmed identity, username, password)``: without input the
        server identity is only observed (no credential is sent); with input,
        only the exact SHA-1 identity the operator confirmed for this host is
        trusted and only username/password material is offered. ``None`` means
        the input cannot be used here and nothing may be attempted."""
        host = str(urlsplit(address).hostname or "").rstrip(".").casefold()
        if submitted is None:
            return host, None, "", ""
        username, password = submitted.value(InputField.USERNAME), submitted.value(InputField.PASSWORD)
        if submitted.method != InputMethod.USERNAME_PASSWORD or not username or not password:
            return None
        identity = self._confirmed_evidence_identity(host, submitted)
        if identity is None:
            return None
        return host, identity, username, password

    @staticmethod
    def _sftp_requirement(host: str, observed: str) -> InputRequirement | None:
        if not host or not _SHA1_IDENTITY.fullmatch(observed) or observed == _HOST_KEY_SENTINEL:
            return None
        return server_identity_required(username_password(), host=host, algorithm="sha-1", fingerprint=observed)

    # Definitive discovery refusals and unavailable facts, as normalized failures.
    _LISTING_FAILURES = {
        "not_found": (Domain.RESOLUTION, Category.SOURCE_NOT_FOUND, Retryability.NEVER),
        "permission_denied": (Domain.RESOLUTION, Category.AUTHORIZATION_FAILED, Retryability.NEVER),
        "not_a_directory": (Domain.REQUEST, Category.INVALID_REQUEST, Retryability.NEVER),
        "unsupported_type": (Domain.REQUEST, Category.UNSUPPORTED_REQUEST, Retryability.NEVER),
        "too_many_entries": (Domain.REQUEST, Category.UNSUPPORTED_REQUEST, Retryability.NEVER),
        "too_large": (Domain.REQUEST, Category.UNSUPPORTED_REQUEST, Retryability.NEVER),
        "unsupported_listing": (Domain.RESOLUTION, Category.PROTOCOL_ERROR, Retryability.NEVER),
        "auth_method_unsupported": (Domain.REQUEST, Category.UNSUPPORTED_CAPABILITY, Retryability.NEVER),
        "sftp_unavailable": (Domain.RESOLUTION, Category.PROTOCOL_ERROR, Retryability.NEVER),
        "destination_rejected": (Domain.SECURITY, Category.DESTINATION_BLOCKED, Retryability.NEVER),
        "timeout": (Domain.NETWORK, Category.CONNECTION_TIMEOUT, Retryability.BACKOFF),
        "dns_failure": (Domain.NETWORK, Category.DNS_FAILURE, Retryability.BACKOFF),
        "connection_refused": (Domain.NETWORK, Category.CONNECTION_REFUSED, Retryability.BACKOFF),
        "tls_failure": (Domain.NETWORK, Category.TLS_FAILURE, Retryability.NEVER),
        "rate_limited": (Domain.NETWORK, Category.RATE_LIMITED, Retryability.BACKOFF),
        "server_error": (Domain.NETWORK, Category.SOURCE_TEMPORARILY_UNAVAILABLE, Retryability.BACKOFF),
    }

    async def discover(self, subject, submitted: SubmittedInput | None = None, *,
                       depth: DiscoveryDepth = DiscoveryDepth.CURRENT, limits: DiscoveryLimits = DiscoveryLimits(),
                       content_limit: int | None = None):
        """Read-only classification of one FTP or SFTP path before any candidate exists.

        The same destination validation, egress route and access decisions
        execution applies: SFTP through ``_sftp_access`` (the identity the
        operator confirms here is the identity aria2 later verifies, and the
        same host-key order and session primitive as evidence); FTP through the
        same-host passive route, anonymously unless a login was supplied. A
        regular file is reported as one file; a directory as its immediate
        regular files -- an FTP or SFTP tree is never listed here (any deeper
        ``depth`` is refused, and so are ``limits`` it does not enforce). HTTP(S)
        is classified and listed through its own collection protocol, WebDAV
        (``_http_discovery``), at any depth and within the requested limits.
        A ``content_limit`` read of one HTTP(S) file is ``_http_discovery``'s
        too, flat and unlimited; no other transport reads content here."""
        candidate = subject.candidate
        endpoint = self._endpoint(candidate)
        if endpoint is None or InputMethod.USERNAME_PASSWORD not in candidate.accepted_input_methods:
            raise self._failure(Category.UNSUPPORTED_CAPABILITY, Stage.RESOLUTION, domain=Domain.REQUEST)
        if content_limit is not None and (endpoint.scheme not in SAMPLED_FINGERPRINT_SCHEMES
                                          or depth != DiscoveryDepth.CURRENT or limits != DiscoveryLimits()):
            raise self._failure(Category.UNSUPPORTED_CAPABILITY, Stage.RESOLUTION, domain=Domain.REQUEST)
        if endpoint.scheme in SAMPLED_FINGERPRINT_SCHEMES:
            result = await self._http_discovery(candidate, endpoint, submitted, depth, limits, content_limit)
            return result if isinstance(result, InputRequirement) else self._discovered(result)
        if depth != DiscoveryDepth.CURRENT or limits != DiscoveryLimits() or endpoint.scheme not in {"ftp", "sftp"}:
            raise self._failure(Category.UNSUPPORTED_CAPABILITY, Stage.RESOLUTION, domain=Domain.REQUEST)
        lan = self._private_lan(candidate)
        try:
            await validate_resolved_public_destination(endpoint.address, **self._granted(lan))
        except DestinationLookupError as exc:
            raise TransferError(NormalizedError(Domain.NETWORK, Category.DNS_FAILURE, Stage.RESOLUTION,
                retryability=Retryability.BACKOFF, integration_id=self.descriptor.id)) from exc
        except ValueError as exc:
            raise self._failure(Category.DESTINATION_BLOCKED, Stage.RESOLUTION, domain=Domain.SECURITY) from exc
        if endpoint.scheme == "sftp":
            access = self._sftp_access(endpoint.address, submitted)
            if access is None:
                raise self._failure(Category.SECURITY_POLICY_REJECTED, Stage.RESOLUTION, domain=Domain.SECURITY)
            host, identity, username, password = access
            result = await sftp_discovery(
                endpoint.address, connect=lambda port=None: self.egress.open_tunnel(endpoint.address,
                                                                                    **self._granted(lan)),
                host_key_algorithms=_NATIVE_HOST_KEY_ORDER, host_identity=identity,
                username=username, password=password, on_authenticated=_acceptance(submitted),
            )
            if isinstance(result, AccessRequired):
                requirement = self._sftp_requirement(host, result.server_identity)
                if requirement is None:
                    raise self._failure(Category.SECURITY_POLICY_REJECTED, Stage.RESOLUTION, domain=Domain.SECURITY)
                return requirement
        else:
            login = self._ftp_login(submitted)
            if login is None:
                raise self._failure(Category.SECURITY_POLICY_REJECTED, Stage.RESOLUTION, domain=Domain.SECURITY)
            result = await ftp_discovery(
                endpoint.address, username=login[0], password=login[1],
                connect=lambda port=None: self.egress.open_tunnel(endpoint.address, scope=RouteScope.SAME_HOST,
                                                                  port=port, **self._granted(lan)),
                on_authenticated=_acceptance(submitted),
            )
        return self._discovered(result)

    async def _http_discovery(self, candidate, endpoint, submitted: SubmittedInput | None, depth: DiscoveryDepth,
                              limits: DiscoveryLimits = DiscoveryLimits(), content_limit: int | None = None):
        """HTTP(S) classification and listing through WebDAV, read-only -- or,
        with ``content_limit``, the complete content of one file
        (``http_content``), never more than that many bytes.

        The same destination validation, redirect control and private-LAN
        grant as HTTP(S) evidence (``services.artifact_sampling``): anonymous
        unless a username/password answer was supplied, and that answer is
        sent only to the origin it was given for. The neutral ``depth`` and
        ``limits`` are translated and enforced by the reader, never by the
        server; a limit left unset keeps the reader's own bound."""
        if endpoint.headers:
            # Discovery carries no provider-issued capability of any kind.
            raise self._failure(Category.SECURITY_POLICY_REJECTED, Stage.RESOLUTION, domain=Domain.SECURITY)
        username = password = ""
        if submitted is not None:
            username, password = submitted.value(InputField.USERNAME), submitted.value(InputField.PASSWORD)
            if submitted.method != InputMethod.USERNAME_PASSWORD or not username or not password:
                raise self._failure(Category.SECURITY_POLICY_REJECTED, Stage.RESOLUTION, domain=Domain.SECURITY)
        start = self._start_address(endpoint.address, submitted)
        access = dict(
            username=username, password=password,
            credential_scope=(submitted.scope or auth_scope(endpoint.address)) if submitted is not None else None,
            on_authenticated=_acceptance(submitted), **self._granted_at(candidate, endpoint.address, start))
        if content_limit is not None:
            result = await http_content(start, max_bytes=content_limit, **access)
        else:
            bounded = {}
            if limits.max_files is not None:
                bounded["max_files"] = limits.max_files
            if limits.timeout_seconds is not None:
                bounded["scan_timeout_seconds"] = limits.timeout_seconds
            result = await webdav_discovery(start, depth=depth, **bounded, **access)
        if isinstance(result, AccessRequired):
            return self._authority_requirement(endpoint.address, result)
        if start != endpoint.address and isinstance(result, (Listing, RemoteFile)) and not result.location:
            # Listed where the path had moved: that is where it was described.
            result = replace(result, location=start)
        return result

    def _discovered(self, result):
        """One transport's discovery facts as the neutral answer."""
        if isinstance(result, AccessRequired):
            return auth_required(username_password())
        if isinstance(result, Opaque):
            return DiscoveryResult(kind=RemoteObjectKind.OPAQUE)
        if isinstance(result, Listing):
            return DiscoveryResult(tuple(
                DiscoveredEntry(name.rsplit("/", 1)[-1], size, relative_path=name if "/" in name else "")
                for name, size in result.entries), result.directory, location=result.location)
        if isinstance(result, RemoteFile):
            return DiscoveryResult(kind=RemoteObjectKind.FILE, expected_bytes=max(0, result.size),
                                   location=result.location, content=result.content)
        reason = result.reason if isinstance(result, ListingRefused) else result[3]
        domain, category, retryability = self._LISTING_FAILURES.get(
            reason, (Domain.NETWORK, Category.CONNECTION_FAILED, Retryability.BACKOFF))
        raise TransferError(NormalizedError(domain, category, Stage.RESOLUTION, retryability=retryability,
                                            integration_id=self.descriptor.id, diagnostic=reason))

    @staticmethod
    def _ftp_login(submitted: SubmittedInput | None):
        """The FTP login for one attempt: aria2's own anonymous default, or the
        supplied username/password; ``None`` for input that cannot log in."""
        if submitted is None:
            return _ANONYMOUS_LOGIN["ftp-user"], _ANONYMOUS_LOGIN["ftp-passwd"]
        username, password = submitted.value(InputField.USERNAME), submitted.value(InputField.PASSWORD)
        if submitted.method != InputMethod.USERNAME_PASSWORD or not username or not password:
            return None
        return username, password

    @staticmethod
    def _confirmed_evidence_identity(host: str, submitted: SubmittedInput) -> str | None:
        """The exact SHA-1 host identity the operator confirmed for ``host``, or None."""
        facts = {fact.name: fact.value for fact in submitted.facts}
        identity = facts.get(InputFactName.SERVER_IDENTITY_FINGERPRINT, "")
        if (not host or facts.get(InputFactName.SERVER_HOST) != host
                or facts.get(InputFactName.SERVER_IDENTITY_ALGORITHM) != "sha-1"
                or not _SHA1_IDENTITY.fullmatch(identity) or identity == _HOST_KEY_SENTINEL):
            return None
        return identity

    @staticmethod
    def _endpoint(candidate):
        return next((item for item in candidate.endpoints if item.scheme in SUPPORTED_SCHEMES), None)

    @staticmethod
    def _granted(lan: bool) -> dict:
        """The private-LAN keyword for a validation/egress call: present only
        for a granted candidate, so every ungranted call is exactly as before."""
        return {"private_lan": True} if lan else {}

    def _private_lan(self, candidate) -> bool:
        """Core's private-LAN grant for this candidate, honored only while the
        guard reports the operator's global policy on. The guard re-checks
        both at every connection; this only lets validation not refuse early."""
        return bool(getattr(candidate, "private_network_grant", False)
                    and getattr(self.egress, "private_lan_enabled", False))

    def input_requirement(self, candidate, observed: ExecutionObservation) -> InputRequirement | None:
        # Only a candidate that explicitly advertises transient username/
        # password input interprets definitive native evidence as a challenge.
        endpoint = self._endpoint(candidate)
        if (endpoint is None or InputMethod.USERNAME_PASSWORD not in candidate.accepted_input_methods
                or observed.state != ExecutionState.FAILED or observed.error is None):
            return None
        code, diagnostic = observed.error.native_code, observed.error.diagnostic
        if endpoint.scheme in {"http", "https"}:
            # aria2 code 24 remains the generic candidate-expiry signal for
            # candidates that accept no input. The executor's own redirected-
            # download refusal names the authority that asked.
            if code != "24":
                return None
            if diagnostic == _AUTHORITY_DIAGNOSTIC:
                asked = self._asked.get(str(observed.handle.attempt_id))
                if asked is not None:
                    return asked
            return auth_required(username_password())
        if endpoint.scheme == "ftp":
            return auth_required(username_password()) if code == "21" and diagnostic == _FTP_LOGIN_REJECTED else None
        if endpoint.scheme == "sftp" and code == "1":
            if diagnostic == _SSH_PASSWORD_REJECTED:
                return auth_required(username_password())
            mismatch = _SSH_HOST_KEY_MISMATCH.fullmatch(diagnostic)
            # Only the fail-closed probe asks the operator. A mismatch against
            # an already-confirmed key is a changed identity and stays a failure.
            if mismatch and mismatch.group(1) == _HOST_KEY_SENTINEL and mismatch.group(2) != _HOST_KEY_SENTINEL:
                host = str(urlsplit(endpoint.address).hostname or "").rstrip(".").casefold()
                if host:
                    return server_identity_required(username_password(), host=host, algorithm="sha-1",
                                                    fingerprint=mismatch.group(2))
        return None

    async def _confirmed_host_identity(self, gid: str) -> str:
        """Recover the host key the operator already confirmed for this owned job.

        Only the non-secret ``ssh-host-key-md`` is read; the client discards the
        rest of the native option map. Anything but a confirmed SHA-1 fails
        closed -- a credential retry never runs without host verification.
        """
        match = _SSH_HOST_KEY_OPTION.fullmatch(await self.client.get_option(gid, "ssh-host-key-md"))
        if match is None or match.group(1) == _HOST_KEY_SENTINEL:
            raise self._failure(Category.SECURITY_POLICY_REJECTED, Stage.QUEUE, domain=Domain.SECURITY)
        return match.group(1)

    async def _options(self, request: ExecutionRequest, handle: ExecutionHandle,
                       submitted: SubmittedInput | None = None, *, host_identity: str | None = None,
                       location: str | None = None) -> tuple[str, dict]:
        """``location``: where ``_download_location`` resolved an HTTP(S)
        endpoint to be answered; the job is pointed there, the private-LAN
        grant and any provider-issued header stay with the endpoint's own
        origin, and the operator credential with its own authority."""
        endpoint = self._endpoint(request.work.subject.candidate)
        if endpoint is None or urlsplit(endpoint.address).scheme != endpoint.scheme:
            raise self._failure(Category.UNSUPPORTED_CAPABILITY, Stage.QUEUE)
        location = location or endpoint.address
        if urlsplit(location).scheme != endpoint.scheme and not (
                endpoint.scheme in SAMPLED_FINGERPRINT_SCHEMES and urlsplit(location).scheme in SAMPLED_FINGERPRINT_SCHEMES):
            raise self._failure(Category.UNSUPPORTED_CAPABILITY, Stage.QUEUE)
        same_origin = location == endpoint.address or (
            urlsplit(location).scheme, str(urlsplit(location).hostname or "").casefold(), urlsplit(location).port) == (
            urlsplit(endpoint.address).scheme, str(urlsplit(endpoint.address).hostname or "").casefold(),
            urlsplit(endpoint.address).port)
        lan = self._private_lan(request.work.subject.candidate) and bool(self._granted_at(
            request.work.subject.candidate, endpoint.address, location))
        try:
            address = await validate_resolved_public_destination(location, **self._granted(lan))
        except DestinationLookupError as exc:
            raise TransferError(NormalizedError(Domain.NETWORK, Category.DNS_FAILURE, Stage.QUEUE,
                retryability=Retryability.BACKOFF, integration_id=self.descriptor.id)) from exc
        except ValueError as exc:
            raise self._failure(Category.DESTINATION_BLOCKED, domain=Domain.SECURITY) from exc
        try:
            await self.egress.ensure_started()
            # One passive FTP job also opens a server-selected data connection
            # to the same host; every other transport is one exact endpoint.
            scope = RouteScope.SAME_HOST if endpoint.scheme == "ftp" else RouteScope.ENDPOINT
            # Every connection of the job draws on aria2's DP download budget.
            guarded = self.egress.job_options(address, scope=scope, budget=self.descriptor.id, **self._granted(lan))
        except Exception as exc:
            raise self._failure(Category.EGRESS_POLICY_VIOLATION, domain=Domain.SECURITY) from exc
        target = self._target(self._plan_target(request))
        cfg = self.configuration
        options = {
            "gid": self._handle_gid(handle), "dir": str(target.parent), "out": target.name,
            "allow-overwrite": "true", "auto-file-renaming": "false",
            "follow-torrent": "false", "follow-metalink": "false",
            "max-http-redirection": "0", "check-certificate": "true",
            "max-tries": "1", "no-netrc": "true", "http-auth-challenge": "true",
            "http-user": "", "http-passwd": "",
            "split": str(max(1, cfg.split)), "min-split-size": cfg.minimum_split_size,
            "max-connection-per-server": str(max(1, cfg.connections_per_server)),
            # Set per start from the core continuation plan, never from config.
            "continue": "false",
            "pause": "true" if request.paused else "false", **guarded,
        }
        if endpoint.scheme in {"ftp", "sftp"}:
            # An owned job never donates its authenticated session to aria2's
            # pool, so every later job authenticates freshly.
            options["ftp-reuse-connection"] = "false"
            options.update(_ANONYMOUS_LOGIN)
        if endpoint.scheme == "ftp":
            # Passive mode is the guarded FTP transport (an active data channel
            # cannot cross the egress guard) and binary keeps artifact bytes
            # exact; pinned so a daemon's global FTP defaults never apply.
            options["ftp-pasv"] = "true"
            options["ftp-type"] = "binary"
        if endpoint.scheme == "sftp":
            options["ssh-host-key-md"] = f"sha-1={host_identity or _HOST_KEY_SENTINEL}"
        if submitted is not None:
            if submitted.method != InputMethod.USERNAME_PASSWORD:
                raise self._failure(Category.INVALID_REQUEST, Stage.QUEUE, domain=Domain.REQUEST)
            username = submitted.value(InputField.USERNAME)
            password = submitted.value(InputField.PASSWORD)
            if not username or not password:
                raise self._failure(Category.INVALID_REQUEST, Stage.QUEUE, domain=Domain.REQUEST)
            if endpoint.scheme in {"http", "https"}:
                # Input exists only because a real HTTP authorization challenge
                # was already observed. Send the submitted correction directly so
                # an aria2 challenge cache cannot replay a superseded credential
                # -- and only to the one authority it was answered for.
                if auth_scope(address) == (submitted.scope or auth_scope(endpoint.address)):
                    options["http-auth-challenge"] = "false"
                    options["http-user"] = username
                    options["http-passwd"] = password
            else:
                options["ftp-user"] = username
                options["ftp-passwd"] = password
        headers = []
        for key, value in endpoint.headers.items():
            if any(char in str(key) + str(value) for char in "\r\n\x00") or str(key).lower() in {"host", "proxy-authorization"}:
                raise self._failure(Category.SECURITY_POLICY_REJECTED, domain=Domain.SECURITY)
            if same_origin:
                # A provider-issued header is its own origin's capability.
                headers.append(f"{key}: {value}")
        if headers:
            options["header"] = headers
        return address, options

    @staticmethod
    def _accepted(handle: ExecutionHandle, *, paused: bool) -> ExecutionObservation:
        """The native queue accepted the owned job: it may start acquiring
        without another core admission, so it holds a reservation."""
        activity = ExecutionActivity(bandwidth_reservation_required=True)
        if paused:
            return ExecutionObservation(handle, ExecutionState.PAUSED, activity=activity,
                                        controls=frozenset({ExecutionControl.RESUME}))
        return ExecutionObservation(handle, ExecutionState.QUEUED, activity=activity,
                                    controls=frozenset({ExecutionControl.PAUSE}))

    async def _admitted(self, handle: ExecutionHandle, *, paused: bool) -> ExecutionObservation:
        """The accepted job's admission, confirmed through the one observation
        (``observe``) for a short bounded window before it is reported.

        aria2 accepts a job that is terminal a few milliseconds later (1.37.0,
        characterized: a refused port at once, a wrong SSH host key or an
        anonymous SSH login within ~250 ms of a real server) and keeps that
        native truth only in its bounded stopped-result history, where later
        stops evict it. A job that dies inside the window is reported as the
        native terminal observation it is, while it is certainly still there;
        one that is acquiring (or already done), still live at the end of the
        window, or not observable right now is reported as accepted -- the ordinary
        observation cadence follows it from then on. Never a second monitor:
        the window ends at the first answer that decides."""
        accepted = self._accepted(handle, paused=paused)
        if paused:
            return accepted
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(_ADMISSION_CONFIRMATION_SECONDS,
                                     max(0.0, float(self.configuration.control_confirmation_timeout)))
        while True:
            observed = await self.observe(handle)
            if observed.state in {ExecutionState.FAILED, ExecutionState.CANCELLED, ExecutionState.ABSENT}:
                return observed
            if (observed.state in {ExecutionState.UNKNOWN, ExecutionState.SUCCEEDED}
                    or observed.progress.completed_bytes > 0):
                return accepted
            remaining = deadline - loop.time()
            if remaining <= 0:
                return accepted
            await asyncio.sleep(min(max(0.01, float(self.configuration.confirmation_delay)), remaining))

    async def _submit(self, handle: ExecutionHandle, address: str, options: dict, secrets, *,
                      paused: bool) -> ExecutionObservation:
        """THE one native job submission (``addUri``) and its admission.

        aria2 answering ``addUri`` with its own JSON-RPC error is a definitive
        refusal -- no job was admitted -- so the start is FAILED with the
        sanitized native evidence, never an uncertain acknowledgement. Only
        what cannot prove the answer (a timeout, a lost connection, a
        malformed or lost response) propagates to the caller's uncertain
        handling."""
        try:
            returned = await self.client._call("aria2.addUri", [[address], options])
        except Aria2ResponseError as exc:
            return ExecutionObservation(handle, ExecutionState.FAILED,
                                        error=exception_failure(exc, stage=Stage.QUEUE, secrets=secrets))
        if str(returned) != self._handle_gid(handle):
            raise self._failure(Category.EXECUTOR_PROTOCOL_VIOLATION)
        return await self._admitted(handle, paused=paused)

    async def start(self, request: ExecutionRequest, handle: ExecutionHandle) -> ExecutionObservation:
        return await self._start(request, handle)

    async def _start(self, request: ExecutionRequest, handle: ExecutionHandle,
                     submitted: SubmittedInput | None = None) -> ExecutionObservation:
        secrets = self._secrets(handle, request=request) + (submitted.secret_values() if submitted is not None else ())
        try:
            gid = await self._check(handle, "start")
            if self.prepare(request) != handle:
                raise self._failure(Category.OWNERSHIP_CONFLICT, domain=Domain.LIFECYCLE)
            # Never adopt a preexisting job by path, URI, or a colliding identity.
            try:
                await self.client.tell_status(gid)
            except Exception as exc:
                if not is_missing(exc, gid):
                    raise
            else:
                raise self._failure(Category.OWNERSHIP_CONFLICT, domain=Domain.LIFECYCLE)
            host_identity = None
            candidate = request.work.subject.candidate
            if submitted is not None and self._endpoint(candidate).scheme == "sftp":
                # Evidence acquisition confirmed this identity before the writer
                # existed; aria2 re-verifies exactly it before authenticating.
                host = str(urlsplit(self._endpoint(candidate).address).hostname or "").rstrip(".").casefold()
                host_identity = self._confirmed_evidence_identity(host, submitted)
                if host_identity is None:
                    raise self._failure(Category.SECURITY_POLICY_REJECTED, Stage.QUEUE, domain=Domain.SECURITY)
            location = await self._download_location(candidate, self._endpoint(candidate), submitted,
                                                     handle.attempt_id)
            if isinstance(location, NormalizedError):
                return ExecutionObservation(handle, ExecutionState.FAILED, error=location)
            secrets += (location,)
            address, options = await self._options(request, handle, submitted, host_identity=host_identity,
                                                   location=location)
            # A deletion can revoke authority during DNS or egress startup.
            await self._check(handle, "start")
            options.update(self._apply_continuation(request, self._target(self._plan_target(request))))
            return await self._submit(handle, address, options, secrets, paused=request.paused)
        except _AdmissionDeferred:
            return ExecutionObservation(handle, ExecutionState.PAUSED)
        except Exception as exc:
            # A lost acknowledgement leaves an uncertain execution, not a
            # failed artifact and not permission to create another native job.
            error = exception_failure(exc, stage=Stage.QUEUE, secrets=secrets)
            uncertain = error.category == Category.EXECUTOR_UNAVAILABLE or error.retryability == Retryability.UNKNOWN
            return ExecutionObservation(handle, ExecutionState.UNKNOWN if uncertain else ExecutionState.FAILED, error=error)

    async def start_with_input(self, request: ExecutionRequest, handle: ExecutionHandle,
                               submitted: SubmittedInput) -> ExecutionObservation:
        if await self._never_started(handle):
            # A freshly prepared attempt: the input already proved this
            # candidate's evidence before the writer existed, so the writer
            # starts with it instead of asking again.
            return await self._start(request, handle, submitted)
        secrets = self._secrets(handle, request=request) + submitted.secret_values()
        try:
            gid = await self._check(handle, "resume")
            before = await self.observe(handle)
            # Input continues only the challenge the live owned job still proves.
            requirement = self.input_requirement(request.work.subject.candidate, before)
            if requirement is None or submitted.method not in {item.method for item in requirement.methods}:
                raise self._failure(Category.RESOURCE_STATE_CONFLICT, domain=Domain.LIFECYCLE)
            host_identity = None
            if requirement.reason == InputReason.SERVER_IDENTITY_REQUIRED:
                # Acceptance holds only for exactly the identity the operator saw,
                # and that must still be the identity the live job observed.
                if set(submitted.facts) != set(requirement.facts):
                    raise self._failure(Category.RESOURCE_STATE_CONFLICT, domain=Domain.LIFECYCLE)
                host_identity = next(fact.value for fact in requirement.facts
                                     if fact.name == InputFactName.SERVER_IDENTITY_FINGERPRINT)
            elif self._endpoint(request.work.subject.candidate).scheme == "sftp":
                host_identity = await self._confirmed_host_identity(gid)
            await self._check(handle, "resume")
            try:
                await self.client._call("aria2.removeDownloadResult", [gid])
            except Exception as exc:
                if not is_missing(exc, gid):
                    raise
            candidate = request.work.subject.candidate
            location = await self._download_location(candidate, self._endpoint(candidate), submitted,
                                                     handle.attempt_id)
            if isinstance(location, NormalizedError):
                return ExecutionObservation(handle, ExecutionState.FAILED, error=location)
            secrets += (location,)
            address, options = await self._options(request, handle, submitted, host_identity=host_identity,
                                                   location=location)
            await self._check(handle, "resume")
            options.update(self._apply_continuation(request, self._target(self._plan_target(request))))
            return await self._submit(handle, address, options, secrets, paused=request.paused)
        except _AdmissionDeferred:
            return await self.observe(handle)
        except Exception as exc:
            error = exception_failure(exc, stage=Stage.QUEUE, secrets=secrets)
            uncertain = error.category == Category.EXECUTOR_UNAVAILABLE or error.retryability == Retryability.UNKNOWN
            return ExecutionObservation(handle, ExecutionState.UNKNOWN if uncertain else ExecutionState.FAILED, error=error)

    async def _never_started(self, handle: ExecutionHandle) -> bool:
        """Still authorized for its FIRST start and absent from the daemon.

        A challenged execution always has its failed native job (the evidence
        the challenge was raised from), so it never qualifies."""
        if handle.executor_id != self.descriptor.id or not await self.authorize(handle, "start"):
            return False
        gid = self._handle_gid(handle)
        try:
            await self.client.tell_status(gid)
        except Exception as exc:
            return is_missing(exc, gid)
        return False

    async def observe(self, handle: ExecutionHandle) -> ExecutionObservation:
        """One handle through the same native truth ``observe_many`` uses:
        private to this executor (confirmation windows, input continuation)."""
        try:
            gid = await self._check(handle, "observe")
            for check in range(3):
                try:
                    native = await self.client.tell_status(gid)
                except Exception as exc:
                    if not is_missing(exc, gid):
                        raise
                    if check < 2:
                        await asyncio.sleep(self.configuration.confirmation_delay)
                    continue
                exact = await self._recover_redactions(handle, native)
                return self._observation(handle, native, exact_redaction=exact)
            return ExecutionObservation(self._bound(handle), ExecutionState.ABSENT)
        except Exception as exc:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN,
                                        error=exception_failure(exc, stage=Stage.RECONCILIATION, secrets=self._secrets(handle)))

    def _observation(self, handle, native, *, exact_redaction: bool = True):
        if str(native.gid) != self._handle_gid(handle):
            raise self._failure(Category.EXECUTOR_PROTOCOL_VIOLATION)
        expected = str(self._target(str(handle.correlation["target"])))
        if any(str(item.get("path") or "") not in {"", expected} for item in (native.files or [])):
            raise self._failure(Category.OWNERSHIP_CONFLICT, domain=Domain.LIFECYCLE)
        result = observation(self._bound(handle), native, secrets=self._secrets(handle, native=native))
        if not exact_redaction and result.error is not None:
            # The native state (and code/category classification) is proven;
            # the native diagnostic text is not provably free of an arbitrary
            # capability value, so it never crosses this boundary.
            result = replace(result, error=replace(result.error, diagnostic=""))
        if result.state != ExecutionState.SUCCEEDED:
            return result
        # The one file this job was authorized to produce, relative to the
        # download root; core verifies it before believing it.
        relative = Path(expected).relative_to(Path(self.configuration.local_root).resolve()).as_posix()
        return ExecutionObservation(result.handle, result.state, result.progress, result.error, result.activity,
                                    result.controls, MaterializationResult(MaterializationKind.FILE, (
                                        MaterializedEntry(relative, result.progress.total_bytes or None),)))

    async def observe_many(self, handles: tuple[ExecutionHandle, ...]) -> ExecutionSnapshot:
        if not handles:
            return ExecutionSnapshot(())
        permitted = {}
        results = []
        for handle in handles:
            try:
                gid = await self._check(handle, "observe")
                permitted[gid] = handle
            except Exception as exc:
                results.append(ExecutionObservation(handle, ExecutionState.UNKNOWN,
                    error=exception_failure(exc, stage=Stage.RECONCILIATION, secrets=self._secrets(handle))))
        if not permitted:
            return ExecutionSnapshot(tuple(results))
        try:
            keys = self.client._keys()
            cfg = self.configuration
            snapshots = await self.client._multicall([
                ("aria2.tellActive", [keys]),
                ("aria2.tellWaiting", [0, max(10, min(1000, cfg.waiting_window)), keys]),
                ("aria2.tellStopped", [0, max(10, min(1000, cfg.stopped_window)), keys]),
            ])
            if len(snapshots) != 3 or any(not isinstance(items, list) for items in snapshots):
                raise self._failure(Category.EXECUTOR_PROTOCOL_VIOLATION)
            found = {}
            for items in snapshots:
                for item in items:
                    if not isinstance(item, dict):
                        raise self._failure(Category.EXECUTOR_PROTOCOL_VIOLATION)
                    gid = str(item.get("gid") or "")
                    if gid in permitted:
                        found[gid] = self.client._normalize(item)
            for gid, handle in permitted.items():
                if gid in found:
                    try:
                        exact = await self._recover_redactions(handle, found[gid])
                        results.append(self._observation(handle, found[gid], exact_redaction=exact))
                    except Exception as exc:
                        results.append(ExecutionObservation(handle, ExecutionState.UNKNOWN,
                            error=exception_failure(exc, stage=Stage.RECONCILIATION, secrets=self._secrets(handle))))
                else:
                    # Bulk windows are incomplete and jobs can move between
                    # lists. Only per-handle confirmation can prove absence.
                    results.append(await self.observe(handle))
            return ExecutionSnapshot(tuple(results))
        except Exception as exc:
            # A failed snapshot never becomes an empty/absent snapshot. Native
            # bulk errors may contain any requested capability, so redact all.
            secrets = tuple(value for handle in handles for value in self._secrets(handle))
            return ExecutionSnapshot((), exception_failure(exc, stage=Stage.RECONCILIATION, secrets=secrets))

    async def _control(self, handle: ExecutionHandle, *, resume: bool) -> ExecutionObservation:
        action = "resume" if resume else "pause"
        expected = ({ExecutionState.RUNNING, ExecutionState.QUEUED, ExecutionState.SUCCEEDED}
                    if resume else {ExecutionState.PAUSED, ExecutionState.SUCCEEDED})
        try:
            gid = await self._check(handle, action)
            before = await self.observe(handle)
            if before.state in expected:
                return before
            if before.error or not before.resumable:
                return before
            await self._check(handle, action)
            mutation_error = None
            try:
                # Ordinary interactive Pause is cooperative. forcePause remains
                # reserved for explicit destructive/cleanup semantics.
                await self.client._call("aria2.unpause" if resume else "aria2.pause", [gid])
            except Exception as exc:
                # RPC acknowledgement is not execution truth. The mutation may
                # have reached aria2, so observe through a bounded convergence
                # window before deciding that control is unresolved.
                mutation_error = exception_failure(exc, stage=Stage.RECONCILIATION, secrets=self._secrets(handle))

            loop = asyncio.get_running_loop()
            deadline = loop.time() + max(0.01, float(self.configuration.control_confirmation_timeout))
            last = before
            while True:
                last = await self.observe(handle)
                if last.state in expected:
                    return last
                if last.state in {ExecutionState.FAILED, ExecutionState.CANCELLED, ExecutionState.ABSENT}:
                    return last
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                delay = max(0.01, float(self.configuration.confirmation_delay))
                await asyncio.sleep(min(delay, remaining))

            diagnostic = mutation_error.diagnostic if mutation_error else (last.error.diagnostic if last.error else "")
            return ExecutionObservation(handle, ExecutionState.UNKNOWN, error=NormalizedError(
                Domain.RECONCILIATION, Category.RECONCILIATION_FAILED, Stage.RECONCILIATION,
                retryability=Retryability.BACKOFF, integration_id=self.descriptor.id,
                diagnostic=diagnostic))
        except _AdmissionDeferred:
            return await self.observe(handle)
        except Exception as exc:
            error = exception_failure(exc, stage=Stage.RECONCILIATION, secrets=self._secrets(handle))
            if error.category == Category.UNMAPPED_EXECUTOR_ERROR:
                error = NormalizedError(
                    Domain.RECONCILIATION, Category.RECONCILIATION_FAILED, Stage.RECONCILIATION,
                    retryability=Retryability.BACKOFF, integration_id=self.descriptor.id,
                    diagnostic=error.diagnostic,
                )
            return ExecutionObservation(handle, ExecutionState.UNKNOWN, error=error)

    async def pause(self, handle: ExecutionHandle) -> ExecutionObservation:
        return await self._control(handle, resume=False)

    async def resume(self, handle: ExecutionHandle) -> ExecutionObservation:
        return await self._control(handle, resume=True)

    async def cancel(self, handle: ExecutionHandle) -> ExecutionObservation:
        """Remove this owned job and report observed truth.

        The remove RPC acknowledgement is never the answer: the job must be
        observed stopped within the bounded confirmation window. A lost or
        unconfirmed acknowledgement stays UNKNOWN so core keeps ownership."""
        try:
            gid = await self._check(handle, "cancel")
            before = await self.observe(handle)
            if before.state == ExecutionState.UNKNOWN:
                return before
            if before.resumable:
                mutation_error = None
                try:
                    await self.client._call("aria2.forceRemove", [gid])
                except Exception as exc:
                    mutation_error = exception_failure(exc, stage=Stage.CLEANUP, secrets=self._secrets(handle))
                last = await self._confirm_stopped(handle)
                if not last.stopped:
                    return ExecutionObservation(last.handle, ExecutionState.UNKNOWN, last.progress, NormalizedError(
                        Domain.RECONCILIATION, Category.RECONCILIATION_FAILED, Stage.CLEANUP,
                        retryability=Retryability.BACKOFF, integration_id=self.descriptor.id,
                        diagnostic=(mutation_error.diagnostic if mutation_error else
                                    (last.error.diagnostic if last.error else ""))))
            # Only this job's own stopped result is removed. This never changes
            # global daemon options, purges results, or mutates unowned jobs.
            if before.state != ExecutionState.ABSENT:
                try:
                    await self.client._call("aria2.removeDownloadResult", [gid])
                except Exception as exc:
                    if not is_missing(exc, gid):
                        raise
            if before.resumable:
                # Removed by this command and then positively observed stopped.
                return ExecutionObservation(self._bound(handle), ExecutionState.CANCELLED, before.progress)
            return before
        except Exception as exc:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN,
                                        error=exception_failure(exc, stage=Stage.CLEANUP, secrets=self._secrets(handle)))

    async def _confirm_stopped(self, handle: ExecutionHandle) -> ExecutionObservation:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.01, float(self.configuration.control_confirmation_timeout))
        while True:
            last = await self.observe(handle)
            if last.stopped:
                return last
            remaining = deadline - loop.time()
            if remaining <= 0:
                return last
            await asyncio.sleep(min(max(0.01, float(self.configuration.confirmation_delay)), remaining))

    async def health(self) -> ExecutorHealth:
        try:
            await self.client.test()
        except Exception as exc:
            return ExecutorHealth(False, False, error=exception_failure(exc, secrets=self.configuration.secrets))
        return ExecutorHealth(True, True, frozenset({ExecutorRuntimeCapability.AGGREGATE_BANDWIDTH_CEILING}))

    async def set_bandwidth_ceiling(self, bytes_per_second: int) -> ExecutorRuntimeControlResult:
        """Enforce the core-assigned aggregate ceiling as aria2's daemon-wide
        download limit (every job in the managed daemon is DP-owned), and
        confirm it by reading the native option back. aria2's own limiter
        averages over a trailing window and, after its limit is raised, runs
        ahead of it until the average catches up; every job's connections also
        draw on aria2's egress download budget at the same rate, which holds
        the share at every moment."""
        requested = max(0, int(bytes_per_second))
        self.egress.budget(self.descriptor.id).set_rate(requested)
        if self.runtime is not None:
            self.runtime.assign_bandwidth_ceiling(requested)
        try:
            await self.client.change_global_options({_OVERALL_DOWNLOAD_LIMIT: str(requested)})
            effective = int((await self.client.get_global_options()).get(_OVERALL_DOWNLOAD_LIMIT) or 0)
        except Exception as exc:
            return ExecutorRuntimeControlResult(requested, None, exception_failure(exc, secrets=self.configuration.secrets))
        return ExecutorRuntimeControlResult(requested, effective if effective == requested else None)
