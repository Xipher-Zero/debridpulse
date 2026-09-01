import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Request, Response


def test_notification_boundary_preserves_null_object_and_added_webhook(monkeypatch):
    import services.notification_service as boundary

    monkeypatch.setattr(
        boundary,
        "get_settings",
        lambda: SimpleNamespace(discord_webhook_url="", discord_webhook_added=""),
    )
    client = boundary.NotificationService().client()
    assert client is not None
    assert client.webhook_url == ""
    assert client.added_webhook_url == ""
    asyncio.run(client.send_added("no-webhook submission"))

    monkeypatch.setattr(
        boundary,
        "get_settings",
        lambda: SimpleNamespace(
            discord_webhook_url="https://example.invalid/main",
            discord_webhook_added="https://example.invalid/added",
        ),
    )
    client = boundary.NotificationService().client()
    assert client.webhook_url.endswith("/main")
    assert client.added_webhook_url.endswith("/added")


@pytest.mark.asyncio
async def test_notification_accepted_and_update_paths_have_resolved_symbols(monkeypatch):
    import core.config
    from services.notifications import NotificationService

    client = NotificationService("https://example.invalid/main")
    client._send = AsyncMock(return_value=True)
    await client.send_added("x", transfer_id="123")

    monkeypatch.setattr(
        core.config,
        "get_settings",
        lambda: SimpleNamespace(discord_notify_update=True),
    )
    await client.send_update("1.0.4", "1.0.5")
    assert client._send.await_count == 2




def test_scheduler_update_notifications_use_service_boundary():
    source = (Path(__file__).resolve().parents[1] / "core" / "scheduler.py").read_text()
    assert "from services.notifications import notifier" not in source
    assert "NotificationService().client().send_update" in source








@pytest.mark.asyncio
async def test_database_json_backup_includes_operational_tables(tmp_path, monkeypatch):
    import db.database as database
    import services.db_maintenance as maintenance

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()
    backup_root = tmp_path / "db-backups"
    monkeypatch.setattr(
        maintenance,
        "get_settings",
        lambda: SimpleNamespace(
            db_backup_enabled=True,
            db_backup_folder=str(backup_root),
            db_backup_keep_days=7,
        ),
    )
    result = await maintenance.run_database_backup()
    assert result["errors"] == []
    assert "transfer_pause_intents" in result["tables"]
    assert "debridpulse_aria2_owned_gids" in result["tables"]


@pytest.mark.asyncio
async def test_online_backup_captures_committed_wal_state(tmp_path, monkeypatch):
    import db.database as database
    import services.backup as backup

    db_path = tmp_path / "live.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()
    async with database.get_db() as db:
        await db.execute("INSERT INTO torrents(hash, name) VALUES(?, ?)", ("wal-hash", "wal-row"))
        await db.commit()

    backup_root = tmp_path / "backups"
    monkeypatch.setattr(
        backup,
        "_cfg",
        lambda: SimpleNamespace(backup_enabled=True, backup_folder=str(backup_root), backup_keep_days=7),
    )
    result = await backup.run_backup()
    assert result["errors"] == []
    copied = Path(result["backup_dir"]) / db_path.name
    with sqlite3.connect(copied) as conn:
        assert conn.execute("SELECT name FROM torrents WHERE hash='wal-hash'").fetchone()[0] == "wal-row"


@pytest.mark.asyncio
async def test_service_permission_error_maps_to_http_403():
    import main

    response = await main.permission_error_handler(None, PermissionError("secret detail"))
    assert response.status_code == 403
    assert response.body == b"Forbidden"


def _request(method: str, path: str, headers: dict[str, str]) -> Request:
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )


@pytest.mark.asyncio
async def test_browser_security_rejects_cross_origin_mutation():
    from auth.middleware import enforce_general_web_security

    request = _request(
        "POST",
        "/api/does-not-exist",
        {
            "Origin": "https://evil.invalid",
            "Host": "testserver",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    call_next = AsyncMock(return_value=Response(status_code=204))
    response = await enforce_general_web_security(request, call_next)
    assert response.status_code == 403
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_security_allows_same_origin_and_nonbrowser_clients():
    from auth.middleware import enforce_general_web_security

    same_origin = _request(
        "POST",
        "/api/test",
        {"Origin": "http://testserver", "Host": "testserver"},
    )
    call_next = AsyncMock(return_value=Response(status_code=204))
    assert (await enforce_general_web_security(same_origin, call_next)).status_code == 204

    script_client = _request(
        "POST",
        "/api/test",
        {"Host": "testserver"},
    )
    call_next = AsyncMock(return_value=Response(status_code=204))
    assert (await enforce_general_web_security(script_client, call_next)).status_code == 204


def test_readme_describes_sqlite_only_runtime():
    root = Path(__file__).resolve().parents[2]
    readme = (root / "README.md").read_text()
    assert "SQLite or PostgreSQL" not in readme
    assert "external PostgreSQL" not in readme
    assert "SQLite/WAL" in readme
