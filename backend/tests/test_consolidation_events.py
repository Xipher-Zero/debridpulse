import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from application.consolidation_events import ConsolidationEvents
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
    """A refused attach (no alternatives, so nothing is written) never announces."""
    from transfers.canonical import CanonicalOwnership

    staged = []

    async def stage(transfer_id):
        staged.append(transfer_id)

    canonical = CanonicalOwnership(SimpleNamespace(), on_attached=stage)
    canonical._initialized = True
    source = SimpleNamespace(transfer_id=42)
    assert await canonical.attach(object(), source, (), 0) is False
    assert staged == []


def test_composition_injects_the_callback_and_nothing_wraps_the_canonical_owner():
    import inspect
    from application import composition, consolidation_events

    assert not hasattr(consolidation_events, "ConsolidationEventCanonical")
    source = inspect.getsource(composition)
    assert "engine.canonical.on_attached = consolidation_events.stage" in source
    assert "engine.canonical =" not in source


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


async def _unverified_leaf(db, source_id, canonical_artifact_id, request_id, ordinal):
    """A terminal UNVERIFIED association: durable non-writer truth owned by
    ``transfers.cohorts`` -- materializing, no artifact, no consolidation row,
    associated with a canonical artifact owned by another transfer."""
    await db.execute(
        """INSERT INTO transfer_requests(id, transfer_id, ordinal, payload, state,
               equivalence_disposition, equivalence_reason, equivalence_target_artifact_id)
           VALUES(?, ?, ?, '{}', 'materializing', 'unverified', 'range_ignored', ?)""",
        (request_id, source_id, ordinal, canonical_artifact_id),
    )


async def _source_with_unverified_leaf(db, *, matched=2, unverified=1, status="consolidated"):
    source_id = await _transfer(db, "source", status)
    canonical_id = await _transfer(db, "canonical-0")
    await _request(db, canonical_id, "canonical-request-0", 0)
    canonical_artifact = await _artifact(db, canonical_id, "canonical-request-0", "canonical-0")
    for index in range(matched):
        request_id = f"source-request-{index}"
        await _request(db, source_id, request_id, index)
        contributing = await _artifact(db, source_id, request_id, f"source-{index}", mirror_state="standby")
        await db.execute(
            """INSERT INTO artifact_consolidations(
                   contributing_artifact_id, source_transfer_id, source_request_id, canonical_artifact_id)
               VALUES(?, ?, ?, ?)""",
            (contributing, source_id, request_id, canonical_artifact),
        )
    for offset in range(unverified):
        await _unverified_leaf(db, source_id, canonical_artifact, f"source-unverified-{offset}", matched + offset)
    await db.commit()
    return source_id, canonical_id


@pytest.mark.asyncio
async def test_consolidation_event_recognizes_terminal_unverified_leaves(isolated_db):
    """DP 1.0.12 consolidation corrective, Gate 9 continuation, Finding 3: a
    transfer that correctly reaches CONSOLIDATED while holding a terminal
    UNVERIFIED association must still produce its consolidation disposition.
    RED before the correction: ``_disposition`` recognizes only ``resolved``
    leaves, so the operator saw a CONSOLIDATED transfer, an UNVERIFIED source
    in canonical Details, and no consolidation notice at all."""
    async with database.get_db() as db:
        source_id, canonical_id = await _source_with_unverified_leaf(db, matched=2, unverified=1)

    events = ConsolidationEvents(repository=None)
    await events.stage(source_id)
    assert await events.finalize_pending() == 1
    assert await events.finalize_pending() == 0

    async with database.get_db() as db:
        rows = await db.fetchall(
            "SELECT detail FROM application_events WHERE transfer_id = ? AND kind = 'duplicate_consolidated'",
            (source_id,),
        )
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail"])
    assert detail == {
        "source_transfer_id": source_id,
        "canonical_transfer_ids": [canonical_id],
        "matched_count": 2,
        "unmatched_count": 0,
        "unverified_count": 1,
    }
    # UNVERIFIED is its own settled state: never counted as a verified member,
    # and never counted as material that will still download normally.
    assert detail["matched_count"] == 2 and detail["unmatched_count"] == 0
    public = ConsolidationEvents.public_payload(rows[0]["detail"])
    assert public["unverified_count"] == 1
    assert public["matched_count"] == 2 and public["unmatched_count"] == 0


@pytest.mark.asyncio
async def test_unverified_leaf_never_alone_produces_a_consolidation_event(isolated_db):
    """A transfer whose ONLY settled leaves are unverified associations has no
    verified contribution to announce -- consolidation means a real canonical
    relationship was established."""
    async with database.get_db() as db:
        source_id, _canonical_id = await _source_with_unverified_leaf(db, matched=0, unverified=2)

    events = ConsolidationEvents(repository=None)
    await events.stage(source_id)
    assert await events.finalize_pending() == 0

    async with database.get_db() as db:
        rows = await db.fetchall(
            "SELECT kind FROM application_events WHERE transfer_id = ? AND kind = 'duplicate_consolidated'",
            (source_id,),
        )
    assert rows == []


@pytest.mark.asyncio
async def test_nonterminal_unresolved_leaf_still_withholds_the_consolidation_event(isolated_db):
    """The control: an unresolved leaf that is NOT terminal (``exhausted``,
    no durable association) is not a stable disposition, so the summary must
    still wait rather than announce a half-decided submission."""
    async with database.get_db() as db:
        source_id, _canonical_id = await _source_with_unverified_leaf(
            db, matched=2, unverified=0, status="downloading")
        await db.execute(
            """INSERT INTO transfer_requests(id, transfer_id, ordinal, payload, state,
                   equivalence_disposition, equivalence_reason)
               VALUES('source-exhausted', ?, 9, '{}', 'materializing', 'exhausted', 'range_ignored')""",
            (source_id,),
        )
        await db.commit()

    events = ConsolidationEvents(repository=None)
    await events.stage(source_id)
    assert await events.finalize_pending() == 0
