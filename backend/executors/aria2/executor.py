"""Canonical aria2 execution boundary with durable identity and scoped mutation.

Core persists a prepared handle before submitting. A lost response is recovered
by observing that same handle, never by a second uncorrelated addUri. Authorization
is injected by the application repository and checked before every native action.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from executors.aria2.client import Aria2Service
from executors.aria2.translation import exception_failure, is_missing, observation
from services.downloader_egress_guard import downloader_egress_guard
from services.network_safety import validate_resolved_public_destination
from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage, TransferError
from transfers.models import (
    CancellationInitiator, Capability, ExecutionHandle, ExecutionObservation,
    ExecutionRequest, ExecutionState, HealthObservation, IntegrationDescriptor,
    OutcomeKind, TransferOutcome,
)


@dataclass(frozen=True)
class Aria2Configuration:
    local_root: str
    remote_root: str = ""
    external: bool = True
    split: int = 1
    minimum_split_size: str = "10M"
    connections_per_server: int = 1
    continue_downloads: bool = True
    confirmation_delay: float = 0.05
    secrets: tuple[str, ...] = field(default=(), repr=False)


class Aria2Executor:
    descriptor = IntegrationDescriptor(
        "aria2", "aria2", frozenset({Capability.PAUSE, Capability.RESUME, Capability.RECONCILE, Capability.HEALTH}),
        schemes=frozenset({"http", "https"}),
    )

    def __init__(self, client: Aria2Service, configuration: Aria2Configuration,
                 authorize: Callable[[ExecutionHandle, str], Awaitable[bool]], *, egress=None):
        self.client = client
        self.configuration = configuration
        self.authorize = authorize
        self.egress = egress or downloader_egress_guard

    def _failure(self, category: Category, stage=Stage.EXECUTION, *, domain=Domain.EXECUTOR) -> TransferError:
        return TransferError(NormalizedError(domain, category, stage, retryability=Retryability.NEVER,
                                            recovery=Recovery.REQUIRE_OPERATOR, integration_id=self.descriptor.id))

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

    def prepare(self, request: ExecutionRequest) -> ExecutionHandle:
        target = self._target(request.target)
        if not request.attempt_id:
            raise self._failure(Category.INVALID_REQUEST)
        gid = hashlib.sha256(request.attempt_id.encode()).hexdigest()[:16]
        if gid == "0" * 16:
            gid = "1" + gid[1:]
        redactions = [value for endpoint in request.candidate.endpoints
                      for value in (endpoint.address, *endpoint.headers.values()) if value]
        return ExecutionHandle(self.descriptor.id, {"gid": gid, "target": str(target), "redactions": redactions}, request.attempt_id)

    def _secrets(self, handle: ExecutionHandle) -> tuple[str, ...]:
        return self.configuration.secrets + tuple(str(item) for item in handle.context.get("redactions", ()))

    async def _check(self, handle: ExecutionHandle, action: str) -> str:
        if handle.executor_id != self.descriptor.id or not await self.authorize(handle, action):
            raise self._failure(Category.OWNERSHIP_CONFLICT, domain=Domain.LIFECYCLE)
        gid = str(handle.context.get("gid") or "")
        if len(gid) != 16 or any(ch not in "0123456789abcdef" for ch in gid):
            raise self._failure(Category.INVALID_ADAPTER_RESPONSE)
        self._target(str(handle.context.get("target") or ""))
        return gid

    def resumable_paths(self, target: str) -> tuple[str, ...]:
        return (str(self._target(target)) + ".aria2",)

    def _remote_target(self, target: Path) -> str:
        if not self.configuration.remote_root:
            return str(target)
        relative = target.relative_to(Path(self.configuration.local_root).resolve())
        return str(PurePosixPath(self.configuration.remote_root) / PurePosixPath(relative.as_posix()))

    async def _options(self, request: ExecutionRequest, handle: ExecutionHandle) -> tuple[str, dict]:
        endpoint = next((item for item in request.candidate.endpoints if item.scheme in self.descriptor.schemes), None)
        if endpoint is None or urlsplit(endpoint.address).scheme != endpoint.scheme:
            raise self._failure(Category.UNSUPPORTED_CAPABILITY, Stage.QUEUE)
        try:
            address = await validate_resolved_public_destination(endpoint.address)
        except ValueError as exc:
            raise self._failure(Category.DESTINATION_BLOCKED, domain=Domain.SECURITY) from exc
        try:
            await self.egress.ensure_started()
            guarded = self.egress.job_options(address, external=self.configuration.external)
        except Exception as exc:
            raise self._failure(Category.EGRESS_POLICY_VIOLATION, domain=Domain.SECURITY) from exc
        target = self._target(request.target)
        remote = PurePosixPath(self._remote_target(target))
        cfg = self.configuration
        options = {
            "gid": handle.context["gid"], "dir": str(remote.parent), "out": remote.name,
            "allow-overwrite": "true", "auto-file-renaming": "false",
            "follow-torrent": "false", "follow-metalink": "false",
            "max-http-redirection": "0", "check-certificate": "true",
            "split": str(max(1, cfg.split)), "min-split-size": cfg.minimum_split_size,
            "max-connection-per-server": str(max(1, cfg.connections_per_server)),
            "continue": "true" if cfg.continue_downloads else "false",
            "pause": "true" if request.paused else "false", **guarded,
        }
        headers = []
        for key, value in endpoint.headers.items():
            if any(char in str(key) + str(value) for char in "\r\n\x00") or str(key).lower() in {"host", "proxy-authorization"}:
                raise self._failure(Category.SECURITY_POLICY_REJECTED, domain=Domain.SECURITY)
            headers.append(f"{key}: {value}")
        if headers:
            options["header"] = headers
        return address, options

    async def start(self, request: ExecutionRequest, handle: ExecutionHandle) -> ExecutionObservation:
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
            address, options = await self._options(request, handle)
            # A deletion can revoke authority during DNS or egress startup.
            await self._check(handle, "start")
            returned = await self.client._call("aria2.addUri", [[address], options])
            if str(returned) != gid:
                raise self._failure(Category.EXECUTOR_PROTOCOL_VIOLATION)
            return ExecutionObservation(handle, ExecutionState.PAUSED if request.paused else ExecutionState.QUEUED)
        except Exception as exc:
            # A lost acknowledgement leaves an uncertain execution, not a
            # failed artifact and not permission to create another native job.
            error = exception_failure(exc, stage=Stage.QUEUE, secrets=self._secrets(handle))
            uncertain = error.recovery == Recovery.RECONCILE or error.retryability == Retryability.UNKNOWN
            return ExecutionObservation(handle, ExecutionState.UNKNOWN if uncertain else ExecutionState.FAILED, error=error)

    async def observe(self, handle: ExecutionHandle) -> ExecutionObservation:
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
                if str(native.gid) != gid:
                    raise self._failure(Category.EXECUTOR_PROTOCOL_VIOLATION)
                expected = self._remote_target(self._target(str(handle.context["target"])))
                if any(str(item.get("path") or "") not in {"", expected} for item in (native.files or [])):
                    raise self._failure(Category.OWNERSHIP_CONFLICT, domain=Domain.LIFECYCLE)
                result = observation(handle, native, secrets=self._secrets(handle))
                # Expose the application's filesystem namespace, not the remote
                # daemon's mount mapping or any native file dictionaries.
                return ExecutionObservation(handle, result.state, result.progress,
                                            (str(handle.context["target"]),), result.error)
            return ExecutionObservation(handle, ExecutionState.ABSENT)
        except Exception as exc:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN,
                                        error=exception_failure(exc, stage=Stage.RECONCILIATION, secrets=self._secrets(handle)))

    async def _control(self, handle: ExecutionHandle, *, resume: bool) -> ExecutionObservation:
        try:
            action = "resume" if resume else "pause"
            gid = await self._check(handle, action)
            before = await self.observe(handle)
            if before.error or not before.resumable:
                return before
            await self.client._call("aria2.unpause" if resume else "aria2.forcePause", [gid])
            expected = {ExecutionState.TRANSFERRING, ExecutionState.QUEUED, ExecutionState.SUCCEEDED} if resume else {ExecutionState.PAUSED, ExecutionState.SUCCEEDED}
            for check in range(3):
                after = await self.observe(handle)
                if after.state in expected or after.error:
                    return after
                if check < 2:
                    await asyncio.sleep(self.configuration.confirmation_delay)
            return ExecutionObservation(handle, ExecutionState.UNKNOWN, error=NormalizedError(
                Domain.RECONCILIATION, Category.RECONCILIATION_FAILED, Stage.RECONCILIATION,
                retryability=Retryability.BACKOFF, recovery=Recovery.RECONCILE, integration_id=self.descriptor.id))
        except Exception as exc:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN,
                                        error=exception_failure(exc, secrets=self._secrets(handle)))

    async def pause(self, handle: ExecutionHandle) -> ExecutionObservation:
        return await self._control(handle, resume=False)

    async def resume(self, handle: ExecutionHandle) -> ExecutionObservation:
        return await self._control(handle, resume=True)

    async def cancel(self, handle: ExecutionHandle) -> TransferOutcome:
        try:
            gid = await self._check(handle, "cancel")
            before = await self.observe(handle)
            if before.error:
                return TransferOutcome(OutcomeKind.FAILURE, before.error)
            if before.resumable:
                await self.client._call("aria2.forceRemove", [gid])
                after = await self.observe(handle)
                if after.error:
                    return TransferOutcome(OutcomeKind.FAILURE, after.error)
                if after.resumable or after.state == ExecutionState.UNKNOWN:
                    raise self._failure(Category.RECONCILIATION_FAILED, Stage.CLEANUP)
            # Shared daemons retain stopped results. This never changes global
            # daemon options, purges results, or mutates unowned jobs.
            if not self.configuration.external and before.state != ExecutionState.ABSENT:
                try:
                    await self.client._call("aria2.removeDownloadResult", [gid])
                except Exception as exc:
                    if not is_missing(exc, gid):
                        raise
            return TransferOutcome(OutcomeKind.CANCELLED, cancellation_initiator=CancellationInitiator.USER)
        except Exception as exc:
            return TransferOutcome(OutcomeKind.FAILURE, exception_failure(exc, stage=Stage.CLEANUP, secrets=self._secrets(handle)))

    async def health(self) -> HealthObservation:
        try:
            await self.client.test()
            return HealthObservation(True)
        except Exception as exc:
            return HealthObservation(False, exception_failure(exc, secrets=self.configuration.secrets))
