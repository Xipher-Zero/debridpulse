"""1.0.13 Gate-9 rev-4, item 1: the attempt exists BEFORE its footprint.

`ExecutionWork.attempt_id` is only useful if core allocates the attempt before
it asks the executor to plan that attempt's native transient material. One
attempt identity must drive the work, the footprint, the request, prepare()
and the durable prepared execution.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import inspect
from types import SimpleNamespace

import pytest

import db.database as database
from transfers.models import (
    ExecutionRequest, ExecutionSubject, ExecutionWork, MaterializationKind,
    MaterializationPlan, TransferCandidate, TransferRequest,
)

VALID_NZB = (b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
             b'<file poster="p@e.net" date="1700000000" subject="job [1/1] - &quot;job.bin&quot; yEnc (1/1)">'
             b"<groups><group>alt.binaries.test</group></groups>"
             b'<segments><segment bytes="1024" number="1">a@e.net</segment></segments>'
             b"</file></nzb>")


# --- the invariant is exact, not permissive -----------------------------

def test_an_execution_request_requires_its_work_to_name_the_same_attempt():
    candidate = TransferCandidate(name="n", endpoints=(), request_kind="nzb",
                                  materialization=MaterializationKind.COLLECTION)
    plan = MaterializationPlan(MaterializationKind.COLLECTION, "/dl/n")
    subject = ExecutionSubject.of(candidate)

    ok = ExecutionRequest(ExecutionWork(subject, plan, "attempt-A"), "attempt-A")
    assert ok.attempt_id == "attempt-A"

    with pytest.raises(ValueError):
        ExecutionRequest(ExecutionWork(subject, plan, "attempt-B"), "attempt-A")
    # Once a request exists the attempt is known, so unattributed work is a bug.
    with pytest.raises(ValueError):
        ExecutionRequest(ExecutionWork(subject, plan), "attempt-A")


# --- core orders allocation before footprint ----------------------------

def test_dispatch_allocates_the_attempt_before_evaluating_the_footprint():
    from transfers import _engine_base
    import textwrap
    source = textwrap.dedent(inspect.getsource(_engine_base.TransferEngine._dispatch))
    tree = ast.parse(source)
    order = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("_work", "_footprint"):
                order.append((node.lineno, node.func.attr))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "new_identity":
            order.append((node.lineno, "new_identity"))
    order.sort()
    names = [name for _line, name in order]
    assert "new_identity" in names, names
    assert names.index("new_identity") < names.index("_footprint"), names


# --- the first dispatch sees the real trees -----------------------------

@pytest.mark.asyncio
async def test_the_first_dispatch_sees_the_attempts_transient_trees(tmp_path, monkeypatch):
    """The FIRST footprint of a new attempt must already name its trees."""
    from executors.sabnzbd.executor import SabnzbdConfiguration, SabnzbdExecutor
    from providers.usenet.provider import UsenetProvider
    from sab_fakes import FakeSab
    from transfers.convergence_engine import TransferEngine
    from transfers.policy import TransferPolicy
    from transfers.recovery_repository import TransferRepository
    from transfers.registry import IntegrationRegistry

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    root = tmp_path / "payloads"
    (root / ".dpwork" / "complete").mkdir(parents=True)
    (root / ".dpwork" / "incomplete").mkdir(parents=True)
    sab = FakeSab(complete_dir=str(root / ".dpwork" / "complete"),
                  download_dir=str(root / ".dpwork" / "incomplete"))

    seen = []

    class RecordingExecutor(SabnzbdExecutor):
        def footprint(self, work):
            result = super().footprint(work)
            seen.append((getattr(work, "attempt_id", None), result))
            return result

    registry = IntegrationRegistry()
    registry.register_provider(UsenetProvider())
    registry.register_executor(RecordingExecutor(
        sab, SabnzbdConfiguration(local_root=str(root),
                                  working_directory=str(root / ".dpwork"),
                                  complete_directory=str(root / ".dpwork" / "complete")),
        repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(root),
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                                                  max_active_executions=2),
                            clock=lambda: 1000.0)
    await engine.initialize()
    await engine.submit((TransferRequest("nzb", VALID_NZB, name="job.nzb"),), name="job",
                        deduplicate=False)
    for _ in range(8):
        await engine.tick()
        await asyncio.sleep(0)

    assert seen, "footprint() was never consulted"
    first_attempt, first_footprint = seen[0]
    assert first_attempt, f"the FIRST footprint had no attempt id: {seen[:2]}"
    assert first_footprint.transient_trees, "the first footprint was empty"
    assert any(".dpwork" in path for path in first_footprint.transient_trees)
    # Every footprint core asked for belongs to a real attempt.
    assert all(attempt for attempt, _ in seen), seen


@pytest.mark.asyncio
async def test_plan_validation_receives_the_attempt_trees(tmp_path, monkeypatch):
    """`validate_plan`/`material_initially_absent` must see the real trees."""
    from executors.sabnzbd.executor import SabnzbdConfiguration, SabnzbdExecutor
    from transfers.filesystem import material_initially_absent, validate_plan

    root = tmp_path / "payloads"
    (root / ".dpwork" / "complete").mkdir(parents=True)
    (root / ".dpwork" / "incomplete").mkdir(parents=True)

    async def authorize(handle, action):
        return True

    executor = SabnzbdExecutor(
        SimpleNamespace(), SabnzbdConfiguration(
            local_root=str(root), working_directory=str(root / ".dpwork"),
            complete_directory=str(root / ".dpwork" / "complete")), authorize)
    candidate = TransferCandidate(
        name="posted", endpoints=(), request_kind="nzb",
        materialization=MaterializationKind.COLLECTION,
        context={"nzb_base64": base64.b64encode(VALID_NZB).decode()})
    plan = MaterializationPlan(MaterializationKind.COLLECTION, str(root / "posted"))
    work = ExecutionWork(ExecutionSubject.of(candidate), plan, "attempt-Z")

    footprint = executor.footprint(work)
    assert footprint.transient_trees
    transient = validate_plan(str(root), plan, footprint)
    assert len(transient) == len(footprint.transient_trees)
    # Nothing exists yet, so the attempt's material is initially absent...
    assert material_initially_absent(plan, footprint) is True
    # ...and pre-existing scratch for THIS attempt is detected.
    first = footprint.transient_trees[0]
    __import__("os").makedirs(first, exist_ok=True)
    assert material_initially_absent(plan, footprint) is False
