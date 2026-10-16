"""An Admin can delete (or narrow) a delivery profile and retire the external
jobs that depend on it (1.0.13).

``PUT /api/external/profiles`` refused (``409 profile_in_use``) any write that
took a profile away from an unsettled job, and nothing could settle such a
job but its own client (``withdraw`` requires that client's principal): a
stuck job, or one whose token was revoked, made its profile permanently
undeletable. Now the Admin may name the profile in ``retire_dependents``:
in the very commit that changes the catalog, exactly those jobs are retired
-- each one's demand withdrawn by ``administrator:profile_retired`` (the
durable, per-job record) -- and then settled through the canonical owner
(``ApplicationService.withdraw_demand``, a download client's "remove"): work
nobody else needs is cancelled and its DebridPulse-owned resources given
back; shared work, other profiles' and native claims, and completed files
stay. A reservation in flight is retired when accepted; maintenance finishes
a settlement that never ran.

Real application, middleware, external service, repository and engine.
"""
from __future__ import annotations

import asyncio

import pytest

import db.database as database
from auth import accounts
import external.service as external_service
from external.service import ExternalClients
from test_external_client_foundation import body, complete, demands, http, transfer_of, world  # noqa: F401
from test_external_profile_guard import catalog, edited, put, without
from transfers.models import TransferState

pytestmark = pytest.mark.asyncio

# Who withdrew a retired job's demand: the durable per-job record.
PROFILE_RETIRED = "administrator:profile_retired"


@pytest.fixture(autouse=True)
def _fresh_settlement_walk(monkeypatch):
    """Each test starts with no walk position, as a new process does."""
    monkeypatch.setattr(external_service, "_RETIREMENT_SETTLEMENT_CURSOR", None, raising=False)

OTHER = "magnet:?xt=urn:btih:" + "a" * 40


async def retire(http, document, profiles, *profile_ids):  # noqa: F811
    return await http.client.put("/api/external/profiles", headers=http.admin_headers,
                                 json={"expected_revision": document["revision"], "profiles": profiles,
                                       "retire_dependents": list(profile_ids)})


async def demand_of(job_id):
    async with database.get_db() as db:
        return await db.fetchone("SELECT d.* FROM external_jobs j JOIN transfer_demands d ON d.id=j.demand_id "
                                 "WHERE j.id=?", (job_id,))


async def dependents(http, profile_id):  # noqa: F811
    response = await http.client.get(f"/api/external/profiles/{profile_id}/dependents", headers=http.admin_headers)
    assert response.status_code == 200, response.text
    return response.json()["affected_jobs"]


async def state_of(world, transfer_id):  # noqa: F811
    return (await world.repository.get(transfer_id)).state


# ── 1, 8, 21: no dependents; idempotent repeat ───────────────────────────────────────────────────────────────

async def test_the_retirement_record_is_the_services_own():
    assert getattr(external_service, "PROFILE_RETIRED", None) == PROFILE_RETIRED


async def test_a_profile_with_no_jobs_deletes_as_before_and_a_repeat_changes_nothing(http, world):  # noqa: F811
    document = await catalog(http)
    assert await dependents(http, "tv") == 0
    deleted = await put(http, document, without(document, "tv"))
    assert deleted.status_code == 200 and "retired_jobs" not in deleted.json()
    again = await catalog(http)
    assert "tv" not in {profile["id"] for profile in again["profiles"]}
    # A repeated retiring write of a profile already gone: nothing to retire.
    repeat = await retire(http, again, again["profiles"], "tv")
    assert repeat.status_code == 200 and "retired_jobs" not in repeat.json()


# ── 2-5, 7: every lifecycle state is retired; exclusively owned work is stopped ─────────────────────────────

async def test_deleting_a_profile_retires_its_jobs_in_every_state(http, world):  # noqa: F811
    queued, _ = await world.external.submit(world.clients.a, body("k-queued"))
    running, _ = await world.external.submit(world.clients.a, body("k-running", source={"kind": "magnet",
                                                                                         "magnet": OTHER}))
    for _ in range(2):
        await world.engine.tick()
    third = "magnet:?xt=urn:btih:" + "b" * 40
    done, _ = await world.external.submit(world.clients.a, body("k-done", source={"kind": "magnet", "magnet": third}))
    done_transfer = await transfer_of(done["id"])
    await complete(world, done_transfer)
    await world.external.job(world.clients.a, done["id"])                # its delivery is committed
    document = await catalog(http)
    assert await dependents(http, "tv") == 3

    refused = await put(http, document, without(document, "tv"))
    assert refused.status_code == 409 and refused.json()["detail"]["affected_jobs"] == 3
    response = await retire(http, document, without(document, "tv"), "tv")
    assert response.status_code == 200, response.text
    result = response.json()
    assert (result["retired_jobs"], result["settled_jobs"], result["cleanup_pending"]) == (3, 3, False)
    assert "tv" not in {profile["id"] for profile in result["profiles"]}

    for job in (queued, running, done):
        demand = await demand_of(job["id"])
        assert demand["withdrawn_at"] is not None and demand["withdrawn_by"] == PROFILE_RETIRED
        async with database.get_db() as db:
            row = await db.fetchone("SELECT projection FROM external_jobs WHERE id=?", (job["id"],))
        assert '"phase": "withdrawn"' in row["projection"] or '"phase":"withdrawn"' in row["projection"]
    # Work nobody else needed is stopped and leaves the ordinary lists; the
    # completed delivery and its files stay.
    for job in (queued, running):
        assert await state_of(world, await transfer_of(job["id"])) == TransferState.DELETED
    assert await state_of(world, done_transfer) == TransferState.COMPLETED
    [artifact] = await world.repository.artifacts(done_transfer)
    from pathlib import Path
    assert Path(artifact.target).exists()
    assert await dependents(http, "tv") == 0


# ── 6: a revoked token never traps the Admin ─────────────────────────────────────────────────────────────────

async def test_a_revoked_clients_jobs_are_retired_by_the_admin_alone(http, world):  # noqa: F811
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    assert await accounts.revoke_token(world.accounts.sonarr.id, world.tokens.a["id"])
    document = await catalog(http)
    response = await retire(http, document, without(document, "tv"), "tv")
    assert response.status_code == 200 and response.json()["retired_jobs"] == 1
    assert (await demand_of(job["id"]))["withdrawn_by"] == PROFILE_RETIRED


# ── 11-14: ownership isolation ─────────────────────────────────────────────────────────────────────────────

async def test_shared_work_keeps_every_other_claim_and_runs_on(http, world):  # noqa: F811
    mine, _ = await world.external.submit(world.clients.a, body("k-mine"))                       # tv
    theirs, _ = await world.external.submit(world.clients.r, body("k-theirs", category="movies"))  # movies
    transfer_id = await transfer_of(mine["id"])
    assert await transfer_of(theirs["id"]) == transfer_id                 # one canonical transfer
    async with database.get_db() as db:                                  # an independent native claim on it
        await db.execute("INSERT INTO transfer_demands(id,transfer_id,account_id,origin,created_at) "
                         "VALUES('native-claim',?,'native-user','submission',1)", (transfer_id,))
        await db.commit()
    document = await catalog(http)
    response = await retire(http, document, without(document, "tv"), "tv")
    assert response.status_code == 200 and response.json()["retired_jobs"] == 1
    assert (await demand_of(mine["id"]))["withdrawn_at"] is not None
    assert (await demand_of(theirs["id"]))["withdrawn_at"] is None       # another profile's claim
    assert await state_of(world, transfer_id) not in {TransferState.CANCELLED, TransferState.DELETED}
    async with database.get_db() as db:
        native_claims = await db.fetchall("SELECT withdrawn_at FROM transfer_demands WHERE transfer_id=? "
                                          "AND account_id='native-user'", (transfer_id,))
    assert [row["withdrawn_at"] for row in native_claims] == [None]       # the native claim untouched


async def test_a_client_in_two_profiles_keeps_its_other_profiles_jobs(http, world):  # noqa: F811
    tv_job, _ = await world.external.submit(world.clients.a, body("k-tv"))
    movies_job, _ = await world.external.submit(world.clients.a, body("k-movies", category="movies",
                                                                      source={"kind": "magnet", "magnet": OTHER}))
    document = await catalog(http)
    assert (await retire(http, document, without(document, "tv"), "tv")).status_code == 200
    assert (await demand_of(tv_job["id"]))["withdrawn_at"] is not None
    assert (await demand_of(movies_job["id"]))["withdrawn_at"] is None
    assert await state_of(world, await transfer_of(movies_job["id"])) not in {TransferState.CANCELLED,
                                                                              TransferState.DELETED}


async def test_removing_one_client_retires_only_that_clients_jobs(http, world):  # noqa: F811
    a_job, _ = await world.external.submit(world.clients.a, body("k-a"))
    b_job, _ = await world.external.submit(world.clients.b, body("k-b", source={"kind": "magnet", "magnet": OTHER}))
    document = await catalog(http)
    narrowed = edited(document, "tv", clients=[world.clients.b.client_id])
    assert (await put(http, document, narrowed)).status_code == 409    # protected without authorization
    response = await retire(http, document, narrowed, "tv")
    assert response.status_code == 200 and response.json()["retired_jobs"] == 1
    assert (await demand_of(a_job["id"]))["withdrawn_at"] is not None
    assert (await demand_of(b_job["id"]))["withdrawn_at"] is None


async def test_a_retiring_write_never_retires_another_profiles_jobs(http, world):  # noqa: F811
    movies_job, _ = await world.external.submit(world.clients.r, body("k-r", category="movies"))
    document = await catalog(http)
    # Authorization for movies is given, but the write changes only tv.
    response = await retire(http, document, without(document, "tv"), "movies")
    assert response.status_code == 200 and "retired_jobs" not in response.json()
    assert (await demand_of(movies_job["id"]))["withdrawn_at"] is None


# ── 9, 17: recovery after a settlement that never ran ──────────────────────────────────────────────────────────

async def test_a_settlement_interrupted_after_commit_is_finished_by_maintenance(http, world, monkeypatch):  # noqa: F811
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    for _ in range(2):
        await world.engine.tick()

    real = ExternalClients.settle_retired

    async def crashed(self, job_ids):                                    # the process ended after the commit
        return 0

    monkeypatch.setattr(ExternalClients, "settle_retired", crashed)
    document = await catalog(http)
    response = await retire(http, document, without(document, "tv"), "tv")
    assert response.status_code == 200 and response.json()["cleanup_pending"] is True
    assert (await demand_of(job["id"]))["withdrawn_by"] == PROFILE_RETIRED        # the durable intent
    monkeypatch.setattr(ExternalClients, "settle_retired", real)
    # A restart: a fresh maintenance pass finishes it through the core.
    result = await ExternalClients(world.application).maintain()
    assert result["profile_retirements"] == 1
    assert await state_of(world, transfer_id) == TransferState.DELETED
    assert (await ExternalClients(world.application).maintain())["profile_retirements"] == 0   # idempotent


# ── 15: a reservation in flight is covered ────────────────────────────────────────────────────────────────────

async def test_an_admission_in_flight_during_the_retirement_is_retired_when_accepted(http, world, monkeypatch):  # noqa: F811
    original = ExternalClients._admit
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(self, *args, **kwargs):
        entered.set()
        await release.wait()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(ExternalClients, "_admit", held)
    submission = asyncio.create_task(world.external.submit(world.clients.a, body("k-1")))
    await entered.wait()
    document = await catalog(http)
    response = await retire(http, document, without(document, "tv"), "tv")
    assert response.status_code == 200 and response.json()["retired_jobs"] == 1
    release.set()
    job, _ = await submission
    demand = await demand_of(job["id"])
    assert demand is not None and demand["withdrawn_by"] == PROFILE_RETIRED
    assert await state_of(world, await transfer_of(job["id"])) in {TransferState.CANCELLED, TransferState.DELETED}


# ── 10: a recreated profile inherits nothing ──────────────────────────────────────────────────────────────────

async def test_a_recreated_profile_with_the_same_id_inherits_no_retired_job(http, world):  # noqa: F811
    old, _ = await world.external.submit(world.clients.a, body("k-old"))
    document = await catalog(http)
    tv = next(profile for profile in document["profiles"] if profile["id"] == "tv")
    assert (await retire(http, document, without(document, "tv"), "tv")).status_code == 200
    after = await catalog(http)
    assert (await put(http, after, [*after["profiles"], tv])).status_code == 200   # same id, same category
    assert await dependents(http, "tv") == 0                              # the retired job is not its
    new, _ = await world.external.submit(world.clients.a, body("k-new", source={"kind": "magnet", "magnet": OTHER}))
    assert (await demand_of(new["id"]))["withdrawn_at"] is None
    assert (await demand_of(old["id"]))["withdrawn_by"] == PROFILE_RETIRED


# ── 24: authorization at the boundary ─────────────────────────────────────────────────────────────────────────

async def test_retirement_and_its_count_are_admin_only(http, world):  # noqa: F811
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    for headers in (http.client_headers, {}):
        assert (await http.client.get("/api/external/profiles/tv/dependents", headers=headers)).status_code in {401,
                                                                                                                 403}
        assert (await http.client.put("/api/external/profiles", headers=headers, json={
            "expected_revision": 0, "profiles": [], "retire_dependents": ["tv"]})).status_code in {401, 403}
    assert (await demand_of(job["id"]))["withdrawn_at"] is None             # nothing happened
    assert len(await demands()) == 1


# ── recovery eligibility is the core's state, never the job's presentation ─────────────────────────────────────

async def _retire_with_failing_settlement(http, world, monkeypatch, jobs=("k-1",)):  # noqa: F811
    """Retire the tv profile while the core settlement after commit fails."""
    submitted = []
    for n, key in enumerate(jobs):
        magnet = "magnet:?xt=urn:btih:" + f"{n:x}" * 40 if n else body(key)["source"]["magnet"]
        job, _ = await world.external.submit(world.clients.a, body(key, source={"kind": "magnet", "magnet": magnet}))
        submitted.append(job)
    for _ in range(2):
        await world.engine.tick()
    real = world.application.withdraw_demand

    async def failing(*args, **kwargs):
        raise RuntimeError("settlement interrupted")

    monkeypatch.setattr(world.application, "withdraw_demand", failing)
    document = await catalog(http)
    response = await retire(http, document, without(document, "tv"), "tv")
    assert response.status_code == 200 and response.json()["cleanup_pending"] is True
    return submitted, real


async def test_a_presentation_refresh_never_consumes_an_unsettled_retirement(http, world, monkeypatch):  # noqa: F811
    (job,), real = await _retire_with_failing_settlement(http, world, monkeypatch)
    transfer_id = await transfer_of(job["id"])
    assert await state_of(world, transfer_id) not in {TransferState.CANCELLED, TransferState.DELETED}
    # An ordinary refresh projects the withdrawn demand: the job reads settled.
    await world.external._refresh(job["id"])
    async with database.get_db() as db:
        row = await db.fetchone("SELECT json_extract(projection,'$.settled') AS settled FROM external_jobs "
                                "WHERE id=?", (job["id"],))
    assert row["settled"] == 1
    # Maintenance still finds the core's unfinished retirement and completes it.
    monkeypatch.setattr(world.application, "withdraw_demand", real)
    result = await ExternalClients(world.application).maintain()
    assert result["profile_retirements"] == 1
    assert await state_of(world, transfer_id) == TransferState.DELETED
    # Completed: nothing is repeated.
    assert (await ExternalClients(world.application).maintain())["profile_retirements"] == 0


async def test_a_transient_failure_recovers_and_shared_or_completed_work_is_never_touched(
        http, world, monkeypatch):  # noqa: F811
    mine, _ = await world.external.submit(world.clients.a, body("k-shared"))
    theirs, _ = await world.external.submit(world.clients.r, body("k-theirs", category="movies"))
    shared = await transfer_of(mine["id"])
    third = "magnet:?xt=urn:btih:" + "b" * 40
    done, _ = await world.external.submit(world.clients.a, body("k-done", source={"kind": "magnet", "magnet": third}))
    done_transfer = await transfer_of(done["id"])
    await complete(world, done_transfer)
    lonely, _ = await world.external.submit(world.clients.a, body("k-lonely", source={"kind": "magnet",
                                                                                       "magnet": OTHER}))
    lonely_transfer = await transfer_of(lonely["id"])
    calls = []
    real = world.application.withdraw_demand

    async def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) <= 3:
            raise RuntimeError("transient")
        return await real(*args, **kwargs)

    monkeypatch.setattr(world.application, "withdraw_demand", flaky)
    document = await catalog(http)
    assert (await retire(http, document, without(document, "tv"), "tv")).status_code == 200
    for job in (mine, done, lonely):
        await world.external._refresh(job["id"])
    for _ in range(3):
        await ExternalClients(world.application).maintain()
    assert await state_of(world, lonely_transfer) == TransferState.DELETED     # recovered
    assert await state_of(world, shared) not in {TransferState.CANCELLED, TransferState.DELETED}
    assert await state_of(world, done_transfer) == TransferState.COMPLETED
    attempts = len(calls)
    for _ in range(2):
        await ExternalClients(world.application).maintain()
    assert len(calls) == attempts                                         # nothing more to do, nothing repeated


async def test_pending_retirements_are_walked_fairly_past_one_that_keeps_failing(
        http, world, monkeypatch):  # noqa: F811
    jobs, real = await _retire_with_failing_settlement(http, world, monkeypatch,
                                                       jobs=("k-0", "k-1", "k-2", "k-3", "k-4"))
    transfers = [await transfer_of(job["id"]) for job in jobs]
    stuck = transfers[0]                                                  # the oldest keeps failing
    attempted = []

    async def selective(demand_ids, **kwargs):
        transfer_ids = await world.repository.served_transfers(demand_ids)
        attempted.extend(transfer_ids)
        if stuck in transfer_ids:
            raise RuntimeError("persistently failing")
        return await real(demand_ids, **kwargs)

    monkeypatch.setattr(world.application, "withdraw_demand", selective)
    external = ExternalClients(world.application)
    passes = []
    for _ in range(3):                                                    # budget 2: ceil(5 / 2) passes
        passes.append(await external._settle_pending_retirements(budget=2))
    assert all(count <= 2 for count in passes)                            # bounded per pass
    assert set(transfers) <= set(attempted)                               # everyone attempted
    for transfer_id in transfers[1:]:
        assert await state_of(world, transfer_id) == TransferState.DELETED
    assert await state_of(world, stuck) != TransferState.DELETED          # still pending, never dropped
    # A restart: the walk begins again and the obligation is still there.
    import external.service as service
    monkeypatch.setattr(service, "_RETIREMENT_SETTLEMENT_CURSOR", None, raising=False)
    monkeypatch.setattr(world.application, "withdraw_demand", real)
    await ExternalClients(world.application)._settle_pending_retirements(budget=2)
    assert await state_of(world, stuck) == TransferState.DELETED


# ── a refresh failing after a successful core settlement ───────────────────────────────────────────────────────

async def settled(job_id):
    """The job's projection has converged: withdrawn and settled."""
    async with database.get_db() as db:
        row = await db.fetchone("SELECT json_extract(projection,'$.settled') AS s, "
                                "json_extract(projection,'$.phase') AS phase FROM external_jobs WHERE id=?",
                                (job_id,))
    return row["s"] == 1 and row["phase"] == "withdrawn"


async def test_a_refresh_failing_after_settlement_never_aborts_maintenance_and_the_job_converges(
        http, world, monkeypatch):  # noqa: F811
    """Core settlement succeeds during maintenance, then refreshing one of the
    two retired jobs fails: the pass completes (the other job is refreshed in
    it), core settlement is never repeated, and the ordinary refresh owner
    settles the stale job on a later pass -- also from a fresh process."""
    jobs, real = await _retire_with_failing_settlement(http, world, monkeypatch, jobs=("k-0", "k-1"))
    transfers = [await transfer_of(job["id"]) for job in jobs]
    calls = []

    async def counted(*args, **kwargs):
        calls.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(world.application, "withdraw_demand", counted)
    original = ExternalClients._refresh
    failing = {jobs[0]["id"]}

    async def refresh(self, job_id):
        if job_id in failing:
            raise RuntimeError("refresh failed")
        return await original(self, job_id)

    monkeypatch.setattr(ExternalClients, "_refresh", refresh)
    result = await ExternalClients(world.application).maintain()        # never raises
    assert result["profile_retirements"] == 2
    assert all([await state_of(world, transfer_id) == TransferState.DELETED for transfer_id in transfers])
    settled_calls = len(calls)

    assert await settled(jobs[1]["id"])                                  # the other job was not held back
    assert not await settled(jobs[0]["id"])                              # stale, but recoverable
    # A restart; the refresh works again: the ordinary refresh owner settles it.
    failing.clear()
    result = await ExternalClients(world.application).maintain()
    assert result["profile_retirements"] == 0                            # the core settlement is not repeated
    assert len(calls) == settled_calls
    assert await settled(jobs[0]["id"])
    # Out of the active queue presentation.
    listing = await world.external.visible(world.clients.a, settled=False)
    assert jobs[0]["id"] not in {job["id"] for job in listing["jobs"]}


async def test_a_refresh_failing_in_the_request_reports_core_settlement_and_converges_without_repeating_it(
        http, world, monkeypatch):  # noqa: F811
    """The PUT's own settlement succeeds but every refresh after it fails: the
    response counts the core settlement (``cleanup_pending`` false), shared
    and independent work keep running, and maintenance converges the stale
    projection through the ordinary refresh owner without settling again."""
    mine, _ = await world.external.submit(world.clients.a, body("k-mine"))                        # tv, shared
    theirs, _ = await world.external.submit(world.clients.r, body("k-theirs", category="movies"))   # movies
    shared = await transfer_of(mine["id"])
    lonely, _ = await world.external.submit(world.clients.a, body("k-lonely", source={"kind": "magnet",
                                                                                       "magnet": OTHER}))
    lonely_transfer = await transfer_of(lonely["id"])
    independent = "magnet:?xt=urn:btih:" + "c" * 40
    other, _ = await world.external.submit(world.clients.r, body("k-other", category="movies",
                                                                 source={"kind": "magnet", "magnet": independent}))
    other_transfer = await transfer_of(other["id"])
    for _ in range(2):
        await world.engine.tick()
    calls = []
    real = world.application.withdraw_demand

    async def counted(*args, **kwargs):
        calls.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(world.application, "withdraw_demand", counted)
    original = ExternalClients._refresh
    failing = {mine["id"], lonely["id"]}

    async def refresh(self, job_id):
        if job_id in failing:
            raise RuntimeError("refresh failed")
        return await original(self, job_id)

    monkeypatch.setattr(ExternalClients, "_refresh", refresh)
    document = await catalog(http)
    response = await retire(http, document, without(document, "tv"), "tv")
    assert response.status_code == 200, response.text
    assert response.json()["retired_jobs"] == 2 and response.json()["settled_jobs"] == 2
    assert response.json()["cleanup_pending"] is False                   # core settlement is done
    assert await state_of(world, lonely_transfer) == TransferState.DELETED
    assert not await settled(mine["id"]) and not await settled(lonely["id"])  # stale projections
    request_calls = len(calls)
    for _ in range(2):                                                   # still failing: nothing raises
        result = await ExternalClients(world.application).maintain()
        assert result["profile_retirements"] == 0
    failing.clear()
    await ExternalClients(world.application).maintain()                  # a fresh process; refresh works
    assert await settled(mine["id"]) and await settled(lonely["id"])
    assert len(calls) == request_calls                                   # the settlement is never repeated
    # Shared and independent work untouched.
    assert (await demand_of(theirs["id"]))["withdrawn_at"] is None
    assert (await demand_of(other["id"]))["withdrawn_at"] is None
    assert await state_of(world, shared) not in {TransferState.CANCELLED, TransferState.DELETED}
    assert await state_of(world, other_transfer) not in {TransferState.CANCELLED, TransferState.DELETED}
    listing = await world.external.visible(world.clients.a, settled=False)
    assert not {mine["id"], lonely["id"]} & {job["id"] for job in listing["jobs"]}
