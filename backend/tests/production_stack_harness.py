"""Reusable production-stack test harness (DP 1.0.12 recovery leveling, Section 32).

Instantiates the ACTUAL production recovery repository/engine ownership chain --
``transfers.convergence_engine.TransferEngine`` + ``transfers.recovery_repository
.TransferRepository`` + ``transfers.registry.IntegrationRegistry`` -- matching
``application.composition.compose()`` symbol-for-symbol. This is deliberately
NOT the lower ``transfers.engine.TransferEngine`` + ``transfers.manual_repository
.TransferRepository`` stack ``test_manual_candidate_failover.py`` instantiated
directly before this leveling pass (Section 0.7): that lower stack skips every
Phase-3 claim/dispatch/truth/retry layer between ``engine.TransferEngine`` and
``convergence_engine.TransferEngine``, so a regression only those layers can
produce is invisible to a test built on it.

Defaults to LOW capacity (``max_active_executions=1``) so capacity-vs-recovery
tests (Section 9) are deterministic with only two artifacts, not dozens.
"""
from __future__ import annotations

import core.config as config
import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.convergence_engine import TransferEngine
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry
from transfers.settings import TransferSettings


async def build_production_runtime(
    tmp_path,
    monkeypatch,
    *,
    db_name: str = "production_stack.db",
    max_active_executions: int = 1,
    provider_ids: tuple[str, ...] = ("provider-a",),
    now: float = 12000.0,
    **policy_overrides,
):
    """Build the real production engine/repository/registry ownership chain.

    Returns ``(repository, registry, providers, executor, engine, now_box)``.
    ``providers`` is a tuple aligned with ``provider_ids``. ``now_box`` is a
    one-item list the caller mutates to advance the injected deterministic
    clock the engine was built with (``engine.clock``).
    """
    monkeypatch.setattr(database, "DB_PATH", tmp_path / db_name)
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    providers = tuple(ParcelProvider(provider_id) for provider_id in provider_ids)
    executor = MemoryExecutor(repository.authorize_execution)
    for provider in providers:
        registry.register_provider(provider)
    registry.register_executor(executor)
    now_box = [now]
    policy = TransferPolicy(
        retry_delay=0,
        adoption_stability_seconds=0,
        max_active_executions=max_active_executions,
        **policy_overrides,
    )
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=policy, clock=lambda: now_box[0],
    )
    await engine.initialize()
    # Keep the durable settings singleton in sync with the engine's actual
    # policy the same way application.composition.configure() does in real
    # production (max_active_executions=settings.transfer_policy
    # .max_concurrent_executions). transfers.presentation_repository reads
    # capacity thresholds from settings rather than a live engine reference
    # (a repository must not hold one), so a test harness must keep the two
    # in sync exactly as production does, or Section 9's capacity-wait
    # classification silently never activates in tests.
    monkeypatch.setattr(config, "_settings", config.get_settings().model_copy(
        update={"transfer_policy": TransferSettings(max_concurrent_executions=max_active_executions)},
    ))
    return repository, registry, providers, executor, engine, now_box
