"""Gate A — provider-neutral file-selection capability, identity and policy contracts.

Covers the capability/contract matrix, neutral manifest/entry identity and
normalization, early-manifest validation, the pure gate decision, executable
manifest reconciliation, and the injectable-clock source rule. No wall clock and
no real timing appear anywhere in this module or in the code it exercises.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from fake_integrations import MemoryExecutor, ParcelProvider
from transfers import file_selection as fs
from transfers.models import (
    Capability, ExecutionRequest, FileManifest, FileManifestEntry, IntegrationDescriptor,
    ProviderObservation, ProviderResource, ResourceState, SourceEntry, TransferCandidate,
    TransferRequest,
)
from transfers.registry import IntegrationRegistry

MODULE = Path(fs.__file__)


# --------------------------------------------------------------------------- #
# Capability / contract
# --------------------------------------------------------------------------- #

def test_file_manifest_provider_with_resource_lookup_registers():
    registry = IntegrationRegistry()
    registry.register_provider(ParcelProvider(file_manifest=True))
    provider = next(iter(registry.providers.values()))
    assert Capability.FILE_MANIFEST in provider.descriptor.capabilities


def test_file_manifest_provider_without_observation_contract_is_rejected():
    class ManifestOnly:
        descriptor = IntegrationDescriptor(
            "manifest-only", "Manifest only",
            frozenset({Capability.RESOLVE, Capability.FILE_MANIFEST}),
            request_types=frozenset({"parcel"}),
        )
        applicability = ParcelProvider().applicability

        async def resolve(self, request):  # pragma: no cover - contract shape only
            raise AssertionError

    with pytest.raises(TypeError):
        IntegrationRegistry().register_provider(ManifestOnly())


def test_capability_is_neutral_and_independent_of_metadata():
    # The existing executable/routable manifest contract is unchanged: a provider
    # may declare METADATA without FILE_MANIFEST and vice versa.
    plain = ParcelProvider()
    assert Capability.METADATA in plain.descriptor.capabilities
    assert Capability.FILE_MANIFEST not in plain.descriptor.capabilities


def test_executor_contract_has_no_selection_vocabulary():
    registry = IntegrationRegistry()
    registry.register_executor(MemoryExecutor(lambda *a, **k: True))
    for field in ExecutionRequest.__dataclass_fields__:
        assert "select" not in field and "manifest" not in field
    for field in TransferCandidate.__dataclass_fields__:
        assert "select" not in field and "manifest" not in field


def test_universal_policy_module_carries_no_native_vocabulary():
    source = MODULE.read_text()
    lowered = source.casefold()
    for token in ("alldebrid", "realdebrid", "real-debrid", "aria2", "statuscode",
                  "general_http", "magnet/files"):
        assert token not in lowered
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("providers.", "executors.", "postprocessors."))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in {"alldebrid", "aria2", "statusCode", "MAGNET_INVALID_ID", "LINK_DOWN"}


def test_policy_module_sources_no_wall_clock():
    source = MODULE.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(alias.name.split(".")[0] in {"time", "datetime"} for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in {"time", "datetime"}
    for banned in ("time.time(", "time.monotonic(", "datetime.now(", "datetime.utcnow(", "utcnow"):
        assert banned not in source


# --------------------------------------------------------------------------- #
# Manifest normalization + identity
# --------------------------------------------------------------------------- #

def canon(resource_id, *entries):
    return fs.canonicalize_manifest(resource_id, FileManifest(tuple(
        FileManifestEntry(name, path, size) for name, path, size in entries
    )))


def test_single_and_multi_file_manifests_canonicalize():
    single = canon("res", ("only.bin", "only.bin", 10))
    assert single.file_count == 1 and single.total_bytes == 10
    multi = canon("res", ("a", "d/a", 1), ("b", "d/b", 2), ("c", "c", 0))
    assert multi.file_count == 3
    assert [e.relative_path for e in multi.entries] == ["c", "d/a", "d/b"]


def test_unknown_size_is_permitted():
    manifest = canon("res", ("a", "a", 0), ("b", "b", 5))
    assert manifest.entries[0].expected_bytes == 0


@pytest.mark.parametrize("path", ["/abs/x", "../escape", "a/../b", "", "   "])
def test_unsafe_or_empty_paths_are_rejected(path):
    with pytest.raises(fs.ManifestInvalid):
        canon("res", ("x", path, 1))


def test_duplicate_and_sanitized_collision_paths_are_rejected():
    with pytest.raises(fs.ManifestInvalid):
        canon("res", ("a", "dir/file", 1), ("b", "dir/file", 2))
    with pytest.raises(fs.ManifestInvalid):
        # Both sanitize to the same destination component.
        canon("res", ("a", "na:me", 1), ("b", "na*me", 2))


def test_negative_size_is_rejected():
    with pytest.raises(fs.ManifestInvalid):
        canon("res", ("a", "a", -1))


def test_provider_reordering_alone_does_not_change_identity():
    forward = canon("res", ("a", "s/a", 1), ("b", "s/b", 2), ("c", "s/c", 3))
    reverse = canon("res", ("c", "s/c", 3), ("b", "s/b", 2), ("a", "s/a", 1))
    assert forward.manifest_id == reverse.manifest_id
    assert forward.manifest_digest == reverse.manifest_digest
    assert forward.entry_ids() == reverse.entry_ids()


def test_size_change_is_a_new_manifest_version_but_keeps_entry_path_identity():
    before = canon("res", ("a", "s/a", 1), ("b", "s/b", 2))
    after = canon("res", ("a", "s/a", 999), ("b", "s/b", 2))
    assert before.manifest_id != after.manifest_id
    entry_before = {e.relative_path: e.entry_id for e in before.entries}
    entry_after = {e.relative_path: e.entry_id for e in after.entries}
    assert entry_before == entry_after  # logical pathname identity is stable


def test_entry_identity_is_path_derived_and_size_independent():
    manifest = canon("res-x", ("a", "s/a", 7))
    assert manifest.entries[0].entry_id == fs.entry_identity("res-x", "s/a")


def test_different_resource_yields_different_identity():
    assert canon("res-1", ("a", "a", 1)).manifest_id != canon("res-2", ("a", "a", 1)).manifest_id


def test_empty_manifest_and_missing_resource_are_rejected():
    with pytest.raises(fs.ManifestInvalid):
        fs.canonicalize_manifest("res", FileManifest(()))
    with pytest.raises(fs.ManifestInvalid):
        fs.canonicalize_manifest("", FileManifest((FileManifestEntry("a", "a", 1),)))


def test_bounded_entry_count():
    huge = FileManifest(tuple(FileManifestEntry(f"f{i}", f"f{i}", 1) for i in range(fs.MAX_MANIFEST_ENTRIES + 1)))
    with pytest.raises(fs.ManifestInvalid):
        fs.canonicalize_manifest("res", huge)


# --------------------------------------------------------------------------- #
# Confirm-request validation
# --------------------------------------------------------------------------- #

def test_validate_selection_ids_orders_by_manifest_and_rejects_bad_input():
    manifest = canon("res", ("a", "s/a", 1), ("b", "s/b", 2), ("c", "s/c", 3))
    ids = [e.entry_id for e in manifest.entries]
    ordered = fs.validate_selection_ids(manifest, [ids[2], ids[0]])
    assert ordered == (ids[0], ids[2])
    with pytest.raises(fs.ManifestInvalid):
        fs.validate_selection_ids(manifest, [])
    with pytest.raises(fs.ManifestInvalid):
        fs.validate_selection_ids(manifest, [ids[0], ids[0]])
    with pytest.raises(fs.ManifestInvalid):
        fs.validate_selection_ids(manifest, ["not-a-real-entry"])


# --------------------------------------------------------------------------- #
# Pure gate decision
# --------------------------------------------------------------------------- #

def state(**kw):
    base = dict(
        decision="pending", initially_available=True, manifest_wait_until=1060.0,
        hold_until=None, manifest_id=None, manifest_file_count=0,
        manifest_committed_at=None, auto_offer_dismissed_at=None,
    )
    base.update(kw)
    return fs.SelectionWindowState(**base)


def test_gate_cached_waits_for_manifest_then_times_out_to_all():
    assert fs.evaluate_gate(state(), now=1000.0).gate == fs.SelectionGate.WAIT_FOR_MANIFEST
    late = fs.evaluate_gate(state(), now=1060.0)
    assert late.gate == fs.SelectionGate.PROCEED
    assert late.resolve_decision == fs.SelectionDecision.ALL
    assert late.resolve_reason == fs.DecisionReason.MANIFEST_TIMEOUT


def test_gate_single_file_proceeds_immediately_as_all():
    result = fs.evaluate_gate(state(manifest_id="m", manifest_file_count=1), now=1001.0)
    assert result.gate == fs.SelectionGate.PROCEED
    assert result.resolve_reason == fs.DecisionReason.SINGLE_FILE


def test_gate_cached_multi_file_holds_until_deadline_then_times_out():
    holding = state(manifest_id="m", manifest_file_count=4, hold_until=1125.0)
    assert fs.evaluate_gate(holding, now=1100.0).gate == fs.SelectionGate.WAIT_FOR_DECISION
    expired = fs.evaluate_gate(holding, now=1125.0)
    assert expired.gate == fs.SelectionGate.PROCEED
    assert expired.resolve_reason == fs.DecisionReason.DECISION_TIMEOUT


def test_gate_uncached_never_holds_locally():
    result = fs.evaluate_gate(
        state(initially_available=False, manifest_id="m", manifest_file_count=4, hold_until=None),
        now=1005.0,
    )
    assert result.gate == fs.SelectionGate.PROCEED


def test_gate_respects_settled_and_committed_facts():
    assert fs.evaluate_gate(state(decision="explicit"), now=1.0).gate == fs.SelectionGate.PROCEED
    assert fs.evaluate_gate(state(decision="all"), now=1.0).gate == fs.SelectionGate.PROCEED
    assert fs.evaluate_gate(
        state(manifest_committed_at=999.0, manifest_id="m", manifest_file_count=9), now=1.0,
    ).gate == fs.SelectionGate.PROCEED


def test_auto_offer_active_window_and_hold_semantics():
    within = state(manifest_id="m", manifest_file_count=3)
    assert fs.auto_offer_active(within, now=1030.0) is True
    assert fs.auto_offer_active(within, now=1075.0) is False   # past 60s, no hold
    held = state(manifest_id="m", manifest_file_count=3, hold_until=1125.0)
    assert fs.auto_offer_active(held, now=1075.0) is True       # active cached hold recoverable
    dismissed = state(manifest_id="m", manifest_file_count=3, auto_offer_dismissed_at=1010.0)
    assert fs.auto_offer_active(dismissed, now=1020.0) is False
    single = state(manifest_id="m", manifest_file_count=1)
    assert fs.auto_offer_active(single, now=1010.0) is False


# --------------------------------------------------------------------------- #
# Executable manifest reconciliation
# --------------------------------------------------------------------------- #

def src(name, path, size):
    return SourceEntry(name, size, path, TransferRequest("parcel-member", f"m:{path}", name=name))


def test_reconcile_matches_subset_and_ignores_unselected_files():
    executable = (src("a", "s/a", 10), src("b", "s/b", 20), src("c", "s/c", 30))
    proven = fs.reconcile_executable_subset([("s/a", 10), ("s/c", 30)], executable)
    assert [e.relative_path for e in proven] == ["s/a", "s/c"]


def test_reconcile_fails_closed_on_missing_selected_path():
    with pytest.raises(fs.SelectionUnprovable):
        fs.reconcile_executable_subset([("s/a", 10), ("s/gone", 5)], (src("a", "s/a", 10),))


def test_reconcile_fails_closed_on_known_size_conflict():
    with pytest.raises(fs.SelectionUnprovable):
        fs.reconcile_executable_subset([("s/a", 10)], (src("a", "s/a", 11),))


def test_reconcile_accepts_unknown_early_size_against_known_late_size():
    proven = fs.reconcile_executable_subset([("s/a", 0)], (src("a", "s/a", 12345),))
    assert proven[0].expected_bytes == 12345


def test_reconcile_fails_closed_on_duplicate_executable_path():
    with pytest.raises(fs.SelectionUnprovable):
        fs.reconcile_executable_subset([("s/a", 10)], (src("a", "s/a", 10), src("a2", "s/a", 10)))


def test_reconcile_never_returns_the_full_list_as_a_fallback():
    executable = (src("a", "s/a", 10), src("b", "s/b", 20))
    with pytest.raises(fs.SelectionUnprovable):
        fs.reconcile_executable_subset([("s/a", 99)], executable)


# --------------------------------------------------------------------------- #
# Observation carries the neutral manifest only as facts
# --------------------------------------------------------------------------- #

def test_provider_observation_manifest_is_pure_facts():
    manifest = FileManifest((FileManifestEntry("a.mkv", "Season 1/a.mkv", 10),))
    observation = ProviderObservation(
        ProviderResource("parcel-lab", {}, id="r"), ResourceState.AVAILABLE, file_manifest=manifest,
    )
    for entry in observation.file_manifest.entries:
        assert set(type(entry).__dataclass_fields__) == {"name", "relative_path", "expected_bytes"}
