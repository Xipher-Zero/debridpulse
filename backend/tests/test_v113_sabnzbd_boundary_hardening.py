"""1.0.13 Gate-9 remediation: SAB HTTP boundary, batching and attempt identity.

Findings 3, 4 and 5 of the Gate 9 rejection. Each expectation here is a safety
property, not a convenience: an unusable answer must never become authoritative
emptiness, a batch must be a batch, and a correlation token must belong to one
DP attempt only.
"""
from __future__ import annotations

import base64

import pytest

from executors.sabnzbd.client import SabApiError, SabEndpoint, SabTransportError, SabnzbdClient
from transfers.models import (
    ExecutionRequest, ExecutionState, ExecutionSubject, ExecutionWork,
    MaterializationKind, MaterializationPlan, TransferCandidate,
)

from sab_fakes import FakeSab, staged_context
from test_v113_sabnzbd_executor import build, lab, request_for  # noqa: F401


# --------------------------------------------------------------------------
# Finding 5 -- HTTP boundary hardening
# --------------------------------------------------------------------------

class _Response:
    def __init__(self, status, body):
        self.status, self._body = status, body
    async def text(self):
        return self._body
    async def __aenter__(self):
        return self
    async def __aexit__(self, *exc):
        return False


class _Session:
    """A session whose every request yields one scripted response."""
    def __init__(self, status, body):
        self._status, self._body = status, body
    def get(self, url):
        return _Response(self._status, self._body)
    def post(self, url, data=None, headers=None):
        return _Response(self._status, self._body)
    async def __aenter__(self):
        return self
    async def __aexit__(self, *exc):
        return False


def client_returning(status, body):
    endpoint = SabEndpoint("http://sab.invalid:8080", "key", 5)
    return SabnzbdClient(endpoint, session_factory=lambda: _Session(status, body))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 502, 503])
async def test_non_2xx_is_an_api_failure_even_with_a_json_body(status):
    """A JSON body on a non-2xx response is never authoritative truth."""
    client = client_returning(status, '{"queue": {"slots": []}}')
    with pytest.raises((SabApiError, SabTransportError)):
        await client.queue_slots()


@pytest.mark.asyncio
async def test_plain_text_api_key_error_is_an_api_failure():
    client = client_returning(200, "API Key Incorrect")
    with pytest.raises(SabApiError):
        await client.queue_slots()


@pytest.mark.asyncio
async def test_explicit_api_error_object_is_an_api_failure():
    client = client_returning(200, '{"status": false, "error": "API Key Incorrect"}')
    with pytest.raises(SabApiError):
        await client.queue_slots()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    '{}',                              # no `queue` key at all
    '{"queue": null}',                 # present but not a mapping
    '{"queue": {}}',                   # no `slots`
    '{"queue": {"slots": null}}',      # slots not a list
    '{"queue": {"slots": {}}}',        # slots wrong shape
    '[]',                              # not an object
])
async def test_successful_response_missing_expected_shape_is_an_api_failure(body):
    """A 200 whose structure is missing must NOT decode to an empty queue."""
    client = client_returning(200, body)
    with pytest.raises(SabApiError):
        await client.queue_slots()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ['{}', '{"history": null}', '{"history": {"slots": null}}'])
async def test_history_shape_is_validated_too(body):
    client = client_returning(200, body)
    with pytest.raises(SabApiError):
        await client.history_slots()


@pytest.mark.asyncio
async def test_addfile_rejects_a_response_without_a_native_identity():
    for body in ['{"status": true}', '{"status": true, "nzo_ids": []}',
                 '{"status": false, "nzo_ids": ["x"]}', '{"nzo_ids": [""]}']:
        client = client_returning(200, body)
        with pytest.raises(SabApiError):
            await client.addfile(b"<nzb/>", job_name="dp-token")


# --------------------------------------------------------------------------
# Finding 4 -- observe_many must genuinely batch
# --------------------------------------------------------------------------

class CountingSab(FakeSab):
    """Counts the round trips an observation costs, split by kind.

    ``*_snapshot`` calls are the bulk listings; ``*_slots`` are narrow
    per-handle searches, which a batched observation must never issue.
    """
    def __init__(self, **kw):
        super().__init__(**kw)
        self.queue_snapshots = 0
        self.history_snapshots = 0
        self.per_handle_lookups = 0

    async def queue_snapshot(self, limit=500):
        self.queue_snapshots += 1
        return await super().queue_snapshot(limit)

    async def history_snapshot(self, limit=500):
        self.history_snapshots += 1
        return await super().history_snapshot(limit)

    async def queue_slots(self, search=None):
        self.per_handle_lookups += 1
        return await super().queue_slots(search)

    async def history_slots(self, search=None, nzo_id=None):
        self.per_handle_lookups += 1
        return await super().history_slots(search, nzo_id)


@pytest.fixture
def counting_lab(tmp_path):
    from types import SimpleNamespace
    root = tmp_path / "download"
    (root / ".dpwork" / "complete").mkdir(parents=True)
    (root / ".dpwork" / "incomplete").mkdir(parents=True)
    sab = CountingSab(complete_dir=str(root / ".dpwork" / "complete"),
                      download_dir=str(root / ".dpwork" / "incomplete"))
    return SimpleNamespace(sab=sab, root=str(root), executor=build(sab, root=str(root)))


async def start_n(lab_, count):
    handles = []
    for index in range(count):
        request = request_for(lab_.root, name=f"job-{index}", attempt=f"attempt-{index}")
        handles.append((await lab_.executor.start(request, lab_.executor.prepare(request))).handle)
    return tuple(handles)


@pytest.mark.asyncio
async def test_observe_many_uses_bulk_snapshots_not_per_handle_requests(counting_lab):
    handles = await start_n(counting_lab, 6)
    counting_lab.sab.queue_snapshots = 0
    counting_lab.sab.history_snapshots = 0
    counting_lab.sab.per_handle_lookups = 0

    snapshot = await counting_lab.executor.observe_many(handles)

    assert len(snapshot.observations) == 6
    # Exactly one bulk queue snapshot, history at most once, and NEVER a
    # per-handle lookup -- the cost must not scale with the number of handles.
    assert counting_lab.sab.queue_snapshots == 1, counting_lab.sab.queue_snapshots
    assert counting_lab.sab.history_snapshots <= 1, counting_lab.sab.history_snapshots
    assert counting_lab.sab.per_handle_lookups == 0, counting_lab.sab.per_handle_lookups


@pytest.mark.asyncio
async def test_batched_observation_still_reports_each_state_correctly(counting_lab):
    handles = await start_n(counting_lab, 3)
    counting_lab.sab.finish(handles[0].native["nzo_id"])
    counting_lab.sab.fail(handles[1].native["nzo_id"])

    observations = {o.handle.native["nzo_id"]: o
                    for o in (await counting_lab.executor.observe_many(handles)).observations}
    assert observations[handles[0].native["nzo_id"]].state == ExecutionState.SUCCEEDED
    assert observations[handles[1].native["nzo_id"]].state == ExecutionState.FAILED
    assert observations[handles[2].native["nzo_id"]].state in {
        ExecutionState.QUEUED, ExecutionState.RUNNING}


@pytest.mark.asyncio
async def test_a_truncated_snapshot_never_manufactures_absence(counting_lab):
    """If the snapshot cannot be authoritative, absence must not be inferred."""
    handles = await start_n(counting_lab, 4)
    counting_lab.sab.truncate_queue_to = 1      # report fewer slots than exist
    snapshot = await counting_lab.executor.observe_many(handles)
    for observation in snapshot.observations:
        assert observation.state != ExecutionState.ABSENT


@pytest.mark.asyncio
async def test_bulk_failure_is_a_snapshot_error_not_an_empty_success(counting_lab):
    handles = await start_n(counting_lab, 3)
    counting_lab.sab.reachable = False
    snapshot = await counting_lab.executor.observe_many(handles)
    assert snapshot.error is not None or all(
        o.state == ExecutionState.UNKNOWN for o in snapshot.observations)
    for observation in snapshot.observations:
        assert observation.state not in {ExecutionState.ABSENT, ExecutionState.FAILED}


# --------------------------------------------------------------------------
# Finding 3 -- the correlation token belongs to ONE attempt
# --------------------------------------------------------------------------

def subject_and_plan(root, name="posted"):
    candidate = TransferCandidate(
        name=name, endpoints=(), provider_id="usenet",
        materialization=MaterializationKind.COLLECTION, request_kind="nzb",
        context=staged_context())
    return candidate, MaterializationPlan(MaterializationKind.COLLECTION, f"{root}/{name}")


def test_distinct_attempts_of_the_same_candidate_get_distinct_tokens(lab):
    """Same candidate, same materialization, different attempt -> different token."""
    candidate, plan = subject_and_plan(lab.root)
    def request(attempt):
        return ExecutionRequest(ExecutionWork(ExecutionSubject.of(candidate), plan, attempt), attempt)

    first = lab.executor.prepare(request("attempt-A"))
    second = lab.executor.prepare(request("attempt-B"))
    assert first.correlation["token"] != second.correlation["token"]
    # ...and the token is actually derived from the attempt.
    again = lab.executor.prepare(request("attempt-A"))
    assert again.correlation["token"] == first.correlation["token"]


@pytest.mark.asyncio
async def test_an_ambiguous_start_never_binds_a_new_attempt_to_an_older_attempts_job(lab):
    """The decisive safety property of finding 3."""
    candidate, plan = subject_and_plan(lab.root)
    def request(attempt):
        return ExecutionRequest(ExecutionWork(ExecutionSubject.of(candidate), plan, attempt), attempt)

    first_request = request("attempt-A")
    first_handle = lab.executor.prepare(first_request)
    first_bound = (await lab.executor.start(first_request, first_handle)).handle
    older_nzo = first_bound.native["nzo_id"]

    # A second attempt for the SAME candidate whose acknowledgement is lost.
    lab.sab.drop_next_response = True
    second_request = request("attempt-B")
    second_handle = lab.executor.prepare(second_request)
    observed = await lab.executor.start(second_request, second_handle)

    if observed.handle.native is not None:
        assert observed.handle.native["nzo_id"] != older_nzo
    assert observed.state != ExecutionState.FAILED
    # The older attempt's job is untouched and still exactly one job per attempt.
    assert older_nzo in lab.sab.queue
    assert len(lab.sab.submissions) == 2


@pytest.mark.asyncio
async def test_an_older_attempts_job_is_never_observed_as_a_newer_attempts(lab):
    candidate, plan = subject_and_plan(lab.root)
    def request(attempt):
        return ExecutionRequest(ExecutionWork(ExecutionSubject.of(candidate), plan, attempt), attempt)

    first_request = request("attempt-A")
    first_bound = (await lab.executor.start(first_request, lab.executor.prepare(first_request))).handle

    # attempt-B is prepared but never started: it owns no native job.
    second_handle = lab.executor.prepare(request("attempt-B"))
    observed = (await lab.executor.observe_many((second_handle,))).observations[0]
    assert observed.state == ExecutionState.ABSENT
    assert observed.handle.native is None
    assert first_bound.native["nzo_id"] in lab.sab.queue
