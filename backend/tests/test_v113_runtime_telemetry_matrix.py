"""1.0.13 release blocker D/E/F: executor-neutral runtime telemetry, verified.

The production symptom was a topbar that oscillated ``0 / MAX`` and ``1 / MAX``
during a Usenet-only acquisition, because generic application state had two
writers with different cadences -- the generic statistics poll and an
aria2-specific one -- and each overwrote the other with its own partial truth.

The canonical owners already exist on this tree. These tests hold them to the
whole matrix rather than to their implementation, so the defect cannot return
through a different door: one core-owned aggregate, one neutral count that no
executor can influence, one browser writer, and one value behind both the
topbar and the browser tab.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from transfers.convergence_engine import TransferEngine
from transfers.models import TransferRequest
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

from executor_fakes import LedgerExecutor, LedgerProvider
from sab_fakes import FakeSab, staged_store

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
APP_JS = (FRONTEND / "static" / "app.js").read_text()

VALID_NZB = b"""<?xml version="1.0" encoding="utf-8" ?>
<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
 <file poster="p@e.net" date="1700000000" subject="job [1/1] - &quot;job.bin&quot; yEnc (1/1)">
  <groups><group>alt.binaries.test</group></groups>
  <segments><segment bytes="1024" number="1">a@e.net</segment></segments>
 </file>
</nzb>
"""


@pytest_asyncio.fixture
async def mixed(tmp_path, monkeypatch):
    """One acquisition service and one wholly unrelated executor, together."""
    from executors.sabnzbd.executor import SabnzbdConfiguration, SabnzbdExecutor
    from providers.usenet.provider import UsenetProvider

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    root = tmp_path / "payloads"
    root.mkdir(parents=True, exist_ok=True)
    sab = FakeSab(complete_dir=str(root / ".dpwork" / "complete"),
                  download_dir=str(root / ".dpwork" / "incomplete"))
    acquisition = SabnzbdExecutor(
        sab,
        SabnzbdConfiguration(local_root=str(root), working_directory=str(root / ".dpwork"),
                             complete_directory=str(root / ".dpwork" / "complete")),
        repository.authorize_execution, staged_input=staged_store())
    other = LedgerExecutor(repository.authorize_execution)
    registry.register_provider(UsenetProvider(staged_input=staged_store()))
    registry.register_provider(LedgerProvider())
    registry.register_executor(acquisition)
    registry.register_executor(other)
    now = [1000.0]
    engine = TransferEngine(repository, registry, download_root=str(root), clock=lambda: now[0],
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                                                  max_active_executions=4))
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, sab=sab,
                           acquisition=acquisition, other=other, now=now)


async def converge(core, rounds=8):
    for _ in range(rounds):
        await core.engine.tick()
        await asyncio.sleep(0)


# --- B1: the count has exactly one writer ----------------------------------

def test_b1_the_topbar_active_count_has_exactly_one_writer():
    """The oscillation was two writers, not a debounce problem."""
    assignments = re.findall(r"runtime-badge-active", APP_JS)
    assert len(assignments) == 1, \
        f"the topbar active element is referenced by {len(assignments)} sites"
    writer = APP_JS.split("function updateRuntimeStatusBadge")[1]
    assert "elActive.textContent = s.active" in writer


def test_b1_the_generic_statistics_poll_does_not_write_the_topbar_count():
    stats = APP_JS.split("async function loadStats")[1].split("\n}\n")[0]
    assert "runtime-badge-active" not in stats
    assert "updateRuntimeStatusBadge" not in stats


def test_b1_no_executor_specific_poll_exists_in_the_browser_at_all():
    for retired in ("/aria2/global-stat", "_aria2BadgeState", "loadAria2TopbarStat",
                    "/sabnzbd/", "loadSabTopbarStat"):
        assert retired not in APP_JS, retired


# --- B2: mixed executors -----------------------------------------------------

@pytest.mark.asyncio
async def test_b2_the_active_count_spans_every_executor(mixed):
    await mixed.engine.submit((TransferRequest("nzb", VALID_NZB, name="posting.nzb"),),
                              name="posting", deduplicate=False)
    await mixed.engine.submit((TransferRequest("ledger", "elsewhere", name="elsewhere"),),
                              name="elsewhere", deduplicate=False)
    await converge(mixed)

    assert len(mixed.sab.queue) == 1, "the acquisition service holds one job"
    assert len(mixed.other.jobs) == 1, "the unrelated executor holds one job"
    assert await mixed.repository.occupied_execution_slots(mixed.now[0]) == 2, \
        "the canonical count must span both, never report one executor's partial truth"


@pytest.mark.asyncio
async def test_b2_the_canonical_count_cannot_be_influenced_by_an_executor(mixed):
    """Enlarging one executor's own notion of activity changes nothing."""
    await mixed.engine.submit((TransferRequest("nzb", VALID_NZB, name="posting.nzb"),),
                              name="posting", deduplicate=False)
    await converge(mixed)
    before = await mixed.repository.occupied_execution_slots(mixed.now[0])
    from sab_fakes import FakeJob
    for index in range(5):
        mixed.sab.queue[f"foreign-{index}"] = FakeJob(f"foreign-{index}", f"not-ours-{index}")
    assert await mixed.repository.occupied_execution_slots(mixed.now[0]) == before


def test_b2_the_count_is_computed_without_naming_any_executor():
    source = Path(__file__).resolve().parents[1] / "transfers" / "_repository_base.py"
    body = source.read_text().split("async def occupied_execution_slots")[1].split("\n    async def")[0]
    for name in ("aria2", "sabnzbd", "usenet", "executor_id"):
        assert name not in body, f"the canonical active count referred to {name}"


# --- B3/B5/B6: the aggregate rate -------------------------------------------

@pytest.mark.asyncio
async def test_b3_the_aggregate_is_the_sum_of_every_executor(mixed):
    from transfers.runtime_telemetry import ExecutionThroughputMeter
    meter = ExecutionThroughputMeter()
    meter.record({"sabnzbd": 3 * 1024 * 1024, "ledger-copy": 2 * 1024 * 1024})
    assert meter.current() == 5 * 1024 * 1024


@pytest.mark.asyncio
async def test_b5_an_idle_cycle_clears_the_rate_rather_than_holding_it():
    from transfers.runtime_telemetry import ExecutionThroughputMeter
    meter = ExecutionThroughputMeter()
    meter.record({"sabnzbd": 9_000_000})
    meter.record({})
    assert meter.current() == 0, "a stale rate must never survive a cycle"


@pytest.mark.asyncio
async def test_b6_a_paused_execution_contributes_no_throughput(mixed):
    await mixed.engine.submit((TransferRequest("ledger", "elsewhere", name="elsewhere"),),
                              name="elsewhere", deduplicate=False)
    await converge(mixed)
    for job in mixed.other.jobs.values():
        job.progress = job.progress.__class__(job.progress.total_bytes,
                                              job.progress.completed_bytes, 0)
        job.activity = job.activity.__class__(network_active=False,
                                              bandwidth_reservation_required=True,
                                              progress_expected=False)
    await converge(mixed, rounds=2)
    assert mixed.engine.throughput.current() == 0


# --- B4: the acquisition executor reports a real neutral rate ---------------

@pytest.mark.asyncio
async def test_b4_the_acquisition_executor_reports_neutral_bytes_per_second(mixed):
    mixed.sab.download_bytes_per_second = 7 * 1024 * 1024
    reported = await mixed.acquisition.aggregate_download_throughput()
    assert reported.observed is True
    assert reported.bytes_per_second == 7 * 1024 * 1024


# --- B7: generic presentation knows no executor -----------------------------

def test_b7_generic_presentation_never_branches_on_an_executor_identity():
    presentation = APP_JS.split("function updateRuntimeStatusBadge")[1].split("\n}\n")[0]
    title = APP_JS.split("function renderOperatorTitle")[1].split("\n}\n")[0]
    for surface in (presentation, title):
        for name in ("aria2", "sabnzbd", "usenet", "nzb"):
            assert name not in surface.lower(), f"generic presentation mentioned {name}"


def test_b7_the_topbar_and_the_browser_tab_read_one_value():
    title = APP_JS.split("function renderOperatorTitle")[1].split("\n}\n")[0]
    badge = APP_JS.split("function updateRuntimeStatusBadge")[1].split("\n}\n")[0]
    assert "_runtimeStatusState" in title and "liveBps" in title
    assert "s.liveBps" in badge
    assert APP_JS.count("_runtimeStatusState = {") == 1, "one state object, one owner"
