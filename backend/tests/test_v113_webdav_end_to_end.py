"""DP 1.0.13 WebDAV through the real engine: a boring protocol edge.

Real engine, real ``general_webdav`` and ``general_http`` providers, the real
core-run discovery reaching the real executor-side WebDAV reader
(``Aria2Executor.discover`` -> ``services.artifact_sampling.webdav_discovery``)
against a deterministic in-process origin. Only byte acquisition is an
in-memory fake that claims ``http``/``https`` endpoints -- so routing,
authentication input, the universal selector, dispatch, recovery and
presentation are exactly the machinery every other source meets.
"""
from __future__ import annotations

from urllib.parse import unquote, urlsplit

import pytest
import pytest_asyncio

from db.database import get_db
from test_v113_transfer_auth_context import CountingVault, lab  # noqa: F401
from test_v113_transport_evidence_sampling import executor_for, guard_for, loopback  # noqa: F401
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
from transfers.models import DiscoveryDepth, ExecutorCapabilities, IntegrationDescriptor, TransferRequest, TransferState
from webdav_origin import WebDavOrigin

pytestmark = pytest.mark.asyncio

USER, PASSWORD = "vault-user-sentinel", "vault-password-sentinel"  # the lab's own credential sentinels
TREE = {"/dav/": None, "/dav/a.txt": b"four", "/dav/b.txt": b"four", "/dav/sub/": None, "/dav/sub/c.txt": b"four",
        "/plain/": None, "/plain/x.bin": b"four"}


class HttpMemory(CountingVault):
    """In-memory byte acquisition for ``http``/``https`` endpoints; discovery is
    the REAL executor-side reader, reached through core exactly as in production."""

    descriptor = IntegrationDescriptor("http-memory", "HTTP memory", frozenset())
    capabilities = ExecutorCapabilities(candidate_sampling=True, per_execution_pause=True, transient_input=True,
                                        remote_discovery=True)
    claim_schemes = frozenset({"http", "https"})

    def __init__(self, authorize, *, reader, **kwargs):
        super().__init__(authorize, **kwargs)
        self.reader = reader
        self.offered: list[tuple[str, str]] = []  # (host, username) of every input-bearing sample

    async def fingerprint_with_input(self, subject, submitted):
        from transfers.models import InputField
        self.offered.append((urlsplit(subject.candidate.endpoints[0].address).hostname,
                             submitted.value(InputField.USERNAME)))
        return await super().fingerprint_with_input(subject, submitted)

    @staticmethod
    def _object(candidate):
        parts = urlsplit(candidate.endpoints[0].address)
        return f"{parts.hostname}{unquote(parts.path)}"

    async def discover(self, subject, submitted=None, *, depth=DiscoveryDepth.CURRENT):
        return await self.reader.discover(subject, submitted, depth=depth)


@pytest_asyncio.fixture
async def stage(lab, loopback, tmp_path):  # noqa: F811
    from providers.general_http.provider import GeneralHttpProvider
    from providers.general_webdav.provider import GeneralWebdavProvider
    repository, registry, engine, *_rest, now = lab
    origin = await WebDavOrigin(TREE).start()
    host = "dav.test"
    objects = {f"{host}{path}": data for path, data in TREE.items() if data is not None}
    objects[f"{host}/plain/"] = b"four"
    registry.register_provider(GeneralHttpProvider())
    webdav = GeneralWebdavProvider()
    registry.register_provider(webdav)
    executor = HttpMemory(repository.authorize_execution, reader=executor_for(tmp_path, guard_for()), objects=objects)
    registry.register_executor(executor)
    yield repository, engine, executor, webdav, origin, now
    await origin.close()


async def _ticks(engine, now, count=24, step=5.0, *, until=None, repository=None, transfer_id=None):
    for _ in range(count):
        now[0] += step
        await engine.tick()
        if transfer_id is not None and (await repository.get(transfer_id)).state in {
                TransferState.COMPLETED, TransferState.FAILED}:
            return


async def _children(repository, transfer_id):
    return sorted(record.request.payload for record in await repository.requests(transfer_id) if record.parent_id)


async def _routes(transfer_id):
    async with get_db() as db:
        rows = await db.fetchall("""SELECT r.parent_id IS NULL AS root, a.provider_id, p.outcome
            FROM route_attempt_provenance p JOIN resolution_attempts a ON a.id=p.resolution_attempt_id
            JOIN transfer_requests r ON r.id=p.request_id WHERE p.transfer_id=? ORDER BY p.ordinal""", (transfer_id,))
    return [(bool(row["root"]), row["provider_id"], row["outcome"]) for row in rows]


def _submit(engine, url, **kwargs):
    return engine.submit((TransferRequest(url.split(":", 1)[0], url, **kwargs),), deduplicate=False)


# ── collections through the existing universal selector ───────────────────────

async def test_an_interactive_collection_materializes_only_the_chosen_members(stage):
    repository, engine, executor, _webdav, origin, now = stage
    transfer = await _submit(engine, origin.url("/dav/", scheme="webdav"), selection_mode="interactive")
    await _ticks(engine, now, 6)
    assert await _children(repository, transfer.id) == [] and executor.calls == []
    view = await repository.file_selection_presentation(transfer.id, now=now[0])
    assert sorted(entry["name"] for entry in view["entries"]) == ["a.txt", "b.txt"]  # current directory only
    chosen = [entry["entry_id"] for entry in view["entries"] if entry["name"] == "b.txt"]
    assert (await repository.confirm_file_selection(transfer.id, view["manifest_id"], chosen,
                                                    now=now[0])).decision == "explicit"
    await _ticks(engine, now, repository=repository, transfer_id=transfer.id)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert await _children(repository, transfer.id) == [origin.url("/dav/b.txt")]
    # Root owned by WebDAV; the child is an ordinary HTTP request routed as one.
    routes = await _routes(transfer.id)
    assert {provider for root, provider, _outcome in routes if root} == {"general_webdav"}
    assert {provider for root, provider, _outcome in routes if not root} == {"general_http"}
    # The collection was listed once, by PROPFIND only; nothing re-lists it.
    assert [(method, path, depth) for method, path, depth, _auth in origin.requests] == [("PROPFIND", "/dav/", "1")]


async def test_selection_all_materializes_every_discovered_member(stage):
    repository, engine, _executor, _webdav, origin, now = stage
    transfer = await _submit(engine, origin.url("/dav/", scheme="dav"))
    await _ticks(engine, now, repository=repository, transfer_id=transfer.id)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert await _children(repository, transfer.id) == [origin.url("/dav/a.txt"), origin.url("/dav/b.txt")]


async def test_a_configured_depth_reaches_subdirectories_through_the_same_selector(stage):
    repository, engine, _executor, webdav, origin, now = stage
    webdav.depth = DiscoveryDepth.UNLIMITED
    transfer = await _submit(engine, origin.url("/dav/", scheme="webdav"))
    await _ticks(engine, now, repository=repository, transfer_id=transfer.id)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert await _children(repository, transfer.id) == [
        origin.url("/dav/a.txt"), origin.url("/dav/b.txt"), origin.url("/dav/sub/c.txt")]
    async with get_db() as db:
        rows = await db.fetchall("SELECT payload FROM transfer_requests WHERE transfer_id=? AND parent_id IS NOT NULL",
                                 (transfer.id,))
    # Depth is enumeration policy only: no child request carries it.
    assert all("depth" not in row["payload"].casefold() and "webdav" not in row["payload"].casefold()
               for row in rows)


# ── a single file is one ordinary candidate owned by WebDAV ───────────────────

async def test_a_single_file_dispatches_normally_under_webdav(stage):
    repository, engine, executor, _webdav, origin, now = stage
    transfer = await _submit(engine, origin.url("/dav/a.txt", scheme="webdav"), selection_mode="interactive")
    await _ticks(engine, now, repository=repository, transfer_id=transfer.id)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert await _children(repository, transfer.id) == []
    assert [(root, provider) for root, provider, _outcome in await _routes(transfer.id)] == [(True, "general_webdav")]
    details = await repository.presentation(transfer.id, details=True)
    assert details["origin_provider_id"] == details["delivering_provider_id"] == "general_webdav"


# ── authentication: asked once, reused within the authority, never beyond ─────

async def test_a_protected_collection_asks_once_and_every_member_reuses_the_answer(stage):
    repository, engine, executor, _webdav, origin, now = stage
    origin.credentials = (USER, PASSWORD)
    executor.locks = {"dav.test": (USER, PASSWORD)}
    transfer = await _submit(engine, origin.url("/dav/", scheme="webdav"))
    asked = []
    for _ in range(40):
        now[0] += 5
        await engine.tick()
        challenge = await engine.challenges.current(transfer.id)
        if challenge is not None and challenge.id not in asked:
            asked.append(challenge.id)
            await engine.submit_input(transfer.id, challenge.id, "username_password",
                                      {"username": USER, "password": PASSWORD})
        if (await repository.get(transfer.id)).state in {TransferState.COMPLETED, TransferState.FAILED}:
            break
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert len(asked) == 1
    assert len(await _children(repository, transfer.id)) == 2
    assert {user for _candidate, user in executor.input_starts} == {USER}


async def test_a_moved_collection_asks_the_new_authority_for_itself_and_completes(stage, loopback):  # noqa: F811
    """The collection moves to a mirror that asks for its OWN credentials:
    the existing INPUT_REQUIRED lifecycle asks again, for that authority, and
    every credential reaches only the authority it was answered for."""
    import base64
    from transfers.input_required import public_challenge
    repository, engine, executor, _webdav, origin, now = stage
    mirror = await WebDavOrigin({"/dav/": None, "/dav/m.txt": b"four"}).start()
    try:
        origin.credentials = (USER, PASSWORD)
        origin.redirects["/dav/"] = mirror.url("/dav/", host="mirror.test")
        mirror.credentials = ("mirror-user", "mirror-password")
        executor.objects["mirror.test/dav/m.txt"] = b"four"
        executor.locks = {"dav.test": (USER, PASSWORD), "mirror.test": ("mirror-user", "mirror-password")}
        transfer = await _submit(engine, origin.url("/dav/", scheme="webdav"))
        answers = {"": (USER, PASSWORD), "http://mirror.test": ("mirror-user", "mirror-password")}
        asked = []
        for _ in range(60):
            now[0] += 5
            await engine.tick()
            challenge = await engine.challenges.current(transfer.id)
            if challenge is not None and challenge.id not in {item.id for item in asked}:
                asked.append(challenge)
                authority = public_challenge(challenge)["authority"].rsplit(":", 1)[0] if challenge.authority else ""
                user, password = answers[authority]
                await engine.submit_input(transfer.id, challenge.id, "username_password",
                                          {"username": user, "password": password})
            if (await repository.get(transfer.id)).state in {TransferState.COMPLETED, TransferState.FAILED}:
                break
        assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
        assert [item.authority for item in asked] == ["", f"http://mirror.test:{mirror.port}"]
        assert await _children(repository, transfer.id) == [mirror.url("/dav/m.txt", host="mirror.test")]

        def basic(user, password):
            return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        # The first authority never sees the mirror's credential, and the
        # mirror never sees the first authority's.
        assert basic("mirror-user", "mirror-password") not in {auth for *_rest, auth in origin.requests}
        assert basic(USER, PASSWORD) not in {auth for *_rest, auth in mirror.requests}
        assert basic("mirror-user", "mirror-password") in {auth for *_rest, auth in mirror.requests}
        # The member's writer reused the mirror's answer: no third question,
        # and nothing of the first authority's was offered for it.
        assert {user for _candidate, user in executor.input_starts} == {"mirror-user"}
        assert ("mirror.test", USER) not in executor.offered
    finally:
        await mirror.close()


# ── routing: aliases are authoritative; plain URLs only probe with a slash ─────

async def test_a_plain_slash_url_that_is_webdav_stays_with_webdav(stage):
    repository, engine, _executor, _webdav, origin, now = stage
    transfer = await _submit(engine, origin.url("/dav/"))
    await _ticks(engine, now, repository=repository, transfer_id=transfer.id)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert [provider for root, provider, _outcome in await _routes(transfer.id) if root] == ["general_webdav"]


async def test_a_plain_slash_url_without_webdav_falls_through_to_general_http(stage):
    repository, engine, _executor, _webdav, origin, now = stage
    origin.status = 405
    transfer = await _submit(engine, origin.url("/plain/"))
    await _ticks(engine, now, repository=repository, transfer_id=transfer.id)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert await _routes(transfer.id) == [(True, "general_webdav", "declined"), (True, "general_http", "completed")]
    details = await repository.presentation(transfer.id, details=True)
    assert details["origin_provider_id"] == "general_http"


@pytest.mark.parametrize("status,category", [(403, Category.AUTHORIZATION_FAILED), (404, Category.SOURCE_NOT_FOUND),
                                             (500, Category.SOURCE_TEMPORARILY_UNAVAILABLE)])
async def test_a_plain_slash_url_failure_never_falls_through(stage, status, category):
    repository, engine, _executor, _webdav, origin, now = stage
    origin.status = status
    transfer = await _submit(engine, origin.url("/plain/"))
    await _ticks(engine, now, 4)
    assert {provider for _root, provider, _outcome in await _routes(transfer.id)} == {"general_webdav"}
    root = next(record for record in await repository.requests(transfer.id) if record.parent_id is None)
    assert root.error is not None and root.error.category == category


async def test_a_plain_url_without_a_slash_never_reaches_webdav(stage):
    repository, engine, _executor, _webdav, origin, now = stage
    transfer = await _submit(engine, origin.url("/plain/x.bin"))
    await _ticks(engine, now, repository=repository, transfer_id=transfer.id)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert [provider for _root, provider, _outcome in await _routes(transfer.id)] == ["general_http"]
    assert origin.requests == []  # no speculative probe of an ordinary file URL


async def test_an_explicit_alias_without_webdav_fails_and_never_tries_plain_http(stage):
    repository, engine, _executor, _webdav, origin, now = stage
    origin.status = 405
    transfer = await _submit(engine, origin.url("/plain/", scheme="webdav"))
    await _ticks(engine, now, 4)
    assert {provider for _root, provider, _outcome in await _routes(transfer.id)} == {"general_webdav"}
    root = next(record for record in await repository.requests(transfer.id) if record.parent_id is None)
    assert root.error is not None and root.error.category == Category.PROTOCOL_ERROR


async def test_a_plain_slash_url_with_a_query_is_general_http_and_never_probed(stage):
    repository, engine, executor, _webdav, origin, now = stage
    executor.objects["dav.test/plain/"] = b"four"
    transfer = await _submit(engine, origin.url("/plain/") + "?C=M;O=A")
    await _ticks(engine, now, repository=repository, transfer_id=transfer.id)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert [provider for _root, provider, _outcome in await _routes(transfer.id)] == ["general_http"]
    assert [method for method, *_rest in origin.requests if method == "PROPFIND"] == []


async def test_an_explicit_alias_with_a_query_fails_at_the_provider_before_any_discovery(stage):
    repository, engine, _executor, _webdav, origin, now = stage
    transfer = await _submit(engine, origin.url("/dav/", scheme="webdav") + "?view=1")
    await _ticks(engine, now, 4)
    assert {provider for _root, provider, _outcome in await _routes(transfer.id)} == {"general_webdav"}
    root = next(record for record in await repository.requests(transfer.id) if record.parent_id is None)
    assert root.error is not None and root.error.category == Category.UNSUPPORTED_REQUEST
    assert root.error.diagnostic == "query_not_supported"
    assert origin.requests == []  # never reached discovery


# ── presentation and recovery stay the existing machinery's ───────────────────

async def test_a_decomposed_collection_keeps_its_webdav_origin_with_truthful_delivery(stage):
    from types import SimpleNamespace
    from api import operational_downloads as downloads
    from integrations.catalog import definitions
    repository, engine, _executor, _webdav, origin, now = stage
    transfer = await _submit(engine, origin.url("/dav/", scheme="webdav"))
    await _ticks(engine, now, repository=repository, transfer_id=transfer.id)
    details = await repository.presentation(transfer.id, details=True)
    assert (details["origin_provider_id"], details["current_provider_id"], details["delivering_provider_id"]) == (
        "general_webdav", "general_http", "general_http")
    page = await downloads.list_operational_torrents(status=None, search=None, limit=25, offset=0,
                                                     application=SimpleNamespace(definitions=definitions, engine=None))
    item = next(row for row in page["items"] if int(row["id"]) == transfer.id)
    assert (item["origin_provider_name"], item["current_provider_name"], item["delivering_provider_name"]) == (
        "WebDAV", "HTTP(S)", "HTTP(S)")


async def test_a_failed_member_enters_the_existing_recovery(stage):
    repository, engine, executor, _webdav, origin, now = stage
    executor.start_errors = [NormalizedError(Domain.NETWORK, Category.CONNECTION_FAILED, Stage.EXECUTION,
                                             retryability=Retryability.BACKOFF)]
    transfer = await _submit(engine, origin.url("/dav/", scheme="webdav"))
    await _ticks(engine, now, 40, repository=repository, transfer_id=transfer.id)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    async with get_db() as db:
        attempts = await db.fetchall("SELECT state FROM execution_attempts e JOIN download_files f "
                                     "ON f.id=e.artifact_id WHERE f.torrent_id=?", (transfer.id,))
    assert len(attempts) == 3  # one failed attempt, retried by the ordinary policy, plus its sibling
