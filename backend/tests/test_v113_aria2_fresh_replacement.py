"""aria2 offers no native source retarget (1.0.13): a source switch reaches
aria2 only as a fresh job through its ordinary start, which owns redirects,
destination and egress binding. Native quiesce and private resume remain --
for the same source.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from executors.aria2.executor import Aria2Configuration, Aria2Executor
from execution_requests import file_request
from transfers import material as mat
from transfers.errors import Category, TransferError
from transfers.models import ContinuationCapability, ContinuationPlan, ContinuationStrategy, Endpoint, TransferCandidate
from transfers.registry import IntegrationRegistry

SIZE = 8 << 20


def test_aria2_declares_same_source_native_lifecycle_and_portable_import_but_no_source_retarget():
    declared = Aria2Executor.capabilities.continuation
    assert {ContinuationCapability.NATIVE_QUIESCE, ContinuationCapability.NATIVE_PRIVATE_RESUME,
            ContinuationCapability.CONTIGUOUS_FROM_OFFSET, ContinuationCapability.IMPORT_SPARSE_MATERIAL} <= declared
    assert "native_source_retarget" not in {item.value for item in declared}
    assert not {"prepare_retarget", "retarget_from", "retarget_truth"} & set(dir(Aria2Executor))
    IntegrationRegistry().register_executor(Aria2Executor(SimpleNamespace(url="http://aria2.invalid/jsonrpc"),
                                                          Aria2Configuration("/tmp"), AsyncMock(return_value=True)))


def test_a_fresh_job_never_honours_a_historical_handoff_plan(tmp_path):
    executor = Aria2Executor(SimpleNamespace(url="http://aria2.invalid/jsonrpc"), Aria2Configuration(str(tmp_path)),
                             AsyncMock(return_value=True))
    target = tmp_path / "movie.bin"
    target.write_bytes(b"x" * (3 << 20))
    Path(str(target) + ".aria2").write_bytes(b"private piece map")
    candidate = TransferCandidate("movie.bin", (Endpoint("https", "https://new.example/file"),), expected_bytes=SIZE)

    def request(strategy, boundary=0, retained=()):
        base = file_request(candidate, str(target), "attempt-c", root=tmp_path)
        plan = ContinuationPlan(1, 1, mat.GEOMETRY_VERSION, "b", "aria2", strategy, boundary, retained, (),
                                ((0, SIZE),), SIZE, "user_candidate_switch")
        return type(base)(base.work, base.attempt_id, base.paused, plan)

    # A recorded handoff plan's sparse ranges lived only in an inherited job: a
    # fresh job fails closed and leaves the payload and control file untouched.
    with pytest.raises(TransferError) as refused:
        executor._apply_continuation(request(ContinuationStrategy.NATIVE_STATE_HANDOFF,
                                             retained=((0, 2 << 20),)), target)
    assert refused.value.error.category == Category.RESOURCE_STATE_CONFLICT
    assert Path(str(target) + ".aria2").exists() and os.path.getsize(target) == 3 << 20
    # A contiguous plan: control file discarded, payload cut to the boundary.
    contiguous = request(ContinuationStrategy.CONTIGUOUS_FROM_OFFSET, 2 << 20, ((0, 2 << 20),))
    assert executor._apply_continuation(contiguous, target) == {"continue": "true"}
    assert not Path(str(target) + ".aria2").exists() and os.path.getsize(target) == 2 << 20
