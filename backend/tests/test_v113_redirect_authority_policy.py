"""DP 1.0.13: authentication belongs to the authority that asked.

A source's address may move (an HTTP(S) redirect) to a server of ANOTHER
authority. Three neutral rules make that safe and usable everywhere at once:

1. ``InputRequirement.authority`` names the server that asked when it is not
   the subject's own; the one authentication-input owner keys every match,
   question and answer by it, and stamps leased input with the scope it was
   given for (``SubmittedInput.scope``).
2. The one in-process HTTP(S) request owner (``_guarded_request``) attaches an
   operator credential only to requests of exactly that authority -- never
   another host or port, never HTTP for an HTTPS answer.
3. An HTTP(S) download is pointed at the address that finally answers
   (``resolve_location``); aria2 itself never follows a redirect.
"""
from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from execution_requests import file_request
from services import artifact_sampling as sampling
from test_aria2_executor_contract import NativeDaemon
from test_v113_transport_evidence_sampling import loopback  # noqa: F401
from test_v113_transfer_auth_context import PASSWORD, USER, lab  # noqa: F401
from fake_integrations import VaultExecutor, neutral_facts
from transfers.errors import Category, Domain, NormalizedError, Stage
from transfers.input_required import EphemeralInputBroker, auth_required, public_challenge, username_password
from transfers.models import (
    Endpoint, ExecutionObservation, ExecutionState, ExecutorCapabilities, InputField, InputMethod,
    InputReason, InputRequirement, IntegrationDescriptor, TransferCandidate, TransferProgress, TransferRequest,
    TransferState,
)
from transfers.requests import auth_scope
from webdav_origin import WebDavOrigin

pytestmark = pytest.mark.asyncio


def _basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# ── 1. the requirement names its authority; the owner scopes by it ────────────

async def test_an_authority_is_always_a_bare_canonical_origin():
    from transfers.models import InputChallenge, InputOrigin, canonical_authority
    assert auth_required(username_password(), authority="https://cdn.example").authority == "https://cdn.example"
    # Path, query and fragment never survive; scheme and host are canonical.
    for given, origin in (("HTTPS://CDN.Example:8443/signed/x?token=s#f", "https://cdn.example:8443"),
                          ("https://cdn.example./a?X-Amz-Signature=abc", "https://cdn.example"),
                          ("http://[2001:DB8::1]:8080/p", "http://[2001:db8::1]:8080")):
        assert canonical_authority(given) == origin
        assert InputRequirement(InputReason.AUTH_REQUIRED, (username_password(),), authority=given).authority == origin
        assert InputChallenge("c", 1, 1, InputReason.AUTH_REQUIRED, InputOrigin.PROVIDER, "p", "o",
                              (username_password(),), authority=given).authority == origin
    for bad in ("https://user:pw@cdn.example", "cdn.example", "https://", "https://cdn.example:0"):
        with pytest.raises(ValueError):
            InputRequirement(InputReason.AUTH_REQUIRED, (username_password(),), authority=bad)


async def test_no_path_query_or_capability_ever_reaches_a_durable_question(lab):  # noqa: F811
    from db.database import get_db
    repository, _registry, engine, *_rest = lab
    transfer = await engine.submit((TransferRequest("vault", "vault://locked.example/solo.bin"),), deduplicate=False)
    record = next(item for item in await repository.requests(transfer.id))
    attempt = await repository.begin_resolution(record.id, "locked-source")
    signed = "https://cdn.example:8443/signed/x?token=capability-sentinel#frag"
    asked = await engine.challenges.wait_provider(attempt, auth_required(username_password(), authority=signed),
                                                  "locked-source")

    async def row():
        async with get_db() as db:
            return dict(await db.fetchone("SELECT * FROM transfer_input_challenges WHERE transfer_id=?",
                                          (transfer.id,)))

    stored = await row()
    assert stored["authority"] == asked.authority == "https://cdn.example:8443"
    assert "signed" not in repr(stored) and "capability-sentinel" not in repr(stored) and "frag" not in repr(stored)
    current = await engine.challenges.current(transfer.id)
    assert current.authority == current.requirement.authority == "https://cdn.example:8443"
    assert public_challenge(current)["authority"] == "https://cdn.example:8443"
    # A reissued question is held to the same contract.
    await engine.challenges.replace(current, auth_required(username_password(),
                                                           authority="https://other.example/p?sig=capability-sentinel"))
    stored = await row()
    assert stored["authority"] == "https://other.example" and "capability-sentinel" not in repr(stored)


async def test_leased_input_carries_its_scope_and_a_release_is_no_verdict():
    broker = EphemeralInputBroker()
    scope = auth_scope("https://dav.example/dir/")
    await broker.supply(1, "root", scope, {InputField.USERNAME: USER, InputField.PASSWORD: PASSWORD}, origin="admission")
    first = await broker.resolve(1, ("root",), scope, auth_required(username_password()))
    assert first.submitted.scope == scope
    assert (await broker.token_scope(first.submitted.token)) == scope
    await broker.release(first.submitted.token)
    again = await broker.resolve(1, ("root",), scope, auth_required(username_password()))
    assert again.outcome == "satisfied"  # released, never rejected, never left pending
    other = await broker.resolve(1, ("root",), auth_scope("https://cdn.example/"), auth_required(username_password()))
    assert other.outcome == "challenge"  # another authority never inherits it


# ── 2. the one redirect owner scopes the credential per hop ───────────────────

class _Answer:
    def __init__(self, status, location=""):
        self.status = status
        self.headers = {"Location": location} if location else {}

    def release(self):
        pass


class _Session:
    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    async def request(self, method, uri, *, headers, data, allow_redirects):
        self.sent.append((uri, dict(headers)))
        return self.script.pop(0)


@pytest.mark.parametrize("target,carried", [
    ("https://dav.example/elsewhere", True),          # same authority: the credential continues
    ("https://cdn.example/signed", False),            # another host
    ("https://dav.example:8443/x", False),            # another port
    ("http://dav.example/plain", False),              # HTTPS to HTTP
])
async def test_a_credential_reaches_only_its_own_authority(loopback, target, carried):  # noqa: F811
    session = _Session([_Answer(302, target), _Answer(200)])
    credential = (auth_scope("https://dav.example/"), _basic(USER, PASSWORD))
    response, _reason, answered = await sampling._guarded_request(
        session, "https://dav.example/file", {}, credential=credential)
    assert response.status == 200 and answered == target
    assert session.sent[0][1].get("Authorization") == credential[1]
    assert ("Authorization" in session.sent[1][1]) is carried


async def test_a_credential_for_the_moved_to_authority_never_reaches_the_first(loopback):  # noqa: F811
    session = _Session([_Answer(302, "https://cdn.example/signed"), _Answer(200)])
    credential = (auth_scope("https://cdn.example/"), _basic("cdn", "secret"))
    await sampling._guarded_request(session, "https://dav.example/file", {}, credential=credential)
    assert "Authorization" not in session.sent[0][1]
    assert session.sent[1][1]["Authorization"] == credential[1]


@pytest_asyncio.fixture
async def origins(loopback):  # noqa: F811
    first = await WebDavOrigin({"/dl/": None, "/dl/file.bin": b"payload"}).start()
    second = await WebDavOrigin({"/cdn/file.bin": b"payload"}).start()
    yield first, second
    await first.close()
    await second.close()


async def test_location_resolution_follows_redirects_with_the_credential_scoped(origins):
    first, second = origins
    first.credentials = (USER, PASSWORD)
    first.redirects["/dl/file.bin"] = second.url("/cdn/file.bin", host="cdn.test")
    credential = (auth_scope(first.url("/dl/file.bin")), _basic(USER, PASSWORD))
    located = await sampling.resolve_location(first.url("/dl/file.bin"), credential=credential)
    assert located == sampling.Located(second.url("/cdn/file.bin", host="cdn.test"), 200)
    assert [auth for *_rest, auth in first.requests] == [_basic(USER, PASSWORD)]
    assert [auth for *_rest, auth in second.requests] == [None]
    # Never the body: a single-byte range is asked for and nothing is read.
    assert all(method == "GET" for method, *_rest in first.requests + second.requests)


async def test_a_moved_to_authority_that_asks_is_named(origins):
    first, second = origins
    first.redirects["/dl/file.bin"] = second.url("/cdn/file.bin", host="cdn.test")
    second.credentials = ("cdn", "secret")
    located = await sampling.resolve_location(first.url("/dl/file.bin"),
                                              credential=(auth_scope(first.url("/")), _basic(USER, PASSWORD)))
    assert located == sampling.AccessRequired(address=second.url("/cdn/file.bin", host="cdn.test"))
    sampled = await sampling.sampled_public_artifact_fingerprint(
        first.url("/dl/file.bin"), credential=(auth_scope(first.url("/")), _basic(USER, PASSWORD)))
    assert sampled == sampling.AccessRequired(address=second.url("/cdn/file.bin", host="cdn.test"))
    assert all(auth is None for *_rest, auth in second.requests)


# ── 3. aria2 is pointed at the answering address, never told to follow ────────

@pytest_asyncio.fixture
async def aria2(tmp_path, origins):
    from executors.aria2.executor import Aria2Configuration, Aria2Executor
    daemon = NativeDaemon()
    grants = {}
    routes = []

    async def authorize(handle, action):
        return grants.get(handle.attempt_id) == handle

    def job_options(address, scope=None, budget=None, **kwargs):
        routes.append((address, kwargs))
        return {"all-proxy": "http://guard:8888"}

    egress = SimpleNamespace(ensure_started=AsyncMock(), job_options=job_options, private_lan_enabled=False)
    executor = Aria2Executor(daemon, Aria2Configuration(str(tmp_path), confirmation_delay=0,
                                                        control_confirmation_timeout=0.02), authorize, egress=egress)

    def prepared(address, *, headers=None, accepts=True, attempt="attempt-1"):
        candidate = TransferCandidate("file.bin", (Endpoint("http", address, dict(headers or {})),),
                                      expected_bytes=7,
                                      accepted_input_methods=(InputMethod.USERNAME_PASSWORD,) if accepts else ())
        request = file_request(candidate, str(tmp_path / attempt / "file.bin"), attempt, root=tmp_path)
        handle = executor.prepare(request)
        grants[handle.attempt_id] = handle
        return candidate, request, handle

    return SimpleNamespace(executor=executor, daemon=daemon, routes=routes, prepared=prepared, origins=origins)


def _submitted(user, password, scope):
    from transfers.input_required import SubmittedInput
    item = SubmittedInput("c", 1, InputMethod.USERNAME_PASSWORD, {InputField.USERNAME: user,
                                                                   InputField.PASSWORD: password})
    item.scope = scope
    return item


def _added(daemon):
    (method, (uris, options)), = [call for call in daemon.calls if call[0] == "aria2.addUri"]
    return uris, options


async def test_a_same_authority_redirect_keeps_the_credential_for_the_answering_address(aria2):
    first, _second = aria2.origins
    first.credentials = (USER, PASSWORD)
    first.tree["/dl/moved.bin"] = b"payload"
    first.redirects["/dl/file.bin"] = "/dl/moved.bin"
    _candidate, request, handle = aria2.prepared(first.url("/dl/file.bin"), headers={"X-Capability": "cap"})
    observed = await aria2.executor.start_with_input(request, handle, _submitted(USER, PASSWORD,
                                                                                  auth_scope(first.url("/"))))
    assert observed.state == ExecutionState.QUEUED
    uris, options = _added(aria2.daemon)
    assert uris == [first.url("/dl/moved.bin")]
    assert (options["http-user"], options["http-passwd"]) == (USER, PASSWORD)
    assert options["header"] == ["X-Capability: cap"]
    assert options["max-http-redirection"] == "0"  # aria2 itself never follows
    assert aria2.routes[-1][0] == first.url("/dl/moved.bin")


async def test_a_signed_cross_authority_target_gets_no_credential_and_no_capability_header(aria2):
    first, second = aria2.origins
    first.credentials = (USER, PASSWORD)
    target = second.url("/cdn/file.bin", host="cdn.test")
    first.redirects["/dl/file.bin"] = target
    _candidate, request, handle = aria2.prepared(first.url("/dl/file.bin"), headers={"X-Capability": "cap"})
    observed = await aria2.executor.start_with_input(request, handle, _submitted(USER, PASSWORD,
                                                                                  auth_scope(first.url("/"))))
    assert observed.state == ExecutionState.QUEUED
    uris, options = _added(aria2.daemon)
    assert uris == [target]
    assert options["http-user"] == "" and options["http-passwd"] == "" and "header" not in options
    assert all(auth is None for *_rest, auth in second.requests)
    assert aria2.routes[-1] == (target, {})  # the guard route is the answering address; no LAN grant moves


async def test_a_moved_to_authority_that_asks_is_answered_for_itself_and_starts_there(aria2):
    first, second = aria2.origins
    first.credentials = (USER, PASSWORD)
    target = second.url("/cdn/file.bin", host="cdn.test")
    first.redirects["/dl/file.bin"] = target
    second.credentials = ("cdn", "secret")
    candidate, request, handle = aria2.prepared(first.url("/dl/file.bin"))
    observed = await aria2.executor.start_with_input(request, handle, _submitted(USER, PASSWORD,
                                                                                  auth_scope(first.url("/"))))
    assert observed.state == ExecutionState.FAILED and observed.error.native_code == "24"
    requirement = aria2.executor.input_requirement(candidate, observed)
    origin = target.rsplit("/cdn/", 1)[0]
    assert requirement.authority == origin and auth_scope(requirement.authority) == auth_scope(target)
    assert not [call for call in aria2.daemon.calls if call[0] == "aria2.addUri"]
    # The answer for that authority starts there; the first server never sees it.
    first.requests.clear()
    _c, request2, handle2 = aria2.prepared(first.url("/dl/file.bin"), attempt="attempt-2")
    observed = await aria2.executor.start_with_input(request2, handle2, _submitted("cdn", "secret",
                                                                                    auth_scope(target)))
    assert observed.state == ExecutionState.QUEUED
    uris, options = _added(aria2.daemon)
    assert uris == [target] and (options["http-user"], options["http-passwd"]) == ("cdn", "secret")
    assert first.requests == []


async def test_a_challenge_at_the_own_authority_is_left_to_the_writer(aria2):
    first, _second = aria2.origins
    first.credentials = (USER, PASSWORD)
    candidate, request, handle = aria2.prepared(first.url("/dl/file.bin"))
    observed = await aria2.executor.start(request, handle)
    assert observed.state == ExecutionState.QUEUED  # aria2 asks itself, exactly as before
    assert _added(aria2.daemon)[0] == [first.url("/dl/file.bin")]


# ── 4. core asks, answers and uses another authority's question as its own ────

MOVED = "vault://b.example"


class MovingVault(VaultExecutor):
    """The subject ``vault://a.example/...`` is answered at another authority
    (``MOVED``), which asks for its own credentials."""

    descriptor = IntegrationDescriptor("moving-vault", "Moving vault", frozenset())
    capabilities = ExecutorCapabilities(candidate_sampling=True, per_execution_pause=True, transient_input=True)

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.offered = []

    async def fingerprint(self, subject):
        return await super().fingerprint(subject)

    def _moved(self, handle):
        error = NormalizedError(Domain.EXECUTOR, Category.UNMAPPED_EXECUTOR_ERROR, Stage.EXECUTION,
                                native_code="moved")
        observed = neutral_facts(ExecutionObservation(handle, ExecutionState.FAILED, TransferProgress(4, 0, 0), error))
        self.jobs[handle.attempt_id] = observed
        return observed

    async def start(self, request, handle):
        assert await self.authorize(handle, "start")
        return self._moved(handle)

    def input_requirement(self, candidate, observation):
        if observation.error is not None and observation.error.native_code == "moved":
            return auth_required(username_password(), authority=MOVED)
        return None

    async def start_with_input(self, request, handle, submitted):
        self.offered.append((submitted.scope, submitted.value(InputField.USERNAME)))
        if submitted.scope != auth_scope(MOVED):
            return self._moved(handle)
        # Continuing the challenged attempt with its answer.
        observed = neutral_facts(ExecutionObservation(handle, ExecutionState.RUNNING, TransferProgress(4, 1, 1)))
        self.jobs[handle.attempt_id] = observed
        self.finish(handle)
        return observed


async def test_an_execution_question_of_another_authority_is_asked_answered_and_used_for_it(lab):  # noqa: F811
    repository, registry, engine, *_rest, now = lab
    executor = MovingVault(repository.authorize_execution, objects={"locked.example/solo.bin": b"four"})
    registry.register_executor(executor)
    # Credentials for the subject's OWN authority arrive with the link.
    transfer = await engine.submit((TransferRequest("vault", f"vault://{USER}:{PASSWORD}@locked.example/solo.bin"),),
                                   deduplicate=False)
    asked = []
    for _ in range(40):
        now[0] += 5
        await engine.tick()
        challenge = await engine.challenges.current(transfer.id)
        if challenge is not None and challenge.id not in {item.id for item in asked}:
            asked.append(challenge)
            await engine.submit_input(transfer.id, challenge.id, "username_password",
                                      {"username": "b-user", "password": "b-password"})
        if (await repository.get(transfer.id)).state in {TransferState.COMPLETED, TransferState.FAILED}:
            break
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert [item.authority for item in asked] == [MOVED]
    # The moved-to authority's answer was used for it alone ...
    assert (auth_scope(MOVED), "b-user") in executor.offered
    assert all(user != "b-user" for scope, user in executor.offered if scope != auth_scope(MOVED))
    # ... and the subject's own material was never condemned by the other's question.
    record = next(item for item in await repository.requests(transfer.id) if item.parent_id is None)
    still = await engine.inputs.resolve(transfer.id, (record.id,), auth_scope("vault://locked.example/solo.bin"),
                                        auth_required(username_password()))
    assert still.outcome in {"satisfied", "pending"}
