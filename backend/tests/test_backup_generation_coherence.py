"""1.0.13 WS6b ARCH-001: one restore point is one generation of configuration and database.

A backup copied ``config.json`` and then, after an await, snapshotted the
database; a configuration write that also commits to the database (an
Admin's profile retirement) could land between the two, so the restore point
held the catalog from before the write beside the database from after it.
Both copies are now taken under the configuration write lock that every such
write holds across its own commit.

The interleaving is the audit's, over real HTTP: the backup is held after its
configuration copy, the profile retirement is issued, then the backup goes on.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import types

import pytest

import core.config as config
import db.database as database
import services.backup as backup
from test_external_client_foundation import body, http, world  # noqa: F401
from test_external_profile_guard import catalog, without
from test_v113_profile_retirement import _fresh_settlement_walk, demand_of, retire  # noqa: F401

pytestmark = pytest.mark.asyncio


def _captured(point, job_id: str) -> dict:
    document = json.loads((point.path / backup._CONFIG_MEMBER).read_text())
    connection = sqlite3.connect(f"file:{point.path / database.DB_PATH.name}?mode=ro", uri=True)
    try:
        withdrawn = connection.execute(
            "SELECT d.withdrawn_at FROM external_jobs j JOIN transfer_demands d ON d.id=j.demand_id WHERE j.id=?",
            (job_id,)).fetchone()[0]
    finally:
        connection.close()
    delivery = document.get("external_delivery") or {}
    return {"revision": delivery.get("revision"),
            "profiles": sorted(item["id"] for item in delivery.get("profiles", [])),
            "job_retired": withdrawn is not None}


async def test_a_profile_retirement_during_a_backup_never_splits_its_restore_point(  # noqa: F811
        http, world, monkeypatch, tmp_path):  # noqa: F811
    from backup_support import prepare_backup_installation

    await prepare_backup_installation(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "_config_write_lock", asyncio.Lock())   # bound to this test's loop
    monkeypatch.setattr(backup, "_BACKUP_RUN_LOCK", asyncio.Lock())
    config.save_settings(config.get_settings())
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    for _ in range(2):
        await world.engine.tick()
    document = await catalog(http)

    config_copied, release = asyncio.Event(), asyncio.Event()
    real_copy, real_to_thread = backup._copy_private, asyncio.to_thread

    def copy(source, target):
        real_copy(source, target)
        if target.name == backup._CONFIG_MEMBER:
            config_copied.set()

    async def to_thread(function, *args, **kwargs):
        if function is backup._sqlite_backup:
            await release.wait()                                 # held between the two copies
        return await real_to_thread(function, *args, **kwargs)

    held = types.SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio) if not name.startswith("__")})
    held.to_thread = to_thread
    monkeypatch.setattr(backup, "_copy_private", copy)
    monkeypatch.setattr(backup, "asyncio", held)

    made = asyncio.ensure_future(http.client.post("/api/admin/backup", headers=http.admin_headers))
    await asyncio.wait_for(config_copied.wait(), 20)
    write = asyncio.ensure_future(retire(http, document, without(document, "tv"), "tv"))
    await asyncio.wait({write}, timeout=2)
    finished_inside_the_backup = write.done()
    release.set()
    made, written = await asyncio.wait_for(made, 30), await asyncio.wait_for(write, 30)
    monkeypatch.setattr(backup, "asyncio", asyncio)
    monkeypatch.setattr(backup, "_copy_private", real_copy)

    assert made.status_code == 200, made.text
    assert written.status_code == 200 and written.json()["retired_jobs"] == 1      # the write itself succeeded
    point = backup.restore_point(made.json()["restore_point"]["id"])
    captured = _captured(point, job["id"])
    # One generation: the catalog from before the write beside the database
    # from before it -- never the old catalog beside the retired job.
    assert captured == {"revision": document["revision"], "profiles": ["movies", "tv"], "job_retired": False}
    assert not finished_inside_the_backup                     # the write waited for the copies
    assert (await demand_of(job["id"]))["withdrawn_at"] is not None              # and then took effect live

    # The point is a valid restore point, and restores as exactly that pair.
    await backup.validate_restore_point(point.id)
    staged = await backup.stage_restore(point.id)
    staged.validate()
    staged.activate()
    staged.commit()
    restored = json.loads(config.CONFIG_PATH.read_text())["external_delivery"]
    assert sorted(item["id"] for item in restored["profiles"]) == ["movies", "tv"]
    assert restored["revision"] == document["revision"]
    connection = sqlite3.connect(database.DB_PATH)
    try:
        assert connection.execute(
            "SELECT d.withdrawn_at FROM external_jobs j JOIN transfer_demands d ON d.id=j.demand_id WHERE j.id=?",
            (job["id"],)).fetchone()[0] is None
    finally:
        connection.close()


async def test_an_ordinary_backup_still_captures_the_current_pair(http, world, monkeypatch, tmp_path):  # noqa: F811
    from backup_support import prepare_backup_installation

    await prepare_backup_installation(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "_config_write_lock", asyncio.Lock())
    monkeypatch.setattr(backup, "_BACKUP_RUN_LOCK", asyncio.Lock())
    config.save_settings(config.get_settings())
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    document = await catalog(http)
    assert (await retire(http, document, without(document, "tv"), "tv")).status_code == 200
    made = await http.client.post("/api/admin/backup", headers=http.admin_headers)
    assert made.status_code == 200, made.text
    point = backup.restore_point(made.json()["restore_point"]["id"])
    assert _captured(point, job["id"]) == {"revision": document["revision"] + 1, "profiles": ["movies"],
                                           "job_retired": True}
