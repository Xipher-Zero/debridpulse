"""1.0.13 WS6b PA-STORE-4: a restore point restores only with the running database's own indexes, triggers and views.

A restore point is compatible when its schema fingerprint -- every table's
columns and the schema markers -- equals the running one. Indexes, triggers
and views are not part of it, so a restore point whose database carries one
the running database does not (or lacks one it has) was admitted and
restored, and changed how the restored state behaves: one extra trigger made
every later link submission fail. Restore validation now also requires
exactly the running database's explicitly defined indexes, triggers and views;
the fingerprint, and every historical restore point it accepts, are unchanged
(the pinned historical backups restore as before: ``test_backup_restore_points``).
"""
from __future__ import annotations

import io
import sqlite3
import zipfile

import pytest

import db.database as database
from services import backup
from test_backup_restore_points import _submit, installation, served  # noqa: F401

pytestmark = pytest.mark.asyncio

SWALLOW = "CREATE TRIGGER dp_swallow AFTER INSERT ON torrents BEGIN DELETE FROM torrents WHERE id=NEW.id; END"


def _defined(path) -> set[str]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('index','trigger','view') AND sql IS NOT NULL")}


async def _altered_point(client, change) -> str:
    """The real Save Backup package of a fresh point, its database altered by
    ``change`` (same tables and columns, so the same fingerprint), offered back
    through Add Backup in place of the original."""
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    package = (await client.get(f"/api/admin/backups/{point}/package")).content
    assert (await client.delete(f"/api/admin/backups/{point}")).status_code == 200
    source, out = zipfile.ZipFile(io.BytesIO(package)), io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == database.DB_PATH.name:
                scratch = backup._folder() / "altered.db"
                scratch.write_bytes(data)
                with sqlite3.connect(scratch) as conn:
                    change(conn)
                data = scratch.read_bytes()
                scratch.unlink()
            target.writestr(info, data)
    added = await client.post("/api/admin/backups", content=out.getvalue(), headers={"Content-Type": "application/zip"})
    assert added.status_code == 200, added.text               # admission is the fingerprint's, unchanged
    return added.json()["backup"]["id"]


def _drop_one_index(conn) -> None:
    name = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL "
                        "ORDER BY name LIMIT 1").fetchone()[0]
    conn.execute(f'DROP INDEX "{name}"')


@pytest.mark.parametrize("change", [lambda conn: conn.execute(SWALLOW), _drop_one_index],
                         ids=["an_extra_trigger", "a_missing_index"])
async def test_a_point_whose_indexes_triggers_or_views_differ_is_refused_and_changes_nothing(  # noqa: F811
        served, change):  # noqa: F811
    app, client, _composed, _calls = served
    kept = await _submit(app, client, "kept")
    before = _defined(database.DB_PATH)
    point = await _altered_point(client, change)

    restored = await client.post("/api/admin/backups/restore", json={"id": point})

    assert restored.status_code == 400
    assert restored.json()["detail"] == ("Backup could not be restored. The backup is not a valid "
                                         "DebridPulse backup. The current DebridPulse state was left unchanged.")
    assert _defined(database.DB_PATH) == before and "dp_swallow" not in before
    # The live state goes on working exactly as before.
    later = await _submit(app, client, "later")
    assert sorted(item.id for item in await app.state.application.repository.active()) == sorted([kept, later])


async def test_an_unaltered_point_still_restores(served):  # noqa: F811
    app, client, _composed, _calls = served
    await _submit(app, client, "kept")
    point = await _altered_point(client, lambda conn: None)
    restored = await client.post("/api/admin/backups/restore", json={"id": point})
    assert restored.status_code == 200, restored.text
