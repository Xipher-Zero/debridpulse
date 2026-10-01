"""DP 1.0.13 Multimeta through the real engine: decompose, then feed the
existing lifecycle.

Real engine, real ``multimeta``, ``general_http`` and ``general_ftp``
providers; a remote descriptor is read by the real executor-side HTTP(S)
reader (``Aria2Executor.discover`` -> ``services.artifact_sampling
.http_content``) from a deterministic in-process origin, through core-run
discovery. Only byte acquisition is an in-memory copier claiming ``http``,
``https`` and ``ftp`` endpoints, so routing, candidates, equivalence,
failover, verification, authentication input and presentation are exactly
the machinery every other source meets. Nothing here asserts a Multimeta
state object: there is none.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import pytest_asyncio

from db.database import get_db
from test_v113_transfer_auth_context import USER, PASSWORD, lab  # noqa: F401
from test_v113_transport_evidence_sampling import executor_for, guard_for, loopback  # noqa: F401
from test_v113_webdav_end_to_end import HttpMemory
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
from transfers.models import DiscoveryResult, RemoteObjectKind, TransferRequest, TransferState
from transfers.staged_input import StagedInputStore
from webdav_origin import WebDavOrigin

pytestmark = pytest.mark.asyncio

DONE = b"done"  # what the memory copier materializes
DONE_SHA256 = hashlib.sha256(DONE).hexdigest()


class MirrorCopier(HttpMemory):
    """Memory acquisition for HTTP(S) and FTP mirrors. A host in ``broken``
    accepts the connection and then fails the transfer as a missing source;
    an FTP path is proven a regular file without a network listing (this
    double's own transport fact); an HTTP(S) read is the real reader's."""

    claim_schemes = frozenset({"http", "https", "ftp"})

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.broken: set[str] = set()
        self.fail_first = False  # the first route to start, whichever it is, is missing
        # The first route to write, whichever it is, delivers the right size but the wrong bytes.
        self.corrupt_first = False
        self.started: list[str] = []
        self.routes: list[str] = []  # the exact address of every started writer
        self._corrupt: set[str] = set()  # attempt ids that write wrong bytes
        self.credentialed: list[str] = []  # every host an execution was offered operator input for

    async def discover(self, subject, submitted=None, **tree):
        endpoint = subject.candidate.endpoints[0]
        if endpoint.scheme == "ftp":
            return DiscoveryResult(kind=RemoteObjectKind.FILE, expected_bytes=len(DONE))
        return await self.reader.discover(subject, submitted, **tree)

    async def start(self, request, handle):
        host = urlsplit(request.work.subject.candidate.endpoints[0].address).hostname
        self.started.append(host)
        self.routes.append(request.work.subject.candidate.endpoints[0].address)
        if self.corrupt_first and len(self.started) == 1:
            self._corrupt.add(handle.attempt_id)
        if host in self.broken or (self.fail_first and len(self.started) == 1):
            self.start_errors = [NormalizedError(Domain.NETWORK, Category.SOURCE_NOT_FOUND, Stage.EXECUTION,
                                                 retryability=Retryability.NEVER)]
        return await super().start(request, handle)

    def finish(self, handle, *, materialize=True):
        super().finish(handle, materialize=materialize)
        if materialize and handle.attempt_id in self._corrupt:
            Path(self.jobs[handle.attempt_id].handle.correlation["destination"]).write_bytes(b"bad!")

    async def start_with_input(self, request, handle, submitted):
        self.credentialed.append(urlsplit(request.work.subject.candidate.endpoints[0].address).hostname)
        return await super().start_with_input(request, handle, submitted)


OBJECTS = {f"{host}/{path}": DONE for host in ("m1.test", "m2.test", "m3.test", "meta.test")
           for path in ("pub/release.iso", "final/mirror/release.iso", "a.bin", "b.bin", "only.bin")}


@pytest_asyncio.fixture
async def stage(lab, loopback, tmp_path):  # noqa: F811
    from providers.general_ftp.provider import GeneralFtpProvider
    from providers.general_http.provider import GeneralHttpProvider
    from providers.multimeta.provider import MultimetaProvider
    repository, registry, engine, *_rest, now = lab
    origin = await WebDavOrigin({}).start()
    store = StagedInputStore(str(tmp_path / "staged"))
    registry.register_provider(GeneralHttpProvider())
    registry.register_provider(GeneralFtpProvider())
    registry.register_provider(MultimetaProvider(staged_input=store))
    copier = MirrorCopier(repository.authorize_execution, reader=executor_for(tmp_path, guard_for()), objects=OBJECTS)
    registry.register_executor(copier)
    yield repository, engine, copier, origin, store, now
    await origin.close()


def meta4(*files: str) -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8"?><metalink xmlns="urn:ietf:params:xml:ns:metalink">'
            + "".join(files) + "</metalink>").encode()


def described(name: str, *urls: str, digest: str | None = DONE_SHA256, size: int | None = 4, extra: str = "") -> str:
    facts = (f"<size>{size}</size>" if size is not None else "") + (
        f'<hash type="sha-256">{digest}</hash>' if digest else "")
    return f'<file name="{name}">{facts}{extra}{"".join(urls)}</file>'


def url(address: str, priority: int | None = None) -> str:
    return f'<url{f" priority=\"{priority}\"" if priority else ""}>{address}</url>'


async def _ticks(engine, repository, transfer_id, now, count=40):
    for _ in range(count):
        now[0] += 5
        await engine.tick()
        if (await repository.get(transfer_id)).state in {TransferState.COMPLETED, TransferState.FAILED}:
            return


async def _requests(transfer_id):
    async with get_db() as db:
        return await db.fetchall("""SELECT r.id,r.parent_id,r.payload,r.state,r.error,a.provider_id
            FROM transfer_requests r LEFT JOIN resolution_attempts a ON a.request_id=r.id
            WHERE r.transfer_id=? ORDER BY r.parent_id IS NOT NULL,r.ordinal""", (transfer_id,))


async def _members(engine, repository, transfer_id):
    """The canonical (non-standby) artifacts and each one's bound providers."""
    result = {}
    for artifact in await repository.artifacts(transfer_id):
        bindings = await engine.canonical.bindings(artifact.id)
        if bindings:
            result[artifact.name] = (artifact, [item["provider_id"] for item in bindings])
    return result


# ── one remote descriptor, several ordinary routes, one canonical artifact ────

async def test_a_remote_descriptor_feeds_ordinary_routes_into_one_canonical_artifact(stage):
    repository, engine, copier, origin, _store, now = stage
    final = origin.url("/final/list.meta4", host="meta.test")
    origin.tree["/final/list.meta4"] = meta4(described(
        "release.iso", url("https://m2.test/pub/release.iso", 2), url("ftp://m1.test/pub/release.iso", 1),
        url("mirror/release.iso", 3)))
    origin.redirects["/start.meta4"] = final
    start = origin.url("/start.meta4", host="meta.test")
    transfer = await engine.submit((TransferRequest("http", start, name="start.meta4"),), deduplicate=False)
    await _ticks(engine, repository, transfer.id, now)

    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    requests = await _requests(transfer.id)
    root, *children = requests
    assert root["provider_id"] == "multimeta"
    # Every source is an ordinary request, routed by the ordinary competition:
    # FTP to the FTP provider, HTTP(S) -- the relative reference resolved
    # against where the document was finally served -- to the HTTP provider.
    assert [(TransferRequest(**_payload(item)).payload, item["provider_id"]) for item in children] == [
        ("ftp://m1.test/pub/release.iso", "general_ftp"),
        ("https://m2.test/pub/release.iso", "general_http"),
        (origin.url("/final/mirror/release.iso", host="meta.test"), "general_http"),
    ]
    # One canonical artifact owns every route; no candidate is Multimeta's.
    members = await _members(engine, repository, transfer.id)
    assert list(members) == ["release.iso"]
    artifact, providers = members["release.iso"]
    assert sorted(providers) == ["general_ftp", "general_http", "general_http"]
    assert "multimeta" not in {candidate.provider_id for candidate in artifact.candidates}
    assert {item.algorithm for candidate in artifact.candidates for item in candidate.integrity} == {"sha256"}
    # One route wrote the file; which one is the canonical cohort owner's
    # decision (the first route proven), never this provider's.
    assert len(copier.started) == 1
    # The document was read once, by GET, through the redirect.
    assert [(method, path) for method, path, _depth, _auth in origin.requests] == [
        ("GET", "/start.meta4"), ("GET", "/final/list.meta4")]
    presentation = await repository.presentation(transfer.id, details=True)
    assert presentation["origin_provider_id"] == "multimeta"
    assert presentation["current_provider_id"] in {"general_ftp", "general_http"}


def _payload(row):
    import json
    return json.loads(row["payload"])


async def test_a_failed_mirror_fails_over_through_the_existing_candidate_lifecycle(stage):
    repository, engine, copier, origin, _store, now = stage
    origin.tree["/r.meta4"] = meta4(described(
        "release.iso", url("https://m1.test/pub/release.iso", 1), url("https://m2.test/pub/release.iso", 2)))
    copier.fail_first = True
    transfer = await engine.submit((TransferRequest("http", origin.url("/r.meta4", host="meta.test")),),
                                   deduplicate=False)
    await _ticks(engine, repository, transfer.id, now)

    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    first, second = copier.started
    assert {first, second} == {"m1.test", "m2.test"}
    artifact, _providers = (await _members(engine, repository, transfer.id))["release.iso"]
    # The switch is the canonical candidate owner's: its durable attempt
    # history names both routes, and the artifact selected the other one.
    context = await repository.recovery_context(artifact.id)
    assert len(context.get("candidate_attempt_history") or ()) == 2
    assert urlsplit(artifact.candidates[artifact.selected].endpoints[0].address).hostname == second


async def test_whole_file_integrity_reaches_the_existing_verifier_and_exhaustion_is_terminal(stage):
    repository, engine, _copier, origin, _store, now = stage
    origin.tree["/bad.meta4"] = meta4(described("release.iso", url("https://m1.test/pub/release.iso"),
                                                digest="0" * 64))
    transfer = await engine.submit((TransferRequest("http", origin.url("/bad.meta4", host="meta.test")),),
                                   deduplicate=False)
    await _ticks(engine, repository, transfer.id, now)
    # The copier wrote the bytes; the declared digest does not match them, so
    # the existing material verification rejects them, and with no alternate
    # left the canonical recovery decision is terminal.
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.state == "error"
    assert artifact.candidates[artifact.selected].integrity[0].digest == "0" * 64
    context = await repository.recovery_context(artifact.id)
    assert (context["decision_action"], context["decision_reason"]) == ("fail_permanently", "integrity_failure")


@pytest.mark.parametrize("first,second", [
    ("https://m1.test/pub/release.iso", "https://m2.test/pub/release.iso"),
    # One server, two protocols: paired by the shared strong digest alone.
    ("ftp://m1.test/pub/release.iso", "https://m1.test/pub/release.iso"),
], ids=["two-servers", "one-server-two-protocols"])
async def test_rejected_material_activates_the_existing_alternate(stage, first, second):
    repository, engine, copier, origin, _store, now = stage
    origin.tree["/r.meta4"] = meta4(described("release.iso", url(first, 1), url(second, 2)))
    copier.corrupt_first = True
    transfer = await engine.submit((TransferRequest("http", origin.url("/r.meta4", host="meta.test")),),
                                   deduplicate=False)
    await _ticks(engine, repository, transfer.id, now)

    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    artifact, _providers = (await _members(engine, repository, transfer.id))["release.iso"]
    assert Path(artifact.target).read_bytes() == DONE  # the rejected bytes never survive
    # The switch is the canonical candidate owner's, decided by the one policy:
    # the route that delivered is the other route, never the rejected one.
    assert sorted(copier.routes) == sorted([first, second])
    assert artifact.candidates[artifact.selected].endpoints[0].address == copier.routes[1]
    context = await repository.recovery_context(artifact.id)
    assert len(context.get("candidate_attempt_history") or ()) == 2


async def test_same_server_routes_with_one_strong_digest_are_one_artifact_with_two_routes(stage):
    repository, engine, copier, origin, _store, now = stage
    origin.tree["/same.meta4"] = meta4(described(
        "release.iso", url("ftp://m1.test/pub/release.iso", 1), url("https://m1.test/pub/release.iso", 2)))
    transfer = await engine.submit((TransferRequest("http", origin.url("/same.meta4", host="meta.test")),),
                                   deduplicate=False)
    await _ticks(engine, repository, transfer.id, now)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    _root, *children = await _requests(transfer.id)
    assert {item["state"] for item in children} == {"resolved"}  # no local_path_conflict
    artifact, providers = (await _members(engine, repository, transfer.id))["release.iso"]
    assert sorted(providers) == ["general_ftp", "general_http"]  # a real failover route
    assert len(copier.started) == 1


async def test_same_server_routes_without_strong_evidence_stay_protected(stage):
    repository, engine, copier, origin, _store, now = stage
    origin.tree["/weak.meta4"] = meta4(described(
        "release.iso", url("ftp://m1.test/pub/release.iso", 1), url("https://m1.test/pub/release.iso", 2),
        digest=None))
    transfer = await engine.submit((TransferRequest("http", origin.url("/weak.meta4", host="meta.test")),),
                                   deduplicate=False)
    await _ticks(engine, repository, transfer.id, now)
    _root, *children = await _requests(transfer.id)
    # The existing same-source rule still refuses to infer one artifact: one
    # route writes, the other is never bound to it.
    members = await _members(engine, repository, transfer.id)
    assert [len(providers) for _artifact, providers in members.values()] == [1]
    assert "local_path_conflict" in " ".join(str(item["error"] or "") for item in children)
    assert len(copier.started) == 1


# ── an uploaded descriptor and the existing selector ──────────────────────────

async def test_an_uploaded_descriptor_offers_the_existing_selector_and_has_no_relative_base(stage):
    repository, engine, copier, _origin, store, now = stage
    staged = store.stage_bytes(meta4(
        described("set/a.bin", url("https://m1.test/a.bin"), url("relative/a.bin")),
        described("set/b.bin", url("https://m2.test/b.bin"))))
    transfer = await engine.submit((TransferRequest("meta4", staged, name="set.meta4", selection_mode="interactive"),),
                                   deduplicate=False)
    for _ in range(4):
        now[0] += 5
        await engine.tick()
    view = await repository.file_selection_presentation(transfer.id, now=now[0])
    assert sorted(entry["relative_path"] for entry in view["entries"]) == ["set/a.bin", "set/b.bin"]
    chosen = [entry["entry_id"] for entry in view["entries"] if entry["relative_path"] == "set/b.bin"]
    assert (await repository.confirm_file_selection(transfer.id, view["manifest_id"], chosen,
                                                    now=now[0])).decision == "explicit"
    await _ticks(engine, repository, transfer.id, now)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    _root, *children = await _requests(transfer.id)
    assert [_payload(item)["payload"] for item in children] == ["https://m2.test/b.bin"]
    assert copier.started == ["m2.test"]
    # Nothing on disk names a relative reference of the upload.
    async with get_db() as db:
        rows = await db.fetchall("SELECT resource FROM transfer_requests WHERE transfer_id=?", (transfer.id,))
    assert not any("relative/a.bin" in str(row["resource"] or "") for row in rows)


async def test_a_single_file_document_needs_no_selection(stage):
    repository, engine, copier, origin, _store, now = stage
    origin.tree["/one.meta4"] = meta4(described("only.bin", url("https://m3.test/only.bin")))
    transfer = await engine.submit((TransferRequest("http", origin.url("/one.meta4", host="meta.test"),
                                                    selection_mode="interactive"),), deduplicate=False)
    await _ticks(engine, repository, transfer.id, now)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert (await repository.get(transfer.id)).name == "only.bin"
    assert copier.started == ["m3.test"]


# ── what a document cannot name, and what routing cannot serve ───────────────

async def test_unusable_and_unsupported_sources_never_poison_a_file_that_has_a_usable_one(stage):
    repository, engine, copier, origin, _store, now = stage
    origin.tree["/mixed.meta4"] = meta4(
        described("a.bin", url("gopher://old.test/a.bin", 1), url("https://m1.test/a.bin", 2)),
        described("only.bin", digest=None, size=None,
                  extra='<metaurl mediatype="torrent">https://m2.test/only.torrent</metaurl>'))
    transfer = await engine.submit((TransferRequest("http", origin.url("/mixed.meta4", host="meta.test")),),
                                   deduplicate=False)
    await _ticks(engine, repository, transfer.id, now)
    _root, *children = await _requests(transfer.id)
    states = {(_payload(item)["kind"], item["state"]) for item in children}
    # The unsupported scheme is refused by routing like any other link; the
    # metadata-only file is reported by its own failed member; a.bin arrives.
    assert ("gopher", "failed") in states and ("meta4-file", "failed") in states
    errors = {_payload(item)["kind"]: item["error"] for item in children if item["state"] == "failed"}
    assert "unsupported_request" in errors["gopher"]
    assert "no_transfer_candidate" in errors["meta4-file"] and "metaurl_only" in errors["meta4-file"]
    assert copier.started == ["m1.test"]
    assert (await _members(engine, repository, transfer.id))["a.bin"][0].state == "completed"
    # The torrent reference was never fetched by anything.
    assert all("only.torrent" not in str(item) for item in copier.calls)


@pytest.mark.parametrize("body,category,diagnostic", [
    (b"<html>not a descriptor</html>", "unsupported_request", "descriptor_unsupported"),
    (b"<metalink", "content_invalid", "descriptor_malformed"),
])
async def test_a_document_dp_does_not_interpret_fails_the_request_cleanly(stage, body, category, diagnostic):
    repository, engine, copier, origin, _store, now = stage
    origin.tree["/x.meta4"] = body
    transfer = await engine.submit((TransferRequest("http", origin.url("/x.meta4", host="meta.test")),),
                                   deduplicate=False)
    await _ticks(engine, repository, transfer.id, now, count=8)
    # The provider's ordinary resolution failure, never an executor lifecycle.
    root, = await _requests(transfer.id)
    assert root["state"] == "failed" and root["provider_id"] == "multimeta"
    assert category in root["error"] and diagnostic in root["error"] and '"retryability":"never"' in root["error"]
    assert copier.started == []


async def test_an_oversized_document_is_refused_without_reading_past_the_bound(stage, monkeypatch):
    import providers.multimeta.provider as provider_module
    repository, engine, _copier, origin, _store, now = stage
    monkeypatch.setattr(provider_module, "MAX_DESCRIPTOR_BYTES", 64)
    origin.tree["/big.meta4"] = meta4(described("release.iso", url("https://m1.test/pub/release.iso")))
    transfer = await engine.submit((TransferRequest("http", origin.url("/big.meta4", host="meta.test")),),
                                   deduplicate=False)
    await _ticks(engine, repository, transfer.id, now, count=8)
    root, = await _requests(transfer.id)
    # The executor-side reader enforces the provider's hard bound itself: one
    # byte past it refuses the read, it is never truncated or parsed.
    assert root["state"] == "failed"
    assert '"category":"unsupported_request"' in root["error"] and '"diagnostic":"too_large"' in root["error"]


# ── the descriptor's authority is never the mirrors' ──────────────────────────

async def test_descriptor_access_input_is_asked_for_its_own_authority_and_never_reaches_a_mirror(stage):
    repository, engine, copier, origin, _store, now = stage
    origin.credentials = (USER, PASSWORD)
    copier.locks["meta.test"] = (USER, PASSWORD)  # the descriptor's authority serves a mirror too
    origin.tree["/locked.meta4"] = meta4(
        described("release.iso", url("https://m1.test/pub/release.iso"), url("https://m2.test/pub/release.iso")),
        described("a.bin", url(origin.url("/final/mirror/release.iso", host="meta.test"))))
    transfer = await engine.submit((TransferRequest("http", origin.url("/locked.meta4", host="meta.test")),),
                                   deduplicate=False)
    for _ in range(3):
        now[0] += 5
        await engine.tick()
    root, = await _requests(transfer.id)
    challenge = await engine.challenges.current(transfer.id)
    # The descriptor request's own question, through the one INPUT_REQUIRED
    # lifecycle; nothing has been fanned out yet.
    assert challenge is not None and challenge.request_id == root["id"] and root["state"] == "input_required"
    await engine.submit_input(transfer.id, challenge.id, "username_password", {"username": USER, "password": PASSWORD})
    await _ticks(engine, repository, transfer.id, now)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    # The answer belongs to the descriptor's authority: the mirror on that
    # same authority used it, and no other mirror was ever offered it -- by
    # evidence sampling or by execution.
    assert copier.credentialed == ["meta.test"]
    assert {host for host, _user in copier.offered} <= {"meta.test"}
    assert len(copier.started) == 2 and set(copier.started) - {"meta.test"} <= {"m1.test", "m2.test"}


async def test_local_network_consent_for_the_descriptor_is_never_a_mirrors(stage):
    repository, engine, _copier, origin, _store, now = stage
    origin.tree["/lan.meta4"] = meta4(
        described("release.iso", url("https://m1.test/pub/release.iso")),
        described("a.bin", url(origin.url("/final/mirror/release.iso", host="meta.test"))))
    transfer = await engine.submit((TransferRequest("http", origin.url("/lan.meta4", host="meta.test"),
                                                    local_network_consent=True),), deduplicate=False)
    await _ticks(engine, repository, transfer.id, now)
    grants = {candidate.name: candidate.private_network_grant
              for artifact in await repository.artifacts(transfer.id) for candidate in artifact.candidates}
    # Only a source on the exact host the operator submitted keeps the grant
    # (the one core owner, ``_authoritative_provider_result``); a mirror the
    # document names elsewhere is evaluated as its own destination.
    assert grants == {"release.iso": False, "a.bin": True}


async def test_an_upload_is_staged_once_and_the_request_carries_only_its_reference(stage):
    from application.service import ApplicationService
    from transfers.staged_input import StagedPayload
    repository, engine, copier, _origin, store, now = stage
    document = meta4(described("only.bin", url("https://m3.test/only.bin")))

    async def chunks():
        yield document[:40]
        yield document[40:]

    result = await ApplicationService(engine, staged_input=store).submit_meta4(chunks(), "only.meta4")
    root = next(record for record in await repository.requests(result["id"]) if record.parent_id is None)
    # The neutral durable-input owner holds the bytes; the request a reference.
    assert root.request.kind == "meta4" and isinstance(root.request.payload, StagedPayload)
    assert store.read(root.request.payload) == document
    await _ticks(engine, repository, result["id"], now)
    assert (await repository.get(result["id"])).state == TransferState.COMPLETED
    assert copier.started == ["m3.test"]


async def test_core_never_accepts_more_content_than_the_provider_asked_for(stage, monkeypatch):
    import providers.multimeta.provider as provider_module
    repository, engine, copier, origin, _store, now = stage
    monkeypatch.setattr(provider_module, "MAX_DESCRIPTOR_BYTES", 64)

    async def overreaching(subject, submitted=None, **tree):
        return DiscoveryResult(kind=RemoteObjectKind.FILE, content=b"x" * 65)

    monkeypatch.setattr(copier, "discover", overreaching)
    transfer = await engine.submit((TransferRequest("http", origin.url("/any.meta4", host="meta.test")),),
                                   deduplicate=False)
    await _ticks(engine, repository, transfer.id, now, count=12)
    # Every attempt is refused at core's boundary: the provider never parses
    # a byte more than it asked for.
    attempts = await _requests(transfer.id)
    assert {row["id"] for row in attempts} == {attempts[0]["id"]} and attempts[0]["state"] == "failed"
    assert "executor_protocol_violation" in attempts[0]["error"]
