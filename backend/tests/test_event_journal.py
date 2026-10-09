"""The durable event journal as its canonical owners write it.

Real temporary databases and the real transfer engine with neutral fixture
providers: correlation across a subordinate failure and a successful
failover, once-only occurrences across replay and rollback, history that
survives transfer deletion, the explicit reset, and configuration events that
name what changed without its value.
"""
from __future__ import annotations

import pytest

import db.database as database
from core.config import AppSettings, configuration_changes
from db import event_journal
from services import db_maintenance
from test_v113_provider_exhaustion_failover import MALFORMED, PROVIDER_FINAL, RouteLab, lab
from transfers import codec
from transfers.repository import TransferRepository
from transfers import journal_events as je
from transfers.models import ExecutionState, TransferRequest, TransferState

pytestmark = pytest.mark.asyncio


async def _events(where="1=1", params=()):
    async with database.get_db() as db:
        return await db.fetchall(f"SELECT * FROM event_journal WHERE {where} ORDER BY id", params)


async def _settle(engine, repository, transfer_id, passes=12):
    """Drive the real engine; the neutral memory executor finishes whatever
    it is running, as a real executor eventually would."""
    for _ in range(passes):
        await engine.tick()
        for artifact in await repository.artifacts(transfer_id):
            handle = artifact.execution
            if handle is None:
                continue
            executor = engine.registry.executor_for_handle(handle)
            job = executor.jobs.get(handle.attempt_id)
            if job is not None and job.state == ExecutionState.RUNNING:
                executor.finish(handle)
        if (await repository.get(transfer_id)).state in {TransferState.COMPLETED, TransferState.FAILED}:
            return


async def test_a_subordinate_route_failure_and_its_failover_are_correlated_facts_not_a_transfer_failure(
        tmp_path, monkeypatch):
    first, second = RouteLab("alpha-route"), RouteLab("beta-route")
    first.script = [PROVIDER_FINAL]  # the first root's route fails finally; everything after succeeds
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    transfer = await engine.submit((TransferRequest("parcel", "root-one", name="one.bin"),
                                    TransferRequest("parcel", "root-two", name="two.bin")),
                                   name="two roots", deduplicate=False)
    await _settle(engine, repository, transfer.id)

    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    events = await _events("transfer_id=?", (transfer.id,))
    types = [row["event_type"] for row in events]
    exhausted = [row for row in events if row["event_type"] == "routing.route_exhausted"]
    assert len(exhausted) == 1
    assert (exhausted[0]["severity"], exhausted[0]["integration_id"], exhausted[0]["error_category"]) == (
        "warning", "alpha-route", "account_limited")
    moved = [row for row in events if row["event_type"] == "routing.provider_changed"]
    assert len(moved) == 1 and moved[0]["integration_id"] == "beta-route"
    assert "account limited" in moved[0]["detail"]
    # The subordinate failure is not the transfer's outcome.
    assert "transfer.failed" not in types and "routing.request_failed" not in types
    assert types.count("transfer.completed") == 1 and types[0] == "transfer.accepted"
    assert types.count("execution.started") == 2 and types.count("execution.file_completed") == 2
    # One exact transfer id finds every related route, attempt and artifact event.
    async with database.get_db() as db:
        page = await event_journal.page(db, limit=250, before=None, snapshot=None, category=None, severity=None,
                                        since=None, terms=event_journal.search_terms(f"#{transfer.id}"))
    assert [item["id"] for item in page["items"]] == [row["id"] for row in reversed(events)]

    # A restart replays nothing that already happened.
    before = len(await _events())
    repository, engine = await lab(tmp_path, monkeypatch, RouteLab("alpha-route"), RouteLab("beta-route"),
                                   fresh=False)
    await _settle(engine, repository, transfer.id, passes=3)
    assert len(await _events()) == before


async def test_one_occurrence_is_one_record_and_an_uncommitted_transition_leaves_none(tmp_path, monkeypatch):
    repository, engine = await lab(tmp_path, monkeypatch, RouteLab("alpha-route"))
    transfer = await engine.submit((TransferRequest("parcel", "x", name="x.bin"),), name="x", deduplicate=False)
    started = je.route_started(transfer_id=transfer.id, request_id="r", attempt_id="attempt-1",
                               provider_id="alpha-route", operation="resolve", transition_kind=None,
                               transition_reason=None, previous_provider_id=None)
    async with database.get_db() as db:
        assert await event_journal.record(db, started) is True
        assert await event_journal.record(db, started) is False  # replayed occurrence
        await db.commit()
    async with database.get_db() as db:
        await event_journal.record(db, je.deleted(transfer.id, remote=False))
        await db.rollback()  # the transition it described never committed
    assert [row["event_type"] for row in await _events("subject_kind='route_attempt' OR event_type='transfer.deleted'")] \
        == ["routing.route_started"]
    count = len(await _events())
    # A refused (fenced) transition writes nothing.
    assert await repository.state(transfer.id, TransferState.QUEUED, expected_epoch=10_000) is False
    assert len(await _events()) == count


async def test_history_survives_transfer_deletion_and_only_the_explicit_reset_clears_it(tmp_path, monkeypatch):
    repository, engine = await lab(tmp_path, monkeypatch, RouteLab("alpha-route"))
    transfer = await engine.submit((TransferRequest("parcel", "kept", name="kept.bin"),), name="kept history",
                                   deduplicate=False)
    async with database.get_db() as db:
        await db.execute("UPDATE event_journal SET occurred_at = occurred_at - 400 * 86400")
        await db.commit()
    await repository.delete(transfer.id, remote=False)
    await repository.delete(transfer.id, remote=False)
    events = await _events("transfer_id=?", (transfer.id,))
    assert [row["event_type"] for row in events][0] == "transfer.accepted"
    assert [row["event_type"] for row in events].count("transfer.deleted") == 1
    assert {row["subject_name"] for row in events} == {"kept history"}
    async with database.get_db() as db:
        page = await event_journal.page(db, limit=50, before=None, snapshot=None, category="transfer", severity=None,
                                        since=None, terms=None)
    assert page["items"] and not any(item["transfer_available"] for item in page["items"])

    while await event_journal.catch_up():
        pass
    last = (await _events())[-1]["id"]
    await db_maintenance.wipe_database(verified_quiesced=True)
    remaining = await _events()
    assert [row["event_type"] for row in remaining] == ["administration.database_reset"]
    assert remaining[0]["id"] > last  # ids keep ascending: no stale cursor aliases new history
    async with database.get_db() as db:
        # The index was cleared with the rows; only the reset event awaits indexing.
        assert (await db.fetchone("SELECT indexed_through FROM event_journal_index"))["indexed_through"] == last
        assert await event_journal.pending_index_rows(db, 10) == 1
        page = await event_journal.page(db, limit=50, before=None, snapshot=None, category=None, severity=None,
                                        since=None, terms=event_journal.search_terms("kept history"))
    assert page["items"] == []


async def test_configuration_events_name_what_changed_never_its_value():
    previous = AppSettings(discord_webhook_url="https://discord.example/api/webhooks/1/secret-token",
                           activity_log_page_size=100)
    current = previous.model_copy(update={"discord_webhook_url": "https://discord.example/api/webhooks/2/other",
                                          "activity_log_page_size": 250})
    changed, toggled = configuration_changes(previous, current)
    assert changed == ["activity_log_page_size", "discord_webhook_url"] and toggled == []
    assert not any("secret-token" in name or "other" in name for name in changed)
    assert configuration_changes(previous, previous) == ([], [])


async def test_each_failed_source_of_one_transfer_is_named_not_one_generic_failure(tmp_path, monkeypatch):
    provider = RouteLab("alpha-route")
    provider.always = MALFORMED  # the request's own, terminal failure through every provider
    repository, engine = await lab(tmp_path, monkeypatch, provider)
    transfer = await engine.submit((TransferRequest("parcel", "first", name="a.bin"),
                                    TransferRequest("parcel", "second", name="b.bin")),
                                   name="two sources", deduplicate=False)
    for _ in range(6):
        await engine.resolve_pending()
    failures = await _events("transfer_id=? AND event_type='routing.request_failed'", (transfer.id,))
    assert sorted(row["message"] for row in failures) == [
        "Source request failed (source 1): Invalid request", "Source request failed (source 2): Invalid request"]
    assert {row["error_category"] for row in failures} == {"invalid_request"}
    assert {row["severity"] for row in failures} == {"error"}

    # A link names its public host; anything that is not a safe host name is
    # left out rather than repaired.
    def label(payload, ordinal=2, parent=None):
        return TransferRepository._request_label({"payload": codec.dump({"payload": payload}), "ordinal": ordinal,
                                                  "parent_id": parent})
    assert label("https://www.1fichier.com/?abc&token=secret") == "source 3, 1fichier.com"
    assert label("https://user:pw@[::1]:8443/x") == "source 3"
    assert label("https://rapidgator.net/file/x", parent="root") == "rapidgator.net"
