"""DP 1.0.13 adverse conditions (Gate 9 revision 2): a permanent route failure
of ONE candidate is scoped to that candidate.

A source that is gone (``source_not_found``) says nothing about the logical
artifact: while a verified alternate exists the one recovery policy tries it
first, and only without one does the artifact fail permanently. Security,
integrity and logical-artifact failures stay terminal whatever else exists.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from fake_integrations import VaultExecutor, VaultProvider
from test_input_required_lifecycle import base  # noqa: F401  (fixture)
from transfers.errors import Category, Domain, NormalizedError, Origin, Permanence, Retryability, Stage
from transfers.models import TransferRequest, TransferState
from transfers.policy import RecoveryAction, RecoveryContext, TransferPolicy

NOW = 1000.0


def _gone(**facts) -> NormalizedError:
    """What an executor reports when the exact source path no longer exists."""
    error = NormalizedError(Domain.RESOLUTION, Category.SOURCE_NOT_FOUND, Stage.EXECUTION,
                            retryability=Retryability.NEVER, permanence=Permanence.PERMANENT,
                            origin=Origin.REMOTE_SOURCE, integration_id="rsync")
    return replace(error, **facts)


def _context(**facts) -> RecoveryContext:
    return replace(RecoveryContext(observed_completed_bytes=0), **facts)


def test_a_permanent_source_failure_tries_a_verified_alternate_first():
    decision = TransferPolicy().recover(_gone(), _context(has_alternate=True), NOW)
    assert decision.action == RecoveryAction.TRY_ALTERNATE_CANDIDATE
    assert decision.retry_at == NOW


def test_a_permanent_source_failure_without_an_alternate_stays_terminal():
    decision = TransferPolicy().recover(_gone(), _context(has_alternate=False), NOW)
    assert decision.action == RecoveryAction.FAIL_PERMANENTLY


@pytest.mark.parametrize("error", [
    _gone(domain=Domain.SECURITY, category=Category.DESTINATION_BLOCKED),       # security
    _gone(domain=Domain.SECURITY, category=Category.HOST_KEY_FAILURE),          # changed identity
    _gone(domain=Domain.INTEGRITY, category=Category.CHECKSUM_MISMATCH),        # integrity
    _gone(domain=Domain.INTEGRITY, category=Category.SIZE_MISMATCH),            # integrity
    _gone(domain=Domain.EXECUTOR, category=Category.CONTENT_INVALID),           # the logical artifact
], ids=["security-destination", "security-host-key", "integrity-checksum", "integrity-size", "content-invalid"])
def test_security_integrity_and_logical_artifact_failures_stay_terminal_even_with_an_alternate(error):
    decision = TransferPolicy().recover(error, _context(has_alternate=True), NOW)
    assert decision.action == RecoveryAction.FAIL_PERMANENTLY


class GoneFirst(VaultExecutor):
    """The first candidate's source is gone the moment its writer starts."""

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.started = []

    async def start(self, request, handle):
        host = self._object(request.work.subject.candidate).partition("/")[0]
        self.started.append(host)
        if host == "gone.example":
            self.start_errors = [_gone(integration_id=self.descriptor.id)]
        return await super().start(request, handle)


async def _submit(base, payload):
    repository, registry, engine, now = base
    registry.register_provider(VaultProvider())
    executor = GoneFirst(repository.authorize_execution, objects={
        "gone.example/item.bin": b"four", "alive.example/item.bin": b"four"})
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("vault", payload, name="item.bin"),), deduplicate=False)
    for _ in range(6):
        now[0] += 5
        await engine.tick()
    return repository, engine, executor, transfer


@pytest.mark.asyncio
async def test_the_engine_fails_over_from_a_gone_source_to_the_verified_alternate(base):
    repository, _engine, executor, transfer = await _submit(base, "gone.example/item.bin|alive.example/item.bin")
    (artifact,) = await repository.artifacts(transfer.id)
    assert executor.started[:2] == ["gone.example", "alive.example"]
    assert artifact.state != "error" and artifact.execution is not None
    assert artifact.candidates[artifact.selected].endpoints[0].address.endswith("alive.example/item.bin")


@pytest.mark.asyncio
async def test_the_engine_fails_a_gone_source_permanently_when_it_is_the_only_one(base):
    repository, _engine, executor, transfer = await _submit(base, "gone.example/item.bin")
    (artifact,) = await repository.artifacts(transfer.id)
    assert executor.started == ["gone.example"]
    assert artifact.state == "error"
    assert (await repository.get(transfer.id)).state == TransferState.FAILED
