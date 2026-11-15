"""One cancel -> delete scenario against the canonical engine and repository,
run as its own process by ``test_v114_cancel_delete_retirement.py``.

The process is the isolation: ``DB_PATH`` (the product's own setting, read by
``db.database`` at import) names this scenario's database, so nothing is
patched. The executor is the existing ledger fake (``executor_fakes``); the
engine, repository, ownership records and retirement are production code.

Usage: python cancel_delete_world.py <scenario> <work-dir>   (prints one JSON object)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

scenario, work = sys.argv[1], Path(sys.argv[2])
os.environ["DB_PATH"] = str(work / "state.db")
sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

import db.database as database  # noqa: E402
from executor_fakes import LedgerExecutor, LedgerProvider  # noqa: E402
from transfers.convergence_engine import TransferEngine  # noqa: E402
from transfers.models import TransferRequest  # noqa: E402
from transfers.policy import TransferPolicy  # noqa: E402
from transfers.recovery_repository import TransferRepository  # noqa: E402
from transfers.registry import IntegrationRegistry  # noqa: E402

report: dict = {"scenario": scenario}
clock = [float(os.environ.get("WORLD_CLOCK", "1000"))]


async def build():
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    registry.register_provider(LedgerProvider())
    executor = LedgerExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(work / "payloads"),
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                                                  max_active_executions=3),
                            clock=lambda: clock[0])
    await engine.initialize()
    return engine, repository, executor


async def cleanup_state(attempt_id):
    async with database.get_db() as db:
        row = await db.fetchone("SELECT cleanup_state,authorized,state FROM execution_attempts WHERE id=?",
                                (attempt_id,))
    return dict(row) if row else None


def files(root: Path) -> list:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) if root.exists() else []


async def main() -> None:
    engine, repository, executor = await build()
    root = work / "payloads"
    state_file = work / "world.json"

    if scenario == "restart-after":
        # A fresh process over the same durable state: nothing in memory
        # survives; the executor no longer knows the native job (ABSENT).
        saved = json.loads(state_file.read_text())
        clock[0] = saved["clock"] + 10_000
        await engine.reconcile_executions()
        await engine.tick()
        report["after restart"] = {"material": Path(saved["target"]).exists(),
                                   "cleanup": await cleanup_state(saved["attempt"]),
                                   "unrelated": (root / "unrelated.bin").read_bytes() == b"unrelated",
                                   "files": files(root)}
        print(json.dumps(report, default=str))
        return

    root.mkdir(parents=True, exist_ok=True)
    (root / "unrelated.bin").write_bytes(b"unrelated")
    requests = (TransferRequest("ledger", "single", name="single"),)
    if scenario == "completed":
        requests += (TransferRequest("ledger", "delivered", name="delivered"),)
    transfer = await engine.submit(requests)
    if scenario == "unowned":
        await engine.resolve_pending()
        artifact = (await repository.artifacts(transfer.id))[0]
        Path(artifact.target).parent.mkdir(parents=True, exist_ok=True)
        Path(artifact.target).write_bytes(b"operator material")  # pre-dates the execution
        await engine.reconcile_executions()
    else:
        await engine.tick()
    artifacts = await repository.artifacts(transfer.id)
    artifact = artifacts[0]
    if scenario == "completed":
        # The sibling completes before the Cancel: its delivered payload must
        # survive Delete while the cancelled file's partial is reclaimed.
        delivered = artifacts[1]
        executor.finish_file(delivered.execution, delivered.target, b"done")
        for _ in range(3):
            await engine.tick()
        report["sibling before cancel"] = (await repository.artifacts(transfer.id))[1].state
    handle = artifact.execution
    target = Path(artifact.target)
    # The ledger executor's declared FILE footprint (``LedgerExecutor.footprint``).
    journal = Path(str(target) + ".ledger-journal")
    report["owned"] = await repository.execution_owns_target(handle)
    if scenario in {"partial-on", "repeat", "live", "lost-ack", "restart-before", "completed"}:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00" * 16)          # the writer's partial target
    if scenario in {"partial-off", "repeat"}:
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_bytes(b"journal")           # the writer's declared transient material
    executor.run(handle)
    material = journal if scenario == "partial-off" else target
    executor.cancel_mode = {"live": "unconfirmed", "restart-before": "unconfirmed",
                            "lost-ack": "lost_ack"}.get(scenario, "confirm")

    errors = await engine.cancel(transfer.id)
    report["after cancel"] = {"errors": [e.category.value for e in errors], "material": material.exists(),
                              "cleanup": await cleanup_state(handle.attempt_id),
                              "writer": executor.job_for(handle).state.value if executor.job_for(handle) else None}

    await engine.delete(transfer.id)
    report["after delete"] = {"transfer": (await repository.get(transfer.id)).state.value,
                              "material": material.exists(),
                              "target": target.read_bytes() if target.exists() else None,
                              "journal": journal.exists(),
                              "cleanup": await cleanup_state(handle.attempt_id),
                              "writer": executor.job_for(handle).state.value if executor.job_for(handle) else None,
                              "unrelated": (root / "unrelated.bin").read_bytes() == b"unrelated",
                              "files": files(root)}
    if scenario == "completed":
        sibling = (await repository.artifacts(transfer.id))[1]
        report["after delete"]["sibling"] = {"state": sibling.state,
                                             "payload": Path(sibling.target).read_bytes()
                                             if Path(sibling.target).exists() else None}
    if scenario == "repeat":
        await engine.delete(transfer.id)
        report["after second delete"] = {"material": target.exists() or journal.exists(),
                                         "cleanup": await cleanup_state(handle.attempt_id),
                                         "unrelated": (root / "unrelated.bin").read_bytes() == b"unrelated",
                                         "files": files(root)}
    if scenario == "lost-ack":
        clock[0] += 10_000
        await engine.tick()
        report["after the retained obligation's next pass"] = {"material": material.exists(),
                                                               "cleanup": await cleanup_state(handle.attempt_id)}
    if scenario == "restart-before":
        state_file.write_text(json.dumps({"clock": clock[0], "target": str(target), "attempt": handle.attempt_id}))
    print(json.dumps(report, default=str))


asyncio.run(main())
