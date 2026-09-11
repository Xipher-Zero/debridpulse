from __future__ import annotations

import ast
import inspect

from services.network_safety import sampled_public_artifact_fingerprint
from transfers.engine import TransferEngine
from transfers.mirrors import reported_sizes_compatible


def test_reported_size_policy_accepts_bounded_jitter_and_rejects_outside() -> None:
    base = 1_000_000_000

    assert reported_sizes_compatible(base, 1_001_000_000)
    assert reported_sizes_compatible(1_001_000_000, base)
    assert not reported_sizes_compatible(base, 1_001_100_000)
    assert not reported_sizes_compatible(base, base + 512 * 1024 * 1024 + 1)
    assert not reported_sizes_compatible(0, base)


def test_failover_and_refresh_delegate_reported_size_compatibility() -> None:
    alternate_source = inspect.getsource(TransferEngine._activate_alternate)
    refresh_source = inspect.getsource(TransferEngine._refresh)

    assert "reported_sizes_compatible(" in alternate_source
    assert "reported_sizes_compatible(" in refresh_source
    assert "artifact.expected_bytes != replacement.expected_bytes" not in alternate_source
    assert "artifact.expected_bytes != replacement_size" not in refresh_source


def test_http_sampler_does_not_own_reported_size_equivalence_policy() -> None:
    """The sampler may use ``expected_bytes`` only as a negative certainty
    guard, never as artifact-identity policy.

    Before the DP 1.0.12 false-negative repair, this test asserted that
    ``sampled_public_artifact_fingerprint`` never read ``expected_bytes`` at
    all. That blanket prohibition was itself proven to be the root cause of a
    production defect: with the sampler blind to the caller's declared size,
    a short, Range-ignoring HTTP 200 response (e.g. a 64 KiB body against a
    ~10 GiB declared candidate) was unconditionally reported as a trustworthy
    ``FULL_CONTENT_SAMPLE``, which downstream equivalence policy then read as
    a genuine size contradiction between independently sourced copies of the
    same file (see transfers.mirrors.shared_evidence and transfers 175/186).
    Section 8 of the corrective task explicitly sanctions exactly the guard
    added here: a positive caller-supplied expected size may be used to
    withhold a false claim of completeness, but never to assert equality or
    inequality between a discovered length and the expected size. The
    narrower, still-enforced invariant below is that no exact-equality
    identity check appears in the sampler.
    """
    sampler_source = inspect.getsource(sampled_public_artifact_fingerprint)
    sampler_tree = ast.parse(sampler_source)
    equality_reads = [
        node for node in ast.walk(sampler_tree)
        if isinstance(node, ast.Compare)
        and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops)
        and any(
            isinstance(operand, ast.Name) and operand.id == "expected_bytes"
            for operand in (node.left, *node.comparators)
        )
    ]

    assert equality_reads == []
    assert "reported_sizes_compatible" not in sampler_source
    assert "length != expected_bytes" not in sampler_source
    assert "total != expected_bytes" not in sampler_source
    assert 'return _unavailable("size_disagreement")' not in sampler_source
