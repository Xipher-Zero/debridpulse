"""aria2 native private resume and native source retarget (1.0.13).

The executor boundary only: a paused job's source is replaced -- in the short
active window aria2 1.37.0 requires -- through the same destination/egress/
header preparation a fresh start applies, its private piece state (and control
file) is never read, written or trusted, and a fresh job still continues only
at a contiguous DebridPulse boundary.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from executors.aria2.client import Aria2ConnectionError, Aria2DownloadStatus, Aria2RPCError
from executors.aria2.executor import Aria2Configuration, Aria2Executor
from execution_requests import file_request
from transfers import material as mat
from transfers.errors import Category, TransferError
from transfers.models import (
    ContinuationCapability, ContinuationPlan, ContinuationStrategy, Endpoint, ExecutionState, RetargetTruth,
    TransferCandidate,
)
from transfers.registry import IntegrationRegistry

SIZE = 8 << 20
OLD = "https://old.example/file?sig=old-secret"
NEW = "https://new.example/file?sig=new-secret"


class Daemon:
    """aria2 1.37.0 as characterized: changing the URIs of a paused job that
    has run is refused here (the real daemon aborts), a used URI removed while
    active is put back when the job pauses (its in-flight segment's request)."""

    def __init__(self):
        self.jobs = {}
        self.calls = []
        self.fail = set()
        self.in_flight = {}
        self.options = {}

    async def tell_status(self, gid):
        if gid not in self.jobs:
            raise Aria2RPCError(f"aria2 [1]: GID {gid} is not found", code=1)
        return self.jobs[gid]

    async def _multicall(self, calls):
        return [await self._call(method, params) for method, params in calls]

    async def _call(self, method, params):
        self.calls.append((method, params))
        if method in self.fail:
            raise Aria2ConnectionError("acknowledgement lost")
        if method == "aria2.getOption":
            return dict(self.options.get(params[0], {}))
        job = self.jobs[params[0]]
        entries = job.files[0]["uris"]
        if method == "aria2.changeOption":
            self.options.setdefault(params[0], {}).update(params[1])
        if method == "aria2.unpause":
            job.status = "active"
        elif method == "aria2.pause":
            job.status = "paused"
            entries.extend({"uri": uri, "status": "used"} for uri in self.in_flight.pop(params[0], ()))
        elif method == "aria2.changeUri":
            assert not (job.status == "paused" and params[3]), "aria2 1.37.0 aborts: URIs added to a paused job"
            for uri in params[2]:
                entry = next(item for item in entries if item["uri"] == uri)
                entries.remove(entry)
                if job.status == "active" and entry["status"] == "used":
                    self.in_flight.setdefault(params[0], []).append(uri)
            entries.extend({"uri": uri, "status": "waiting"} for uri in params[3])
            return [len(params[2]), len(params[3])]
        return "OK"

    def mutations(self):
        return [call for call in self.calls if call[0] in {"aria2.changeOption", "aria2.changeUri", "aria2.addUri",
                                                            "aria2.unpause", "aria2.pause", "aria2.forceRemove"}]


def plan(strategy=ContinuationStrategy.NATIVE_STATE_HANDOFF, boundary=0):
    retained = ((0, 2 << 20), (4 << 20, 5 << 20)) if strategy == ContinuationStrategy.NATIVE_STATE_HANDOFF else (
        ((0, boundary),) if boundary else ())
    return ContinuationPlan(1, 1, mat.GEOMETRY_VERSION, "b", "aria2", strategy, boundary, retained, (),
                            ((0, SIZE),), SIZE, "user_candidate_switch")


@pytest.fixture
def aria2(tmp_path, monkeypatch):
    validated = []

    async def validate(address, **_kwargs):
        validated.append(address)
        if "blocked" in address:
            raise ValueError("destination rejected")
        return address
    monkeypatch.setattr("executors.aria2.executor.validate_resolved_public_destination", validate)
    daemon = Daemon()
    grants = {}

    async def authorize(handle, action):
        grant = grants.get(handle.attempt_id)
        return grant is not None and grant[0] == handle and (action == "observe" or action in grant[1])

    guards = []

    def job_options(address, scope=None, **_kwargs):
        guards.append(address)
        return {"all-proxy": "http://guard:8888", "all-proxy-user": "route:" + address.split("/")[2]}
    egress = SimpleNamespace(ensure_started=AsyncMock(), job_options=job_options)
    executor = Aria2Executor(daemon, Aria2Configuration(str(tmp_path), confirmation_delay=0,
                                                        control_confirmation_timeout=0.05), authorize, egress=egress)
    target = tmp_path / "movie.bin"

    def request(url, attempt, continuation=None, headers=None):
        candidate = TransferCandidate("movie.bin", (Endpoint(url.split(":", 1)[0], url, dict(headers or {})),),
                                      expected_bytes=SIZE)
        base = file_request(candidate, str(target), attempt, root=tmp_path)
        return type(base)(base.work, base.attempt_id, base.paused, continuation)

    old = executor.prepare(request(OLD, "attempt-a"))
    gid = old.native["gid"]
    daemon.jobs[gid] = Aria2DownloadStatus(gid, "paused", SIZE, 3 << 20, 0, files=[
        {"path": str(target), "uris": [{"uri": OLD, "status": "used"}, {"uri": OLD, "status": "waiting"}]}])
    grants[old.attempt_id] = (old, {"pause", "resume", "cancel"})
    daemon.options[gid] = {"all-proxy-user": "route:old.example", "header": ""}
    target.write_bytes(b"x" * (3 << 20))
    Path(str(target) + ".aria2").write_bytes(b"private piece map")
    return SimpleNamespace(executor=executor, daemon=daemon, grants=grants, old=old, gid=gid, request=request,
                           target=target, validated=validated, guards=guards)


def test_aria2_declares_native_private_resume_and_source_retarget_and_registers():
    declared = Aria2Executor.capabilities.continuation
    assert {ContinuationCapability.NATIVE_QUIESCE, ContinuationCapability.NATIVE_PRIVATE_RESUME,
            ContinuationCapability.NATIVE_SOURCE_RETARGET} <= declared
    registry = IntegrationRegistry()
    registry.register_executor(Aria2Executor(SimpleNamespace(url="http://aria2.invalid/jsonrpc"),
                                             Aria2Configuration("/tmp"), AsyncMock(return_value=True)))


@pytest.mark.asyncio
async def test_prepare_retarget_adopts_the_native_job_without_any_native_mutation(aria2):
    new_request = aria2.request(NEW, "attempt-b", plan())
    handle = await aria2.executor.prepare_retarget(new_request, aria2.old)
    assert handle.attempt_id == "attempt-b" and handle.native == {"gid": aria2.gid}
    assert handle.correlation == aria2.old.correlation
    assert NEW in aria2.validated and not aria2.daemon.mutations()


@pytest.mark.asyncio
@pytest.mark.parametrize("url,continuation,headers", [
    ("ftp://mirror.example/file", plan(), None),                     # FTP carries session state
    ("https://blocked.example/file", plan(), None),                  # destination policy
    (NEW, plan(ContinuationStrategy.CONTIGUOUS_FROM_OFFSET, 2 << 20), None),  # not a handoff plan
    (NEW, plan(), {"X-Bad": "a\r\nHost: evil"}),                      # header injection
])
async def test_prepare_retarget_refuses_incompatible_or_unsafe_pairs(aria2, url, continuation, headers):
    assert await aria2.executor.prepare_retarget(aria2.request(url, "attempt-b", continuation, headers),
                                                 aria2.old) is None
    assert not aria2.daemon.mutations()


@pytest.mark.asyncio
async def test_prepare_retarget_refuses_a_non_http_job_or_another_target(aria2, tmp_path):
    job = aria2.daemon.jobs[aria2.gid]
    job.files[0]["uris"] = [{"uri": "sftp://host.example/file", "status": "used"}]
    assert await aria2.executor.prepare_retarget(aria2.request(NEW, "attempt-b", plan()), aria2.old) is None
    job.files[0]["uris"] = [{"uri": OLD, "status": "used"}]
    other = aria2.request(NEW, "attempt-b", plan())
    moved = file_request(other.work.subject.candidate, str(tmp_path / "other.bin"), "attempt-b", root=tmp_path)
    moved = type(moved)(moved.work, moved.attempt_id, False, plan())
    assert await aria2.executor.prepare_retarget(moved, aria2.old) is None


@pytest.mark.asyncio
async def test_retarget_swaps_sources_only_while_active_and_leaves_the_job_paused(aria2):
    new_request = aria2.request(NEW, "attempt-b", plan())
    handle = await aria2.executor.prepare_retarget(new_request, aria2.old)
    aria2.grants["attempt-b"] = (handle, {"retarget"})
    control_file = Path(str(aria2.target) + ".aria2").read_bytes()
    payload_size = os.path.getsize(aria2.target)

    observed = await aria2.executor.retarget_from(new_request, handle, aria2.old)
    assert observed.state == ExecutionState.PAUSED and observed.error is None and observed.handle == handle
    methods = [call[0] for call in aria2.daemon.mutations()]
    # Active window, atomic swap, quiesce again, then drop what aria2 put back.
    assert methods == ["aria2.unpause", "aria2.changeUri", "aria2.changeOption", "aria2.pause", "aria2.changeUri"]
    swap, options_call, _pause, strip = (aria2.daemon.mutations()[1:])
    assert swap[1] == [aria2.gid, 1, [OLD, OLD], [NEW]] and strip[1] == [aria2.gid, 1, [OLD], []]
    options = options_call[1][1]
    # The replacement's own guarded route and headers; nothing of the job's identity.
    assert options["all-proxy-user"] == "route:new.example" and aria2.guards[-1] == NEW
    assert options["header"] == [] and options["max-http-redirection"] == "0"
    assert not {"gid", "dir", "out", "pause", "continue", "split"} & set(options)
    assert [item["uri"] for item in aria2.daemon.jobs[aria2.gid].files[0]["uris"]] == [NEW]
    # Private state is neither read nor rewritten; the payload is not cut.
    assert Path(str(aria2.target) + ".aria2").read_bytes() == control_file
    assert os.path.getsize(aria2.target) == payload_size
    assert "new-secret" not in repr(observed)


@pytest.mark.asyncio
async def test_retarget_refusals_before_mutation_are_failed_and_touch_nothing(aria2):
    new_request = aria2.request(NEW, "attempt-b", plan())
    handle = await aria2.executor.prepare_retarget(new_request, aria2.old)
    # Not authorized for this attempt (e.g. no longer current, not prepared, or paused).
    refused = await aria2.executor.retarget_from(new_request, handle, aria2.old)
    assert refused.state == ExecutionState.FAILED
    aria2.grants["attempt-b"] = (handle, {"retarget"})
    # A request that must stay quiesced is never retargeted (aria2 must run it).
    quiesced = type(new_request)(new_request.work, new_request.attempt_id, True, new_request.continuation)
    assert (await aria2.executor.retarget_from(quiesced, handle, aria2.old)).state == ExecutionState.FAILED
    # Not quiesced.
    aria2.daemon.jobs[aria2.gid].status = "active"
    assert (await aria2.executor.retarget_from(new_request, handle, aria2.old)).state == ExecutionState.FAILED
    aria2.daemon.jobs[aria2.gid].status = "paused"
    # The replacement fails destination policy at retarget time.
    blocked = aria2.request("https://blocked.example/file", "attempt-b", plan())
    result = await aria2.executor.retarget_from(blocked, handle, aria2.old)
    assert result.state == ExecutionState.FAILED and result.error.category == Category.DESTINATION_BLOCKED
    assert not aria2.daemon.mutations()


@pytest.mark.asyncio
async def test_a_lost_retarget_acknowledgement_is_uncertain_not_failed(aria2):
    new_request = aria2.request(NEW, "attempt-b", plan())
    handle = await aria2.executor.prepare_retarget(new_request, aria2.old)
    aria2.grants["attempt-b"] = (handle, {"retarget"})
    aria2.daemon.fail.add("aria2.changeUri")
    observed = await aria2.executor.retarget_from(new_request, handle, aria2.old)
    assert observed.state == ExecutionState.UNKNOWN
    assert "new-secret" not in repr(observed) and "old-secret" not in repr(observed)


@pytest.mark.asyncio
async def test_a_fresh_job_stays_prefix_only_and_never_honours_a_handoff_plan(aria2):
    # A handoff plan's sparse ranges live only in the inherited job: a fresh
    # job fails closed and leaves the payload and control file untouched.
    fresh = aria2.request(NEW, "attempt-c", plan())
    with pytest.raises(TransferError) as refused:
        aria2.executor._apply_continuation(fresh, aria2.target)
    assert refused.value.error.category == Category.RESOURCE_STATE_CONFLICT
    assert Path(str(aria2.target) + ".aria2").exists() and os.path.getsize(aria2.target) == 3 << 20
    # A contiguous plan: control file discarded, payload cut to the boundary.
    contiguous = aria2.request(NEW, "attempt-c", plan(ContinuationStrategy.CONTIGUOUS_FROM_OFFSET, 2 << 20))
    assert aria2.executor._apply_continuation(contiguous, aria2.target) == "true"
    assert not Path(str(aria2.target) + ".aria2").exists() and os.path.getsize(aria2.target) == 2 << 20


@pytest.mark.asyncio
async def test_retarget_truth_proves_a_source_only_by_its_uris_and_its_route_binding(aria2):
    new_request = aria2.request(NEW, "attempt-b", plan())
    original = aria2.request(OLD, "attempt-a")
    handle = await aria2.executor.prepare_retarget(new_request, aria2.old)
    aria2.grants["attempt-b"] = (handle, {"retarget"})
    job = aria2.daemon.jobs[aria2.gid]
    truth = aria2.executor.retarget_truth
    # Untouched: provably still the previous source.
    assert await truth(new_request, handle, original) == RetargetTruth.ORIGINAL
    # URIs switched but the route binding still the old source's: not proven.
    job.files[0]["uris"] = [{"uri": NEW, "status": "waiting"}]
    assert await truth(new_request, handle, original) == RetargetTruth.UNKNOWN
    # A mix of both sources: not proven either way.
    job.files[0]["uris"] = [{"uri": OLD, "status": "used"}, {"uri": NEW, "status": "waiting"}]
    assert await truth(new_request, handle, original) == RetargetTruth.UNKNOWN
    assert not aria2.daemon.mutations()  # judging is read-only
    # A completed retarget: URIs and binding both the replacement's.
    job.files[0]["uris"] = [{"uri": OLD, "status": "used"}, {"uri": OLD, "status": "waiting"}]
    assert (await aria2.executor.retarget_from(new_request, handle, aria2.old)).state == ExecutionState.PAUSED
    after_retarget = len(aria2.daemon.mutations())
    assert await truth(new_request, handle, original) == RetargetTruth.RETARGETED
    # A job that is acquiring is never judged.
    job.status = "active"
    assert await truth(new_request, handle, original) == RetargetTruth.UNKNOWN
    assert len(aria2.daemon.mutations()) == after_retarget
