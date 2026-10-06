"""Live provider routing corrective (transfers 516 and 519).

Permanence is scoped to the authority that produced the error. A provider's
own refusal to produce a candidate for a root exhausts THAT provider for the
root and the neutral router continues through the remaining legitimate
claimants; the logical transfer becomes terminal only when routing proves none
remains. A failure about the request itself stays terminal and never fails
over. The provider adapter translates its native code into that neutral scope;
the router branches on no provider, code or diagnostic.

A manual-switch target is ``prepared`` only when provider-side preparation is
complete and the backup is eligible for immediate promotion; a backup whose
provider is still acquiring the content is ``preparing`` and not a target, and
one its own provider has since contradicted is no promise at all. A remote
preparation that makes no useful progress for the Stalled Timeout is that
provider's failure, observed on the existing resource-poll cadence.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fake_integrations import MemoryExecutor
from test_v113_collection_route_generic_closure import Clock
from test_v113_realdebrid_provider import FILES as RD_FILES
from test_v113_realdebrid_provider import LINKS as RD_LINKS
from test_v113_realdebrid_provider import FakeClient, info, provider_resource
from test_v113_root_provider_switch import MAGNET, magnet_provider, root_of, rows

from db import database
from providers.realdebrid.client import RealDebridAPIError
from providers.realdebrid.provider import RealDebridProvider
from providers.realdebrid.translation import translate_error
from transfers import manual_route_switch
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Permanence, Retryability, Stage, TransferError
from transfers.manual_route_switch import standby_choice, switch_root_provider
from transfers.models import ResourceState, TransferProgress, TransferRequest
from transfers.policy import TransferPolicy, provider_attributable
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

FILES = [("one.bin", "Show/one.bin", 4), ("two.bin", "Show/two.bin", 4), ("three.bin", "Show/three.bin", 4)]
BACKEND = Path(__file__).resolve().parents[1]


def native_refusal(code: int, diagnostic: str) -> TransferError:
    """What the Real-Debrid adapter raises for a native refusal met while
    preparing candidates: its own translation owner, unchanged."""
    return TransferError(translate_error(RealDebridAPIError(code, diagnostic, 451),
                                         stage=Stage.CANDIDATE_PREPARATION))


def refusing(provider, *failures):
    """``provider`` publishes the root's manifest, then every executable
    manifest it is asked for fails with the next of ``failures`` (the last
    repeats)."""
    remaining = list(failures)

    async def manifest(resource):
        provider.calls.append(("manifest", resource.id))
        raise remaining.pop(0) if len(remaining) > 1 else remaining[0]

    provider.manifest = manifest
    return provider


def offer(provider, *, native="x", state=ResourceState.AVAILABLE):
    result = provider.parcel(native, state=state, files=FILES)
    observed = replace(result.observation, request=TransferRequest("magnet", MAGNET))
    provider.resources[observed.resource.id] = observed
    provider.responses.append(replace(result, observation=observed))
    return observed.resource


STALLED_AFTER = 300.0                                         # the Stalled Timeout these proofs run under


async def lab(tmp_path, monkeypatch, *providers):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "routing.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    for provider in providers:
        registry.register_provider(provider)
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3,
                                                         stalled_after_seconds=STALLED_AFTER), clock=Clock())
    await engine.initialize()
    return repository, engine


async def submit(engine):
    return await engine.submit((TransferRequest("magnet", MAGNET, name="Show", selection_mode="all"),),
                               name="Show", deduplicate=False)


async def settle(engine, ticks=8):
    for _ in range(ticks):
        engine.clock.now += 31
        await engine.tick()


async def history(request_id):
    return [(row["provider_id"], row["state"], row["outcome"], row["transition_reason"],
             row["error"]) for row in await rows(
        """SELECT a.provider_id,a.state,a.error,p.outcome,p.transition_reason FROM route_attempt_provenance p
           JOIN resolution_attempts a ON a.id=p.resolution_attempt_id WHERE a.request_id=? ORDER BY p.ordinal""",
        (request_id,))]


async def terminal_count(transfer_id):
    return (await rows("SELECT COUNT(*) AS n FROM application_events WHERE transfer_id=? AND kind='error'",
                       (transfer_id,)))[0]["n"]


async def status(transfer_id):
    return (await rows("SELECT status FROM torrents WHERE id=?", (transfer_id,)))[0]["status"]


# -- 11.1: the transfer-519 path, native refusal -> neutral provider scope -> exhaustion -> next claimant ----------

async def test_the_real_debrid_adapter_scopes_an_infringing_file_refusal_to_itself():
    """Through the real adapter: the member link's unrestriction answers
    native code 35 while the executable manifest is prepared."""
    provider = RealDebridProvider(FakeClient(
        torrent_info=info(files=RD_FILES, links=RD_LINKS),
        unrestrict_link=RealDebridAPIError(35, "infringing_file", 451)))
    with pytest.raises(TransferError) as refused:
        await provider.manifest(provider_resource())
    error = refused.value.error
    assert (error.domain, error.category, error.origin, error.stage) == (
        Domain.PROVIDER, Category.CANDIDATE_REJECTED, Origin.PROVIDER, Stage.CANDIDATE_PREPARATION)
    assert (error.permanence, error.retryability) == (Permanence.PERMANENT, Retryability.NEVER)
    assert (error.integration_id, error.native_code, error.diagnostic) == ("realdebrid", "35", "infringing_file")
    assert provider_attributable(error)
    # A request-global refusal from the same adapter stays the request's own.
    assert not provider_attributable(native_refusal(30, "torrent_file_invalid").error)


async def test_a_provider_scoped_candidate_refusal_exhausts_only_that_provider_and_the_next_claimant_continues(
        tmp_path, monkeypatch):
    first = refusing(magnet_provider("parcel-a"), native_refusal(35, "infringing_file"))
    second = magnet_provider("parcel-b")
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    offer(first)
    offer(second)
    transfer = await submit(engine)
    root = await root_of(repository, transfer.id)

    await settle(engine)

    assert await repository.exhausted_route_providers(root.id) == frozenset({"parcel-a"})
    assert await repository.bound_route_provider(root.id) == "parcel-b"            # one current route
    attempts = await history(root.id)
    assert [(provider, state, outcome) for provider, state, outcome, _reason, _error in attempts] == [
        ("parcel-a", "exhausted", "failed"), ("parcel-b", "succeeded", "resolved")]
    preserved = attempts[0][4]
    for fact in ('"category":"candidate_rejected"', '"native_code":"35"', '"diagnostic":"infringing_file"',
                 '"integration_id":"realdebrid"', '"stage":"candidate_preparation"'):
        assert fact in preserved, fact                                             # exact provider error kept
    assert attempts[1][3] == "candidate_rejected"                                  # why the route moved
    artifacts = await repository.artifacts(transfer.id)
    assert len(artifacts) == len(FILES)                                            # one graph, never two
    assert {candidate.provider_id for artifact in artifacts for candidate in artifact.candidates} == {"parcel-b"}
    assert await status(transfer.id) != "error" and await terminal_count(transfer.id) == 0


async def test_neutral_routing_names_no_provider_native_code_or_diagnostic():
    for owner in ("transfers/policy.py", "transfers/_engine_base.py", "transfers/engine.py",
                  "transfers/registry.py", "transfers/manual_route_switch.py", "transfers/repository.py",
                  "transfers/_repository_base.py"):
        text = (BACKEND / owner).read_text().lower()
        for native in ("infringing", "realdebrid", "real-debrid", "debridlink", '"35"', "== 35"):
            assert native not in text, (owner, native)


# -- 11.2: a request-global failure at the same stage stays terminal and never fails over --------------------------

@pytest.mark.parametrize("failure", [
    native_refusal(30, "torrent_file_invalid"),                                   # invalid request (native)
    TransferError(NormalizedError(Domain.SECURITY, Category.PATH_POLICY_VIOLATION, Stage.CANDIDATE_PREPARATION)),
    TransferError(NormalizedError(Domain.RESOLUTION, Category.CONTENT_INVALID, Stage.CANDIDATE_PREPARATION,
                                  Retryability.NEVER, origin=Origin.REMOTE_SOURCE, permanence=Permanence.PERMANENT)),
], ids=["invalid-request", "path-policy", "content-invalid"])
async def test_a_request_global_failure_is_terminal_once_and_no_alternate_is_tried(tmp_path, monkeypatch, failure):
    first = refusing(magnet_provider("parcel-a"), failure)
    second = magnet_provider("parcel-b")
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    offer(first)
    offer(second)
    transfer = await submit(engine)
    root = await root_of(repository, transfer.id)

    await settle(engine)

    assert ("resolve", MAGNET) not in second.calls, "a request-global failure failed over"
    assert await repository.exhausted_route_providers(root.id) == frozenset()
    assert (await root_of(repository, transfer.id)).state == "failed"
    assert await status(transfer.id) == "error" and await terminal_count(transfer.id) == 1


# -- 11.3: every legitimate provider exhausted -> terminal exactly once, every failure kept ------------------------

async def test_the_last_provider_s_failure_ends_the_transfer_once_with_every_provider_failure_kept(
        tmp_path, monkeypatch):
    protocol = TransferError(NormalizedError(Domain.PROVIDER, Category.PROVIDER_PROTOCOL_VIOLATION,
                                             Stage.RECONCILIATION, Retryability.UNKNOWN, origin=Origin.PROVIDER,
                                             integration_id="parcel-b", diagnostic="invalid JSON"))
    first = refusing(magnet_provider("parcel-a"), native_refusal(35, "infringing_file"))
    second = refusing(magnet_provider("parcel-b"), protocol)
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    offer(first)
    for native in ("b1", "b2", "b3"):                                             # one per retry of its budget
        offer(second, native=native)
    transfer = await submit(engine)
    root = await root_of(repository, transfer.id)

    await settle(engine, ticks=14)

    assert await repository.exhausted_route_providers(root.id) == frozenset({"parcel-a", "parcel-b"})
    attempts = await history(root.id)
    assert {provider for provider, state, *_ in attempts if state == "exhausted"} == {"parcel-a", "parcel-b"}
    exhausted = {provider: error for provider, state, _outcome, _reason, error in attempts if state == "exhausted"}
    assert '"candidate_rejected"' in exhausted["parcel-a"] and '"provider_protocol_violation"' in exhausted["parcel-b"]
    record = await root_of(repository, transfer.id)
    assert record.state == "failed" and record.error.category == Category.PROVIDER_PROTOCOL_VIOLATION
    assert await status(transfer.id) == "error" and await terminal_count(transfer.id) == 1
    assert await repository.artifacts(transfer.id) == ()


# -- 11.4: the only remaining claimant is still preparing: wait through the existing route, wake or bound ---------

async def preparing_backup(repository, engine, provider, transfer_id):
    root = await root_of(repository, transfer_id)
    acquiring = provider.parcel("acquiring", state=ResourceState.PREPARING, files=FILES)
    standby_id, _attempts = await repository.begin_standby(transfer_id, root.id, provider.descriptor.id,
                                                           engine.clock())
    await repository.bind_standby(standby_id, transfer_id, acquiring.observation.resource, ResourceState.PREPARING,
                                  engine.clock())
    return standby_id, acquiring.observation.resource


async def test_a_preparing_alternate_is_waited_for_and_continues_by_itself_when_it_matures(tmp_path, monkeypatch):
    first = refusing(magnet_provider("parcel-a"), native_refusal(35, "infringing_file"))
    second = magnet_provider("parcel-b")
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    offer(first)
    transfer = await submit(engine)
    standby_id, resource = await preparing_backup(repository, engine, second, transfer.id)

    await settle(engine)                                       # parcel-a exhausted; parcel-b still acquiring
    root = await root_of(repository, transfer.id)
    assert await repository.exhausted_route_providers(root.id) == frozenset({"parcel-a"})
    assert await repository.bound_route_provider(root.id) == "parcel-b"
    assert await status(transfer.id) != "error" and await terminal_count(transfer.id) == 0
    assert await repository.artifacts(transfer.id) == ()

    matured = second.parcel("acquiring", state=ResourceState.AVAILABLE, files=FILES)  # the remote side finishes
    assert matured.observation.resource.id == resource.id
    await settle(engine)                                       # the ordinary resolution cadence observes it

    assert len(await repository.artifacts(transfer.id)) == len(FILES)
    assert ("resolve", MAGNET) not in second.calls, "a cold duplicate resource was created"
    assert [item["id"] for item in await repository.standbys(transfer.id) if item.get("promoted_at")] == [standby_id]
    assert await status(transfer.id) != "error" and await terminal_count(transfer.id) == 0


async def preparing_after_first_route_failed(tmp_path, monkeypatch):
    """parcel-a refuses on its own account; parcel-b, the only claimant left,
    holds a backup that is still preparing."""
    first = refusing(magnet_provider("parcel-a"), native_refusal(35, "infringing_file"))
    second = magnet_provider("parcel-b")
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    offer(first)
    transfer = await submit(engine)
    _standby_id, resource = await preparing_backup(repository, engine, second, transfer.id)
    return repository, engine, second, transfer, resource


def report_progress(provider, resource, completed):
    provider.resources[resource.id] = replace(provider.resources[resource.id],
                                              progress=TransferProgress(10_000, completed, 0))


async def test_a_preparation_that_keeps_progressing_outlasts_the_stalled_timeout(tmp_path, monkeypatch):
    repository, engine, second, transfer, resource = await preparing_after_first_route_failed(tmp_path, monkeypatch)
    started = engine.clock.now
    for tick in range(30):                                     # ~3x the Stalled Timeout in total
        report_progress(second, resource, 100 * (tick + 1))
        await settle(engine, ticks=1)
    assert engine.clock.now - started > 3 * STALLED_AFTER
    root = await root_of(repository, transfer.id)
    assert await repository.exhausted_route_providers(root.id) == frozenset({"parcel-a"})
    assert await repository.bound_route_provider(root.id) == "parcel-b"
    assert await status(transfer.id) != "error" and await terminal_count(transfer.id) == 0


@pytest.mark.parametrize("completed", [1234, None], ids=["fixed-progress", "no-progress-metric"])
async def test_a_preparation_without_progress_is_exhausted_by_the_existing_budget(tmp_path, monkeypatch, completed):
    repository, engine, second, transfer, resource = await preparing_after_first_route_failed(tmp_path, monkeypatch)
    if completed is not None:
        report_progress(second, resource, completed)
    await settle(engine, ticks=2)
    root = await root_of(repository, transfer.id)
    assert await repository.bound_route_provider(root.id) == "parcel-b"
    began = engine.clock.now
    while await status(transfer.id) != "error" and engine.clock.now - began < 4 * STALLED_AFTER:
        assert "parcel-b" not in await repository.exhausted_route_providers(root.id) \
            or engine.clock.now - began >= STALLED_AFTER - 31, "exhausted before the Stalled Timeout"
        await settle(engine, ticks=1)

    root = await root_of(repository, transfer.id)
    assert await repository.exhausted_route_providers(root.id) == frozenset({"parcel-a", "parcel-b"})
    stalled = [error for provider, state, _outcome, _reason, error in await history(root.id)
               if provider == "parcel-b" and state == "exhausted"]
    assert stalled and '"category":"transfer_stalled"' in stalled[0]
    assert root.state == "failed"
    assert await status(transfer.id) == "error" and await terminal_count(transfer.id) == 1


# -- 11.6 / 11.7: Prepared is a promise of immediate promotion; nothing weaker uses it -----------------------------

@pytest.mark.parametrize("state, resource, contradicted, expected", [
    ("bound", "available", False, ("prepared", True)),
    ("bound", "available", True, ("failed_earlier", True)),      # transfer 519: its own route failed since
    ("bound", "preparing", False, ("preparing", False)),         # transfer 516: still acquiring
    ("creating", None, False, ("preparing", False)),
    ("bound", "unknown", False, ("failed_earlier", True)),
    ("bound", "unavailable", False, ("failed_earlier", True)),
    ("bound", "absent", False, ("available", True)),             # nothing held: a cold target
    ("deferred", None, False, ("deferred", True)),
])
async def test_only_an_uncontradicted_available_backup_is_prepared(state, resource, contradicted, expected):
    assert standby_choice({"state": state, "resource_state": resource, "contradicted": contradicted}) == expected


async def test_a_backup_its_own_provider_contradicted_is_never_promised_and_switching_is_admitted_cold(
        tmp_path, monkeypatch):
    first, second = magnet_provider("parcel-a"), magnet_provider("parcel-b")
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    offer(first)
    transfer = await submit(engine)
    await settle(engine, ticks=3)
    root = await root_of(repository, transfer.id)
    backup = second.parcel("backup", state=ResourceState.AVAILABLE, files=FILES)
    standby_id, _attempts = await repository.begin_standby(transfer.id, root.id, "parcel-b", engine.clock())
    await repository.bind_standby(standby_id, transfer.id, backup.observation.resource, ResourceState.AVAILABLE,
                                  engine.clock())
    chosen = lambda status: {entry["provider_id"]: (entry["status"], entry["selectable"])  # noqa: E731
                             for entry in status["providers"]}["parcel-b"]
    assert chosen(await manual_route_switch.route_providers(engine, transfer.id)) == ("prepared", True)

    # Its own route of the root fails after the resource was last seen available.
    async with database.get_db() as db:
        await db.execute(
            """INSERT INTO resolution_attempts(id,request_id,provider_id,state,error,created_at,updated_at)
               VALUES('contradiction',?,'parcel-b','released','{"category":"provider_protocol_violation"}',
                      CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""", (root.id,))
        await db.commit()
    assert chosen(await manual_route_switch.route_providers(engine, transfer.id)) == ("failed_earlier", True)

    admitted = []
    make_room = engine._make_primary_room

    async def counted(record, provider):
        admitted.append(provider.descriptor.id)
        return await make_room(record, provider)

    monkeypatch.setattr(engine, "_make_primary_room", counted)
    await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    assert admitted == ["parcel-b"], "an unproven backup bypassed primary admission as if prepared"


async def test_a_prepared_backup_is_promised_and_promoted_without_a_cold_duplicate(tmp_path, monkeypatch):
    first, second = magnet_provider("parcel-a"), magnet_provider("parcel-b")
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    offer(first)
    transfer = await submit(engine)
    await settle(engine, ticks=3)
    root = await root_of(repository, transfer.id)
    backup = second.parcel("backup", state=ResourceState.AVAILABLE, files=FILES)
    standby_id, _attempts = await repository.begin_standby(transfer.id, root.id, "parcel-b", engine.clock())
    await repository.bind_standby(standby_id, transfer.id, backup.observation.resource, ResourceState.AVAILABLE,
                                  engine.clock())
    status_before = await manual_route_switch.route_providers(engine, transfer.id)
    assert {entry["provider_id"]: entry["status"] for entry in status_before["providers"]}["parcel-b"] == "prepared"

    await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    await settle(engine)

    assert (await root_of(repository, transfer.id)).resource.id == backup.observation.resource.id
    assert ("resolve", MAGNET) not in second.calls
    assert [item["id"] for item in await repository.standbys(transfer.id) if item.get("promoted_at")] == [standby_id]
    artifacts = await repository.artifacts(transfer.id)
    assert len(artifacts) == len(FILES)
    assert {candidate.provider_id for artifact in artifacts for candidate in artifact.candidates} == {"parcel-b"}
