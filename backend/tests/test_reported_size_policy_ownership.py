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
    sampler_source = inspect.getsource(sampled_public_artifact_fingerprint)
    sampler_tree = ast.parse(sampler_source)
    expected_size_reads = [
        node for node in ast.walk(sampler_tree)
        if isinstance(node, ast.Name)
        and node.id == "expected_bytes"
        and isinstance(node.ctx, ast.Load)
    ]

    assert expected_size_reads == []
    assert "reported_sizes_compatible" not in sampler_source
    assert "length != expected_bytes" not in sampler_source
    assert "total != expected_bytes" not in sampler_source
    assert 'return _unavailable("size_disagreement")' not in sampler_source
