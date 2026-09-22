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
from pathlib import Path
import re
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from executors.aria2.client import Aria2Service
from executors.aria2.translation import exception_failure, is_missing, observation
from services.artifact_sampling import (
    SAMPLED_FINGERPRINT_SCHEMES, AccessRequired, ftp_fingerprint, sampled_public_artifact_fingerprint,
    sftp_fingerprint,
)
from services.downloader_egress_guard import RouteScope, downloader_egress_guard
from services.network_safety import DestinationLookupError, validate_resolved_public_destination
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.input_required import SubmittedInput, auth_required, server_identity_required, username_password
from transfers.models import (
    ArtifactFingerprint, ExecutionActivity, ExecutionControl, ExecutionFootprint, ExecutionHandle, ExecutionObservation, ExecutionRequest,
    ExecutionState, ExecutionSnapshot, ExecutorCapabilities, ExecutorClaim, ExecutorHealth,
    ExecutorRuntimeCapability, ExecutorRuntimeControlResult, FingerprintKind, InputFactName, InputField,
    InputMethod, InputReason, InputRequirement, IntegrationDescriptor, MaterializationKind, MaterializationResult,
    MaterializedEntry,
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
# restricted to subsets of ECDSA/Ed25519/RSA keys). Evidence acquisition asks for
# the same order, so the identity an operator confirms is the one aria2 verifies.
_NATIVE_HOST_KEY_ORDER = (
    "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
    "ssh-ed25519", "rsa-sha2-512", "rsa-sha2-256", "ssh-rsa",
)
_SHA1_IDENTITY = re.compile(r"[0-9a-f]{40}")
# Positive transport claim only: a transport aria2 actually delivers and that
# the canonical destination validator and egress guard cover. Everything else
# -- scp, rsync, ftps, webdav, metalink, magnet, native torrent -- is
# unsupported by absence, never by a denial list. Executor-private: core never
# routes by it.
SUPPORTED_SCHEMES = frozenset({"http", "https", "ftp", "sftp"})
_OVERALL_DOWNLOAD_LIMIT = "max-overall-download-limit"


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
    capabilities = ExecutorCapabilities(
        candidate_sampling=True, per_execution_pause=True, aggregate_bandwidth_ceiling=True,
        transient_input=True, materialization_kinds=frozenset({MaterializationKind.FILE}),
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
            if credentials is not None:
                if provider_authorization:
                    return None
                headers["Authorization"] = "Basic " + base64.b64encode(":".join(credentials).encode()).decode()
            result = await sampled_public_artifact_fingerprint(
                endpoint.address, headers=headers, expected_bytes=max(0, int(candidate.expected_bytes or 0)),
            )
            if isinstance(result, AccessRequired):
                return auth_required(username_password()) if accepts_input and not provider_authorization else refused
            return ArtifactFingerprint(*result) if result else None
        # FTP/SFTP evidence reaches its origin only through the egress guard,
        # after the same destination validation execution applies.
        try:
            await validate_resolved_public_destination(endpoint.address)
        except DestinationLookupError:
            return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "dns_failure")
        except ValueError:
            return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "destination_rejected")
        if endpoint.scheme == "ftp":
            username, password = credentials or (_ANONYMOUS_LOGIN["ftp-user"], _ANONYMOUS_LOGIN["ftp-passwd"])
            result = await ftp_fingerprint(
                endpoint.address, username=username, password=password,
                connect=lambda port=None: self.egress.open_tunnel(endpoint.address, scope=RouteScope.SAME_HOST,
                                                                  port=port),
            )
            if isinstance(result, AccessRequired):
                return auth_required(username_password()) if accepts_input else refused
            return ArtifactFingerprint(*result)
        if endpoint.scheme == "sftp":
            # SFTP evidence always needs the operator: a confirmed identity and
            # a credential. Without advertised input there is no sample.
            if not accepts_input:
                return None
            host = str(urlsplit(endpoint.address).hostname or "").rstrip(".").casefold()
            identity = None
            if submitted is not None:
                identity = self._confirmed_evidence_identity(host, submitted)
                if identity is None:
                    return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "destination_rejected")
            result = await sftp_fingerprint(
                endpoint.address, connect=lambda port=None: self.egress.open_tunnel(endpoint.address),
                host_key_algorithms=_NATIVE_HOST_KEY_ORDER, host_identity=identity,
                username=credentials[0] if credentials else "", password=credentials[1] if credentials else "",
            )
            if isinstance(result, AccessRequired):
                if not host or not _SHA1_IDENTITY.fullmatch(result.server_identity) \
                        or result.server_identity == _HOST_KEY_SENTINEL:
                    return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "destination_rejected")
                return server_identity_required(username_password(), host=host, algorithm="sha-1",
                                                fingerprint=result.server_identity)
            return ArtifactFingerprint(*result)
        return None

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
            # candidates that accept no input.
            return auth_required(username_password()) if code == "24" else None
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
                       submitted: SubmittedInput | None = None, *, host_identity: str | None = None) -> tuple[str, dict]:
        endpoint = self._endpoint(request.work.subject.candidate)
        if endpoint is None or urlsplit(endpoint.address).scheme != endpoint.scheme:
            raise self._failure(Category.UNSUPPORTED_CAPABILITY, Stage.QUEUE)
        try:
            address = await validate_resolved_public_destination(endpoint.address)
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
            guarded = self.egress.job_options(address, scope=scope)
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
            "continue": "true" if cfg.continue_downloads else "false",
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
                # an aria2 challenge cache cannot replay a superseded credential.
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
            address, options = await self._options(request, handle, submitted, host_identity=host_identity)
            # A deletion can revoke authority during DNS or egress startup.
            await self._check(handle, "start")
            returned = await self.client._call("aria2.addUri", [[address], options])
            if str(returned) != gid:
                raise self._failure(Category.EXECUTOR_PROTOCOL_VIOLATION)
            return self._accepted(handle, paused=request.paused)
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
            address, options = await self._options(request, handle, submitted, host_identity=host_identity)
            await self._check(handle, "resume")
            returned = await self.client._call("aria2.addUri", [[address], options])
            if str(returned) != gid:
                raise self._failure(Category.EXECUTOR_PROTOCOL_VIOLATION)
            return self._accepted(handle, paused=request.paused)
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
        confirm it by reading the native option back."""
        requested = max(0, int(bytes_per_second))
        if self.runtime is not None:
            self.runtime.assign_bandwidth_ceiling(requested)
        try:
            await self.client.change_global_options({_OVERALL_DOWNLOAD_LIMIT: str(requested)})
            effective = int((await self.client.get_global_options()).get(_OVERALL_DOWNLOAD_LIMIT) or 0)
        except Exception as exc:
            return ExecutorRuntimeControlResult(requested, None, exception_failure(exc, secrets=self.configuration.secrets))
        return ExecutorRuntimeControlResult(requested, effective if effective == requested else None)
