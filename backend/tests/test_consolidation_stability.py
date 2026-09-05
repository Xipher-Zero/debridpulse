import pytest
import pytest_asyncio

from application.consolidation_events import ConsolidationEvents
from db import database


@pytest_asyncio.fixture()
async def stability_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "consolidation-stability.sqlite")
    await database.init_db()
    return database


@pytest.mark.asyncio
async def test_partial_summary_waits_while_sibling_is_materializing(stability_db):
    async with database.get_db() as db:
        canonical_id = await db.execute_returning_id(
            "INSERT INTO torrents(hash,name,status) VALUES('canonical-hash','canonical','downloading')"
        )
        source_id = await db.execute_returning_id(
            "INSERT INTO torrents(hash,name,status) VALUES('source-hash','source','downloading')"
        )
        await db.execute(
            "INSERT INTO transfer_requests(id,transfer_id,ordinal,payload,state) VALUES('canonical-request',?,0,'{}','resolved')",
            (canonical_id,),
        )
        canonical_artifact = await db.execute_returning_id(
            """INSERT INTO download_files(torrent_id,filename,size_bytes,status,request_id,mirror_state,blocked)
               VALUES(?,'canonical.bin',10,'pending','canonical-request','primary',0)""",
            (canonical_id,),
        )
        for ordinal, state in ((0, "resolved"), (1, "materializing")):
            request_id = f"source-request-{ordinal}"
            await db.execute(
                "INSERT INTO transfer_requests(id,transfer_id,ordinal,payload,state) VALUES(?,?,?,'{}',?)",
                (request_id, source_id, ordinal, state),
            )
            artifact_id = await db.execute_returning_id(
                """INSERT INTO download_files(torrent_id,filename,size_bytes,status,request_id,mirror_state,blocked)
                   VALUES(?,?,10,'pending',?,?,0)""",
                (source_id, f"source-{ordinal}.bin", request_id, "standby" if ordinal == 0 else ""),
            )
            if ordinal == 0:
                await db.execute(
                    """INSERT INTO artifact_consolidations(
                           contributing_artifact_id,source_transfer_id,source_request_id,canonical_artifact_id)
                       VALUES(?,?,?,?)""",
                    (artifact_id, source_id, request_id, canonical_artifact),
                )
        await db.commit()

    events = ConsolidationEvents(repository=None)
    await events.stage(source_id)
    assert await events.finalize_pending() == 0

    async with database.get_db() as db:
        await db.execute(
            "UPDATE transfer_requests SET state='resolved' WHERE id='source-request-1'"
        )
        await db.commit()

    assert await events.finalize_pending() == 1
