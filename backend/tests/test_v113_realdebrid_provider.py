"""Real-Debrid provider: native REST/OAuth mechanics behind neutral contracts.

Native HTTP is replaced at the client boundary (an injected transport) or the
provider's client is a fake; nothing here waits for a real OAuth interval."""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlsplit

import pytest

from providers.realdebrid import admin
from providers.realdebrid.client import (
    API, OAUTH, OPEN_SOURCE_CLIENT_ID, Credential, RawResponse, RealDebridAPIError, RealDebridProtocolError,
    RealDebridService,
)
from providers.realdebrid.definition import RealDebridOptions, definition
from providers.realdebrid.host_runtime import (
    HOST_SCHEMA_VERSION, RealDebridHostMaintenance, RealDebridHostSnapshotError, RealDebridRequestApplicability,
    decode_host_snapshot, encode_host_snapshot, parse_native_host_snapshot,
)
from providers.realdebrid.provider import RealDebridProvider
from providers.realdebrid.translation import translate_error
from transfers.applicability import ApplicabilityReadiness
from transfers import codec
from transfers.errors import Category, MutationOutcome, Origin, Retryability, Stage, TransferError
from transfers.file_selection import SelectionUnprovable, normalize_relative_path, reconcile_executable_subset
from transfers.models import (
    CachePresence, CleanupAuthority, CleanupDirective, DeliveryKind, OutcomeKind, Ownership, ResourceState,
    SourceIdentity, TransferRequest,
)
from transfers.policy import provider_attributable

CREDENTIAL = Credential("bound-client", "client-secret-value", "refresh-token-value")


class Transport:
    """Scripted native HTTP: each call takes the next response for its route."""

    def __init__(self, script):
        self.script = {key: list(value) for key, value in script.items()}
        self.calls = []

    async def __call__(self, method, url, *, headers=None, params=None, data=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "params": dict(params or {}), "data": data, "timeout": timeout})
        status, payload, *extra = self.script[(method, url)].pop(0)
        body = payload if isinstance(payload, bytes) else (b"" if payload is None else json.dumps(payload).encode())
        return RawResponse(status, extra[0] if extra else {}, body)


class NoLimit:
    async def acquire(self):
        return None


def service(script, *, credential=CREDENTIAL, on_refresh=None, clock=lambda: 1000.0):
    transport = Transport(script)
    return RealDebridService(credential, rate_limiter=NoLimit(), transport=transport, on_refresh=on_refresh,
                             clock=clock), transport


# Real-Debrid's answer for a device the user has not approved yet (observed
# live 2026-10-01), and for an unknown device code.
NOT_YET_AUTHORIZED = (403, {"error": None, "error_code": None})
UNKNOWN_DEVICE = (200, {"client_id": None, "client_secret": None})

TOKEN = (200, {"access_token": "access-token-value", "expires_in": 3600, "token_type": "Bearer",
               "refresh_token": "refresh-token-value"})


def refusal(exc) -> tuple:
    error = translate_error(exc)
    return error.category, error.retryability


# --------------------------------------------------------------------------- #
# Client and OAuth device flow
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_device_flow_uses_the_open_source_client_and_exchanges_the_device_code():
    client, transport = service({
        ("GET", f"{OAUTH}/device/code"): [(200, {"device_code": "DEV", "user_code": "ABCD1234", "interval": 5,
                                                 "expires_in": 600, "verification_url": "https://real-debrid.com/device"})],
        ("GET", f"{OAUTH}/device/credentials"): [NOT_YET_AUTHORIZED,
                                                 (200, {"client_id": "bound", "client_secret": "secret"})],
        ("POST", f"{OAUTH}/token"): [TOKEN],
    }, credential=None)
    native = await client.device_code()
    assert native["user_code"] == "ABCD1234"
    assert transport.calls[0]["params"] == {"client_id": OPEN_SOURCE_CLIENT_ID, "new_credentials": "yes"}
    assert await client.device_credentials("DEV") is None           # not authorized yet: pending, not failure
    granted = await client.device_credentials("DEV")
    tokens = await client.token(granted["client_id"], granted["client_secret"], "DEV")
    assert tokens["refresh_token"] == "refresh-token-value"
    assert transport.calls[-1]["data"] == {"client_id": "bound", "client_secret": "secret", "code": "DEV",
                                           "grant_type": "http://oauth.net/grant_type/device/1.0"}


@pytest.mark.asyncio
@pytest.mark.parametrize("response, outcome", [
    ((429, {"error": "too_many_requests", "error_code": 34}), (Category.RATE_LIMITED, Retryability.BACKOFF)),
    ((400, {"error": "bad_parameter", "error_code": 2}), (Category.INVALID_REQUEST, Retryability.NEVER)),
    ((403, {"error": "permission_denied", "error_code": 9}),
     (Category.AUTHORIZATION_FAILED, Retryability.AFTER_RESOURCE_CHANGE)),
], ids=["rate-limited", "unrelated-400", "unrelated-403"])
async def test_only_the_not_yet_authorized_signature_is_pending(response, outcome):
    client, _ = service({("GET", f"{OAUTH}/device/credentials"): [response]}, credential=None)
    with pytest.raises(RealDebridAPIError) as refused:
        await client.device_credentials("DEV")
    assert refusal(refused.value) == outcome


@pytest.mark.asyncio
async def test_a_success_without_the_client_credential_is_malformed_not_pending():
    client, _ = service({("GET", f"{OAUTH}/device/credentials"): [UNKNOWN_DEVICE]}, credential=None)
    with pytest.raises(RealDebridProtocolError):
        await client.device_credentials("DEV")


@pytest.mark.asyncio
async def test_rest_calls_refresh_provider_locally_and_authenticate_by_bearer_header_only():
    rotated = []

    async def persist(credential):
        rotated.append(credential.refresh_token)

    client, transport = service({
        ("POST", f"{OAUTH}/token"): [(200, {**TOKEN[1], "refresh_token": "rotated-refresh"})],
        ("GET", f"{API}/user"): [(200, {"username": "alice", "type": "premium"})],
    }, on_refresh=persist)
    assert (await client.user())["username"] == "alice"
    refresh, user = transport.calls
    assert refresh["data"]["code"] == "refresh-token-value"
    assert user["headers"] == {"Authorization": "Bearer access-token-value"}
    assert "access-token-value" not in json.dumps(user["params"]) + user["url"]
    assert rotated == ["rotated-refresh"] and client.credential.refresh_token == "rotated-refresh"


@pytest.mark.asyncio
async def test_a_refused_access_token_is_refreshed_once_and_the_call_replayed_once():
    client, transport = service({
        ("POST", f"{OAUTH}/token"): [TOKEN, TOKEN],
        ("GET", f"{API}/user"): [(401, {"error": "bad_token", "error_code": 8}), (200, {"username": "alice"})],
    })
    assert (await client.user())["username"] == "alice"
    assert [call["url"].rsplit("/", 1)[-1] for call in transport.calls] == ["token", "user", "token", "user"]


@pytest.mark.asyncio
async def test_a_revoked_grant_is_a_definitive_reauthorization_requirement():
    client, _ = service({("POST", f"{OAUTH}/token"): [(400, {"error": "invalid_grant"})]})
    with pytest.raises(RealDebridAPIError) as refused:
        await client.user()
    assert refusal(refused.value) == (Category.CREDENTIAL_INVALID, Retryability.AFTER_REAUTH)
    unconfigured = RealDebridService(None, rate_limiter=NoLimit(), transport=Transport({}))
    with pytest.raises(RealDebridAPIError) as missing:
        await unconfigured.user()
    assert refusal(missing.value) == (Category.CREDENTIAL_MISSING, Retryability.AFTER_REAUTH)


@pytest.mark.asyncio
async def test_rate_limit_is_a_typed_refusal_core_decides_on_and_is_never_retried_here():
    client, transport = service({
        ("POST", f"{OAUTH}/token"): [TOKEN],
        ("POST", f"{API}/unrestrict/link"): [(429, {"error": "too_many_requests", "error_code": 34})],
    })
    with pytest.raises(RealDebridAPIError) as refused:
        await client.unrestrict_link("https://hoster.example/f/1")
    assert refusal(refused.value) == (Category.RATE_LIMITED, Retryability.BACKOFF)
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_malformed_responses_and_unknown_codes_never_become_speculative_facts():
    client, _ = service({("POST", f"{OAUTH}/token"): [TOKEN], ("GET", f"{API}/user"): [(200, b"<html>")]})
    with pytest.raises(RealDebridProtocolError) as malformed:
        await client.user()
    assert translate_error(malformed.value).category == Category.PROVIDER_PROTOCOL_VIOLATION
    assert refusal(RealDebridAPIError(999, "new_thing", 400)) == (Category.UNMAPPED_PROVIDER_ERROR,
                                                                  Retryability.UNKNOWN)
    assert refusal(RealDebridAPIError(16, "hoster_unsupported", 503)) == (Category.UNSUPPORTED_REQUEST,
                                                                          Retryability.NEVER)
    assert refusal(RealDebridAPIError(None, "", 503))[0] == Category.PROVIDER_UNAVAILABLE


def test_diagnostics_never_carry_a_secret():
    client = RealDebridService(CREDENTIAL, rate_limiter=NoLimit(), transport=Transport({}))
    error = translate_error(RuntimeError("echo client-secret-value refresh-token-value"), secrets=client.secrets())
    assert "client-secret-value" not in error.diagnostic and "refresh-token-value" not in error.diagnostic


@pytest.mark.asyncio
async def test_device_authorization_polls_no_faster_than_real_debrid_asks():
    now = [0.0]
    client, transport = service({
        ("GET", f"{OAUTH}/device/code"): [(200, {"device_code": "DEV", "user_code": "CODE", "interval": 5,
                                                 "expires_in": 60, "verification_url": "https://real-debrid.com/device"})],
        ("GET", f"{OAUTH}/device/credentials"): [NOT_YET_AUTHORIZED,
                                                 (200, {"client_id": "bound", "client_secret": "secret"})],
        ("POST", f"{OAUTH}/token"): [TOKEN],
    }, credential=None)
    clock = lambda: now[0]
    started = await admin.start_authorization(service=client, clock=clock)
    assert started == {"state": "pending", "user_code": "CODE", "verification_url": "https://real-debrid.com/device",
                       "interval": 5, "expires_in": 60}
    assert "DEV" not in json.dumps(started)
    assert (await admin.poll_authorization(service=client, clock=clock))["state"] == "pending"  # too early
    assert len(transport.calls) == 1
    now[0] = 5
    assert (await admin.poll_authorization(service=client, clock=clock))["state"] == "pending"
    now[0] = 10
    outcome = await admin.poll_authorization(service=client, clock=clock)
    assert outcome.credential == Credential("bound", "secret", "refresh-token-value")
    assert admin.authorization_state() == {"state": "idle"}
    await admin.start_authorization(service=service({("GET", f"{OAUTH}/device/code"): [(200, {
        "device_code": "D2", "user_code": "C2", "interval": 5, "expires_in": 60,
        "verification_url": "https://real-debrid.com/device"})]}, credential=None)[0], clock=clock)
    now[0] = 100
    assert (await admin.poll_authorization(service=client, clock=clock)) == {"state": "expired"}


def test_the_test_proves_the_grant_and_a_refreshed_token_never_revokes_it():
    saved = RealDebridOptions(client_id="bound", client_secret="secret", refresh_token="one")
    refreshed = saved.model_copy(update={"refresh_token": "two", "rate_limit_per_minute": 100})
    assert definition.verification_fingerprints(saved.model_dump()) == \
        definition.verification_fingerprints(refreshed.model_dump())
    assert definition.verification_fingerprints(RealDebridOptions().model_dump()) == {}
    public = definition.public_options(saved.model_dump())
    assert public["client_secret"] == "" and public["refresh_token"] == "" and public["client_id_configured"]


# --------------------------------------------------------------------------- #
# Supported-host applicability
# --------------------------------------------------------------------------- #

PATTERNS = [r"/(http|https):\/\/(\w+\.)?1fichier\.com\/\?([^( |\"|'|>|<|\r\n\|\r|\n|:|$)]+)/",
            r"/(http|https):\/\/(\w+\.)?real-debrid\.com\/d\/([0-9A-Z]+)/"]
SNAPSHOT = parse_native_host_snapshot(["1fichier.com", "real-debrid.com"], PATTERNS)


def claimed(applicability, url):
    facts = applicability(TransferRequest(urlsplit(url).scheme, url))
    return facts.readiness, [claim.host for claim in facts.specialized_hosts]


def test_host_applicability_is_provider_local_and_carries_only_neutral_facts():
    applies = RealDebridRequestApplicability(SNAPSHOT)
    assert claimed(applies, "https://1fichier.com/?abc123") == (ApplicabilityReadiness.READY, ["1fichier.com"])
    # A supported domain whose link the native validator refuses is no claim.
    assert claimed(applies, "https://1fichier.com/dir/") == (ApplicabilityReadiness.READY, [])
    assert claimed(applies, "https://unsupported.example/file") == (ApplicabilityReadiness.READY, [])
    assert claimed(applies, "https://real-debrid.com/d/ABC123") == (ApplicabilityReadiness.READY, ["real-debrid.com"])
    assert claimed(RealDebridRequestApplicability(None), "https://1fichier.com/?abc") == \
        (ApplicabilityReadiness.UNRESOLVED, [])


@pytest.mark.parametrize("patterns", [["(unanchored"], ["/(a/"], ["/a/x"], ["/(?=lookahead)/"]])
def test_an_unusable_provider_regex_rejects_the_whole_replacement(patterns):
    with pytest.raises(RealDebridHostSnapshotError):
        parse_native_host_snapshot(["1fichier.com"], patterns)


def test_the_snapshot_round_trips_through_its_persisted_form():
    assert decode_host_snapshot(encode_host_snapshot(SNAPSHOT)) == SNAPSHOT
    with pytest.raises(RealDebridHostSnapshotError):
        decode_host_snapshot(b'{"source":"other","domains":[],"patterns":[]}')


class MemoryStore:
    def __init__(self):
        self.record = None

    async def load(self, integration_id, state_key="default"):
        return self.record

    async def replace(self, integration_id, payload, *, schema_version, state_key="default", observed_at=None,
                      stale_after=None, successful_at=None, expected_generation=None):
        from integrations.runtime_state import RuntimeStateRecord
        self.record = RuntimeStateRecord(integration_id, state_key, schema_version, payload, observed_at, stale_after,
                                         successful_at, observed_at, observed_at,
                                         (self.record.generation if self.record else 0) + 1)
        return self.record


class HostClient:
    configured = True

    def __init__(self, fail=False):
        self.fail = fail
        self.fetches = 0

    def secrets(self):
        return ()

    async def hosts_domains(self):
        self.fetches += 1
        if self.fail:
            raise RealDebridAPIError(None, "", 503)
        return ["1fichier.com", "real-debrid.com"]

    async def hosts_regex(self):
        return PATTERNS


@pytest.mark.asyncio
async def test_maintenance_keeps_last_known_good_and_never_reads_transient_host_status():
    store = MemoryStore()
    now = [1000.0]
    first = RealDebridProvider(HostClient())
    maintenance = RealDebridHostMaintenance(first, store, clock=lambda: now[0])
    assert first.applicability.readiness == ApplicabilityReadiness.UNRESOLVED
    await maintenance.maintain()
    assert store.record.schema_version == HOST_SCHEMA_VERSION
    assert claimed(first.applicability_for, "https://1fichier.com/?x")[1] == ["1fichier.com"]
    # A rebuilt provider restores the persisted snapshot; a failed refresh of a
    # stale one keeps it routing rather than dropping the provider's routes.
    now[0] += 2 * 24 * 3600
    failing = HostClient(fail=True)
    second = RealDebridProvider(failing)
    await RealDebridHostMaintenance(second, store, clock=lambda: now[0]).maintain()
    assert failing.fetches == 1
    assert claimed(second.applicability_for, "https://1fichier.com/?x")[1] == ["1fichier.com"]
    assert not hasattr(HostClient, "hosts_status")


# --------------------------------------------------------------------------- #
# Provider: direct links, torrents, manifests, inventory, cleanup
# --------------------------------------------------------------------------- #

class FakeClient:
    configured = True

    def __init__(self, **responses):
        self.responses = responses
        self.calls = []

    def secrets(self):
        return ("refresh-token-value",)

    def _respond(self, name, *args):
        self.calls.append((name, *args))
        value = self.responses[name]
        if callable(value):
            value = value(*args)
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    async def unrestrict_link(self, link):
        return self._respond("unrestrict_link", link)

    async def add_magnet(self, magnet):
        return self._respond("add_magnet", magnet)

    async def add_torrent(self, data):
        return self._respond("add_torrent", data)

    async def select_files(self, native_id, files):
        return self._respond("select_files", native_id, files)

    async def torrent_info(self, native_id):
        return self._respond("torrent_info", native_id)

    async def torrents_page(self, page, limit=5000):
        return self._respond("torrents_page", page)

    async def delete_torrent(self, native_id):
        return self._respond("delete_torrent", native_id)

    async def user(self):
        return self._respond("user")


HOSTER = TransferRequest("https", "https://1fichier.com/?abc123", name="guess.bin")
UNRESTRICTED = {"id": "U1", "filename": "film.mkv", "filesize": 4096, "host": "1fichier.com", "chunks": 16,
                "download": "https://cdn.real-debrid.example/d/U1/film.mkv"}


@pytest.mark.asyncio
async def test_a_hoster_link_becomes_a_provider_issued_candidate_with_the_original_host_identity():
    provider = RealDebridProvider(FakeClient(unrestrict_link=UNRESTRICTED))
    result = await provider.resolve(HOSTER)
    (candidate,) = result.candidates
    assert result.state == ResourceState.AVAILABLE
    assert candidate.delivery == DeliveryKind.PROVIDER_ISSUED and candidate.provider_id == "realdebrid"
    assert candidate.source_identity == SourceIdentity("host", "1fichier.com") and candidate.refresh_request is HOSTER
    assert candidate.resolver_identity_evidence.resolved_name == "film.mkv"
    assert candidate.resolver_identity_evidence.exact_bytes == 4096
    nameless = RealDebridProvider(FakeClient(unrestrict_link={**UNRESTRICTED, "filesize": 0}))
    assert (await nameless.resolve(HOSTER)).candidates[0].resolver_identity_evidence is None
    refreshed = await provider.refresh(candidate.__class__(**{**candidate.__dict__, "relative_path": "a/b.mkv"}))
    assert refreshed.candidates[0].id == candidate.id and refreshed.candidates[0].relative_path == "a/b.mkv"


@pytest.mark.asyncio
@pytest.mark.parametrize("native, category", [
    ({**UNRESTRICTED, "download": "https://127.0.0.1/d/U1"}, Category.DESTINATION_BLOCKED),
    ({**UNRESTRICTED, "alternative": [{"download": "https://cdn.example/480p"}]}, Category.RESOLUTION_FAILED),
])
async def test_an_unsafe_or_ambiguous_unrestriction_fails_closed(native, category):
    with pytest.raises(TransferError) as failed:
        await RealDebridProvider(FakeClient(unrestrict_link=native)).resolve(HOSTER)
    assert failed.value.error.category == category


def info(status="downloaded", files=None, links=None, **extra):
    return {"id": "T1", "filename": "Root", "original_filename": "Root", "hash": "a" * 40, "bytes": 400,
            "progress": 100 if status == "downloaded" else 10, "status": status, "speed": 0,
            "files": files if files is not None else [], "links": links if links is not None else [], **extra}


@pytest.mark.asyncio
async def test_magnet_and_torrent_creation_select_nothing_upstream_and_report_unknown_cache():
    """Which files Real-Debrid fetches is DebridPulse's decision, synchronized
    once made: creating the torrent selects nothing."""
    for request, method in ((TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40, fingerprint="a" * 40),
                             "add_magnet"),
                            (TransferRequest("torrent", b"d4:infod4:name1:xee", "x.torrent"), "add_torrent")):
        client = FakeClient(**{method: {"id": "T1", "uri": "https://api.real-debrid.com/rest/1.0/torrents/info/T1"},
                               "torrent_info": info("waiting_files_selection")})
        result = await RealDebridProvider(client).resolve(request)
        assert [call[0] for call in client.calls] == [method, "torrent_info"]
        assert result.state == ResourceState.PREPARING and result.observation.request is request
        assert result.observation.resource.ownership == Ownership.CREATED
        assert result.observation.cache_presence == CachePresence.UNKNOWN


@pytest.mark.asyncio
async def test_a_torrent_waiting_for_its_selection_is_observed_with_its_selectable_file_list_unselected():
    client = FakeClient(add_magnet={"id": "T1"},
                        torrent_info=[info("magnet_conversion"),
                                      info("waiting_files_selection", files=[{**record, "selected": 0}
                                                                             for record in FILES])])
    provider = RealDebridProvider(client)
    result = await provider.resolve(TransferRequest("magnet", "magnet:?xt=urn:btih:" + "b" * 40))
    assert result.state == ResourceState.PREPARING and result.observation.file_manifest is None
    observation = await provider.observe(result.observation.resource)
    assert observation.state == ResourceState.PREPARING
    assert [entry.relative_path for entry in observation.file_manifest.entries] == [
        "A/same.bin", "B/same.bin", "skip.nfo", "unique-b.bin", "unique-a.bin"]
    assert not [call for call in client.calls if call[0] == "select_files"]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer, mutation", [
    (RealDebridProtocolError("Real-Debrid returned invalid JSON"), MutationOutcome.UNCERTAIN),
    ({"uri": "https://api.real-debrid.com/rest/1.0/torrents/info/"}, MutationOutcome.UNCERTAIN),
    (RealDebridAPIError(None, "", 524), MutationOutcome.UNCERTAIN),
    (RealDebridAPIError(21, "too_many_active_downloads", 509), MutationOutcome.NOT_COMMITTED),
    (RealDebridAPIError(19, "hoster_unavailable", 503), MutationOutcome.NOT_COMMITTED),
], ids=["unreadable-success", "success-naming-no-torrent", "uncoded-server-failure", "native-refusal",
        "coded-server-refusal"])
async def test_a_creation_without_a_usable_answer_says_whether_it_may_have_happened(answer, mutation):
    # Only Real-Debrid's own refusal proves nothing was created; an answer
    # that arrived but names no torrent, or a server failure page, does not.
    with pytest.raises(TransferError) as failed:
        await RealDebridProvider(FakeClient(add_magnet=answer)).resolve(
            TransferRequest("magnet", "magnet:?xt=urn:btih:" + "b" * 40))
    assert failed.value.error.mutation == mutation


UNUSABLE_TOKEN = (200, {"token_type": "Bearer"})                  # a token answer without a token


@pytest.mark.asyncio
@pytest.mark.parametrize("script, sent", [
    ({("POST", f"{OAUTH}/token"): [UNUSABLE_TOKEN]}, 0),
    ({("POST", f"{OAUTH}/token"): [TOKEN, UNUSABLE_TOKEN],
      ("POST", f"{API}/torrents/addMagnet"): [(401, {"error": "bad_token", "error_code": 8})]}, 1),
], ids=["refresh-before-the-create", "refresh-before-its-replay"])
async def test_a_creation_whose_token_refresh_failed_before_sending_was_not_committed(script, sent):
    # The create was never performed -- not sent at all, or refused for its
    # stale token and never replayed -- so nothing can have been created,
    # whatever the refresh failure itself was.
    client, transport = service(script)
    with pytest.raises(TransferError) as failed:
        await RealDebridProvider(client).resolve(TransferRequest("magnet", "magnet:?xt=urn:btih:" + "b" * 40))
    assert failed.value.error.mutation == MutationOutcome.NOT_COMMITTED
    assert failed.value.error.category == Category.PROVIDER_PROTOCOL_VIOLATION      # the cause, translated as ever
    assert len([call for call in transport.calls if call["url"].endswith("/torrents/addMagnet")]) == sent


@pytest.mark.asyncio
@pytest.mark.parametrize("refused, category", [
    (RealDebridAPIError(2, "parameter_missing", 400), Category.INVALID_REQUEST),
    (RealDebridAPIError(21, "too_many_active_downloads", 400), Category.CONCURRENCY_LIMITED),
    (RealDebridAPIError(25, "service_unavailable", 503), Category.PROVIDER_UNAVAILABLE),
    (RealDebridAPIError(999, "something_new", 400), Category.UNMAPPED_PROVIDER_ERROR),
], ids=["parameter-refusal", "unrelated-400", "server-refusal", "unknown-native"])
async def test_a_first_observation_refusal_hands_over_the_created_torrent_with_that_refusal(refused, category):
    # The torrent exists once add_magnet answered its id: a refused first
    # observation is Real-Debrid's answer, but never a reason to lose the only
    # record of the torrent. It crosses the durable boundary unready, carrying
    # the normalized refusal.
    client = FakeClient(add_magnet={"id": "T1"}, torrent_info=refused)
    request = TransferRequest("magnet", "magnet:?xt=urn:btih:" + "b" * 40)
    result = await RealDebridProvider(client).resolve(request)
    assert result.state == ResourceState.UNKNOWN and result.error is None
    assert result.observation.resource.context["id"] == "T1"
    assert result.observation.resource.ownership == Ownership.CREATED
    assert result.observation.error.category == category and result.observation.request is request


@pytest.mark.asyncio
@pytest.mark.parametrize("status, state, category", [
    ("downloaded", ResourceState.AVAILABLE, None),
    ("queued", ResourceState.PREPARING, None),
    ("uploading", ResourceState.PREPARING, None),
    ("magnet_error", ResourceState.UNAVAILABLE, Category.SOURCE_UNAVAILABLE),
    ("virus", ResourceState.UNAVAILABLE, Category.CONTENT_INVALID),
    ("dead", ResourceState.UNAVAILABLE, Category.SOURCE_UNAVAILABLE),
    ("error", ResourceState.UNAVAILABLE, Category.RESOLUTION_FAILED),
])
async def test_native_status_becomes_a_neutral_observation(status, state, category):
    provider = RealDebridProvider(FakeClient(torrent_info=info(status)))
    resource = provider_resource()
    observation = await provider.observe(resource)
    assert observation.state == state
    assert (observation.error.category if observation.error else None) == category
    # Even a finished torrent is no authoritative cache fact.
    assert observation.cache_presence == CachePresence.UNKNOWN


def provider_resource(ownership=Ownership.CREATED):
    from providers.realdebrid.translation import resource_from_native
    return resource_from_native({"id": "T1"}, ownership=ownership)


@pytest.mark.asyncio
async def test_an_absent_torrent_is_absent_and_cleanup_honours_cleanup_authority():
    gone = RealDebridAPIError(7, "unknown_ressource", 404)
    provider = RealDebridProvider(FakeClient(torrent_info=gone, delete_torrent=[None, gone]))
    assert (await provider.observe(provider_resource())).state == ResourceState.ABSENT
    owned = CleanupDirective(provider_resource(), CleanupAuthority.OWNED)
    assert (await provider.cleanup(owned)).kind == OutcomeKind.SUCCESS
    assert (await provider.cleanup(owned)).kind == OutcomeKind.SUCCESS          # already gone
    observed = CleanupDirective(provider_resource(Ownership.OBSERVED), CleanupAuthority.OWNED)
    assert (await provider.cleanup(observed)).kind == OutcomeKind.SKIPPED


@pytest.mark.asyncio
async def test_an_already_active_torrent_is_adopted_only_when_its_hash_names_exactly_one():
    active = RealDebridAPIError(33, "torrent_already_active", 400)
    client = FakeClient(add_magnet=active, torrents_page=[([info("downloading")], 1)], select_files=202,
                        torrent_info=info("downloading"))
    result = await RealDebridProvider(client).resolve(
        TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40, fingerprint="a" * 40))
    assert result.observation.resource.ownership == Ownership.ADOPTED
    stranger = FakeClient(add_magnet=RealDebridAPIError(33, "torrent_already_active", 400),
                          torrents_page=[([], 0)])
    with pytest.raises(TransferError) as refused:
        await RealDebridProvider(stranger).resolve(
            TransferRequest("magnet", "magnet:?xt=urn:btih:" + "c" * 40, fingerprint="c" * 40))
    assert refused.value.error.category == Category.RESOURCE_STATE_CONFLICT


# Root/A/same.bin, Root/B/same.bin (same name, different size), a file
# Real-Debrid did not select, then two unique members -- in native order.
FILES = [
    {"id": 1, "path": "/Root/A/same.bin", "bytes": 100, "selected": 1},
    {"id": 2, "path": "/Root/B/same.bin", "bytes": 200, "selected": 1},
    {"id": 3, "path": "/Root/skip.nfo", "bytes": 5, "selected": 0},
    {"id": 4, "path": "/Root/unique-b.bin", "bytes": 400, "selected": 1},
    {"id": 5, "path": "/Root/unique-a.bin", "bytes": 300, "selected": 1},
]
LINKS = [f"https://real-debrid.com/d/L{index}" for index in range(1, 5)]
UNRESTRICT = {
    LINKS[0]: {"filename": "same.bin", "filesize": 100, "download": "https://cdn.example/1"},
    LINKS[1]: {"filename": "same.bin", "filesize": 200, "download": "https://cdn.example/2"},
    LINKS[2]: {"filename": "unique-b.bin", "filesize": 400, "download": "https://cdn.example/3"},
    LINKS[3]: {"filename": "unique-a.bin", "filesize": 300, "download": "https://cdn.example/4"},
}


def torrent(files=FILES, links=LINKS, unrestrict=UNRESTRICT):
    return RealDebridProvider(FakeClient(torrent_info=info(files=files, links=links),
                                         unrestrict_link=lambda link: unrestrict[link]))


def unrestricted_links(provider):
    return [call[1] for call in provider.client.calls if call[0] == "unrestrict_link"]


@pytest.mark.asyncio
async def test_proven_links_keep_native_file_order_and_paths_match_the_early_manifest():
    provider = torrent()
    early = (await provider.observe(provider_resource())).file_manifest
    assert [entry.relative_path for entry in early.entries] == [
        "A/same.bin", "B/same.bin", "skip.nfo", "unique-b.bin", "unique-a.bin"]
    entries = await provider.manifest(provider_resource())
    # Native order kept (never sorted), the file no link proves absent, the root
    # wrapper removed once, and duplicate basenames kept apart by their path.
    assert [(entry.relative_path, entry.expected_bytes, entry.request.payload) for entry in entries] == [
        ("A/same.bin", 100, LINKS[0]), ("B/same.bin", 200, LINKS[1]),
        ("unique-b.bin", 400, LINKS[2]), ("unique-a.bin", 300, LINKS[3])]
    assert {entry.request.preferred_provider for entry in entries} == {"realdebrid"}
    # DebridPulse's own selection reconciles against the same coordinates and
    # never changes which link a member gets.
    chosen = [(normalize_relative_path("B/same.bin"), 200)]
    (selected,) = reconcile_executable_subset(chosen, entries)
    assert selected.request.payload == LINKS[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("unrestrict, failed_link", [
    # Same size, contradicting name: the supporting fact still refutes it.
    ({**UNRESTRICT, LINKS[2]: {**UNRESTRICT[LINKS[2]], "filename": "other.bin"}}, 2),
    # A matching name never overrides a size contradiction.
    ({**UNRESTRICT, LINKS[3]: {**UNRESTRICT[LINKS[3]], "filesize": 301}}, 3),
], ids=["name-contradiction", "size-contradiction"])
async def test_a_link_whose_identity_matches_no_file_fails_the_manifest_closed(unrestrict, failed_link):
    """T3."""
    provider = torrent(unrestrict=unrestrict)
    with pytest.raises(TransferError) as failed:
        await provider.manifest(provider_resource())
    error = failed.value.error
    assert (error.category, error.stage, error.diagnostic) == (
        Category.PROVIDER_PROTOCOL_VIOLATION, Stage.CANDIDATE_PREPARATION, "a torrent link matches no file")
    # Processing stops at the link that proved nothing: one unrestriction per
    # link examined, and its already-returned identity is the evidence.
    assert unrestricted_links(provider) == LINKS[:failed_link + 1]
    returned = unrestrict[LINKS[failed_link]]
    evidence = error.as_dict(diagnostics=True)["diagnostic_evidence"]
    assert evidence["link_identity"] == {
        "link_ordinal": failed_link, "reason": "no_member",
        "returned": {"filename": returned["filename"], "filesize": returned["filesize"]},
        "match_count": 0, "matched": [], "matched_omitted": 0,
        "restricted_link": {"ordinal": failed_link, "scheme": "https", "host": "real-debrid.com", "port": None,
                            "has_resource_component": True}}
    assert (evidence["native_selected_count"], evidence["link_count"]) == (4, 4)
    durable = codec.dump(error)
    assert "cdn.example" not in durable and "/d/L" not in durable


@pytest.mark.asyncio
async def test_links_are_identified_by_their_answer_never_by_their_position_and_unsafe_paths_fail_closed():
    """T6: the two same-name members' links swapped, and a file Real-Debrid
    did not select left without one -- each link is the member its answer
    proves."""
    swapped = {**UNRESTRICT, LINKS[0]: UNRESTRICT[LINKS[1]], LINKS[1]: UNRESTRICT[LINKS[0]]}
    files = [*FILES[:4], {**FILES[4], "selected": 0}]
    provider = torrent(files=files, links=LINKS[:3], unrestrict=swapped)
    entries = await provider.manifest(provider_resource())
    assert [(entry.relative_path, entry.expected_bytes, entry.request.payload) for entry in entries] == [
        ("A/same.bin", 100, LINKS[1]), ("B/same.bin", 200, LINKS[0]), ("unique-b.bin", 400, LINKS[2])]
    assert unrestricted_links(provider) == LINKS[:3]
    unsafe = [*FILES[:1], {"id": 9, "path": "/Root/../escape.bin", "bytes": 1, "selected": 1}]
    with pytest.raises(TransferError) as escaped:
        await torrent(files=unsafe, links=LINKS[:2]).manifest(provider_resource())
    assert escaped.value.error.category == Category.PATH_POLICY_VIOLATION
    assert (await torrent(files=unsafe).observe(provider_resource())).file_manifest is None


@pytest.mark.asyncio
async def test_an_identity_failure_records_the_native_facts_that_justified_it():
    unmatched = {"filename": "nothing.bin", "filesize": 7, "download": "https://cdn.example/x"}
    provider = torrent(links=LINKS[:3], unrestrict={**UNRESTRICT, LINKS[2]: unmatched})
    with pytest.raises(TransferError) as failed:
        await provider.manifest(provider_resource())
    error = failed.value.error
    assert error.category == Category.PROVIDER_PROTOCOL_VIOLATION
    evidence = error.as_dict(diagnostics=True).get("diagnostic_evidence")
    assert evidence, "the rejection carries no evidence of what Real-Debrid returned"
    assert {key: value for key, value in evidence.items() if key not in ("files", "links", "link_identity")} == {
        "provider_operation": "torrent_manifest", "native_status": "downloaded", "native_torrent_id": "T1",
        "native_file_count": 5, "native_selected_count": 4, "link_count": 3,
        "files_total": 5, "files_emitted": 5, "files_omitted": 0,
        "links_total": 3, "links_emitted": 3, "links_omitted": 0}
    # Native order, ids, member paths (the one interpretation), sizes and
    # selected flags exactly as the decision saw them.
    assert evidence["files"] == [
        {"ordinal": index, "native_id": record["id"], "relative_path": path, "bytes": record["bytes"],
         "selected": record["selected"] == 1}
        for index, (record, path) in enumerate(zip(FILES, (
            "A/same.bin", "B/same.bin", "skip.nfo", "unique-b.bin", "unique-a.bin")))]
    assert evidence["links"] == [{"ordinal": index, "scheme": "https", "host": "real-debrid.com", "port": None,
                                  "has_resource_component": True} for index in range(3)]
    assert evidence["link_identity"]["returned"] == {"filename": "nothing.bin", "filesize": 7}
    durable = codec.dump(error)
    assert "/d/L" not in durable and not re.search(r"real-debrid\.com/", durable) and "cdn.example" not in durable
    assert [call[0] for call in provider.client.calls] == ["torrent_info", *["unrestrict_link"] * 3]


@pytest.mark.asyncio
async def test_a_large_native_answer_is_recorded_within_the_evidence_bound():
    files = [{"id": index + 1, "path": f"/Root/Season 01/Episode {index:03d} of a long running series title.mkv",
              "bytes": 1_000_000 + index, "selected": 0 if index == 7 else 1} for index in range(200)]
    links = [f"https://real-debrid.com/d/LINK{index:03d}" for index in range(200)]
    unmatched = {"filename": "nothing.bin", "filesize": 7, "download": "https://cdn.example/x"}
    provider = torrent(files=files, links=links, unrestrict=dict.fromkeys(links, unmatched))
    with pytest.raises(TransferError) as failed:
        await provider.manifest(provider_resource())
    assert unrestricted_links(provider) == links[:1]
    evidence = failed.value.error.as_dict(diagnostics=True)["diagnostic_evidence"]
    assert (evidence["native_file_count"], evidence["native_selected_count"], evidence["link_count"]) == (200, 199, 200)
    assert evidence["files_total"] == evidence["links_total"] == 200
    assert evidence["_truncated"] is True
    for name in ("files", "links"):
        emitted = evidence[f"{name}_emitted"]
        assert 0 < emitted <= 64 and len(evidence[name]) == emitted
        assert evidence[f"{name}_omitted"] == 200 - emitted
        assert [item["ordinal"] for item in evidence[name]] == list(range(emitted))   # native-order prefix
    assert evidence["files"][7]["selected"] is False
    compact = json.dumps(evidence, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8")
    assert len(compact) <= 16_384
    assert "LINK0" not in codec.dump(failed.value.error)


@pytest.mark.asyncio
@pytest.mark.parametrize("links, shape", [
    (["https://real-debrid.com/d/L1", {"url": "https://real-debrid.com/d/OBJECT-SECRET"}, 7],
     {"links_container_type": "list", "links_total": 3, "link_element_types": ["str", "dict", "int"],
      "link_element_types_omitted": 0}),
    ("https://real-debrid.com/d/STRING-SECRET", {"links_container_type": "str"}),
], ids=["mixed-list", "not-a-list"])
async def test_malformed_links_record_only_their_shape(links, shape):
    provider = RealDebridProvider(FakeClient(torrent_info=info(files=FILES, links=links)))
    with pytest.raises(TransferError) as failed:
        await provider.manifest(provider_resource())
    error = failed.value.error
    assert (error.category, error.diagnostic) == (Category.PROVIDER_PROTOCOL_VIOLATION, "torrent links are malformed")
    assert error.as_dict(diagnostics=True)["diagnostic_evidence"] == {
        "provider_operation": "torrent_manifest", "native_status": "downloaded", "native_torrent_id": "T1", **shape}
    durable = codec.dump(error)
    assert "SECRET" not in durable and "/d/" not in durable


# Transfer 538: five native files, every one selected upstream, and one link.
# Which file a link is, is its unrestricted identity's answer -- never a count
# or an ordinal.
WHALE = "The.Whale.2022.HDR.2160p.WEB.H265-NAISU[TGx]"
MOVIE = "the.whale.2022.hdr.2160p.web.h265-naisu.mkv"
NFO = "the.whale.2022.hdr.2160p.web.h265-naisu.nfo"
WHALE_FILES = [
    {"id": 1, "path": "/NEW upcoming releases by Xclusive.txt", "bytes": 175, "selected": 1},
    {"id": 2, "path": "/[TGx]Downloaded from torrentgalaxy.to .txt", "bytes": 718, "selected": 1},
    {"id": 3, "path": f"/{MOVIE}", "bytes": 22_576_859_233, "selected": 1},
    {"id": 4, "path": f"/{NFO}", "bytes": 400, "selected": 1},
    {"id": 5, "path": "/the.whale.2022.hdr.2160p.web.h265-naisu.sfv", "bytes": 1890, "selected": 1},
]
WHALE_LINK = "https://real-debrid.com/d/WHALE538"
AS_MOVIE = {"filename": MOVIE, "filesize": 22_576_859_233, "download": "https://cdn.example/whale/movie"}


# What Real-Debrid holds once DebridPulse synchronized a movie-only selection.
WHALE_MOVIE_ONLY = [{**record, "selected": int(record["id"] == 3)} for record in WHALE_FILES]


def whale(links=(WHALE_LINK,), unrestrict=None, files=WHALE_FILES):
    answers = unrestrict if unrestrict is not None else {WHALE_LINK: AS_MOVIE}
    return RealDebridProvider(FakeClient(
        torrent_info=info(files=files, links=list(links), filename=WHALE, original_filename=WHALE),
        unrestrict_link=lambda link: answers[link]))


@pytest.mark.asyncio
async def test_the_one_link_of_transfer_538_is_proven_to_be_the_movie_by_its_identity():
    """FB-1 / T1, with only the movie selected upstream."""
    provider = whale(files=WHALE_MOVIE_ONLY)
    try:
        entries = await provider.manifest(provider_resource())
    except TransferError as exc:
        raise AssertionError((exc.error.category, exc.error.diagnostic, unrestricted_links(provider))) from None
    (entry,) = entries
    assert (entry.relative_path, entry.expected_bytes) == (MOVIE, 22_576_859_233)
    assert (entry.request.payload, entry.request.preferred_provider) == (WHALE_LINK, "realdebrid")
    assert unrestricted_links(provider) == [WHALE_LINK]


@pytest.mark.asyncio
async def test_the_one_link_may_prove_a_file_the_transfer_did_not_select_and_core_refuses_it():
    """T2: the adapter states what the link is; selection is core's."""
    nfo_only = [{**record, "selected": int(record["id"] == 4)} for record in WHALE_FILES]
    provider = whale(files=nfo_only,
                     unrestrict={WHALE_LINK: {"filename": NFO, "filesize": 400, "download": "https://cdn.example/n"}})
    (entry,) = await provider.manifest(provider_resource())
    assert (entry.relative_path, entry.expected_bytes, entry.request.payload) == (NFO, 400, WHALE_LINK)
    assert unrestricted_links(provider) == [WHALE_LINK]
    with pytest.raises(SelectionUnprovable) as refused:
        reconcile_executable_subset([(normalize_relative_path(MOVIE), 22_576_859_233)], (entry,))
    assert refused.value.reason == "selected_path_missing"


@pytest.mark.asyncio
async def test_a_link_whose_identity_matches_several_files_is_never_assigned_to_the_first():
    """T4: same basename and size in two directories."""
    files = [{"id": 1, "path": "/Root/CD1/disc.iso", "bytes": 700, "selected": 1},
             {"id": 2, "path": "/Root/CD2/disc.iso", "bytes": 700, "selected": 1},
             {"id": 3, "path": "/Root/readme.txt", "bytes": 9, "selected": 1}]
    link = "https://real-debrid.com/d/DISC"
    provider = torrent(files=files, links=[link],
                       unrestrict={link: {"filename": "disc.iso", "filesize": 700, "download": "https://cdn.example/d"}})
    with pytest.raises(TransferError) as failed:
        await provider.manifest(provider_resource())
    error = failed.value.error
    assert (error.category, error.diagnostic) == (Category.PROVIDER_PROTOCOL_VIOLATION,
                                                  "a torrent link matches more than one file")
    assert unrestricted_links(provider) == [link]
    identity = error.as_dict(diagnostics=True)["diagnostic_evidence"]["link_identity"]
    assert (identity["reason"], identity["match_count"], identity["matched_omitted"]) == ("ambiguous_member", 2, 0)
    assert identity["matched"] == [{"ordinal": 0, "native_id": 1, "relative_path": "CD1/disc.iso"},
                                   {"ordinal": 1, "native_id": 2, "relative_path": "CD2/disc.iso"}]
    assert "/d/DISC" not in codec.dump(error)


@pytest.mark.asyncio
async def test_two_links_proving_the_same_file_never_become_two_members():
    """T5."""
    second = "https://real-debrid.com/d/WHALE538-AGAIN"
    provider = whale(links=(WHALE_LINK, second), unrestrict={WHALE_LINK: AS_MOVIE, second: AS_MOVIE})
    with pytest.raises(TransferError) as failed:
        await provider.manifest(provider_resource())
    error = failed.value.error
    assert (error.category, error.diagnostic) == (Category.PROVIDER_PROTOCOL_VIOLATION,
                                                  "two torrent links match the same file")
    assert unrestricted_links(provider) == [WHALE_LINK, second]
    identity = error.as_dict(diagnostics=True)["diagnostic_evidence"]["link_identity"]
    assert (identity["link_ordinal"], identity["reason"], identity["proven_by_link_ordinal"]) == (
        1, "member_already_proven", 0)
    assert identity["matched"] == [{"ordinal": 2, "native_id": 3, "relative_path": MOVIE}]
    assert "WHALE538" not in codec.dump(error)


@pytest.mark.asyncio
async def test_every_file_with_its_own_link_in_any_order_is_emitted_in_native_order():
    """T6: five files, five links, ``links[]`` shuffled."""
    links = [f"https://real-debrid.com/d/W{record['id']}" for record in WHALE_FILES]
    answers = {link: {"filename": record["path"].lstrip("/"), "filesize": record["bytes"],
                      "download": f"https://cdn.example/{record['id']}"} for link, record in zip(links, WHALE_FILES)}
    shuffled = [links[3], links[0], links[4], links[2], links[1]]
    provider = whale(links=shuffled, unrestrict=answers)
    entries = await provider.manifest(provider_resource())
    assert [(entry.relative_path, entry.expected_bytes, entry.request.payload) for entry in entries] == [
        (record["path"].lstrip("/"), record["bytes"], link) for record, link in zip(WHALE_FILES, links)]
    assert unrestricted_links(provider) == shuffled


@pytest.mark.asyncio
async def test_an_unsafe_link_is_refused_by_network_safety_before_any_unrestriction():
    """T8: never classified as an identity mismatch."""
    provider = whale(links=(WHALE_LINK, "https://127.0.0.1/d/LOCAL"))
    with pytest.raises(TransferError) as failed:
        await provider.manifest(provider_resource())
    assert failed.value.error.category == Category.DESTINATION_BLOCKED
    assert unrestricted_links(provider) == []


@pytest.mark.asyncio
async def test_a_downloaded_torrent_without_links_fails_and_says_so():
    """T9: never an empty manifest."""
    provider = whale(links=())
    with pytest.raises(TransferError) as failed:
        await provider.manifest(provider_resource())
    error = failed.value.error
    assert (error.category, error.stage, error.origin, error.diagnostic) == (
        Category.PROVIDER_PROTOCOL_VIOLATION, Stage.CANDIDATE_PREPARATION, Origin.PROVIDER,
        "torrent has no executable links")
    assert provider_attributable(error)
    evidence = error.as_dict(diagnostics=True)["diagnostic_evidence"]
    assert (evidence["native_file_count"], evidence["native_selected_count"], evidence["link_count"]) == (5, 5, 0)
    assert evidence["links"] == [] and len(evidence["files"]) == 5
    assert unrestricted_links(provider) == []


@pytest.mark.asyncio
async def test_a_single_file_torrent_keeps_its_own_name_and_a_shared_prefix_is_not_a_root():
    single = [{"id": 1, "path": "/Root", "bytes": 10, "selected": 1}]
    early = (await torrent(files=single).observe(provider_resource())).file_manifest
    assert [entry.relative_path for entry in early.entries] == ["Root"]
    shared = [{"id": 1, "path": "/Disc 1/a.flac", "bytes": 1, "selected": 1},
              {"id": 2, "path": "/Disc 1/b.flac", "bytes": 1, "selected": 1}]
    early = (await torrent(files=shared).observe(provider_resource())).file_manifest
    assert [entry.relative_path for entry in early.entries] == ["Disc 1/a.flac", "Disc 1/b.flac"]


@pytest.mark.asyncio
async def test_inventory_pages_until_complete_and_never_reads_a_malformed_page_as_empty():
    pages = FakeClient(torrents_page=[([info(), {**info(), "id": "T2"}], 3), ([{**info(), "id": "T3"}], 3)])
    snapshot = await RealDebridProvider(pages).inventory()
    assert snapshot.complete and len(snapshot.observations) == 3
    assert (await RealDebridProvider(FakeClient(torrents_page=[([], None)])).inventory()).complete
    for broken in ([(["not a record"], 1)], [([info()], 2), ([], 2)]):
        with pytest.raises(TransferError) as failed:
            await RealDebridProvider(FakeClient(torrents_page=broken)).inventory()
        assert failed.value.error.category == Category.PROVIDER_PROTOCOL_VIOLATION


# --------------------------------------------------------------------------- #
# Status truth
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_status_is_truthful_about_disablement_configuration_and_the_account():
    assert (await admin.runtime_status(None, enabled=False))["state"] == "disabled"
    unconfigured = RealDebridProvider(RealDebridService(None, rate_limiter=NoLimit(), transport=Transport({})))
    assert (await admin.runtime_status(unconfigured, enabled=True))["state"] == "unconfigured"
    healthy = await admin.runtime_status(RealDebridProvider(FakeClient(user={
        "username": "alice", "type": "premium", "premium": 86400, "expiration": "2027-01-31T10:00:00.000Z"})),
        enabled=True)
    neutral = healthy.pop("account")
    assert healthy == {"integration": "realdebrid", "state": "healthy", "checked": True, "username": "alice",
                       "account_type": "premium", "premium": True, "premium_seconds": 86400,
                       "expiration": "2027-01-31T10:00:00.000Z"}
    assert (neutral["service_class"], neutral["entitlement"], neutral["functional"]) == ("premium", "ready", "usable")
    revoked = RealDebridProvider(FakeClient(user=RealDebridAPIError(None, "oauth_grant_rejected", 400)))
    assert (await admin.runtime_status(revoked, enabled=True))["state"] == "auth_required"
    free = await admin.runtime_status(RealDebridProvider(FakeClient(user={"username": "bob", "type": "free"})),
                                      enabled=True)
    assert free["premium"] is False


def test_an_unconnected_provider_never_participates():
    unconnected = RealDebridProvider(RealDebridService(None, transport=Transport({})))
    assert unconnected.descriptor.enabled is False
    assert definition.default_enabled is False
    assert asyncio.iscoroutinefunction(RealDebridProvider.resolve)


# --------------------------------------------------------------------------- #
# Routes: the credential is saved and forgotten only through the canonical
# integration mutation
# --------------------------------------------------------------------------- #

class _Stored:
    """One in-memory saved configuration behind every settings read and write."""

    def __init__(self, enabled=True, **options):
        from core.config import AppSettings
        from integrations.definition import IntegrationSettings
        self.cfg = AppSettings(integrations={"realdebrid": IntegrationSettings(enabled=enabled, options=options)})

    def read(self):
        return self.cfg.model_copy(deep=True)

    def write(self, cfg):
        self.cfg = cfg


def _application(provider=None):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    @asynccontextmanager
    async def operation():
        yield

    return SimpleNamespace(
        definitions=(definition,), application_operation=operation, configuration_admission=operation,
        configure=lambda: None,
        apply_integration_configuration=AsyncMock(return_value=None), validate_configuration=AsyncMock(),
        notify_applicability_changed=lambda _identity: None,
        refresh_account_entitlement=AsyncMock(return_value=False),
        # No integration of this double gates an option on account truth.
        option_availability=lambda _definition: {},
        engine=SimpleNamespace(registry=SimpleNamespace(providers={"realdebrid": provider} if provider else {})))


def _settings_owner(stored):
    from contextlib import ExitStack
    from unittest.mock import patch
    stack = ExitStack()
    for target in ("api.routes", "core.config", "api.settings_validation_routes"):
        stack.enter_context(patch(f"{target}.get_settings", side_effect=stored.read))
    for target in ("api.routes", "core.config"):
        stack.enter_context(patch(f"{target}.load_settings", side_effect=stored.read))
        stack.enter_context(patch(f"{target}.save_settings", side_effect=stored.write))
        stack.enter_context(patch(f"{target}.apply_settings"))
    return stack


ACCOUNT = {"username": "alice", "account_type": "premium", "premium": True, "premium_seconds": 9,
           "expiration": "2027-01-31T10:00:00.000Z"}


@pytest.mark.asyncio
async def test_an_approved_device_is_saved_proven_and_never_echoed_to_the_browser():
    from unittest.mock import AsyncMock, patch
    from api import settings_validation_routes as routes

    stored = _Stored(enabled=False)
    granted = admin.Authorized(Credential("bound", "the-secret", "the-refresh"))
    with _settings_owner(stored), \
            patch.object(routes.realdebrid_admin, "poll_authorization", AsyncMock(return_value=granted)), \
            patch.object(routes.realdebrid_admin, "verify", AsyncMock(return_value=ACCOUNT)):
        result = await routes.poll_realdebrid_authorization(application=_application())
    assert result["state"] == "connected" and result["username"] == "alice"
    # Connecting a proven account is the decision to use it: configured,
    # verified and enabled, in canonical state and in the returned projection.
    projection = result["integration"]
    assert (projection["configured"], projection["verified"], projection["enabled"]) == (True, True, True)
    assert stored.cfg.integrations["realdebrid"].enabled is True
    assert "the-secret" not in json.dumps(result) and "the-refresh" not in json.dumps(result)
    assert stored.cfg.integrations["realdebrid"].options["refresh_token"] == "the-refresh"


@pytest.mark.asyncio
async def test_an_unproven_connection_or_a_pending_one_never_enables():
    from unittest.mock import AsyncMock, patch
    from api import settings_validation_routes as routes

    stored = _Stored(enabled=False)
    granted = admin.Authorized(Credential("bound", "the-secret", "the-refresh"))
    with _settings_owner(stored), \
            patch.object(routes.realdebrid_admin, "poll_authorization", AsyncMock(return_value=granted)), \
            patch.object(routes.realdebrid_admin, "verify", AsyncMock(side_effect=RealDebridAPIError(8, "bad_token", 401))):
        result = await routes.poll_realdebrid_authorization(application=_application())
    assert (result["integration"]["configured"], result["integration"]["verified"],
            result["integration"]["enabled"]) == (True, False, False)
    assert stored.cfg.integrations["realdebrid"].enabled is False
    pending = _Stored(enabled=False)
    with _settings_owner(pending), \
            patch.object(routes.realdebrid_admin, "poll_authorization", AsyncMock(return_value={"state": "pending"})):
        assert (await routes.poll_realdebrid_authorization(application=_application())) == {"state": "pending"}
    assert pending.cfg.integrations["realdebrid"].enabled is False
    assert pending.cfg.integrations["realdebrid"].options == {}


@pytest.mark.asyncio
async def test_a_test_of_an_intentionally_disabled_account_never_enables_it():
    from unittest.mock import AsyncMock, patch
    from api import settings_validation_routes as routes

    stored = _Stored(enabled=False, client_id="bound", client_secret="the-secret", refresh_token="the-refresh")
    with _settings_owner(stored), patch.object(routes.realdebrid_admin, "verify", AsyncMock(return_value=ACCOUNT)):
        result = await routes.validate_realdebrid(application=_application())
    assert result["integration"]["verified"] is True and result["integration"]["enabled"] is False
    assert stored.cfg.integrations["realdebrid"].enabled is False


def test_the_operator_tunables_have_their_defaults_and_bounds():
    defaults = RealDebridOptions()
    assert (defaults.rate_limit_per_minute, defaults.request_timeout_seconds,
            defaults.torrent_upload_timeout_seconds, defaults.host_refresh_interval_hours) == (240, 30, 120, 24)
    for field, low, high in (("rate_limit_per_minute", 1, 250), ("request_timeout_seconds", 5, 300),
                             ("torrent_upload_timeout_seconds", 30, 900), ("host_refresh_interval_hours", 1, 168)):
        RealDebridOptions(**{field: low}), RealDebridOptions(**{field: high})
        for bad in (low - 1, high + 1):
            with pytest.raises(ValueError):
                RealDebridOptions(**{field: bad})


@pytest.mark.asyncio
async def test_the_timeouts_reach_their_operations_and_the_refresh_interval_reaches_maintenance():
    from types import SimpleNamespace
    from providers.realdebrid.definition import build
    client, transport = service({
        ("POST", f"{OAUTH}/token"): [TOKEN],
        ("GET", f"{API}/user"): [(200, {"username": "alice"})],
        ("PUT", f"{API}/torrents/addTorrent"): [(201, {"id": "T1"})],
    })
    tuned = RealDebridService(CREDENTIAL, rate_limiter=NoLimit(), transport=transport,
                              request_timeout_seconds=45, upload_timeout_seconds=600)
    await tuned.user()
    await tuned.add_torrent(b"d4:infod4:name1:xee")
    assert {call["url"].rsplit("/", 1)[-1]: call["timeout"].total for call in transport.calls} == {
        "token": 45, "user": 45, "addTorrent": 600}
    provider = build(RealDebridOptions(client_id="bound", client_secret="s", refresh_token="r",
                                       request_timeout_seconds=45, torrent_upload_timeout_seconds=600,
                                       host_refresh_interval_hours=6), SimpleNamespace())
    assert (provider.client.request_timeout.total, provider.client.upload_timeout.total) == (45, 600)
    assert provider.hosts._refresh_seconds == 6 * 3600


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", [None, RealDebridAPIError(None, "", 503)])
async def test_disconnect_forgets_the_credential_even_when_revocation_fails(revocation):
    from unittest.mock import AsyncMock
    from api import settings_validation_routes as routes

    stored = _Stored(client_id="bound", client_secret="the-secret", refresh_token="the-refresh")
    client = RealDebridService(CREDENTIAL, rate_limiter=NoLimit(), transport=Transport({}))
    order = []

    async def revoke():
        order.append(stored.cfg.integrations["realdebrid"].options["client_id"])
        if revocation is not None:
            raise revocation

    client.disable_access_token = AsyncMock(side_effect=revoke)
    with _settings_owner(stored):
        result = await routes.disconnect_realdebrid(application=_application(RealDebridProvider(client)))
    assert stored.cfg.integrations["realdebrid"].options["client_id"] == ""
    assert order == [""]                       # the local credential was gone before revocation was asked
    assert result["revoked"] is (revocation is None)
    assert result["integration"]["configured"] is False


def test_the_wrapper_is_the_neutral_rule_and_real_hierarchy_survives_it():
    """Real-Debrid reads ``files[]``; the wrapper decision is the neutral
    member-path rule's. A shared first directory that is not the torrent's
    name stays, a same-named inner directory stays, and native order holds."""
    from providers.realdebrid import translation
    from transfers import file_selection

    assert not hasattr(translation, "UnsafeMemberPath")
    shared = [{"path": "/Disc 1/b.flac", "bytes": 2, "selected": 1},
              {"path": "/Disc 1/a.flac", "bytes": 1, "selected": 1}]
    assert [member.relative_path for member in translation.native_members(shared, root_name="Root")] == [
        "Disc 1/b.flac", "Disc 1/a.flac"]
    nested = [{"path": "/Root/Root/CD1/x.flac", "bytes": 1, "selected": 1}]
    assert [member.relative_path for member in translation.native_members(nested, root_name="Root")] == [
        "Root/CD1/x.flac"]
    single = [{"path": "/Root", "bytes": 1, "selected": 1}]
    assert [member.relative_path for member in translation.native_members(single, root_name="Root")] == ["Root"]
    with pytest.raises(file_selection.ManifestInvalid):
        translation.native_members([{"path": "/Root//x.bin", "bytes": 1, "selected": 1}], root_name="Root")


# -- the created torrent's ownership survives its failed first observation ---------------------------------

@pytest.mark.asyncio
async def test_a_created_torrent_whose_first_observation_failed_stays_owned_resumes_and_is_cleaned_up(
        tmp_path, monkeypatch):
    """RD1 (and the neutral O1/O2 through the real adapter): once add_magnet
    answered the torrent's id, a failing first observation never loses it.
    The root holds the CREATED torrent durably and unready; the ordinary
    observation of a bound resource sees it progress; a restart observes it
    instead of adding it again; and removing the transfer cleans it up
    through the one cleanup owner."""
    from test_v113_standby_preparation import lab

    client = FakeClient(add_magnet={"id": "T1"},
                        torrent_info=[RealDebridAPIError(25, "service_unavailable", 503)] + [info("downloading")] * 9,
                        delete_torrent=None)
    request = TransferRequest("magnet", "magnet:?xt=urn:btih:" + "c" * 40, name="Root", fingerprint="c" * 40)
    repository, _registry, engine = await lab(tmp_path, monkeypatch, RealDebridProvider(client))
    transfer = await engine.submit((request,), name="Root", deduplicate=False)
    await engine.resolve_pending()

    def calls(name):
        return [call for call in client.calls if call[0] == name]

    (bound, state, _pending), = await repository.resources(transfer.id)
    assert (bound.provider_id, bound.context["id"], bound.ownership) == ("realdebrid", "T1", Ownership.CREATED)
    assert state == ResourceState.UNKNOWN                                # unready, never fabricated ready
    root = next(item for item in await repository.requests(transfer.id) if item.parent_id is None)
    assert root.state == "waiting" and root.resource.id == bound.id
    assert len(calls("add_magnet")) == 1

    await engine.resolve_pending()                                       # the ordinary bound observation
    (_resource, state, _pending), = await repository.resources(transfer.id)
    assert state == ResourceState.PREPARING                              # and the torrent progresses

    reopened, _registry, restarted = await lab(tmp_path, monkeypatch, RealDebridProvider(client), fresh=False)
    await restarted.resolve_pending()
    assert len(calls("add_magnet")) == 1                                 # observed, never added again
    assert transfer.id in {item.id for item in await reopened.active()}
    assert not calls("select_files")                                     # nothing to select: no file list

    await restarted.delete(transfer.id, remote=True)
    assert calls("delete_torrent") == [("delete_torrent", "T1")]


# --------------------------------------------------------------------------- #
# The whole bounded body
# --------------------------------------------------------------------------- #
#
# One StreamReader.read(n) returns what has arrived, not the body: TorBox and
# Debrid-Link were truncated exactly so (transfers 530/531). These run the real
# aiohttp transport against a local server writing each answer in delayed
# parts, cutting it off, trickling it, or streaming past the size bound.

class LocalRealDebrid:
    """Real-Debrid's API on 127.0.0.1: each route answers its scripted
    ``(how, status, body)`` -- ``parts`` (three delayed writes), ``cut`` (a
    prefix, then the connection dies), ``trickle`` (slowly) or ``flood``
    (megabyte writes past the bound)."""

    def __init__(self, script):
        self.script = {key: list(value) for key, value in script.items()}
        self.seen = []

    async def __aenter__(self):
        from aiohttp import web
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *_exc):
        await self.runner.cleanup()

    def service(self, **options):
        from providers.realdebrid.client import aiohttp_transport

        async def local(method, url, **kwargs):
            return await aiohttp_transport(method, url.replace("https://api.real-debrid.com", self.origin), **kwargs)
        return RealDebridService(CREDENTIAL, rate_limiter=NoLimit(), transport=local, clock=lambda: 1000.0,
                                 **options)

    async def handle(self, request):
        from aiohttp import web
        self.seen.append((request.method, request.path))
        how, status, body = self.script[(request.method, request.path)].pop(0)
        response = web.StreamResponse(status=status, headers={"Content-Type": "application/json"})
        await response.prepare(request)
        third = max(1, len(body) // 3)
        try:
            if how == "flood":
                for _ in range(24):
                    await response.write(b" " * (1 << 20))
                await response.write_eof()
                return response
            if how == "cut":
                await response.write(body[:third])
                await asyncio.sleep(0.05)
                request.transport.close()
                return response
            step, pause = (8, 0.1) if how == "trickle" else (third, 0.05)
            for start in range(0, len(body), step):
                await response.write(body[start:start + step])
                await asyncio.sleep(pause)
            await response.write_eof()
        except (ConnectionError, RuntimeError):
            pass
        return response


REFRESHED = ("parts", 200, json.dumps(TOKEN[1]).encode())
TOKEN_ROUTE = ("POST", "/oauth/v2/token")
INFO_ROUTE = ("GET", "/rest/1.0/torrents/info/T1")
ADD_ROUTE = ("POST", "/rest/1.0/torrents/addMagnet")


def large_info():
    files = [{"id": index, "path": f"/Show/S{index // 20 + 1}/e{index:03d}.mkv", "bytes": 1846517, "selected": 1}
             for index in range(1, 201)]
    return json.dumps({"id": "T1", "filename": "Show", "hash": "a" * 40, "bytes": 1846517 * 200,
                       "status": "downloaded", "progress": 100, "files": files,
                       "links": [f"https://real-debrid.com/d/{index}" for index in range(200)]}).encode()


def added():
    return json.dumps({"id": "T1", "uri": "https://api.real-debrid.com/rest/1.0/torrents/info/T1",
                       "pad": "p" * 300}).encode()


@pytest.mark.asyncio
async def test_an_answer_written_in_parts_is_read_through_its_end():
    """RD-R1: strictly valid JSON, longer than its first write."""
    body = large_info()
    assert len(json.loads(body)["files"]) == 200
    async with LocalRealDebrid({TOKEN_ROUTE: [REFRESHED], INFO_ROUTE: [("parts", 200, body)]}) as remote:
        info = await remote.service().torrent_info("T1")
    assert info["id"] == "T1" and len(info["files"]) == 200


@pytest.mark.asyncio
async def test_an_oversized_streamed_answer_stays_bounded():
    """RD-R2: the client stops once past the bound; nothing is decoded."""
    async with LocalRealDebrid({TOKEN_ROUTE: [REFRESHED], INFO_ROUTE: [("flood", 200, b"")]}) as remote:
        with pytest.raises(RealDebridProtocolError) as caught:
            await remote.service().torrent_info("T1")
    assert "oversized response" in str(caught.value)


@pytest.mark.asyncio
async def test_an_answer_cut_off_mid_body_is_a_transport_failure_never_a_partial_answer():
    """RD-R3 / RD-R4: a create whose answer dies after the request was sent
    stays UNCERTAIN and is not repeated; the same on a read is an ordinary
    network failure with no mutation."""
    async with LocalRealDebrid({TOKEN_ROUTE: [REFRESHED], ADD_ROUTE: [("cut", 200, added())],
                                INFO_ROUTE: [("cut", 200, large_info())]}) as remote:
        provider = RealDebridProvider(remote.service())
        with pytest.raises(TransferError) as creating:
            await provider.resolve(TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40, "Show", "a" * 40))
        with pytest.raises(TransferError) as reading:
            await provider.observe(provider_resource())
    assert creating.value.error.category == Category.CONNECTION_FAILED
    assert creating.value.error.mutation == MutationOutcome.UNCERTAIN
    assert reading.value.error.category == Category.CONNECTION_FAILED
    assert reading.value.error.mutation == MutationOutcome.NOT_COMMITTED
    assert remote.seen.count(ADD_ROUTE) == 1


@pytest.mark.asyncio
async def test_a_trickled_answer_is_bounded_by_the_total_timeout():
    """RD-R5: the existing total timeout covers the whole body."""
    import time
    async with LocalRealDebrid({TOKEN_ROUTE: [REFRESHED], ADD_ROUTE: [("trickle", 200, added())],
                                INFO_ROUTE: [("trickle", 200, large_info())]}) as remote:
        provider = RealDebridProvider(remote.service(request_timeout_seconds=0.5))
        started = time.monotonic()
        with pytest.raises(TransferError) as creating:
            await provider.resolve(TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40, "Show", "a" * 40))
        with pytest.raises(TransferError) as reading:
            await provider.observe(provider_resource())
        elapsed = time.monotonic() - started
    assert creating.value.error.category == Category.CONNECTION_TIMEOUT
    assert creating.value.error.mutation == MutationOutcome.UNCERTAIN
    assert reading.value.error.category == Category.CONNECTION_TIMEOUT
    assert elapsed < 10
