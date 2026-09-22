"""Architecture proven by absence: no aria2-shaped executor model remains in core.

Every assertion here reads production source. A comment or docstring does not
count as proof; only the absence of the superseded owner does.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
import re

import pytest

BACKEND = Path(__file__).resolve().parents[1]
CORE_PACKAGES = ("transfers", "application", "core")
PRODUCTION_PACKAGES = ("transfers", "application", "core", "executors", "integrations", "api", "providers",
                       "services", "postprocessors", "db", "auth")


def _files(packages):
    for package in packages:
        for path in sorted((BACKEND / package).rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path
    if packages == PRODUCTION_PACKAGES:
        yield BACKEND / "main.py"


def _without_legacy_input_maps(path: Path) -> str:
    """Historical flat configuration KEY spellings (one-way migration input) are
    the only permitted occurrence of an executor name in core modules."""
    source = path.read_text()
    tree = ast.parse(source)
    lines = source.splitlines()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "LEGACY_INPUT_FIELDS"
                                                 for target in node.targets)):
            for index in range(node.lineno - 1, node.end_lineno):
                lines[index] = ""
    return "\n".join(lines)


def test_universal_core_contains_no_concrete_executor_name():
    offenders = []
    for path in _files(CORE_PACKAGES):
        text = _without_legacy_input_maps(path)
        if re.search(r"aria2|sabnzbd|\bsab\b|rsync", text, re.I):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []


def test_universal_core_contains_no_native_job_identity_names():
    offenders = []
    for path in _files(CORE_PACKAGES):
        if re.search(r"\bgids?\b|nzo_id|tellActive|tellWaiting|tellStopped|addUri", path.read_text(), re.I):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []


def test_universal_core_contains_no_native_runtime_keys():
    offenders = []
    for path in _files(CORE_PACKAGES):
        if re.search(r"max-overall-download-limit|max-concurrent-downloads", path.read_text()):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []


def test_no_scheme_based_executor_router_remains_anywhere():
    from transfers.models import IntegrationDescriptor
    from transfers.registry import IntegrationRegistry
    assert "schemes" not in IntegrationDescriptor.__dataclass_fields__
    for name in ("eligible_executors", "executor_for"):
        assert not hasattr(IntegrationRegistry, name)
    offenders = []
    for path in _files(PRODUCTION_PACKAGES):
        text = path.read_text()
        if re.search(r"descriptor\.schemes|eligible_executors|executor_for\(", text):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []


def test_evidence_sampling_and_continuation_route_through_subject_claims():
    from transfers import _engine_base, mirrors
    assert "executor_for_subject" in inspect.getsource(mirrors.shared_evidence)
    assert "executor_for_subject" in inspect.getsource(mirrors.self_evidence)
    assert "executor_for_subject" in inspect.getsource(_engine_base.TransferEngine._evidence_target)
    assert "executor_for_subject" in inspect.getsource(_engine_base.TransferEngine._continue_executor_input)


def test_core_does_not_reject_executable_subjects_for_missing_endpoints():
    from transfers import _engine_base
    source = inspect.getsource(_engine_base.TransferEngine._materialize)
    assert "candidate.endpoints" not in source


def test_universal_core_never_parses_opaque_executor_identity():
    offenders = []
    pattern = re.compile(r"\.(?:native|correlation)\s*(?:\[|\.get\(|\.keys\(|\.items\(|\.values\()")
    for path in _files(CORE_PACKAGES + ("api",)):
        if pattern.search(path.read_text()):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []


def test_old_and_new_executor_control_models_do_not_coexist():
    from transfers import contracts, models
    assert not {"PAUSE", "RESUME", "RECONCILE"} & set(models.Capability.__members__)
    assert "TRANSFERRING" not in models.ExecutionState.__members__
    assert not hasattr(models.ExecutionObservation, "occupies_slot")
    assert "paths" not in models.ExecutionObservation.__dataclass_fields__
    assert not hasattr(contracts, "BatchObservation")
    assert not hasattr(contracts.Executor, "observe")
    assert not hasattr(contracts.Executor, "resumable_paths")
    offenders = []
    for path in _files(PRODUCTION_PACKAGES):
        text = path.read_text()
        if re.search(r"resumable_paths|occupies_slot|BatchObservation|Capability\.(?:PAUSE|RESUME|RECONCILE)"
                     r"|ExecutionState\.TRANSFERRING|isinstance\([^)]*PauseResume", text):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []


def test_one_file_completion_helpers_are_owned_only_by_the_generalized_verifier():
    offenders = []
    for path in _files(PRODUCTION_PACKAGES):
        if path.relative_to(BACKEND).as_posix() == "transfers/filesystem.py":
            continue
        if re.search(r"\b(?:stable_payload|stable_material_size|retire_partial|payload_matches)\b", path.read_text()):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []
    from transfers import _engine_base
    assert "tuple(item.target for item in artifacts)" not in inspect.getsource(_engine_base)


def test_no_second_input_required_owner_is_introduced():
    offenders, tables = [], []
    for path in _files(PRODUCTION_PACKAGES):
        text = path.read_text()
        tables += re.findall(r"CREATE TABLE IF NOT EXISTS (\w*(?:challenge|secret|credential)\w*)", text)
        if path.relative_to(BACKEND).as_posix() == "transfers/input_required.py":
            continue
        if re.search(r"class \w*(?:Broker|ChallengeStore)\b", text):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []
    assert tables == ["transfer_input_challenges"]


def test_cancellation_acknowledgement_is_never_stop_truth():
    from transfers.contracts import Executor
    assert "ExecutionObservation" in str(inspect.signature(Executor.cancel).return_annotation)
    offenders = []
    for path in _files(CORE_PACKAGES):
        text = path.read_text()
        if re.search(r"(?:outcome|result|cancelled)\s*=\s*await\s+[\w.]+\.cancel\(", text):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []


def test_static_capability_is_never_runtime_availability_proof():
    from transfers import runtime_coordination
    source = inspect.getsource(runtime_coordination)
    assert "available_runtime_capabilities" in source and ".health()" in source


def test_central_composition_has_no_concrete_executor_branch():
    from application import composition
    source = inspect.getsource(composition)
    assert not re.search(r"aria2|Aria2|from executors", source)
    assert "integration_surfaces(" in source


def test_no_runtime_monkeypatch_or_proxy_owner_seams():
    offenders = []
    for path in _files(("transfers", "application", "core", "executors", "integrations")):
        text = path.read_text()
        if re.search(r"_orig_|_proxy_owner|setattr\(\s*(?:TransferEngine|IntegrationRegistry|routes|engine)\b", text):
            offenders.append(str(path.relative_to(BACKEND)))
        tree = ast.parse(text)
        imported = {alias.asname or alias.name.split(".")[0]
                    for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                            and target.value.id in imported and target.value.id[:1].isupper()):
                        offenders.append(f"{path.relative_to(BACKEND)}:{node.lineno}")
    assert offenders == []


@pytest.mark.parametrize("module", ["transfers._engine_base", "transfers.convergence_engine",
                                    "transfers.candidate_activation", "transfers.mirrors",
                                    "transfers._engine_recovery", "transfers.cohorts"])
def test_core_selects_executors_only_through_subject_claims(module):
    import importlib
    source = inspect.getsource(importlib.import_module(module))
    assert not re.search(r"registry\.executor_for\(|eligible_executors", source)
