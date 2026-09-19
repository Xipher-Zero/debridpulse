"""``NormalizedError.recovery`` is a one-way, derived compatibility projection.

A field is only a compatibility boundary if nothing reads it back to decide
behavior. This proves that from three directions:

1. static  -- no production code reads ``.recovery`` / ``.operator_action_required``
   except the dataclass itself, and no emitter supplies ``recovery=`` to an error;
2. dynamic -- for the whole cross-product of canonical facts, setting ``recovery``
   (and ``operator_action_required``) to *any* value changes no policy decision;
3. derived -- ``compatibility_error`` output depends on canonical facts only, so a
   stamped value is a function of them and cannot carry independent information.
"""
import ast
import itertools
from dataclasses import replace
from pathlib import Path

from transfers.errors import Category, Domain, NormalizedError, Permanence, Recovery, Retryability, Stage
from transfers import policy as policy_module
from transfers.policy import (
    RecoveryContext, TransferPolicy, compatibility_error, failure_signature, recovery_action,
)

BACKEND = Path(__file__).resolve().parents[1]
PROJECTED = {"recovery", "operator_action_required"}
# The dataclass declares the projected fields and serializes every field
# generically; nothing else may touch them by attribute.
DECLARING_MODULE = BACKEND / "transfers" / "errors.py"


def production_modules():
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND)
        if rel.parts[0] in {"tests", "__pycache__"} or ".venv" in rel.parts:
            continue
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def test_no_production_code_reads_the_projected_fields_back():
    readers = []
    for path, tree in production_modules():
        if path == DECLARING_MODULE:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in PROJECTED and isinstance(node.ctx, ast.Load):
                readers.append(f"{path.relative_to(BACKEND)}:{node.lineno} .{node.attr}")
    assert readers == [], "a compatibility field is read to decide behavior:\n" + "\n".join(readers)


def test_no_emitter_supplies_a_recovery_to_an_error():
    supplied = []
    for path, tree in production_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "recovery":
                        supplied.append((path.relative_to(BACKEND).as_posix(), node.lineno))
    # The one writer is the projection itself (dataclasses.replace in policy.py).
    assert [path for path, _line in supplied] == ["transfers/policy.py"]


def _errors():
    # Every stage x retryability x permanence, over three domains that reach
    # distinct policy branches. Categories: a stride sample plus every member of
    # each category set the policy branches on, so no decision branch is missed.
    branch_sets = set().union(*(getattr(policy_module, name) for name in (
        "_EXPIRY_CATEGORIES", "_RECONCILE_CATEGORIES", "_PERMANENT_CATEGORIES", "_TRANSIENT_CATEGORIES",
        "_EXECUTION_ALTERNATE_CATEGORIES", "_EXECUTION_RECONCILE_CATEGORIES")))
    categories = [c for c in Category if c in branch_sets or list(Category).index(c) % 5 == 0]
    domains = (Domain.PROVIDER, Domain.EXECUTOR, Domain.LOCAL_RESOURCE)
    for domain, category, stage, retryability, permanence in itertools.product(
        domains, categories, Stage, Retryability, (Permanence.UNKNOWN, Permanence.PERMANENT),
    ):
        yield NormalizedError(domain, category, stage, retryability=retryability, permanence=permanence)


def _decisions(error):
    policy = TransferPolicy(max_attempts=3)
    out = [recovery_action(error), failure_signature(error)]
    for attempts, can_refresh, has_alternate in itertools.product((0, 1, 3, 4), (False, True), (False, True)):
        out.append(policy.retry(error, attempts, 100.0, can_refresh=can_refresh, has_alternate=has_alternate))
    for context in (RecoveryContext(), RecoveryContext(can_refresh=True), RecoveryContext(has_alternate=True, execution_attempts=4),
                    RecoveryContext(storage_ready=False, consecutive_no_progress_failures=5)):
        out.append(policy.recover(error, context, 100.0))
    return out


def test_no_decision_depends_on_the_recovery_or_operator_fields():
    checked = 0
    for base in _errors():
        expected = _decisions(base)
        for value in Recovery:
            for operator in (False, True):
                poisoned = replace(base, recovery=value, operator_action_required=operator)
                assert _decisions(poisoned) == expected, (base.category, base.stage, base.retryability, value, operator)
                checked += 1
    assert checked > 100_000


def test_the_projection_is_a_pure_function_of_canonical_facts():
    for base in _errors():
        stamped = compatibility_error(base)
        assert stamped.recovery == recovery_action(base)
        # Whatever an upstream value said, the stamped output is identical.
        for value in (Recovery.FAIL, Recovery.RETRY, Recovery.REQUIRE_OPERATOR):
            assert compatibility_error(replace(base, recovery=value, operator_action_required=True)) == stamped
