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

import pytest

import db.database as database
from fake_integrations import MemoryExecutor
from test_manual_candidate_failover import HostParcelProvider
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Recovery, Retryability, Stage
from transfers.models import ExecutionState, TransferRequest, TransferState
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
