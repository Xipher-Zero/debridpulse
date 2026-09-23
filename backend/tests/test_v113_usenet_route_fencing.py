"""1.0.13 Gate-9 rev-4, item 2: the ROUTES are fenced, not just the helper.

Server mutations must pass through the same canonical
`ApplicationService.validate_configuration` every other settings write uses.
These drive the real HTTP routes with a genuinely durable `sabnzbd` execution
present -- calling the validator directly would prove nothing about the path.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import db.database as database
from api.routes import router
from application.dependencies import get_application
from application.service import ApplicationService
from integrations.catalog import definitions
from transfers.models import TransferRequest

VALID_NZB = (b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
             b'<file poster="p@e.net" date="1700000000" subject="job [1/1] - &quot;job.bin&quot; yEnc (1/1)">'
             b"<groups><group>alt.binaries.test</group></groups>"
             b'<segments><segment bytes="1024" number="1">a@e.net</segment></segments>'
             b"</file></nzb>")


@pytest.fixture
def usenet_app(tmp_path, monkeypatch):
    """A real app wired to a real engine, with Usenet enabled and one server."""
    from core import config as core_config
    from executors.sabnzbd.executor import SabnzbdConfiguration, SabnzbdExecutor
    from integrations.definition import IntegrationSettings
    from integrations.usenet.definition import UsenetOptions, UsenetServer
    from providers.usenet.provider import UsenetProvider
    from sab_fakes import FakeSab, staged_store
    from transfers.convergence_engine import TransferEngine
    from transfers.policy import TransferPolicy
    from transfers.recovery_repository import TransferRepository
    from transfers.registry import IntegrationRegistry

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(core_config, "CONFIG_PATH", tmp_path / "config.json", raising=False)
    # This fixture owns its loop outright. Borrowing the ambient one via
    # asyncio.get_event_loop() only works when some earlier test happens to
    # have left a usable loop installed, and the repository's connections bind
    # to whichever loop opened them -- so the lab and every test that drives it
    # must share exactly this one.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(database.init_db())

    root = tmp_path / "payloads"
    (root / ".dpwork" / "complete").mkdir(parents=True)
    (root / ".dpwork" / "incomplete").mkdir(parents=True)
    sab = FakeSab(complete_dir=str(root / ".dpwork" / "complete"),
                  download_dir=str(root / ".dpwork" / "incomplete"))

    repository = TransferRepository()
    registry = IntegrationRegistry()
    registry.register_provider(UsenetProvider(staged_input=staged_store()))
    registry.register_executor(SabnzbdExecutor(
        sab, SabnzbdConfiguration(local_root=str(root),
                                  working_directory=str(root / ".dpwork"),
                                  complete_directory=str(root / ".dpwork" / "complete")),
        repository.authorize_execution, staged_input=staged_store()))
    engine = TransferEngine(repository, registry, download_root=str(root),
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                                                  max_active_executions=2),
                            clock=lambda: 1000.0)

    server = UsenetServer(host="news.a.net", username="u", password="p")
    settings = core_config.get_settings()
    settings.download_folder = str(root)
    settings.integrations = {
        **{d.id: IntegrationSettings() for d in definitions},
        "usenet": IntegrationSettings(enabled=True,
                                      options=UsenetOptions(servers=[server]).model_dump()),
    }
    core_config.apply_settings(settings)
    core_config.save_settings(settings)

    service = ApplicationService(engine)
    service.definitions = definitions
    service.configuration_appliers = {}

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_application] = lambda: service

    async def boot():
        await engine.initialize()
    loop.run_until_complete(boot())
    lab = type("Lab", (), {"client": TestClient(app), "engine": engine, "sab": sab,
                           "server": server, "service": service, "loop": loop})()
    yield lab
    asyncio.set_event_loop(None)
    loop.close()


async def own_an_execution(lab):
    await lab.engine.submit((TransferRequest("nzb", VALID_NZB, name="job.nzb"),),
                            name="job", deduplicate=False)
    for _ in range(8):
        await lab.engine.tick()
        await asyncio.sleep(0)


def test_server_mutations_are_allowed_while_no_work_is_owned(usenet_app):
    response = usenet_app.client.post("/api/usenet/servers", json={
        "host": "news.b.net", "username": "u2", "password": "p2"})
    assert response.status_code == 200, response.text


def test_updating_a_server_is_refused_while_owned_work_exists(usenet_app):
    usenet_app.loop.run_until_complete(own_an_execution(usenet_app))
    assert usenet_app.sab.submissions, "the fixture must own a native job"
    response = usenet_app.client.put(
        f"/api/usenet/servers/{usenet_app.server.id}", json={"host": "news.changed.net"})
    assert response.status_code == 409, response.text


def test_adding_a_server_is_refused_while_owned_work_exists(usenet_app):
    usenet_app.loop.run_until_complete(own_an_execution(usenet_app))
    response = usenet_app.client.post("/api/usenet/servers", json={
        "host": "news.new.net", "username": "u", "password": "p"})
    assert response.status_code == 409, response.text


def test_removing_a_server_is_refused_while_owned_work_exists(usenet_app):
    usenet_app.loop.run_until_complete(own_an_execution(usenet_app))
    response = usenet_app.client.delete(f"/api/usenet/servers/{usenet_app.server.id}")
    assert response.status_code == 409, response.text


def test_the_refusal_comes_from_the_canonical_fence_not_usenet_code():
    """The fence lives in ApplicationService; Usenet must not re-implement it."""
    import inspect
    from api import routes
    helper = inspect.getsource(routes._mutate_usenet_servers)
    assert "validate_configuration" in helper
    assert "has_integration_references" not in helper
    from integrations.usenet import servers as servers_module
    body = inspect.getsource(servers_module)
    for forbidden in ("has_integration_references", "validate_configuration", "owned_identities"):
        assert forbidden not in body, forbidden
