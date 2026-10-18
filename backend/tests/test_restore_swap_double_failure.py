"""1.0.13 WS6b PA-STORE-3: a restore whose swap and reversal both fail keeps what reverses it.

A swap step that failed was reversed and the staging dropped. If that
reversal failed as well, the restore route went on to discard the staging --
the journal and the displaced live files with it -- reopened the previous
application on the half-swapped database and reported that the current state
was left unchanged. Such a restore is now unresolved: nothing reversing it is
discarded, nothing is reopened or reported as unchanged, and the next start
puts the previous state back from the journal.

Real restore route, real staging and swap; the two failures are injected at
exactly the swap's renames (installing the configuration, then reversing the
database install).
"""
from __future__ import annotations

import os
import sqlite3

import pytest

import core.config as config
import db.database as database
from services import backup
from test_backup_restore_points import _submit, installation, served  # noqa: F401

pytestmark = pytest.mark.asyncio


def _transfer_ids() -> list[int]:
    with sqlite3.connect(database.DB_PATH) as conn:
        return sorted(row[0] for row in conn.execute("SELECT id FROM torrents"))


async def test_a_swap_whose_reversal_also_fails_is_left_recoverable_and_never_reported_unchanged(  # noqa: F811
        served, monkeypatch):  # noqa: F811
    app, client, _composed, calls = served
    kept = await _submit(app, client, "kept")
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    later = await _submit(app, client, "later")
    live_config = config.CONFIG_PATH.read_bytes()
    real_replace, fired = os.replace, []

    def replace(source, target, *args, **kwargs):
        source, target = str(source), str(target)
        if source.endswith("restored-config.json") and target == str(config.CONFIG_PATH):
            fired.append("install configuration")
            raise OSError(5, "injected: the configuration install failed")
        if source == str(database.DB_PATH) and target.endswith(f"restored-{database.DB_PATH.name}"):
            fired.append("reverse database install")
            raise OSError(5, "injected: the reversal failed")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(backup.os, "replace", replace)
    calls.clear()
    with pytest.raises(OSError, match="reversal failed"):
        await client.post("/api/admin/backups/restore", json={"id": point})
    monkeypatch.setattr(backup.os, "replace", real_replace)

    assert fired == ["install configuration", "reverse database install"]
    # Unresolved, and kept so: the journal and the displaced live database.
    assert (database.DB_PATH.parent / backup._JOURNAL_NAME).exists()
    displaced = list(database.DB_PATH.parent.glob(f"{backup._RESTORE_PREFIX}*/displaced-{database.DB_PATH.name}"))
    assert len(displaced) == 1
    # Nothing was reopened on the half-swapped state, and nothing claimed it unchanged.
    assert [name for name, _service in calls] == ["stop"]
    with sqlite3.connect(database.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM event_journal "
                            "WHERE event_type='administration.restore_refused'").fetchone()[0] == 0

    # The next start reverses it from the journal: exactly the previous state.
    assert backup.recover_interrupted_restore() is True
    assert _transfer_ids() == sorted([kept, later])
    assert config.CONFIG_PATH.read_bytes() == live_config
    assert not list(database.DB_PATH.parent.glob(f"{backup._RESTORE_PREFIX}*"))
    assert not list(config.CONFIG_PATH.parent.glob(f"{backup._RESTORE_PREFIX}*"))


async def test_a_failed_swap_that_reverses_cleanly_is_still_refused_as_unchanged(served, monkeypatch):  # noqa: F811
    app, client, _composed, _calls = served
    kept = await _submit(app, client, "kept")
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    later = await _submit(app, client, "later")
    real_replace = os.replace

    def replace(source, target, *args, **kwargs):
        if str(source).endswith("restored-config.json") and str(target) == str(config.CONFIG_PATH):
            raise OSError(5, "injected: the configuration install failed")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(backup.os, "replace", replace)
    response = await client.post("/api/admin/backups/restore", json={"id": point})
    monkeypatch.setattr(backup.os, "replace", real_replace)

    assert response.status_code == 400 and "left unchanged" in response.json()["detail"]
    assert _transfer_ids() == sorted([kept, later])
    assert not (database.DB_PATH.parent / backup._JOURNAL_NAME).exists()
    assert not list(database.DB_PATH.parent.glob(f"{backup._RESTORE_PREFIX}*"))
