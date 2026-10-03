"""Explicit alternative-source groups and productive-acquisition admission.

A Quick Add row of TAB-separated links is ONE logical member whose sources the
operator declared interchangeable, preferred left to right; a new line is
another member. Grouping is submission intent -- never equivalence evidence,
never inferred from a paste, a name, a size or a host -- and it is not
collection route ownership. Core keeps at most one alternative of a group
selected at a time (the others are dormant ``skipped`` roots), prefers one the
provider already holds when the provider can say so without creating anything
(``CachedResolution``), and hands the group to the next alternative in
submitted order only when the selected one terminally fails. Ordinary,
ungrouped input never enters any of this.

Every provider here is a neutral fixture; no concrete integration is named.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess

import pytest

import db.database as database
from fake_integrations import MemoryExecutor
from transfers.applicability import ApplicabilityReadiness, HostClaim, HostClaimScope, ProviderApplicability
from transfers.contracts import CachedResolution
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Permanence, Retryability, Stage, TransferError
from transfers.models import (
    CachePresence, Capability, Endpoint, IntegrationDescriptor, OutcomeKind, Ownership, ProviderObservation,
    ProviderResource, ResolutionResult, ResourceState, TransferCandidate, TransferOutcome, TransferRequest,
    TransferState,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.recovery_repository import TransferRepository
from transfers.requests import direct_link_rows

from test_file_selection_api import api  # noqa: F401  (shared fixture)

pytestmark = pytest.mark.asyncio

NOW = 1_000.0
HOST = "mirror.example"
A, B, C = (f"https://{HOST}/{name}/part1.rar" for name in ("a", "b", "c"))
# A source's own terminal failure (request-scoped: it never exhausts a provider).
SOURCE_GONE = NormalizedError(Domain.RESOLUTION, Category.SOURCE_NOT_FOUND, Stage.RESOLUTION, Retryability.NEVER,
                              origin=Origin.REMOTE_SOURCE, permanence=Permanence.PERMANENT)
# A provider-final failure of one root's route: that provider is exhausted for
# THAT request only.
PROVIDER_FINAL = NormalizedError(Domain.PROVIDER, Category.ACCOUNT_LIMITED, Stage.RESOLUTION, Retryability.NEVER,
                                 origin=Origin.PROVIDER, permanence=Permanence.PERMANENT)


class AlternativeLab:
    """A neutral specialized provider of one host. Its ordinary ``resolve`` is
    PRODUCTIVE: every call creates one remote object (recorded in
    ``created``). ``hold`` keeps a created object preparing remotely;
    otherwise resolution yields one executable candidate for the link."""

    def __init__(self, identity="alt-lab", *, hold=False):
        self.descriptor = IntegrationDescriptor(
            identity, identity,
            frozenset({Capability.RESOLVE, Capability.RESOURCE_CREATION, Capability.RESOURCE_LOOKUP,
                       Capability.CLEANUP}),
            request_types=frozenset({"https"}))
        self.applicability = ProviderApplicability(
            specialized_hosts=(HostClaim(HOST, HostClaimScope.DOMAIN, frozenset({"https"})),),
            specialized=True, readiness=ApplicabilityReadiness.READY)
        self.hold = hold
        self.created: list[str] = []
        self.failing: dict[str, NormalizedError] = {}
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    def _resolved(self, payload, *, cached=False):
        if self.hold:
            resource = ProviderResource(self.descriptor.id, {"ticket": payload}, Ownership.CREATED,
                                        id=f"{self.descriptor.id}:{payload}")
            return ResolutionResult(ResourceState.PREPARING,
                                    observation=ProviderObservation(resource, ResourceState.PREPARING, "remote"))
        return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
            "part1.rar", (Endpoint("memory", f"memory:{payload}"),), expected_bytes=4,
            provider_id=self.descriptor.id),))

    async def resolve(self, request):
        payload = str(request.payload)
        if self.entered is not None:
            self.entered.set()
            await self.release.wait()
        self.created.append(payload)
        if payload in self.failing:
            raise TransferError(replace(self.failing[payload], integration_id=self.descriptor.id))
        return self._resolved(payload)

    async def observe(self, resource):
        return ProviderObservation(resource, ResourceState.PREPARING, "remote")

    async def cleanup(self, directive):
        return TransferOutcome(OutcomeKind.SUCCESS)


class CachedAlternativeLab(AlternativeLab):
    """The same provider, also able to say -- creating nothing -- which links
    it already holds, and to resolve a held link without acquiring it."""

    def __init__(self, identity="alt-lab", **options):
        super().__init__(identity, **options)
        self.cache: set[str] = set()
        self.asked: list[tuple[str, ...]] = []
        self.from_cache: list[str] = []
        self.stale: set[str] = set()

    async def cache_presence(self, requests):
        self.asked.append(tuple(str(request.payload) for request in requests))
        return tuple(CachePresence.HIT if str(request.payload) in self.cache else CachePresence.MISS
                     for request in requests)

    async def resolve_cached(self, request):
        payload = str(request.payload)
        if payload not in self.cache or payload in self.stale:
            return None
        self.from_cache.append(payload)
        return self._resolved(payload, cached=True)


async def lab(tmp_path, monkeypatch, *providers, fresh=True):
    if fresh:
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "groups.sqlite3")
        await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    for provider in providers:
        registry.register_provider(provider)
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "downloads"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3, resource_poll_interval=0),
                            clock=lambda: NOW)
    await engine.initialize()
    return repository, engine, executor


async def submit(engine, rows):
    """Submit ``rows`` exactly as ``ApplicationService.submit_links`` builds
    them: a tuple is one explicit group, a plain link an ordinary root."""
    links = [link for row in rows for link in ((row,) if isinstance(row, str) else row)]
    groups = tuple(index if not isinstance(row, str) and len(row) > 1 else None
                   for index, row in enumerate(rows, 1) for _ in ((row,) if isinstance(row, str) else row))
    options = {"alternative_groups": groups} if any(group is not None for group in groups) else {}
    return await engine.submit(tuple(TransferRequest("https", link, name="part1.rar") for link in links),
                               name="mirrors", deduplicate=False, **options)


async def roots(repository, transfer_id):
    return {str(item.request.payload): item for item in await repository.requests(transfer_id)
            if item.parent_id is None}


async def attempts(transfer_id):
    async with database.get_db() as db:
        rows = await db.fetchall(
            """SELECT r.payload,a.state FROM resolution_attempts a JOIN transfer_requests r ON r.id=a.request_id
               WHERE r.transfer_id=? ORDER BY a.rowid""", (transfer_id,))
    return [(json.loads(row["payload"])["payload"], row["state"]) for row in rows]


async def drive(engine, passes=6):
    for _ in range(passes):
        await engine.resolve_pending()


async def settle(engine, executor, passes=8):
    """Resolution, execution and completion until quiescent."""
    for _ in range(passes):
        await engine.tick()
        for attempt_id, job in list(executor.jobs.items()):
            if job.state.value == "running":
                executor.finish(job.handle)


def states(records):
    return {payload: record.state for payload, record in records.items()}


# -- the text grammar -------------------------------------------------------------

def test_a_single_link_and_new_lines_are_ordinary_rows():
    assert direct_link_rows([A]) == [(A,)]
    assert direct_link_rows([A, B, C]) == [(A,), (B,), (C,)]


def test_tab_separates_alternatives_of_one_row_in_submitted_order():
    assert direct_link_rows([f"{A}\t{B}\t{C}"]) == [(A, B, C)]
    assert direct_link_rows([f"{C}\t{A}\t{B}"]) == [(C, A, B)]  # never reordered


def test_mixed_rows_keep_their_sizes():
    d, e, f = (f"https://{HOST}/{name}/x" for name in "def")
    rows = direct_link_rows([f"{A}\t{B}", C, f"{d}\t{e}\t{f}"])
    assert [len(row) for row in rows] == [2, 1, 3]


@pytest.mark.parametrize("row, expected", [
    (f"{A}\t{B}\t", (A, B)),                 # trailing tab
    (f"{A}\t\t\t{B}", (A, B)),               # consecutive tabs / empty cells
    (f"\t{A}\t \t{B}", (A, B)),              # leading tab, whitespace-only cell
    (f"{A}\t{A}\t{B}", (A, B)),              # the same link twice in one row
    (f"{A}\t{A}", (A,)),                     # ... which leaves one ordinary link
])
def test_malformed_rows_are_normalized_never_guessed(row, expected):
    assert direct_link_rows([row]) == [expected]


def test_spaces_are_never_a_delimiter():
    spaced = f"{A} {B}"
    assert direct_link_rows([spaced]) == [(spaced,)]


def test_a_link_in_two_rows_is_refused_and_an_identical_row_is_one_row():
    with pytest.raises(ValueError, match="only one row"):
        direct_link_rows([f"{A}\t{B}", f"{B}\t{C}"])
    with pytest.raises(ValueError, match="only one row"):
        direct_link_rows([f"{A}\t{B}", A])
    assert direct_link_rows([f"{A}\t{B}", f"{A}\t{B}"]) == [(A, B)]


# -- the submission surfaces --------------------------------------------------------

async def _rows(transfer_id):
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT payload,state,alternative_group,ordinal FROM transfer_requests "
                                 "WHERE transfer_id=? AND parent_id IS NULL ORDER BY ordinal", (transfer_id,))
    return [(json.loads(row["payload"])["payload"], row["state"], row["alternative_group"]) for row in rows]


async def test_text_without_a_tab_is_submitted_exactly_as_before(api):
    response = await api.client.post("/api/links/add", json={"links": f"{A}\n{B}\n{C}\n"})
    assert response.status_code == 200
    assert await _rows(response.json()["id"]) == [(A, "pending", None), (B, "pending", None), (C, "pending", None)]


async def test_a_tab_row_is_one_group_with_one_selected_alternative(api):
    d = f"https://{HOST}/d/other.bin"
    response = await api.client.post("/api/links/add", json={"links": f"{A}\t{B}\t{C}\n{d}\n"})
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 4
    assert await _rows(body["id"]) == [(A, "pending", 1), (B, "skipped", 1), (C, "skipped", 1), (d, "pending", None)]
    # Two members are named, not four links.
    assert body["name"] == "part1.rar + 1 more"


async def test_a_list_submission_uses_the_same_row_grammar(api):
    response = await api.client.post("/api/links/add", json={"links": [f"{A}\t{B}", C]})
    assert await _rows(response.json()["id"]) == [(A, "pending", 1), (B, "skipped", 1), (C, "pending", None)]


async def test_one_invalid_link_among_valid_alternatives_admits_nothing(api):
    response = await api.client.post("/api/links/add", json={"links": f"{A}\tnot-a-link\t{B}"})
    assert response.status_code == 400
    async with database.get_db() as db:
        assert (await db.fetchone("SELECT COUNT(*) AS n FROM transfer_requests"))["n"] == 0


async def test_a_link_file_with_a_tab_bearing_value_is_never_grouped(api):
    # Baseline: a header-ed list yields one value containing a literal TAB;
    # the file grammar owns it and it stays ONE ordinary link, as before.
    document = f"url\n{A}\t{B}\n".encode()
    response = await api.client.post("/api/links/add-file",
                                     files={"file": ("links.txt", document, "text/plain")})
    assert response.status_code == 200
    transfer_id = response.json()["items"][0]["id"]
    assert await _rows(transfer_id) == [(f"{A}\t{B}", "pending", None)]


def test_the_browser_keeps_tab_rows_intact_and_never_intercepts_the_tab_key():
    root = Path(__file__).resolve().parents[2] / "frontend" / "static"
    script = (root / "app.js").read_text()
    html = (root / "index.html").read_text()
    textarea = html[html.index('id="q-transfer-input"'):]
    textarea = textarea[:textarea.index(">")]
    assert 'title="Paste alternate sources for one item separated by tabs; use new lines for separate items."' in textarea
    assert "Tab" not in textarea  # no keydown handling of the Tab key
    start = script.index("function classifyDashboardEntries(raw)")
    source = script[start:script.index("\n}\n", start) + 2]
    node = shutil.which("node")
    assert node, "node is required by the contract layer"
    probe = source + f"""
const r = classifyDashboardEntries({json.dumps(f"{A}\t\t{B}\t\n{C}\n{A}\tmagnet:?xt=urn:btih:x\n  {C}  \n")});
console.log(JSON.stringify(r));"""
    result = json.loads(subprocess.run([node, "-e", probe], capture_output=True, text=True, check=True).stdout)
    assert [entry["value"] for entry in result["direct"]] == [f"{A}\t{B}", C]
    assert [entry["line"] for entry in result["invalid"]] == [3]
    assert result["magnets"] == []


# -- admission ------------------------------------------------------------------------

async def test_ungrouped_requests_never_enter_the_group_path(tmp_path, monkeypatch):
    provider = CachedAlternativeLab()
    provider.cache.add(B)
    repository, engine, _executor = await lab(tmp_path, monkeypatch, provider)
    transfer = await submit(engine, [A, B, C])
    assert {record.alternative_group for record in (await roots(repository, transfer.id)).values()} == {None}
    await drive(engine)
    # The ordinary path: every root resolved through ordinary resolve, the
    # cache was never asked, nothing went dormant or was reselected.
    assert sorted(provider.created) == sorted([A, B, C])
    assert provider.asked == [] and provider.from_cache == []
    assert "skipped" not in states(await roots(repository, transfer.id)).values()


async def test_all_uncached_admits_exactly_one_productive_alternative(tmp_path, monkeypatch):
    provider = CachedAlternativeLab(hold=True)
    repository, engine, _executor = await lab(tmp_path, monkeypatch, provider)
    transfer = await submit(engine, [(A, B, C)])
    assert provider.created == []  # nothing remote before admission
    await drive(engine)
    assert provider.created == [A]  # one productive acquisition, the first in submitted order
    assert provider.asked == [(A, B, C)]  # the cache was asked once, nothing created by asking
    assert states(await roots(repository, transfer.id)) == {A: "waiting", B: "skipped", C: "skipped"}


async def test_a_held_alternative_is_preferred_and_misses_are_never_created(tmp_path, monkeypatch):
    provider = CachedAlternativeLab()
    provider.cache.add(B)
    repository, engine, executor = await lab(tmp_path, monkeypatch, provider)
    transfer = await submit(engine, [(A, B, C)])
    await settle(engine, executor)
    assert provider.from_cache == [B] and provider.created == []
    assert states(await roots(repository, transfer.id)) == {A: "skipped", B: "resolved", C: "skipped"}
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert len(await repository.artifacts(transfer.id)) == 1  # one member, one writer


async def test_several_held_alternatives_instantiate_only_the_first(tmp_path, monkeypatch):
    provider = CachedAlternativeLab()
    provider.cache.update({B, C})
    repository, engine, executor = await lab(tmp_path, monkeypatch, provider)
    transfer = await submit(engine, [(A, B, C)])
    await settle(engine, executor)
    assert provider.from_cache == [B] and provider.created == []
    assert len(await repository.artifacts(transfer.id)) == 1


async def test_a_stale_cache_answer_creates_nothing_and_returns_to_submitted_order(tmp_path, monkeypatch):
    provider = CachedAlternativeLab(hold=True)
    provider.cache.add(C)
    provider.stale.add(C)  # held when asked, gone when resolved
    repository, engine, _executor = await lab(tmp_path, monkeypatch, provider)
    transfer = await submit(engine, [(A, B, C)])
    await drive(engine)
    # C was tried from the cache only, found nothing and created nothing; the
    # productive acquisition is the first alternative in submitted order.
    assert provider.from_cache == [] and provider.created == [A]
    assert states(await roots(repository, transfer.id)) == {A: "waiting", B: "skipped", C: "skipped"}
    assert ("https://mirror.example/c/part1.rar", "failed") in await attempts(transfer.id)


async def test_terminal_failure_advances_left_to_right_one_at_a_time(tmp_path, monkeypatch):
    provider = CachedAlternativeLab(hold=True)
    provider.failing.update({A: SOURCE_GONE, B: SOURCE_GONE})
    repository, engine, _executor = await lab(tmp_path, monkeypatch, provider)
    transfer = await submit(engine, [(A, B, C)])
    for _ in range(3):
        await engine.resolve_pending()
        current = states(await roots(repository, transfer.id))
        # Never more than one selected alternative.
        assert sum(state not in {"skipped", "failed"} for state in current.values()) <= 1
    await drive(engine)
    assert provider.created == [A, B, C]  # strictly in submitted order, one at a time
    assert states(await roots(repository, transfer.id)) == {A: "failed", B: "failed", C: "waiting"}


async def test_one_root_s_provider_exhaustion_never_exhausts_its_siblings(tmp_path, monkeypatch):
    provider = AlternativeLab(hold=True)
    provider.failing[A] = PROVIDER_FINAL
    repository, engine, _executor = await lab(tmp_path, monkeypatch, provider)
    transfer = await submit(engine, [(A, B, C)])
    await drive(engine)
    records = await roots(repository, transfer.id)
    assert states(records) == {A: "failed", B: "waiting", C: "skipped"}
    assert provider.created == [A, B]
    assert await repository.exhausted_route_providers(records[A].id) == frozenset({provider.descriptor.id})
    assert await repository.exhausted_route_providers(records[B].id) == frozenset()


async def test_a_provider_wide_outage_stays_authoritative_for_every_alternative(tmp_path, monkeypatch):
    provider = AlternativeLab(hold=True)
    repository, engine, _executor = await lab(tmp_path, monkeypatch, provider)
    grouped = await submit(engine, [(A, B, C)])
    ordinary = await submit(engine, [f"https://{HOST}/o/part1.rar"])
    provider.descriptor = replace(provider.descriptor, enabled=False)  # the provider itself is unavailable
    await drive(engine)
    # Nothing is acquired through an unavailable provider, and each
    # alternative meets exactly the existing provider-wide outcome an
    # ordinary root meets -- the group neither weakens nor hides it.
    assert provider.created == []
    expected = next(iter((await roots(repository, ordinary.id)).values()))
    assert (expected.state, expected.error.category) == ("failed", Category.UNSUPPORTED_REQUEST)
    for record in (await roots(repository, grouped.id)).values():
        assert (record.state, record.error.category) == (expected.state, expected.error.category)


async def test_a_satisfied_group_settles_and_records_its_unattempted_alternatives(tmp_path, monkeypatch):
    provider = CachedAlternativeLab()
    repository, engine, executor = await lab(tmp_path, monkeypatch, provider)
    transfer = await submit(engine, [(A, B, C)])
    await settle(engine, executor)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    records = await roots(repository, transfer.id)
    assert states(records) == {A: "resolved", B: "skipped", C: "skipped"}
    # B and C are still durable, visible roots of the group -- never attempted,
    # never failed, carrying no error or evidence of any kind.
    for payload in (B, C):
        assert records[payload].alternative_group == 1
        assert records[payload].attempts == 0 and records[payload].error is None
    assert [payload for payload, _state in await attempts(transfer.id)] == [A]
    async with database.get_db() as db:
        dispositions = await db.fetchall("SELECT equivalence_disposition AS d FROM transfer_requests "
                                         "WHERE transfer_id=? AND parent_id IS NULL", (transfer.id,))
    assert {row["d"] for row in dispositions} == {""}


async def test_no_requeue_ever_admits_an_unattempted_alternative_of_a_satisfied_group(tmp_path, monkeypatch):
    provider = CachedAlternativeLab()
    provider.failing[A] = SOURCE_GONE
    repository, engine, executor = await lab(tmp_path, monkeypatch, provider)
    transfer = await submit(engine, [(A, B, C), f"https://{HOST}/z/other.bin"])
    other = f"https://{HOST}/z/other.bin"
    provider.failing[other] = SOURCE_GONE
    await settle(engine, executor)
    # B satisfied group 1 after A failed; the other member failed, so the
    # transfer failed and every requeue path is available.
    assert states(await roots(repository, transfer.id)) == {A: "failed", B: "resolved", C: "skipped", other: "failed"}
    for requeue in (lambda: repository.retry_requests(transfer.id),
                    lambda: repository.retry_requests(transfer.id, reset_budget=True),
                    lambda: engine.resume(transfer.id)):
        await requeue()
        current = states(await roots(repository, transfer.id))
        assert current[C] == "skipped" and current[A] in {"skipped", "failed"}, current
    # Startup reconciliation: a fresh engine over the same durable state.
    _repository, restarted, _ = await lab(tmp_path, monkeypatch, provider, fresh=False)
    del provider.failing[other]
    await settle(restarted, executor)
    assert provider.created.count(C) == 0 and provider.created.count(A) == 1
    assert states(await roots(repository, transfer.id))[C] == "skipped"


async def test_a_requeued_failed_group_selects_only_its_first_alternative(tmp_path, monkeypatch):
    provider = AlternativeLab(hold=True)
    provider.failing.update({A: SOURCE_GONE, B: SOURCE_GONE, C: SOURCE_GONE})
    repository, engine, _executor = await lab(tmp_path, monkeypatch, provider)
    transfer = await submit(engine, [(A, B, C)])
    await drive(engine)
    assert states(await roots(repository, transfer.id)) == {A: "failed", B: "failed", C: "failed"}
    provider.failing.clear()
    await repository.retry_requests(transfer.id, reset_budget=True)
    assert states(await roots(repository, transfer.id)) == {A: "pending", B: "skipped", C: "skipped"}
    await drive(engine)
    assert provider.created[3:] == [A]


async def test_restart_before_creation_keeps_one_selected_alternative(tmp_path, monkeypatch):
    provider = AlternativeLab(hold=True)
    repository, engine, _executor = await lab(tmp_path, monkeypatch, provider)
    transfer = await submit(engine, [(A, B, C)])
    _repository, restarted, _ = await lab(tmp_path, monkeypatch, provider, fresh=False)
    assert states(await roots(repository, transfer.id)) == {A: "pending", B: "skipped", C: "skipped"}
    await drive(restarted)
    assert provider.created == [A]


async def test_restart_after_creation_observes_the_object_and_never_creates_again(tmp_path, monkeypatch):
    provider = AlternativeLab(hold=True)
    repository, engine, _executor = await lab(tmp_path, monkeypatch, provider)
    transfer = await submit(engine, [(A, B, C)])
    await drive(engine)
    assert provider.created == [A]
    _repository, restarted, _ = await lab(tmp_path, monkeypatch, provider, fresh=False)
    await drive(restarted)
    assert provider.created == [A]
    assert states(await roots(repository, transfer.id)) == {A: "waiting", B: "skipped", C: "skipped"}


async def test_an_interrupted_resolution_never_starts_another_alternative(tmp_path, monkeypatch):
    # The process died after asking the provider to create A and before the
    # answer was recorded: whether A exists remotely is unknown, so startup
    # reconciliation fails A without handing the group to B.
    provider = AlternativeLab(hold=True)
    repository, engine, _executor = await lab(tmp_path, monkeypatch, provider)
    transfer = await submit(engine, [(A, B, C)])
    record = (await roots(repository, transfer.id))[A]
    assert await repository.begin_resolution(record.id, provider.descriptor.id) is not None
    _repository, restarted, _ = await lab(tmp_path, monkeypatch, provider, fresh=False)
    await drive(restarted)
    assert provider.created == []
    assert states(await roots(repository, transfer.id)) == {A: "failed", B: "skipped", C: "skipped"}


async def test_concurrent_scheduler_passes_create_exactly_once(tmp_path, monkeypatch):
    provider = AlternativeLab(hold=True)
    provider.entered, provider.release = asyncio.Event(), asyncio.Event()
    repository, first, _executor = await lab(tmp_path, monkeypatch, provider)
    await submit(first, [(A, B, C)])
    _repository, second, _ = await lab(tmp_path, monkeypatch, provider, fresh=False)
    passes = [asyncio.create_task(engine.resolve_pending()) for engine in (first, second, first, second)]
    await provider.entered.wait()
    await asyncio.sleep(0.05)
    provider.release.set()
    await asyncio.gather(*passes)
    assert provider.created == [A]


async def test_five_groups_of_four_admit_one_acquisition_per_group(tmp_path, monkeypatch):
    provider = CachedAlternativeLab(hold=True)
    repository, engine, _executor = await lab(tmp_path, monkeypatch, provider)
    rows = [tuple(f"https://{HOST}/{host}/part{part}.rar" for host in "abcd") for part in range(1, 6)]
    transfer = await submit(engine, rows)
    await drive(engine)
    # One productive acquisition per member, each its group's first source;
    # sharing a provider and a host never collapses two members together.
    assert sorted(provider.created) == sorted(row[0] for row in rows)
    records = await repository.requests(transfer.id)
    groups = {record.alternative_group for record in records}
    assert groups == {1, 2, 3, 4, 5}
    for group in groups:
        selected = [record for record in records if record.alternative_group == group and record.state != "skipped"]
        assert len(selected) == 1


async def test_the_seam_is_a_neutral_optional_protocol():
    assert isinstance(CachedAlternativeLab(), CachedResolution)
    assert not isinstance(AlternativeLab(), CachedResolution)


# -- negative evidence control (deterministic) -------------------------------------
#
# No live cached negative object was available, so discrimination is proven
# here through the SAME bounded range sampler the positive live control used
# (``services.artifact_sampling``): equal-sized X and Y must not collapse.

async def test_the_bounded_sampler_distinguishes_different_material_of_equal_size(monkeypatch):
    import socket
    from urllib.parse import urlsplit

    from aiohttp import web

    import services.artifact_sampling as sampling
    import services.network_safety as safety
    from transfers.models import FingerprintKind

    size = 24 * 1024
    material = {"/x": bytes(index % 251 for index in range(size)),
                "/y": bytes((index * 7 + 3) % 253 for index in range(size)),
                "/x-again": bytes(index % 251 for index in range(size))}

    async def handler(request):
        payload = material[request.path]
        start_text, end_text = request.headers["Range"][6:].split("-", 1)
        start, end = int(start_text), min(int(end_text), size - 1)
        return web.Response(status=206, body=payload[start:end + 1],
                            headers={"Content-Range": f"bytes {start}-{end}/{size}"})

    app = web.Application()
    app.router.add_route("GET", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    async def allow(uri):
        if urlsplit(uri).hostname != "fixture.example":
            raise safety.UnsafeDestinationError("fixture escape")
        return uri

    async def local(self, host, port=0, family=socket.AF_UNSPEC):
        return [{"hostname": host, "host": "127.0.0.1", "port": port, "family": socket.AF_INET,
                 "proto": socket.IPPROTO_TCP, "flags": socket.AI_NUMERICHOST}]

    monkeypatch.setattr(safety, "validate_resolved_public_destination", allow)
    monkeypatch.setattr(safety.PublicDestinationResolver, "resolve", local)
    try:
        vectors = {}
        for path in material:
            total, signature, kind, _reason, prefix = await sampling.sampled_public_artifact_fingerprint(
                f"http://fixture.example:{port}{path}", sample_bytes=4096, expected_bytes=size)
            assert (total, kind) == (size, FingerprintKind.FULL_CONTENT_SAMPLE)
            vectors[path] = (signature, prefix)
    finally:
        await runner.cleanup()
    assert vectors["/x"] == vectors["/x-again"]  # the positive control still holds
    assert vectors["/x"][0] != vectors["/y"][0] and vectors["/x"][1] != vectors["/y"][1]
