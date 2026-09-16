"""DP 1.0.12 canonical lifecycle/recovery/completion rework, Section 3.3/5.2
(Gate 9 revision): unit coverage for the one canonical size-knowledge
resolver, ``transfers.filesystem.size_knowledge``, and a scope-boundary
proof that no provider or executor currently wired into this codebase
supplies affirmative known-zero evidence.
"""
from __future__ import annotations

import inspect

import pytest

from transfers.filesystem import known_positive_size, size_knowledge
from transfers.models import SizeKnowledge


def test_known_positive_size_prefers_expected_bytes_over_observed_total():
    assert known_positive_size(10, 4) == 10


def test_known_positive_size_falls_back_to_observed_total():
    assert known_positive_size(0, 4) == 4


def test_known_positive_size_is_none_when_both_are_zero_or_absent():
    assert known_positive_size(0, 0) is None
    assert known_positive_size(-1, 0) is None
    assert known_positive_size(0, -1) is None


def test_known_positive_size_rejects_bool_masquerading_as_int():
    # bool is a subclass of int in Python; True/False must never be read as
    # a byte count.
    assert known_positive_size(True, 0) is None
    assert known_positive_size(0, True) is None


def test_size_knowledge_reports_known_positive_when_a_positive_size_exists():
    knowledge, size = size_knowledge(10, 0)
    assert knowledge == SizeKnowledge.KNOWN_POSITIVE
    assert size == 10

    knowledge, size = size_knowledge(0, 4)
    assert knowledge == SizeKnowledge.KNOWN_POSITIVE
    assert size == 4


def test_size_knowledge_is_unknown_by_default_when_both_are_zero():
    knowledge, size = size_knowledge(0, 0)
    assert knowledge == SizeKnowledge.UNKNOWN
    assert size == 0


def test_size_knowledge_only_reports_known_zero_with_explicit_affirmative_evidence():
    """SIZE_UNKNOWN and SIZE_KNOWN(0) are distinct facts: the same (0, 0)
    inputs must resolve differently depending on whether the caller can
    supply genuine affirmative-zero evidence, never from the inputs alone."""
    knowledge, size = size_knowledge(0, 0, affirmative_zero=True)
    assert knowledge == SizeKnowledge.KNOWN_ZERO
    assert size == 0

    knowledge, size = size_knowledge(0, 0, affirmative_zero=False)
    assert knowledge == SizeKnowledge.UNKNOWN


def test_affirmative_zero_never_overrides_a_real_known_positive_size():
    """A caller-asserted affirmative-zero claim must never be allowed to
    contradict an already-known positive size -- known-positive always
    takes precedence over a claimed-zero input."""
    knowledge, size = size_knowledge(10, 0, affirmative_zero=True)
    assert knowledge == SizeKnowledge.KNOWN_POSITIVE
    assert size == 10


def test_general_http_and_aria2_never_pass_affirmative_zero():
    """DP 1.0.12 canonical lifecycle/recovery/completion rework, Section 5.2
    (Gate 9 revision, finding 3): ``affirmative_zero`` must only ever be set
    from a genuine, positively-confirmed zero-length signal. No provider or
    executor currently wired into this codebase has one -- General HTTP and
    aria2 both resolve a reported size through a ``value or 0``-shaped
    fallback that cannot distinguish an explicitly reported zero from an
    absent field. This is a static, source-level proof of that scope
    boundary: it must fail loudly (not silently regress into a fabricated
    affirmative-zero signal) the moment someone adds
    ``affirmative_zero=True`` to a real call site without first building a
    genuine evidence channel for it."""
    import providers.general_http.provider as general_http_provider
    import executors.aria2.client as aria2_client
    import executors.aria2.executor as aria2_executor
    import executors.aria2.translation as aria2_translation
    import transfers._engine_base as engine_base
    import transfers._engine_recovery as engine_recovery

    for module in (
        general_http_provider, aria2_client, aria2_executor, aria2_translation,
        engine_base, engine_recovery,
    ):
        source = inspect.getsource(module)
        assert "affirmative_zero=True" not in source, (
            f"{module.__name__} passes affirmative_zero=True without a real evidence "
            "channel backing it"
        )


@pytest.mark.parametrize("size", [0, 4])
def test_known_positive_size_and_size_knowledge_agree_on_positive_branch(size):
    """size_knowledge must never diverge from known_positive_size for the
    already-established known-positive contract -- it is a superset, not a
    parallel reimplementation."""
    expected = known_positive_size(size, 0)
    knowledge, resolved = size_knowledge(size, 0)
    if expected is None:
        assert knowledge != SizeKnowledge.KNOWN_POSITIVE
    else:
        assert knowledge == SizeKnowledge.KNOWN_POSITIVE
        assert resolved == expected
