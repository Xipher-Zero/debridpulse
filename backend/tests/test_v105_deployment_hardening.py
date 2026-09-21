import re
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_install_surfaces_preserve_published_image_and_public_health_endpoint():
    root = Path(__file__).resolve().parents[2]
    # Install surfaces name the newest published release, derived from the
    # canonical CHANGELOG. VERSION owns the in-development version, which on a
    # development line has no published image, so install surfaces must not
    # fabricate a tag for it.
    headings = re.findall(
        r"^## \[(\d+(?:\.\d+)+)\](.*)$", (root / "CHANGELOG.md").read_text(), re.M
    )
    released = next(v for v, title in headings if "development" not in title.casefold())
    expected_image = f"ghcr.io/xipher-zero/debridpulse:v{released}"
    compose = (root / "docker-compose.yml").read_text()
    readme = (root / "README.md").read_text()
    project_page = (root / "index.html").read_text()
    assert expected_image in compose
    assert readme.count(expected_image) >= 2
    assert expected_image in project_page
    development = (root / "VERSION").read_text().strip()
    if development != released:
        unreleased = f"ghcr.io/xipher-zero/debridpulse:v{development}"
        for surface in (compose, readme, project_page):
            assert unreleased not in surface
    assert "http://localhost:8080/api/health" in compose
    assert "http://localhost:8080/api/stats" not in compose


@pytest.mark.asyncio
async def test_database_json_backup_is_private(tmp_path, monkeypatch):
    import db.database as database
    import services.db_maintenance as maintenance

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()

    backup_root = tmp_path / "exports"
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
    backup_dir = Path(result["backup_dir"])
    backup_file = Path(result["file"])

    assert result["errors"] == []
    assert _mode(backup_root) == 0o700
    assert _mode(backup_dir) == 0o700
    assert _mode(backup_file) == 0o600


@pytest.mark.asyncio
async def test_online_backup_sqlite_copy_is_private(tmp_path, monkeypatch):
    import db.database as database
    import services.backup as backup

    db_path = tmp_path / "live.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()
    async with database.get_db() as db:
        await db.execute("INSERT INTO torrents(hash, name) VALUES(?, ?)", ("mode-test", "mode-test"))
        await db.commit()

    backup_root = tmp_path / "backups"
    monkeypatch.setattr(
        backup,
        "_cfg",
        lambda: SimpleNamespace(
            backup_enabled=True,
            backup_folder=str(backup_root),
            backup_keep_days=7,
        ),
    )
    result = await backup.run_backup()
    backup_dir = Path(result["backup_dir"])
    db_copy = backup_dir / db_path.name

    assert result["errors"] == []
    assert _mode(backup_root) == 0o700
    assert _mode(backup_dir) == 0o700
    assert _mode(db_copy) == 0o600
