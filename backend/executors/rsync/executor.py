"""Canonical rsync execution boundary: one owned native process per attempt.

rsync is a transport, not a policy owner. This executor moves the bytes of one
exact remote regular file into the one target core planned, under the one
continuation plan core authorized, and reports neutral facts; identity,
equivalence, retry, failover, switching, pause and material truth stay core's.

What was characterized against the packaged rsync 3.4.1 (and why the
implementation is shaped as it is):

* ``--append`` writes in place at the target and grows it strictly from the
  existing length; after SIGTERM (exit 20) the target holds an exact prefix of
  the source. So a FILE continues exactly at a core-authorized offset
  (CONTIGUOUS_FROM_OFFSET + IMPORT_EXISTING_MATERIAL) and the target's length
  IS exact final-file evidence (EXPORT_MATERIAL_RANGES) -- never a byte
  counter. With Partial Transfers off, rsync writes a private temporary file
  (inside this executor's declared footprint) and nothing reaches the target
  before completion; then only FULL_RESTART is declared.
* Nothing at the destination is reused unless core authorized it: the target
  is cut to the plan boundary (or emptied) before rsync starts, and
  ``--ignore-times`` is always passed, so rsync's size/mtime quick check can
  never mark a file done. Exit 0 is not completion either (rsync exits 0 after
  skipping a non-regular source, or when the destination is already longer):
  success is reported only for a regular target of exactly the source length.
* rsync expands wildcards in a source path in a daemon and over SSH alike, so
  every path is passed literally escaped (``translation.literal_path``).
* A daemon at its ``max connections`` limit refuses at once with a
  distinguishable error; it never queues. That is reported as the neutral
  remote capacity fact; core alone decides admission and waiting.
* Neither native pause nor native private resume is declared: stopping an
  rsync process ends its session, and keeping a stopped one would hold a
  server connection slot for a paused transfer. A new process continues
  portably from DebridPulse material under a new plan.

Execution runs through the process-ownership owner
(``executors.process_ownership``): a durable, flock-proven identity per
attempt, never a duplicate start. Transport is the executor's: a daemon is
reached through the egress guard's authenticated CONNECT (``RSYNC_PROXY``),
SSH through the one SSH exec channel (``services.ssh_channel``) over a guard
tunnel opened here. Discovery, evidence and execution share one preparation
(``_transport``), so none of them can trust, authenticate or route differently.
Secrets never reach argv, the environment, a file or a diagnostic.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import sys
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import unquote, urlsplit

import services
from executors.process_ownership import OwnedProcess, ProcessGroupAlive, ProcessOwnership
from executors.rsync.translation import (
    ListingUnusable, channel_failure, listed_roots, listing_entries, literal_path, native_failure,
)
from services import ssh_channel
from services.artifact_sampling import (
    MAX_LISTED_ENTRIES, SAMPLE_BYTES, SSH_HOST_KEY_ALGORITHMS, _offset_windows, last_window_start, sample_size,
    unavailable,
)
from services.downloader_egress_guard import TunnelTargetRefused, downloader_egress_guard
from services.network_safety import DestinationLookupError, validate_resolved_public_destination
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.filesystem import validate_target
from transfers.mirrors import REMOTE_CAPACITY_REASON
from transfers.policy import remote_source_capacity
from transfers.input_required import (
    SubmittedInput, auth_required, server_identity_required, username_password, username_private_key,
)
from transfers.models import (
    ArtifactFingerprint, ContinuationCapability, ContinuationStrategy, DiscoveredEntry, DiscoveryResult,
    ExecutionActivity, ExecutionFootprint, ExecutionHandle, ExecutionObservation, ExecutionRequest, ExecutionSnapshot,
    ExecutionState, ExecutorCapabilities, ExecutorClaim, ExecutorHealth, ExecutorRuntimeCapability,
    ExecutorRuntimeControlResult, FingerprintKind, InputFactName, InputField,
    InputMethod, InputReason, InputRequirement, IntegrationDescriptor, MaterializationKind, MaterializationResult,
    MaterializedEntry, RemoteObjectKind, TransferProgress,
)

# Positive transport claim only; every other scheme is unsupported by absence.
SUPPORTED_SCHEMES = frozenset({"rsync", "rsync+ssh"})
# The oldest client whose behaviour this executor was characterized against.
MINIMUM_VERSION = (3, 4, 0)
_VERSION = re.compile(rb"rsync\s+version\s+v?(\d+)\.(\d+)\.(\d+)")
_DAEMON_PORT = 873
# The record rsync prints when a file's transfer begins (``--out-format``),
# carrying the source's own length: the native end of the connection phase.
_BEGUN = b"dp-transfer-begun "
_SHA1 = re.compile(r"[0-9a-f]{40}")
_STDERR_LIMIT = 16 * 1024
_LISTING_LIMIT = 16 * 1024 * 1024
_TERMINATE_GRACE = 5.0
_HEALTH_TTL = 60.0
_FINISHED_MEMORY = 4096
# Bounded waits (1.5 s in all) for a connection-limited server to free the slot
# of the evidence session just before this one.
_SESSION_RELEASE_WAITS = (0.1, 0.2, 0.4, 0.8)
# A daemon module's login refusals: no login supplied, or the one supplied rejected.
_DAEMON_LOGIN = frozenset({Category.AUTHENTICATION_FAILED, Category.CREDENTIAL_MISSING})


@dataclass(frozen=True)
class RsyncConfiguration:
    local_root: str
    runtime_dir: str
    partial_transfers: bool = True
    compression: bool = False
    preserve_modification_time: bool = True
    connection_timeout_seconds: int = 30
    transfer_timeout_seconds: int = 300
    binary: str = "rsync"
    python: str = sys.executable


@dataclass(frozen=True)
class _Remote:
    """One remote object, decoded once from a canonical endpoint address."""
    scheme: str
    address: str
    host: str
    port: int
    segments: tuple[str, ...]
    home: bool

    @property
    def daemon(self) -> bool:
        return self.scheme == "rsync"

    def native(self, *, directory: bool = False, segments: tuple[str, ...] | None = None) -> str:
        """The native source argument: a literal path (never a pattern), a
        daemon URL naming the real host (the egress guard authorizes it), or the
        SSH channel's placeholder host."""
        parts = self.segments if segments is None else segments
        joined = "/".join(parts)
        suffix = "/" if directory and parts else ""
        if self.daemon:
            host = f"[{self.host}]" if ":" in self.host else self.host
            authority = host if self.port == _DAEMON_PORT else f"{host}:{self.port}"
            return f"rsync://{authority}/" + literal_path(joined) + suffix
        if self.home:
            path = "/".join(parts[1:])
            path = f"./{path}" if path.startswith("-") else path
        else:
            path = "/" + joined
        return f"{ssh_channel.CHANNEL_HOST}:" + literal_path(path) + suffix


@dataclass
class _Transport:
    """Everything one native invocation needs to reach one source.
    ``options(descriptors)`` receives the inherited secret pipes' numbers."""
    options: Callable[[dict[str, int]], list[str]]
    env: dict[str, str]
    secrets: dict[str, bytes]
    inherited: list = field(default_factory=list)
    stdin: bytes | None = None
    status: int | None = None
    status_child: int | None = None
    redactions: tuple[str, ...] = ()

    def argv(self, head: list[str], tail: list[str], descriptors: dict[str, int]) -> list[str]:
        return [*head, *self.options(descriptors), "--", *tail]

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return tuple(item.fileno() for item in self.inherited) + (
            (self.status_child,) if self.status_child is not None else ())

    def discard(self) -> None:
        """Release everything, including the parent's status reader."""
        self.close()
        if self.status is not None:
            try:
                os.close(self.status)
            except OSError:
                pass
            self.status = None

    def close(self) -> None:
        for item in self.inherited:
            try:
                item.close() if hasattr(item, "close") else os.close(item)
            except OSError:
                pass
        self.inherited.clear()
        if self.status_child is not None:
            try:
                os.close(self.status_child)
            except OSError:
                pass
            self.status_child = None


@dataclass
class _Run:
    """One owned native process of one attempt, in this process's memory."""
    owned: OwnedProcess
    target: Path
    staging: Path
    expected: int
    appending: bool
    status: int | None
    redactions: tuple[str, ...]
    started: float
    begun: bool = False
    reported_length: int | None = None
    stderr: bytearray = field(default_factory=bytearray)
    records: list = field(default_factory=list)
    cancelled: bool = False
    timed_out: bool = False
    tasks: list = field(default_factory=list)
    sample: tuple[float, int] = (0.0, 0)
    terminal: ExecutionObservation | None = None


class _Refused(Exception):
    """A discovery/evidence invocation ended in a typed fact."""

    def __init__(self, outcome):
        super().__init__("refused")
        self.outcome = outcome


class RsyncExecutor:
    descriptor = IntegrationDescriptor("rsync", "rsync", frozenset())
    capabilities = ExecutorCapabilities(
        candidate_sampling=True, transient_input=True, remote_discovery=True, aggregate_bandwidth_ceiling=True,
        materialization_kinds=frozenset({MaterializationKind.FILE}),
        continuation=frozenset({
            ContinuationCapability.FULL_RESTART, ContinuationCapability.CONTIGUOUS_FROM_OFFSET,
            ContinuationCapability.IMPORT_EXISTING_MATERIAL, ContinuationCapability.EXPORT_MATERIAL_RANGES,
        }),
    )

    def __init__(self, configuration: RsyncConfiguration,
                 authorize: Callable[[ExecutionHandle, str], Awaitable[bool]], *, egress=None):
        self.configuration = configuration
        self.authorize = authorize
        self.egress = egress or downloader_egress_guard
        self.processes = ProcessOwnership(configuration.runtime_dir)
        self.evidence_dir = Path(configuration.runtime_dir) / "evidence"
        self._runs: dict[str, _Run] = {}
        self._finished: OrderedDict[str, None] = OrderedDict()
        self._health: tuple[float, ExecutorHealth] | None = None
        self.binding = f"{Path(configuration.local_root).resolve()}|{Path(configuration.runtime_dir).resolve()}"
        if not configuration.partial_transfers:
            # Partial Transfers off: nothing partial ever reaches the target, so
            # nothing can be continued -- core plans restarts for rsync.
            self.capabilities = replace(type(self).capabilities, continuation=frozenset({
                ContinuationCapability.FULL_RESTART}))

    # ── claim, identity, footprint ─────────────────────────────────────────

    def claim(self, subject) -> ExecutorClaim:
        """Pure: a subject is claimed when one of its candidate endpoints is an
        rsync transport this executor delivers."""
        return ExecutorClaim(self._endpoint(subject.candidate) is not None)

    @staticmethod
    def _endpoint(candidate):
        return next((item for item in candidate.endpoints if item.scheme in SUPPORTED_SCHEMES), None)

    def _failure(self, category: Category, stage=Stage.EXECUTION, *, domain=Domain.EXECUTOR,
                 retryability=Retryability.NEVER) -> TransferError:
        return TransferError(NormalizedError(domain, category, stage, retryability=retryability,
                                            integration_id=self.descriptor.id))

    def _remote(self, candidate, stage=Stage.QUEUE) -> _Remote:
        endpoint = self._endpoint(candidate)
        if endpoint is None:
            raise self._failure(Category.UNSUPPORTED_CAPABILITY, stage, domain=Domain.REQUEST)
        address = str(endpoint.address)
        try:
            parts = urlsplit(address)
            port = parts.port
        except ValueError:
            raise self._failure(Category.INVALID_REQUEST, stage, domain=Domain.REQUEST) from None
        host = str(parts.hostname or "").rstrip(".").casefold()
        if (parts.scheme.casefold() != endpoint.scheme or not host or parts.query or parts.fragment
                or parts.username is not None or parts.password is not None):
            raise self._failure(Category.INVALID_REQUEST, stage, domain=Domain.REQUEST)
        raw = [item for item in parts.path.split("/")[1:]]
        if raw and raw[-1] == "":
            raw.pop()
        segments = tuple(unquote(item) for item in raw)
        if any(not item or item in {".", ".."} or "/" in item
               or any(ord(char) < 32 or ord(char) == 127 for char in item) for item in segments):
            raise self._failure(Category.INVALID_REQUEST, stage, domain=Domain.REQUEST)
        daemon = endpoint.scheme == "rsync"
        home = not daemon and bool(segments) and segments[0] == "~"
        if not daemon and (not segments or segments == ("~",) or (segments[0].startswith("~") and not home)):
            raise self._failure(Category.INVALID_REQUEST, stage, domain=Domain.REQUEST)
        default = _DAEMON_PORT if daemon else 22
        return _Remote(endpoint.scheme, address, host, port or default, segments, home)

    def _target(self, target) -> Path:
        path = validate_target(self.configuration.local_root, str(target or ""))
        return path.resolve()

    @staticmethod
    def _plan_target(request: ExecutionRequest) -> str:
        plan = request.work.materialization
        if plan.kind != MaterializationKind.FILE or plan.target is None:
            raise TransferError(NormalizedError(Domain.REQUEST, Category.UNSUPPORTED_CAPABILITY, Stage.QUEUE,
                                                retryability=Retryability.NEVER, integration_id="rsync"))
        return plan.target

    @staticmethod
    def _staging(target: Path) -> Path:
        """This executor's private temporary tree for one target: one writer
        per artifact at a time, so one tree per target is never shared."""
        return target.parent / f".{target.name}.dp-rsync"

    def footprint(self, work) -> ExecutionFootprint:
        """The private temporary tree beside the planned target is the only
        native transient material; the target itself is the plan's."""
        target = validate_target(self.configuration.local_root, work.materialization.target)
        return ExecutionFootprint(transient_trees=(str(self._staging(Path(target))),))

    def prepare(self, request: ExecutionRequest) -> ExecutionHandle:
        target = self._target(self._plan_target(request))
        self._remote(request.work.subject.candidate)
        if not request.attempt_id:
            raise self._failure(Category.INVALID_REQUEST)
        # Durable identity only: no address, credential or native option.
        return ExecutionHandle(self.descriptor.id, request.attempt_id,
                               {"target": str(target), "binding": self.binding})

    def prepare_with_input(self, request: ExecutionRequest, submitted: SubmittedInput) -> ExecutionHandle:
        """Preparation never needs input: access input applies only to a
        native start (``start_with_input``)."""
        return self.prepare(request)

    async def _check(self, handle: ExecutionHandle, action: str) -> bool:
        """Ownership first; then whether ``action`` is authorized now."""
        if handle.executor_id != self.descriptor.id or not await self.authorize(handle, "observe"):
            raise self._failure(Category.OWNERSHIP_CONFLICT, domain=Domain.LIFECYCLE)
        if handle.correlation.get("binding") != self.binding:
            raise self._failure(Category.EXECUTOR_UNAVAILABLE, retryability=Retryability.BACKOFF)
        self._target(handle.correlation.get("target"))
        return action == "observe" or await self.authorize(handle, action)

    # ── access input and identity ──────────────────────────────────────────

    @staticmethod
    def _methods(remote: _Remote, candidate) -> tuple:
        accepted = set(candidate.accepted_input_methods)
        methods = []
        if InputMethod.USERNAME_PASSWORD in accepted:
            methods.append(username_password())
        if not remote.daemon and InputMethod.USERNAME_PRIVATE_KEY in accepted:
            methods.append(username_private_key())
        return tuple(methods)

    @staticmethod
    def _confirmed_identity(host: str, submitted: SubmittedInput | None) -> str | None:
        """The exact SHA-1 identity the operator confirmed for ``host``, or None."""
        if submitted is None:
            return None
        facts = {fact.name: fact.value for fact in submitted.facts}
        identity = facts.get(InputFactName.SERVER_IDENTITY_FINGERPRINT, "")
        if (not host or facts.get(InputFactName.SERVER_HOST) != host
                or facts.get(InputFactName.SERVER_IDENTITY_ALGORITHM) != "sha-1" or not _SHA1.fullmatch(identity)):
            return None
        return identity

    @staticmethod
    def _credential(submitted: SubmittedInput | None) -> dict[str, str] | None:
        """The one supplied credential, or None when the input cannot log in."""
        if submitted is None:
            return {}
        username = submitted.value(InputField.USERNAME)
        if submitted.method == InputMethod.USERNAME_PASSWORD:
            password = submitted.value(InputField.PASSWORD)
            return {"username": username, "password": password} if username and password else None
        if submitted.method == InputMethod.USERNAME_PRIVATE_KEY:
            key = submitted.value(InputField.PRIVATE_KEY)
            if not username or not key:
                return None
            return {"username": username, "private_key": key, "passphrase": submitted.value(InputField.PASSPHRASE) or ""}
        return None

    def _requirement(self, remote: _Remote, candidate, outcome: str, facts: dict) -> InputRequirement | None:
        """A definitive access fact as the canonical question, when the
        candidate advertises input that could answer it."""
        methods = self._methods(remote, candidate)
        if not methods:
            return None
        if outcome == "identity_required":
            observed = str(facts.get("observed") or "")
            if not _SHA1.fullmatch(observed):
                return None
            return server_identity_required(*methods, host=remote.host, algorithm="sha-1", fingerprint=observed)
        if outcome in {"authentication_rejected", "key_unusable", "daemon_authentication"}:
            return auth_required(*methods)
        return None

    def _granted(self, candidate) -> dict:
        """Core's private-LAN grant, honored only while the operator's global
        policy is on; the guard re-checks both at every connection."""
        lan = bool(getattr(candidate, "private_network_grant", False)
                   and getattr(self.egress, "private_lan_enabled", False))
        return {"private_lan": True} if lan else {}

    # ── one preparation for discovery, evidence and execution ──────────────

    async def _transport(self, remote: _Remote, candidate, submitted: SubmittedInput | None, stage) -> _Transport:
        credential = self._credential(submitted)
        if credential is None or (remote.daemon and "private_key" in credential):
            raise self._failure(Category.INVALID_REQUEST, stage, domain=Domain.REQUEST)
        granted = self._granted(candidate)
        try:
            await validate_resolved_public_destination(remote.address, **granted)
        except DestinationLookupError as exc:
            raise TransferError(NormalizedError(Domain.NETWORK, Category.DNS_FAILURE, stage,
                retryability=Retryability.BACKOFF, integration_id=self.descriptor.id)) from exc
        except ValueError as exc:
            raise self._failure(Category.DESTINATION_BLOCKED, stage, domain=Domain.SECURITY) from exc
        cfg = self.configuration
        if remote.daemon:
            try:
                await self.egress.ensure_started()
                # The route carries rsync's own Connection Timeout: the guard's
                # hop to the server is bounded by it (rsync's --contimeout is not).
                proxy_host, proxy_port, user, token = self.egress.proxy_credential(
                    remote.address, connect_timeout_seconds=float(cfg.connection_timeout_seconds),
                    budget=self.descriptor.id, **granted)
            except Exception as exc:
                raise self._failure(Category.EGRESS_POLICY_VIOLATION, stage, domain=Domain.SECURITY) from exc
            extra = {"RSYNC_PROXY": f"{user}:{token}@{proxy_host}:{proxy_port}"}
            if credential.get("username"):
                extra["USER"] = credential["username"]
            # The password is read once from standard input (``-``); without
            # input it is explicitly empty, so a module that requires a login
            # answers with its own definitive refusal and nothing ever prompts.
            password = (credential.get("password") or "") + "\n"

            def daemon_options(_descriptors: dict[str, int]) -> list[str]:
                return [f"--contimeout={int(cfg.connection_timeout_seconds)}", "--password-file=-"]

            return _Transport(daemon_options, self.processes.environment(extra), {},
                              stdin=password.encode("utf-8"),
                              redactions=tuple(value for value in (token, credential.get("password")) if value))
        if not cfg.python or any(char.isspace() or char in "'\"" for char in cfg.python):
            # rsync splits its remote-shell program on spaces without a shell.
            raise self._failure(Category.INVALID_CONFIGURATION, stage)
        try:
            tunnel = await self.egress.open_tunnel(remote.address, timeout_seconds=float(cfg.connection_timeout_seconds),
                                                   budget=self.descriptor.id, **granted)
        except TunnelTargetRefused as exc:
            raise self._failure(Category.CONNECTION_REFUSED, stage, domain=Domain.NETWORK,
                                retryability=Retryability.BACKOFF) from exc
        except PermissionError as exc:
            raise self._failure(Category.CONNECTION_FAILED, stage, domain=Domain.NETWORK,
                                retryability=Retryability.BACKOFF) from exc
        except TimeoutError as exc:
            raise self._failure(Category.CONNECTION_TIMEOUT, stage, domain=Domain.NETWORK,
                                retryability=Retryability.BACKOFF) from exc
        except ValueError as exc:
            raise self._failure(Category.DESTINATION_BLOCKED, stage, domain=Domain.SECURITY) from exc
        except OSError as exc:
            raise self._failure(Category.CONNECTION_FAILED, stage, domain=Domain.NETWORK,
                                retryability=Retryability.BACKOFF) from exc
        status_read, status_write = os.pipe()
        os.set_blocking(status_read, False)
        spec = ssh_channel.channel_spec(
            host=remote.host, username=credential.get("username") or "", password=credential.get("password") or "",
            private_key=credential.get("private_key") or "", passphrase=credential.get("passphrase") or "",
            identity=self._confirmed_identity(remote.host, submitted), host_key_algorithms=SSH_HOST_KEY_ALGORITHMS,
            timeout=float(cfg.connection_timeout_seconds))
        backend = str(Path(services.__file__).resolve().parent.parent)
        env = self.processes.environment({"PYTHONPATH": backend, "PYTHONDONTWRITEBYTECODE": "1"})
        tunnel_fd = tunnel.fileno()

        def channel_options(descriptors: dict[str, int]) -> list[str]:
            # rsync splits its remote-shell program on spaces itself (no shell);
            # the interpreter path is checked to need no quoting.
            shell = ssh_channel.remote_shell(cfg.python, spec_fd=descriptors["spec"], status_fd=status_write,
                                             tunnel_fd=tunnel_fd)
            # The remote command's file arguments travel inside the rsync
            # protocol, never on a remote shell command line.
            return ["-e", " ".join(shell), "--secluded-args"]

        return _Transport(channel_options, env, {"spec": spec}, inherited=[tunnel], status=status_read,
                          status_child=status_write,
                          redactions=tuple(value for value in credential.values() if value))

    # ── bounded invocations: listings and evidence windows ─────────────────

    async def _invoke(self, argv_head: list[str], tail: list[str], transport: _Transport, *,
                      stop: Callable[[], bool] | None = None, limit: int = _LISTING_LIMIT):
        """One bounded native invocation under a throwaway ownership identity;
        returns ``(exit code, stdout, stderr, channel records)``."""
        identity = f"invocation:{uuid.uuid4().hex}"
        cfg = self.configuration
        try:
            owned = await self.processes.spawn(
                identity, [], env=transport.env, secrets=transport.secrets, stdin=transport.stdin,
                pass_fds=transport.pass_fds,
                fd_argv=lambda descriptors: transport.argv(argv_head, tail, descriptors))
        except BaseException:
            transport.discard()
            raise
        transport.close()
        deadline = time.monotonic() + cfg.connection_timeout_seconds + cfg.transfer_timeout_seconds
        stdout, stderr = bytearray(), bytearray()

        async def pump(stream, into, cap):
            while chunk := await stream.read(65536):
                if len(into) < cap:
                    into.extend(chunk[:cap - len(into)])

        pumps = [asyncio.ensure_future(pump(owned.process.stdout, stdout, limit)),
                 asyncio.ensure_future(pump(owned.process.stderr, stderr, _STDERR_LIMIT))]
        try:
            while owned.process.returncode is None:
                if (stop is not None and stop()) or time.monotonic() >= deadline or len(stdout) >= limit:
                    await self.processes.terminate(owned, identity, grace=1.0)
                    break
                try:
                    await asyncio.wait_for(asyncio.shield(owned.process.wait()), timeout=0.05)
                except TimeoutError:
                    pass
            await self.processes.terminate(owned, identity, grace=1.0)
            await asyncio.wait_for(asyncio.gather(*pumps, return_exceptions=True), timeout=5)
        finally:
            for task in pumps:
                task.cancel()
            records = self._status(transport.status)
            self.processes.forget(identity)
        overflow = len(stdout) >= limit
        return owned.process.returncode, bytes(stdout), bytes(stderr), records, overflow

    @staticmethod
    def _status(fd: int | None) -> list[dict]:
        if fd is None:
            return []
        data = bytearray()
        try:
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                data.extend(chunk)
        finally:
            os.close(fd)
        return ssh_channel.read_status(bytes(data))

    def _head(self, *extra: str) -> list[str]:
        cfg = self.configuration
        return [cfg.binary, "--no-motd", "--no-h", f"--timeout={int(cfg.transfer_timeout_seconds)}", *extra]

    def _outcome(self, remote: _Remote, candidate, code, stderr: bytes, records, secrets, stage) -> None:
        """Raise the typed fact a failed bounded invocation ended in."""
        for record in records:
            event = record.get("event")
            if event in {"identity_required", "authentication_rejected", "key_unusable"}:
                requirement = self._requirement(remote, candidate, event, record)
                if requirement is not None:
                    raise _Refused(requirement)
            if event in ssh_channel_failures():
                raise TransferError(channel_failure(event if event != "unavailable" else record.get("reason", ""),
                                                    stage=stage))
            if event == "exit" and record.get("status") == 127:
                raise TransferError(native_failure("127", stderr.decode("utf-8", "replace"), stage=stage,
                                                   secrets=secrets))
        error = native_failure(code, stderr.decode("utf-8", "replace"), stage=stage, secrets=secrets)
        if error.category in _DAEMON_LOGIN and remote.daemon:
            requirement = self._requirement(remote, candidate, "daemon_authentication", {})
            if requirement is not None:
                raise _Refused(requirement)
        raise TransferError(error)

    async def _list(self, remote: _Remote, candidate, submitted, *, segments=None, directory=False,
                    recursive=False, stage=Stage.RESOLUTION) -> bytes:
        transport = await self._transport(remote, candidate, submitted, stage)
        secrets = transport.redactions
        head = self._head("--list-only", "-8", *(["-r"] if recursive else []))
        if remote.daemon and not (remote.segments if segments is None else segments):
            # A daemon sends its module list only with its message of the day.
            head.remove("--no-motd")
        code, stdout, stderr, records, overflow = await self._invoke(
            head, [remote.native(directory=directory, segments=segments)], transport)
        if overflow:
            raise TransferError(NormalizedError(Domain.REQUEST, Category.UNSUPPORTED_REQUEST, stage,
                retryability=Retryability.NEVER, integration_id=self.descriptor.id, diagnostic="too_many_entries"))
        if code != 0:
            self._outcome(remote, candidate, code, stderr, records, secrets, stage)
        return stdout

    # ── discovery ───────────────────────────────────────────────────────────

    async def discover(self, subject, submitted: SubmittedInput | None = None, *, recursive: bool = False):
        """Read-only classification of one rsync path before any candidate
        exists, through exactly the preparation execution uses. A regular file
        is one file; a directory is its immediate regular files, or every
        regular file of its tree when ``recursive``; a daemon's server root is
        the tree of every root it advertises. Links and special files are never
        members and never followed. Only a complete listing is a result."""
        candidate = subject.candidate
        remote = self._remote(candidate, Stage.RESOLUTION)
        try:
            if remote.daemon and not remote.segments:
                roots = listed_roots(await self._list(remote, candidate, submitted, directory=True))
                entries = []
                for root in roots:
                    entries += self._tree(await self._list(remote, candidate, submitted, segments=(root,),
                                                           directory=True, recursive=recursive), prefix=root)
                return self._directory(entries)
            if not (remote.daemon and len(remote.segments) == 1):
                listed = listing_entries(await self._list(remote, candidate, submitted))
                if len(listed) != 1:
                    raise self._failure(Category.PROTOCOL_ERROR, Stage.RESOLUTION, domain=Domain.RESOLUTION)
                kind, size, _name = listed[0]
                if kind == "-":
                    return DiscoveryResult(kind=RemoteObjectKind.FILE, expected_bytes=max(0, size))
                if kind != "d":
                    raise TransferError(NormalizedError(Domain.REQUEST, Category.UNSUPPORTED_REQUEST,
                        Stage.RESOLUTION, retryability=Retryability.NEVER, integration_id=self.descriptor.id,
                        diagnostic="unsupported_type"))
            output = await self._list(remote, candidate, submitted, directory=True, recursive=recursive)
            return self._directory(self._tree(output, prefix=""))
        except _Refused as refused:
            return refused.outcome
        except ListingUnusable as exc:
            raise TransferError(NormalizedError(Domain.RESOLUTION, Category.PROTOCOL_ERROR, Stage.RESOLUTION,
                retryability=Retryability.NEVER, integration_id=self.descriptor.id,
                diagnostic=str(exc))) from None

    @staticmethod
    def _tree(output: bytes, *, prefix: str) -> list[tuple[str, int]]:
        members = []
        for kind, size, name in listing_entries(output):
            if kind != "-":
                continue  # the directory itself, subdirectories, links and specials
            if name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/")):
                raise ListingUnusable("unexpected member path")
            members.append((f"{prefix}/{name}" if prefix else name, size))
        return members

    def _directory(self, members: list[tuple[str, int]]) -> DiscoveryResult:
        if len(members) > MAX_LISTED_ENTRIES:
            raise TransferError(NormalizedError(Domain.REQUEST, Category.UNSUPPORTED_REQUEST, Stage.RESOLUTION,
                retryability=Retryability.NEVER, integration_id=self.descriptor.id, diagnostic="too_many_entries"))
        return DiscoveryResult(tuple(
            DiscoveredEntry(path.rsplit("/", 1)[-1], max(0, size), relative_path=path) for path, size in sorted(members)))

    # ── evidence ────────────────────────────────────────────────────────────

    async def fingerprint(self, subject):
        return await self._evidence(subject.candidate)

    async def fingerprint_with_input(self, subject, submitted: SubmittedInput):
        return await self._evidence(subject.candidate, submitted)

    async def _evidence(self, candidate, submitted: SubmittedInput | None = None):
        """Bounded neutral content evidence over the transport execution would
        use: the same two offset windows and digest as every other transport
        (``services.artifact_sampling._offset_windows``), each read by an
        rsync append from the window's offset into a private scratch file."""
        try:
            remote = self._remote(candidate, Stage.CANDIDATE_PREPARATION)
        except TransferError:
            return None
        try:
            # The size discovery already established for this candidate is the
            # window geometry; a file that changed since yields other bytes, so
            # it can only ever disprove, never prove, equivalence. Without one,
            # the file is listed first.
            total = max(0, int(candidate.expected_bytes or 0))
            if not total:
                listed = listing_entries(await self._list(remote, candidate, submitted,
                                                          stage=Stage.CANDIDATE_PREPARATION))
                if len(listed) != 1 or listed[0][0] != "-":
                    return ArtifactFingerprint(*unavailable("range_unsupported"))
                total = max(0, listed[0][1])

            size = sample_size(SAMPLE_BYTES)
            tail = last_window_start(total, size)
            fetched: dict[tuple[int, int], object] = {}

            async def read(offset: int, count: int):
                # Every window is its own server session, and a connection-
                # limited server frees a finished session's slot only once its
                # own side of that session has exited -- a moment after ours.
                # A capacity refusal can therefore be the server still releasing
                # the session just before this one (this proof's previous
                # window, or the previous proof's last): the window waits a
                # short, bounded moment and asks again. A server that stays
                # full is still reported as capacity, never a spent proof.
                for release in (*_SESSION_RELEASE_WAITS, None):
                    try:
                        return await self._window(remote, candidate, submitted, offset, count)
                    except TransferError as exc:
                        if release is None or not remote_source_capacity(exc.error):
                            return exc
                    except Exception as exc:  # replayed exactly when that window is asked for
                        return exc
                    await asyncio.sleep(release)

            async def window(offset: int, count: int) -> bytes | None:
                if offset == 0 and total > count and (tail, total - tail) not in fetched:
                    # The last window is read first: it ends by itself, while the
                    # first is stopped once it is complete -- and a stopped
                    # session holds a daemon's slot longer still, so on a
                    # connection-limited server it must be this proof's last.
                    fetched[(tail, total - tail)] = await read(tail, total - tail)
                outcome = fetched.pop((offset, count)) if (offset, count) in fetched else await read(offset, count)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

            return ArtifactFingerprint(*await _offset_windows(total, window, size))
        except _Refused as refused:
            return refused.outcome
        except TransferError as exc:
            if remote_source_capacity(exc.error):
                # The server refused the evidence connection for capacity: a
                # neutral wait, never a spent proof attempt.
                return ArtifactFingerprint(*unavailable(REMOTE_CAPACITY_REASON))
            reason = "destination_rejected" if exc.error.domain == Domain.SECURITY else (
                "timeout" if exc.error.category in {Category.CONNECTION_TIMEOUT, Category.READ_TIMEOUT}
                else "sampler_unavailable")
            return ArtifactFingerprint(*unavailable(reason))
        except ListingUnusable:
            return ArtifactFingerprint(*unavailable("sampler_unavailable"))

    async def _window(self, remote: _Remote, candidate, submitted, offset: int, count: int) -> bytes | None:
        self.evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        scratch = self.evidence_dir / uuid.uuid4().hex
        try:
            with open(scratch, "wb") as handle:
                handle.truncate(offset)
            transport = await self._transport(remote, candidate, submitted, Stage.CANDIDATE_PREPARATION)
            want = offset + count

            def enough() -> bool:
                try:
                    return scratch.stat().st_size >= want
                except OSError:
                    return False

            code, _out, stderr, records, _overflow = await self._invoke(
                self._head("-I", "--append"), [remote.native(), str(scratch)], transport, stop=enough)
            if not enough() and code not in {0}:
                self._outcome(remote, candidate, code, stderr, records, transport.redactions,
                              Stage.CANDIDATE_PREPARATION)
            with open(scratch, "rb") as handle:
                handle.seek(offset)
                data = handle.read(count)
            return data if len(data) == count else None
        finally:
            scratch.unlink(missing_ok=True)

    def input_requirement(self, candidate, observed: ExecutionObservation) -> InputRequirement | None:
        """Only definitive, typed access evidence of a failed execution is a
        question -- and only for a candidate that advertises input."""
        try:
            remote = self._remote(candidate)
        except TransferError:
            return None
        if observed.state != ExecutionState.FAILED or observed.error is None:
            return None
        code = str(observed.error.native_code or "")
        if code.startswith("ssh:"):
            facts = {}
            if code == "ssh:identity_required":
                facts["observed"] = str(observed.error.diagnostic or "").removeprefix("sha-1=")
            return self._requirement(remote, candidate, code[4:], facts)
        if remote.daemon and observed.error.category in _DAEMON_LOGIN:
            return self._requirement(remote, candidate, "daemon_authentication", {})
        return None

    # ── execution ───────────────────────────────────────────────────────────

    def _authorized_material(self, request: ExecutionRequest, target: Path, staging: Path) -> bool:
        """Put the target in exactly the state the core plan authorizes and
        return whether rsync continues it in place (``--append``).

        Nothing the plan does not retain survives: a contiguous plan cuts the
        target to its boundary and fails closed when that prefix is not
        physically present; any other plan empties the target (or, with
        Partial Transfers off, removes it). The private temporary tree never
        outlives a writer."""
        plan = request.continuation
        if plan is not None and plan.strategy == ContinuationStrategy.NATIVE_STATE_HANDOFF:
            raise self._failure(Category.RESOURCE_STATE_CONFLICT, Stage.QUEUE, domain=Domain.LIFECYCLE)
        target.parent.mkdir(parents=True, exist_ok=True)
        if staging.is_symlink() or (staging.exists() and not staging.is_dir()):
            raise self._failure(Category.PATH_POLICY_VIOLATION, Stage.QUEUE, domain=Domain.SECURITY)
        shutil.rmtree(staging, ignore_errors=True)
        try:
            info = target.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise self._failure(Category.LOCAL_PATH_CONFLICT, Stage.QUEUE, domain=Domain.LOCAL_RESOURCE,
                                retryability=Retryability.AFTER_RESOURCE_CHANGE)
        appending = ContinuationCapability.CONTIGUOUS_FROM_OFFSET in self.capabilities.continuation
        boundary = 0
        if (plan is not None and plan.strategy == ContinuationStrategy.CONTIGUOUS_FROM_OFFSET and plan.boundary > 0
                and appending):
            if info is None or info.st_size < plan.boundary:
                raise self._failure(Category.RESOURCE_STATE_CONFLICT, Stage.QUEUE, domain=Domain.LIFECYCLE)
            boundary = plan.boundary
        if appending:
            with open(target, "ab") as handle:
                handle.truncate(boundary)
        else:
            target.unlink(missing_ok=True)
            staging.mkdir(mode=0o700)
        return appending

    def _execution_head(self, appending: bool, staging: Path) -> list[str]:
        cfg = self.configuration
        # Never recursive, never links, never ownership/permission/ACL/xattr
        # replication, never a delete or a source-side change: exactly the
        # contents of one regular file into one local target, with ordinary
        # local permissions (umask-governed) and --ignore-times so no
        # quick check can mark it done.
        head = self._head("-I", "--chmod=F666", "--out-format=" + _BEGUN.decode("ascii") + "%l")
        if cfg.compression:
            head.append("--compress")
        if cfg.preserve_modification_time:
            head.append("--times")
        head += ["--append"] if appending else [f"--temp-dir={staging}"]
        return head

    async def start(self, request: ExecutionRequest, handle: ExecutionHandle) -> ExecutionObservation:
        return await self._start(request, handle)

    async def _start(self, request: ExecutionRequest, handle: ExecutionHandle,
                     submitted: SubmittedInput | None = None) -> ExecutionObservation:
        secrets = submitted.secret_values() if submitted is not None else ()
        try:
            if not await self._check(handle, "start"):
                return ExecutionObservation(handle, ExecutionState.PAUSED)
            if self.prepare(request) != handle:
                raise self._failure(Category.OWNERSHIP_CONFLICT, domain=Domain.LIFECYCLE)
            if handle.attempt_id in self._runs or self.processes.alive(handle.attempt_id):
                # This attempt already owns a native group: never a second one.
                return await self.observe(handle)
            candidate = request.work.subject.candidate
            remote = self._remote(candidate)
            target = self._target(self._plan_target(request))
            staging = self._staging(target)
            transport = await self._transport(remote, candidate, submitted, Stage.QUEUE)
            secrets += transport.redactions
            # A deletion or pause can revoke authority during DNS or egress setup.
            if not await self._check(handle, "start"):
                transport.discard()
                return ExecutionObservation(handle, ExecutionState.PAUSED)
            try:
                appending = self._authorized_material(request, target, staging)
            except BaseException:
                transport.discard()
                raise
            head = self._execution_head(appending, staging)
            try:
                owned = await self.processes.spawn(
                    handle.attempt_id, [], env=transport.env, secrets=transport.secrets, stdin=transport.stdin,
                    pass_fds=transport.pass_fds,
                    fd_argv=lambda descriptors: transport.argv(head, [remote.native(), str(target)], descriptors))
            except ProcessGroupAlive:
                transport.discard()
                return await self.observe(handle)
            except BaseException:
                transport.discard()
                raise
            transport.close()
            run = _Run(owned, target, staging, max(0, int(candidate.expected_bytes or 0)), appending,
                       transport.status, tuple(value for value in secrets if value), time.monotonic())
            self._runs[handle.attempt_id] = run
            run.tasks = [asyncio.ensure_future(self._follow(run)),
                         asyncio.ensure_future(self._pump_stderr(run)),
                         asyncio.ensure_future(self._watch(run))]
            return self._running(handle, run)
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else native_failure(
                "", type(exc).__name__, stage=Stage.QUEUE)
            uncertain = error.retryability == Retryability.UNKNOWN
            return ExecutionObservation(handle, ExecutionState.UNKNOWN if uncertain else ExecutionState.FAILED,
                                        error=error)

    async def start_with_input(self, request: ExecutionRequest, handle: ExecutionHandle,
                               submitted: SubmittedInput) -> ExecutionObservation:
        """Start with input: a fresh attempt whose input evidence acquisition
        already proved, or the same attempt again after its own native process
        ended in the definitive challenge this input answers."""
        run = self._runs.get(handle.attempt_id)
        if run is not None:
            before = await self.observe(handle)
            requirement = self.input_requirement(request.work.subject.candidate, before)
            if requirement is None or submitted.method not in {item.method for item in requirement.methods}:
                return ExecutionObservation(handle, ExecutionState.FAILED, error=NormalizedError(
                    Domain.LIFECYCLE, Category.RESOURCE_STATE_CONFLICT, Stage.QUEUE,
                    retryability=Retryability.NEVER, integration_id=self.descriptor.id))
            if requirement.reason == InputReason.SERVER_IDENTITY_REQUIRED and set(submitted.facts) != set(
                    requirement.facts):
                # Acceptance holds only for exactly the identity the operator saw.
                return ExecutionObservation(handle, ExecutionState.FAILED, error=NormalizedError(
                    Domain.LIFECYCLE, Category.RESOURCE_STATE_CONFLICT, Stage.QUEUE,
                    retryability=Retryability.NEVER, integration_id=self.descriptor.id))
            del self._runs[handle.attempt_id]
            self._finished.pop(handle.attempt_id, None)
            self.processes.forget(handle.attempt_id)
        return await self._start(request, handle, submitted)

    async def _follow(self, run: _Run) -> None:
        """Read rsync's own records: the begun record ends the connection phase."""
        stream = run.owned.process.stdout
        while line := await stream.readline():
            if line.startswith(_BEGUN) and not run.begun:
                run.begun = True
                try:
                    run.reported_length = int(line[len(_BEGUN):].strip() or b"0")
                except ValueError:
                    run.reported_length = None

    async def _pump_stderr(self, run: _Run) -> None:
        stream = run.owned.process.stderr
        while chunk := await stream.read(4096):
            room = _STDERR_LIMIT - len(run.stderr)
            if room > 0:
                run.stderr.extend(chunk[:room])

    async def _watch(self, run: _Run) -> None:
        """The Connection Timeout: until rsync's begun record, the executor --
        not a stall detector -- bounds how long reaching and opening the source
        may take. A daemon's capacity refusal is immediate and never waits here."""
        deadline = run.started + float(self.configuration.connection_timeout_seconds)
        while run.owned.process.returncode is None and not run.begun:
            if time.monotonic() >= deadline:
                run.timed_out = True
                await self.processes.terminate(run.owned, run.owned.attempt_id, grace=_TERMINATE_GRACE)
                return
            try:
                await asyncio.wait_for(asyncio.shield(run.owned.process.wait()), timeout=0.2)
            except TimeoutError:
                pass

    def _measured(self, run: _Run) -> int:
        try:
            if run.appending:
                info = run.target.lstat()
                return info.st_size if stat.S_ISREG(info.st_mode) else 0
            return sum(item.stat().st_size for item in run.staging.iterdir() if item.is_file())
        except OSError:
            return 0

    def _running(self, handle: ExecutionHandle, run: _Run) -> ExecutionObservation:
        size = self._measured(run)
        now = time.monotonic()
        then, before = run.sample
        rate = int((size - before) / (now - then)) if then and now > then and size >= before else 0
        run.sample = (now, size)
        total = run.expected or run.reported_length or 0
        # Until rsync's begun record the session is not yet proven (connected
        # and authenticated), so no byte counts as having arrived -- the
        # retained prefix already at the target is DebridPulse's, not this
        # writer's progress. Its material evidence is reported either way.
        return ExecutionObservation(
            handle, ExecutionState.RUNNING, TransferProgress(total, size if run.begun else 0, max(0, rate)),
            activity=ExecutionActivity(network_active=True, bandwidth_reservation_required=True,
                                       progress_expected=run.begun),
            material=((0, size),) if run.appending and size > 0 else None)

    async def _terminal(self, handle: ExecutionHandle, run: _Run) -> ExecutionObservation:
        if run.terminal is not None:
            return run.terminal
        try:
            # A descendant still holding rsync's output open never delays truth.
            await asyncio.wait_for(asyncio.gather(*run.tasks, return_exceptions=True), timeout=5)
        except TimeoutError:
            for task in run.tasks:
                task.cancel()
        records = self._status(run.status)
        run.status = None
        run.records = records
        code = run.owned.process.returncode
        total = run.expected or run.reported_length or 0
        size = self._measured(run)
        progress = TransferProgress(total, size)
        if run.cancelled:
            observation = ExecutionObservation(handle, ExecutionState.CANCELLED, progress,
                                               material=((0, size),) if run.appending and size > 0 else None)
        elif run.timed_out:
            observation = ExecutionObservation(handle, ExecutionState.FAILED, progress, NormalizedError(
                Domain.NETWORK, Category.CONNECTION_TIMEOUT, Stage.EXECUTION, retryability=Retryability.BACKOFF,
                integration_id=self.descriptor.id, native_code="connection_timeout"))
        elif code == 0:
            observation = self._completed(handle, run, total)
        else:
            observation = ExecutionObservation(handle, ExecutionState.FAILED, progress,
                                               self._native_error(code, run, records),
                                               material=((0, size),) if run.appending and size > 0 else None)
        run.terminal = observation
        self.processes.forget(handle.attempt_id)
        self._retain(handle.attempt_id)
        return observation

    def _retain(self, attempt_id: str) -> None:
        """Keep a bounded memory of finished runs, oldest forgotten first. A
        forgotten finished run is observed ABSENT -- its group is proven gone
        -- and a live run is never forgotten."""
        self._finished[attempt_id] = None
        self._finished.move_to_end(attempt_id)
        while len(self._finished) > _FINISHED_MEMORY:
            oldest, _ = self._finished.popitem(last=False)
            run = self._runs.get(oldest)
            if run is not None and run.terminal is not None:
                del self._runs[oldest]

    def _completed(self, handle: ExecutionHandle, run: _Run, total: int) -> ExecutionObservation:
        """Exit 0 is evidence, not completion: only a regular target of
        exactly the source's length is a success."""
        try:
            info = run.target.lstat()
        except FileNotFoundError:
            info = None
        if info is None or not stat.S_ISREG(info.st_mode):
            # rsync skipped a source that is no longer a regular file.
            return ExecutionObservation(handle, ExecutionState.FAILED, TransferProgress(total, 0), NormalizedError(
                Domain.RESOLUTION, Category.SOURCE_NOT_FOUND, Stage.EXECUTION,
                retryability=Retryability.AFTER_RERESOLUTION, integration_id=self.descriptor.id,
                native_code="0", diagnostic="source_not_regular"))
        lengths = {value for value in (run.expected, run.reported_length) if value}
        if not run.begun and (not lengths or info.st_size not in lengths):
            # Nothing began: rsync skipped the source (it is no longer one
            # regular file) and the target is not already the whole payload.
            return ExecutionObservation(handle, ExecutionState.FAILED, TransferProgress(total, 0), NormalizedError(
                Domain.RESOLUTION, Category.SOURCE_NOT_FOUND, Stage.EXECUTION,
                retryability=Retryability.AFTER_RERESOLUTION, integration_id=self.descriptor.id,
                native_code="0", diagnostic="source_not_regular"))
        if len(lengths) > 1 or (lengths and info.st_size not in lengths):
            return ExecutionObservation(handle, ExecutionState.FAILED, TransferProgress(total, info.st_size),
                NormalizedError(Domain.INTEGRITY, Category.SIZE_MISMATCH, Stage.VERIFICATION,
                                retryability=Retryability.AFTER_RERESOLUTION, integration_id=self.descriptor.id,
                                native_code="0"))
        size = info.st_size
        # The one file this execution was authorized to produce, relative to
        # the download root; core verifies it before believing it.
        relative = run.target.relative_to(Path(self.configuration.local_root).resolve()).as_posix()
        return ExecutionObservation(handle, ExecutionState.SUCCEEDED, TransferProgress(size, size),
                                    materialization=MaterializationResult(MaterializationKind.FILE, (
                                        MaterializedEntry(relative, size or None),)),
                                    material=((0, size),) if size > 0 and ContinuationCapability.EXPORT_MATERIAL_RANGES
                                    in self.capabilities.continuation else None)

    def _native_error(self, code: int, run: _Run, records) -> NormalizedError:
        diagnostic = bytes(run.stderr).decode("utf-8", "replace")
        for record in records:
            event = record.get("event")
            if event == "identity_required":
                observed = str(record.get("observed") or "")
                return channel_failure(event, diagnostic=f"sha-1={observed}" if _SHA1.fullmatch(observed) else "")
            if event in ssh_channel_failures():
                return channel_failure(event if event != "unavailable" else str(record.get("reason") or ""))
            if event == "exit" and record.get("status") == 127:
                return native_failure("127", diagnostic, secrets=run.redactions)
        if code is not None and code < 0:
            return native_failure("20", diagnostic, secrets=run.redactions)
        return native_failure(code, diagnostic, secrets=run.redactions)

    async def observe(self, handle: ExecutionHandle) -> ExecutionObservation:
        try:
            await self._check(handle, "observe")
        except TransferError as exc:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN, error=exc.error)
        run = self._runs.get(handle.attempt_id)
        if run is not None:
            if run.owned.process.returncode is None:
                return self._running(handle, run)
            return await self._terminal(handle, run)
        alive = self.processes.alive(handle.attempt_id)
        if alive:
            # A group this attempt started before a restart is still alive: it
            # is owned and running, but its native records are gone, so it
            # reports no progress or material -- only that it must be stopped
            # through ``cancel`` before anything else writes this artifact.
            return ExecutionObservation(handle, ExecutionState.RUNNING,
                                        activity=ExecutionActivity(network_active=True,
                                                                   bandwidth_reservation_required=True))
        if alive is False:
            self.processes.forget(handle.attempt_id)
        return ExecutionObservation(handle, ExecutionState.ABSENT)

    async def observe_many(self, handles: tuple[ExecutionHandle, ...]) -> ExecutionSnapshot:
        results = []
        for handle in handles:
            try:
                results.append(await self.observe(handle))
            except Exception as exc:
                results.append(ExecutionObservation(handle, ExecutionState.UNKNOWN, error=native_failure(
                    "", type(exc).__name__, stage=Stage.RECONCILIATION)))
        return ExecutionSnapshot(tuple(results))

    async def cancel(self, handle: ExecutionHandle) -> ExecutionObservation:
        """Stop this attempt's native group and report observed truth: only a
        group proven gone is CANCELLED; an unconfirmed stop stays UNKNOWN."""
        try:
            if not await self._check(handle, "cancel"):
                raise self._failure(Category.OWNERSHIP_CONFLICT, Stage.CLEANUP, domain=Domain.LIFECYCLE)
        except TransferError as exc:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN, error=exc.error)
        run = self._runs.get(handle.attempt_id)
        if run is not None:
            if run.owned.process.returncode is not None:
                return await self._terminal(handle, run)
            run.cancelled = True
            if not await self.processes.terminate(run.owned, handle.attempt_id, grace=_TERMINATE_GRACE):
                return self._unconfirmed(handle)
            return await self._terminal(handle, run)
        alive = self.processes.alive(handle.attempt_id)
        if alive:
            if not await self.processes.terminate(None, handle.attempt_id, grace=_TERMINATE_GRACE):
                return self._unconfirmed(handle)
            self.processes.forget(handle.attempt_id)
            return ExecutionObservation(handle, ExecutionState.CANCELLED)
        if alive is False:
            self.processes.forget(handle.attempt_id)
        return ExecutionObservation(handle, ExecutionState.ABSENT)

    def _unconfirmed(self, handle: ExecutionHandle) -> ExecutionObservation:
        return ExecutionObservation(handle, ExecutionState.UNKNOWN, error=NormalizedError(
            Domain.RECONCILIATION, Category.RECONCILIATION_FAILED, Stage.CLEANUP,
            retryability=Retryability.BACKOFF, integration_id=self.descriptor.id))

    async def health(self) -> ExecutorHealth:
        """The local rsync binary is present and at least the characterized
        version. A remote rsync over SSH is execution truth, not a startup
        assumption. Nothing is ever installed at runtime."""
        now = time.monotonic()
        if self._health is not None and now - self._health[0] < _HEALTH_TTL:
            return self._health[1]
        binary = shutil.which(self.configuration.binary)
        health = ExecutorHealth(False, False, error=NormalizedError(
            Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE, Stage.QUEUE, retryability=Retryability.BACKOFF,
            integration_id=self.descriptor.id, diagnostic="rsync_missing"))
        if binary is not None:
            try:
                process = await asyncio.create_subprocess_exec(
                    binary, "--version", stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, env=self.processes.environment())
                output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
                match = _VERSION.search(output or b"")
                version = tuple(int(item) for item in match.groups()) if match else None
            except (OSError, TimeoutError):
                version = None
            if version is not None and version >= MINIMUM_VERSION:
                health = ExecutorHealth(True, True, frozenset({ExecutorRuntimeCapability.AGGREGATE_BANDWIDTH_CEILING}))
            elif version is not None:
                health = ExecutorHealth(True, False, error=NormalizedError(
                    Domain.EXECUTOR, Category.INVALID_CONFIGURATION, Stage.QUEUE, retryability=Retryability.NEVER,
                    integration_id=self.descriptor.id, diagnostic="rsync_version_unsupported"))
        self._health = (now, health)
        return health

    async def set_bandwidth_ceiling(self, bytes_per_second: int) -> ExecutorRuntimeControlResult:
        """Enforce the core-assigned aggregate ceiling: every rsync connection,
        daemon or SSH, crosses the egress guard on a route that draws on this
        executor's one download budget, so all of rsync's concurrent transfers
        together stay within it -- whatever the server does -- and a change
        applies to running transfers at once."""
        requested = max(0, int(bytes_per_second))
        effective = self.egress.budget(self.descriptor.id).set_rate(requested)
        return ExecutorRuntimeControlResult(requested, effective if effective == requested else None)


def ssh_channel_failures() -> frozenset[str]:
    """Channel outcomes that are failures, not questions."""
    return frozenset({"identity_changed", "method_unsupported", "unavailable"})
