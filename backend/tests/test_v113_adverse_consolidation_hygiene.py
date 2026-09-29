"""DP 1.0.13 adverse conditions, Pass 2: consolidation hygiene and evidence liveness.

Defect C -- a dead source (its resolution permanently failed: not found,
refused, blocked) submitted in the SAME cohort as members that were proven to
be one canonical artifact is associated with that artifact as a FAILED
contribution: never a writer, never a candidate, its exact failure kept in the
canonical source history, and its transfer no longer standing as top-level
failed clutter. Association is not equivalence: contradiction, a structural
mismatch or an ambiguous cohort keeps it independent.

Defect D -- a proof that only timed out is TEMPORARILY unproven: exhausting the
short automatic retry budget keeps the writer barrier and the association, and
leaves a durable, low-frequency reconsideration through the one existing
retry_at seam -- never a hot loop, never a duplicate writer, never "distinct".
"""
from __future__ import annotations

import json

import pytest

import db.database as database
from fake_integrations import VaultExecutor, VaultProvider
from test_input_required_lifecycle import base  # noqa: F401  (fixture)
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
from transfers.models import (
    ArtifactFingerprint, FingerprintKind, ResolutionResult, ResourceState, TransferRequest, TransferState,
)

pytestmark = pytest.mark.asyncio

SAME = b"four"  # the memory copier reports a four-byte total
DEAD = {
    "dead-notfound.example": Category.SOURCE_NOT_FOUND,
    "dead-refused.example": Category.CONNECTION_REFUSED,
    "dead-blocked.example": Category.DESTINATION_BLOCKED,
}


# Ordinary transient resolution failures: retried with backoff until the
# resolution budget is spent -- never evidence that the route is dead.
TRANSIENT = {
    "flaky-timeout.example": Category.CONNECTION_TIMEOUT,
    "flaky-dns.example": Category.DNS_FAILURE,
    "flaky-connect.example": Category.CONNECTION_FAILED,
}


class CohortProvider(VaultProvider):
    """``VaultProvider`` whose dead hosts fail resolution permanently, exactly
    as a decommissioned FTP mirror fails its core-run discovery."""

    def __init__(self):
        super().__init__()
        self.resolved = []

    async def resolve(self, request):
        host = str(request.payload).partition("/")[0]
        self.resolved.append(host)
        if host in DEAD:
            domain = Domain.SECURITY if DEAD[host] == Category.DESTINATION_BLOCKED else Domain.NETWORK
            return ResolutionResult(ResourceState.UNKNOWN, error=NormalizedError(
                domain, DEAD[host], Stage.RESOLUTION, retryability=Retryability.NEVER, integration_id="vault-lab"))
        if host in TRANSIENT:
            return ResolutionResult(ResourceState.UNKNOWN, error=NormalizedError(
                Domain.NETWORK, TRANSIENT[host], Stage.RESOLUTION, retryability=Retryability.BACKOFF,
                integration_id="vault-lab"))
        return await super().resolve(request)


class FlakyVault(VaultExecutor):
    """Sampling of ``flaky.example`` times out while ``flaky`` is set."""

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.flaky = True

    async def fingerprint(self, subject):
        if self._object(subject.candidate).startswith("flaky.example/") and self.flaky:
            self.samples.append((str(subject.candidate.id), "timeout"))
            return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "timeout")
        return await super().fingerprint(subject)


def _lab(base, *, objects=None):
    repository, registry, engine, now = base
    registry.register_provider(CohortProvider())
    vault = FlakyVault(repository.authorize_execution, objects={
        "canonical.example/item.bin": SAME, "good.example/item.bin": SAME, "flaky.example/item.bin": SAME,
        "contra.example/item.bin": b"diff", **(objects or {}),
    })
    registry.register_executor(vault)
    return repository, engine, vault, now


async def _ticks(engine, now, count=6, step=5):
    for _ in range(count):
        now[0] += step
        await engine.tick()


async def _canonical(repository, engine, now):
    transfer = await engine.submit((TransferRequest("vault", "canonical.example/item.bin", name="item.bin"),),
                                   deduplicate=False)
    await _ticks(engine, now, 3)
    (artifact,) = await repository.artifacts(transfer.id)
    return transfer, artifact


async def _requests(transfer_id):
    """Each request of ``transfer_id`` keyed by the host of its payload."""
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT * FROM transfer_requests WHERE transfer_id=? ORDER BY ordinal",
                                 (transfer_id,))
    return {json.loads(row["payload"])["payload"].partition("/")[0]: row for row in rows}


def _cohort(*hosts, name="item.bin"):
    return tuple(TransferRequest("vault", f"{host}/{name}", name=name) for host in hosts)


# ── Defect C: dead-link hygiene ─────────────────────────────────────────────

async def test_dead_roots_of_a_proven_cohort_become_failed_contributions_of_the_canonical_artifact(base):
    repository, engine, vault, now = _lab(base)
    canonical, artifact = await _canonical(repository, engine, now)
    cohort = await engine.submit(_cohort("good.example", *DEAD), deduplicate=False)
    await _ticks(engine, now, 8)

    rows = await _requests(cohort.id)
    assert rows["good.example"]["equivalence_disposition"] == "recovered"  # the executable alternate
    for host, category in DEAD.items():
        row = rows[host]
        # Associated for hygiene, never executable: the failure itself is kept.
        assert row["state"] == "failed", host
        assert row["equivalence_disposition"] == "failed_contribution", host
        assert int(row["equivalence_target_artifact_id"]) == artifact.id, host
        assert category.value in row["error"], host
    # No writer, candidate or binding was ever created from a dead root.
    async with database.get_db() as db:
        own = await db.fetchall("SELECT id FROM download_files WHERE torrent_id=? AND status!='duplicate'",
                                (cohort.id,))
        bound = await db.fetchall("""SELECT o.request_id FROM canonical_candidate_origins o
            WHERE o.contributing_transfer_id=?""", (cohort.id,))
    assert own == []
    assert {row["request_id"] for row in bound} == {rows["good.example"]["id"]}
    # The contributing submission is settled into the canonical artifact:
    # it is no longer top-level failed clutter.
    assert (await repository.get(cohort.id)).state == TransferState.CONSOLIDATED
    # The canonical transfer's source history names every dead mirror and why.
    detail = await repository.presentation(canonical.id, details=True)
    failed = [row for row in detail["file_presentations"] if row.get("relationship") == "failed"]
    assert sorted(row["request_id"] for row in failed) == sorted(rows[host]["id"] for host in DEAD)
    assert {row["failure_reason"] for row in failed} == {category.value for category in DEAD.values()}
    assert all(row["artifact_id"] is None and row["contributing_transfer_id"] == cohort.id for row in failed)
    routes = [item for item in detail["route_attempts"] if item.get("relation") == "failed"]
    assert sorted(item["request_id"] for item in routes) == sorted(rows[host]["id"] for host in DEAD)
    assert all(item["verification_state"] == "failed" for item in routes)
    # Exactly one physical writer ever existed for the object.
    assert len([call for call in vault.calls if call[0] == "start"]) == 1


async def test_a_dead_root_that_fails_after_its_cohort_consolidated_is_associated_too(base):
    repository, engine, vault, now = _lab(base)
    _canonical_transfer, artifact = await _canonical(repository, engine, now)
    provider = next(item for item in engine.registry.providers.values() if isinstance(item, CohortProvider))
    DEAD["late-dead.example"] = Category.CONNECTION_REFUSED
    try:
        cohort = await engine.submit(_cohort("good.example", "late-dead.example"), deduplicate=False)
        await _ticks(engine, now, 8)
    finally:
        DEAD.pop("late-dead.example")
    rows = await _requests(cohort.id)
    assert "late-dead.example" in provider.resolved
    assert rows["late-dead.example"]["equivalence_disposition"] == "failed_contribution"
    assert int(rows["late-dead.example"]["equivalence_target_artifact_id"]) == artifact.id
    assert (await repository.get(cohort.id)).state == TransferState.CONSOLIDATED


async def test_a_contradictory_sibling_keeps_the_cohort_and_its_dead_roots_independent(base):
    repository, engine, vault, now = _lab(base)
    await _canonical(repository, engine, now)
    cohort = await engine.submit(_cohort("good.example", "contra.example", "dead-refused.example"), deduplicate=False)
    await _ticks(engine, now, 8)
    rows = await _requests(cohort.id)
    assert rows["contra.example"]["equivalence_disposition"] in {"contradictory", "independent"}
    # Nothing hygienic may be inferred from a cohort that is proven NOT one object.
    assert rows["dead-refused.example"]["equivalence_disposition"] != "failed_contribution"
    assert rows["dead-refused.example"]["equivalence_target_artifact_id"] is None
    assert (await repository.get(cohort.id)).state != TransferState.CONSOLIDATED


async def test_a_dead_root_whose_structural_identity_differs_is_never_associated(base):
    repository, engine, vault, now = _lab(base)
    await _canonical(repository, engine, now)
    requests = (TransferRequest("vault", "good.example/item.bin", name="item.bin"),
                TransferRequest("vault", "dead-notfound.example/other.iso", name="other.iso"))
    cohort = await engine.submit(requests, deduplicate=False)
    await _ticks(engine, now, 8)
    rows = await _requests(cohort.id)
    assert rows["dead-notfound.example"]["state"] == "failed"
    assert rows["dead-notfound.example"]["equivalence_disposition"] != "failed_contribution"
    assert (await repository.get(cohort.id)).state != TransferState.CONSOLIDATED


async def test_a_cohort_of_only_dead_roots_is_never_associated(base):
    repository, engine, vault, now = _lab(base)
    await _canonical(repository, engine, now)
    cohort = await engine.submit(_cohort(*DEAD), deduplicate=False)
    await _ticks(engine, now, 6)
    rows = await _requests(cohort.id)
    assert all(row["equivalence_disposition"] != "failed_contribution" for row in rows.values())
    assert (await repository.get(cohort.id)).state != TransferState.CONSOLIDATED


# ── Defect D: a transient proof timeout is temporarily unproven ─────────────

async def test_a_transient_proof_timeout_waits_quiescently_and_converges_when_proof_returns(base):
    repository, engine, vault, now = _lab(base)
    _canonical_transfer, artifact = await _canonical(repository, engine, now)
    incoming = await engine.submit((TransferRequest("vault", "flaky.example/item.bin", name="item.bin"),),
                                   deduplicate=False)
    await _ticks(engine, now, 8, step=2)
    (row,) = (await _requests(incoming.id)).values()
    # The short automatic budget is spent: held, associated, never distinct.
    assert row["state"] == "materializing"
    assert row["equivalence_disposition"] == "unverified" and row["equivalence_reason"] == "timeout"
    assert int(row["equivalence_target_artifact_id"]) == artifact.id
    # ...but not stranded: a durable, future reconsideration exists.
    assert float(row["retry_at"]) > now[0]
    assert await repository.artifacts(incoming.id) == ()  # no duplicate writer
    # Quiescent until then: many ticks before it is due sample nothing.
    sampled = len(vault.samples)
    for _ in range(10):
        now[0] = min(now[0] + 1, float(row["retry_at"]) - 1)
        await engine.tick()
    assert len(vault.samples) == sampled
    # The source recovers; the due reconsideration proves it through the normal owner.
    vault.flaky = False
    now[0] = float(row["retry_at"]) + 1
    await _ticks(engine, now, 4, step=1)
    (row,) = (await _requests(incoming.id)).values()
    assert row["equivalence_disposition"] == "recovered"
    assert (await repository.get(incoming.id)).state == TransferState.CONSOLIDATED
    assert len([call for call in vault.calls if call[0] == "start"]) == 1


async def test_repeated_reconsiderations_back_off_and_stay_bounded(base):
    repository, engine, vault, now = _lab(base)
    await _canonical(repository, engine, now)
    incoming = await engine.submit((TransferRequest("vault", "flaky.example/item.bin", name="item.bin"),),
                                   deduplicate=False)
    await _ticks(engine, now, 8, step=2)
    delays = []
    for _ in range(4):
        (row,) = (await _requests(incoming.id)).values()
        delays.append(float(row["retry_at"]) - now[0])
        now[0] = float(row["retry_at"]) + 1
        await engine.tick()
        await engine.tick()
    assert all(later >= earlier for earlier, later in zip(delays, delays[1:]))
    assert delays[0] >= 30.0  # low-frequency, never a hot loop
    (row,) = (await _requests(incoming.id)).values()
    assert row["equivalence_disposition"] == "unverified"
    assert await repository.artifacts(incoming.id) == ()


async def test_a_structurally_unprovable_hold_stays_terminal(base):
    """Proof that can never exist (the sampler refuses the route) keeps the
    existing fail-closed terminal hold: nothing is ever reconsidered."""
    repository, engine, vault, now = _lab(base)
    await _canonical(repository, engine, now)
    original = vault.fingerprint

    async def refused(subject):
        if vault._object(subject.candidate).startswith("flaky.example/"):
            return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "destination_rejected")
        return await original(subject)
    vault.fingerprint = refused
    incoming = await engine.submit((TransferRequest("vault", "flaky.example/item.bin", name="item.bin"),),
                                   deduplicate=False)
    await _ticks(engine, now, 8, step=2)
    (row,) = (await _requests(incoming.id)).values()
    assert row["equivalence_disposition"] in {"unverified", "exhausted"}
    assert float(row["retry_at"] or 0) == 0.0


async def test_transient_failures_that_exhaust_resolution_retries_are_never_failed_contributions(base):
    repository, engine, vault, now = _lab(base)
    await _canonical(repository, engine, now)
    cohort = await engine.submit(_cohort("good.example", *TRANSIENT), deduplicate=False)
    for _ in range(40):
        now[0] += 400  # well past every backoff: the resolution budget is spent
        await engine.tick()
    rows = await _requests(cohort.id)
    assert rows["good.example"]["equivalence_disposition"] == "recovered"
    for host, category in TRANSIENT.items():
        row = rows[host]
        assert row["state"] == "failed", host  # retries really were exhausted
        assert category.value in row["error"], host
        # Not dead, only unreachable for now: no hygiene association from it.
        assert row["equivalence_disposition"] != "failed_contribution", host
        assert row["equivalence_target_artifact_id"] is None, host
    assert (await repository.get(cohort.id)).state != TransferState.CONSOLIDATED


def test_only_terminal_or_intrinsically_dead_route_failures_are_dead_sources():
    from transfers.policy import dead_source

    def error(category, retryability, domain=Domain.NETWORK):
        return NormalizedError(domain, category, Stage.RESOLUTION, retryability=retryability)
    for category in (Category.SOURCE_NOT_FOUND, Category.RESOURCE_NOT_FOUND, Category.UNSUPPORTED_REQUEST):
        assert dead_source(error(category, Retryability.BACKOFF)), category
    assert dead_source(error(Category.DESTINATION_BLOCKED, Retryability.NEVER, Domain.SECURITY))
    assert dead_source(error(Category.CONNECTION_REFUSED, Retryability.NEVER))
    for category in (Category.CONNECTION_TIMEOUT, Category.DNS_FAILURE, Category.CONNECTION_FAILED,
                     Category.CONNECTION_REFUSED, Category.SOURCE_UNAVAILABLE):
        assert not dead_source(error(category, Retryability.BACKOFF)), category
    assert not dead_source(error(Category.AUTHENTICATION_FAILED, Retryability.NEVER))
