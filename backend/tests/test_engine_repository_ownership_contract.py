"""Method-level ownership contract for the transfer engine and repository stacks.

The stacks are inheritance chains (``convergence_engine`` -> ``engine`` ->
``_engine_recovery`` -> ``_engine_base`` and ``recovery_repository`` ->
``manual_repository`` -> ``presentation_repository`` -> ``repository`` ->
``_repository_base``). A chain is only sound when correctness never depends on
the ORDER in which layers override each other. This contract computes, from the
source, every method defined by more than one class in a stack and admits only
two legitimate shapes:

* a specialization that extends the inherited behavior and says so with
  ``super().<method>()`` -- exactly one such wrapper per method, so there is no
  successive-correction stack; or
* a concrete implementation of an explicit abstract hook (the inherited body
  is just ``raise NotImplementedError``).

A full replacement of a concrete inherited body (a masked, dead layer), or two
wrappers stacked on one method, fails. Adding a new overriding layer therefore
requires an entry in the register below with its semantic owner and reason.
"""
import ast
import inspect
import sys

from transfers.convergence_engine import TransferEngine
from transfers.recovery_repository import TransferRepository

# (defining module, method) -> the one semantic reason the layer exists.
SPECIALIZATIONS = {
    # engine stack
    ("convergence_engine", "initialize"): "snapshot artifacts active at startup for STARTUP_RECONCILE",
    ("convergence_engine", "_dispatch"): "route pre-execution readiness failures into canonical recovery",
    ("convergence_engine", "reconcile_executions"): "startup reconcile + quiescent-recovery wake before the base sweep",
    ("convergence_engine", "_process_executions"): "provider/executor/materialization admission guard per artifact",
    ("engine", "_request_failure"): "project context-free compatibility facts onto factual integration errors",
    ("_engine_recovery", "resolve_pending"): "collection-affinity serialization and post-resolution aggregation",
    ("_engine_recovery", "_process_request"): "collection-affinity precondition",
    ("_engine_recovery", "_materialize"): "cohort lock, collection coordination, candidate provenance",
    ("_engine_recovery", "_execution_result"): "refine an unknown expected size from a verified final total",
    # repository stack
    ("presentation_repository", "presentation"): "the one assembled read model over the base rows",
}
# Concrete implementations of abstract hooks declared in the base.
HOOK_IMPLEMENTATIONS = {
    ("engine", "_resolve"),
    ("engine", "_observe_resource"),
}


def _module(cls) -> str:
    return cls.__module__.split(".")[-1]


def _definitions(top):
    """method name -> [(class, ast node)] for every method defined in more than one class of the stack."""
    mro = [cls for cls in top.__mro__ if cls is not object]
    found: dict[str, list] = {}
    for cls in mro:
        tree = ast.parse(inspect.getsource(sys.modules[cls.__module__]))
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls.__name__)
        for member in node.body:
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.setdefault(member.name, []).append((cls, member))
    return {name: defs for name, defs in found.items() if len(defs) > 1}


def _calls_super(node, name) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == name
        and isinstance(n.func.value, ast.Call) and getattr(n.func.value.func, "id", None) == "super"
        for n in ast.walk(node)
    )


def _is_abstract_hook(node) -> bool:
    body = [n for n in node.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    return len(body) == 1 and isinstance(body[0], ast.Raise) and "NotImplementedError" in ast.dump(body[0])


def _violations(top):
    problems = []
    for name, defs in _definitions(top).items():
        overriding, base = defs[:-1], defs[-1]
        base_cls, base_node = base
        if _is_abstract_hook(base_node):
            for cls, _node in overriding:
                if (_module(cls), name) not in HOOK_IMPLEMENTATIONS:
                    problems.append(f"{_module(cls)}.{name}: implements an unregistered abstract hook")
            if len(overriding) != 1:
                problems.append(f"{name}: an abstract hook must have exactly one implementation")
            continue
        if len(overriding) != 1:
            problems.append(f"{name}: {len(overriding)} stacked overrides "
                            f"({', '.join(_module(c) for c, _ in overriding)}) -- flatten into one owner")
            continue
        cls, node = overriding[0]
        if not _calls_super(node, name):
            problems.append(f"{_module(cls)}.{name}: fully replaces a concrete inherited body "
                            f"({_module(base_cls)}); the masked layer must be deleted or the owner moved")
        if (_module(cls), name) not in SPECIALIZATIONS:
            problems.append(f"{_module(cls)}.{name}: unregistered specialization")
    return problems


def test_engine_and_repository_stacks_have_no_superseded_override_layer():
    assert _violations(TransferEngine) == []
    assert _violations(TransferRepository) == []


def test_every_registered_specialization_and_hook_still_exists():
    live = {(_module(defs[0][0]), name) for stack in (TransferEngine, TransferRepository)
            for name, defs in _definitions(stack).items()}
    assert set(SPECIALIZATIONS) | HOOK_IMPLEMENTATIONS <= live


def test_presentation_has_one_assembling_owner_above_the_base_read_model():
    owners = [_module(cls) for cls, _ in _definitions(TransferRepository)["presentation"]]
    assert owners == ["presentation_repository", "_repository_base"]
    # The candidate overlay is a named step of that owner, not a second override.
    from transfers.repository import TransferRepository as RepositoryLayer

    assert "presentation" not in RepositoryLayer.__dict__
    assert "_overlay_candidate_presentation" in RepositoryLayer.__dict__


def test_recovery_state_methods_are_defined_exactly_once():
    """A recovery-state operation has one implementation, so its behavior can
    never depend on which layer of the repository stack a caller instantiates."""
    from transfers import _repository_base, manual_repository, recovery_repository, repository

    for name in ("_recovery_snapshot", "reset_retry_budget", "record_source_failure",
                 "consume_recovery_refresh", "reset_source_recovery", "execution",
                 "execution_idle_seconds", "bound_route_provider"):
        owners = [m.__name__ for m in (recovery_repository, manual_repository, repository, _repository_base)
                  if name in getattr(m, "TransferRepository").__dict__]
        assert len(owners) == 1, (name, owners)


def test_snapshot_reader_seeds_the_claim_and_fence_fields_at_the_one_reader():
    from transfers.repository import _RECOVERY_SNAPSHOT_DEFAULTS

    assert {"recovery_generation", "recovery_claim_token", "blocked_retry_at"} <= set(_RECOVERY_SNAPSHOT_DEFAULTS)
    source = inspect.getsource(TransferRepository._recovery_snapshot)
    assert "**_RECOVERY_SNAPSHOT_DEFAULTS" in source
    assert 'max(3, int(snapshot.get("version") or 0))' in source


# -- cleanup-claim and selection-generation ownership ------------------------------
#
# Same-object resubmission correction: exactly ONE owner for the provider-cleanup
# claim (a lease/token in ``_repository_base``, driven by ONE engine cadence) and
# exactly ONE owner for the file-selection generation guarantee
# (``TransferRepository.ensure_selection_generation``, reached through the ONE
# engine entry ``_secure_root_selection``). These are source-level contracts so a
# resurrected duplicate helper, a second writer, or a request-kind special case
# fails here instead of resurfacing as a lifecycle bug.

import re
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
_CLEANUP_STATE_WRITERS = {"cleanup_intent", "claim_cleanup", "renew_cleanup_claim", "cleanup_complete", "cleanup_retry"}


def _production_files():
    for path in sorted(_BACKEND.rglob("*.py")):
        rel = path.relative_to(_BACKEND).as_posix()
        if rel.startswith(("tests/", ".venv/")) or "/__pycache__/" in rel:
            continue
        yield rel, path


def _functions(tree):
    """(qualified function name, node) for every function, innermost owner first."""
    def walk(node, owner):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield child.name, child
                yield from walk(child, child.name)
            else:
                yield from walk(child, owner)
    yield from walk(tree, None)


def _string_owners(pattern):
    """{(relative file, enclosing function)} for every string literal matching ``pattern``."""
    found = set()
    regex = re.compile(pattern, re.S)
    for rel, path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name, fn in _functions(tree):
            for node in ast.walk(fn):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and regex.search(node.value):
                    found.add((rel, name))
    return found


def _call_owners(attr):
    found = set()
    for rel, path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name, fn in _functions(tree):
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == attr):
                    found.add((rel, name))
    return found


def _defined(name):
    return {rel for rel, path in _production_files()
            if any(fn == name for fn, _ in _functions(ast.parse(path.read_text(encoding="utf-8"))))}


def test_provider_cleanup_state_has_exactly_one_writer_set_in_the_repository_base():
    writers = _string_owners(
        r"UPDATE provider_resources SET[^;]*\b(cleanup_claim_token|cleanup_claim_until|cleanup_authority|"
        r"cleanup_abandoned|cleanup_retry_at|cleanup_attempts|cleanup_blocked)\b")
    # The repository owns every live transition. The single other writer is the one-way
    # upgrade step that zeroes the RETIRED boolean marker; it never touches a lease.
    assert writers == {("transfers/_repository_base.py", name) for name in _CLEANUP_STATE_WRITERS} | {
        ("db/database.py", "_normalize_legacy_cleanup_claims")}


def test_the_cleanup_claim_is_driven_by_exactly_one_engine_cadence():
    for attr in ("claim_cleanup", "cleanup_complete", "cleanup_retry"):
        assert _call_owners(attr) == {("transfers/_engine_base.py", "_cleanup_pending")}, attr
    # Lease renewal is part of the same owner: only the heartbeat that lives and dies with
    # the one provider call may renew, and only the cadence starts that call.
    assert _call_owners("renew_cleanup_claim") == {("transfers/_engine_base.py", "_hold_cleanup_lease")}
    assert _call_owners("_run_owned_cleanup") == {("transfers/_engine_base.py", "_cleanup_pending")}
    assert _call_owners("_hold_cleanup_lease") == {("transfers/_engine_base.py", "_run_owned_cleanup")}
    # No second cleanup scheduler/loop: the cadence is only ever entered by the engine's own drains.
    assert {rel for rel, _ in _call_owners("pending_cleanup")} == {"transfers/_engine_base.py"}


def test_the_retired_boolean_claim_marker_is_only_ever_normalized_never_used():
    assert {rel for rel, _ in _string_owners(r"\bcleanup_blocked\b")} == {"db/database.py"}


def test_superseded_cleanup_selection_and_adoption_helpers_are_physically_removed():
    for name in ("reclaim_stale_cleanup_claims", "attach_inventory", "_after_resolution_persisted",
                 "selection_generation_exists", "transfer_has_selection_generation"):
        assert _defined(name) == set(), name
        assert _call_owners(name) == set(), name


def test_one_owner_creates_the_selection_generation_and_one_engine_entry_reaches_it():
    assert _call_owners("begin_file_selection_window") == {("transfers/repository.py", "ensure_selection_generation")}
    assert _call_owners("ensure_selection_generation") == {("transfers/_engine_base.py", "_secure_root_selection")}
    assert _call_owners("_secure_root_selection") == {
        ("transfers/_engine_base.py", "_apply_resolution"),
        ("transfers/_engine_base.py", "reconcile_inventory"),
        ("transfers/engine.py", "_observe_resource"),           # the fail-closed materialization boundary
    }
    assert _defined("_secure_root_selection") == {"transfers/_engine_base.py"}


def test_the_interactive_policy_is_read_in_one_place_and_the_engine_never_reads_it():
    readers = _string_owners(r"^interactive$") | {
        (rel, name) for rel, path in _production_files()
        for name, fn in _functions(ast.parse(path.read_text(encoding="utf-8")))
        for node in ast.walk(fn)
        if isinstance(node, ast.Attribute) and node.attr == "SELECTION_MODE_INTERACTIVE"}
    assert {rel for rel, _ in readers} <= {"transfers/repository.py", "transfers/file_selection.py",
                                            "transfers/models.py", "api/routes.py", "application/service.py"}
    repo_readers = {name for rel, name in readers if rel == "transfers/repository.py"}
    assert repo_readers == {"_selection_required"}
    for engine_file in ("transfers/_engine_base.py", "transfers/engine.py"):
        tree = ast.parse((_BACKEND / engine_file).read_text(encoding="utf-8"))
        touched = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert not touched & {"selection_mode", "SELECTION_MODE_INTERACTIVE", "SELECTION_MODE_ALL"}, engine_file


_KIND_LITERALS = {"magnet", "torrent", "torrent_file"}
_KIND_CONSTANTS = {"BITTORRENT_REQUEST_KINDS", "TORRENT_FILE_REQUEST_KINDS"}


def _request_kind_branches(node):
    """Request-kind special-casing: a literal magnet/torrent kind or the BitTorrent
    kind-set constants, as code (never as prose in a docstring)."""
    hits = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and child.value in _KIND_LITERALS:
            hits.append(child.value)
        elif isinstance(child, ast.Name) and child.id in _KIND_CONSTANTS:
            hits.append(child.id)
    return hits


def test_no_request_kind_special_case_in_the_cleanup_selection_or_adoption_owners():
    """Torrent/magnet is the reproducer, never the abstraction boundary: only the
    real capability (``Capability.FILE_MANIFEST``) may condition the guarantee."""
    for rel in ("transfers/_engine_base.py", "transfers/engine.py", "transfers/repository.py"):
        tree = ast.parse((_BACKEND / rel).read_text(encoding="utf-8"))
        assert _request_kind_branches(tree) == [], rel
    owners = _CLEANUP_STATE_WRITERS | {"_predecessor_cleanup_blocks", "predecessor_cleanup_barrier",
                                       "pending_cleanup", "adopt_inventory_resource"}
    tree = ast.parse((_BACKEND / "transfers/_repository_base.py").read_text(encoding="utf-8"))
    checked = set()
    for name, fn in _functions(tree):
        if name in owners:
            checked.add(name)
            assert _request_kind_branches(fn) == [], name
    assert checked == owners
    engine_base = ast.parse((_BACKEND / "transfers/_engine_base.py").read_text(encoding="utf-8"))
    guard = next(fn for name, fn in _functions(engine_base) if name == "_file_manifest_root")
    assert any(isinstance(n, ast.Attribute) and n.attr == "FILE_MANIFEST" for n in ast.walk(guard))


def test_the_selection_guard_is_capability_conditioned_and_fails_closed_at_the_boundary():
    engine = (_BACKEND / "transfers/engine.py").read_text(encoding="utf-8")
    body = engine[engine.index("async def _observe_resource"):]
    guard, manifest = body.index("_secure_root_selection"), body.index("provider.manifest(")
    assert guard < manifest                                           # decided BEFORE any manifest can expand
    assert "authority.held" in body[guard:manifest]                   # and a HOLD never falls through to ALL


# -- the detector itself -------------------------------------------------------

class _Base:
    def wrapped(self):
        return 1

    def replaced(self):
        return 1

    def stacked(self):
        return 1

    def hook(self):
        raise NotImplementedError("hook")


class _Middle(_Base):
    def stacked(self):
        return super().stacked() + 1


class _Top(_Middle):
    def wrapped(self):
        return super().wrapped() + 1

    def replaced(self):
        return 2

    def stacked(self):
        return super().stacked() + 1

    def hook(self):
        return 3


def test_detector_flags_replacement_stacking_and_unregistered_layers():
    problems = "\n".join(_violations(_Top))
    assert "replaced: fully replaces a concrete inherited body" in problems
    assert "stacked: 2 stacked overrides" in problems
    assert "wrapped: unregistered specialization" in problems
    assert "hook: implements an unregistered abstract hook" in problems


def test_lower_engine_stack_owns_no_candidate_activation():
    """Workstream B: candidate activation has exactly one command owner
    (``convergence_engine.TransferEngine``) and one mutation
    (``candidate_activation.activate_candidate``, which requires a real
    recovery claim). No layer beneath the final engine defines, imports, or
    names an alternate activation mode. This verifies the audited stack; it
    does not reorder or flatten it."""
    from transfers import _engine_base, _engine_recovery, candidate_activation, convergence_engine, engine, manual_failover

    lower = (_engine_base.TransferEngine, _engine_recovery.TransferEngine, engine.TransferEngine)
    for cls in lower:
        for name in ("activate_candidate", "activate_candidate_command", "_activate_alternate"):
            assert name not in cls.__dict__, f"{cls.__module__}.{name}: second candidate-activation owner"
    owners = [_module(cls) for cls in TransferEngine.__mro__ if "activate_candidate_command" in cls.__dict__]
    assert owners == ["convergence_engine"]

    for module in (_engine_base, _engine_recovery, engine, manual_failover):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # Read-only helpers (resolve_candidate_index) may be imported; the mutation may not.
                assert not any(alias.name == "activate_candidate" for alias in node.names), module.__name__
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "activate_candidate":
                raise AssertionError(f"{module.__name__} calls activate_candidate directly")

    # The operator path reaches the one mutation only through the engine's command.
    assert "activate_candidate_command(" in inspect.getsource(manual_failover)
    assert "activate_candidate(" in inspect.getsource(convergence_engine.TransferEngine._apply_recovery_decision)
    assert "activate_candidate(" in inspect.getsource(convergence_engine.TransferEngine.activate_candidate_command)
    assert candidate_activation.activate_candidate.__module__ == "transfers.candidate_activation"
