"""Permanent import/policy boundaries for the canonical transfer package."""
import ast
import json
from pathlib import Path
import subprocess
import sys

from api.serializers import public_download_file, public_payload, public_torrent
from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage
from transfers.policy import TransferPolicy


def test_core_imports_without_any_production_integration():
    script = """
import importlib.abc
import sys
class RejectIntegrations(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.startswith(('providers.', 'executors.')):
            raise AssertionError('Concrete integration imported: ' + fullname)
sys.meta_path.insert(0, RejectIntegrations())
from transfers.engine import TransferEngine
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository
engine = TransferEngine(TransferRepository(), IntegrationRegistry(), download_root='/tmp/proof')
assert engine.registry.providers == {}
assert engine.registry.executors == {}
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_universal_modules_never_import_or_branch_on_native_integrations():
    root = Path(__file__).parents[1] / "transfers"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("providers.", "executors.", "services.manager")), path
            if isinstance(node, ast.Import):
                assert not any(alias.name.startswith(("providers.", "executors.", "services.manager")) for alias in node.names), path
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in {"alldebrid", "aria2", "statusCode", "MAGNET_INVALID_ID", "LINK_DOWN"}, path
            if path.name in {"engine.py", "policy.py", "registry.py"} and isinstance(node, ast.Attribute):
                assert node.attr not in {"native_code", "diagnostic"}, (path, node.attr)


def test_core_policy_ignores_native_code_message_and_context():
    policy = TransferPolicy()
    error = NormalizedError(Domain.NETWORK, Category.CONNECTION_TIMEOUT, Stage.EXECUTION,
                            Retryability.BACKOFF, Recovery.BACKOFF,
                            native_code="new-provider-error", diagnostic="do not retry", context={"number": 7})
    first = policy.retry(error, 1, 100)
    changed = NormalizedError(Domain.NETWORK, Category.CONNECTION_TIMEOUT, Stage.EXECUTION,
                              Retryability.BACKOFF, Recovery.BACKOFF,
                              native_code="permanent", diagnostic="always retry", context={"number": 8})
    assert first == policy.retry(changed, 1, 100)


def test_presentation_never_exposes_opaque_context_or_native_diagnostics():
    error = NormalizedError(Domain.PROVIDER, Category.UNMAPPED_PROVIDER_ERROR, Stage.RESOLUTION,
                            native_code="opaque-native-code", diagnostic="native diagnostic")
    row = {"id": 1, "name": "file", "filename": "file", "status": "error",
           "normalized_error": json.dumps(error.as_dict(diagnostics=True)),
           "candidates": "opaque-signed-capability", "handle": "opaque-handle-secret",
           "context": {"api_key": "opaque-key-secret"}, "payload": "opaque-request-secret"}
    for value in (public_torrent(row), public_download_file(row), public_payload({"rows": [row]})):
        encoded = json.dumps(value)
        for secret in ("opaque-", "native diagnostic"):
            assert secret not in encoded
    assert public_torrent(row)["error"]["category"] == "unmapped_provider_error"
