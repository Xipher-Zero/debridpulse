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


def test_production_engine_and_repository_are_the_composed_classes():
    """The composed production classes, by identity. The hierarchy itself is not
    frozen for its own sake: what protects ownership is
    ``test_engine_and_repository_stacks_have_no_superseded_override_layer``
    (transfers ownership contract), which fails on any layer whose behavior
    depends on override order."""
    from transfers.convergence_engine import TransferEngine
    from transfers.recovery_repository import TransferRepository
    from application import composition

    assert composition.TransferEngine is TransferEngine
    assert composition.TransferRepository is TransferRepository
    for cls in (TransferEngine, TransferRepository):
        retired = [c.__module__ for c in cls.__mro__ if c.__module__.split(".")[-1].startswith(
            ("_convergence_phase3", "_recovery_repository_"))]
        assert not retired, retired


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


# --------------------------------------------------------------------------- #
# DP 1.0.12 canonical architecture correction (Workstreams A/B/C) guardrails.
# Section 13.1: static architecture assertions preventing regression back to
# provider/executor-specific policy leaking into universal core modules.
# --------------------------------------------------------------------------- #

_ARIA2_NATIVE_KEYS = (
    "min-split-size", "max-connection-per-server", "disk-cache", "file-allocation",
    "max-overall-download-limit", "max-overall-upload-limit", "max-concurrent-downloads",
    "lowest-speed-limit",
)
# transfers/mirrors.py's own EvidenceKind member name legitimately contains
# "split" nowhere, but "split" alone is too common a substring (file
# splitting, string.split, etc.) to check standalone; native aria2 "split" is
# only ever meaningful alongside these more specific sibling keys, which are
# the ones a leaking native option dict would actually carry.


def test_universal_transfer_core_carries_no_aria2_native_option_keys():
    """Specification section 2.5, 13.1: universal policy must not depend on
    aria2-native option names after migration; only the aria2 implementation
    may map neutral concepts to them internally."""
    for path in (ROOT / "transfers").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for key in _ARIA2_NATIVE_KEYS:
            assert key not in source, f"{path.relative_to(ROOT)} references native aria2 option {key!r}"


def test_canonical_equivalence_has_no_provider_specific_branch():
    """Specification section 2.2, 13.1: canonical equivalence must not branch
    on provider_id == 'alldebrid' -- resolver-attested evidence (Workstream B)
    is provider-neutral in the transfer model."""
    source = (ROOT / "transfers/mirrors.py").read_text(encoding="utf-8")
    assert "alldebrid" not in source.casefold()


def test_no_provider_specific_branch_anywhere_in_universal_transfer_core():
    for path in (ROOT / "transfers").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "alldebrid" not in source.casefold(), f"{path.relative_to(ROOT)} references a concrete provider"
        assert "general_http" not in source.casefold(), f"{path.relative_to(ROOT)} references a concrete provider"


def test_recovery_and_convergence_do_not_branch_on_file_selection_implementation_types():
    """Specification section 2.2, 11: recovery may consume the neutral
    PROCEED/HOLD/STALE admission decision; it must not import or branch on
    file-selection's own implementation types to decide admission."""
    for name in ("convergence_engine.py", "_engine_recovery.py"):
        source = (ROOT / "transfers" / name).read_text(encoding="utf-8")
        assert "import file_selection" not in source
        assert "from transfers import file_selection" not in source
        assert "from transfers.file_selection" not in source


def test_materialization_admission_is_the_sole_repository_owned_decision_type():
    """Specification section 7.1, 7.2: one neutral admission result type,
    reused (not re-invented) by every dispatch entry point."""
    for name in ("_engine_base.py", "convergence_engine.py"):
        source = (ROOT / "transfers" / name).read_text(encoding="utf-8")
        assert "materialization_authorization" in source
    # No competing transfer-global mutable authorization flag was introduced
    # (docstrings may name the forbidden pattern to explain why it is absent).
    for path in (ROOT / "transfers").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "selection_authorized =" not in source
        assert "selection_authorized=" not in source


def test_bandwidth_and_tuning_routes_never_acquire_application_wide_maintenance():
    """Specification section 2.6, 6, 9.6: executor-local/runtime-limit
    mutations use only the ordinary application-operation admission, never
    ``ApplicationMaintenanceGate`` merely because they are persisted."""
    source = (ROOT / "api/routes.py").read_text(encoding="utf-8")
    for name in (
        "aria2_set_global_options", "patch_execution_runtime_limits",
        "patch_transfer_policy", "patch_integration_configuration",
    ):
        start = source.index(f"async def {name}(")
        end = source.index("\n\n@router.", start)
        body = source[start:end]
        assert "application.application_operation()" in body
        assert "application.configuration_admission()" not in body


def test_aria2_runtime_builds_native_options_from_canonical_namespaces_only():
    """Specification sections 4.3, 9.1, 9.3: the native aria2 global-option
    dict is rebuilt only from injected typed configuration -- the
    ``integrations.aria2`` options, the universal ``transfer_policy``
    concurrency and the neutral ``execution_runtime_limits`` cap -- never from
    flat ``AppSettings.aria2_*`` fields and never from a global settings
    lookup. The former ``get_settings()``-defaulting convenience wrappers had
    no production caller and are retired."""
    source = (ROOT / "executors/aria2/runtime.py").read_text(encoding="utf-8")
    for retired in ("def aria2_global_options(", "def is_builtin_mode(", "def builtin_rpc_url("):
        assert retired not in source, f"{retired!r} is a retired global-settings wrapper"
    assert "get_settings" not in source
    start = source.index("def build_aria2_global_options(")
    end = source.index("\ndef ", start)
    body = source[start:end]
    for forbidden in (
        "aria2_split", "aria2_min_split_size", "aria2_max_connection_per_server",
        "aria2_disk_cache", "aria2_file_allocation", "aria2_continue_downloads",
        "aria2_lowest_speed_limit", "aria2_max_active_downloads", "aria2_max_download_limit",
        "aria2_max_download_result", "aria2_keep_unfinished_download_result", "aria2_max_upload_limit",
    ):
        assert forbidden not in body, f"build_aria2_global_options still reads flat field {forbidden!r}"
    assert "options.max_download_result" in body
    # Universal Executor Leveling: DP global concurrency and global bandwidth
    # have one core owner each and are never mirrored into native tuning.
    assert "max_concurrent_executions" not in body
    assert "max_download_bytes_per_second" not in body
    assert "max-overall-download-limit" not in body
    assert "NATIVE_ACTIVE_DOWNLOADS" in body


def test_only_composition_and_migration_bind_flat_aria2_tuning_fields():
    """Specification section 9.1: after migration, only the one-way legacy
    translation path (``integrations.configuration.normalize_settings`` via
    ``executors.aria2.definition``'s auto-derived ``legacy_fields``) may still
    read the flat ``aria2_*`` fields; executor runtime/admin code must not
    (specification section 9.3) -- for tuning, lifecycle, or administration
    fields alike."""
    for name in ("runtime.py", "admin.py"):
        source = (ROOT / "executors/aria2" / name).read_text(encoding="utf-8")
        for forbidden in (
            "getattr(cfg, \"aria2_split\"", "getattr(cfg, \"aria2_min_split_size\"",
            "getattr(cfg, \"aria2_max_connection_per_server\"", "getattr(cfg, \"aria2_disk_cache\"",
            "getattr(cfg, \"aria2_file_allocation\"", "getattr(cfg, \"aria2_continue_downloads\"",
            "getattr(cfg, \"aria2_lowest_speed_limit\"", "getattr(cfg, \"aria2_max_active_downloads\"",
            "getattr(cfg, \"aria2_purge_interval_minutes\"", "getattr(cfg, \"aria2_restart_interval_hours\"",
            "getattr(cfg, \"aria2_max_download_result\"", "getattr(cfg, \"aria2_keep_unfinished_download_result\"",
            "getattr(cfg, \"aria2_operation_timeout_seconds\"", "cfg.aria2_purge_interval_minutes",
            "cfg.aria2_restart_interval_hours", "get_settings().aria2_operation_timeout_seconds",
        ):
            assert forbidden not in source, f"{name} still rebuilds native tuning from flat field ({forbidden!r})"


def test_runtime_and_administration_never_call_get_settings():
    """Gate 9 revision-2 rejection finding, specification section 9.3:
    namespace canonicalization alone (flat fields -> ``_canonical_aria2_options()``)
    is not dependency inversion. ``Aria2Runtime`` and
    ``Aria2Administration`` -- the long-lived singletons -- must receive
    typed configuration through injection (``Aria2RuntimeConfiguration``,
    composed by ``application.composition.configure()``) and never call
    ``core.config.get_settings()`` themselves. The settings-boundary
    translation helper (``_canonical_aria2_options``) remains legitimate for
    genuine settings-boundary callers (API routes, composition itself) -- this test isolates the two runtime
    CLASSES specifically, not the whole module."""
    source = (ROOT / "executors/aria2/runtime.py").read_text(encoding="utf-8")
    class_start = source.index("class Aria2Runtime:")
    class_end = source.index("\nruntime = Aria2Runtime()", class_start)
    class_body = source[class_start:class_end]
    assert "get_settings" not in class_body
    assert "self._config" in class_body

    admin_source = (ROOT / "executors/aria2/admin.py").read_text(encoding="utf-8")
    assert "get_settings" not in admin_source
    assert "from core.config import" not in admin_source
    assert "self.runtime" in admin_source


def test_composition_is_the_sole_aria2_runtime_configuration_injection_point():
    """Universal Executor Leveling (supersedes specification section 9.3's
    composition-owned construction): the aria2 integration factory is the one
    place that builds typed ``Aria2RuntimeConfiguration`` from the aria2-owned
    namespace, injects it via ``runtime.configure()`` and constructs
    ``Aria2Administration`` -- reaching the application only through the
    generic integration lifecycle/administration seam. Central composition
    constructs no concrete executor runtime or administration."""
    factory = (ROOT / "executors/aria2/definition.py").read_text(encoding="utf-8")
    assert "Aria2RuntimeConfiguration(" in factory
    assert "runtime.configure(" in factory
    assert "Aria2Administration(" in factory
    source = (ROOT / "application/composition.py").read_text(encoding="utf-8")
    for concrete in ("Aria2RuntimeConfiguration", "Aria2Administration", "aria2_runtime", "executors.aria2"):
        assert concrete not in source
    assert "integration_surfaces(" in source
