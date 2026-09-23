"""1.0.13 Gate-9 rev-3, item 4: ownership fencing covers BOTH halves.

A paired integration configures one namespace but registers two durable
identities (a provider and an executor). Configuration changes that could
abandon owned work must be fenced against ALL of them, or a live execution
recorded only under the executor identity would be invisible to the fence.

The mechanism is generic: a definition declares the identities its
configuration owns, and the repository fences against that set.
"""
from __future__ import annotations

import pytest

import db.database as database
from integrations.catalog import definitions
from transfers.recovery_repository import TransferRepository


def definition_for(identity):
    return next(item for item in definitions if item.id == identity)


# --- the declaration is generic -----------------------------------------

def test_every_definition_declares_the_identities_it_owns():
    for definition in definitions:
        owned = definition.owned_identities
        assert isinstance(owned, frozenset) and owned
        # A single-implementation integration owns exactly its own id.
        if definition.kind != "provider_executor":
            assert owned == frozenset({definition.id})


def test_a_paired_definition_owns_both_halves():
    usenet = definition_for("usenet")
    assert usenet.kind == "provider_executor"
    assert usenet.owned_identities == frozenset({"usenet", "sabnzbd"})


def test_the_declared_identities_match_what_is_actually_registered(tmp_path):
    """The declaration must not drift from the real descriptors."""
    from types import SimpleNamespace
    from integrations.catalog import register
    from integrations.definition import IntegrationEnvironment, IntegrationSettings
    from transfers.registry import IntegrationRegistry

    settings = SimpleNamespace(integrations={d.id: IntegrationSettings() for d in definitions})
    registry = IntegrationRegistry()
    register(registry, settings, IntegrationEnvironment(
        SimpleNamespace(authorize_execution=None), str(tmp_path)))
    registered = set(registry.providers) | set(registry.executors)
    declared = set().union(*(d.owned_identities for d in definitions))
    assert declared == registered, (declared ^ registered)


def test_no_definition_special_cases_a_named_integration():
    """The fence must be generic, not a usenet/sabnzbd branch."""
    import inspect
    from application import service
    source = inspect.getsource(service.ApplicationService.validate_configuration)
    for name in ("usenet", "sabnzbd", "aria2", "alldebrid"):
        assert name not in source.lower()


# --- the fence actually counts an executor-only reference ----------------

VALID_NZB = b"""<?xml version="1.0" encoding="iso-8859-1" ?>
<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
 <file poster="p@e.net" date="1700000000" subject="job [1/1] - &quot;job.bin&quot; yEnc (1/1)">
  <groups><group>alt.binaries.test</group></groups>
  <segments><segment bytes="1024" number="1">a@e.net</segment></segments>
 </file>
</nzb>
"""


async def owned_execution(tmp_path, monkeypatch):
    """Drive the REAL engine until one Usenet execution is durably owned."""
    import asyncio
    from types import SimpleNamespace
    from executors.sabnzbd.executor import SabnzbdConfiguration, SabnzbdExecutor
    from providers.usenet.provider import UsenetProvider
    from sab_fakes import FakeSab, staged_store
    from transfers.convergence_engine import TransferEngine
    from transfers.models import TransferRequest
    from transfers.policy import TransferPolicy
    from transfers.registry import IntegrationRegistry

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    root = tmp_path / "payloads"
    (root / ".dpwork" / "complete").mkdir(parents=True)
    (root / ".dpwork" / "incomplete").mkdir(parents=True)
    sab = FakeSab(complete_dir=str(root / ".dpwork" / "complete"),
                  download_dir=str(root / ".dpwork" / "incomplete"))
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
    await engine.initialize()
    await engine.submit((TransferRequest("nzb", VALID_NZB, name="job.nzb"),), name="job",
                        deduplicate=False)
    for _ in range(8):
        await engine.tick()
        await asyncio.sleep(0)
    return repository, sab


@pytest.mark.asyncio
async def test_a_live_execution_under_the_executor_identity_is_a_reference(tmp_path, monkeypatch):
    repository, sab = await owned_execution(tmp_path, monkeypatch)
    assert sab.submissions, "the harness must actually own a native job"

    # The provider identity alone sees nothing: the attempt is recorded under
    # the EXECUTOR identity, which is exactly the revision-2 blind spot.
    assert await repository.has_integration_references("usenet") is False
    assert await repository.has_integration_references("sabnzbd") is True
    # The paired set therefore must see it.
    assert await repository.has_integration_references(
        definition_for("usenet").owned_identities) is True


@pytest.mark.asyncio
async def test_changing_a_fenced_option_while_work_is_owned_is_refused(tmp_path, monkeypatch):
    """A news-server change can invalidate an owned native acquisition."""
    from types import SimpleNamespace
    from core.config import AppSettings
    from integrations.definition import IntegrationSettings
    from integrations.usenet.definition import UsenetOptions, UsenetServer
    from application.service import ApplicationService

    repository, _ = await owned_execution(tmp_path, monkeypatch)
    service = ApplicationService(SimpleNamespace(repository=repository))
    service.repository = repository
    service.definitions = definitions

    def settings_with(servers):
        value = AppSettings()
        value.integrations = {d.id: IntegrationSettings() for d in definitions}
        value.integrations["usenet"] = IntegrationSettings(
            enabled=True, options=UsenetOptions(servers=servers).model_dump())
        return value

    before = settings_with([UsenetServer(host="news.a.net")])
    after = settings_with([UsenetServer(host="news.b.net")])
    with pytest.raises(ValueError):
        await service.validate_configuration(before, after)


@pytest.mark.asyncio
async def test_an_unfenced_option_may_still_change_while_work_is_owned(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from core.config import AppSettings
    from integrations.definition import IntegrationSettings
    from integrations.usenet.definition import UsenetOptions, UsenetServer
    from application.service import ApplicationService

    repository, _ = await owned_execution(tmp_path, monkeypatch)
    service = ApplicationService(SimpleNamespace(repository=repository))
    service.repository = repository
    service.definitions = definitions

    def settings_with(timeout):
        value = AppSettings()
        value.integrations = {d.id: IntegrationSettings() for d in definitions}
        value.integrations["usenet"] = IntegrationSettings(
            enabled=True, options=UsenetOptions(
                operation_timeout_seconds=timeout,
                servers=[UsenetServer(id="fixed", host="news.a.net")]).model_dump())
        return value

    # Tuning is not ownership: it never invalidates an owned acquisition.
    await service.validate_configuration(settings_with(30), settings_with(45))
