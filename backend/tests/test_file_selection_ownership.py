"""1.0.13 PA-AUTHZ-6: choosing files is a destructive act.

Confirming a file selection can leave out files another account's request
still needs -- exactly what blocking one file does -- so it takes the same one
ownership rule (``TRANSFERS_DESTROY`` plus ownership of every outstanding
demand, re-checked under the commit's own write lock). Dismissing keeps every
file and deprives no one, so it stays an ordinary operation.

Real routes, the real policy dependency, real accounts and a real pending
selection whose demand is User B's.
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI

from account_support import account_database
from api.file_selection_routes import router as file_selection_router
from application.observability import Observability
from application.service import ApplicationService
from auth import accounts
from auth.accounts import Role
from auth.models import Principal
from auth.permissions import authorize_request, destruction_precondition
from fake_integrations import MemoryExecutor, ParcelProvider
from file_selection_support import Clock
from transfers._repository_base import DemandChanged
from transfers.engine import TransferEngine
from transfers.models import ResourceState, SubmissionDemand, TransferRequest
from transfers.policy import TransferPolicy
from transfers.presentation_repository import TransferRepository
from transfers.registry import IntegrationRegistry

FILES = [("a.mkv", "a.mkv", 100), ("b.mkv", "b.mkv", 200), ("c.mkv", "c.mkv", 300)]
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def lab(tmp_path, monkeypatch):
    import main

    await account_database(tmp_path, monkeypatch)
    user_a = await accounts.create_local_account("usera", "pw-a", role=Role.USER)
    user_b = await accounts.create_local_account("userb", "pw-b", role=Role.USER)
    reader = await accounts.create_local_account("reader", "pw-r", role=Role.READONLY)
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider(file_manifest=True)
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(adoption_stability_seconds=0, resource_poll_interval=1, retry_delay=0,
                              resolution_retry_delay=0),
        clock=Clock(1000.0))
    await engine.initialize()
    application = ApplicationService(engine)
    application.observability = Observability(repository)
    principals = {"A": Principal.password_session(user_a), "B": Principal.password_session(user_b),
                  "ReadOnly": Principal.password_session(reader), "Admin": Principal.standalone()}

    app = FastAPI(dependencies=[Depends(authorize_request)])

    @app.middleware("http")
    async def principal(request, call_next):
        request.state.principal = principals[request.headers["x-as"]]
        return await call_next(request)

    app.add_exception_handler(DemandChanged, main.demand_changed_handler)
    app.state.application = application
    app.include_router(file_selection_router, prefix="/api")
    counter = [0]
    requests = {}

    async def pending(owner=None):
        """A fresh transfer with a pending 3-file selection, demanded by ``owner``."""
        counter[0] += 1
        provider.responses.append(provider.parcel(f"show{counter[0]}", state=ResourceState.AVAILABLE, files=FILES))
        request = TransferRequest("parcel", f"box{counter[0]}", name="show", selection_mode="interactive",
                                  fingerprint=f"{counter[0]:040x}")
        transfer = await engine.submit((request,), demand=SubmissionDemand((owner or user_b).id))
        await engine.resolve_pending()
        requests[transfer.id] = request
        return transfer.id

    async def repeat(transfer_id, owner):
        """Another account submits the same request: its demand lands on the same transfer."""
        demand = SubmissionDemand(owner.id)
        same, _created = await repository.admit((requests[transfer_id],), name="show", demand=demand)
        assert same.id == transfer_id
        return demand.id

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        async def view(transfer_id):
            return (await client.get(f"/api/torrents/{transfer_id}/file-selection", headers={"x-as": "B"})).json()

        async def confirm(who, transfer_id, keep=1):
            current = await view(transfer_id)
            return await client.post(
                f"/api/torrents/{transfer_id}/file-selection/confirm", headers={"x-as": who},
                json={"manifest_id": current["manifest_id"],
                      "entry_ids": [entry["entry_id"] for entry in current["entries"]][:keep]})

        async def dismiss(who, transfer_id):
            current = await view(transfer_id)
            return await client.post(f"/api/torrents/{transfer_id}/file-selection/dismiss", headers={"x-as": who},
                                     json={"manifest_id": current["manifest_id"]})

        yield type("Lab", (), dict(
            client=client, application=application, repository=repository, engine=engine, pending=pending,
            repeat=repeat,
            view=view, confirm=confirm, dismiss=dismiss, principals=principals, user_a=user_a, user_b=user_b))


async def test_another_user_cannot_narrow_a_selection_someone_else_still_needs(lab):
    transfer = await lab.pending()
    refused = await lab.confirm("A", transfer)
    assert refused.status_code == 403
    assert "Another account still needs this transfer" in refused.json()["detail"]
    after = await lab.view(transfer)
    assert after["decision"] == "pending" and not after.get("selected_entry_ids")


async def test_the_owner_and_an_admin_choose_files(lab):
    for who in ("B", "Admin"):
        transfer = await lab.pending()
        chosen = await lab.confirm(who, transfer)
        assert chosen.status_code == 200 and chosen.json()["decision"] == "explicit"
        assert len((await lab.view(transfer))["selected_entry_ids"]) == 1


async def test_a_user_chooses_files_on_their_own_request(lab):
    transfer = await lab.pending(owner=lab.user_a)
    assert (await lab.confirm("A", transfer, keep=2)).status_code == 200
    assert len((await lab.view(transfer))["selected_entry_ids"]) == 2


async def test_readonly_neither_chooses_nor_dismisses(lab):
    transfer = await lab.pending()
    assert (await lab.confirm("ReadOnly", transfer)).status_code == 403
    assert (await lab.dismiss("ReadOnly", transfer)).status_code == 403
    assert (await lab.view(transfer))["decision"] == "pending"


async def test_dismissing_keeps_every_file_so_any_user_may(lab):
    transfer = await lab.pending()
    dismissed = await lab.dismiss("A", transfer)
    assert dismissed.status_code == 200
    after = await lab.view(transfer)
    assert after["decision"] == "all"  # nothing B asked for was left out


async def test_demand_that_arrives_after_authorization_refuses_the_choice_at_commit(lab):
    """B is authorized against B's own demand; before the choice commits,
    A's request for the same transfer arrives. The commit's own write lock
    sees it and refuses, changing nothing."""
    transfer = await lab.pending()
    permitted, expected = await destruction_precondition(lab.principals["B"], lab.repository, transfer)
    assert permitted and expected
    await lab.repeat(transfer, lab.user_a)
    current = await lab.view(transfer)
    with pytest.raises(DemandChanged):
        await lab.application.confirm_file_selection(
            transfer, current["manifest_id"], [current["entries"][0]["entry_id"]], expected_demand=expected)
    assert (await lab.view(transfer))["decision"] == "pending"
    # Through the route, the same arrival is refused for B before the commit.
    assert (await lab.confirm("B", transfer)).status_code == 403


async def test_a_withdrawn_competing_demand_lets_the_remaining_owner_choose(lab):
    transfer = await lab.pending()
    competing = await lab.repeat(transfer, lab.user_a)
    assert (await lab.confirm("B", transfer)).status_code == 403
    await lab.repository.withdraw_demand((competing,), by=f"account:{lab.user_a.id}")
    assert (await lab.confirm("B", transfer)).status_code == 200
