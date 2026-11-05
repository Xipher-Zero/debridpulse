"""1.0.14 C1: a provider's refusal of a NEW dispatch is a new failure
occurrence, even before any native execution exists.

Transfer 1279 (2026-11-02): Debrid-Link refused each fresh download link with
``maxData``. The refusal comes before an execution attempt exists, so its
failure identity was ``no-execution:0:<signature>`` every time and
``record_source_failure_once`` counted only the first: the bounded
same-candidate retry never spent its budget, and seven files re-asked
Debrid-Link about once a minute for three hours while equivalent AllDebrid
sources sat unused (investigations/dp-1.0.14-debridlink-quota-stagnation).

The real Debrid-Link client (envelope parsing), provider (refresh -> resolve)
and translation are driven by an in-memory HTTP transport; the real engine,
repository and policy decide on a scratch database. The refusal has the
incident's recorded shape -- a JSON answer that is not a success, whose
``error`` is a native quota code -- at 403 (the incident's status was never
recorded; the investigation showed the outcome is the same at every status).
The code is ``maxLink``: the same quota class without a scope fact, so this
is C1's accounting alone. The recorded ``maxData`` itself also places a
temporary account quota hold (C2), proven in test_quota_scope_holds.py.
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

import db.database as database
from providers.debridlink.client import DebridLinkService, RawResponse
from providers.debridlink.host_runtime import (
    DebridLinkRequestApplicability, applicability_facts, parse_native_host_snapshot,
)
from providers.debridlink.provider import DebridLinkProvider
from test_v113_debridlink_engine import HOSTS, KEY, SIZE, Elsewhere, Sampler
from transfers._engine_base import ProviderIssuanceRefusal
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage, TransferError
from transfers.models import ExecutionState, TransferRequest
from transfers.policy import RecoveryAction, TransferPolicy
from transfers.recovery_execution import RecoveryTrigger
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

HERE = "https://hoster.example/f/movie.bin"      # Debrid-Link's link
THERE = "https://other.example/movie.bin"        # another provider's equivalent link
BODIES = {"hoster.example/f/movie.bin": b"same", "other.example/movie.bin": b"same"}
QUOTA = "resolution|provider|quota_exceeded|after_resource_change"


class DebridLinkHTTP:
    """Debrid-Link's HTTP side, in memory. From the ``refuse_after``-th
    ``downloader/add`` of HERE on, it answers the recorded refusal; every
    other call gets Debrid-Link's documented success with a fresh link."""

    def __init__(self, refuse_after=None):
        self.refuse_after = refuse_after
        self.adds = []          # (refused?, url) per POST downloader/add

    def refusals(self):
        return sum(1 for refused, _url in self.adds if refused)

    async def __call__(self, method, url, *, headers=None, params=None, data=None, timeout=None):
        path = url.split("/v2/", 1)[-1]
        if method == "GET" and path.startswith("account/infos"):
            return self._ok({"accountType": 1, "premiumLeft": 86400})
        assert method == "POST" and path == "downloader/add", (method, path)
        link = json.loads(data)["url"]
        served = sum(1 for _refused, item in self.adds if item == link)
        if link == HERE and self.refuse_after is not None and served >= self.refuse_after:
            self.adds.append((True, link))
            return RawResponse(403, {"content-type": "application/json"},
                               json.dumps({"success": False, "error": "maxLink"}).encode())
        self.adds.append((False, link))
        key = link.split("://", 1)[1]
        return self._ok({"id": f"l{len(self.adds):06d}", "name": "movie.bin", "size": SIZE, "url": link,
                         "downloadUrl": f"https://dl6.debrid.link/dl/{key}?capability=dl-token-{len(self.adds)}",
                         "expired": False})

    @staticmethod
    def _ok(value):
        return RawResponse(200, {"content-type": "application/json"},
                           json.dumps({"success": True, "value": value}).encode())


class Clock:
    now = 1_000_000.0

    def __call__(self):
        return self.now


def build(tmp_path, http, clock, *, other=True):
    """One application start over the scratch database: production policy
    defaults (retry delays, the same-candidate limit of 2)."""
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = DebridLinkProvider(DebridLinkService(KEY, transport=http))
    snapshot = parse_native_host_snapshot(HOSTS)
    provider.applicability = applicability_facts(snapshot)
    provider.applicability_for = DebridLinkRequestApplicability(snapshot)
    registry.register_provider(provider)
    if other:
        registry.register_provider(Elsewhere())
    executor = Sampler(repository.authorize_execution, BODIES)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "downloads"),
                            policy=TransferPolicy(adoption_stability_seconds=0, max_active_executions=8,
                                                  resolution_concurrency=8),
                            clock=clock)
    return engine, repository, executor


async def lab(tmp_path, monkeypatch, *, other=True):
    """Debrid-Link's link first (1279's canonical source) and, with ``other``,
    an equivalent link on another provider, converged into ONE artifact. HERE
    is asked for resolution, for the equal-bytes evidence sample (only when
    there is an equivalent to compare) and then for the execution's own fresh
    link; the account runs out of data at that last call, as 1279 did."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "quota.db")
    await database.init_db()
    http, clock = DebridLinkHTTP(refuse_after=2 if other else 1), Clock()
    engine, repository, executor = build(tmp_path, http, clock, other=other)
    await engine.initialize()
    requests = (TransferRequest("https", HERE, "movie.bin"),)
    if other:
        requests += (TransferRequest("https", THERE, "movie.bin"),)
    transfer = await engine.submit(requests, name="movie", deduplicate=False)
    await engine.tick()
    [artifact] = await repository.artifacts(transfer.id)
    assert [candidate.provider_id for candidate in artifact.candidates] == (
        ["debridlink", "elsewhere"] if other else ["debridlink"])
    return engine, repository, executor, http, clock, transfer, artifact


async def wake(engine, clock, seconds=400.0):
    """One ordinary scheduler cycle after ``seconds`` (past any retry_at)."""
    clock.now += seconds
    await engine.reconcile_executions()
    await engine.tick()


async def facts(artifact_id):
    async with database.get_db() as db:
        file = await db.fetchone("SELECT status,selected_candidate,recovery_failures,execution_attempt_id "
                                 "FROM download_files WHERE id=?", (artifact_id,))
        state = await db.fetchone("SELECT decision_action,decision_reason,quiescence_reason,wake_condition,"
                                  "consecutive_no_progress_failures,last_failure_identity "
                                  "FROM artifact_recovery_state WHERE artifact_id=?", (artifact_id,))
        audit = await db.fetchall("SELECT detail FROM application_events WHERE kind='recovery_audit'")
    return dict(file), dict(state), [json.loads(row["detail"]).get("transition") for row in audit]


# -- 1. two distinct refused dispatches are two occurrences; the alternate starts ----

async def test_two_refused_dispatches_consume_two_occurrences_and_the_alternate_starts(tmp_path, monkeypatch):
    engine, repository, executor, http, clock, transfer, artifact = await lab(tmp_path, monkeypatch)
    file, state, transitions = await facts(artifact.id)
    assert http.refusals() == 1 and file["recovery_failures"] == 1
    assert state["decision_reason"] == "bounded_same_candidate_retry"

    await wake(engine, clock)   # a new dispatch, refused again: the second occurrence

    file, state, transitions = await facts(artifact.id)
    assert http.refusals() == 2
    assert transitions.count("source_failure") == 2
    # The budget is spent: the other provider's equivalent source is selected
    # and its one writer started.
    assert file["selected_candidate"] == 1
    attempts = await repository.executions(transfer.id)
    assert [attempt.candidate.provider_id for attempt in attempts] == ["elsewhere"]
    assert file["execution_attempt_id"] == attempts[0].handle.attempt_id
    assert [call for call in executor.calls if call[0] == "start"] == [("start", attempts[0].handle)]

    for _ in range(3):
        await wake(engine, clock)
    assert http.refusals() == 2     # Debrid-Link is not asked again
    assert len(await repository.executions(transfer.id)) == 1


# -- 2. sole source: the bounded budget ends in the truthful operator wait ----------

async def test_a_sole_source_refused_repeatedly_ends_in_the_operator_wait_not_endless_retry(tmp_path, monkeypatch):
    engine, repository, executor, http, clock, transfer, artifact = await lab(tmp_path, monkeypatch, other=False)
    for _ in range(4):
        await wake(engine, clock)

    file, state, transitions = await facts(artifact.id)
    assert http.refusals() == 2
    assert (state["decision_action"], state["decision_reason"]) == (
        RecoveryAction.WAIT_FOR_OPERATOR.value, "resource_change_operator")
    assert (state["quiescence_reason"], state["wake_condition"]) == ("recovery_exhausted", "operator_retry")
    assert not await repository.executions(transfer.id)
    # Parked: further cycles neither claim nor ask the provider.
    claims = transitions.count("claim")
    for _ in range(3):
        await wake(engine, clock)
    assert http.refusals() == 2
    assert (await facts(artifact.id))[2].count("claim") == claims


async def test_an_operator_retry_resets_the_budget_and_is_bounded_again(tmp_path, monkeypatch):
    """USER_RETRY's authorized exhaustion reset keeps its meaning: the file
    is retried from a fresh budget and, still refused, parks again."""
    engine, repository, executor, http, clock, transfer, artifact = await lab(tmp_path, monkeypatch, other=False)
    for _ in range(3):
        await wake(engine, clock)
    assert http.refusals() == 2
    assert await engine.recover_artifact(artifact.id, trigger=RecoveryTrigger.USER_RETRY) is not None
    for _ in range(4):
        await wake(engine, clock)
    _file, state, _transitions = await facts(artifact.id)
    assert http.refusals() == 4
    assert state["quiescence_reason"] == "recovery_exhausted"


# -- 3. the same refusal reported twice, or a pass with no provider call ----------

async def test_one_refusal_reported_twice_within_its_claim_counts_once(tmp_path, monkeypatch):
    engine, repository, executor, http, clock, transfer, artifact = await lab(tmp_path, monkeypatch)
    report = engine._schedule_refresh

    async def twice(target, error, **kwargs):
        await report(target, error, **kwargs)
        return await report(target, error, **kwargs)

    engine._schedule_refresh = twice   # the one dispatch's refusal, reported again in the same claim
    await wake(engine, clock)
    file, _state, transitions = await facts(artifact.id)
    assert http.refusals() == 2
    # Counted once (two occurrences in all, not three): the budget is spent
    # and the alternate is selected; the repeated report, attributed to the
    # Debrid-Link candidate it refused, does not count against the newly
    # selected source. (As before this change, a report that counts nothing
    # still decides: here one ordinary backoff before the alternate starts.)
    assert transitions.count("source_failure") == 2
    assert file["selected_candidate"] == 1
    engine._schedule_refresh = report
    await wake(engine, clock)
    assert http.refusals() == 2
    assert (await facts(artifact.id))[2].count("source_failure") == 2
    attempts = await repository.executions(transfer.id)
    assert [attempt.candidate.provider_id for attempt in attempts] == ["elsewhere"]


async def test_a_pass_without_a_new_provider_call_counts_nothing(tmp_path, monkeypatch):
    engine, repository, executor, http, clock, transfer, artifact = await lab(tmp_path, monkeypatch)
    for _ in range(3):
        await wake(engine, clock, seconds=1.0)   # the backoff is still active
    file, state, transitions = await facts(artifact.id)
    assert http.refusals() == 1 and file["recovery_failures"] == 1
    assert transitions.count("source_failure") == 1
    # Re-deciding the stored refusal, with no provider call, does not count it again.
    stored = (await repository.artifacts(transfer.id))[0].error
    clock.now += 400.0
    await engine.recover_artifact(artifact.id, trigger=RecoveryTrigger.AUTO_RETRY, error=stored)
    assert (await facts(artifact.id))[0]["recovery_failures"] == 1


# -- 4/5. restart and startup reconciliation -------------------------------------

async def test_a_restart_neither_recounts_nor_resets_and_the_next_dispatch_counts_once(tmp_path, monkeypatch):
    engine, repository, executor, http, clock, transfer, artifact = await lab(tmp_path, monkeypatch)
    assert (await facts(artifact.id))[0]["recovery_failures"] == 1

    engine, repository, executor = build(tmp_path, http, clock)   # restart, the backoff still active
    await engine.initialize()
    await engine.reconcile_executions()
    await engine.tick()
    file, _state, transitions = await facts(artifact.id)
    assert http.refusals() == 1 and file["recovery_failures"] == 1
    assert "startup_reconcile" in json.dumps(await _claims(artifact.id))

    await wake(engine, clock)
    file, _state, _transitions = await facts(artifact.id)
    assert http.refusals() == 2 and file["selected_candidate"] == 1
    assert [attempt.candidate.provider_id for attempt in await repository.executions(transfer.id)] == ["elsewhere"]


async def _claims(artifact_id):
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT detail FROM application_events WHERE kind='recovery_audit'")
    return [json.loads(row["detail"]) for row in rows
            if json.loads(row["detail"]).get("transition") == "claim"
            and json.loads(row["detail"]).get("artifact_id") == artifact_id]


async def test_a_refusal_after_a_restart_is_a_new_occurrence_even_against_a_legacy_identity(tmp_path, monkeypatch):
    """A snapshot written before this change holds the legacy
    ``no-execution`` identity; the next refused dispatch is a new answer of
    the provider and counts, once."""
    engine, repository, executor, http, clock, transfer, artifact = await lab(tmp_path, monkeypatch)
    async with database.get_db() as db:
        await db.execute("UPDATE artifact_recovery_state SET last_failure_identity=? WHERE artifact_id=?",
                         (f"0:no-execution:0:{QUOTA}", artifact.id))
        await db.commit()
    await wake(engine, clock)
    file, _state, transitions = await facts(artifact.id)
    assert http.refusals() == 2 and transitions.count("source_failure") == 2
    assert file["selected_candidate"] == 1


# -- 6. execution-stage failures keep their identity; TLS then quota still fails over -

async def test_execution_stage_and_unmarked_identities_are_unchanged():
    error = NormalizedError(Domain.NETWORK, Category.TLS_FAILURE, Stage.EXECUTION, Retryability.BACKOFF,
                            origin=Origin.REMOTE_SOURCE)

    class Handle:
        attempt_id = "a1"

    class Observed:
        class progress:
            completed_bytes = 77

    with_execution = type("A", (), {"execution": Handle()})()
    without = type("A", (), {"execution": None})()
    signature = "execution|network|tls_failure|backoff"
    assert TransferEngine._failure_identity(with_execution, error, Observed()) == f"a1:77:{signature}"
    assert TransferEngine._failure_identity(with_execution, error, Observed(), occurrence="9/c") == f"a1:77:{signature}"
    assert TransferEngine._failure_identity(without, error) == f"no-execution:0:{signature}"
    assert TransferEngine._failure_identity(without, error, occurrence="9/c") == f"no-execution:0:{signature}#9/c"


async def test_tls_failure_then_quota_still_reaches_the_alternate(tmp_path, monkeypatch):
    """22408's path: an execution fails (TLS), the refreshed link is refused."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "quota.db")
    await database.init_db()
    http, clock = DebridLinkHTTP(), Clock()
    engine, repository, executor = build(tmp_path, http, clock)
    await engine.initialize()
    transfer = await engine.submit((TransferRequest("https", HERE, "movie.bin"),
                                    TransferRequest("https", THERE, "movie.bin")), name="movie", deduplicate=False)
    await engine.tick()
    [attempt] = await repository.executions(transfer.id)
    assert attempt.candidate.provider_id == "debridlink"
    tls = NormalizedError(Domain.NETWORK, Category.TLS_FAILURE, Stage.EXECUTION, Retryability.BACKOFF,
                          origin=Origin.REMOTE_SOURCE, integration_id=executor.descriptor.id)
    executor.jobs[attempt.handle.attempt_id] = replace(executor.jobs[attempt.handle.attempt_id], state=ExecutionState.FAILED, error=tls)
    http.refuse_after = sum(1 for _refused, url in http.adds if url == HERE)
    for _ in range(4):
        await wake(engine, clock)
    attempts = await repository.executions(transfer.id)
    assert [item.candidate.provider_id for item in attempts] == ["debridlink", "elsewhere"]
    [artifact] = await repository.artifacts(transfer.id)
    assert artifact.selected == 1 and artifact.execution.attempt_id == attempts[-1].handle.attempt_id


# -- 7. one writer: a held claim blocks, nothing counts, nothing starts ------------

async def test_a_competing_claim_blocks_the_dispatch_and_nothing_counts(tmp_path, monkeypatch):
    engine, repository, executor, http, clock, transfer, artifact = await lab(tmp_path, monkeypatch)
    clock.now += 400.0
    held = await repository.claim_recovery(artifact.id, RecoveryTrigger.USER_CANDIDATE_SWITCH, clock(),
                                           lease_seconds=10_000)
    assert held is not None
    await engine.reconcile_executions()
    await engine.tick()
    file, _state, _transitions = await facts(artifact.id)
    assert http.refusals() == 1 and file["recovery_failures"] == 1
    assert not await repository.executions(transfer.id)


# -- 8. only a provider's answer is marked; core-detected failures are not ---------

async def test_only_a_provider_answer_to_issuance_is_a_refusal_occurrence(tmp_path, monkeypatch):
    engine, repository, executor, http, clock, transfer, artifact = await lab(tmp_path, monkeypatch)
    candidate = artifact.candidates[0]
    request = candidate.refresh_request
    with pytest.raises(ProviderIssuanceRefusal) as refused:
        await engine._issued_material(candidate, request)
    assert refused.value.error.native_code == "maxLink"
    # No provider able to issue: core's own finding, not a provider answer.
    engine.registry.providers.pop("debridlink")
    with pytest.raises(TransferError) as structural:
        await engine._issued_material(candidate, request)
    assert not isinstance(structural.value, ProviderIssuanceRefusal)
    assert structural.value.error.category == Category.CANDIDATE_EXPIRED
