"""1.0.14: an estimated time remaining per actively downloading transfer.

The estimate is the transfer's provable remaining acquisition bytes
(``TransferRepository.acquisition_work``) over the summed rate the one core
throughput meter measures for exactly the attempts acquiring them
(``ExecutionThroughputMeter.acquisition_rate``, composed by
``runtime_telemetry.acquisition_eta`` and read through
``TransferEngine.acquisition_etas``). No second sampler, no global rate, no
estimate where the remaining work cannot be proven.

Expected figures are computed independently from the counters the tests feed,
never read back from the estimator. Meter cases use a stepped clock; engine
cases run the real repository and engine over a memory executor.
"""
from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import db.database as database
from application.service import ApplicationService
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.convergence_engine import TransferEngine
from transfers.models import ExecutionActivity, ExecutionState, TransferProgress, TransferRequest
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry
from transfers.runtime_telemetry import (
    ETA_HOLD_SECONDS, ETA_MIN_SPAN_SECONDS, ETA_WINDOW_SECONDS, MAX_ETA_SECONDS, ExecutionThroughputMeter,
    acquisition_eta, acquisition_eta_payload,
)

MIB = 1024 * 1024
ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self, now=1000.0):
        self.now = float(now)

    def __call__(self):
        return self.now


def _meter():
    clock = Clock()
    return ExecutionThroughputMeter(clock=clock), clock


def _run(meter, clock, counters, seconds, rates, step=0.5):
    """``rates`` bytes/second per attempt for ``seconds``, one pass per ``step``."""
    for _ in range(int(round(seconds / step))):
        clock.now += step
        for key, rate in rates.items():
            counters[key] = counters.get(key, 0) + int(rate * step)
        meter.record({key: (clock.now, value) for key, value in counters.items()})


# ── the estimator's measurement rules (stepped clock) ─────────────────────────

def test_the_estimator_derives_its_bounds_from_the_meters_own_windows():
    from transfers import runtime_telemetry as telemetry
    assert ETA_WINDOW_SECONDS == telemetry.SUSTAINED_WINDOW_SECONDS == 30.0
    assert ETA_MIN_SPAN_SECONDS == telemetry.WINDOW_SECONDS == 4.0
    assert ETA_HOLD_SECONDS == telemetry._SUSTAINED_GAP_SECONDS == 5.0
    assert MAX_ETA_SECONDS == 9999 * 3600 + 59 * 60 + 59


def test_a_new_run_has_no_rate_until_it_spans_the_presentation_window():
    meter, clock = _meter()
    counters = {}
    _run(meter, clock, counters, 3.5, {"a": 2 * MIB})
    assert meter.acquisition_rate("a") is None                      # 3.0 s of movement measured: not yet
    _run(meter, clock, counters, 1.0, {"a": 2 * MIB})
    rate, held = meter.acquisition_rate("a")
    assert rate == pytest.approx(2 * MIB) and held is False


def test_a_steady_download_counts_down_by_the_time_that_passed():
    meter, clock = _meter()
    counters, total = {}, 400 * MIB
    _run(meter, clock, counters, 10, {"a": 4 * MIB})
    seen = []
    for _ in range(10):
        _run(meter, clock, counters, 1, {"a": 4 * MIB})
        remaining = total - counters["a"]
        seen.append(acquisition_eta(remaining, ["a"], meter))
    expected = [(math.ceil((total - (10 + second) * 4 * MIB) / (4 * MIB)), False) for second in range(1, 11)]
    assert seen == expected                                         # 89, 88, ... 80: one second per second
    assert all(later[0] < earlier[0] for earlier, later in zip(seen, seen[1:]))


def test_a_bursty_coarse_counter_moves_the_estimate_gently():
    """A counter that only moves in 8 MiB steps every 2 s (4 MiB/s on average)
    swings the 4 s presentation speed between bursts; the estimate, measured
    over up to the sustained window, stays near the true average."""
    meter, clock = _meter()
    counter, total, estimates = 0, 2048 * MIB, []
    for step in range(120):                                         # 60 s
        clock.now += 0.5
        if step % 4 == 3:
            counter += 8 * MIB
        meter.record({"a": (clock.now, counter)})
        if step >= 60:
            estimates.append(acquisition_eta(total - counter, ["a"], meter)[0])
    true_seconds = [(total - 4 * MIB * (0.5 * (step + 1))) / (4 * MIB) for step in range(60, 120)]
    for got, true in zip(estimates, true_seconds):
        assert abs(got - true) <= 0.15 * true                       # never an absurd swing
    assert max(b - a for a, b in zip(estimates, estimates[1:])) <= 12  # no jump upward of note


def test_irregular_observation_intervals_are_measured_by_their_timestamps():
    meter, clock = _meter()
    counter = 0
    for step in (0.3, 0.9, 0.4, 1.4, 0.5, 0.7, 0.8, 0.6):          # 5.6 s at exactly 3 MiB/s
        clock.now += step
        counter += int(3 * MIB * step)
        meter.record({"a": (clock.now, counter)})
    rate, _held = meter.acquisition_rate("a")
    assert rate == pytest.approx(3 * MIB, rel=1e-3)


def test_a_brief_stall_holds_the_estimate_exactly_and_says_so():
    meter, clock = _meter()
    counters, total = {}, 100 * MIB
    _run(meter, clock, counters, 8, {"a": 2 * MIB})
    shown = acquisition_eta(total - counters["a"], ["a"], meter)
    assert shown[1] is False
    for missing in range(1, 7):                                     # up to 3 s of flat samples
        clock.now += 0.5
        meter.record({"a": (clock.now, counters["a"])})
        held = acquisition_eta(total - counters["a"], ["a"], meter)
        assert held[0] == shown[0], "a held estimate never counts down"
        assert held[1] is (missing * 0.5 > 1.0)                     # held past the presentation grace


def test_a_sustained_stall_has_no_estimate():
    meter, clock = _meter()
    counters = {}
    _run(meter, clock, counters, 8, {"a": 2 * MIB})
    _run(meter, clock, counters, ETA_HOLD_SECONDS, {"a": 0})
    assert acquisition_eta(10 * MIB, ["a"], meter) is not None       # exactly at the hold: still held
    _run(meter, clock, counters, 0.5, {"a": 0})
    assert acquisition_eta(10 * MIB, ["a"], meter) is None


def test_observation_that_stops_altogether_ends_the_estimate_too():
    meter, clock = _meter()
    counters = {}
    _run(meter, clock, counters, 8, {"a": 2 * MIB})
    clock.now += ETA_HOLD_SECONDS + 0.5                             # no pass at all
    assert meter.acquisition_rate("a") is None


def test_a_resumed_download_is_measured_afresh_never_from_its_old_rate():
    meter, clock = _meter()
    counters, total = {}, 1000 * MIB
    _run(meter, clock, counters, 10, {"a": 1 * MIB})
    _run(meter, clock, counters, 10, {"a": 0})                      # a sustained stall
    _run(meter, clock, counters, 3.5, {"a": 5 * MIB})
    assert acquisition_eta(total - counters["a"], ["a"], meter) is None, "no resurrected estimate"
    _run(meter, clock, counters, 1.5, {"a": 5 * MIB})
    rate, _held = meter.acquisition_rate("a")
    assert rate == pytest.approx(5 * MIB), "neither the stall nor the old 1 MiB/s dilutes the new run"


@pytest.mark.parametrize("discontinuity", ["rollback", "not_acquiring", "new_attempt"])
def test_a_discontinuity_starts_a_new_measurement(discontinuity):
    meter, clock = _meter()
    counters = {}
    _run(meter, clock, counters, 8, {"a": 2 * MIB})
    assert meter.acquisition_rate("a") is not None
    clock.now += 0.5
    if discontinuity == "rollback":
        counters["a"] = 1 * MIB
        meter.record({"a": (clock.now, counters["a"])})
        assert meter.acquisition_rate("a") is None
    elif discontinuity == "not_acquiring":
        meter.record({"a": (clock.now, None)})
        assert meter.acquisition_rate("a") is None
    else:
        meter.record({"b": (clock.now, 0)})                         # the replacement attempt
        assert meter.acquisition_rate("a") is None and meter.acquisition_rate("b") is None


def test_concurrent_attempts_are_summed_and_only_the_named_ones():
    meter, clock = _meter()
    counters = {}
    _run(meter, clock, counters, 8, {"a": 1 * MIB, "b": 3 * MIB, "other": 50 * MIB})
    assert acquisition_eta(40 * MIB, ["a", "b"], meter) == (10, False)   # 40 MiB at 4 MiB/s
    assert acquisition_eta(40 * MIB, ["a", "a", "b"], meter) == (10, False)   # each attempt once
    assert acquisition_eta(40 * MIB, ["a", "missing"], meter) is None    # every writer needs a rate


def test_rounding_and_the_representable_ceiling():
    meter, clock = _meter()
    counters = {}
    _run(meter, clock, counters, 8, {"a": 1000})                    # exactly 1000 B/s
    assert acquisition_eta(1, ["a"], meter) == (1, False)           # a fraction of a second is 1 s, never 0
    assert acquisition_eta(1500, ["a"], meter) == (2, False)        # rounded up
    assert acquisition_eta(MAX_ETA_SECONDS * 1000, ["a"], meter) == (MAX_ETA_SECONDS, False)
    assert acquisition_eta(MAX_ETA_SECONDS * 1000 + 1, ["a"], meter) is None
    assert acquisition_eta(100 * 3600 * 1000, ["a"], meter) == (360000, False)   # 100 h stays 100 h
    assert acquisition_eta(0, ["a"], meter) is None                  # no remaining work is no estimate
    assert acquisition_eta(None, ["a"], meter) is None
    assert acquisition_eta(10, [], meter) is None
    assert acquisition_eta_payload(None) is None
    assert acquisition_eta_payload((61, True)) == {"seconds": 61, "held": True}


# ── the logical transfer's remaining work (real repository and engine) ──────────

async def _core(tmp_path, monkeypatch, *, files=1, transfers=1, width=4):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(ParcelProvider())
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"), clock=lambda: 1000.0,
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                                                  max_active_executions=width))
    await engine.initialize()
    submitted = []
    for transfer in range(transfers):
        submitted.append(await engine.submit(tuple(
            TransferRequest("parcel", f"payload-{transfer}-{index}", name=f"file-{transfer}-{index}.bin")
            for index in range(files)), name=f"transfer-{transfer}", deduplicate=False))
    for _ in range(6):
        await engine.tick()
    clock = Clock()
    engine.throughput = ExecutionThroughputMeter(clock=clock)
    return SimpleNamespace(engine=engine, repository=repository, executor=executor, clock=clock,
                           transfers=[item.id for item in submitted])


async def _writers(core, transfer_id):
    return [item.execution.attempt_id for item in await core.repository.artifacts(transfer_id) if item.execution]


async def _acquire(core, seconds, totals, rates, *, step=0.5):
    """Each writer acquires at its rate toward its exact total, observed by
    the engine's own fast observation pass every ``step``."""
    for _ in range(int(round(seconds / step))):
        core.clock.now += step
        for attempt, rate in rates.items():
            job = core.executor.jobs[attempt]
            completed = min(totals[attempt], job.progress.completed_bytes + int(rate * step))
            core.executor.jobs[attempt] = replace(job, state=ExecutionState.RUNNING,
                                                  progress=TransferProgress(totals[attempt], completed, 0))
        await core.engine.sample_throughput()


def _started(core, attempt, total):
    core.executor.jobs[attempt] = replace(core.executor.jobs[attempt], progress=TransferProgress(total, 0, 0))


@pytest.mark.asyncio
async def test_one_transfer_estimate_is_its_writers_remaining_bytes_over_their_rate(tmp_path, monkeypatch):
    core = await _core(tmp_path, monkeypatch)
    [transfer] = core.transfers
    [attempt] = await _writers(core, transfer)
    _started(core, attempt, 400 * MIB)
    await _acquire(core, 3, {attempt: 400 * MIB}, {attempt: 4 * MIB})
    assert await core.engine.acquisition_etas([transfer]) == {}       # still measuring
    await _acquire(core, 7, {attempt: 400 * MIB}, {attempt: 4 * MIB})
    assert await core.repository.acquisition_work([transfer]) == {transfer: (360 * MIB, (attempt,))}
    assert await core.engine.acquisition_etas([transfer]) == {transfer: (90, False)}
    await _acquire(core, 5, {attempt: 400 * MIB}, {attempt: 4 * MIB})
    assert await core.engine.acquisition_etas([transfer]) == {transfer: (85, False)}


@pytest.mark.asyncio
async def test_concurrent_transfers_never_share_a_rate(tmp_path, monkeypatch):
    core = await _core(tmp_path, monkeypatch, transfers=2)
    slow, fast = core.transfers
    [a] = await _writers(core, slow)
    [b] = await _writers(core, fast)
    totals = {a: 100 * MIB, b: 100 * MIB}
    for attempt, total in totals.items():
        _started(core, attempt, total)
    await _acquire(core, 10, totals, {a: 1 * MIB, b: 4 * MIB})
    etas = await core.engine.acquisition_etas([slow, fast])
    assert etas == {slow: (90, False), fast: (15, False)}             # 90 MiB at 1, 60 MiB at 4
    # The global speed (5 MiB/s) would have claimed 18 s and 12 s.
    assert core.engine.throughput.current() == 5 * MIB


@pytest.mark.asyncio
async def test_concurrent_artifacts_of_one_transfer_are_one_estimate(tmp_path, monkeypatch):
    core = await _core(tmp_path, monkeypatch, files=2)
    [transfer] = core.transfers
    a, b = await _writers(core, transfer)
    totals = {a: 100 * MIB, b: 300 * MIB}
    for attempt, total in totals.items():
        _started(core, attempt, total)
    await _acquire(core, 10, totals, {a: 1 * MIB, b: 3 * MIB})
    work = await core.repository.acquisition_work([transfer])
    assert work == {transfer: (90 * MIB + 270 * MIB, tuple(sorted((a, b))))}   # each writer once
    assert await core.engine.acquisition_etas([transfer]) == {transfer: (90, False)}   # 360 MiB at 4 MiB/s


@pytest.mark.asyncio
async def test_a_waiting_member_of_known_size_is_remaining_work(tmp_path, monkeypatch):
    """Counterexample to an executor-level estimate: one writer of a
    two-file transfer is nearly done, but the transfer is not."""
    core = await _core(tmp_path, monkeypatch, files=2, width=1)
    [transfer] = core.transfers
    [attempt] = await _writers(core, transfer)
    artifacts = await core.repository.artifacts(transfer)
    [waiting] = [item for item in artifacts if not item.execution]
    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET size_bytes=? WHERE id=?", (600 * MIB, waiting.id))
        await db.commit()
    _started(core, attempt, 100 * MIB)
    await _acquire(core, 10, {attempt: 100 * MIB}, {attempt: 4 * MIB})
    work = await core.repository.acquisition_work([transfer])
    assert work == {transfer: (60 * MIB + 600 * MIB, (attempt,))}
    assert await core.engine.acquisition_etas([transfer]) == {transfer: (165, False)}   # not the writer's 15 s


@pytest.mark.asyncio
@pytest.mark.parametrize("unprovable", ["unknown_size", "open_request", "error_member"])
async def test_work_that_cannot_be_proven_complete_has_no_estimate(tmp_path, monkeypatch, unprovable):
    core = await _core(tmp_path, monkeypatch, files=2, width=1)
    [transfer] = core.transfers
    [attempt] = await _writers(core, transfer)
    [waiting] = [item for item in await core.repository.artifacts(transfer) if not item.execution]
    async with database.get_db() as db:
        if unprovable == "unknown_size":
            await db.execute("UPDATE download_files SET size_bytes=0,size_knowledge='unknown' WHERE id=?",
                             (waiting.id,))
        elif unprovable == "open_request":
            await db.execute("UPDATE transfer_requests SET state='resolving' WHERE id=?", (waiting.request_id,))
        else:
            await db.execute("UPDATE download_files SET status='error' WHERE id=?", (waiting.id,))
        await db.commit()
    _started(core, attempt, 100 * MIB)
    await _acquire(core, 10, {attempt: 100 * MIB}, {attempt: 4 * MIB})
    assert await core.repository.acquisition_work([transfer]) == {}
    assert await core.engine.acquisition_etas([transfer]) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["units", "processing", "estimated_only"])
async def test_a_writer_without_an_exact_byte_scope_has_no_estimate(tmp_path, monkeypatch, scope):
    core = await _core(tmp_path, monkeypatch)
    [transfer] = core.transfers
    [attempt] = await _writers(core, transfer)
    job = core.executor.jobs[attempt]
    if scope == "units":
        progress, activity = TransferProgress(0, 0, 0, completed_units=3, total_units=10), job.activity
    elif scope == "processing":
        progress = TransferProgress(100 * MIB, 100 * MIB, 0)
        activity = ExecutionActivity(network_active=False, bandwidth_reservation_required=False,
                                     progress_expected=True)
    else:
        progress, activity = TransferProgress(0, 5 * MIB, 0, estimated_percentage=40.0), job.activity
    core.executor.jobs[attempt] = replace(job, state=ExecutionState.RUNNING, progress=progress, activity=activity)
    await core.engine.reconcile_executions()
    assert await core.repository.acquisition_work([transfer]) == {}


@pytest.mark.asyncio
async def test_list_and_live_updates_carry_the_same_estimate_and_clear_it_together(tmp_path, monkeypatch):
    from api import operational_downloads as downloads
    import application.service as service_module

    core = await _core(tmp_path, monkeypatch)
    [transfer] = core.transfers
    [attempt] = await _writers(core, transfer)
    application = ApplicationService(core.engine)
    published = []

    async def publish(kind, payload):
        published.append((kind, payload))
    monkeypatch.setattr(service_module, "publish", publish)

    async def listed():
        page = await downloads.list_operational_torrents(status=None, search=None, limit=0, offset=0,
                                                         application=application)
        return next(item["acquisition_eta"] for item in page["items"] if item["id"] == transfer)

    async def live(seconds, rate):
        before = await core.repository.active()
        await _acquire(core, seconds, {attempt: 200 * MIB}, {attempt: rate})
        await application._publish_active_changes(before, stats=False)
        updates = [item for kind, payload in published if kind == "torrent_updated" for item in payload["items"]
                   if item["id"] == transfer]
        return updates[-1]["acquisition_eta"] if updates else "unpublished"

    _started(core, attempt, 200 * MIB)
    await live(8, 2 * MIB)
    measured = await live(0.5, 2 * MIB)
    assert measured == {"seconds": math.ceil((200 - 17) * MIB / (2 * MIB)), "held": False}
    assert await listed() == measured                                # one owner, one truth
    published.clear()
    held = await live(1.5, 0)
    assert held == {"seconds": measured["seconds"], "held": True}    # same figure, marked held
    assert await listed() == held
    published.clear()
    assert await live(3.0, 0) == "unpublished"                       # 4.5 s still: held, nothing changed
    assert await listed() == held
    published.clear()
    assert await live(1.0, 0) is None                                # 5.5 s: past the hold, published as none
    assert await listed() is None


@pytest.mark.asyncio
async def test_a_paused_transfer_has_no_estimate_anywhere(tmp_path, monkeypatch):
    from api import operational_downloads as downloads

    core = await _core(tmp_path, monkeypatch)
    [transfer] = core.transfers
    [attempt] = await _writers(core, transfer)
    application = ApplicationService(core.engine)
    _started(core, attempt, 200 * MIB)
    await _acquire(core, 8, {attempt: 200 * MIB}, {attempt: 2 * MIB})
    assert await core.engine.acquisition_etas([transfer])
    import application.service as service_module
    published = []

    async def publish(kind, payload):
        published.append((kind, payload))
    monkeypatch.setattr(service_module, "publish", publish)
    await application._publish_active_changes(await core.repository.active(), stats=False)   # an estimate is live
    published.clear()
    before = await core.repository.active()
    await application.pause(transfer)
    await application._publish_active_changes(before, stats=False)
    [update] = [item for kind, payload in published if kind == "torrent_updated" and payload.get("progress_only")
                for item in payload["items"] if item["id"] == transfer][-1:]
    assert update["acquisition_eta"] is None                          # the live path drops it too
    page = await downloads.list_operational_torrents(status=None, search=None, limit=0, offset=0,
                                                     application=application)
    row = next(item for item in page["items"] if item["id"] == transfer)
    assert row["presentation_status"] != "downloading" and row["acquisition_eta"] is None


@pytest.mark.asyncio
async def test_concurrent_publishers_leave_the_memo_equal_to_what_was_last_sent(monkeypatch):
    """The reconcile cycle and the fast observation publish concurrently. An
    older computation that finishes last publishes against the newer memo, so
    the memo always equals what the browser last received."""
    import asyncio
    import application.service as service_module

    transfer = SimpleNamespace(id=7, state="downloading", progress=10.0, active_execution_progress=None,
                               active_execution_basis=None, paused=False)
    release = asyncio.Event()
    answers = iter([({7: (50, False)}, release), ({7: (40, False)}, None)])

    class Engine:
        async def acquisition_etas(self, ids):
            etas, gate = next(answers)
            if gate is not None:
                await gate.wait()
            return etas

    class Repository:
        async def active(self):
            return (transfer,)

    application = ApplicationService.__new__(ApplicationService)
    # The browser shows 50 s, the figure last published.
    application.engine, application.repository, application._published_etas = Engine(), Repository(), {7: (50, False)}
    sent = []

    async def publish(kind, payload):
        sent.extend(item["acquisition_eta"] for item in payload.get("items", ()))
    monkeypatch.setattr(service_module, "publish", publish)
    older = asyncio.create_task(application._publish_active_changes((transfer,), stats=False))
    await asyncio.sleep(0)                                                # computes 50 s: no change yet
    await application._publish_active_changes((transfer,), stats=False)   # newer: 40 s, sent
    release.set()
    await older                                                           # older finishes last
    # Against the memo it found then (50 s) it would send nothing and leave the
    # memo at 50 s while the browser shows 40 s -- a later unchanged 50 s would
    # never correct it. Against what was last sent (40 s) it sends its 50 s.
    assert sent == [{"seconds": 40, "held": False}, {"seconds": 50, "held": False}]
    assert application._published_etas == {7: (50, False)}              # the memo matches the last sent


# ── what the estimate must not touch ──────────────────────────────────────────

def test_external_adapters_carry_no_estimate():
    """Deferred, not done: SABnzbd's ``timeleft`` and qBittorrent's ``eta``
    keep their unknown forms; no adapter reads the new estimate."""
    sab = (ROOT / "backend/api/arr_sabnzbd.py").read_text()
    qbit = (ROOT / "backend/api/arr_qbittorrent.py").read_text()
    external = (ROOT / "backend/external/service.py").read_text()
    for source in (sab, qbit, external):
        assert "acquisition_eta" not in source
    assert '"timeleft": "0:00:00"' in sab
    assert '"eta": UNKNOWN_ETA' in qbit


def test_every_surface_reads_the_one_estimate_owner():
    downloads = (ROOT / "backend/api/operational_downloads.py").read_text()
    service = (ROOT / "backend/application/service.py").read_text()
    app = (ROOT / "frontend/static/app.js").read_text()
    for source in (downloads, service):
        assert "acquisition_etas(" in source and "acquisition_eta_payload(" in source
        assert "acquisition_rate(" not in source and "acquisition_eta(" not in source.replace(
            "acquisition_eta_payload(", "")
    # The browser renders the backend's estimate; it keeps no clock or rate of its own.
    eta_code = app[app.index("const ACQUISITION_ETA_MAX_SECONDS"):app.index("function progress(")]
    for forbidden in ("setInterval", "setTimeout", "Date.now", "performance.now", "/execution/throughput"):
        assert forbidden not in eta_code
    assert f"const ACQUISITION_ETA_MAX_SECONDS = 9999 * 3600 + 59 * 60 + 59;" in app
