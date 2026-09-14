"""Enforce final runtime ownership rather than the previous wrapper structure."""
import ast
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RETIRED = {
    "manager_v2", "torrent_state", "transfer_service", "provider_gateway", "aria2_gateway",
    "aria2_error_recovery", "ownership_ledger", "transfer_state_machine", "transfer_control_service",
    "dispatch_coordinator", "reconciliation_service", "transfer_repository", "transfer_control",
    "restart_resume_control", "transfer_integrity", "transfer_runtime_guard", "direct_link_result_guard",
    "direct_link_retry_guard", "extraction_service",
}


def test_superseded_owners_are_physically_absent_and_never_imported():
    for name in RETIRED:
        assert not (ROOT / "services" / f"{name}.py").exists()
    for path in ROOT.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {"services." + name for name in RETIRED}, path
            elif isinstance(node, ast.Import):
                assert not any(alias.name in {"services." + name for name in RETIRED} for alias in node.names), path


# DP 1.0.12 leveling remediation (ARCH-001): the eight historical
# `_convergence_phase3_*` / `_recovery_repository_*` inheritance layers are
# gone. Production is a single engine class (transfers.convergence_engine
# .TransferEngine) and a single repository class (transfers.recovery_repository
# .TransferRepository), neither depending on override order for correctness.
RETIRED_TRANSFER_MODULES = {
    "_convergence_phase3_base", "_convergence_phase3_public_base",
    "_convergence_phase3_retry_base", "_convergence_phase3_truth_base",
    "_convergence_phase3_dispatch_base",
    "_recovery_repository_claim_base", "_recovery_repository_audit",
    "_recovery_repository_phase3",
}


def test_retired_recovery_leveling_layers_are_physically_absent_and_never_imported():
    for name in RETIRED_TRANSFER_MODULES:
        assert not (ROOT / "transfers" / f"{name}.py").exists()
    qualified = {"transfers." + name for name in RETIRED_TRANSFER_MODULES}
    for path in ROOT.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in qualified, path
            elif isinstance(node, ast.Import):
                assert not any(alias.name in qualified for alias in node.names), path


def test_production_engine_and_repository_mro_is_shallow_and_exact():
    from transfers.convergence_engine import TransferEngine
    from transfers.recovery_repository import TransferRepository

    engine_mro = [f"{cls.__module__}.{cls.__name__}" for cls in TransferEngine.__mro__]
    assert engine_mro == [
        "transfers.convergence_engine.TransferEngine",
        "transfers.engine.TransferEngine",
        "transfers._engine_recovery.TransferEngine",
        "transfers._engine_base.TransferEngine",
        "builtins.object",
    ]

    repository_mro = [f"{cls.__module__}.{cls.__name__}" for cls in TransferRepository.__mro__]
    assert repository_mro == [
        "transfers.recovery_repository.TransferRepository",
        "transfers.manual_repository.TransferRepository",
        "transfers.presentation_repository.TransferRepository",
        "transfers.repository.TransferRepository",
        "transfers._repository_base.TransferRepository",
        "builtins.object",
    ]


def test_engine_recovery_no_longer_mutates_another_module_stable_payload():
    """ARCH-001: the transitional cross-module monkeypatch seam
    (_engine_recovery <-> _engine_base <-> engine, each reassigning the
    other's ``stable_payload``/``retire_partial`` module attribute at import
    time) is gone. Every owner now calls transfers.filesystem's real
    function through a normal, unmutated import."""
    for name in ("_engine_recovery.py", "_engine_base.py", "engine.py"):
        source = (ROOT / "transfers" / name).read_text()
        assert "_stable_payload_proxy" not in source
        assert "_retire_partial_proxy" not in source
        assert ".stable_payload = " not in source
        assert ".retire_partial = " not in source


def test_application_commands_initialize_without_concrete_integrations():
    code = '''
import builtins
original = builtins.__import__
def isolated(name, *args, **kwargs):
    if name.startswith(("providers.", "executors.", "postprocessors.")):
        raise AssertionError("Concrete integration import: " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = isolated
from application.service import ApplicationService
from transfers.engine import TransferEngine
from transfers.repository import TransferRepository
from transfers.registry import IntegrationRegistry
service = ApplicationService(TransferEngine(TransferRepository(), IntegrationRegistry(), download_root="/tmp/unused"))
assert service.engine.registry.providers == {}
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_api_commands_never_write_lifecycle_or_execution_fields():
    tree = ast.parse((ROOT / "api/routes.py").read_text())
    forbidden = ("SET status", "download_id=", "alldebrid_id=", "provider_status=", "execution_attempt_id=")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not any(fragment.casefold() in node.value.casefold() for fragment in forbidden)


def test_scheduler_has_one_owner_for_each_cadence():
    source = (ROOT / "core/scheduler.py").read_text()
    assert "application.resolve_pending()" in source
    assert "application.reconcile_executions()" in source
    assert "application.process_postprocessors()" in source
    assert "executors.aria2" not in source
    assert "providers.alldebrid" not in source
    assert "_orig_" not in source


def test_native_client_has_no_duplicate_retry_or_adoption_owner():
    source = (ROOT / "executors/aria2/client.py").read_text()
    assert "def ensure_download" not in source
    assert "def find_existing_download" not in source
    assert "def _find_all_matches" not in source
