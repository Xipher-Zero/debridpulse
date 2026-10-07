"""A creation the provider may have made is reconciled, never repeated blindly.

A provider that was asked to create a root's resource and gave no usable
answer -- transfer 524: Debrid-Link created the torrent but answered a
nominal success that was not JSON -- may have created it. That failure says
so (``MutationOutcome.UNCERTAIN``): the attempt stays open on its route and
the provider's own inventory settles it -- exactly one match by the request's
fingerprint is the root's resource, a complete inventory without one proves
nothing was created and the attempt ends as the ordinary failure it then was,
anything less certain waits within the ordinary retry budget. A refusal that
proves nothing was created never waits. The same holds across a restart.

The providers are neutral fixtures.
"""
from __future__ import annotations

import math

import pytest
from test_v113_collection_route_generic_closure import Clock
from test_v113_standby_preparation import lab, magnet, root_of, route_providers

from db.database import get_db
from transfers.errors import Category, Domain, MutationOutcome, NormalizedError, Origin, Stage, TransferError
from transfers.models import (
    Capability, IntegrationDescriptor, OutcomeKind, Ownership, ProviderObservation, ProviderResource,
    ResolutionResult, ResourceSnapshot, ResourceState, TransferOutcome,
)
from transfers.policy import TransferPolicy

pytestmark = pytest.mark.asyncio

HASH = "a" * 40
POLL = 31.0                                        # past the resource poll interval


def lost_answer(provider_id, *, mutation=MutationOutcome.UNCERTAIN):
    return NormalizedError(Domain.PROVIDER, Category.PROVIDER_PROTOCOL_VIOLATION, Stage.RESOLUTION,
                           origin=Origin.PROVIDER, integration_id=provider_id, mutation=mutation)


class Creator:
    """A neutral BitTorrent provider whose scripted creations can fail; its
    inventory answers ``snapshot``."""

    def __init__(self, identity, *, priority=10, fails=()):
        self.descriptor = IntegrationDescriptor(
            identity, identity, frozenset({Capability.RESOLVE, Capability.RESOURCE_LOOKUP, Capability.INVENTORY,
                                           Capability.CLEANUP}),
            request_types=frozenset({"magnet"}), priority=priority)
        self.fails = list(fails)
        self.creations = 0
        self.inventories = 0
        self.snapshot = ResourceSnapshot((), complete=True)
        self.cleaned = []

    def held(self, n=1, *, state=ResourceState.PREPARING):
        resource = ProviderResource(self.descriptor.id, {"n": n}, Ownership.OBSERVED, id=f"{self.descriptor.id}:{n}")
        return ProviderObservation(resource, state, "Show", fingerprint=HASH)

    async def resolve(self, request):
        self.creations += 1
        if self.fails:
            raise TransferError(self.fails.pop(0))
        resource = ProviderResource(self.descriptor.id, {"n": self.creations}, Ownership.CREATED,
                                    id=f"{self.descriptor.id}:{self.creations}")
        observation = ProviderObservation(resource, ResourceState.PREPARING, "Show", request=request)
        return ResolutionResult(ResourceState.PREPARING, observation=observation)

    async def observe(self, resource):
        return ProviderObservation(resource, ResourceState.PREPARING, "Show")

    async def inventory(self):
        self.inventories += 1
        return self.snapshot

    async def cleanup(self, directive):
        self.cleaned.append(directive.resource.id)
        return TransferOutcome(OutcomeKind.SUCCESS)


async def attempts(request_id):
    async with get_db() as db:
        rows = await db.fetchall("SELECT provider_id,state FROM resolution_attempts WHERE request_id=? ORDER BY rowid",
                                 (request_id,))
    return [(row["provider_id"], row["state"]) for row in rows]


async def owed(request_id):
    """Whether a creation reconciliation is still owed for the request."""
    async with get_db() as db:
        row = await db.fetchone("SELECT COUNT(*) AS n FROM resolution_attempts WHERE request_id=? "
                                "AND reconcile_at IS NOT NULL", (request_id,))
    return bool(row["n"])


async def opened(tmp_path, monkeypatch, *providers):
    clock = Clock()
    # The live re-entry interval (transfer 523): a provider whose route ended
    # transiently does not re-enter before another provider proceeds.
    repository, _registry, engine = await lab(tmp_path, monkeypatch, *providers, clock=clock,
                                              policy=TransferPolicy(retry_delay=60.0, max_attempts=3))
    transfer = await engine.submit((magnet(),), name="Show", deduplicate=False)
    await engine.resolve_pending()
    return repository, engine, clock, transfer


async def test_an_uncertain_creation_is_held_across_restart_and_adopted_never_created_again(tmp_path, monkeypatch):
    """A-T6, A-T7, A-T10: no answer named the torrent, so nothing may treat it
    as absent -- the root keeps its open attempt on that route, no other
    provider takes over and nothing is created again; after a restart the one
    inventory match is adopted onto that attempt and owned like any resource."""
    creator, alternate = Creator("parcel-a", priority=20, fails=[lost_answer("parcel-a")]), Creator("parcel-b")
    repository, engine, clock, transfer = await opened(tmp_path, monkeypatch, creator, alternate)
    root = await root_of(repository, transfer)

    assert await attempts(root.id) == [("parcel-a", "started")]   # held open, never failed or exhausted
    assert root.state == "pending" and root.error.mutation == MutationOutcome.UNCERTAIN
    assert alternate.creations == 0 and creator.inventories == 0
    await engine.resolve_pending()                                  # before the next reading: nothing at all
    assert creator.creations == 1 and alternate.creations == 0

    creator.snapshot = ResourceSnapshot((creator.held(7), alternate.held(9)), complete=True)
    reopened, _registry, restarted = await lab(tmp_path, monkeypatch, creator, alternate, fresh=False, clock=clock,
                                               policy=TransferPolicy(retry_delay=60.0, max_attempts=3))
    clock.now += POLL
    await restarted.resolve_pending()

    root = await root_of(reopened, transfer)
    assert root.state == "waiting" and root.resource.id == "parcel-a:7"
    assert root.resource.ownership == Ownership.ADOPTED
    assert await attempts(root.id) == [("parcel-a", "succeeded")]
    assert creator.creations == 1 and alternate.creations == 0       # never created again
    await restarted.delete(transfer.id, remote=True)
    assert creator.cleaned == ["parcel-a:7"]                         # the one cleanup owner


async def test_a_complete_inventory_without_it_ends_the_attempt_as_the_ordinary_failure(tmp_path, monkeypatch):
    """A-T8: absence proven -- the attempt ends as the provider failure it then
    was, so transfer 523's exhaustion and failover proceed unchanged."""
    creator, alternate = Creator("parcel-a", priority=20, fails=[lost_answer("parcel-a")]), Creator("parcel-b")
    repository, engine, clock, transfer = await opened(tmp_path, monkeypatch, creator, alternate)
    clock.now += POLL
    await engine.resolve_pending()
    await engine.resolve_pending()                                  # the competition continues

    root = await root_of(repository, transfer)
    assert (await attempts(root.id))[0] == ("parcel-a", "exhausted")
    assert "parcel-a" in await repository.exhausted_route_providers(root.id)
    assert alternate.creations == 1 and await repository.bound_route_provider(root.id) == "parcel-b"
    assert creator.creations == 1


@pytest.mark.parametrize("snapshot", ["incomplete", "two", "unreadable"])
async def test_an_inconclusive_inventory_waits_without_creating_and_is_bounded(tmp_path, monkeypatch, snapshot):
    """A-T9: neither one match nor proven absence -- the attempt stays open,
    nothing is created, and the ordinary retry budget bounds the wait."""
    creator = Creator("parcel-a", priority=20, fails=[lost_answer("parcel-a")])
    if snapshot == "incomplete":
        creator.snapshot = ResourceSnapshot((), complete=False)
    elif snapshot == "two":
        creator.snapshot = ResourceSnapshot((creator.held(1), creator.held(2)), complete=True)
    else:
        creator.snapshot = ResourceSnapshot((), complete=True, error=lost_answer("parcel-a"))
    repository, engine, clock, transfer = await opened(tmp_path, monkeypatch, creator)
    root = await root_of(repository, transfer)

    clock.now += POLL
    await engine.resolve_pending()
    assert await attempts(root.id) == [("parcel-a", "started")] and creator.creations == 1
    for _ in range(4):
        clock.now += 3_600
        await engine.resolve_pending()
    assert (await attempts(root.id))[0][1] == "exhausted"             # the budget ended the wait ...
    assert creator.creations == 1                                     # ... and nothing was created blindly
    assert "parcel-a" in await repository.exhausted_route_providers(root.id, math.inf)   # never again here
    assert await owed(root.id)                                        # but the reconciliation is still owed

    creator.snapshot = ResourceSnapshot((creator.held(7),), complete=True)    # it had been created after all
    clock.now += 3_600
    await engine.resolve_pending()
    assert creator.cleaned == ["parcel-a:7"] and not await owed(root.id)       # found, owned, cleaned up
    assert creator.creations == 1


@pytest.mark.parametrize("remote", [True, False])
async def test_deleting_the_transfer_while_its_creation_is_held_keeps_the_reconciliation(tmp_path, monkeypatch,
                                                                                         remote):
    """Delete ends the transfer, never the obligation: the creation the
    provider may have made is still reconciled; found, it is bound to the
    deleted transfer and removed exactly when Delete asked to remove remote
    resources."""
    creator = Creator("parcel-a", priority=20, fails=[lost_answer("parcel-a")])
    repository, engine, clock, transfer = await opened(tmp_path, monkeypatch, creator)
    root = await root_of(repository, transfer)
    await engine.delete(transfer.id, remote=remote)
    assert await owed(root.id)

    creator.snapshot = ResourceSnapshot((creator.held(7),), complete=True)
    clock.now += POLL
    await engine.resolve_pending()
    assert not await owed(root.id) and creator.creations == 1
    assert "parcel-a:7" in {resource.id for resource, _state, _pending in await repository.resources(transfer.id)}
    assert creator.cleaned == (["parcel-a:7"] if remote else [])


async def test_a_definitive_refusal_is_never_held(tmp_path, monkeypatch):
    """A-T5: a refusal that proves nothing was created exhausts the provider
    and fails over at once; its inventory is never asked."""
    refused = lost_answer("parcel-a", mutation=MutationOutcome.NOT_COMMITTED)
    creator, alternate = Creator("parcel-a", priority=20, fails=[refused]), Creator("parcel-b")
    repository, engine, _clock, transfer = await opened(tmp_path, monkeypatch, creator, alternate)
    await engine.resolve_pending()                                  # the competition continues
    root = await root_of(repository, transfer)
    assert (await attempts(root.id))[0] == ("parcel-a", "exhausted")
    assert alternate.creations == 1 and creator.inventories == 0
    assert await route_providers(transfer.id) == ["parcel-a", "parcel-b"]


@pytest.mark.parametrize("created", [True, False], ids=["created-then-process-ended", "process-ended-before-sending"])
async def test_a_process_that_ends_inside_the_create_call_leaves_the_reconciliation_armed(tmp_path, monkeypatch,
                                                                                          created):
    """The obligation is armed BEFORE the productive call, so a process that
    ends inside it -- no answer, no error ever handled -- leaves it: after the
    restart the one creation reconciliation runs before anything is created.
    The provider had created the torrent: it is adopted, never created again.
    The process ended before sending: a complete inventory without it settles
    the attempt, and nothing is owned that was never created."""
    import asyncio

    creator = Creator("parcel-a", priority=20)

    async def dies(request):
        creator.creations += 1
        if created:
            creator.snapshot = ResourceSnapshot((creator.held(7),), complete=True)
        raise asyncio.CancelledError()                      # nothing after this point ever runs

    creator.resolve = dies
    clock = Clock()
    repository, _registry, engine = await lab(tmp_path, monkeypatch, creator, clock=clock,
                                              policy=TransferPolicy(retry_delay=60.0, max_attempts=3))
    transfer = await engine.submit((magnet(),), name="Show", deduplicate=False)
    try:
        await engine.resolve_pending()
    except asyncio.CancelledError:
        pass
    root = await root_of(repository, transfer)
    assert root.state == "resolving" and await attempts(root.id) == [("parcel-a", "started")]
    assert await owed(root.id)

    del creator.resolve                                     # the restarted process creates ordinarily
    reopened, _registry, restarted = await lab(tmp_path, monkeypatch, creator, fresh=False, clock=clock,
                                               policy=TransferPolicy(retry_delay=60.0, max_attempts=3))
    for _ in range(2):                                      # held into reconciliation, then settled
        await restarted.resolve_pending()
    root = await root_of(reopened, transfer)
    first = (await attempts(root.id))[0]
    adopted = [resource.id for resource, _state, _pending in await reopened.resources(transfer.id)
               if resource.ownership == Ownership.ADOPTED]
    if created:
        assert root.state == "waiting" and root.resource.id == "parcel-a:7" and adopted == ["parcel-a:7"]
        assert first == ("parcel-a", "succeeded") and creator.creations == 1      # never created again
    else:
        assert first[1] != "started" and adopted == []      # settled by proven absence; nothing falsely owned
    assert not await owed(root.id)


@pytest.mark.parametrize("created", [True, False], ids=["may-have-created", "before-sending"])
async def test_an_unexpected_exception_inside_the_armed_call_is_reconciled_never_recreated(tmp_path, monkeypatch,
                                                                                         created):
    """An ordinary exception with no normalized outcome escapes the armed
    create call: nothing proved that nothing was created, so the obligation is
    reconciled -- the provider is never asked to create again on its ordinary
    re-entry, even while its inventory stays inconclusive, and a later exact
    match is adopted. A complete inventory without it proves the absence and
    the transfer proceeds normally."""
    creator = Creator("parcel-a", priority=20)
    first = creator.resolve

    async def raises(request):
        creator.creations += 1
        creator.resolve = first                             # one unexpected failure only
        creator.snapshot = ResourceSnapshot((), complete=not created)   # created: not listed yet
        raise RuntimeError("unexpected adapter fault")

    creator.resolve = raises
    repository, engine, clock, transfer = await opened(tmp_path, monkeypatch, creator)
    root = await root_of(repository, transfer)
    assert await attempts(root.id) == [("parcel-a", "started")] and await owed(root.id)

    if not created:
        clock.now += POLL
        await engine.resolve_pending()                      # complete inventory, no match: absence proven
        assert (await attempts(root.id))[0][1] != "started" and not await owed(root.id)
        for _ in range(3):
            clock.now += 61                                  # past the ordinary re-entry: proceeds normally
            await engine.resolve_pending()
        root = await root_of(repository, transfer)
        assert root.state == "waiting" and root.resource.ownership == Ownership.CREATED
        return

    for _ in range(2):
        clock.now += 61                                      # past the provider's ordinary re-entry time ...
        await engine.resolve_pending()
    assert creator.creations == 1 and await owed(root.id)  # ... it is never asked to create again
    creator.snapshot = ResourceSnapshot((creator.held(7),), complete=True)
    clock.now += 3_600
    await engine.resolve_pending()
    root = await root_of(repository, transfer)
    assert root.state == "waiting" and root.resource.id == "parcel-a:7"
    assert root.resource.ownership == Ownership.ADOPTED and creator.creations == 1 and not await owed(root.id)


# -- the account a creation was asked of stays the connection until it settles ------------------------------------

def _connection():
    """A neutral provider configuration whose ``api_key`` IS its connection
    (an ownership field), saved in memory behind the canonical mutation."""
    import asyncio

    from pydantic import BaseModel

    from core.config import AppSettings
    from integrations.configuration_mutation import SettingsStore
    from integrations.definition import IntegrationDefinition, IntegrationSettings

    class Connection(BaseModel):
        api_key: str = ""
        rate: int = 1                                                    # an ordinary tunable

    definition = IntegrationDefinition("parcel-a", "provider", "Parcel A", Connection, lambda options, env: None,
                                       secret_fields=frozenset({"api_key"}), ownership_fields=frozenset({"api_key"}))
    saved = {"cfg": AppSettings(integrations={"parcel-a": IntegrationSettings(options={"api_key": "account-a"})})}
    lock = asyncio.Lock()
    store = SettingsStore(lambda: saved["cfg"].model_copy(deep=True), lambda: saved["cfg"].model_copy(deep=True),
                          lambda cfg: saved.__setitem__("cfg", cfg), lambda cfg: None, lambda: lock)
    return definition, store, lambda: saved["cfg"].integrations["parcel-a"].options["api_key"]


def _application(engine, definition):
    from unittest.mock import AsyncMock

    from application.service import ApplicationService
    application = ApplicationService(engine)
    application.definitions = (definition,)
    application.configure = lambda: None
    application.apply_integration_configuration = AsyncMock(return_value=None)
    return application


async def test_a_connection_owing_a_creation_reconciliation_is_not_replaced_until_it_settles(tmp_path, monkeypatch):
    """A creation the provider may have made under account A can only be
    settled from A's inventory: replacing the connection is refused while it
    is owed, A stays the connection and settles it, and only then may the
    connection change."""
    from integrations.configuration_mutation import ConfigurationRefused, mutate_integration_configuration

    creator = Creator("parcel-a", priority=20, fails=[lost_answer("parcel-a")])
    repository, engine, clock, transfer = await opened(tmp_path, monkeypatch, creator)
    root = await root_of(repository, transfer)
    definition, store, connection = _connection()
    application = _application(engine, definition)
    assert await owed(root.id) and await repository.has_integration_references("parcel-a")

    with pytest.raises(ConfigurationRefused):
        await mutate_integration_configuration(application, definition, options={"api_key": "account-b"}, store=store)
    assert connection() == "account-a"                                   # A remains the connection

    clock.now += POLL
    await engine.resolve_pending()                                       # A's complete inventory: never created
    assert not await owed(root.id) and not await repository.has_integration_references("parcel-a")
    await mutate_integration_configuration(application, definition, options={"api_key": "account-b"}, store=store)
    assert connection() == "account-b"


async def test_replacing_the_connection_waits_out_an_admitted_create_and_then_sees_its_obligation(tmp_path,
                                                                                                monkeypatch):
    """The connection replacement takes the exclusive configuration
    admission: a create already admitted against account A finishes (and arms
    its obligation) first, and the replacement then reads that reference and
    is refused -- nothing is created under A and reconciled under B."""
    import asyncio

    from integrations.configuration_mutation import ConfigurationRefused, mutate_integration_configuration

    creator = Creator("parcel-a", priority=20)
    entered, release = asyncio.Event(), asyncio.Event()

    async def creating(request):
        creator.creations += 1
        entered.set()
        await release.wait()
        raise TransferError(lost_answer("parcel-a"))

    creator.resolve = creating
    clock = Clock()
    repository, _registry, engine = await lab(tmp_path, monkeypatch, creator, clock=clock,
                                              policy=TransferPolicy(retry_delay=60.0, max_attempts=3))
    transfer = await engine.submit((magnet(),), name="Show", deduplicate=False)
    definition, store, connection = _connection()
    application = _application(engine, definition)

    create = asyncio.create_task(application.resolve_pending())
    await entered.wait()                                                 # armed against A, inside the call
    change = asyncio.create_task(mutate_integration_configuration(
        application, definition, options={"api_key": "account-b"}, store=store))
    for _ in range(10):
        await asyncio.sleep(0)
    assert not change.done()                                             # it waits for the admitted create
    release.set()
    await create
    with pytest.raises(ConfigurationRefused):
        await change
    root = await root_of(repository, transfer)
    assert connection() == "account-a" and await owed(root.id) and creator.creations == 1


async def test_the_http_connection_surface_owns_its_admission_and_never_deadlocks(tmp_path, monkeypatch):
    """Through the production mutation-admission middleware and the real
    route: replacing the connection takes the exclusive admission itself --
    no outer request admission to wait on -- so it drains the create already
    admitted against account A, sees the obligation it left and is refused;
    an ordinary tunable completes with the ordinary admission."""
    import asyncio

    import httpx
    from fastapi import FastAPI

    import main
    from api import routes

    creator = Creator("parcel-a", priority=20)
    entered, release = asyncio.Event(), asyncio.Event()

    async def creating(request):
        creator.creations += 1
        entered.set()
        await release.wait()
        raise TransferError(lost_answer("parcel-a"))

    creator.resolve = creating
    clock = Clock()
    repository, _registry, engine = await lab(tmp_path, monkeypatch, creator, clock=clock,
                                              policy=TransferPolicy(retry_delay=60.0, max_attempts=3))
    transfer = await engine.submit((magnet(),), name="Show", deduplicate=False)
    definition, store, connection = _connection()
    application = _application(engine, definition)
    for name, value in (("get_settings", store.current), ("load_settings", store.load),
                        ("save_settings", store.save), ("apply_settings", store.apply)):
        monkeypatch.setattr(routes, name, value)
    exclusive = []
    admission = application.configuration_admission
    monkeypatch.setattr(application, "configuration_admission",
                        lambda: (exclusive.append(True), admission())[1])
    app = FastAPI()
    app.state.application = application
    app.middleware("http")(main.application_mutation_admission_middleware)
    app.include_router(routes.router, prefix="/api")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        tuned = await asyncio.wait_for(client.patch("/api/integrations/parcel-a/configuration",
                                                    json={"options": {"rate": 5}}), 5)
        assert tuned.status_code == 200 and exclusive == []                   # ordinary admission

        create = asyncio.create_task(application.resolve_pending())
        await entered.wait()                                                  # armed against A
        change = asyncio.create_task(client.patch("/api/integrations/parcel-a/configuration",
                                                  json={"options": {"api_key": "account-b"}}))
        for _ in range(10):
            await asyncio.sleep(0)
        assert not change.done()                                              # draining the admitted create
        release.set()
        await create
        refused = await asyncio.wait_for(change, 5)                           # bounded: a deadlock fails here
    assert refused.status_code == 409 and "Parcel A" in refused.json()["detail"]
    assert exclusive == [True] and connection() == "account-a"
    root = await root_of(repository, transfer)
    assert await owed(root.id) and creator.creations == 1
