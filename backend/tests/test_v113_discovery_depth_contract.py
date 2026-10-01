"""DP 1.0.13 Generalized Extension B -- neutral discovery depth.

The neutral discovery contract says WHAT DP wants enumerated below a
discovered directory, never how a protocol expresses it:

    DiscoveryDepth.CURRENT      the directory's immediate regular files only
    DiscoveryDepth.levels(N)    descend through at most N subdirectory levels
    DiscoveryDepth.UNLIMITED    every reachable subdirectory

It replaces the binary ``recursive`` flag (one fact, one representation).
rsync -- the existing whole-tree consumer -- asks for UNLIMITED by default and
behaves exactly as before (its finite depths: test_v113_rsync_discovery_depth);
a flat discovery is still exactly the call it always was. Depth is enumeration policy only: it never reaches a
candidate, an executor's execution work, continuation or materialization.
"""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from transfers.models import DiscoveryRequest, Endpoint, InputMethod, TransferCandidate, TransferRequest

ROOT = Path(__file__).resolve().parents[1]


def _depth():
    from transfers.models import DiscoveryDepth
    return DiscoveryDepth


def test_the_neutral_depth_has_current_finite_and_unlimited_values():
    depth = _depth()
    assert depth.CURRENT.levels == 0 and not depth.CURRENT.unlimited
    assert depth.UNLIMITED.levels is None and depth.UNLIMITED.unlimited
    two = depth.of(2)
    assert two.levels == 2 and not two.unlimited and two == depth(2)
    # "may this traversal descend below a collection at this level?"
    assert [depth.CURRENT.descends(level) for level in (0, 1)] == [False, False]
    assert [two.descends(level) for level in (0, 1, 2)] == [True, True, False]
    assert all(depth.UNLIMITED.descends(level) for level in (0, 1, 50))


@pytest.mark.parametrize("value", [-1, True, 1.5, "2"])
def test_a_depth_is_a_non_negative_integer_or_unlimited(value):
    with pytest.raises(ValueError):
        _depth()(value)


def test_the_discovery_request_carries_the_neutral_depth_not_a_binary_flag():
    request = DiscoveryRequest(Endpoint("ftp", "ftp://h.example/dir/"))
    assert request.depth == _depth().CURRENT
    assert "recursive" not in {item.name for item in fields(DiscoveryRequest)}


def test_the_remote_discovery_contract_speaks_depth():
    import inspect
    from transfers.contracts import RemoteDiscovery
    parameters = inspect.signature(RemoteDiscovery.discover).parameters
    assert "depth" in parameters and "recursive" not in parameters


def test_depth_never_reaches_a_candidate_or_execution_work():
    from transfers.models import ExecutionSubject, ExecutionWork
    for model in (TransferCandidate, ExecutionSubject, ExecutionWork, TransferRequest):
        names = {item.name for item in fields(model)}
        assert not any("depth" in name or "recursive" in name for name in names), model


# ── rsync: UNLIMITED by default; behavior unchanged ──────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["rsync://h.example/pub/tree", "rsync://h.example/pub/tree/",
                                 "rsync+ssh://h.example/srv/tree", "rsync://h.example/"])
async def test_rsync_still_asks_for_the_whole_tree(url):
    from providers.general_rsync.provider import GeneralRsyncProvider
    result = await GeneralRsyncProvider().resolve(TransferRequest(url.split(":", 1)[0], url))
    assert result.discovery.depth == _depth().UNLIMITED


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["ftp", "sftp"])
async def test_ftp_and_sftp_discovery_still_refuse_any_tree(tmp_path, scheme):
    from test_v113_transport_evidence_sampling import executor_for, guard_for
    from transfers.errors import Category, TransferError
    from transfers.models import ExecutionSubject
    executor = executor_for(tmp_path, guard_for())
    candidate = TransferCandidate("dir", (Endpoint(scheme, f"{scheme}://h.example/dir/"),),
                                  accepted_input_methods=(InputMethod.USERNAME_PASSWORD,), request_kind=scheme)
    for depth in (_depth().of(1), _depth().UNLIMITED):
        with pytest.raises(TransferError) as raised:
            await executor.discover(ExecutionSubject.of(candidate), depth=depth)
        assert raised.value.error.category == Category.UNSUPPORTED_CAPABILITY


def test_core_passes_depth_only_for_a_non_flat_discovery():
    """A flat discovery is exactly the call it always was; only a deeper one
    carries the neutral depth to the executor."""
    source = (ROOT / "transfers/_engine_base.py").read_text()
    assert '{"depth": request.depth} if request.depth != DiscoveryDepth.CURRENT else {}' in source
    assert "recursive" not in source.split("async def _discover", 1)[1].split("async def ", 1)[0]
