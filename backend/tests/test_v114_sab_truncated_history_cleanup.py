"""DP 1.0.14: a SABnzbd job missing from a truncated history page is resolved
by its exact identity, so cleanup of a deleted execution can converge.

SAB pages its history (DP reads one 500-row page). Once SAB holds more, a
bound job that is in neither the complete queue nor that page used to stay
UNKNOWN ("SAB listing was not authoritative") forever: the deleted attempt's
cleanup never finished and kept SAB's bandwidth reservation. Now the job's
exact ``nzo_id`` is read (live history, then archive); a hit is reported as
SAB has it, and only a negative answer whose scope is proven -- confirmed again
``ABSENCE_CONFIRMATION_SECONDS`` later, past SAB's queue -> post-processing
hand-off -- is ABSENT. Every uncertain answer stays UNKNOWN.

All identifiers are synthetic. The fake lists history in insertion order and
truncates the bulk page to its first ``truncate_history_to`` slots, so a job
inserted after the fillers is "beyond the page".
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from executors.sabnzbd import executor as sab_executor
from executors.sabnzbd.client import SabApiError, SabEndpoint, SabTransportError, SabnzbdClient
from transfers.models import ExecutionState

from sab_fakes import FakeJob, FakeSab
from test_v113_sabnzbd_executor import build, request_for

PAGE = 500


class OrderedSab(FakeSab):
    """Records every native read in order: ``(kind, nzo_id or None, archive)``.
    ``before_exact`` runs just before an exact-id read is answered, so a test
    can change native state between two reads deterministically."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls: list[tuple] = []
        self.before_exact = None
        self.after_queue_snapshot = None

    async def queue_snapshot(self, limit=500):
        snapshot = await super().queue_snapshot(limit)
        self.calls.append(("queue_snapshot", None, False))
        if self.after_queue_snapshot is not None:
            hook, self.after_queue_snapshot = self.after_queue_snapshot, None
            hook()
        return snapshot

    async def history_snapshot(self, limit=500):
        self.calls.append(("history_snapshot", None, False))
        return await super().history_snapshot(limit)

    async def history_entry(self, nzo_id, *, archive=False):
        self.calls.append(("history_entry", nzo_id, archive))
        if self.before_exact is not None:
            self.before_exact(nzo_id, archive)
        return await super().history_entry(nzo_id, archive=archive)

    async def queue_slots(self, search=None):
        self.calls.append(("queue_slots", search, False))
        return await super().queue_slots(search)

    async def history_slots(self, search=None):
        self.calls.append(("history_slots", search, False))
        return await super().history_slots(search)

    async def delete(self, nzo_id):
        self.calls.append(("delete", nzo_id, False))
        return await super().delete(nzo_id)

    def exact(self):
        return [call for call in self.calls if call[0] == "history_entry"]


@pytest.fixture
def lab(tmp_path):
    root = tmp_path / "download"
    (root / ".dpwork" / "complete").mkdir(parents=True)
    (root / ".dpwork" / "incomplete").mkdir(parents=True)
    sab = OrderedSab(complete_dir=str(root / ".dpwork" / "complete"),
                     download_dir=str(root / ".dpwork" / "incomplete"))
    executor = build(sab, root=str(root))
    clock = SimpleNamespace(now=1000.0)
    executor._clock = lambda: clock.now
    return SimpleNamespace(sab=sab, root=str(root), executor=executor, clock=clock)


async def started(lab_, count=1):
    handles = []
    for index in range(count):
        request = request_for(lab_.root, name=f"job-{index}", attempt=f"attempt-{index}")
        handles.append((await lab_.executor.start(request, lab_.executor.prepare(request))).handle)
    return handles


def history_of(sab, entries=548, *, page=PAGE):
    """SAB holding ``entries`` unrelated finished jobs; DP's bulk page shows ``page``."""
    for index in range(entries):
        sab.history[f"filler{index:04d}"] = FakeJob(f"filler{index:04d}", f"old-{index}", status="Completed")
    sab.truncate_history_to = page


def vanish(sab, handle):
    """The job is in neither SAB listing (as the seven probed jobs were)."""
    nzo_id = handle.native["nzo_id"]
    sab.queue.pop(nzo_id, None)
    sab.history.pop(nzo_id, None)
    return nzo_id


async def observe(lab_, handles):
    lab_.sab.calls.clear()
    return {o.handle.attempt_id: o for o in (await lab_.executor.observe_many(tuple(handles))).observations}


# --- the incident: 548 history entries, a 500-row page, jobs gone natively ---

@pytest.mark.asyncio
async def test_seven_jobs_absent_beyond_a_truncated_history_become_absent_once_confirmed(lab):
    handles = await started(lab, 7)
    for handle in handles:
        vanish(lab.sab, handle)
    history_of(lab.sab)
    first = await observe(lab, handles)
    # Nothing is concluded from one negative read: UNKNOWN, awaiting confirmation.
    assert {o.state for o in first.values()} == {ExecutionState.UNKNOWN}
    assert {o.error.diagnostic for o in first.values()} == {"SAB absence not yet confirmed"}
    # Exactly: one complete queue snapshot, then the history page, then each
    # job's exact id (history, then archive) -- queue first, always.
    assert lab.sab.calls[0] == ("queue_snapshot", None, False)
    assert lab.sab.calls[1] == ("history_snapshot", None, False)
    assert len(lab.sab.exact()) == 14
    lab.clock.now += sab_executor.ABSENCE_CONFIRMATION_SECONDS
    second = await observe(lab, handles)
    assert {o.state for o in second.values()} == {ExecutionState.ABSENT}


@pytest.mark.asyncio
async def test_a_job_in_history_beyond_the_page_is_found_by_its_exact_id(lab):
    (handle,) = await started(lab)
    nzo_id = handle.native["nzo_id"]
    history_of(lab.sab)
    lab.sab.finish(nzo_id)                                   # completes: now history entry 549
    assert list(lab.sab.history).index(nzo_id) >= PAGE
    observation = (await observe(lab, [handle]))[handle.attempt_id]
    assert observation.state == ExecutionState.SUCCEEDED
    assert observation.handle.native["nzo_id"] == nzo_id        # its own identity, nothing adopted
    assert lab.sab.exact() == [("history_entry", nzo_id, False)]
    assert not any(call[0] in {"queue_slots", "history_slots"} for call in lab.sab.calls)


@pytest.mark.asyncio
async def test_an_archived_job_is_found_never_absent(lab):
    (handle,) = await started(lab)
    nzo_id = handle.native["nzo_id"]
    history_of(lab.sab)
    lab.sab.finish(nzo_id)
    lab.sab.archive[nzo_id] = lab.sab.history.pop(nzo_id)       # archived: hidden from the default history
    observation = (await observe(lab, [handle]))[handle.attempt_id]
    assert observation.state == ExecutionState.SUCCEEDED
    assert lab.sab.exact() == [("history_entry", nzo_id, False), ("history_entry", nzo_id, True)]


@pytest.mark.asyncio
async def test_jobs_in_the_queue_or_on_the_page_need_no_exact_read(lab):
    queued, paged = await started(lab, 2)
    lab.sab.finish(paged.native["nzo_id"])                      # first history entry: on the page
    history_of(lab.sab)
    observations = await observe(lab, [queued, paged])
    assert observations[queued.attempt_id].state in {ExecutionState.QUEUED, ExecutionState.RUNNING}
    assert observations[paged.attempt_id].state == ExecutionState.SUCCEEDED
    assert lab.sab.exact() == []


@pytest.mark.asyncio
async def test_complete_listings_keep_the_bulk_fast_path(lab):
    handles = await started(lab, 6)
    vanish(lab.sab, handles[0])                                  # complete queue + history: plain ABSENT
    observations = await observe(lab, handles)
    assert observations[handles[0].attempt_id].state == ExecutionState.ABSENT
    assert [call[0] for call in lab.sab.calls] == ["queue_snapshot", "history_snapshot"]


# --- inter-read ordering ------------------------------------------------------

@pytest.mark.asyncio
async def test_a_job_seen_in_the_queue_stays_found_when_it_moves_to_history_before_the_history_read(lab):
    moving, missing = await started(lab, 2)
    history_of(lab.sab)
    vanish(lab.sab, missing)                                     # B: makes the history read happen
    moving_id = moving.native["nzo_id"]

    def move_to_history_beyond_the_page():
        job = lab.sab.queue.pop(moving_id)
        job.status = "Completed"
        lab.sab.history[moving_id] = job

    lab.sab.after_queue_snapshot = move_to_history_beyond_the_page
    observations = await observe(lab, [moving, missing])
    # A was in the complete queue snapshot: found from it, never absent, and
    # no exact read is spent on it.
    assert observations[moving.attempt_id].state in {ExecutionState.QUEUED, ExecutionState.RUNNING}
    assert all(call[1] != moving_id for call in lab.sab.exact())
    assert [call[0] for call in lab.sab.calls[:2]] == ["queue_snapshot", "history_snapshot"]


@pytest.mark.asyncio
async def test_a_job_reaching_history_before_its_exact_read_is_reported_not_absent(lab):
    (handle,) = await started(lab)
    history_of(lab.sab)
    nzo_id = handle.native["nzo_id"]
    job = lab.sab.queue.pop(nzo_id)                              # in neither listing for the bulk reads

    def lands_in_history(requested, archive):
        if requested == nzo_id:
            lab.sab.queue[nzo_id] = job
            lab.sab.finish(nzo_id)                               # finished: history, beyond the page
            lab.sab.before_exact = None

    lab.sab.before_exact = lands_in_history
    observation = (await observe(lab, [handle]))[handle.attempt_id]
    assert observation.state == ExecutionState.SUCCEEDED
    assert lab.sab.calls == [("queue_snapshot", None, False), ("history_snapshot", None, False),
                             ("history_entry", nzo_id, False)]


@pytest.mark.asyncio
async def test_a_job_only_late_visible_in_the_queue_is_never_declared_absent(lab):
    """Queue-first then history does not exclude a job that is listed in the
    queue only AFTER the snapshot (SAB's hand-offs): one negative is never
    absence, and a job seen again resets the confirmation."""
    (handle,) = await started(lab)
    history_of(lab.sab)
    nzo_id = vanish(lab.sab, handle)
    hidden = FakeJob(nzo_id, "job-0")
    first = (await observe(lab, [handle]))[handle.attempt_id]
    assert first.state == ExecutionState.UNKNOWN
    lab.sab.queue[nzo_id] = hidden                               # now listed
    lab.clock.now += sab_executor.ABSENCE_CONFIRMATION_SECONDS
    second = (await observe(lab, [handle]))[handle.attempt_id]
    assert second.state in {ExecutionState.QUEUED, ExecutionState.RUNNING}
    lab.sab.queue.pop(nzo_id)                                    # gone again: confirmation starts over
    lab.clock.now += sab_executor.ABSENCE_CONFIRMATION_SECONDS
    third = (await observe(lab, [handle]))[handle.attempt_id]
    assert third.state == ExecutionState.UNKNOWN


# --- fail closed ------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_incomplete_queue_never_yields_absence_from_history_alone(lab):
    handles = await started(lab, 3)
    nzo_id = vanish(lab.sab, handles[0])
    history_of(lab.sab)
    lab.sab.truncate_queue_to = 1
    for _ in range(2):
        observation = (await observe(lab, handles))[handles[0].attempt_id]
        assert observation.state == ExecutionState.UNKNOWN
        lab.clock.now += sab_executor.ABSENCE_CONFIRMATION_SECONDS
    assert lab.sab.exact() == []
    # The single-handle path (cancel/observe) refuses the same way.
    single = await lab.executor._observe(handles[0])
    assert single.state == ExecutionState.UNKNOWN and nzo_id


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [SabTransportError("SAB is unreachable"),
                                     SabApiError("SAB did not apply the history id filter")])
async def test_an_exact_read_that_cannot_answer_is_unknown_and_never_confirms(lab, failure):
    (handle,) = await started(lab)
    history_of(lab.sab)
    vanish(lab.sab, handle)

    def fails(requested, archive):
        raise failure

    lab.sab.before_exact = fails
    for _ in range(3):
        observation = (await observe(lab, [handle]))[handle.attempt_id]
        assert observation.state == ExecutionState.UNKNOWN
        lab.clock.now += sab_executor.ABSENCE_CONFIRMATION_SECONDS
    assert not lab.executor._unconfirmed_absence


@pytest.mark.asyncio
async def test_an_unbound_handle_gets_no_exact_read_and_no_absence(lab):
    (handle,) = await started(lab)
    unbound = type(handle)(handle.executor_id, handle.attempt_id, handle.correlation, None)
    vanish(lab.sab, handle)
    history_of(lab.sab)
    for _ in range(2):
        observation = (await observe(lab, [unbound]))[handle.attempt_id]
        assert observation.state == ExecutionState.UNKNOWN
        assert observation.handle.native is None                  # no identity adopted
        lab.clock.now += sab_executor.ABSENCE_CONFIRMATION_SECONDS
    assert lab.sab.exact() == []


@pytest.mark.asyncio
async def test_exact_reads_per_batch_are_bounded(lab):
    handles = await started(lab, 10)
    for handle in handles:
        vanish(lab.sab, handle)
    history_of(lab.sab)
    observations = await observe(lab, handles)
    assert len({call[1] for call in lab.sab.exact()}) == sab_executor._EXACT_LOOKUPS_PER_BATCH
    diagnostics = [o.error.diagnostic for o in observations.values()]
    assert diagnostics.count("SAB listing was not authoritative") == 10 - sab_executor._EXACT_LOOKUPS_PER_BATCH


# --- the client: only a proven-scope answer is "not found" ------------------

class _Recorded:
    def __init__(self, status, body, sent):
        self.status, self._body, self._sent = status, body, sent

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, body, sent):
        self._body, self._sent = body, sent

    def post(self, url, data=None, headers=None):
        self._sent.append({key: values[0] for key, values in parse_qs(data.decode()).items()})
        return _Recorded(200, self._body, self._sent)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def client_answering(history, sent):
    body = json.dumps({"history": history})
    return SabnzbdClient(SabEndpoint("http://sab.invalid:8090", "synthetic-key", 5),
                         session_factory=lambda: _Session(body, sent))


@pytest.mark.asyncio
async def test_a_scoped_empty_answer_is_not_found_and_asks_exactly_for_the_id():
    sent = []
    client = client_answering({"slots": [], "noofslots": 0}, sent)
    assert await client.history_entry("nzo-synthetic-1") is None
    assert await client.history_entry("nzo-synthetic-1", archive=True) is None
    assert sent[0]["mode"] == "history" and sent[0]["nzo_ids"] == "nzo-synthetic-1" and "archive" not in sent[0]
    assert sent[1]["archive"] == "1"


@pytest.mark.asyncio
async def test_a_scoped_hit_is_returned():
    client = client_answering({"slots": [{"nzo_id": "nzo-synthetic-1", "status": "Completed"}], "noofslots": 1}, [])
    assert (await client.history_entry("nzo-synthetic-1"))["status"] == "Completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [
    {"slots": [{"nzo_id": "nzo-other", "status": "Completed"}], "noofslots": 548},    # filter ignored
    {"slots": [], "noofslots": -1},                                                    # native count failed
    {"slots": []},                                                                     # scope not reported
    {"slots": [], "noofslots": "many"},
    {"slots": [], "noofslots": 3},                                                     # partial
    {"slots": [{"nzo_id": "nzo-synthetic-1"}], "noofslots": 2},
])
async def test_an_answer_without_proven_scope_is_an_api_failure_not_absence(history):
    with pytest.raises(SabApiError):
        await client_answering(history, []).history_entry("nzo-synthetic-1")


@pytest.mark.asyncio
async def test_an_exact_read_needs_an_id():
    with pytest.raises(SabApiError):
        await client_answering({"slots": [], "noofslots": 0}, []).history_entry("")


# --- the core: cleanup completes and SAB's reservation is released -----------

@pytest.mark.asyncio
async def test_a_deleted_execution_beyond_the_page_completes_cleanup_and_releases_the_reservation(
        tmp_path, monkeypatch):
    import asyncio
    import db.database as database
    from executors.sabnzbd.executor import SabnzbdConfiguration, SabnzbdExecutor
    from providers.usenet.provider import UsenetProvider
    from sab_fakes import staged_store
    from transfers.convergence_engine import TransferEngine
    from transfers.policy import TransferPolicy
    from transfers.recovery_repository import TransferRepository
    from transfers.registry import IntegrationRegistry
    from application.service import ApplicationService

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "scratch.db")
    await database.init_db()
    repository = TransferRepository()
    root = tmp_path / "payloads"
    (root / ".dpwork" / "complete").mkdir(parents=True)
    (root / ".dpwork" / "incomplete").mkdir(parents=True)
    sab = OrderedSab(complete_dir=str(root / ".dpwork" / "complete"),
                     download_dir=str(root / ".dpwork" / "incomplete"))
    executor = SabnzbdExecutor(
        sab, SabnzbdConfiguration(local_root=str(root), working_directory=str(root / ".dpwork"),
                                  complete_directory=str(root / ".dpwork" / "complete")),
        repository.authorize_execution, staged_input=staged_store())
    clock = SimpleNamespace(now=1000.0)
    executor._clock = lambda: clock.now
    registry = IntegrationRegistry()
    registry.register_provider(UsenetProvider(staged_input=staged_store()))
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(root),
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                                                  max_active_executions=2),
                            clock=lambda: clock.now)
    await engine.initialize()
    nzb = (b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
           b'<file poster="p@e.net" date="1700000000" subject="x [1/1] - &quot;x.bin&quot; yEnc (1/1)">'
           b"<groups><group>alt.binaries.test</group></groups>"
           b'<segments><segment bytes="1024" number="1">a@e.net</segment></segments>'
           b"</file></nzb>")
    transfer_id = int((await ApplicationService(engine).submit_nzb(nzb, "posting.nzb"))["id"])
    for _ in range(6):
        await engine.tick()
        await asyncio.sleep(0)
    assert sab.queue, "the job reached SAB"
    assert "sabnzbd" in await repository.executors_with_live_work()

    for nzo_id in list(sab.queue):                               # gone natively, as probed
        sab.queue.pop(nzo_id)
    history_of(sab)
    await engine.delete(transfer_id)

    async def cleanup_row():
        async with database.get_db() as db:
            return await db.fetchone("SELECT cleanup_state, authorized, cleanup_attempts, cleanup_error "
                                     "FROM execution_attempts WHERE transfer_id=? AND executor_id='sabnzbd'",
                                     (transfer_id,))

    row = await cleanup_row()
    assert row["cleanup_state"] == "pending" and row["authorized"] == 1       # one negative: not yet absence
    assert "sabnzbd" in await repository.executors_with_live_work()
    for _ in range(3):                                            # the ordinary cleanup cadence
        clock.now += 31.0
        await engine.reconcile_executions()
    row = await cleanup_row()
    assert row["cleanup_state"] == "complete" and row["authorized"] == 0
    assert row["cleanup_attempts"] == 0                           # nothing was cancelled natively
    assert "sabnzbd" not in await repository.executors_with_live_work()
    assert "sabnzbd" not in engine.runtime.reserved               # released on positive native truth



# --- revision 2: a listing is complete only when SAB proves its coverage -----

def _snapshot_client(section_name, section):
    body = json.dumps({section_name: section})
    return SabnzbdClient(SabEndpoint("http://sab.invalid:8090", "synthetic-key", 5),
                         session_factory=lambda: _Session(body, []))


SLOT = {"nzo_id": "nzo-synthetic-1", "filename": "job", "status": "Downloading"}


@pytest.mark.asyncio
@pytest.mark.parametrize("section,complete", [
    ({"slots": [], "noofslots_total": 0, "noofslots": 0}, True),
    ({"slots": [SLOT], "noofslots_total": 1, "noofslots": "1"}, True),
    ({"slots": [], "noofslots_total": -1, "noofslots": -1}, False),       # failed native count
    ({"slots": [], "noofslots": -1}, False),
    ({"slots": []}, False),                                                # coverage not reported
    ({"slots": [], "noofslots_total": "many", "noofslots": 0}, False),     # malformed
    ({"slots": [], "noofslots_total": True, "noofslots": 0}, False),
    ({"slots": [], "noofslots_total": 0.5, "noofslots": 0}, False),
    ({"slots": [], "noofslots_total": 3, "noofslots": 0}, False),          # conflicting counts
    ({"slots": [], "noofslots_total": 0, "noofslots": 3}, False),          # conflicting, total read first
    ({"slots": [SLOT], "noofslots_total": 0, "noofslots": 0}, False),      # fewer than returned
    ({"slots": [SLOT], "noofslots_total": 548, "noofslots": 548}, False),  # a page of a longer queue
])
async def test_queue_coverage_is_proven_never_assumed(section, complete):
    assert (await _snapshot_client("queue", section).queue_snapshot()).complete is complete


@pytest.mark.asyncio
@pytest.mark.parametrize("section,complete", [
    ({"slots": [], "noofslots": 0}, True),
    ({"slots": [], "noofslots": -1}, False),
    ({"slots": []}, False),
    ({"slots": [], "noofslots": "x"}, False),
    ({"slots": [SLOT], "noofslots": 548}, False),
])
async def test_history_coverage_is_proven_never_assumed(section, complete):
    assert (await _snapshot_client("history", section).history_snapshot()).complete is complete


class _RoutedSession:
    """One scripted SAB: the queue answer, the bulk history page and the
    exact-id history answer, chosen by the request's own form fields."""

    def __init__(self, routes, sent):
        self._routes, self._sent = routes, sent

    def post(self, url, data=None, headers=None):
        fields = {key: values[0] for key, values in parse_qs(data.decode()).items()}
        self._sent.append(fields)
        if fields.get("mode") == "queue":
            body = {"queue": self._routes["queue"]}
        elif fields.get("nzo_ids"):
            body = {"history": self._routes["exact"]}
        else:
            body = {"history": self._routes["history"]}
        return _Recorded(200, json.dumps(body), self._sent)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("queue", [
    {"slots": [], "noofslots_total": 3, "noofslots": 0},                   # conflicting
    {"slots": [], "noofslots_total": 0, "noofslots": 3},                   # conflicting, total read first
    {"slots": [], "noofslots_total": -1, "noofslots": -1},                 # negative
    {"slots": []},                                                          # missing
    {"slots": [], "noofslots_total": "?", "noofslots": 0},                 # malformed
])
async def test_a_queue_without_proven_coverage_never_yields_absence_even_after_two_exact_negatives(tmp_path, queue):
    from transfers.models import ExecutionHandle
    from executors.sabnzbd.translation import EXECUTOR_ID
    sent = []
    fillers = [{"nzo_id": f"filler{index:04d}", "name": f"old-{index}", "status": "Completed"} for index in range(PAGE)]
    routes = {"queue": queue, "history": {"slots": fillers, "noofslots": 548},
              "exact": {"slots": [], "noofslots": 0}}
    client = SabnzbdClient(SabEndpoint("http://sab.invalid:8090", "synthetic-key", 5),
                           session_factory=lambda: _RoutedSession(routes, sent))
    executor = build(client, root=str(tmp_path))
    clock = SimpleNamespace(now=1000.0)
    executor._clock = lambda: clock.now
    handle = ExecutionHandle(EXECUTOR_ID, "attempt-synthetic", {"token": "dp-synthetic-token"},
                             {"nzo_id": "nzo-synthetic-1"})
    for _ in range(2):
        bulk = (await executor.observe_many((handle,))).observations[0]
        single = await executor._observe(handle)                 # the cancel/observe path
        assert bulk.state == ExecutionState.UNKNOWN and single.state == ExecutionState.UNKNOWN
        clock.now += sab_executor.ABSENCE_CONFIRMATION_SECONDS
    # The single-job path did read the exact id twice (history + archive each
    # time) and still refused: a negative history never outweighs an
    # unproven queue. The bulk path did not even ask.
    exact = [fields for fields in sent if fields.get("nzo_ids")]
    assert len(exact) == 4 and {fields["nzo_ids"] for fields in exact} == {"nzo-synthetic-1"}


# --- revision 2: the single-job path and cancellation across the hand-off ----

@pytest.mark.asyncio
async def test_a_bound_job_between_queue_and_post_processing_is_not_absent_to_a_single_observation(lab):
    (handle,) = await started(lab)
    nzo_id = handle.native["nzo_id"]
    job = lab.sab.queue.pop(nzo_id)                              # removed from the queue, not yet listed
    first = await lab.executor._observe(handle)
    assert first.state == ExecutionState.UNKNOWN
    assert first.error.diagnostic == "SAB absence not yet confirmed"
    lab.sab.queue[nzo_id] = job
    lab.sab.finish(nzo_id)                                        # post-processing done: in history
    lab.clock.now += sab_executor.ABSENCE_CONFIRMATION_SECONDS
    second = await lab.executor._observe(handle)
    assert second.state == ExecutionState.SUCCEEDED and second.handle.native["nzo_id"] == nzo_id


@pytest.mark.asyncio
async def test_a_single_observation_of_a_job_really_gone_is_absent_once_confirmed(lab):
    (handle,) = await started(lab)
    vanish(lab.sab, handle)
    assert (await lab.executor._observe(handle)).state == ExecutionState.UNKNOWN
    lab.clock.now += sab_executor.ABSENCE_CONFIRMATION_SECONDS
    assert (await lab.executor._observe(handle)).state == ExecutionState.ABSENT


@pytest.mark.asyncio
async def test_cancelling_a_job_in_the_hand_off_issues_no_delete_and_claims_nothing(lab):
    (handle,) = await started(lab)
    nzo_id = handle.native["nzo_id"]
    job = lab.sab.queue.pop(nzo_id)                              # mid hand-off when cancel observes
    lab.sab.calls.clear()
    result = await lab.executor.cancel(handle)
    assert result.state == ExecutionState.UNKNOWN                # not ABSENT: cleanup is not completed on it
    assert not [call for call in lab.sab.calls if call[0] == "delete"]
    lab.sab.queue[nzo_id] = job
    lab.sab.finish(nzo_id)
    lab.clock.now += sab_executor.ABSENCE_CONFIRMATION_SECONDS
    assert (await lab.executor._observe(handle)).state == ExecutionState.SUCCEEDED


@pytest.mark.asyncio
async def test_an_ordinary_cancel_is_still_confirmed_at_once_by_sabs_own_removal(lab):
    (handle,) = await started(lab)
    nzo_id = handle.native["nzo_id"]
    lab.sab.calls.clear()
    result = await lab.executor.cancel(handle)
    assert result.state == ExecutionState.CANCELLED              # no confirmation wait: SAB named it removed
    assert ("delete", nzo_id, False) in lab.sab.calls
    assert nzo_id not in lab.sab.queue
