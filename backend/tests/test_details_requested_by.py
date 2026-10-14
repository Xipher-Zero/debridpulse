"""Transfer Details "Requested By" (1.0.13, after ACL Stage 3).

Who asked for a transfer's work is read from the demand ledger alone: one
entry per request record the transfer serves, including demand consolidated
into its artifacts, oldest first with withdrawn requests in place, each with
its own time, attributed to the account that made it (whatever became of that
account since), an Integration request to its account and token label, and an
ownerless legacy request to nobody.
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
    assert [entry["name"] for entry in (await _requested_by(repository, shared))["requests"]] == ["Alice"]
    await _submit(repository, alice.id)                                  # a repeat: its own entry
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
    entries = projected["requests"]
    assert set(projected) == {"requests"}
    # Every request record, once, in the ledger's order -- withdrawn ones in place.
    assert [(entry["name"], entry["reference"]) for entry in entries] == [
        ("Alice", alice.id[-8:]), ("Alice", alice.id[-8:]), ("Bob", None), ("Alice", namesake.id[-8:]),
        ("Dave", None), ("Erin", None), ("Sonarr", None), ("Carol", None)]
    times = [entry["requested_at"] for entry in entries]
    assert all(time.endswith("+00:00") for time in times) and times == sorted(times)
    by_name = {entry["name"]: entry for entry in entries}
    assert by_name["Erin"]["standing"] == "merged"                         # never re-attributed to Alice
    assert (by_name["Sonarr"]["kind"], by_name["Sonarr"]["token_label"]) == ("integration", "sonarr-main")
    assert by_name["Carol"]["via_transfer_id"] == contributor
    assert all(entry["via_transfer_id"] is None for entry in entries if entry["name"] != "Carol")
    assert by_name["Bob"]["standing"] == "active" and by_name["Bob"]["withdrawn_at"].endswith("+00:00")
    assert by_name["Dave"]["standing"] == "deleted" and by_name["Dave"]["withdrawn_at"]   # historical identity kept
    assert [entry["name"] for entry in entries if entry["withdrawn_at"] is None] == [
        "Alice", "Alice", "Alice", "Erin", "Sonarr", "Carol"]
    assert all("requests" not in entry for entry in entries)              # nothing aggregated

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

    [entry] = (await _requested_by(repository, legacy))["requests"]
    assert {key: entry[key] for key in ("name", "kind", "standing", "token_label", "reference", "withdrawn_at")} == {
        "name": "Legacy / Unknown", "kind": "legacy", "standing": None, "token_label": None, "reference": None,
        "withdrawn_at": None}
    assert [(entry["name"], entry["kind"]) for entry in (await _requested_by(repository, unattributed))["requests"]] \
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
    assert [entry["name"] for entry in body["requested_by"]["requests"]] == ["Someone"]
    assert body["access"] == {"destroy": False, "own_requests": []}
    assert (await client.get(f"/api/torrents/{transfer_id}", headers=as_(Role.INTEGRATION))).status_code == 403


# ── one entry per request record (ACL UI follow-up, Contract E) ─────────────

async def _stamp(demand_id: str, created_at: float) -> None:
    async with database.get_db() as db:
        await db.execute("UPDATE transfer_demands SET created_at=? WHERE id=?", (created_at, demand_id))
        await db.commit()


async def test_repeated_requests_by_one_account_and_token_stay_separate_with_their_own_times(core):
    repository, _engine, _application = core
    bot = await accounts.create_integration_account("Radarr")
    main_token, _raw = await accounts.create_token(bot.id, "radarr-main")
    spare_token, _raw2 = await accounts.create_token(bot.id, "radarr-spare")
    demands = []
    for token in (main_token, main_token, spare_token):
        demand = SubmissionDemand(bot.id, token["id"])
        transfer, _created = await repository.admit(
            (TransferRequest("magnet", MAGNET, name="shared.bin", fingerprint=MAGNET[-52:-12]),), name="shared.bin",
            demand=demand)
        demands.append(demand.id)
    for demand_id, at in zip(demands, (1_800_000_000, 1_800_000_060, 1_800_000_120)):
        await _stamp(demand_id, at)

    entries = (await _requested_by(repository, transfer.id))["requests"]
    assert [(entry["name"], entry["token_label"], entry["requested_at"]) for entry in entries] == [
        ("Radarr", "radarr-main", "2027-01-15T08:00:00+00:00"),
        ("Radarr", "radarr-main", "2027-01-15T08:01:00+00:00"),       # same account and token: its own entry and time
        ("Radarr", "radarr-spare", "2027-01-15T08:02:00+00:00"),      # another token: another entry
    ]


async def test_requests_are_chronological_across_withdrawal_and_consolidation(core):
    repository, engine, _application = core
    alice = await accounts.create_local_account("alice-c", "alice-secret", role="user", display_name="Alice")
    shared, first = await _submit(repository, alice.id)
    _again, second = await _submit(repository, alice.id)
    contributor, through = await _submit(repository, alice.id, payload=_magnet("f"))
    await _consolidate(contributor, shared)
    # The ledger's times decide the order, whatever was withdrawn since.
    for demand_id, at in ((first, 1_800_000_000), (through, 1_800_000_030), (second, 1_800_000_090)):
        await _stamp(demand_id, at)
    await engine.withdraw_demand((first,), by=f"account:{alice.id}")

    entries = (await _requested_by(repository, shared))["requests"]
    assert [(entry["requested_at"], entry["via_transfer_id"], entry["withdrawn_at"] is not None) for entry in entries] == [
        ("2027-01-15T08:00:00+00:00", None, True),                    # earliest, withdrawn, still first
        ("2027-01-15T08:00:30+00:00", contributor, False),            # same account via consolidation: its own entry
        ("2027-01-15T08:01:30+00:00", None, False),
    ]
    assert {entry["name"] for entry in entries} == {"Alice"}


async def test_every_reader_sees_every_request_and_withdrawal_stays_the_owners(served, monkeypatch):
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
    other = await accounts.create_local_account("other", "other-secret", role="user", display_name="Other")
    transfer_id, _other_first = await _submit(repository, other.id)
    await _submit(repository, other.id)
    _same, own = await _submit(repository, "user-id")                  # the signed-in User's own request
    owner_before = (await repository.get(transfer_id))

    seen = {}
    for role in (Role.ADMIN, Role.USER, Role.READONLY):
        response = await client.get(f"/api/torrents/{transfer_id}", headers=as_(role))
        assert response.status_code == 200
        body = response.json()
        seen[role] = body
        assert len(body["requested_by"]["requests"]) == 3
    assert seen[Role.ADMIN]["requested_by"] == seen[Role.USER]["requested_by"] == seen[Role.READONLY]["requested_by"]
    # Withdrawal authorization is unchanged: only the User's own request is theirs to withdraw.
    assert seen[Role.USER]["access"]["own_requests"] == [own]
    assert seen[Role.READONLY]["access"] == {"destroy": False, "own_requests": []}
    assert seen[Role.ADMIN]["access"]["own_requests"] == []
    assert (await client.get(f"/api/torrents/{transfer_id}", headers=as_(Role.INTEGRATION))).status_code == 403
    # Reading Details changed no ownership, demand or state.
    after = await repository.get(transfer_id)
    assert (after.state, after.id) == (owner_before.state, owner_before.id)
    assert len(await repository.outstanding_demand(transfer_id)) == 3
