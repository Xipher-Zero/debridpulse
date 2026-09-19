"""Global processing pause is operational state with exactly one authority.

The durable application state (``TransferRepository.globally_paused``) is the only
truth. ``AppSettings`` carries no ``paused`` field, nothing writes the pause back
into configuration, ``/stats`` and the pause/resume results project from the
authority, and the frontend keeps only a non-persisted projection of it. A
pre-1.0.12 ``config.json`` that still holds ``"paused"`` is a one-way migration
input consumed by the database migration; it never becomes a setting.
"""
import json
import re
from pathlib import Path

import pytest

import core.config as config
from test_application_runtime import runtime  # noqa: F401  (shared fixture)

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
STATIC = ROOT / "frontend" / "static"


def test_pause_is_not_a_setting_and_is_never_served_or_persisted(tmp_path, monkeypatch):
    assert "paused" not in config.AppSettings.model_fields
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"paused": True, "download_folder": str(tmp_path)}))
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(config, "_settings", config.AppSettings())

    loaded = config.load_settings()
    config.apply_settings(loaded)
    config.save_settings(loaded)

    assert "paused" not in json.loads(path.read_text()), "pause must not be written back into configuration"
    assert not hasattr(config.get_settings(), "paused")
    # The legacy value is offered once, as migration input only.
    assert config.legacy_paused_input() is True


@pytest.mark.asyncio
async def test_stats_and_operations_project_from_the_durable_authority(runtime, monkeypatch):  # noqa: F811
    application, _provider, _executor, client = runtime
    import api.routes as routes

    assert (await client.get("/api/stats")).json()["paused"] is False
    paused = await client.post("/api/processing/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["paused"] is True
    assert await application.repository.globally_paused() is True
    assert (await client.get("/api/stats")).json()["paused"] is True
    assert "paused" not in (await client.get("/api/settings")).json()

    resumed = await client.post("/api/processing/resume")
    assert resumed.json()["paused"] is False
    assert await application.repository.globally_paused() is False
    assert (await client.get("/api/stats")).json()["paused"] is False
    # Configuration was never consulted or mutated on the way.
    assert not hasattr(routes.get_settings(), "paused")


@pytest.mark.asyncio
async def test_the_settings_document_cannot_change_the_pause_authority(runtime, tmp_path, monkeypatch):  # noqa: F811
    application, _provider, _executor, client = runtime
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "_settings", config.get_settings())  # restored on teardown
    current = (await client.get("/api/settings")).json()
    current["paused"] = True
    response = await client.put("/api/settings", json=current)
    assert response.status_code in {200, 400, 422}, response.text
    assert await application.repository.globally_paused() is False
    assert (await client.get("/api/stats")).json()["paused"] is False


def test_no_write_back_synchronization_or_config_mirror_remains():
    service = (BACKEND / "application" / "service.py").read_text()
    composition = (BACKEND / "application" / "composition.py").read_text()
    routes = (BACKEND / "api" / "routes.py").read_text()
    assert "pause_changed" not in service + composition
    assert "save_settings" not in composition
    assert "save_settings" not in service
    assert not re.search(r"get_settings\(\)\.paused|settings\.paused|cfg\.paused|\"paused\"\)\s*\)", routes)
    assert "paused=" not in re.sub(r"transfer\.paused|globally_paused|_paused", "", composition)
    frontend = "\n".join(path.read_text() for path in STATIC.glob("*.js"))
    assert not re.search(r"settingsData\??\.paused", frontend)
    assert "processingPaused = !!s.paused" in (STATIC / "app.js").read_text()
