"""DP 1.0.12 canonical lifecycle/recovery/completion rework.

Section 10.3/10.4/15: CANON-001 -- proves the production
``convergence_engine.TransferEngine``/``recovery_repository.TransferRepository``
stack now has exactly one parent-lifecycle semantic owner
(``transfers._repository_base.TransferRepository.aggregate_lifecycle``), with
no layered post-aggregate override capable of persisting a contradictory
decision moments after the canonical one committed -- the exact shape
production transfer 265 proved wrong: one artifact actively transferring
while a sibling sits in autonomous recovery wait, and the parent oscillated
``downloading -> queued -> downloading -> queued``.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from test_manual_candidate_failover import HostParcelProvider
from transfers import codec
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Recovery, Retryability, Stage
from transfers.models import (
    ExecutionState, ResolutionResult, ResourceState, SourceEntry, TransferRequest, TransferState,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio


def _backoff_failure():
    return NormalizedError(
        Domain.NETWORK, Category.REMOTE_READ_FAILED, Stage.EXECUTION,
        Retryability.BACKOFF, Recovery.TRY_ALTERNATE_CANDIDATE, origin=Origin.REMOTE_SOURCE,
    )


async def _build_two_artifact_transfer(tmp_path, monkeypatch):
    """Two genuinely independent (non-mirror) artifacts in ONE transfer --
    matching the shape ``transfers.cohorts`` never merges (distinct payload
    identity, proven non-equivalent by the same real sampler every other
    convergence test in this suite uses)."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = HostParcelProvider("only")
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(
            retry_delay=5, adoption_stability_seconds=0, max_active_executions=8,
            resolution_concurrency=8, same_candidate_no_progress_limit=2,
        ),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    transfer = await engine.submit(
        (
            TransferRequest("parcel", "file-a", name="a.bin", preferred_provider=provider.descriptor.id),
            TransferRequest("parcel", "file-b", name="b.bin", preferred_provider=provider.descriptor.id),
        ),
        deduplicate=False,
    )
    await engine.resolve_pending()
    await engine.reconcile_executions()
    artifacts = await repository.artifacts(transfer.id)
    assert len(artifacts) == 2, "two genuinely distinct payloads must not merge into one canonical artifact"
    assert all(item.execution is not None for item in artifacts)
    return engine, repository, transfer, artifacts


async def test_active_download_with_sibling_recovery_wait_has_no_lifecycle_churn(tmp_path, monkeypatch):
    engine, repository, transfer, artifacts = await _build_two_artifact_transfer(tmp_path, monkeypatch)
    downloading, failing = artifacts[0], artifacts[1]

    executor = engine.registry.executors[downloading.execution.executor_id]
    executor.jobs[failing.execution.attempt_id] = replace(
        executor.jobs[failing.execution.attempt_id], state=ExecutionState.FAILED, error=_backoff_failure(),
    )
    await engine.reconcile_executions()

    current = {item.id: item for item in await repository.artifacts(transfer.id)}
    assert current[failing.id].state == "recovery_wait"
    assert current[downloading.id].state == "downloading"
    assert current[downloading.id].execution is not None

    # DP 1.0.12 Section 7 (CANON-001): repeated scheduler cycles with the
    # recovery-waiting sibling still present must never persist QUEUED while
    # the OTHER artifact is genuinely, continuously transferring -- the exact
    # `force_queued_for_autonomous_wait` layered-override symptom
    # (`downloading -> queued -> downloading -> queued` churn) transfer 265
    # proved.
    observed_states = []
    for _ in range(6):
        await engine.reconcile_executions()
        observed_states.append((await repository.get(transfer.id)).state)

    assert observed_states == [TransferState.TRANSFERRING] * len(observed_states), (
        f"parent lifecycle churned away from TRANSFERRING while a real artifact was "
        f"actively downloading: {observed_states}"
    )
    still_downloading = next(item for item in await repository.artifacts(transfer.id) if item.id == downloading.id)
    assert still_downloading.execution is not None and still_downloading.state == "downloading"


async def test_no_layered_aggregate_override_writes_parent_state():
    """DP 1.0.12 Section 3.1/7.2/15 structural proof: neither
    ``transfers._engine_recovery.TransferEngine`` nor ``transfers.engine
    .TransferEngine`` may still define an ``_aggregate`` override -- the
    removed ``force_queued_for_autonomous_wait`` post-aggregate recovery
    override and the removed paused-state crash/restart repair are both
    folded into the ONE atomic ``TransferRepository.aggregate_lifecycle``
    decision. A future reintroduction of either override is exactly the
    "second parent-lifecycle authority" regression this rework exists to
    make structurally impossible, so this must fail loudly rather than rely
    on production evidence surfacing it again."""
    import transfers._engine_recovery as engine_recovery_module
    import transfers.engine as engine_module

    assert "_aggregate" not in vars(engine_recovery_module.TransferEngine)
    assert "_aggregate" not in vars(engine_module.TransferEngine)
    assert not hasattr(engine_recovery_module.TransferEngine, "_AUTONOMOUS_WAIT_ARTIFACT_STATES")


async def test_force_queued_for_autonomous_wait_no_longer_exists():
    """The removed method itself must not silently reappear (e.g. via a
    well-meaning revert of only part of this change)."""
    from transfers._repository_base import TransferRepository as BaseRepository

    assert not hasattr(BaseRepository, "force_queued_for_autonomous_wait")


_SEMANTIC_RECOVERY_CONTROL_NAMES = frozenset({
    "pause", "resume", "pause_all", "resume_all", "retry",
    "_reacquire_transfer", "_renew_source_parent",
    "_refresh", "_schedule_refresh", "_recover_artifact",
})


async def test_no_alternate_recovery_decision_implementation_below_canonical_owner():
    """DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    closure, Gate 9 revision 6): audits the WHOLE production engine MRO below
    ``convergence_engine.TransferEngine`` -- ``transfers._engine_recovery
    .TransferEngine``, ``transfers.engine.TransferEngine``, AND
    ``transfers._engine_base.TransferEngine`` -- per (class, method), never
    by a global name classification.

    Two earlier revisions of this test were rejected: one classified a
    complete, functioning, non-claim-fenced alternate implementation as
    "safe because the canonical owner's own version always shadows it"; the
    next replaced those alternates with ``raise NotImplementedError`` stubs
    and classified THAT as sufficient. Both retain architectural residue --
    "a dead historical method is still architectural residue. It can be
    accidentally filled back in, delegated to, or revived by a future
    refactor." The actual requirement ("Lower layers may contain neutral
    primitives only") is satisfied only by ABSENCE: none of
    ``pause``/``resume``/``pause_all``/``resume_all``/``retry``/
    ``_reacquire_transfer``/``_renew_source_parent``/``_refresh``/
    ``_schedule_refresh``/``_recover_artifact`` may be defined at all,
    anywhere below ``convergence_engine.TransferEngine`` -- not as a working
    alternate, not as a refusal stub, not as an alias.

    This is checked directly against each class's own ``__dict__``
    (``name not in vars(cls)``), per class, so a method reintroduced on ANY
    one of the three lower classes fails this test even if the other two
    remain clean -- there is no global/merged classification left to
    accidentally bless a different implementation in another lower class.
    """
    import transfers._engine_recovery as engine_recovery_module
    import transfers.engine as engine_module
    import transfers._engine_base as engine_base_module
    import transfers.convergence_engine as convergence_engine_module

    canonical = convergence_engine_module.TransferEngine

    for name in _SEMANTIC_RECOVERY_CONTROL_NAMES:
        assert name in vars(canonical), (
            f"{name!r} must be implemented by transfers.convergence_engine.TransferEngine, the "
            "sole canonical owner of semantic recovery/control operations"
        )

    for label, cls in (
        ("_engine_recovery", engine_recovery_module.TransferEngine),
        ("engine", engine_module.TransferEngine),
        ("_engine_base", engine_base_module.TransferEngine),
    ):
        for name in _SEMANTIC_RECOVERY_CONTROL_NAMES:
            assert name not in vars(cls), (
                f"transfers.{label}.TransferEngine defines {name!r} -- semantic recovery/control "
                "operations must exist ONLY on transfers.convergence_engine.TransferEngine. A "
                "lower-layer definition of this name, whether a working alternate, a refusal "
                "stub, or an alias, is exactly the architectural residue CANON-001 forbids."
            )

    # A small, explicitly justified set of genuine neutral orchestration
    # methods IS legitimately shadowed by the canonical owner via a clean
    # super()-based extension (not a semantic recovery/control operation in
    # its own right). Verified by source inspection, not assumed.
    import inspect

    delegates = frozenset({"_dispatch", "_process_executions", "initialize", "reconcile_executions"})
    assert delegates.isdisjoint(_SEMANTIC_RECOVERY_CONTROL_NAMES)
    for name in delegates:
        src = inspect.getsource(getattr(canonical, name))
        assert f"super().{name}(" in src, (
            f"convergence_engine.TransferEngine.{name} was assumed to delegate to the lower "
            "implementation via super() for its reachable path(s) -- if that changed, this "
            "must be reclassified"
        )

    def own_methods(cls):
        return {
            name for name, value in vars(cls).items()
            if callable(value) and not name.startswith("__")
        }

    canonical_own = own_methods(canonical)
    classified = delegates
    for label, cls in (
        ("_engine_recovery", engine_recovery_module.TransferEngine),
        ("engine", engine_module.TransferEngine),
        ("_engine_base", engine_base_module.TransferEngine),
    ):
        shadowed = own_methods(cls) & canonical_own
        unclassified = shadowed - classified
        assert not unclassified, (
            f"{sorted(unclassified)} on transfers.{label}.TransferEngine are shadowed by "
            "convergence_engine.TransferEngine with no recorded classification -- every such "
            "name must either be in `delegates` (verified super()-extension) or be one of the "
            "semantic recovery/control names proven absent below the canonical owner above; "
            "there is no third, silently-omitted category."
        )


async def _canonical_runtime(tmp_path, monkeypatch):
    """Minimal real canonical engine/repository runtime (ParcelProvider +
    MemoryExecutor) for constructing the durable request/artifact facts the
    CANON-001 exhausted-identity completion policy reads. Distinct from
    ``_build_two_artifact_transfer`` only in using a plain ``ParcelProvider``
    (no cross-provider host identity needed for these single-provider
    scenarios)."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(
            retry_delay=5, adoption_stability_seconds=0, max_active_executions=8,
            resolution_concurrency=8, same_candidate_no_progress_limit=2,
        ),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return engine, repository, provider, executor


async def _freeze_request(request_id, *, retry_at=10 ** 9):
    """Push a request's ``retry_at`` far into the future so the real
    resolve/bootstrap pipeline never touches it -- used to keep a sibling
    request inert (never becomes a cohort peer, never resolves, never
    produces a candidate/artifact) while a companion request in the SAME
    transfer is driven to real completion through the ordinary engine path."""
    async with database.get_db() as db:
        await db.execute("UPDATE transfer_requests SET retry_at=? WHERE id=?", (retry_at, request_id))
        await db.commit()


async def _hold_request(request_id, *, name, reason="dns_failure", disposition="exhausted", retry_count=2,
                        relative_path=None):
    """Durably rewrite a frozen sibling into the exact production transfer-270
    shape: a proof-exhausted, non-writer, ``materializing`` request whose
    declared logical name is ``name`` -- rewriting ``payload`` (never touched
    by ordinary production code after submission) only so this test can place
    the sibling on a chosen logical slot without racing it through the real
    same-transfer cohort/bootstrap pipeline, which is proven correct
    elsewhere (test_multi_mirror_general_http_convergence.py) and is not what
    this decision-level test exercises.

    ``relative_path``, when given, additionally stamps a ``SourceEntry`` onto
    ``metadata`` -- the same durable field ``RequestRecord.entry`` reads --
    so the held request's own logical-slot key carries a real pathful
    identity (``_logical_slot_key_for_request`` reads ``record.entry`` first)
    instead of falling back to a bare ``TransferRequest.name``, proving the
    same-basename/different-relative-path distinction on the REQUEST side
    too, not only the artifact side."""
    async with database.get_db() as db:
        row = await db.fetchone("SELECT payload FROM transfer_requests WHERE id=?", (request_id,))
        request = replace(codec.request(codec.load(row["payload"])), name=name)
        metadata_column = ""
        params = [codec.dump(request), disposition, reason, retry_count]
        if relative_path is not None:
            metadata_column = ", metadata=?"
            params.append(codec.dump(SourceEntry(name, 0, relative_path, request)))
        params.append(request_id)
        await db.execute(
            f"""UPDATE transfer_requests SET payload=?, state='materializing', equivalence_disposition=?,
               equivalence_reason=?, equivalence_retry_count=?, retry_at=0{metadata_column} WHERE id=?""",
            tuple(params),
        )
        await db.commit()


async def _build_completed_slot_with_sibling(tmp_path, monkeypatch, *, slot_name="a.iso", complete=True):
    """One transfer: a request that resolves through the REAL engine path to
    a genuine completed canonical artifact named ``slot_name``, plus one
    additional sibling request (frozen, never resolved) the caller rewrites
    with ``_hold_request`` into whatever shape a given case needs."""
    engine, repository, provider, executor = await _canonical_runtime(tmp_path, monkeypatch)
    transfer = await engine.submit(
        (
            TransferRequest("parcel", "good-1", name=slot_name, preferred_provider=provider.descriptor.id),
            TransferRequest("parcel", "sibling-1", name="__frozen_sibling__", preferred_provider=provider.descriptor.id),
        ),
        name=slot_name, deduplicate=False,
    )
    records = {record.request.payload: record for record in await repository.requests(transfer.id)}
    good, sibling = records["good-1"], records["sibling-1"]
    await _freeze_request(sibling.id)

    await engine.tick()
    artifacts = await repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.request_id == good.id
    assert artifact.state != "completed"

    if complete:
        executor.finish(artifact.execution)
        await engine.tick()
        artifact = (await repository.artifacts(transfer.id))[0]
        assert artifact.state == "completed"

    return engine, repository, transfer, artifact, sibling.id


class _PathedParcelProvider(ParcelProvider):
    """Test-only provider whose single resolved candidate carries an
    explicit, caller-chosen ``relative_path`` -- proving
    ``_logical_slot_key_for_artifact`` reads pathful identity from the
    artifact's own durable candidates rather than collapsing to a bare
    basename (Gate 9 revision: same-basename/different-relative-path
    collisions)."""

    def __init__(self, identity, relative_path):
        super().__init__(identity)
        self._relative_path = relative_path

    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        candidate = replace(
            self.candidate(request.name or "payload.bin", payload=request.payload),
            relative_path=self._relative_path,
        )
        return ResolutionResult(ResourceState.AVAILABLE, (candidate,))


async def _build_pathed_canonical_with_sibling(tmp_path, monkeypatch, *, basename, canonical_relative_path):
    """One transfer: a request whose candidate carries an explicit
    ``relative_path``, resolved through the REAL engine path to a genuine
    completed canonical artifact, plus one frozen sibling request in the same
    transfer for the caller to rewrite via ``_hold_request``."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = _PathedParcelProvider("pathed-provider", canonical_relative_path)
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(
            retry_delay=5, adoption_stability_seconds=0, max_active_executions=8,
            resolution_concurrency=8, same_candidate_no_progress_limit=2,
        ),
        clock=lambda: 1000.0,
    )
    await engine.initialize()

    transfer = await engine.submit(
        (
            TransferRequest("parcel", "good-1", name=basename, preferred_provider=provider.descriptor.id),
            TransferRequest("parcel", "sibling-1", name="__frozen_sibling__", preferred_provider=provider.descriptor.id),
        ),
        name=basename, deduplicate=False,
    )
    records = {record.request.payload: record for record in await repository.requests(transfer.id)}
    good, sibling = records["good-1"], records["sibling-1"]
    await _freeze_request(sibling.id)

    await engine.tick()
    artifacts = await repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.request_id == good.id
    executor.finish(artifact.execution)
    await engine.tick()
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.state == "completed"
    assert artifact.candidates and artifact.candidates[0].relative_path == canonical_relative_path

    return engine, repository, transfer, artifact, sibling.id


async def test_same_basename_same_relative_path_satisfies_and_reaches_completed(tmp_path, monkeypatch):
    """Gate 9 revision (provider-neutral logical-slot identity): same
    basename AND the SAME relative logical path must still satisfy -- a
    pathful canonical artifact (e.g. ``disc1/file.iso``) must not lose its
    directory identity merely because the completion-obligation check reads
    it off the artifact's own candidates rather than a bare filename."""
    engine, repository, transfer, artifact, sibling_id = await _build_pathed_canonical_with_sibling(
        tmp_path, monkeypatch, basename="file.iso", canonical_relative_path="disc1/file.iso",
    )
    await _hold_request(sibling_id, name="file.iso", reason="dns_failure", relative_path="disc1/file.iso")

    outcome = await repository.aggregate_lifecycle(transfer.id, input_required=False)
    assert outcome is not None and outcome.should_complete is True
    await engine._aggregate(transfer.id)
    transfer_after = await repository.get(transfer.id)
    assert transfer_after.state == TransferState.COMPLETED


async def test_same_basename_different_relative_path_remains_queued(tmp_path, monkeypatch):
    """Gate 9 revision (provider-neutral logical-slot identity): same
    basename but a DIFFERENT relative logical path (``disc1/file.iso`` vs.
    ``disc2/file.iso``) must never be excused by the completed canonical --
    the exact collision a bare-filename reduction of the artifact's logical
    key would have falsely satisfied."""
    engine, repository, transfer, artifact, sibling_id = await _build_pathed_canonical_with_sibling(
        tmp_path, monkeypatch, basename="file.iso", canonical_relative_path="disc1/file.iso",
    )
    await _hold_request(sibling_id, name="file.iso", reason="dns_failure", relative_path="disc2/file.iso")

    outcome = await repository.aggregate_lifecycle(transfer.id, input_required=False)
    assert outcome is not None and outcome.should_complete is False
    transfer_after = await repository.get(transfer.id)
    assert transfer_after.state == TransferState.QUEUED


async def test_exhausted_non_writer_satisfied_by_completed_canonical_reaches_completed(tmp_path, monkeypatch):
    """DP 1.0.12 CANON-001 exhausted-identity completion policy, Section 14 --
    the required deterministic RED/GREEN proof and Section 15 Case A.

    Production transfer 270's shape: one completed canonical artifact for a
    logical slot, plus one same-slot request durably held
    (``state=materializing``, ``equivalence_disposition=exhausted``,
    ``retry_at=0``) that never produced its own artifact or execution. This
    assertion (``parent == COMPLETED``) FAILS against starting commit
    ``335fce31`` -- the held sibling's presence alone durably sticks the
    parent at QUEUED even though the user's delivery obligation for that slot
    is already satisfied."""
    engine, repository, transfer, artifact, sibling_id = await _build_completed_slot_with_sibling(
        tmp_path, monkeypatch, slot_name="ubuntu-26.04-desktop-amd64.iso",
    )
    await _hold_request(sibling_id, name="ubuntu-26.04-desktop-amd64.iso", reason="dns_failure")

    outcome = await repository.aggregate_lifecycle(transfer.id, input_required=False)
    assert outcome is not None and outcome.should_complete is True

    # ``aggregate_lifecycle`` alone only decides -- Section 12's own
    # docstring: the caller runs the separate, non-transactional, executor-
    # touching completion sequence (payload verification, in this case
    # trivial file-presence checks) when ``should_complete`` is True. That
    # neutral orchestration (``TransferEngine._aggregate``) is exercised here
    # to observe the actual persisted parent state, not merely the decision.
    await engine._aggregate(transfer.id)
    transfer_after = await repository.get(transfer.id)
    assert transfer_after.state == TransferState.COMPLETED
    assert transfer_after.progress == 100


async def test_held_request_still_blocks_before_canonical_completion(tmp_path, monkeypatch):
    """Section 15 Case B: the same shape as Case A, but the canonical
    artifact has not completed yet -- the held sibling must still keep the
    parent nonterminal."""
    engine, repository, transfer, artifact, sibling_id = await _build_completed_slot_with_sibling(
        tmp_path, monkeypatch, slot_name="ubuntu-26.04-desktop-amd64.iso", complete=False,
    )
    await _hold_request(sibling_id, name="ubuntu-26.04-desktop-amd64.iso", reason="dns_failure")

    await repository.aggregate_lifecycle(transfer.id, input_required=False)
    transfer_after = await repository.get(transfer.id)
    assert transfer_after.state != TransferState.COMPLETED


async def test_no_satisfying_canonical_keeps_existing_quiescent_queued_behavior(tmp_path, monkeypatch):
    """Section 15 Case C: a held/exhausted non-writer with no completed
    canonical anywhere in the transfer for its slot must keep the existing
    (pre-CANON-001-follow-up) quiescent QUEUED behavior -- this is the
    conservative default this correction must never weaken."""
    engine, repository, provider, executor = await _canonical_runtime(tmp_path, monkeypatch)
    transfer = await engine.submit(
        (TransferRequest("parcel", "lone-1", name="only.iso", preferred_provider=provider.descriptor.id),),
        name="only.iso", deduplicate=False,
    )
    record = (await repository.requests(transfer.id))[0]
    await _hold_request(record.id, name="only.iso", reason="dns_failure")

    outcome = await repository.aggregate_lifecycle(transfer.id, input_required=False)
    assert outcome is not None and outcome.should_complete is False
    transfer_after = await repository.get(transfer.id)
    assert transfer_after.state == TransferState.QUEUED


async def test_different_logical_slot_remains_independent_obligation(tmp_path, monkeypatch):
    """Section 15 Case D: ``a.iso`` completed must never satisfy a held
    ``b.iso`` sibling -- distinct logical slots remain distinct obligations."""
    engine, repository, transfer, artifact, sibling_id = await _build_completed_slot_with_sibling(
        tmp_path, monkeypatch, slot_name="a.iso",
    )
    await _hold_request(sibling_id, name="b.iso", reason="dns_failure")

    outcome = await repository.aggregate_lifecycle(transfer.id, input_required=False)
    assert outcome is not None and outcome.should_complete is False
    transfer_after = await repository.get(transfer.id)
    assert transfer_after.state != TransferState.COMPLETED
    assert transfer_after.state == TransferState.QUEUED


async def test_genuine_independent_failure_still_votes_failed(tmp_path, monkeypatch):
    """Section 15 Case E: a genuinely independent, terminally-failed artifact
    (never merely held) must still fail the parent even though a sibling slot
    completed -- the correction must never degrade into "any artifact
    completed => transfer completed"."""
    engine, repository, transfer, artifacts = await _build_two_artifact_transfer(tmp_path, monkeypatch)
    completed_artifact, failing_artifact = artifacts[0], artifacts[1]

    executor = engine.registry.executors[completed_artifact.execution.executor_id]
    executor.finish(completed_artifact.execution)
    await engine.reconcile_executions()
    completed_after = next(
        item for item in await repository.artifacts(transfer.id) if item.id == completed_artifact.id
    )
    assert completed_after.state == "completed"
    # A genuinely independent, terminally-failed artifact -- distinct from a
    # scheduler-held REQUEST -- has no durable canonical mapping of its own,
    # so it always still casts its FAILED vote (transfers._repository_base
    # ._satisfied_elsewhere only ever excuses a failed row with a durable
    # canonical target). Direct status mutation exercises exactly that
    # durable fact without depending on the separately-tested recovery state
    # machine that would normally produce it.
    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET status='error' WHERE id=?", (failing_artifact.id,))
        await db.commit()

    outcome = await repository.aggregate_lifecycle(transfer.id, input_required=False)
    assert outcome is not None and outcome.should_complete is False
    transfer_after = await repository.get(transfer.id)
    assert transfer_after.state == TransferState.FAILED


async def test_affirmatively_independent_disposition_is_never_excused_by_same_name_completion(tmp_path, monkeypatch):
    """Section 15 Case F: a same-name sibling that is NOT held (its
    disposition is affirmatively ``independent``, outside
    ``_HELD_DISPOSITIONS``) must never be treated as satisfied merely because
    a same-named artifact completed -- it keeps materializing its own
    independent obligation, so the parent stays nonterminal until IT resolves
    too."""
    engine, repository, transfer, artifact, sibling_id = await _build_completed_slot_with_sibling(
        tmp_path, monkeypatch, slot_name="a.iso",
    )
    await _hold_request(sibling_id, name="a.iso", reason="sample_mismatch", disposition="independent")

    outcome = await repository.aggregate_lifecycle(transfer.id, input_required=False)
    assert outcome is not None and outcome.should_complete is False
    transfer_after = await repository.get(transfer.id)
    assert transfer_after.state != TransferState.COMPLETED
    assert transfer_after.state == TransferState.RESOLVING


async def test_multiple_slots_all_satisfied_reaches_completed(tmp_path, monkeypatch):
    """Section 15 Case G: two distinct logical slots, both completed; one of
    them also has a held exhausted alternate. The parent must still reach
    COMPLETED -- every slot has its own satisfied obligation, this is not a
    coincidental single-slot special case."""
    engine, repository, provider, executor = await _canonical_runtime(tmp_path, monkeypatch)
    transfer = await engine.submit(
        (
            TransferRequest("parcel", "good-a", name="a.iso", preferred_provider=provider.descriptor.id),
            TransferRequest("parcel", "good-b", name="b.iso", preferred_provider=provider.descriptor.id),
            TransferRequest("parcel", "sibling-1", name="__frozen_sibling__", preferred_provider=provider.descriptor.id),
        ),
        name="a.iso", deduplicate=False,
    )
    records = {record.request.payload: record for record in await repository.requests(transfer.id)}
    sibling = records["sibling-1"]
    await _freeze_request(sibling.id)

    await engine.tick()
    artifacts = {item.request_id: item for item in await repository.artifacts(transfer.id)}
    assert len(artifacts) == 2
    for artifact in artifacts.values():
        executor.finish(artifact.execution)
    await engine.tick()
    artifacts_after = await repository.artifacts(transfer.id)
    assert all(item.state == "completed" for item in artifacts_after)

    await _hold_request(sibling.id, name="a.iso", reason="dns_failure")

    outcome = await repository.aggregate_lifecycle(transfer.id, input_required=False)
    assert outcome is not None and outcome.should_complete is True
    await engine._aggregate(transfer.id)
    transfer_after = await repository.get(transfer.id)
    assert transfer_after.state == TransferState.COMPLETED


async def _rebuild_engine(tmp_path):
    """A brand new engine/repository/registry/executor bound to the SAME
    durable database -- Section 15 Case H requires the decision (and the
    completion sequence it feeds) to be re-derivable from cold state, with no
    in-memory classification carried over from whatever process originally
    produced the durable facts."""
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(
            retry_delay=5, adoption_stability_seconds=0, max_active_executions=8,
            resolution_concurrency=8, same_candidate_no_progress_limit=2,
        ),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return engine, repository


async def test_restart_reentry_reaggregation_reaches_completed_with_no_in_memory_state(tmp_path, monkeypatch):
    """Section 15 Case H: persisting Case A's shape and re-deriving the
    decision from a BRAND NEW ``TransferRepository``/``TransferEngine``
    instance against the same durable database must reach the identical
    COMPLETED conclusion -- no in-memory classification may be required."""
    engine, repository, transfer, artifact, sibling_id = await _build_completed_slot_with_sibling(
        tmp_path, monkeypatch, slot_name="ubuntu-26.04-desktop-amd64.iso",
    )
    await _hold_request(sibling_id, name="ubuntu-26.04-desktop-amd64.iso", reason="dns_failure")

    fresh_engine, fresh_repository = await _rebuild_engine(tmp_path)
    outcome = await fresh_repository.aggregate_lifecycle(transfer.id, input_required=False)
    assert outcome is not None and outcome.should_complete is True
    await fresh_engine._aggregate(transfer.id)
    transfer_after = await fresh_repository.get(transfer.id)
    assert transfer_after.state == TransferState.COMPLETED


async def test_no_bad_source_provenance_fabrication_after_completion(tmp_path, monkeypatch):
    """Section 15 Case I: after Case A's parent reaches COMPLETED, the held
    sibling must show zero fabricated provenance -- no canonical binding, no
    artifact row, no execution attempt -- and its real proof-failure reason
    must remain untouched."""
    engine, repository, transfer, artifact, sibling_id = await _build_completed_slot_with_sibling(
        tmp_path, monkeypatch, slot_name="ubuntu-26.04-desktop-amd64.iso",
    )
    await _hold_request(sibling_id, name="ubuntu-26.04-desktop-amd64.iso", reason="dns_failure")

    outcome = await repository.aggregate_lifecycle(transfer.id, input_required=False)
    assert outcome is not None and outcome.should_complete is True
    await engine._aggregate(transfer.id)
    transfer_after = await repository.get(transfer.id)
    assert transfer_after.state == TransferState.COMPLETED

    async with database.get_db() as db:
        artifact_count = await db.fetchone(
            "SELECT COUNT(*) AS n FROM download_files WHERE request_id=?", (sibling_id,),
        )
        execution_count = await db.fetchone(
            "SELECT COUNT(*) AS n FROM execution_attempts WHERE artifact_id IN "
            "(SELECT id FROM download_files WHERE request_id=?)", (sibling_id,),
        )
        binding_count = await db.fetchone(
            "SELECT COUNT(*) AS n FROM canonical_candidate_origins WHERE request_id=?", (sibling_id,),
        )
        request_row = await db.fetchone(
            "SELECT state,equivalence_disposition,equivalence_reason,retry_at FROM transfer_requests WHERE id=?",
            (sibling_id,),
        )
    assert int(artifact_count["n"]) == 0
    assert int(execution_count["n"]) == 0
    assert int(binding_count["n"]) == 0
    assert request_row["state"] == "materializing"
    assert request_row["equivalence_disposition"] == "exhausted"
    assert request_row["equivalence_reason"] == "dns_failure"
    assert float(request_row["retry_at"] or 0) == 0


async def test_completion_obligation_helper_consumed_only_by_aggregate_lifecycle():
    """Section 18 anti-layering structural requirement: the new completion-
    blocking-hold determination must be reachable ONLY from
    ``TransferRepository.aggregate_lifecycle`` -- never reimplemented or
    referenced as a second, independently-timed decision in any engine
    layer."""
    import inspect

    import transfers._engine_base as engine_base_module
    import transfers._engine_recovery as engine_recovery_module
    import transfers.engine as engine_module
    import transfers.convergence_engine as convergence_engine_module
    from transfers import _repository_base

    helper_name = "_completion_obligation_satisfied"
    assert hasattr(_repository_base, helper_name), (
        "the CANON-001 exhausted-identity completion-obligation helper must live in the "
        "canonical lifecycle module, transfers._repository_base"
    )
    owner_source = inspect.getsource(_repository_base.TransferRepository.aggregate_lifecycle)
    assert helper_name in owner_source, (
        "TransferRepository.aggregate_lifecycle must be the consumer of the completion-"
        "obligation helper"
    )

    for module in (engine_base_module, engine_recovery_module, engine_module, convergence_engine_module):
        assert helper_name not in inspect.getsource(module), (
            f"{module.__name__} must not reference or reimplement the canonical completion-"
            f"obligation helper {helper_name!r} -- it may be consumed only from "
            "TransferRepository.aggregate_lifecycle"
        )


async def test_quiescent_hold_contract_shares_one_disposition_set_with_cohorts():
    """DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    closure, Gate 9 revision): the parent-lifecycle quiescent-hold check in
    ``TransferRepository.aggregate_lifecycle`` is not a second, independently
    invented interpretation of ``(state, equivalence_disposition)`` -- it
    consumes the EXACT SAME ``_HELD_DISPOSITIONS`` object
    ``transfers.cohorts.coordinate_collection`` already uses to stop
    autonomous materialization for a held request (object identity, not
    merely equal values, so the two call sites cannot silently drift apart
    into different disposition sets over time)."""
    from transfers import _repository_base, cohorts

    assert _repository_base._HELD_DISPOSITIONS is cohorts._HELD_DISPOSITIONS


# ---------------------------------------------------------------------------
# DP 1.0.12 canonical transfer-detail materialized-path/provenance
# correction, Case D and Case H.
# ---------------------------------------------------------------------------

async def test_exhausted_non_writer_never_becomes_target_path_owner(tmp_path, monkeypatch):
    """Case D: transfer-270 shape at the transfer-detail materialized-path
    layer. The durably held, proof-exhausted, non-writer sibling (no
    artifact/execution/binding of its own) must never own or influence the
    materialized path; the completed canonical artifact's own target remains
    current, and the exhausted sibling never materializes a row that could
    even be considered."""
    engine, repository, transfer, artifact, sibling_id = await _build_completed_slot_with_sibling(
        tmp_path, monkeypatch, slot_name="ubuntu-26.04-desktop-amd64.iso",
    )
    await _hold_request(sibling_id, name="ubuntu-26.04-desktop-amd64.iso", reason="dns_failure")

    outcome = await repository.aggregate_lifecycle(transfer.id, input_required=False)
    assert outcome is not None and outcome.should_complete is True
    await engine._aggregate(transfer.id)
    transfer_after = await repository.get(transfer.id)
    assert transfer_after.state == TransferState.COMPLETED

    expected_path = str(Path(artifact.target).parent)
    details = await repository.presentation(transfer.id, details=True)
    assert details["local_path"] == expected_path
    async with database.get_db() as db:
        sibling_artifact_count = await db.fetchone(
            "SELECT COUNT(*) AS n FROM download_files WHERE request_id=?", (sibling_id,),
        )
    assert int(sibling_artifact_count["n"]) == 0, (
        "the exhausted, unresolved sibling must never materialize its own row -- there is "
        "nothing for presentation to even mistakenly select"
    )


async def test_multi_slot_per_file_target_paths_remain_isolated(tmp_path, monkeypatch):
    """Case H: two distinct logical slots (``a.iso``, ``b.iso``) each
    complete their own independent canonical artifact in the same transfer.
    Per-file materialized path in transfer-detail (``files[].local_path``)
    must map only to its own artifact -- never cross-attributed from one
    slot's request to the other's target."""
    engine, repository, provider, executor = await _canonical_runtime(tmp_path, monkeypatch)
    transfer = await engine.submit(
        (
            TransferRequest("parcel", "good-a", name="a.iso", preferred_provider=provider.descriptor.id),
            TransferRequest("parcel", "good-b", name="b.iso", preferred_provider=provider.descriptor.id),
        ),
        name="a.iso", deduplicate=False,
    )
    await engine.tick()
    artifacts = {item.name: item for item in await repository.artifacts(transfer.id)}
    assert set(artifacts) == {"a.iso", "b.iso"}
    for artifact in artifacts.values():
        executor.finish(artifact.execution)
    await engine.tick()

    details = await repository.presentation(transfer.id, details=True)
    files_by_name = {item["filename"]: item for item in details["files"]}
    assert files_by_name["a.iso"]["local_path"] == artifacts["a.iso"].target
    assert files_by_name["b.iso"]["local_path"] == artifacts["b.iso"].target
    assert files_by_name["a.iso"]["local_path"] != files_by_name["b.iso"]["local_path"]
