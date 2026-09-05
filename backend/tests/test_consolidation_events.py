import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from application.consolidation_events import ConsolidationEventCanonical, ConsolidationEvents
from application.observability import Observability
from db import database


@pytest_asyncio.fixture()
async def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "consolidation-events.sqlite")
    await database.init_db()
    return database


async def _transfer(db, suffix, status="downloading"):
    return await db.execute_returning_id(
        "INSERT INTO torrents(hash, name, status) VALUES(?, ?, ?)",
        (f"hash-{suffix}", f"transfer-{suffix}", status),
    )


async def _artifact(db, transfer_id, request_id, suffix, *, mirror_state="", blocked=0):
    return await db.execute_returning_id(
        """INSERT INTO download_files(torrent_id, filename, size_bytes, status, request_id, mirror_state, blocked)
           VALUES(?, ?, 10, 'pending', ?, ?, ?)""",
        (transfer_id, f"file-{suffix}.bin", request_id, mirror_state, blocked),
    )


async def _request(db, transfer_id, request_id, ordinal, state="resolved"):
    await db.execute(
        """INSERT INTO transfer_requests(id, transfer_id, ordinal, payload, state)
           VALUES(?, ?, ?, '{}', ?)""",
        (request_id, transfer_id, ordinal, state),
    )


async def _mapped_source(db, *, matched, unmatched=0, targets=1, status=None):
    source_status = status or ("consolidated" if unmatched == 0 else "downloading")
    source_id = await _transfer(db, "source", source_status)
    canonical_ids = [await _transfer(db, f"canonical-{index}") for index in range(targets)]
    canonical_artifacts = []
    for index, canonical_id in enumerate(canonical_ids):
        request_id = f"canonical-request-{index}"
        await _request(db, canonical_id, request_id, 0)
        canonical_artifacts.append(await _artifact(db, canonical_id, request_id, f"canonical-{index}"))

    for index in range(matched + unmatched):
        request_id = f"source-request-{index}"
        await _request(db, source_id, request_id, index)
        contributing = await _artifact(
            db,
            source_id,
            request_id,
            f"source-{index}",
            mirror_state="standby" if index < matched else "",
        )
        if index < matched:
            target_index = index % targets
            await db.execute(
                """INSERT INTO artifact_consolidations(
                       contributing_artifact_id, source_transfer_id, source_request_id, canonical_artifact_id)
                   VALUES(?, ?, ?, ?)""",
                (contributing, source_id, request_id, canonical_artifacts[target_index]),
            )
    await db.commit()
    return source_id, canonical_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("target_count", [1, 2])
async def test_complete_consolidation_promotes_one_safe_event(isolated_db, target_count):
    async with database.get_db() as db:
        source_id, canonical_ids = await _mapped_source(db, matched=7, targets=target_count)

    events = ConsolidationEvents(repository=None)
    for _ in range(3):
        await events.stage(source_id)
    assert await events.finalize_pending() == 1
    assert await events.finalize_pending() == 0

    async with database.get_db() as db:
        rows = await db.fetchall(
            "SELECT kind, detail, claimed FROM application_events WHERE transfer_id = ? AND kind = 'duplicate_consolidated'",
            (source_id,),
        )
    assert len(rows) == 1
    assert rows[0]["claimed"] == 0
    assert json.loads(rows[0]["detail"]) == {
        "source_transfer_id": source_id,
        "canonical_transfer_ids": canonical_ids,
        "matched_count": 7,
        "unmatched_count": 0,
    }


@pytest.mark.asyncio
async def test_partial_consolidation_waits_for_stable_unmatched_artifacts(isolated_db):
    async with database.get_db() as db:
        source_id, canonical_ids = await _mapped_source(db, matched=5, unmatched=1, targets=1)
        pending_request = "source-request-pending"
        await _request(db, source_id, pending_request, 6)
        await db.commit()

    events = ConsolidationEvents(repository=None)
    await events.stage(source_id)
    assert await events.finalize_pending() == 0

    async with database.get_db() as db:
        await _artifact(db, source_id, pending_request, "pending-now-materialized")
        await db.commit()

    assert await events.finalize_pending() == 1
    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT detail FROM application_events WHERE transfer_id = ? AND kind = 'duplicate_consolidated'",
            (source_id,),
        )
    assert json.loads(row["detail"]) == {
        "source_transfer_id": source_id,
        "canonical_transfer_ids": canonical_ids,
        "matched_count": 5,
        "unmatched_count": 2,
    }


@pytest.mark.asyncio
async def test_public_payload_strips_secret_material():
    detail = json.dumps({
        "source_transfer_id": 10,
        "canonical_transfer_ids": [20, 20],
        "matched_count": 3,
        "unmatched_count": 1,
        "signed_url": "https://secret.invalid/capability?token=super-secret",
        "authorization": "Bearer super-secret",
        "cookie": "session=super-secret",
        "api_key": "super-secret",
    })
    assert ConsolidationEvents.public_payload(detail) == {
        "source_transfer_id": 10,
        "canonical_transfer_ids": [20],
        "matched_count": 3,
        "unmatched_count": 1,
    }


@pytest.mark.asyncio
async def test_failed_attach_never_stages_success_event():
    class Canonical:
        async def attach(self, *_args):
            return False

    class Events:
        staged = []

        async def stage(self, transfer_id):
            self.staged.append(transfer_id)

    source = SimpleNamespace(transfer_id=42)
    events = Events()
    canonical = ConsolidationEventCanonical(Canonical(), events)
    assert await canonical.attach(object(), source, (), 0) is False
    assert events.staged == []


@pytest.mark.asyncio
async def test_observability_delivers_safe_event_once(monkeypatch):
    event = {
        "id": 1,
        "transfer_id": 10,
        "kind": "duplicate_consolidated",
        "detail": json.dumps({
            "source_transfer_id": 10,
            "canonical_transfer_ids": [20, 21],
            "matched_count": 7,
            "unmatched_count": 0,
            "signed_url": "https://secret.invalid/?token=never-publish",
        }),
    }

    class Repository:
        claimed = False

        async def pending_events(self):
            return [] if self.claimed else [event]

        async def presentation(self, _transfer_id, details=False):
            return {"id": 10, "status": "consolidated"}

        async def claim_event(self, _event_id):
            if self.claimed:
                return False
            self.claimed = True
            return True

    class Pending:
        calls = 0

        async def finalize_pending(self):
            self.calls += 1
            return 0

    class Notifications:
        def client(self):
            return object()

    published = []

    async def capture(kind, payload):
        published.append((kind, payload))

    monkeypatch.setattr("application.observability.publish", capture)
    monkeypatch.setattr("application.observability.get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr("application.observability.NotificationService", Notifications)

    observability = Observability(Repository(), Pending())
    await observability.deliver()
    await observability.deliver()

    summaries = [payload for kind, payload in published if kind == "duplicate_consolidated"]
    assert summaries == [{
        "source_transfer_id": 10,
        "canonical_transfer_ids": [20, 21],
        "matched_count": 7,
        "unmatched_count": 0,
    }]
    assert "secret" not in json.dumps(summaries).lower()
