"""Test support for proofs whose subject is what a DebridPulse backup captures.

A restore point is created only from a complete installation: a database the
v1.0.12 schema owner has marked current, and a persisted configuration. These
helpers give a test's already-initialized database exactly that, then let the
one creation owner (``services.backup.create_restore_point``) do the rest.
"""
from __future__ import annotations

import json
from pathlib import Path


async def prepare_backup_installation(tmp_path: Path, monkeypatch) -> None:
    import core.config as config
    from db.migrations.v112 import _mark_current

    await _mark_current()
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "backup-config" / "config.json")
    monkeypatch.setattr(config.get_settings(), "backup_folder", str(tmp_path / "backups"))


async def restore_point_database(tmp_path: Path, monkeypatch) -> Path:
    """Create one real restore point and return its database member."""
    from services import backup

    await prepare_backup_installation(tmp_path, monkeypatch)
    point = await backup.create_restore_point()
    manifest = json.loads((point.path / ".debridpulse-backup.json").read_text(encoding="utf-8"))
    return point.path / manifest["database"]
