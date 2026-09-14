"""DP 1.0.12 leveling remediation: physical-architecture ownership contracts.

Most of the assertions this file's name implies already have an exact,
non-duplicate existing owner and are deliberately NOT repeated here (the task
specification permits this: "If an existing test file cleanly owns every
proposed test_leveling_architecture.py assertion, it is acceptable to put the
tests there and not create the new file... Do not create duplicate test
owners"):

- deleted modules physically absent + no stale import (AST-walked) --
  ``test_canonical_runtime_architecture.py::
  test_retired_recovery_leveling_layers_are_physically_absent_and_never_imported``;
- production engine/repository MRO is shallow and exact --
  ``test_canonical_runtime_architecture.py::
  test_production_engine_and_repository_mro_is_shallow_and_exact``;
- production composition still binds the canonical engine/repository classes
  by identity -- ``test_candidate_activation_phase2.py::
  test_production_composition_uses_the_final_leveled_engine_and_repository``;
- no cross-module ``stable_payload``/``retire_partial`` mutation --
  ``test_canonical_runtime_architecture.py::
  test_engine_recovery_no_longer_mutates_another_module_stable_payload``;
- no operational recovery-net compensation --
  ``test_operational_downloads_projection.py::
  test_recovery_net_compensation_no_longer_exists``.

This file owns the one remaining architecture contract that had no existing
owner: a single canonical terminal-lifecycle semantic definition, consumed
correctly (not merely mechanically) by every production consumer.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The exact historical local-literal spellings this remediation replaced with
# a reference to transfers.policy's shared owner. Each name is checked as a
# whole-word assignment target (``NAME =``) so an unrelated identifier that
# merely contains one of these substrings is never a false positive.
#
# transfers/canonical.py's former ``_TERMINAL_TRANSFERS = {"completed",
# "consolidated", "deleted", "cancelled", "error"}`` and transfers/engine.py's
# former local ``terminal = {TransferState.DELETED, ...}`` (four members, no
# FAILED) were confirmed by call-site tracing to be TWO DIFFERENT canonical
# families, not the same one applied twice:
#
# - canonical.py's ``lower_materializing`` excludes a record whose own parent
#   transfer has already settled from cross-transfer consolidation-ordering
#   arbitration. A FAILED/"error" transfer's still-``materializing`` residue
#   cannot still be a live contender either (its generation is done, even
#   though FAILED itself remains operator-reopenable) -- this is the SAME
#   settled-generation concept FUNC-001 canonicalized, so it consumes
#   ``SIDE_STATE_RETIRING_TRANSFER_STATES`` (five members, includes FAILED,
#   whose enum value IS the string ``"error"`` -- membership is identical to
#   the literal it replaces).
# - engine.py's ``_aggregate`` decides whether a crash/restart-repair pass
#   should still try to converge raw status to PAUSED. ``transition_allowed``
#   explicitly permits FAILED -> PAUSED unconditionally, so a FAILED transfer
#   must NOT be excluded here the way a genuine dead end is -- this consumes
#   the narrower ``TERMINAL_TRANSFER_STATES`` (four members, no FAILED).
_RETIRED_LITERAL_ASSIGNMENTS = {
    "transfers/_repository_base.py": ("_AGGREGATE_TERMINAL_STATES = frozenset(",),
    "transfers/input_required.py": ("_TERMINAL_FOR_INPUT =",),
    "transfers/convergence_engine.py": ("_TERMINAL_TRANSFER_STATES = frozenset(", "_TERMINAL = frozenset("),
    "transfers/canonical.py": ("_TERMINAL_TRANSFERS = {",),
    "transfers/engine.py": ("terminal = {",),
}

# Each consumer must reference the SPECIFIC canonical constant its confirmed
# semantics require -- not merely "one of the two" (a loose either/or check
# would silently accept a wrong-family substitution, e.g. the narrower
# TERMINAL_TRANSFER_STATES where the broader side-state-retiring family is
# actually required, or vice versa).
_REQUIRED_CANONICAL_CONSTANT = {
    "transfers/_repository_base.py": "SIDE_STATE_RETIRING_TRANSFER_STATES",
    "transfers/input_required.py": "SIDE_STATE_RETIRING_TRANSFER_STATES",
    "transfers/convergence_engine.py": "TERMINAL_TRANSFER_STATES",
    "transfers/canonical.py": "SIDE_STATE_RETIRING_TRANSFER_STATES",
    "transfers/engine.py": "TERMINAL_TRANSFER_STATES",
}


def test_terminal_literal_consumers_no_longer_define_an_independent_literal():
    """FUNC-001/ARCH-001: transfers.policy is the single canonical owner of
    parent lifecycle terminal/settled-family semantics. Repository, input-
    challenge, canonical-consolidation, and engine/recovery code must consume
    ``TERMINAL_TRANSFER_STATES`` / ``SIDE_STATE_RETIRING_TRANSFER_STATES``
    rather than maintaining an independent literal terminal-state set."""
    for relative, retired_spellings in _RETIRED_LITERAL_ASSIGNMENTS.items():
        source = (ROOT / relative).read_text()
        for spelling in retired_spellings:
            assert spelling not in source, (
                f"{relative} still defines the retired independent literal "
                f"{spelling.split(' =')[0].split(' {')[0]!r} instead of consuming "
                "transfers.policy's shared definition"
            )


def test_terminal_literal_consumers_import_the_semantically_correct_constant():
    """Positive half of the same contract, per-file exact (not "either
    constant will do"): each consumer must import the specific canonical
    family its confirmed use-site semantics require. See the module-level
    comment above for why canonical.py and engine.py require DIFFERENT
    families despite both replacing a "terminal-looking" local literal."""
    for relative, constant in _REQUIRED_CANONICAL_CONSTANT.items():
        source = (ROOT / relative).read_text()
        assert "from transfers.policy import" in source, relative
        assert constant in source, (
            f"{relative} does not import/consume transfers.policy.{constant}"
        )


def test_canonical_and_engine_consume_different_terminal_families_on_purpose():
    """Guards against a future well-meaning "unify these two imports" edit:
    canonical.py's consolidation-ordering exclusion and engine.py's
    pause-repair dead-end check are deliberately NOT the same family (see the
    module-level comment). If a future change makes both files import the
    SAME single family, this test forces a human to re-confirm the semantics
    rather than silently drifting one of them onto the wrong set."""
    canonical_source = (ROOT / "transfers/canonical.py").read_text()
    engine_source = (ROOT / "transfers/engine.py").read_text()
    assert "SIDE_STATE_RETIRING_TRANSFER_STATES" in canonical_source
    assert "SIDE_STATE_RETIRING_TRANSFER_STATES" not in engine_source
    assert "TERMINAL_TRANSFER_STATES" in engine_source
    assert "TERMINAL_TRANSFER_STATES" not in canonical_source


def test_policy_is_the_sole_definer_of_the_terminal_literal():
    """The positive half of the same contract: transfers.policy actually
    defines both canonical constants (not just re-exports another owner's
    literal under a new name)."""
    source = (ROOT / "transfers/policy.py").read_text()
    assert "TERMINAL_TRANSFER_STATES = frozenset(" in source
    assert "SIDE_STATE_RETIRING_TRANSFER_STATES = " in source
