"""1.0.14 C2: a provider quota refusal with a typed scope holds that scope
temporarily -- recovery goes on elsewhere, never back into the same scope
while the hold is current, and the scope requalifies through ONE real
dispatch of pending work every ``TransferPolicy.quota_requalify_seconds``.

Transfer 1279 (2026-11-02): Debrid-Link refused fresh links with ``maxData``.
Artifact 22409's next untried source was another Debrid-Link host on the
same account, refused again, while proven-equivalent AllDebrid sources --
bound from consolidated submissions -- waited unused
(investigations/dp-1.0.14-debridlink-quota-stagnation).

The real Debrid-Link client (envelope), provider (refresh -> resolve) and
translation (which states the scope: ``maxData`` the account, ``maxDataHost``
the account's use of the link's host) run over an in-memory HTTP transport;
the real engine, repository and policy decide on a scratch database. The
refusal has the incident's recorded shape -- a JSON answer that is not a
success, with the native ``error`` -- at 403 (the recorded status is
unknown; the investigation showed the classification is the same at every
status). No reset time is claimed anywhere.
"""
from __future__ import annotations

import json
from contextlib import closing
from dataclasses import replace
import sqlite3

import pytest

import db.database as database
from integrations.runtime_state import credential_scope
from providers.debridlink.client import DebridLinkService, RawResponse
from providers.debridlink.host_runtime import (
    DebridLinkRequestApplicability, applicability_facts, parse_native_host_snapshot,
)
from providers.debridlink.provider import DebridLinkProvider
from test_v113_debridlink_engine import HOSTS, KEY, SIZE, Elsewhere, Sampler
from transfers.convergence_engine import TransferEngine
from transfers.errors import QUOTA_SCOPE
from transfers.models import ResolverArtifactIdentityEvidence, TransferRequest
from transfers.policy import RecoveryAction, TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

HERE = "https://hoster.example/f/movie.bin"      # Debrid-Link, host hoster.example (1279: gofile.io)
HERE2 = "https://mirror.example/f/movie.bin"     # Debrid-Link, same account, host mirror.example (1279: pixeldrain.com)
THERE = "https://other.example/movie.bin"        # another provider's equivalent link (1279: AllDebrid)
OTHER = "https://hoster.example/f/second.bin"    # a different file on Debrid-Link
BODIES = {"hoster.example/f/movie.bin": b"same", "mirror.example/f/movie.bin": b"same",
          "other.example/movie.bin": b"same", "hoster.example/f/second.bin": b"diff"}
INTERVAL = TransferPolicy().quota_requalify_seconds
ACCOUNT = credential_scope("debridlink", KEY)


class DebridLinkHTTP:
    """Debrid-Link's HTTP side, in memory. ``code`` (``maxData`` or
    ``maxDataHost``) is answered for a link once it has been served
    ``warm[link]`` times (resolution and evidence sampling come first); for
    ``maxData``, the first refusal exhausts the whole account, so every later
    link is refused too. ``recovered`` ends all refusals."""

    def __init__(self, code="maxData", warm=None, hosts=None):
        self.code, self.warm, self.hosts = code, dict(warm or {}), hosts
        self.exhausted = False
        self.recovered = False
        self.faults = 0         # answers that are neither an issuance nor a quota refusal (a 503)
        self.adds = []          # (refused?, url); a fault is recorded as ("fault", url)

    def asked(self, url=None, *, refused=None):
        return sum(1 for was, item in self.adds
                   if (url is None or item == url) and (refused is None or was == refused))

    def _refuses(self, link):
        if self.recovered or not link.startswith("https://") or "other.example" in link:
            return False
        if self.code == "maxDataHost" and self.hosts is not None and link.split("/")[2] not in self.hosts:
            return False
        if self.exhausted and self.code == "maxData":
            return True
        return link in self.warm and self.asked(link) >= self.warm[link]

    async def __call__(self, method, url, *, headers=None, params=None, data=None, timeout=None):
        path = url.split("/v2/", 1)[-1]
        if method == "GET" and path.startswith("account/infos"):
            return self._ok({"accountType": 1, "premiumLeft": 86400})
        assert method == "POST" and path == "downloader/add", (method, path)
        link = json.loads(data)["url"]
        if self.faults and link.startswith("https://") and "other.example" not in link:
            self.faults -= 1
            self.adds.append(("fault", link))
            return RawResponse(503, {"content-type": "application/json"}, json.dumps({"success": False}).encode())
        if self._refuses(link):
            self.exhausted = True
            self.adds.append((True, link))
            return RawResponse(403, {"content-type": "application/json"},
                               json.dumps({"success": False, "error": self.code}).encode())
        self.adds.append((False, link))
        key = link.split("://", 1)[1]
        return self._ok({"id": f"l{len(self.adds):06d}", "name": link.rsplit("/", 1)[-1], "size": SIZE, "url": link,
                         "downloadUrl": f"https://dl6.debrid.link/dl/{key}?capability=dl-token-{len(self.adds)}",
                         "expired": False})

    @staticmethod
    def _ok(value):
        return RawResponse(200, {"content-type": "application/json"},
                           json.dumps({"success": True, "value": value}).encode())


class AttestedElsewhere(Elsewhere):
    """The other provider stating name and exact size in its own answer, as
    AllDebrid's resolutions did for 1279's consolidated sources: equivalence
    is resolver-attested, with no sample of the held provider's material."""

    async def resolve(self, request):
        result = await super().resolve(request)
        return replace(result, candidates=tuple(
            replace(item, resolver_identity_evidence=ResolverArtifactIdentityEvidence(
                resolved_name="movie.bin", exact_bytes=SIZE)) for item in result.candidates))


class Clock:
    now = 1_000_000.0

    def __call__(self):
        return self.now


HOSTS2 = [{**HOSTS[0], "domains": ["hoster.example", "mirror.example"]}]


def build(tmp_path, http, clock, *, key=KEY, attested=True):
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = DebridLinkProvider(DebridLinkService(key, transport=http))
    snapshot = parse_native_host_snapshot(HOSTS2)
    provider.applicability = applicability_facts(snapshot)
    provider.applicability_for = DebridLinkRequestApplicability(snapshot)
    registry.register_provider(provider)
    registry.register_provider(AttestedElsewhere() if attested else Elsewhere())
    executor = Sampler(repository.authorize_execution, BODIES)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "downloads"),
                            policy=TransferPolicy(adoption_stability_seconds=0, max_active_executions=8,
                                                  resolution_concurrency=8),
                            clock=clock)
    return engine, repository, executor


async def start(tmp_path, monkeypatch, http, *, attested=True):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "quota.db")
    await database.init_db()
    clock = Clock()
    engine, repository, executor = build(tmp_path, http, clock, attested=attested)
    await engine.initialize()
    return engine, repository, executor, clock


async def cycle(engine, clock, seconds=10.0):
    clock.now += seconds
    await engine.reconcile_executions()
    await engine.tick()


async def holds():
    async with database.get_db() as db:
        return [dict(row) for row in await db.fetchall(
            "SELECT * FROM provider_quota_holds ORDER BY scope_kind, scope_key")]


async def state(artifact_id):
    async with database.get_db() as db:
        row = await db.fetchone("SELECT decision_action,decision_reason,quiescence_reason,wake_condition "
                                "FROM artifact_recovery_state WHERE artifact_id=?", (artifact_id,))
    return dict(row or {})


async def claims(artifact_id):
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT detail FROM application_events WHERE kind='recovery_audit'")
    return sum(1 for row in rows if json.loads(row["detail"]).get("transition") == "claim"
               and json.loads(row["detail"]).get("artifact_id") == artifact_id)


async def only_artifact(repository, transfer_id):
    [artifact] = await repository.artifacts(transfer_id)
    return artifact


# -- 1. account scope: the same account's other host is skipped; the other provider starts

async def test_an_account_quota_skips_the_same_accounts_other_host_and_the_other_provider_starts(
        tmp_path, monkeypatch):
    """1279's shape: Debrid-Link's two hosts of one file in one submission,
    the other provider's equivalent arriving in a later one."""
    http = DebridLinkHTTP("maxData", warm={HERE: 1})       # resolution, then the execution's fresh link
    engine, repository, executor, clock = await start(tmp_path, monkeypatch, http)
    first = await engine.submit((TransferRequest("https", HERE, "movie.bin"),
                                 TransferRequest("https", HERE2, "movie.bin")), name="movie", deduplicate=False)
    await engine.tick()
    artifact = await only_artifact(repository, first.id)
    assert [(c.provider_id, c.source_identity.key) for c in artifact.candidates] == [
        ("debridlink", "hoster.example"), ("debridlink", "mirror.example")]
    assert http.asked(HERE, refused=True) == 1
    [hold] = await holds()
    assert (hold["integration_id"], hold["account_scope"], hold["scope_kind"], hold["scope_key"]) == (
        "debridlink", ACCOUNT, "account", "")
    assert KEY not in json.dumps(hold)
    # The same account's other host is not tried; with nothing else the file
    # waits on the hold -- truthfully, with no further claims or calls.
    assert (await state(artifact.id))["quiescence_reason"] == "provider_quota"
    mirror_asked = http.asked(HERE2)
    parked = await claims(artifact.id)
    for _ in range(3):
        await cycle(engine, clock)
    assert http.asked(HERE2) == mirror_asked and http.asked(HERE, refused=True) == 1
    assert await claims(artifact.id) == parked

    await engine.submit((TransferRequest("https", THERE, "movie.bin"),), name="movie-elsewhere")
    for _ in range(4):
        await cycle(engine, clock)
    artifact = await only_artifact(repository, first.id)
    assert artifact.candidates[artifact.selected].provider_id == "elsewhere"
    attempts = await repository.executions(first.id)
    assert [attempt.candidate.provider_id for attempt in attempts] == ["elsewhere"]
    assert artifact.execution.attempt_id == attempts[0].handle.attempt_id
    assert http.asked(HERE2) == mirror_asked     # never asked for the refused account's other host
    assert http.asked(HERE, refused=True) == 1


# -- 2. host scope: another host on the same account stays eligible; accounts are isolated

async def test_a_host_quota_leaves_the_same_accounts_other_host_eligible(tmp_path, monkeypatch):
    http = DebridLinkHTTP("maxDataHost", warm={HERE: 1}, hosts={"hoster.example"})
    engine, repository, executor, clock = await start(tmp_path, monkeypatch, http)
    transfer = await engine.submit((TransferRequest("https", HERE, "movie.bin"),
                                    TransferRequest("https", HERE2, "movie.bin")), name="movie", deduplicate=False)
    await engine.tick()
    [hold] = await holds()
    assert (hold["scope_kind"], hold["scope_key"], hold["account_scope"]) == ("host", "hoster.example", ACCOUNT)
    for _ in range(4):
        await cycle(engine, clock)
    artifact = await only_artifact(repository, transfer.id)
    assert artifact.candidates[artifact.selected].source_identity.key == "mirror.example"
    attempts = await repository.executions(transfer.id)
    assert [attempt.candidate.source_identity.key for attempt in attempts] == ["mirror.example"]


async def test_holds_are_per_account_and_carry_no_credential(tmp_path, monkeypatch):
    http = DebridLinkHTTP("maxData", warm={HERE: 1})
    engine, repository, executor, clock = await start(tmp_path, monkeypatch, http)
    transfer = await engine.submit((TransferRequest("https", HERE, "movie.bin"),), name="movie")
    await engine.tick()
    artifact = await only_artifact(repository, transfer.id)
    candidate = artifact.candidates[0]
    assert await repository.quota_hold_until(engine._quota_keys(candidate), artifact.id, clock()) is not None
    # Another API key is another account: nothing of this hold applies to it.
    other, _repository, _executor = build(tmp_path, DebridLinkHTTP(), clock, key="another-account-key")
    assert other._quota_keys(candidate) and other._quota_keys(candidate) != engine._quota_keys(candidate)
    assert await repository.quota_hold_until(other._quota_keys(candidate), artifact.id, clock()) is None
    with closing(sqlite3.connect(database.DB_PATH)) as conn:
        dumped = json.dumps(conn.execute("SELECT * FROM provider_quota_holds").fetchall())
    assert KEY not in dumped and "dl-token" not in dumped


# -- 3/4. unknown permanence: a truthful wait, one probe per interval, cleared by success

async def test_a_sole_source_waits_on_the_hold_and_one_probe_per_interval_requalifies_it(tmp_path, monkeypatch):
    http = DebridLinkHTTP("maxData", warm={HERE: 1})
    engine, repository, executor, clock = await start(tmp_path, monkeypatch, http)
    transfer = await engine.submit((TransferRequest("https", HERE, "movie.bin"),), name="movie")
    await engine.tick()
    artifact = await only_artifact(repository, transfer.id)
    decided = await state(artifact.id)
    assert (decided["decision_action"], decided["decision_reason"], decided["quiescence_reason"]) == (
        RecoveryAction.WAIT_FOR_PROVIDER.value, "provider_quota_hold", "provider_quota")
    assert decided["wake_condition"].startswith("quota_requalify:")
    assert artifact.state == "recovery_wait"
    from transfers.presentation_repository import _WAIT_PRESENTATION
    assert _WAIT_PRESENTATION["provider_quota"][0] == "waiting_for_provider"

    # Before the interval: no call, no claim.
    parked = await claims(artifact.id)
    for _ in range(5):
        await cycle(engine, clock, 50.0)          # 250 s
    assert http.asked(HERE, refused=True) == 1 and await claims(artifact.id) == parked

    # Due, still refused: exactly one probe, the hold renewed (never terminal).
    await cycle(engine, clock, 60.0)              # 310 s
    assert http.asked(HERE, refused=True) == 2
    [hold] = await holds()
    assert hold["refusals"] == 2 and hold["requalify_at"] == pytest.approx(clock() + INTERVAL)
    assert (await state(artifact.id))["quiescence_reason"] == "provider_quota"
    for _ in range(5):
        await cycle(engine, clock, 50.0)
    assert http.asked(HERE, refused=True) == 2

    # The quota is back: the next probe is issued, clears the hold and the writer starts.
    http.recovered = True
    await cycle(engine, clock, 60.0)
    [hold] = await holds()
    assert hold["cleared_at"] >= hold["refused_at"]
    assert await repository.quota_hold_until(engine._quota_keys(artifact.candidates[0]), artifact.id, clock()) is None
    attempts = await repository.executions(transfer.id)
    assert [attempt.candidate.provider_id for attempt in attempts] == ["debridlink"]
    assert (await only_artifact(repository, transfer.id)).state == "downloading"


# -- 5. no preemption: a healthy writer elsewhere is left alone when the hold ends

async def test_a_healthy_writer_on_another_provider_is_not_preempted_when_the_hold_requalifies(
        tmp_path, monkeypatch):
    http = DebridLinkHTTP("maxData", warm={HERE: 1})
    engine, repository, executor, clock = await start(tmp_path, monkeypatch, http)
    transfer = await engine.submit((TransferRequest("https", HERE, "movie.bin"),
                                    TransferRequest("https", THERE, "movie.bin")), name="movie", deduplicate=False)
    await engine.tick()
    for _ in range(3):
        await cycle(engine, clock)
    [attempt] = await repository.executions(transfer.id)
    assert attempt.candidate.provider_id == "elsewhere"
    asked = http.asked()
    http.recovered = True
    for _ in range(4):
        await cycle(engine, clock, INTERVAL / 2)    # well past requalification
    assert [item.candidate.provider_id for item in await repository.executions(transfer.id)] == ["elsewhere"]
    assert [call for call in executor.calls if call[0] == "cancel"] == []
    assert http.asked() == asked
    artifact = await only_artifact(repository, transfer.id)
    assert artifact.execution.attempt_id == attempt.handle.attempt_id


# -- 6. restart and concurrency: the hold survives; one probe for many waiting files

async def test_two_waiting_files_on_one_account_make_one_probe_and_the_hold_survives_a_restart(
        tmp_path, monkeypatch):
    http = DebridLinkHTTP("maxData", warm={HERE: 1, OTHER: 1})
    engine, repository, executor, clock = await start(tmp_path, monkeypatch, http)
    one = await engine.submit((TransferRequest("https", HERE, "movie.bin"),), name="movie")
    two = await engine.submit((TransferRequest("https", OTHER, "second.bin"),), name="second")
    await engine.tick()
    await cycle(engine, clock)
    refused = http.asked(refused=True)
    assert len(await holds()) == 1 and refused >= 1

    engine, repository, executor = build(tmp_path, http, clock)   # restart inside the interval
    await engine.initialize()
    for _ in range(3):
        await cycle(engine, clock, 50.0)
    assert http.asked(refused=True) == refused                       # the hold survived: nothing asked
    await cycle(engine, clock, INTERVAL)
    assert http.asked(refused=True) == refused + 1                   # ONE probe for both files
    for _ in range(3):
        await cycle(engine, clock, 10.0)
    assert http.asked(refused=True) == refused + 1
    for transfer in (one, two):
        assert (await state((await only_artifact(repository, transfer.id)).id))["quiescence_reason"] == "provider_quota"


async def test_a_refusal_asked_before_a_success_cannot_renew_the_cleared_hold(tmp_path, monkeypatch):
    http = DebridLinkHTTP()
    engine, repository, executor, clock = await start(tmp_path, monkeypatch, http)
    key = ("debridlink", ACCOUNT, "account", "")
    assert await repository.hold_quota(key, asked_at=100.0, now=101.0, requalify_seconds=INTERVAL)
    assert await repository.clear_quota_holds((key,), answered_at=110.0) == 1
    # A slower refusal asked at 105 and answered at 120 is older than the success.
    assert not await repository.hold_quota(key, asked_at=105.0, now=120.0, requalify_seconds=INTERVAL)
    assert await repository.quota_hold_until((key,), 1, 130.0) is None
    # A success answered before a newer refusal does not end that refusal's hold.
    assert await repository.hold_quota(key, asked_at=130.0, now=131.0, requalify_seconds=INTERVAL)
    await repository.clear_quota_holds((key,), answered_at=125.0)
    assert await repository.quota_hold_until((key,), 1, 140.0) == pytest.approx(131.0 + INTERVAL)
    # The requalification turn is one artifact's; another waits for its lease.
    assert await repository.quota_admission((key,), 1, 131.0 + INTERVAL, lease_seconds=60.0) is None
    assert await repository.quota_admission((key,), 2, 131.0 + INTERVAL, lease_seconds=60.0) == pytest.approx(
        131.0 + INTERVAL + 60.0)


# -- 7. classes without a scope, and providers without an account identity, hold nothing

@pytest.mark.parametrize("code", ["maxLink", "maxLinkHost", "maxTorrent", "fileNotAvailable", "notDebrid"])
async def test_refusals_without_a_scope_fact_hold_nothing(tmp_path, monkeypatch, code):
    http = DebridLinkHTTP(code, warm={HERE: 1})
    engine, repository, executor, clock = await start(tmp_path, monkeypatch, http)
    transfer = await engine.submit((TransferRequest("https", HERE, "movie.bin"),), name="movie")
    await engine.tick()
    assert http.asked(HERE, refused=True) == 1
    assert await holds() == []
    error = (await only_artifact(repository, transfer.id)).error
    assert QUOTA_SCOPE not in (error.context or {})
    assert (await state((await only_artifact(repository, transfer.id)).id)).get("quiescence_reason") != "provider_quota"


async def test_the_scope_fact_is_the_adapters_and_absent_from_the_public_error():
    from providers.debridlink.client import DebridLinkAPIError
    from providers.debridlink.translation import error_from_native
    assert error_from_native(DebridLinkAPIError("maxData", 403)).context == {QUOTA_SCOPE: "account"}
    assert error_from_native(DebridLinkAPIError("maxDataHost", 403)).context == {QUOTA_SCOPE: "host"}
    for code in ("maxLink", "maxLinkHost", "maxTorrent", ""):
        assert QUOTA_SCOPE not in error_from_native(DebridLinkAPIError(code, 403)).context
    assert "context" not in error_from_native(DebridLinkAPIError("maxData", 403)).as_dict()


async def test_a_provider_without_an_account_identity_holds_nothing(tmp_path, monkeypatch):
    http = DebridLinkHTTP("maxData", warm={HERE: 1})
    engine, repository, executor, clock = await start(tmp_path, monkeypatch, http)
    provider = engine.registry.providers["debridlink"]
    provider.account_identity = lambda: ""
    await engine.submit((TransferRequest("https", HERE, "movie.bin"),), name="movie")
    await engine.tick()
    assert http.asked(HERE, refused=True) == 1 and await holds() == []


# -- 8. C1 composition: each refused dispatch still counts once ----------------

async def test_each_refused_probe_is_still_one_counted_occurrence(tmp_path, monkeypatch):
    http = DebridLinkHTTP("maxData", warm={HERE: 1})
    engine, repository, executor, clock = await start(tmp_path, monkeypatch, http)
    transfer = await engine.submit((TransferRequest("https", HERE, "movie.bin"),), name="movie")
    await engine.tick()
    await cycle(engine, clock, INTERVAL + 1)
    artifact = await only_artifact(repository, transfer.id)
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT detail FROM application_events WHERE kind='recovery_audit'")
    failures = [json.loads(row["detail"]) for row in rows if json.loads(row["detail"]).get("transition") == "source_failure"
                and json.loads(row["detail"]).get("artifact_id") == artifact.id]
    assert http.asked(HERE, refused=True) == 2 and len(failures) == 2


# -- 9. operator authority: a switch onto a held source is not frozen or misreported

async def test_an_operator_switch_onto_a_held_source_waits_truthfully_and_can_switch_away(tmp_path, monkeypatch):
    http = DebridLinkHTTP("maxData", warm={HERE: 1})
    engine, repository, executor, clock = await start(tmp_path, monkeypatch, http)
    transfer = await engine.submit((TransferRequest("https", HERE, "movie.bin"),
                                    TransferRequest("https", THERE, "movie.bin")), name="movie", deduplicate=False)
    await engine.tick()
    for _ in range(3):
        await cycle(engine, clock)
    artifact = await only_artifact(repository, transfer.id)
    assert artifact.candidates[artifact.selected].provider_id == "elsewhere"
    held = next(index for index, item in enumerate(artifact.candidates) if item.provider_id == "debridlink")
    asked = http.asked()
    result = await engine.activate_candidate_command(transfer.id, artifact.id, held)
    assert result is not None and result.committed
    for _ in range(3):
        await cycle(engine, clock)
    artifact = await only_artifact(repository, transfer.id)
    assert artifact.selected == held and http.asked() == asked     # Debrid-Link is not asked while held
    assert artifact.execution is None or artifact.execution.executor_id != "debridlink"
    back = next(index for index, item in enumerate(artifact.candidates) if item.provider_id == "elsewhere")
    result = await engine.activate_candidate_command(transfer.id, artifact.id, back)
    assert result is not None and result.committed
    for _ in range(3):
        await cycle(engine, clock)
    artifact = await only_artifact(repository, transfer.id)
    assert artifact.candidates[artifact.selected].provider_id == "elsewhere"
    assert artifact.execution is not None and artifact.state in {"downloading", "queued"}


# -- Gate 9 rejection of 001e3848, blocker 2: one issuance per turn, not per artifact

async def test_a_spent_requalification_turn_is_not_granted_again_to_the_same_artifact(tmp_path, monkeypatch):
    await start(tmp_path, monkeypatch, DebridLinkHTTP())
    repository = TransferRepository()
    key = ("debridlink", ACCOUNT, "account", "")
    assert await repository.hold_quota(key, asked_at=100.0, now=100.0, requalify_seconds=INTERVAL)
    due = 100.0 + INTERVAL
    issue = {"requalify_seconds": INTERVAL, "lease_seconds": 120.0}
    # Repeated admission checks of one dispatch, before it asks, are harmless.
    assert await repository.quota_admission((key,), 1, due, lease_seconds=120.0) is None
    assert await repository.quota_admission((key,), 1, due + 1, lease_seconds=120.0) is None
    # The issuance spends the turn ...
    assert await repository.quota_issuance((key,), 1, due + 2, **issue) is None
    # ... so neither the same artifact, inside its lease, nor anyone else is
    # admitted or issued again before the next interval -- whatever the first
    # answer was (here: none at all, as after an unrelated failure).
    nxt = due + 2 + INTERVAL
    assert await repository.quota_admission((key,), 1, due + 3, lease_seconds=120.0) == pytest.approx(nxt)
    assert await repository.quota_issuance((key,), 1, due + 3, **issue) == pytest.approx(nxt)
    assert await repository.quota_issuance((key,), 2, due + 3, **issue) == pytest.approx(nxt)
    assert await repository.quota_hold_until((key,), 1, due + 3) == pytest.approx(nxt)
    [hold] = await holds()
    assert hold["refusals"] == 1 and hold["refused_at"] == 100.0 and hold["cleared_at"] == 0
    # An evidence sample never takes the turn, even when it is due.
    assert await repository.quota_issuance((key,), None, nxt + 1, **issue) is not None
    # The next interval is one turn again.
    assert await repository.quota_issuance((key,), 2, nxt + 1, **issue) is None
    assert await repository.quota_issuance((key,), 1, nxt + 2, **issue) is not None


async def test_an_unrelated_probe_failure_does_not_buy_a_second_request_before_the_next_interval(
        tmp_path, monkeypatch):
    http = DebridLinkHTTP("maxData", warm={HERE: 1})
    engine, repository, executor, clock = await start(tmp_path, monkeypatch, http)
    transfer = await engine.submit((TransferRequest("https", HERE, "movie.bin"),), name="movie")
    await engine.tick()
    artifact = await only_artifact(repository, transfer.id)
    assert http.asked(HERE, refused=True) == 1
    [before] = await holds()

    # The probe is due; Debrid-Link answers it with a 503 -- neither an
    # issuance nor a quota refusal: the hold is neither cleared nor renewed.
    http.faults = 1
    await cycle(engine, clock, INTERVAL + 1)
    assert http.asked(HERE, refused="fault") == 1
    probed = clock()
    asked = http.asked()
    # Its own classification stands (the provider's 503, counted once as a
    # failure of its own -- not a quota) ...
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT detail FROM application_events WHERE kind='recovery_audit' ORDER BY id")
    classified = [json.loads(row["detail"])["failure_classification"] for row in rows
                  if "failure_classification" in json.loads(row["detail"])]
    assert classified[-1]["category"] == "provider_unavailable"
    [after] = await holds()
    assert (after["refusals"], after["refused_at"], after["cleared_at"]) == (
        before["refusals"], before["refused_at"], before["cleared_at"])
    # ... but the redispatch it schedules gets no second turn in the interval.
    for _ in range(14):
        await cycle(engine, clock, 20.0)        # 280 s, well past retry backoff and the 120 s lease
    assert http.asked() == asked
    assert (await state(artifact.id))["quiescence_reason"] == "provider_quota"
    # The next interval is one real request again (and the quota is back).
    http.recovered = True
    while clock() < probed + INTERVAL + 30:
        await cycle(engine, clock, 10.0)
    assert http.asked() == asked + 1 and http.asked(HERE, refused=False) == 2
    assert [item.candidate.provider_id for item in await repository.executions(transfer.id)] == ["debridlink"]


# -- Gate 9 rejection of 001e3848, blocker 1: equivalence sampling under a hold

async def test_equivalence_sampling_under_a_hold_never_asks_and_never_moves_its_requalification(
        tmp_path, monkeypatch):
    """The late, UNATTESTED equivalent (the other provider names no size):
    its proof needs a sample of the held Debrid-Link source. The sample is
    not issued while the hold is current -- the proof stays unavailable, so
    the barrier holds it (fail closed: no independent writer) on its own
    bounded reconsideration schedule -- and the held file's requalification
    happens on time."""
    http = DebridLinkHTTP("maxData", warm={HERE: 1})
    engine, repository, executor, clock = await start(tmp_path, monkeypatch, http, attested=False)
    gated = []
    issuance = repository.quota_issuance

    async def recorded(keys, artifact_id, now, **kwargs):
        blocked = await issuance(keys, artifact_id, now, **kwargs)
        gated.append((artifact_id, blocked is not None))
        return blocked
    monkeypatch.setattr(repository, "quota_issuance", recorded)
    first = await engine.submit((TransferRequest("https", HERE, "movie.bin"),), name="movie")
    await engine.tick()
    artifact = await only_artifact(repository, first.id)
    [hold] = await holds()
    requalify_at = hold["requalify_at"]
    assert http.asked(HERE, refused=True) == 1

    late = await engine.submit((TransferRequest("https", THERE, "movie.bin"),), name="movie-elsewhere")
    while clock() < requalify_at - 15:
        await cycle(engine, clock, 15.0)
    samples = [blocked for artifact_id, blocked in gated if artifact_id is None]
    assert len(samples) >= 3 and all(samples)          # sampled repeatedly, every one held back
    assert http.asked() == 2                            # resolution + the one refusal: no sample asked
    [hold] = await holds()
    assert (hold["requalify_at"], hold["refusals"]) == (requalify_at, 1)
    assert not await repository.executions(late.id)       # fail closed: no independent writer
    async with database.get_db() as db:
        row = dict(await db.fetchone("SELECT equivalence_disposition,equivalence_retry_count,retry_at "
                                     "FROM transfer_requests WHERE transfer_id=? AND parent_id IS NULL", (late.id,)))
    assert row["equivalence_disposition"] in {"pending", "unverified"}
    assert row["retry_at"] > clock() and row["equivalence_retry_count"] <= 10   # bounded, low-frequency

    # The scheduled requalification of the held file's pending work is on time:
    # one request, its refusal renewing the hold from its own answer.
    while clock() < requalify_at + 15:
        await cycle(engine, clock, 15.0)
    assert http.asked(HERE, refused=True) == 2 and http.asked() == 3
    [hold] = await holds()
    assert hold["refusals"] == 2 and hold["requalify_at"] > requalify_at
    assert all(artifact_id == artifact.id for artifact_id, blocked in gated if not blocked)

    # Bounded liveness of the late source: the quota comes back, the held
    # file's next requalification clears the hold and starts its writer, and
    # the late source's next scheduled reconsideration (low-frequency, capped,
    # ``transfers.cohorts``) samples once and joins that file -- proven, not
    # assumed; never a second writer.
    http.recovered = True
    late_request = None
    while clock() < requalify_at + 2 * INTERVAL + 3600:
        await cycle(engine, clock, 15.0)
        async with database.get_db() as db:
            late_request = dict(await db.fetchone(
                "SELECT equivalence_disposition FROM transfer_requests WHERE transfer_id=? AND parent_id IS NULL",
                (late.id,)))
        if late_request["equivalence_disposition"] not in {"pending", "unverified"}:
            break
    assert late_request["equivalence_disposition"] == "recovered"
    assert http.asked(refused=True) == 2 and http.asked() == 5      # the clearing probe + one sample
    assert [item.candidate.provider_id for item in await repository.executions(first.id)] == ["debridlink"]
    assert not await repository.executions(late.id)
