"""Real-Debrid provider: native REST/OAuth mechanics behind neutral contracts.

Native HTTP is replaced at the client boundary (an injected transport) or the
provider's client is a fake; nothing here waits for a real OAuth interval."""
from __future__ import annotations

import asyncio
import json
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
from transfers.errors import Category, Retryability, TransferError
from transfers.file_selection import normalize_relative_path, reconcile_executable_subset
from transfers.models import (
    CachePresence, CleanupAuthority, CleanupDirective, DeliveryKind, OutcomeKind, Ownership, ResourceState,
    SourceIdentity, TransferRequest,
)

CREDENTIAL = Credential("bound-client", "client-secret-value", "refresh-token-value")


class Transport:
    """Scripted native HTTP: each call takes the next response for its route."""

    def __init__(self, script):
        self.script = {key: list(value) for key, value in script.items()}
        self.calls = []

    async def __call__(self, method, url, *, headers=None, params=None, data=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "params": dict(params or {}), "data": data})
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

    async def select_files(self, native_id, files="all"):
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
async def test_magnet_and_torrent_creation_select_every_file_upstream_and_report_unknown_cache():
    for request, method in ((TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40, fingerprint="a" * 40),
                             "add_magnet"),
                            (TransferRequest("torrent", b"d4:infod4:name1:xee", "x.torrent"), "add_torrent")):
        client = FakeClient(**{method: {"id": "T1", "uri": "https://api.real-debrid.com/rest/1.0/torrents/info/T1"},
                               "select_files": 202, "torrent_info": info("downloading")})
        result = await RealDebridProvider(client).resolve(request)
        assert ("select_files", "T1", "all") in client.calls
        assert result.state == ResourceState.PREPARING and result.observation.request is request
        assert result.observation.resource.ownership == Ownership.CREATED
        assert result.observation.cache_presence == CachePresence.UNKNOWN


@pytest.mark.asyncio
async def test_a_selection_refused_before_the_file_list_exists_is_made_when_real_debrid_waits_for_it():
    client = FakeClient(add_magnet={"id": "T1"}, select_files=[RealDebridAPIError(2, "parameter_missing", 400), 204],
                        torrent_info=[info("magnet_conversion"), info("magnet_conversion"),
                                      info("waiting_files_selection")])
    provider = RealDebridProvider(client)
    result = await provider.resolve(TransferRequest("magnet", "magnet:?xt=urn:btih:" + "b" * 40))
    assert result.state == ResourceState.PREPARING
    observation = await provider.observe(result.observation.resource)
    assert observation.state == ResourceState.PREPARING
    assert [call for call in client.calls if call[0] == "select_files"] == [("select_files", "T1", "all")] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("refused, status, category", [
    # The same parameter refusal once Real-Debrid has a file list is not timing.
    (RealDebridAPIError(2, "parameter_missing", 400), "downloading", Category.INVALID_REQUEST),
    (RealDebridAPIError(21, "too_many_active_downloads", 400), "magnet_conversion", Category.CONCURRENCY_LIMITED),
    (RealDebridAPIError(7, "unknown_ressource", 404), "magnet_conversion", Category.RESOURCE_NOT_FOUND),
    (RealDebridAPIError(999, "something_new", 400), "magnet_conversion", Category.UNMAPPED_PROVIDER_ERROR),
], ids=["parameter-refusal-after-conversion", "unrelated-400", "not-found", "unknown-native"])
async def test_every_other_selection_refusal_propagates(refused, status, category):
    client = FakeClient(add_magnet={"id": "T1"}, select_files=refused, torrent_info=info(status))
    with pytest.raises(TransferError) as failed:
        await RealDebridProvider(client).resolve(TransferRequest("magnet", "magnet:?xt=urn:btih:" + "b" * 40))
    assert failed.value.error.category == category


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


@pytest.mark.asyncio
async def test_links_pair_with_selected_files_by_native_ordinal_and_paths_match_the_early_manifest():
    provider = torrent()
    early = (await provider.observe(provider_resource())).file_manifest
    assert [entry.relative_path for entry in early.entries] == [
        "A/same.bin", "B/same.bin", "skip.nfo", "unique-b.bin", "unique-a.bin"]
    entries = await provider.manifest(provider_resource())
    # Native order kept (never sorted), the unselected file skipped, the root
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
@pytest.mark.parametrize("unrestrict", [
    # The two same-name members' links swapped: only the size tells them apart.
    {**UNRESTRICT, LINKS[0]: UNRESTRICT[LINKS[1]], LINKS[1]: UNRESTRICT[LINKS[0]]},
    # Same size, contradicting name: the supporting fact still refutes it.
    {**UNRESTRICT, LINKS[2]: {**UNRESTRICT[LINKS[2]], "filename": "other.bin"}},
    # A matching name never overrides a size contradiction.
    {**UNRESTRICT, LINKS[3]: {**UNRESTRICT[LINKS[3]], "filesize": 301}},
], ids=["swapped-duplicates", "name-contradiction", "size-contradiction"])
async def test_a_member_link_that_is_not_its_file_fails_the_manifest_closed(unrestrict):
    with pytest.raises(TransferError) as failed:
        await torrent(unrestrict=unrestrict).manifest(provider_resource())
    assert failed.value.error.category == Category.PROVIDER_PROTOCOL_VIOLATION


@pytest.mark.asyncio
async def test_counts_that_do_not_reconcile_and_unsafe_paths_fail_closed():
    with pytest.raises(TransferError) as short:
        await torrent(links=LINKS[:3]).manifest(provider_resource())
    assert short.value.error.category == Category.PROVIDER_PROTOCOL_VIOLATION
    unsafe = [*FILES[:1], {"id": 9, "path": "/Root/../escape.bin", "bytes": 1, "selected": 1}]
    with pytest.raises(TransferError) as escaped:
        await torrent(files=unsafe, links=LINKS[:2]).manifest(provider_resource())
    assert escaped.value.error.category == Category.PATH_POLICY_VIOLATION
    assert (await torrent(files=unsafe).observe(provider_resource())).file_manifest is None


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
    assert healthy == {"integration": "realdebrid", "state": "healthy", "checked": True, "username": "alice",
                       "account_type": "premium", "premium": True, "premium_seconds": 86400,
                       "expiration": "2027-01-31T10:00:00.000Z"}
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

    def __init__(self, **options):
        from core.config import AppSettings
        from integrations.definition import IntegrationSettings
        self.cfg = AppSettings(integrations={"realdebrid": IntegrationSettings(enabled=True, options=options)})

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
        definitions=(definition,), application_operation=operation, configure=lambda: None,
        apply_integration_configuration=AsyncMock(return_value=None), validate_configuration=AsyncMock(),
        notify_applicability_changed=lambda _identity: None,
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

    stored = _Stored()
    granted = admin.Authorized(Credential("bound", "the-secret", "the-refresh"))
    with _settings_owner(stored), \
            patch.object(routes.realdebrid_admin, "poll_authorization", AsyncMock(return_value=granted)), \
            patch.object(routes.realdebrid_admin, "verify", AsyncMock(return_value=ACCOUNT)):
        result = await routes.poll_realdebrid_authorization(application=_application())
    assert result["state"] == "connected" and result["username"] == "alice"
    assert result["integration"]["configured"] is True and result["integration"]["verified"] is True
    assert "the-secret" not in json.dumps(result) and "the-refresh" not in json.dumps(result)
    assert stored.cfg.integrations["realdebrid"].options["refresh_token"] == "the-refresh"


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
