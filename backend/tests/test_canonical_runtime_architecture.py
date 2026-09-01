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
