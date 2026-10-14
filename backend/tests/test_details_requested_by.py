"""Transfer Details "Requested By" (1.0.13, after ACL Stage 3).

Who asked for a transfer's work is read from the demand ledger alone: every
account with demand the transfer serves, including demand consolidated into
its artifacts, outstanding apart from withdrawn, attributed to the account
that made it (whatever became of that account since), an Integration request
to its account and token label, and an ownerless legacy request to nobody.
"""
from __future__ import annotations

import json

import pytest

import db.database as database
from api.routes import _requested_by
from auth import accounts
from auth.accounts import DEFAULT_ADMIN_ID, Role
from transfers.models import SubmissionDemand, TransferRequest
from test_access_control import MAGNET, _submit, core, served  # noqa: F401  (fixtures)

pytestmark = pytest.mark.asyncio


def _magnet(char: str) -> str:
    return MAGNET.replace("b" * 40, char * 40)


async def _consolidate(contributor: int, canonical: int) -> None:
    async with database.get_db() as db:
        primary = await db.execute_returning_id(
            "INSERT INTO download_files(torrent_id,filename,status) VALUES(?,'x','pending')", (canonical,))
        standby = await db.execute_returning_id(
            "INSERT INTO download_files(torrent_id,filename,status) VALUES(?,'x','duplicate')", (contributor,))
        request = (await db.fetchone("SELECT id FROM transfer_requests WHERE transfer_id=?", (contributor,)))["id"]
        await db.execute("INSERT INTO artifact_consolidations(contributing_artifact_id,source_transfer_id,"
                         "source_request_id,canonical_artifact_id) VALUES(?,?,?,?)",
                         (standby, contributor, request, primary))
        await db.commit()


async def test_requested_by_names_every_requester_from_the_ledger(core):
    repository, engine, _application = core
    alice = await accounts.create_local_account("alice-login", "alice-secret", role="user", display_name="Alice")
    bob = await accounts.create_local_account("bob-login", "bob-secret", role="user", display_name="Bob")
    namesake = await accounts.create_local_account("alice-two", "two-secret", role="readonly", display_name="Alice")
    dave = await accounts.create_local_account("dave-login", "dave-secret", role="user", display_name="Dave")
    carol = await accounts.create_local_account("carol-login", "carol-secret", role="user", display_name="Carol")
    erin = await accounts.resolve_oidc("https://idp.example", "erin-subject",
                                       {"name": "Erin", "email": "erin@example.com", "email_verified": True},
                                       default_role="user")
    bot = await accounts.create_integration_account("Sonarr")
    token, raw = await accounts.create_token(bot.id, "sonarr-main")

    # One human requester, then repeats and others sharing the same transfer.
    shared, _alice_demand = await _submit(repository, alice.id)
    assert [entry["name"] for entry in (await _requested_by(repository, shared))["outstanding"]] == ["Alice"]
    await _submit(repository, alice.id)                                  # a repeat: counted, not collapsed away
    _same, bob_demand = await _submit(repository, bob.id)
    await _submit(repository, namesake.id)
    await _submit(repository, dave.id)
    await _submit(repository, erin.id)
    transfer, _created = await repository.admit(
        (TransferRequest("magnet", MAGNET, name="shared.bin", fingerprint=MAGNET[-52:-12]),), name="shared.bin",
        demand=SubmissionDemand(bot.id, token["id"]))
    assert transfer.id == shared
    # Carol's own transfer was consolidated into this one's artifact.
    contributor, _carol_demand = await _submit(repository, carol.id, payload=_magnet("c"))
    await _consolidate(contributor, shared)

    # Bob withdraws; Dave's account is deleted (withdrawing his demand); Erin
    # is merged into Alice (her demand stays hers, and outstanding).
    await engine.withdraw_demand((bob_demand,), by=f"account:{bob.id}")
    await accounts.delete_account(DEFAULT_ADMIN_ID, dave.id)
    await accounts.merge_accounts(DEFAULT_ADMIN_ID, erin.id, alice.id)

    projected = await _requested_by(repository, shared)
    outstanding = {(entry["name"], entry["reference"]): entry for entry in projected["outstanding"]}
    assert outstanding[("Alice", alice.id[-8:])]["requests"] == 2
    assert outstanding[("Alice", namesake.id[-8:])]["requests"] == 1       # same name, told apart
    assert outstanding[("Erin", None)]["standing"] == "merged"             # never re-attributed to Alice
    assert outstanding[("Sonarr", None)]["kind"] == "integration"
    assert outstanding[("Sonarr", None)]["token_label"] == "sonarr-main"
    assert outstanding[("Carol", None)]["via_transfer_id"] == contributor
    assert all(entry["via_transfer_id"] is None for name, entry in outstanding.items() if name[0] != "Carol")
    assert "Bob" not in {name for name, _ref in outstanding} and "Dave" not in {name for name, _ref in outstanding}

    history = {entry["name"]: entry for entry in projected["history"]}
    assert set(history) == {"Bob", "Dave"}
    assert history["Bob"]["standing"] == "active" and history["Bob"]["withdrawn_at"].endswith("+00:00")
    assert history["Dave"]["standing"] == "deleted"                         # historical identity kept
    assert all("withdrawn_at" not in entry for entry in projected["outstanding"])

    # Nothing secret or identifying beyond the display name reaches Details.
    text = json.dumps(projected)
    for secret in (raw, token["id"], "alice-login", "alice-secret", "erin@example.com", "erin-subject",
                   "https://idp.example", "verifier", "password", alice.id, bot.id):
        assert secret not in text


async def test_legacy_and_unattributed_requests_are_credited_to_nobody(core):
    repository, _engine, _application = core
    legacy = (await repository.admit(
        (TransferRequest("magnet", _magnet("d"), name="old.bin", fingerprint="d" * 40),), name="old.bin"))[0].id
    async with database.get_db() as db:
        # A transfer from before submitter demand existed, given its explicit
        # ownerless demand by the real startup backfill.
        await db.execute("DELETE FROM transfer_demands WHERE transfer_id=?", (legacy,))
        await database._backfill_legacy_demand(db)
        await db.commit()
    unattributed = (await repository.admit(
        (TransferRequest("magnet", _magnet("e"), name="new.bin", fingerprint="e" * 40),), name="new.bin"))[0].id

    [entry] = (await _requested_by(repository, legacy))["outstanding"]
    assert {key: entry[key] for key in ("name", "kind", "standing", "token_label", "reference")} == {
        "name": "Legacy / Unknown", "kind": "legacy", "standing": None, "token_label": None, "reference": None}
    assert [(entry["name"], entry["kind"]) for entry in (await _requested_by(repository, unattributed))["outstanding"]] \
        == [("Unknown", "unknown")]


async def test_details_show_requested_by_to_readonly_without_mutation_and_never_to_integration(served, monkeypatch):
    import main
    from application.service import ApplicationService
    from transfers.engine import TransferEngine
    from transfers.policy import TransferPolicy
    from transfers.recovery_repository import TransferRepository
    from transfers.registry import IntegrationRegistry

    client, as_ = served
    repository = TransferRepository()
    engine = TransferEngine(repository, IntegrationRegistry(), download_root="/nonexistent-payloads",
                            policy=TransferPolicy(retry_delay=0), clock=lambda: 1000.0)
    monkeypatch.setattr(main.app.state, "application", ApplicationService(engine), raising=False)
    someone = await accounts.create_local_account("someone", "someone-secret", role="user", display_name="Someone")
    transfer_id, _demand = await _submit(repository, someone.id)

    readonly = await client.get(f"/api/torrents/{transfer_id}", headers=as_(Role.READONLY))
    assert readonly.status_code == 200
    body = readonly.json()
    assert [entry["name"] for entry in body["requested_by"]["outstanding"]] == ["Someone"]
    assert body["access"] == {"destroy": False, "own_requests": []}
    assert (await client.get(f"/api/torrents/{transfer_id}", headers=as_(Role.INTEGRATION))).status_code == 403
