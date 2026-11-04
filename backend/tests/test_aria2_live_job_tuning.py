"""1.0.14: saved Network Sources tuning reaches existing DP-owned aria2 jobs.

``Aria2Executor.apply_tuning`` changes one owned, current, live job in place
(``aria2.changeOption``, only what differs) and reports what it observed;
``Aria2Administration.converge_job_tuning`` selects the jobs from the core's
own execution truth and applies them one at a time under the daemon's tuning
lock. The packaged-aria2 proofs are in test_aria2_live_job_tuning_runtime.py.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from executors.aria2.admin import Aria2Administration
from executors.aria2.client import Aria2ResponseError, Aria2Service
from executors.aria2.definition import Aria2Options
from executors.aria2.executor import Aria2Configuration, Aria2Executor, TuningOutcome
from executors.aria2.runtime import TuningState
from transfers.models import ExecutionAttempt, ExecutionHandle

pytestmark = pytest.mark.asyncio
TUNING = ("max-connection-per-server", "split", "min-split-size")


class FakeDaemon(Aria2Service):
    """aria2's per-job truth for the RPCs ``apply_tuning`` uses, recording
    every call. ``jobs``: gid -> {"status", "options"}."""

    def __init__(self, jobs):
        super().__init__("http://aria2.invalid/jsonrpc", "", 5)
        self.jobs = jobs
        self.calls = []
        self.sent = []
        self.on_change = None
        self.on_read = None
        self.change_raises = None
        self.after_change = None

    async def _call(self, method, params=None, **_kwargs):
        params = list(params or [])
        self.calls.append((method, params[0] if params else None))
        gid = params[0] if params else None
        if gid not in self.jobs:
            raise Aria2ResponseError(f"aria2 [1]: GID {gid} is not found", code=1)
        job = self.jobs[gid]
        if method == "aria2.tellStatus":
            return {"gid": gid, "status": job["status"], "totalLength": "100", "completedLength": "10",
                    "downloadSpeed": "0"}
        if method == "aria2.getOption":
            if self.on_read is not None:
                await self.on_read(gid)
            # The real map carries the guarded route too; it must stay unread.
            return {**job["options"], "all-proxy-passwd": "never-returned"}
        if method == "aria2.changeOption":
            self.sent.append((gid, dict(params[1])))
            if self.on_change is not None:
                await self.on_change(gid, params[1])
            if self.change_raises is not None and not isinstance(self.change_raises, asyncio.TimeoutError):
                raise self.change_raises
            job["options"].update(params[1])
            if self.after_change is not None:
                job["status"] = self.after_change
            if self.change_raises is not None:
                raise self.change_raises
            return "OK"
        raise AssertionError(f"unexpected aria2 call {method}")

    def changes(self):
        return [call for call in self.calls if call[0] == "aria2.changeOption"]


def held(split="16", connections="16", minimum="10485760"):
    return {"split": split, "max-connection-per-server": connections, "min-split-size": minimum}


def world(tmp_path, jobs, *, split=5, connections=4, minimum="64M", authorize=None):
    daemon = FakeDaemon(jobs)

    async def allowed(_handle, _action):
        return True

    executor = Aria2Executor(daemon, Aria2Configuration(str(tmp_path), split=split, minimum_split_size=minimum,
                                                        connections_per_server=connections,
                                                        confirmation_delay=0.01, control_confirmation_timeout=0.2),
                             authorize or allowed)
    return executor, daemon


def handle(executor, tmp_path, gid, attempt="a1", *, binding=None):
    return ExecutionHandle("aria2", attempt, {"target": str(Path(tmp_path) / f"{attempt}.bin"),
                                             "binding": binding or executor.binding}, {"gid": gid})


GID = "0123456789abcdef"


async def apply(executor, item, seconds=5.0):
    return await executor.apply_tuning(item, deadline=asyncio.get_running_loop().time() + seconds)


@pytest.mark.parametrize("status", ["active", "waiting", "paused"])
async def test_a_live_owned_job_takes_only_the_changed_tuning_in_place(tmp_path, status):
    jobs = {GID: {"status": status, "options": held()}}
    executor, daemon = world(tmp_path, jobs, split=5, connections=16, minimum="10M")
    assert await apply(executor, handle(executor, tmp_path, GID)) == TuningOutcome.APPLIED
    # Exactly this GID, exactly the one option that differs (10M is 10485760).
    assert daemon.changes() == [("aria2.changeOption", GID)]
    assert jobs[GID]["options"] == held(split="5")
    # Never paused, unpaused, removed or resubmitted; it stays where it was.
    assert {call[0] for call in daemon.calls} <= {"aria2.tellStatus", "aria2.getOption", "aria2.changeOption"}
    assert jobs[GID]["status"] == status


async def test_all_three_controls_change_together_and_sizes_compare_by_value(tmp_path):
    jobs = {GID: {"status": "active", "options": held()}}
    executor, daemon = world(tmp_path, jobs, split=5, connections=4, minimum="64M")
    seen = []

    async def record(_gid, options):
        seen.append(dict(options))

    daemon.on_change = record
    assert await apply(executor, handle(executor, tmp_path, GID)) == TuningOutcome.APPLIED
    assert seen == [{"max-connection-per-server": "4", "split": "5", "min-split-size": "64M"}]


async def test_a_job_that_already_holds_the_tuning_is_sent_nothing(tmp_path):
    jobs = {GID: {"status": "active", "options": held(split="5", connections="4", minimum=str(64 << 20))}}
    executor, daemon = world(tmp_path, jobs)
    assert await apply(executor, handle(executor, tmp_path, GID)) == TuningOutcome.UNCHANGED
    assert daemon.changes() == []


@pytest.mark.parametrize("status", ["complete", "error", "removed"])
async def test_a_job_that_is_no_longer_live_is_never_changed(tmp_path, status):
    jobs = {GID: {"status": status, "options": held()}}
    executor, daemon = world(tmp_path, jobs)
    assert await apply(executor, handle(executor, tmp_path, GID)) == TuningOutcome.NOT_LIVE
    # (A failed job's observation reads its options only for redaction.)
    assert daemon.changes() == [] and jobs[GID]["options"] == held()


async def test_an_absent_job_is_gone_not_updated(tmp_path):
    executor, daemon = world(tmp_path, {})
    assert await apply(executor, handle(executor, tmp_path, GID)) == TuningOutcome.GONE
    assert daemon.changes() == []


async def test_a_job_that_is_not_the_current_authorized_execution_is_never_touched(tmp_path):
    jobs = {GID: {"status": "active", "options": held()}}
    asked = []

    async def current_only(_handle, action):
        asked.append(action)
        return action == "observe"

    executor, daemon = world(tmp_path, jobs, authorize=current_only)
    assert await apply(executor, handle(executor, tmp_path, GID)) == TuningOutcome.NOT_OWNED
    assert daemon.calls == [] and "pause" in asked
    # Another daemon/root binding, or an arbitrary GID shape: refused before any RPC.
    executor, daemon = world(tmp_path, jobs)
    assert await apply(executor, handle(executor, tmp_path, GID, binding="0" * 64)) == TuningOutcome.NOT_OWNED
    assert await apply(executor, handle(executor, tmp_path, "not-a-gid")) == TuningOutcome.NOT_OWNED
    assert daemon.calls == []


async def test_authority_revoked_while_the_job_was_read_stops_the_change(tmp_path):
    jobs = {GID: {"status": "active", "options": held()}}
    pauses = []

    async def revoked_after_first(_handle, action):
        if action == "pause":
            pauses.append(action)
            return len(pauses) == 1
        return True

    executor, daemon = world(tmp_path, jobs, authorize=revoked_after_first)
    assert await apply(executor, handle(executor, tmp_path, GID)) == TuningOutcome.NOT_OWNED
    assert daemon.changes() == [] and jobs[GID]["options"] == held()


async def test_aria2s_own_refusal_is_reported_and_never_retried(tmp_path):
    jobs = {GID: {"status": "active", "options": held()}}
    executor, daemon = world(tmp_path, jobs)
    daemon.change_raises = Aria2ResponseError("aria2 [1]: We encountered a problem while processing the option")
    assert await apply(executor, handle(executor, tmp_path, GID)) == TuningOutcome.REFUSED
    assert len(daemon.changes()) == 1 and jobs[GID]["options"] == held()


async def test_a_lost_answer_is_decided_by_the_read_back_and_never_repeated(tmp_path):
    # The change reached aria2, but its answer was lost: proven by the read-back.
    jobs = {GID: {"status": "active", "options": held()}}
    executor, daemon = world(tmp_path, jobs)
    daemon.change_raises = asyncio.TimeoutError()
    assert await apply(executor, handle(executor, tmp_path, GID)) == TuningOutcome.APPLIED
    assert len(daemon.changes()) == 1
    # It never reached aria2: the read-back cannot prove it, so it stays uncertain.
    jobs = {GID: {"status": "active", "options": held()}}
    executor, daemon = world(tmp_path, jobs)
    daemon.change_raises = ConnectionResetError()
    assert await apply(executor, handle(executor, tmp_path, GID)) == TuningOutcome.UNCERTAIN
    assert len(daemon.changes()) == 1 and jobs[GID]["options"] == held()


async def test_a_job_that_disappears_or_stops_after_the_change_is_not_reported_applied(tmp_path):
    jobs = {GID: {"status": "active", "options": held()}}
    executor, daemon = world(tmp_path, jobs)

    async def vanish(gid, _options):
        jobs.pop(gid)

    daemon.on_change = vanish
    assert await apply(executor, handle(executor, tmp_path, GID)) == TuningOutcome.GONE
    jobs = {GID: {"status": "active", "options": held()}}
    executor, daemon = world(tmp_path, jobs)
    daemon.after_change = "error"
    assert await apply(executor, handle(executor, tmp_path, GID)) == TuningOutcome.INTERRUPTED


async def test_a_daemon_that_cannot_be_asked_is_unavailable(tmp_path):
    executor, daemon = world(tmp_path, {})

    async def down(*_args, **_kwargs):
        raise ConnectionResetError("connection refused")

    daemon._call = down
    assert await apply(executor, handle(executor, tmp_path, GID)) == TuningOutcome.UNAVAILABLE


# ── selection and ordering (Aria2Administration.converge_job_tuning) ────────

class Repository:
    def __init__(self, attempts, current):
        self.attempts, self.current, self.asked = attempts, current, []

    async def live_executions(self):
        return tuple(self.attempts)

    async def authorize_executions(self, handles, action):
        self.asked.append((tuple(item.attempt_id for item in handles), action))
        return [item.attempt_id in self.current for item in handles]


def attempt(item):
    return ExecutionAttempt(item, 1, 1, "running")


async def test_only_the_cores_current_owned_executions_are_selected_never_the_daemons_list(tmp_path):
    gids = {name: f"{index:016x}" for index, name in enumerate(("mine", "old", "other", "foreign", "unlisted"), 1)}
    jobs = {gid: {"status": "active", "options": held()} for gid in gids.values()}
    executor, daemon = world(tmp_path, jobs)
    mine = handle(executor, tmp_path, gids["mine"], "mine")
    old = handle(executor, tmp_path, gids["old"], "old")
    other = ExecutionHandle("rsync", "other", {"target": str(tmp_path / "o")}, {"gid": gids["other"]})
    foreign = handle(executor, tmp_path, gids["foreign"], "foreign", binding="f" * 64)
    repository = Repository([attempt(item) for item in (mine, old, other, foreign)], current={"mine"})
    admin = Aria2Administration(executor, repository, None, SimpleNamespace())
    result = await admin.converge_job_tuning()
    # The one canonical predicate decides "current" for this daemon's own jobs.
    assert repository.asked == [(("mine", "old"), "pause")]
    assert [call[1] for call in daemon.changes()] == [gids["mine"]]
    assert jobs[gids["unlisted"]]["options"] == held() and jobs[gids["foreign"]]["options"] == held()
    assert result["selected"] == 1 and result["applied"] == 1 and result["not_owned"] == 1


async def test_jobs_are_changed_one_at_a_time_and_terminal_ones_are_not_counted(tmp_path):
    jobs = {f"{index:016x}": {"status": status, "options": held()}
            for index, status in enumerate(("active", "active", "paused", "complete"), 1)}
    executor, daemon = world(tmp_path, jobs)
    handles = [handle(executor, tmp_path, gid, f"a{index}") for index, gid in enumerate(jobs)]
    running = []

    async def one_at_a_time(_gid, _options):
        running.append(1)
        assert len(running) == 1
        await asyncio.sleep(0.01)
        running.pop()

    daemon.on_change = one_at_a_time
    admin = Aria2Administration(executor, Repository([attempt(item) for item in handles],
                                                     {item.attempt_id for item in handles}),
                                None, SimpleNamespace())
    result = await admin.converge_job_tuning()
    assert result["selected"] == 3 and result["applied"] == 3 and result["not_live"] == 1


async def test_an_earlier_save_still_converging_never_overwrites_a_later_one(tmp_path):
    """Save A (split 5) is converging when Save B (split 9) lands: whatever the
    interleaving, every job ends on B's values, because each job is given the
    values current when it is reached, under one lock."""
    jobs = {f"{index:016x}": {"status": "active", "options": held()} for index in range(1, 4)}
    runtime = SimpleNamespace(options=Aria2Options(split=5, max_connection_per_server=4, min_split_size="64M"),
                              tuning=TuningState(), redactions={})
    daemon = FakeDaemon(jobs)

    async def allowed(_handle, _action):
        return True

    def executor():
        built = Aria2Executor(daemon, Aria2Configuration(str(tmp_path), confirmation_delay=0.01,
                                                         control_confirmation_timeout=0.2),
                              allowed, runtime=runtime)
        return built

    first = executor()
    handles = [handle(first, tmp_path, gid, f"a{index}") for index, gid in enumerate(jobs)]
    repository = Repository([attempt(item) for item in handles], {item.attempt_id for item in handles})
    saved_b = asyncio.Event()

    async def save_b_lands_mid_pass(_gid, _options):
        if not saved_b.is_set():
            saved_b.set()
            runtime.options = Aria2Options(split=9, max_connection_per_server=4, min_split_size="64M")
            # A's change of this job (split 5) is still on its way: without the
            # lock, B's pass would change the job first and A's 5 land after it.
            await asyncio.sleep(0.05)

    daemon.on_change = save_b_lands_mid_pass
    pass_a = asyncio.create_task(Aria2Administration(first, repository, None, runtime).converge_job_tuning())
    await saved_b.wait()
    # B's pass runs on the executor B's configure built, behind A's.
    pass_b = asyncio.create_task(Aria2Administration(executor(), repository, None, runtime).converge_job_tuning())
    await asyncio.gather(pass_a, pass_b)
    assert all(job["options"]["split"] == "9" for job in jobs.values())


async def test_a_job_submitted_with_tuning_a_save_replaced_converges_to_the_current_values(tmp_path):
    jobs = {}
    runtime = SimpleNamespace(options=Aria2Options(split=9), tuning=TuningState(), redactions={})
    daemon = FakeDaemon(jobs)
    original = daemon._call

    async def call(method, params=None, **kwargs):
        if method == "aria2.addUri":
            options = params[1]
            jobs[options["gid"]] = {"status": "paused", "options": {name: options[name] for name in TUNING}}
            return options["gid"]
        if method == "aria2.getSessionInfo":
            return {"sessionId": "s1"}
        return await original(method, params, **kwargs)

    daemon._call = call

    async def allowed(_handle, _action):
        return True

    executor = Aria2Executor(daemon, Aria2Configuration(str(tmp_path), confirmation_delay=0.01,
                                                        control_confirmation_timeout=0.2), allowed, runtime=runtime)
    item = handle(executor, tmp_path, GID)
    # Built before the Save: it carries split 16 while the Save made it 9.
    stale = {"gid": GID, **held()}
    observed = await executor._submit(item, "http://example.invalid/f", stale, (), paused=True)
    assert observed.state.value == "paused"
    assert jobs[GID]["options"]["split"] == "9" and jobs[GID]["status"] == "paused"
    # A current submission is left alone.
    jobs.clear()
    daemon.calls.clear()
    await executor._submit(item, "http://example.invalid/f", {"gid": GID, **executor._job_tuning()}, (), paused=True)
    assert daemon.changes() == []


async def test_new_jobs_inherit_the_saved_tuning_through_their_ordinary_submission(tmp_path):
    executor, _daemon = world(tmp_path, {}, split=5, connections=4, minimum="64M")
    assert executor._job_tuning() == {"max-connection-per-server": "4", "split": "5", "min-split-size": "64M"}
    runtime = SimpleNamespace(options=Aria2Options(split=7, max_connection_per_server=3, min_split_size="32M"),
                              tuning=TuningState(), redactions={})
    stale = replace(executor.configuration, split=1)
    built = Aria2Executor(executor.client, stale, executor.authorize, runtime=runtime)
    assert built._job_tuning() == {"max-connection-per-server": "3", "split": "7", "min-split-size": "32M"}


# ── bounded time, unanswered changes and overlapping passes ─────────────────

def daemon_world(tmp_path, count, *, split=5):
    """``count`` active jobs on one daemon whose current Save holds ``split``."""
    jobs = {f"{index:016x}": {"status": "active", "options": held()} for index in range(1, count + 1)}
    runtime = SimpleNamespace(options=Aria2Options(split=split, max_connection_per_server=16, min_split_size="10M"),
                              tuning=TuningState(), redactions={})
    daemon = FakeDaemon(jobs)

    async def allowed(_handle, _action):
        return True

    def executor():
        return Aria2Executor(daemon, Aria2Configuration(str(tmp_path), confirmation_delay=0.01,
                                                        control_confirmation_timeout=0.2), allowed, runtime=runtime)

    first = executor()
    handles = [handle(first, tmp_path, gid, f"a{index}") for index, gid in enumerate(jobs)]
    repository = Repository([attempt(item) for item in handles], {item.attempt_id for item in handles})
    return jobs, runtime, daemon, executor, repository


async def converge(executor, repository, seconds):
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await Aria2Administration(executor, repository, None, SimpleNamespace()).converge_job_tuning(
        deadline=started + seconds)
    return result, loop.time() - started


async def settle(runtime):
    """Let changes aria2 answers late, and the catch-ups they cause, finish."""
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not runtime.tuning.inflight and not runtime.tuning.followups:
            return
    raise AssertionError("tuning changes did not settle")


async def test_a_change_aria2_does_not_answer_in_time_is_uncertain_never_resent_and_the_pass_ends_on_time(tmp_path):
    jobs, runtime, daemon, executor, repository = daemon_world(tmp_path, 3)
    answer = asyncio.Event()

    async def unanswered(_gid, _options):
        await answer.wait()

    daemon.on_change = unanswered
    result, took = await converge(executor(), repository, 0.4)
    assert took < 0.7
    assert (result["selected"], result["uncertain"], result["not_reached"], result["applied"]) == (3, 1, 2, 0)
    assert len(daemon.changes()) == 1
    # aria2 answers later: the change takes effect, nothing is sent again.
    answer.set()
    await settle(runtime)
    first = next(iter(jobs))
    assert jobs[first]["options"]["split"] == "5" and len(daemon.changes()) == 1


async def test_a_slow_read_ends_the_job_before_anything_is_sent(tmp_path):
    _jobs, _runtime, daemon, executor, repository = daemon_world(tmp_path, 2)

    async def slow(_gid):
        await asyncio.sleep(5)

    daemon.on_read = slow
    result, took = await converge(executor(), repository, 0.3)
    assert took < 0.6 and result["not_reached"] == 2 and daemon.changes() == []


@pytest.mark.parametrize("answered", ["while_the_newer_pass_waits", "after_the_newer_pass_gave_up"])
async def test_an_older_change_answered_late_never_leaves_the_older_values(tmp_path, answered):
    """Save A (split 5) sends job 2's change and aria2 does not answer in time.
    Save B (split 9) follows. No change of job 2 is sent while A's is
    unanswered; whenever A's lands, job 2 ends on B's values -- and A's change
    is never sent a second time."""
    jobs, runtime, daemon, executor, repository = daemon_world(tmp_path, 2)
    gids = list(jobs)
    answer = asyncio.Event()

    async def second_job_unanswered(gid, options):
        if gid == gids[1] and options.get("split") == "5":
            await answer.wait()

    daemon.on_change = second_job_unanswered
    result_a, _ = await converge(executor(), repository, 0.3)
    assert (result_a["applied"], result_a["uncertain"]) == (1, 1)

    runtime.options = Aria2Options(split=9, max_connection_per_server=16, min_split_size="10M")
    if answered == "while_the_newer_pass_waits":
        asyncio.get_running_loop().call_later(0.1, answer.set)
        result_b, _ = await converge(executor(), repository, 2.0)
        assert result_b["applied"] == 2
    else:
        result_b, _ = await converge(executor(), repository, 0.3)
        # Job 2 is left alone while A's change is unanswered: uncertain, not resent.
        assert (result_b["applied"], result_b["uncertain"]) == (1, 1)
        assert [options.get("split") for gid, options in daemon.sent if gid == gids[1]] == ["5"]
        answer.set()
    await settle(runtime)
    assert all(job["options"]["split"] == "9" for job in jobs.values())
    assert [options.get("split") for gid, options in daemon.sent if gid == gids[1]] == ["5", "9"]


async def test_a_newer_pass_waiting_for_the_lock_takes_over_from_an_older_one(tmp_path):
    jobs, runtime, daemon, executor, repository = daemon_world(tmp_path, 4)

    async def slow(_gid, _options):
        await asyncio.sleep(0.1)

    daemon.on_change = slow
    pass_a = asyncio.create_task(converge(executor(), repository, 5.0))
    await asyncio.sleep(0.15)  # A is changing its second job
    runtime.options = Aria2Options(split=9, max_connection_per_server=16, min_split_size="10M")
    (result_b, took_b), (result_a, _) = await asyncio.gather(converge(executor(), repository, 5.0), pass_a)
    # A stopped at its next job and left the rest to B, which waited for one job only.
    assert result_a["superseded"] >= 2 and result_a["applied"] + result_a["superseded"] == 4
    assert result_b["selected"] == 4 and result_b["applied"] + result_b["unchanged"] == 4
    assert took_b < 4 * 0.1 + 0.3
    assert all(job["options"]["split"] == "9" for job in jobs.values())


async def test_a_pass_that_cannot_get_the_lock_in_time_touches_nothing_and_counts_nothing(tmp_path):
    _jobs, runtime, daemon, executor, repository = daemon_world(tmp_path, 3)
    await runtime.tuning.lock.acquire()
    try:
        result, took = await converge(executor(), repository, 0.2)
    finally:
        runtime.tuning.lock.release()
    # Which jobs exist was never read: unknown, not "no jobs".
    assert took < 0.5 and result == {"selection": "unavailable"} and daemon.changes() == []


class SlowRepository(Repository):
    def __init__(self, attempts, current, slow):
        super().__init__(attempts, current)
        self.slow = slow

    async def live_executions(self):
        if self.slow == "live_executions":
            await asyncio.sleep(5)
        if self.slow == "fails":
            raise RuntimeError("database is locked")
        return await super().live_executions()

    async def authorize_executions(self, handles, action):
        if self.slow == "authorize_executions":
            await asyncio.sleep(5)
        return await super().authorize_executions(handles, action)


@pytest.mark.parametrize("slow", ["live_executions", "authorize_executions", "fails"])
async def test_a_selection_that_cannot_complete_in_time_releases_the_lock_and_says_so(tmp_path, slow):
    jobs, runtime, daemon, executor, repository = daemon_world(tmp_path, 2)
    stalled = SlowRepository(repository.attempts, repository.current, slow)
    result, took = await converge(executor(), stalled, 0.3)
    assert took < 0.6
    # No invented count: the jobs are unknown, so none is reported, none touched.
    assert result == {"selection": "unavailable"} and daemon.changes() == []
    # The lock was released at once: the next pass runs and selects normally.
    assert not runtime.tuning.lock.locked()
    result, _ = await converge(executor(), repository, 2.0)
    assert result["selection"] == "complete" and (result["selected"], result["applied"]) == (2, 2)
    assert all(job["options"]["split"] == "5" for job in jobs.values())


def submitting_world(tmp_path, allowed):
    """A daemon accepting ``addUri`` for one job whose current Save holds
    split 9, and an executor to submit it."""
    jobs = {}
    runtime = SimpleNamespace(options=Aria2Options(split=9), tuning=TuningState(), redactions={})
    daemon = FakeDaemon(jobs)
    original = daemon._call

    async def call(method, params=None, **kwargs):
        if method == "aria2.addUri":
            daemon.calls.append((method, params[1]["gid"]))
            options = params[1]
            jobs[options["gid"]] = {"status": "paused", "options": {name: options[name] for name in TUNING}}
            return options["gid"]
        if method == "aria2.getSessionInfo":
            return {"sessionId": "s1"}
        return await original(method, params, **kwargs)

    daemon._call = call
    executor = Aria2Executor(daemon, Aria2Configuration(str(tmp_path), confirmation_delay=0.01,
                                                        control_confirmation_timeout=0.2), allowed, runtime=runtime)
    return jobs, runtime, daemon, executor


@pytest.mark.parametrize("revoked", [False, True], ids=["still_owned", "revoked_before_convergence"])
async def test_a_job_submitted_while_a_pass_holds_the_lock_converges_once_it_is_free(tmp_path, monkeypatch, revoked):
    """A Save's pass holds the daemon's tuning lock longer than a submission
    waits for it: the job submitted with the replaced values is not left on
    them -- the bounded catch-up gives it the current values once the lock is
    free, the same job, never removed or resubmitted. A job whose authority
    was revoked meanwhile is never touched."""
    import executors.aria2.executor as executor_module

    monkeypatch.setattr(executor_module, "_JOB_TUNING_SECONDS", 0.2)
    owned = {"pause": True}

    async def authorize(_handle, action):
        return owned.get(action, True)

    jobs, runtime, daemon, executor = submitting_world(tmp_path, authorize)
    item = handle(executor, tmp_path, GID)
    await runtime.tuning.lock.acquire()  # the Save's pass, still running
    loop = asyncio.get_running_loop()
    started = loop.time()
    observed = await executor._submit(item, "http://example.invalid/f", {"gid": GID, **held()}, (), paused=True)
    # The submission waited only briefly, and was not blocked by the pass.
    assert observed.state.value == "paused" and loop.time() - started < 0.6
    assert jobs[GID]["options"]["split"] == "16" and len(runtime.tuning.followups) == 1
    if revoked:
        owned["pause"] = False
    runtime.tuning.lock.release()
    await settle(runtime)
    assert [method for method, _gid in daemon.calls if method == "aria2.addUri"] == ["aria2.addUri"]
    assert not [method for method, _gid in daemon.calls if "remove" in method.casefold()]
    assert jobs[GID]["status"] == "paused"
    if revoked:
        assert daemon.changes() == [] and jobs[GID]["options"]["split"] == "16"
    else:
        assert daemon.changes() == [("aria2.changeOption", GID)] and jobs[GID]["options"]["split"] == "9"
