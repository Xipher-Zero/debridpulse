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
    # The process-wide seal this restore sets is lifted again when the test ends.
    monkeypatch.setattr(database.database_maintenance_gate, "_sealed", False)
    calls.clear()
    response = await client.post("/api/admin/backups/restore", json={"id": point})
    monkeypatch.setattr(backup.os, "replace", real_replace)

    assert fired == ["install configuration", "reverse database install"]
    # Its own outcome: neither "unchanged" nor restored, and storage sealed until restart.
    assert response.status_code == 503
    assert "could not be put back" in response.json()["detail"] and "unchanged" not in response.json()["detail"]
    assert database.database_maintenance_gate.sealed and app.state.application._admission.sealed
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


# ── 1.0.13 WS R (FUNC-A09, POST-AUDIT-A09b): an unresolved restore fails closed ──
#
# The audit reproduced the loss: after an unresolved restore the process kept
# serving, one ordinary read recreated the displaced database empty (or one
# settings write recreated a default configuration), and the next start's
# reversal skipped the occupied path and swept the original away with the
# staging. Now: nothing in the process may open the database or act on the
# stored state until the next start, and that start never deletes an original
# it could not put back.

import hashlib

from db.database import DatabaseSealed
from services.maintenance_gate import ApplicationSealed

_POLICY = {"execution_retry_count": 3}


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _unresolved_with_the_database_displaced(monkeypatch):
    """The database is displaced, the configuration's displacement fails and
    the database's reversal fails: the live database path is empty."""
    real_replace, fired = os.replace, []

    def replace(source, target, *args, **kwargs):
        source, target = str(source), str(target)
        if source == str(config.CONFIG_PATH) and target.endswith(f"displaced-{config.CONFIG_PATH.name}"):
            fired.append("displace configuration")
            raise OSError(5, "injected: the configuration could not be displaced")
        if target == str(database.DB_PATH) and source.endswith(f"displaced-{database.DB_PATH.name}"):
            fired.append("reverse database displacement")
            raise OSError(5, "injected: the database could not be put back")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(backup.os, "replace", replace)
    return real_replace, fired


async def test_an_unresolved_restore_seals_storage_so_nothing_recreates_the_database_and_restart_returns_it(
        served, monkeypatch):  # noqa: F811
    app, client, _composed, _calls = served
    kept = await _submit(app, client, "kept")
    # The settings write used below, accepted while nothing is unresolved.
    assert (await client.patch("/api/transfer-policy", json=_POLICY)).status_code == 200
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    later = await _submit(app, client, "later")
    live_config = config.CONFIG_PATH.read_bytes()
    monkeypatch.setattr(database.database_maintenance_gate, "_sealed", False)
    real_replace, fired = _unresolved_with_the_database_displaced(monkeypatch)

    response = await client.post("/api/admin/backups/restore", json={"id": point})
    monkeypatch.setattr(backup.os, "replace", real_replace)

    assert fired == ["displace configuration", "reverse database displacement"]
    assert response.status_code == 503 and "unchanged" not in response.json()["detail"]
    assert not database.DB_PATH.exists()
    (original,) = database.DB_PATH.parent.glob(f"{backup._RESTORE_PREFIX}*/displaced-{database.DB_PATH.name}")
    original_sha = _sha(original)
    journal = database.DB_PATH.parent / backup._JOURNAL_NAME
    journal_bytes = journal.read_bytes()

    # What recreated the database empty in the audit: any read, here the
    # statistics and a direct session, and the restore's own validation.
    # (This test app has no exception handlers: the refusal is raised, where
    # the application answers it with 503 -- see the handler test below.)
    with pytest.raises(DatabaseSealed):
        await client.get("/api/stats")
    with pytest.raises(DatabaseSealed):
        async with database.get_db():
            pass
    with pytest.raises(sqlite3.OperationalError):
        backup.schema_version(database.DB_PATH)
    # What recreated a default configuration: a scoped settings write.
    with pytest.raises(ApplicationSealed):
        await client.patch("/api/transfer-policy", json=_POLICY)
    # POST-AUDIT-A09b: a second restore neither runs nor touches the journal.
    again = await client.post("/api/admin/backups/restore", json={"id": point})
    assert again.status_code == 503
    health = await client.get("/api/health")
    assert health.status_code == 503 and health.json()["status"] == "restore_unresolved"
    assert not database.DB_PATH.exists()
    assert config.CONFIG_PATH.read_bytes() == live_config
    assert journal.read_bytes() == journal_bytes and _sha(original) == original_sha

    # The next start (a new process: nothing is sealed) puts the original back.
    database.database_maintenance_gate._sealed = False
    assert backup.recover_interrupted_restore() is True
    assert _sha(database.DB_PATH) == original_sha
    assert _transfer_ids() == sorted([kept, later])
    assert config.CONFIG_PATH.read_bytes() == live_config
    assert not journal.exists()
    assert not list(database.DB_PATH.parent.glob(f"{backup._RESTORE_PREFIX}*"))


async def test_an_unresolved_restore_never_lets_a_settings_write_recreate_a_default_configuration(
        served, monkeypatch):  # noqa: F811
    """The configuration variant: its install fails and the database's
    reversal fails, so the live configuration path is empty."""
    app, client, _composed, _calls = served
    await _submit(app, client, "kept")
    assert (await client.patch("/api/transfer-policy", json=_POLICY)).status_code == 200
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    live_config = config.CONFIG_PATH.read_bytes()
    monkeypatch.setattr(database.database_maintenance_gate, "_sealed", False)
    real_replace = os.replace

    def replace(source, target, *args, **kwargs):
        source, target = str(source), str(target)
        if source.endswith("restored-config.json") and target == str(config.CONFIG_PATH):
            raise OSError(5, "injected: the configuration install failed")
        if source == str(database.DB_PATH) and target.endswith(f"restored-{database.DB_PATH.name}"):
            raise OSError(5, "injected: the reversal failed")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(backup.os, "replace", replace)
    assert (await client.post("/api/admin/backups/restore", json={"id": point})).status_code == 503
    monkeypatch.setattr(backup.os, "replace", real_replace)
    assert not config.CONFIG_PATH.exists()

    with pytest.raises(ApplicationSealed):
        await client.patch("/api/transfer-policy", json=_POLICY)
    assert not config.CONFIG_PATH.exists()

    database.database_maintenance_gate._sealed = False
    assert backup.recover_interrupted_restore() is True
    assert config.CONFIG_PATH.read_bytes() == live_config


async def test_startup_never_deletes_an_original_whose_place_was_taken(served, monkeypatch):  # noqa: F811
    """Should anything recreate the live path anyway, the next start chooses
    nothing: neither file is moved or deleted, the journal and the staging
    stay, and the process does not start on either."""
    app, client, _composed, _calls = served
    await _submit(app, client, "kept")
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    monkeypatch.setattr(database.database_maintenance_gate, "_sealed", False)
    real_replace, _fired = _unresolved_with_the_database_displaced(monkeypatch)
    assert (await client.post("/api/admin/backups/restore", json={"id": point})).status_code == 503
    monkeypatch.setattr(backup.os, "replace", real_replace)
    (original,) = database.DB_PATH.parent.glob(f"{backup._RESTORE_PREFIX}*/displaced-{database.DB_PATH.name}")
    original_sha = _sha(original)
    journal = database.DB_PATH.parent / backup._JOURNAL_NAME
    # Exactly what the audit's empty database was: a zero-byte file at the live path.
    database.DB_PATH.write_bytes(b"")
    database.database_maintenance_gate._sealed = False

    with pytest.raises(RuntimeError, match="cannot be reversed"):
        backup.recover_interrupted_restore()

    assert _sha(original) == original_sha
    assert database.DB_PATH.read_bytes() == b""
    assert journal.exists()


async def test_a_second_restore_never_overwrites_an_unresolved_journal(served, monkeypatch):  # noqa: F811
    """POST-AUDIT-A09b at the swap itself: a staged restore that finds another
    restore's journal moves nothing, and abandoning it leaves that journal."""
    app, client, _composed, _calls = served
    await _submit(app, client, "kept")
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    journal = database.DB_PATH.parent / backup._JOURNAL_NAME
    journal.write_text('{"moves": [], "stage_dirs": []}', encoding="utf-8")
    foreign = journal.read_bytes()
    live_db, live_config = _sha(database.DB_PATH), config.CONFIG_PATH.read_bytes()

    staged = await backup.stage_restore(point)
    async with database.database_maintenance():
        with pytest.raises(backup.BackupRejected) as refused:
            staged.activate()
    staged.discard()

    assert refused.value.reason == "unresolved"
    assert journal.read_bytes() == foreign
    assert _sha(database.DB_PATH) == live_db and config.CONFIG_PATH.read_bytes() == live_config
    assert not any(path.is_dir() for path in database.DB_PATH.parent.glob(f"{backup._RESTORE_PREFIX}*"))


async def test_the_application_answers_a_sealed_store_with_its_own_503():
    """main.py maps both seals to one plain answer: not maintenance, not unchanged."""
    from main import sealed_storage_handler

    for exc in (DatabaseSealed("x"), ApplicationSealed("x")):
        response = await sealed_storage_handler(None, exc)
        assert response.status_code == 503 and "Retry-After" not in response.headers
        assert b"restart it to put the previous state back" in response.body


async def test_a_rollback_whose_reversal_fails_is_unresolved_and_sealed_too(served, monkeypatch):  # noqa: F811
    """The restored state fails to start and putting the original database
    back fails as well: the same unresolved outcome as a failed swap."""
    app, client, _composed, _calls = served
    kept = await _submit(app, client, "kept")
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    later = await _submit(app, client, "later")
    live_config = config.CONFIG_PATH.read_bytes()
    monkeypatch.setattr(database.database_maintenance_gate, "_sealed", False)

    import application.lifetime as lifetime

    original_start, attempts = lifetime.start_application, []

    async def failing(service):
        attempts.append(service)
        raise RuntimeError("restored startup failed")

    real_replace = os.replace

    def replace(source, target, *args, **kwargs):
        if str(target) == str(database.DB_PATH) and str(source).endswith(f"displaced-{database.DB_PATH.name}"):
            raise OSError(5, "injected: the original could not be put back")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(lifetime, "start_application", failing)
    monkeypatch.setattr(backup.os, "replace", replace)
    response = await client.post("/api/admin/backups/restore", json={"id": point})
    monkeypatch.setattr(backup.os, "replace", real_replace)
    monkeypatch.setattr(lifetime, "start_application", original_start)

    assert len(attempts) == 1
    assert response.status_code == 503 and "unchanged" not in response.json()["detail"]
    assert database.database_maintenance_gate.sealed and app.state.application._admission.sealed
    assert (database.DB_PATH.parent / backup._JOURNAL_NAME).exists()
    with pytest.raises(DatabaseSealed):
        async with database.get_db():
            pass
    assert not database.DB_PATH.exists()

    database.database_maintenance_gate._sealed = False
    assert backup.recover_interrupted_restore() is True
    assert _transfer_ids() == sorted([kept, later])
    assert config.CONFIG_PATH.read_bytes() == live_config


async def test_a_restore_cancelled_mid_swap_finishes_the_swap_then_seals_for_the_next_start(served, monkeypatch):  # noqa: F811
    """Cancellation (a shutdown) never unwinds the restore while its swap is
    still moving files, nor leaves the replaced application serving swapped
    files: the swap completes, storage is sealed, and the journal reverses it
    at the next start."""
    import asyncio
    import threading

    import application.lifetime as lifetime

    app, client, _composed, _calls = served
    kept = await _submit(app, client, "kept")
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    later = await _submit(app, client, "later")
    monkeypatch.setattr(database.database_maintenance_gate, "_sealed", False)
    started, proceed = threading.Event(), threading.Event()
    real_activate = backup.StagedRestore.activate

    def held_activate(staged):
        started.set()
        proceed.wait(10)
        return real_activate(staged)

    monkeypatch.setattr(backup.StagedRestore, "activate", held_activate)
    restore = asyncio.create_task(lifetime.restore_backup(app.state, point))
    assert await asyncio.to_thread(started.wait, 10)
    restore.cancel()
    await asyncio.sleep(0.05)
    assert not restore.done(), "the restore unwound while its swap was still moving files"
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await restore

    assert database.database_maintenance_gate.sealed and app.state.application._admission.sealed
    assert (database.DB_PATH.parent / backup._JOURNAL_NAME).exists()
    database.database_maintenance_gate._sealed = False
    assert backup.recover_interrupted_restore() is True
    assert _transfer_ids() == sorted([kept, later])


@pytest.mark.parametrize("refusal", [DatabaseSealed, ApplicationSealed])
async def test_authentication_against_a_sealed_store_answers_the_same_503(monkeypatch, refusal):
    """Token, Basic or session checks run before the exception handlers."""
    import main

    async def sealed(_request, _call_next):
        raise refusal("x")

    monkeypatch.setattr(main, "enforce_authentication", sealed)
    response = await main.authentication_boundary_middleware(None, None)
    assert response.status_code == 503 and b"restart it to put the previous state back" in response.body


async def test_a_restore_cancelled_while_the_restored_state_starts_seals_for_the_next_start(  # noqa: F811
        served, monkeypatch):  # noqa: F811
    """After the swap, a cancellation (a shutdown) during the restored state's
    startup leaves the replaced application serving nothing: storage is sealed
    and the journal reverses the swap at the next start."""
    import asyncio

    import application.lifetime as lifetime

    app, client, _composed, _calls = served
    kept = await _submit(app, client, "kept")
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    later = await _submit(app, client, "later")
    monkeypatch.setattr(database.database_maintenance_gate, "_sealed", False)

    async def interrupted(_service):
        raise asyncio.CancelledError

    monkeypatch.setattr(lifetime, "start_application", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await lifetime.restore_backup(app.state, point)

    assert database.database_maintenance_gate.sealed and app.state.application._admission.sealed
    assert (database.DB_PATH.parent / backup._JOURNAL_NAME).exists()
    with pytest.raises(DatabaseSealed):
        async with database.get_db():
            pass
    database.database_maintenance_gate._sealed = False
    assert backup.recover_interrupted_restore() is True
    assert _transfer_ids() == sorted([kept, later])
