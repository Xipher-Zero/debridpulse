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
    ("engine", "_after_resolution_persisted"),
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
