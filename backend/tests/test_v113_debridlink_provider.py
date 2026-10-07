"""Debrid-Link provider: native REST mechanics behind neutral contracts.

Native HTTP is replaced at the client boundary (an injected transport); nothing
here talks to Debrid-Link. Routing, failover, placement, refresh, evidence and
file selection are the existing neutral owners, consumed unchanged.
"""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from integrations.account_entitlement import AccountEntitlementMaintenance
from integrations.catalog import definitions
from integrations.definition import verification_fingerprint
from integrations.runtime_state import ScopedRuntimeStateStore, credential_scope
from integrations.configuration import IntegrationSettings
from providers.alldebrid.provider import AllDebridProvider
from providers.debridlink import account as accounts
from providers.debridlink import admin
from providers.debridlink.client import (
    API, DebridLinkAPIError, DebridLinkService, RawResponse, aiohttp_transport, member_address, parse_member_address,
)
from providers.debridlink.definition import DebridLinkOptions, credential_material, definition
from providers.debridlink.host_runtime import (
    DebridLinkHostMaintenance, DebridLinkRequestApplicability, decode_host_snapshot, encode_host_snapshot,
    parse_native_host_snapshot,
)
from providers.debridlink.provider import DebridLinkProvider
from providers.debridlink.translation import LINKS, SEEDBOX, identity, translate_error
from providers.general_http.provider import GeneralHttpProvider
from providers.realdebrid.provider import RealDebridProvider
from providers.torbox.provider import TorBoxProvider
from transfers.applicability import (
    ApplicabilityReadiness, HostClaim, HostClaimScope, ProviderApplicability,
)
from transfers.entitlement import AccountServiceClass, ProviderEntitlements
from transfers.errors import Category, MutationOutcome, Retryability, TransferError
from transfers.models import (
    CachePresence, CleanupAuthority, CleanupDirective, DeliveryKind, OutcomeKind, Ownership, ResourceState,
    SourceIdentity, TransferRequest,
)
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

KEY = "dl-private-api-key-0123456789abcdef"
NOW = 1_800_000_000.0
HOSTER = TransferRequest("https", "https://hoster.example/f/abc123", "movie.mkv")
MAGNET = "magnet:?xt=urn:btih:" + "d" * 40 + "&dn=Show"


def ok(value, status=200, **extra):
    return status, {"success": True, "value": value, **extra}, {}


def refused(code, status=400, headers=None):
    return status, {"success": False, "error": code}, headers or {}


class Transport:
    """Scripted native HTTP: each call takes the next response for its route."""

    def __init__(self, script):
        self.script = {key: list(value) for key, value in script.items()}
        self.calls = []

    async def __call__(self, method, url, *, headers=None, params=None, data=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "params": dict(params or {}), "data": data})
        status, payload, response_headers = self.script[(method, url.removeprefix(API + "/"))].pop(0)
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return RawResponse(status, {key.casefold(): value for key, value in response_headers.items()}, body)


def service(script, *, key=KEY):
    transport = Transport(script)
    return DebridLinkService(key, transport=transport), transport


def link(link_id="aa11", *, name="movie.mkv", size=4096, url="https://hoster.example/f/abc123",
         download="https://dl6.debrid.link/dl/aa11/movie.mkv", expired=False):
    return {"id": link_id, "name": name, "size": size, "url": url, "downloadUrl": download,
            "expired": expired, "chunk": 8, "host": "hoster", "created": 1}


def torrent(torrent_id="t0rr3nt", *, status=4, percent=40, files=None, name="Show", zipped=False):
    return {"id": torrent_id, "name": name, "hashString": "d" * 40, "status": status, "totalSize": 3000,
            "downloadPercent": percent, "downloadSpeed": 10, "isZip": zipped,
            "files": files if files is not None else [
                {"id": f"{torrent_id}-1", "name": "Show/e01.mkv", "size": 1000, "downloadPercent": percent,
                 "downloadUrl": f"https://seed20.debrid.link/dl/{torrent_id}-1"},
                {"id": f"{torrent_id}-2", "name": "Show/Extras/e02.mkv", "size": 2000, "downloadPercent": percent,
                 "downloadUrl": f"https://seed20.debrid.link/dl/{torrent_id}-2"}]}


def provider_with(script):
    client, transport = service(script)
    return DebridLinkProvider(client), transport


# -- definition, configuration, credential -------------------------------------------

async def test_debridlink_registers_once_through_the_ordinary_catalog():
    assert [item.id for item in definitions].count("debridlink") == 1
    assert definition.name == "Debrid-Link" and definition.kind == "provider"
    assert definition.default_enabled is False
    assert definition.secret_fields == frozenset({"api_key"}) == definition.ownership_fields
    assert definition.presentation.status_endpoint == "/integration-status/debridlink"
    assert definition.presentation.premium is True


async def test_the_api_key_is_redacted_and_never_in_a_repr():
    public = definition.public_options({"api_key": KEY})
    assert public["api_key"] == "" and public["api_key_configured"] is True
    assert KEY not in repr(DebridLinkOptions(api_key=KEY))


async def test_verification_is_bound_to_the_key_alone():
    options = DebridLinkOptions(api_key=KEY)
    [subject] = definition.verification_subjects(options)
    assert subject.material == credential_material(options) == {"api_key": KEY}
    tuned = options.model_copy(update={"request_timeout_seconds": 90, "host_refresh_interval_hours": 2})
    assert verification_fingerprint(credential_material(tuned)) == verification_fingerprint(subject.material)
    changed = options.model_copy(update={"api_key": KEY + "-rotated"})
    assert verification_fingerprint(credential_material(changed)) != verification_fingerprint(subject.material)
    assert definition.verification_subjects(DebridLinkOptions()) == ()


@pytest.mark.parametrize("enabled, key, participates", [
    (True, KEY, True), (False, KEY, False), (True, "", False), (False, "", False)])
async def test_participation_is_enabled_and_configured_exactly_like_the_other_providers(enabled, key, participates):
    built = definition.build(IntegrationSettings(enabled=enabled, options={"api_key": key}), SimpleNamespace())
    assert built.descriptor.enabled is participates
    assert built.descriptor.request_types == frozenset({"magnet", "torrent", "http", "https"})


async def test_the_key_travels_only_as_a_bearer_header_and_never_in_an_error():
    client, transport = service({
        ("GET", "account/infos"): [ok({"accountType": 1, "premiumLeft": 86400, "username": "amy"}),
                                   refused("badToken", 401)],
        ("GET", "downloader/hosts"): [ok([])],
    })
    await client.account()
    await client.hosts()
    assert transport.calls[0]["headers"]["Authorization"] == f"Bearer {KEY}"
    assert KEY not in json.dumps(transport.calls[0]["params"])
    assert "Authorization" not in transport.calls[1]["headers"]  # the public catalogue carries no account
    with pytest.raises(DebridLinkAPIError) as caught:
        await client.account()
    error = translate_error(caught.value, secrets=client.secrets())
    assert error.category == Category.CREDENTIAL_INVALID
    assert KEY not in json.dumps(error.as_dict(diagnostics=True), default=str)


async def test_status_never_carries_the_key():
    provider, _ = provider_with({("GET", "account/infos"): [ok({"accountType": 2, "premiumLeft": -1,
                                                                 "username": "amy", "email": "a***@x"})]})
    status = await admin.runtime_status(provider, enabled=True)
    assert status["state"] == "healthy" and KEY not in json.dumps(status)
    assert set(status) == {"integration", "state", "checked", "username", "account"}
    assert await admin.runtime_status(provider, enabled=False) == {
        "integration": "debridlink", "state": "disabled", "checked": False}


# -- routing: a disabled or unconfigured Debrid-Link is a complete no-op ---------------

def _claims(host="hoster.example"):
    return ProviderApplicability(specialized_hosts=(HostClaim(host, HostClaimScope.EXACT, frozenset({"https"})),),
                                 specialized=True, readiness=ApplicabilityReadiness.READY)


def _existing():
    """AllDebrid, Real-Debrid and TorBox, each authoritatively claiming
    hoster.example through its own published applicability seam."""
    alldebrid = AllDebridProvider(KEY)
    realdebrid = RealDebridProvider(SimpleNamespace(configured=True, secrets=lambda: ()))
    torbox = TorBoxProvider(SimpleNamespace(configured=True, secrets=lambda: ()))
    for provider in (alldebrid, realdebrid, torbox):
        provider.applicability = _claims()
        provider.applicability_for = lambda _request, facts=_claims(): facts
    return alldebrid, realdebrid, torbox


def _registry(*extra):
    registry = IntegrationRegistry()
    for provider in (*_existing(), GeneralHttpProvider(), *extra):
        registry.register_provider(provider)
    return registry


REQUESTS = (
    HOSTER,
    TransferRequest("https", "https://unclaimed.example/file.bin", "file.bin"),
    TransferRequest("magnet", MAGNET, "Show"),
    TransferRequest("torrent", b"d4:infod4:name4:showee", "show.torrent"),
)


def _outcome(registry):
    return [[provider.descriptor.id for provider in registry.eligible_providers(request)] for request in REQUESTS]


def _disabled():
    built = definition.build(IntegrationSettings(enabled=False, options={"api_key": KEY}), SimpleNamespace())
    return built  # its host maintenance never loaded: an unresolved specialized competitor if it counted


def _unconfigured():
    provider = DebridLinkProvider(DebridLinkService(""))
    DebridLinkHostMaintenance(provider, SimpleNamespace())
    return provider


@pytest.mark.parametrize("make", [_disabled, _unconfigured], ids=["disabled", "unconfigured"])
async def test_a_debridlink_that_cannot_participate_changes_no_selection(make):
    baseline = _outcome(_registry())
    with_debridlink = _registry(make())
    assert _outcome(with_debridlink) == baseline
    assert baseline == [["alldebrid", "realdebrid", "torbox"], ["general_http"],
                        ["alldebrid", "realdebrid", "torbox"], ["alldebrid", "realdebrid", "torbox"]]
    for request in REQUESTS:
        assert with_debridlink.provider_for(request).descriptor.id == _registry().provider_for(request).descriptor.id


async def test_four_equal_debrid_claimants_compete_in_the_neutral_selector_order():
    debridlink = DebridLinkProvider(DebridLinkService(KEY))
    debridlink.applicability = _claims()
    debridlink.applicability_for = lambda _request: _claims()
    registry = _registry(debridlink)
    order = [provider.descriptor.id for provider in registry.eligible_providers(HOSTER)]
    # Equal priority, class, specificity and entitlement: the neutral identity
    # tie-break is the whole decision -- no provider preference exists.
    assert order == ["alldebrid", "debridlink", "realdebrid", "torbox"]
    assert {provider.descriptor.priority for provider in registry.providers.values()} == {0}
    # The operator's preference is still the one thing that moves a provider.
    preferred = replace(HOSTER, preferred_provider="torbox")
    assert registry.provider_for(preferred).descriptor.id == "torbox"


async def test_specialized_collection_ownership_never_reopens_generic_http_for_debridlink():
    debridlink = DebridLinkProvider(DebridLinkService(KEY))
    debridlink.applicability_for = lambda request: (_claims() if "hoster.example" in str(request.payload)
                                                    else ProviderApplicability(specialized=True,
                                                                               readiness=ApplicabilityReadiness.READY))
    registry = IntegrationRegistry()
    registry.register_provider(debridlink)
    registry.register_provider(GeneralHttpProvider())
    unclaimed = TransferRequest("https", "https://unclaimed.example/junk.html", "junk.html")
    assert [p.descriptor.id for p in registry.eligible_providers(unclaimed)] == ["general_http"]  # unowned still works
    assert registry.eligible_providers(unclaimed, generic_closed=True) == ()
    assert [p.descriptor.id for p in registry.eligible_providers(
        HOSTER, exhausted=frozenset({"debridlink"}), generic_closed=True)] == []


# -- supported hosts -------------------------------------------------------------------

HOSTS = [
    {"name": "hoster", "type": "host", "domains": ["hoster.example", "ho.example"],
     "regexs": [r"(https?:\/\/)?(www\.)?hoster\.example\/f\/[a-z0-9]+"]},
    {"name": "plain", "type": "host", "domains": ["plain.example"], "regexs": []},
    {"name": "broken", "type": "host", "domains": ["broken.example"], "regexs": ["(?<=lookbehind)x"]},
    {"name": "own", "type": "host", "domains": ["debrid-link.com"], "regexs": []},
    "not a record",
]


async def test_only_links_a_hoster_validator_accepts_are_claimed():
    applicability = DebridLinkRequestApplicability(parse_native_host_snapshot(HOSTS))

    def claimed(url):
        return [claim.host for claim in applicability(TransferRequest("https", url)).specialized_hosts]

    assert claimed("https://hoster.example/f/abc123") == ["hoster.example"]
    assert claimed("https://hoster.example/about") == []             # host, but not a link it can generate
    assert claimed("https://plain.example/anything") == ["plain.example"]  # a hoster with no validators
    assert claimed("https://broken.example/x") == []                  # its only validator is unusable
    assert claimed("https://elsewhere.example/f/abc123") == []        # never every HTTP URL
    member = member_address("t0rr3nt", "t0rr3nt-1")
    assert [c.host for c in applicability(TransferRequest("https", member)).specialized_hosts] == ["debrid-link.com"]
    assert DebridLinkRequestApplicability(None)(HOSTER).readiness == ApplicabilityReadiness.UNRESOLVED


async def test_the_host_snapshot_round_trips_and_a_useless_catalogue_is_refused():
    snapshot = parse_native_host_snapshot(HOSTS)
    assert decode_host_snapshot(encode_host_snapshot(snapshot)) == snapshot
    assert "debrid-link.com" not in snapshot.domains
    with pytest.raises(ValueError):
        parse_native_host_snapshot([{"type": "host", "domains": ["x"], "regexs": []}])


# -- direct hoster links -------------------------------------------------------------

async def test_a_single_file_link_is_one_transient_provider_issued_candidate():
    provider, transport = provider_with({("POST", "downloader/add"): [ok(link())]})
    result = await provider.resolve(HOSTER)
    [candidate] = result.candidates
    [endpoint] = candidate.endpoints
    assert endpoint.transient is True and candidate.refresh_request == HOSTER
    assert candidate.delivery == DeliveryKind.PROVIDER_ISSUED and candidate.provider_id == "debridlink"
    assert candidate.source_identity == SourceIdentity("host", "hoster.example")
    assert (candidate.name, candidate.expected_bytes) == ("movie.mkv", 4096)
    assert json.loads(transport.calls[0]["data"]) == {"url": HOSTER.payload}


async def test_refresh_issues_fresh_material_through_the_one_refresh_seam():
    provider, transport = provider_with({("POST", "downloader/add"): [
        ok(link()), ok(link(download="https://dl6.debrid.link/dl/aa11/fresh"))]})
    [first] = (await provider.resolve(HOSTER)).candidates
    [fresh] = (await provider.refresh(first)).candidates
    assert fresh.id == first.id and fresh.endpoints[0].address.endswith("/fresh") and fresh.endpoints[0].transient
    assert [call["url"] for call in transport.calls] == [f"{API}/downloader/add"] * 2


async def test_a_folder_link_is_a_resource_whose_files_are_members_never_mirrors():
    files = [link("aa11", name="a.bin", size=10, url="https://hoster.example/f/a1"),
             link("bb22", name="b.bin", size=20, url="https://hoster.example/f/b2")]
    provider, _ = provider_with({("POST", "downloader/add"): [ok(files)],
                                 ("GET", "downloader/list"): [ok(files), ok(files)]})
    result = await provider.resolve(HOSTER)
    assert result.candidates == ()
    assert result.state == ResourceState.AVAILABLE and result.observation.resource.ownership == Ownership.CREATED
    assert identity(result.observation.resource) == (LINKS, ("aa11", "bb22"))
    assert "downloadUrl" not in json.dumps(result.observation.resource.context)
    assert [(entry.name, entry.relative_path, entry.expected_bytes) for entry in result.observation.file_manifest.entries] \
        == [("a.bin", "a.bin", 10), ("b.bin", "b.bin", 20)]
    observed = await provider.observe(result.observation.resource)
    assert observed.state == ResourceState.AVAILABLE
    members = await provider.manifest(result.observation.resource)
    # Each member is its own ordinary request, materialized independently.
    assert [(m.relative_path, m.request.payload, m.request.preferred_provider) for m in members] == [
        ("a.bin", "https://hoster.example/f/a1", "debridlink"), ("b.bin", "https://hoster.example/f/b2", "debridlink")]


async def test_a_folder_whose_files_cannot_be_told_apart_fails_rather_than_guessing():
    same = [link("aa11", name="a.bin"), link("bb22", name="b.bin")]  # one URL for both
    provider, _ = provider_with({("POST", "downloader/add"): [ok(same)]})
    with pytest.raises(TransferError) as caught:
        await provider.resolve(HOSTER)
    assert caught.value.error.category == Category.RESOLUTION_FAILED
    assert caught.value.error.retryability == Retryability.NEVER


@pytest.mark.parametrize("code, category, retry", [
    ("hostNotValid", Category.UNSUPPORTED_REQUEST, Retryability.NEVER),
    ("maxLinkHost", Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),
    ("maxDataHost", Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),
    ("maxLink", Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),
    ("notFreeHost", Category.ACCOUNT_LIMITED, Retryability.AFTER_RESOURCE_CHANGE),
    ("maintenanceHost", Category.SOURCE_TEMPORARILY_UNAVAILABLE, Retryability.BACKOFF),
    ("fileNotFound", Category.SOURCE_NOT_FOUND, Retryability.NEVER),
    ("badFilePassword", Category.SOURCE_UNAVAILABLE, Retryability.NEVER),
    ("badToken", Category.CREDENTIAL_INVALID, Retryability.AFTER_REAUTH),
    ("serverNotAllowed", Category.AUTHORIZATION_FAILED, Retryability.AFTER_RESOURCE_CHANGE),
    ("freeServerOverload", Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF),
    ("somethingNew", Category.UNMAPPED_PROVIDER_ERROR, Retryability.UNKNOWN),
])
async def test_native_refusals_normalize_and_never_escape(code, category, retry):
    provider, _ = provider_with({("POST", "downloader/add"): [refused(code)]})
    with pytest.raises(TransferError) as caught:
        await provider.resolve(HOSTER)
    error = caught.value.error
    assert (error.category, error.retryability, error.integration_id) == (category, retry, "debridlink")
    assert error.native_code == code


async def test_a_flood_refusal_carries_the_servers_own_retry_delay():
    provider, _ = provider_with({("POST", "downloader/add"): [refused("floodDetected", 429, {"Retry-After": "90"})]})
    with pytest.raises(TransferError) as caught:
        await provider.resolve(HOSTER)
    assert caught.value.error.category == Category.RATE_LIMITED
    assert caught.value.error.retry_after_seconds == 90.0


# -- torrents --------------------------------------------------------------------------

async def test_a_magnet_creates_one_owned_torrent_observed_until_every_file_is_stored():
    preparing, stored = torrent(), torrent(status=100, percent=100)
    provider, transport = provider_with({("POST", "seedbox/add"): [ok(torrent(files=[]))],
                                         ("GET", "seedbox/list"): [ok([preparing]), ok([stored])]})
    result = await provider.resolve(TransferRequest("magnet", MAGNET, "Show"))
    resource = result.observation.resource
    assert result.state == ResourceState.PREPARING and result.observation.file_manifest is None
    assert resource.ownership == Ownership.CREATED and resource.context == {"family": SEEDBOX, "id": "t0rr3nt"}
    assert transport.calls[0]["data"] == {"url": MAGNET, "wait": "false"}  # a form; never native selection
    assert "Content-Type" not in transport.calls[0]["headers"]
    observed = await provider.observe(resource)
    assert observed.state == ResourceState.AVAILABLE and observed.fingerprint == "d" * 40
    assert observed.cache_presence == CachePresence.UNKNOWN
    assert [(entry.name, entry.relative_path, entry.expected_bytes) for entry in observed.file_manifest.entries] \
        == [("e01.mkv", "e01.mkv", 1000), ("e02.mkv", "Extras/e02.mkv", 2000)]
    from providers.debridlink.translation import seedbox_resource
    # Stable identity: the native torrent id alone, never a link or a secret.
    assert resource.id == seedbox_resource("t0rr3nt").id


async def test_a_created_torrent_whose_first_observation_failed_is_handed_over_unready():
    """DL1: Debrid-Link answered the new torrent's id, then its first read was
    a nominal success that is not JSON. The torrent is handed over as an
    owned, unready resource carrying that protocol violation -- never lost
    with the exception -- and the ordinary observation reads it next."""
    provider, _ = provider_with({("POST", "seedbox/add"): [ok(torrent(files=[]))],
                                 ("GET", "seedbox/list"): [(200, b"<html>busy</html>", {}), ok([torrent()])]})
    request = TransferRequest("magnet", MAGNET, "Show")
    result = await provider.resolve(request)
    assert result.state == ResourceState.UNKNOWN and result.error is None
    assert result.observation.resource.context == {"family": SEEDBOX, "id": "t0rr3nt"}
    assert result.observation.resource.ownership == Ownership.CREATED and result.observation.request is request
    assert result.observation.error.category == Category.PROVIDER_PROTOCOL_VIOLATION
    assert (await provider.observe(result.observation.resource)).state == ResourceState.PREPARING


async def test_a_nominal_success_create_answer_that_is_not_json_is_an_uncertain_protocol_violation():
    """DL4 and transfer 524's class: /seedbox/add answers JSON (Debrid-Link's
    v2 documentation), so a 2xx answer that is not JSON is never read as a
    creation -- no id is invented, nothing is handed over -- yet it is no
    proof the torrent was not created either. Debrid-Link's own refusal is."""
    provider, transport = provider_with({("POST", "seedbox/add"): [(200, b"<html>busy</html>", {}),
                                                                  refused("maxTorrent", 403)]})
    with pytest.raises(TransferError) as caught:
        await provider.resolve(TransferRequest("magnet", MAGNET, "Show"))
    assert caught.value.error.category == Category.PROVIDER_PROTOCOL_VIOLATION
    assert caught.value.error.mutation == MutationOutcome.UNCERTAIN
    assert [call["url"].removeprefix(API + "/") for call in transport.calls] == ["seedbox/add"]
    with pytest.raises(TransferError) as refusal:
        await provider.resolve(TransferRequest("magnet", MAGNET, "Show"))
    assert refusal.value.error.mutation == MutationOutcome.NOT_COMMITTED


async def test_a_torrent_upload_is_multipart_and_never_selects_files():
    provider, transport = provider_with({("POST", "seedbox/add"): [ok(torrent(files=[]))],
                                         ("GET", "seedbox/list"): [ok([torrent()])]})
    await provider.resolve(TransferRequest("torrent", b"d4:infod4:name4:showee", "show.torrent"))
    fields = {field[0]["name"]: field[2] for field in transport.calls[0]["data"]._fields}
    assert fields["file"] == b"d4:infod4:name4:showee" and fields["wait"] == "false"


async def on_the_wire(data):
    """What aiohttp actually sends for ``data``, received by a local server
    through this client's own transport: the Content-Type and the fields."""
    from aiohttp import web
    seen = {}

    async def receive(request):
        seen["content_type"] = request.headers.get("Content-Type", "")
        form = await request.post()
        seen["fields"] = {key: value.file.read() if hasattr(value, "file") else value for key, value in form.items()}
        return web.json_response(ok({})[1])

    app = web.Application()
    app.router.add_post("/", receive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        await aiohttp_transport("POST", f"http://127.0.0.1:{port}/", data=data)
    finally:
        await runner.cleanup()
    return seen


async def test_a_magnet_is_added_as_a_form_and_a_torrent_file_as_multipart_on_the_wire():
    """DL-FB1: seedbox/add takes form data, as every working seedbox client
    sends it -- never the magnet in JSON, and never the retired ``async``."""
    provider, transport = provider_with({("POST", "seedbox/add"): [ok(torrent(files=[])), ok(torrent(files=[]))],
                                         ("GET", "seedbox/list"): [ok([torrent()]), ok([torrent()])]})
    await provider.resolve(TransferRequest("magnet", MAGNET, "Show"))
    await provider.resolve(TransferRequest("torrent", b"d4:infod4:name4:showee", "show.torrent"))
    magnet = await on_the_wire(transport.calls[0]["data"])
    assert magnet == {"content_type": "application/x-www-form-urlencoded",
                      "fields": {"url": MAGNET, "wait": "false"}}
    upload = await on_the_wire(transport.calls[2]["data"])
    assert upload["content_type"].startswith("multipart/form-data; boundary=")
    assert upload["fields"] == {"file": b"d4:infod4:name4:showee", "wait": "false"}
    assert transport.calls[0]["headers"] == transport.calls[2]["headers"] == {"Authorization": f"Bearer {KEY}"}


async def test_a_redirected_create_is_an_uncertain_safe_protocol_fact_never_invalid_json():
    """HTTP-FB1 / DL-A5 / DL-A6: a 302 answer to seedbox/add is never decoded
    as JSON and never followed; it names what it was -- status, media type,
    the Location's scheme, host and path -- without the key, a query or a
    full body, and it is still no proof the torrent was not created."""
    location = f"https://debrid-link.fr/api/v2/seedbox/add?apikey={KEY}"
    provider, transport = provider_with({("POST", "seedbox/add"): [
        (302, b"<html><head><title>302 Found</title></head></html>",
         {"Content-Type": "text/html", "Location": location})]})
    with pytest.raises(TransferError) as caught:
        await provider.resolve(TransferRequest("magnet", MAGNET, "Show"))
    error = caught.value.error
    assert error.category == Category.PROVIDER_PROTOCOL_VIOLATION and error.mutation == MutationOutcome.UNCERTAIN
    text = error.diagnostic
    assert "invalid JSON" not in text
    for fact in ("POST /api/v2/seedbox/add", "redirect", "HTTP 302", "content-type=text/html",
                 "location-scheme=https", "location-host=debrid-link.fr", "location-path=/api/v2/seedbox/add",
                 "body-prefix=", "302 Found"):
        assert fact in text, fact
    rendered = json.dumps(error.as_dict(diagnostics=True), default=str)
    assert KEY not in rendered and "apikey" not in rendered
    assert len(transport.calls) == 1   # one native operation; nothing retried or followed


async def test_a_malformed_success_read_keeps_bounded_safe_evidence():
    provider, _ = provider_with({("GET", "seedbox/list"): [(200, b"<!doctype html><p>maintenance</p>",
                                                            {"Content-Type": "text/html"})]})
    from providers.debridlink.translation import seedbox_resource
    with pytest.raises(TransferError) as caught:
        await provider.observe(seedbox_resource("t0rr3nt"))
    text = caught.value.error.diagnostic
    assert caught.value.error.category == Category.PROVIDER_PROTOCOL_VIOLATION
    assert "GET /api/v2/seedbox/list" in text and "HTTP 200" in text and "not JSON" in text
    assert "content-type=text/html" in text and "maintenance" in text and "ids=" not in text


@pytest.mark.parametrize("status, category, mutation", [
    (502, Category.PROVIDER_UNAVAILABLE, MutationOutcome.UNCERTAIN),
    (403, Category.AUTHORIZATION_FAILED, MutationOutcome.NOT_COMMITTED),
])
async def test_a_malformed_error_status_keeps_its_classification_and_safe_evidence(status, category, mutation):
    page = f"<html><title>{status}</title>Bearer {KEY}</html>".encode()
    provider, transport = provider_with({("POST", "seedbox/add"): [(status, page, {"Content-Type": "text/html"})]})
    with pytest.raises(TransferError) as caught:
        await provider.resolve(TransferRequest("magnet", MAGNET, "Show"))
    error = caught.value.error
    assert (error.category, error.mutation, error.native_code) == (category, mutation, str(status))
    for fact in ("POST /api/v2/seedbox/add", f"HTTP {status}", "content-type=text/html", f"length={len(page)}",
                 "body-prefix=", f"<title>{status}</title>"):
        assert fact in error.diagnostic, fact
    assert KEY not in json.dumps(error.as_dict(diagnostics=True), default=str)
    assert len(transport.calls) == 1


async def test_the_native_transport_never_follows_a_redirect():
    from aiohttp import web
    followed = []

    async def moved(request):
        return web.Response(status=302, headers={"Location": "/elsewhere"}, content_type="text/html",
                            text="<html>moved</html>")

    async def elsewhere(request):
        followed.append(request.method)
        return web.json_response(ok({})[1])

    app = web.Application()
    app.router.add_route("*", "/add", moved)
    app.router.add_route("*", "/elsewhere", elsewhere)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        for method in ("POST", "GET"):
            answer = await aiohttp_transport(method, f"http://127.0.0.1:{port}/add", data=None)
            assert answer.status == 302 and answer.headers["location"] == "/elsewhere"
    finally:
        await runner.cleanup()
    assert followed == []


async def test_a_zip_listed_or_unstored_torrent_publishes_no_manifest():
    zipped = torrent(status=100, percent=100, zipped=True)
    partial = torrent(status=8, percent=100)
    partial["files"][1]["downloadPercent"] = 50
    provider, _ = provider_with({("GET", "seedbox/list"): [ok([zipped]), ok([partial])]})
    from providers.debridlink.translation import seedbox_resource
    for _ in range(2):
        observed = await provider.observe(seedbox_resource("t0rr3nt"))
        assert observed.state == ResourceState.PREPARING and observed.file_manifest is None


async def test_torrent_members_are_durable_addresses_resolved_to_fresh_transient_links():
    stored = torrent(status=100, percent=100)
    provider, _ = provider_with({("GET", "seedbox/list"): [ok([stored]), ok([stored])]})
    from providers.debridlink.translation import seedbox_resource
    members = await provider.manifest(seedbox_resource("t0rr3nt", ownership=Ownership.CREATED))
    assert [(m.relative_path, m.request.kind, m.request.preferred_provider) for m in members] == [
        ("e01.mkv", "https", "debridlink"), ("Extras/e02.mkv", "https", "debridlink")]
    assert parse_member_address(members[1].request.payload) == ("t0rr3nt", "t0rr3nt-2")
    assert "seed20" not in members[1].request.payload and KEY not in members[1].request.payload
    [candidate] = (await provider.resolve(members[1].request)).candidates
    assert candidate.endpoints[0].transient and candidate.endpoints[0].address.endswith("t0rr3nt-2")
    assert candidate.refresh_request == members[1].request and candidate.expected_bytes == 2000


async def test_a_member_address_is_exact():
    address = member_address("t0rr3nt", "t0rr3nt-2")
    assert parse_member_address(address) == ("t0rr3nt", "t0rr3nt-2")
    for other in (address + "&x=1", address.replace("https", "http"), address + "#f",
                  address.replace("debrid-link.com", "debrid-link.com.evil"), "https://hoster.example/f/abc123"):
        assert parse_member_address(other) is None


async def test_cleanup_is_idempotent_and_never_touches_an_observed_resource():
    from providers.debridlink.translation import links_resource, seedbox_resource
    provider, transport = provider_with({
        ("DELETE", "seedbox/t0rr3nt/remove"): [ok(["t0rr3nt"]), refused("badId")],
        ("DELETE", "downloader/aa11,bb22/remove"): [ok(["aa11", "bb22"])],
    })
    created = seedbox_resource("t0rr3nt", ownership=Ownership.CREATED)
    for _ in range(2):  # gone already is what cleanup wanted
        outcome = await provider.cleanup(CleanupDirective(created, CleanupAuthority.OWNED))
        assert outcome.kind == OutcomeKind.SUCCESS
    folder = links_resource(("aa11", "bb22"))
    assert (await provider.cleanup(CleanupDirective(folder, CleanupAuthority.OWNED))).kind == OutcomeKind.SUCCESS
    observed = seedbox_resource("t0rr3nt")
    assert (await provider.cleanup(CleanupDirective(observed, CleanupAuthority.OWNED))).kind == OutcomeKind.SKIPPED
    assert len(transport.calls) == 3


async def test_a_missing_torrent_is_absent():
    from providers.debridlink.translation import seedbox_resource
    provider, _ = provider_with({("GET", "seedbox/list"): [ok([]), refused("badId")]})
    for _ in range(2):
        assert (await provider.observe(seedbox_resource("t0rr3nt"))).state == ResourceState.ABSENT


async def test_inventory_pages_until_debridlink_says_it_ended():
    provider, transport = provider_with({("GET", "seedbox/list"): [
        ok([torrent("aaa")], pagination={"page": 0, "next": 1}),
        ok([torrent("bbb")], pagination={"page": 1, "next": -1})]})
    snapshot = await provider.inventory()
    assert snapshot.complete and [o.resource.context["id"] for o in snapshot.observations] == ["aaa", "bbb"]
    assert [call["params"]["page"] for call in transport.calls] == [0, 1]


# -- account and entitlement: no lifetime state; free accounts narrowed per hoster ------

OFFERED = frozenset({"magnet", "torrent", "http", "https"})


@pytest.mark.parametrize("native, service_class, kinds, expires, plan, degraded", [
    ({"accountType": 1, "premiumLeft": 30 * 86400}, AccountServiceClass.PREMIUM, OFFERED,
     (NOW + 30 * 86400) // 60 * 60, "Premium", False),
    # Type 2 has no Debrid-Link-defined meaning: a paid account whose end is
    # not reported -- premium, expiry unknown, never a never-ending state --
    # entitled to hoster use only; seedbox capability is not established.
    ({"accountType": 2, "premiumLeft": -1}, AccountServiceClass.PREMIUM, {"http", "https"}, None, "Premium",
     False),
    ({"accountType": 0, "premiumLeft": 0}, AccountServiceClass.STANDARD, {"http", "https"}, None, "Free", False),
    ({"accountType": 1, "premiumLeft": 0}, AccountServiceClass.STANDARD, {"http", "https"}, None, "Free", True),
    ({"accountType": 1, "premiumLeft": -5}, AccountServiceClass.STANDARD, {"http", "https"}, None, "Free", True),
])
async def test_account_types_translate_to_neutral_entitlement(native, service_class, kinds, expires, plan, degraded):
    value = accounts.entitlement(accounts.account_facts(native, now=NOW), offered=OFFERED, now=NOW)
    assert (value.service_class, value.request_types, value.expires_at, value.plan, value.degraded) \
        == (service_class, frozenset(kinds), expires, plan, degraded)


async def test_no_lifetime_state_exists_and_an_unreported_expiry_stays_unknown():
    assert not hasattr(ProviderEntitlements(), "lifetime")
    assert set(ProviderEntitlements().public()) == {"entitlement", "service_class", "functional", "request_types",
                                                    "plan", "expires_at"}
    undated = accounts.entitlement(accounts.account_facts({"accountType": 2, "premiumLeft": -1}, now=NOW),
                                   offered=OFFERED, now=NOW)
    assert undated.expires_at is None and undated.plan == "Premium"
    with pytest.raises(ValueError):
        accounts.account_facts({"accountType": 1}, now=NOW)                    # premium without time
    with pytest.raises(ValueError):
        accounts.account_facts({"accountType": 7, "premiumLeft": 0}, now=NOW)  # not a known type
    with pytest.raises(ValueError):
        accounts.account_facts({"account_type": 2, "premium_until": NOW + 60}, now=NOW)


class Store:
    def __init__(self, records=None):
        self.records = dict(records or {})

    async def load(self, integration_id, state_key):
        return self.records.get((integration_id, state_key))

    async def replace(self, integration_id, payload, *, schema_version, state_key, observed_at, successful_at,
                      stale_after, expected_generation):
        record = SimpleNamespace(payload=payload, schema_version=schema_version, generation=expected_generation + 1,
                                 stale_after=stale_after)
        self.records[(integration_id, state_key)] = record
        return record


FREE_HOSTS = [
    {"name": "freehost", "type": "host", "domains": ["free.example"], "regexs": [], "isFree": True},
    {"name": "paidhost", "type": "host", "domains": ["hoster.example"], "regexs": [], "isFree": False},
    {"name": "unsaid", "type": "host", "domains": ["unsaid.example"], "regexs": [], "isFree": "yes"},
]


def _with_account(native):
    client, _ = service({("GET", "account/infos"): [ok(native)]})
    provider = DebridLinkProvider(client)
    from providers.debridlink.host_runtime import applicability_facts
    snapshot = parse_native_host_snapshot(FREE_HOSTS)
    provider.applicability = applicability_facts(snapshot)
    provider.applicability_for = DebridLinkRequestApplicability(snapshot)
    provider.account = AccountEntitlementMaintenance(
        provider, accounts.DebridLinkAccountTranslation(client, clock=lambda: NOW),
        ScopedRuntimeStateStore(Store(), credential_scope("debridlink", KEY)), integration_id="debridlink",
        clock=lambda: NOW)
    return provider


async def test_the_host_catalogue_keeps_only_an_explicit_true_isfree():
    snapshot = parse_native_host_snapshot(FREE_HOSTS)
    # Kept as stated; a malformed flag is unknown, and only true is free.
    assert {hoster.domains[0]: hoster.free for hoster in snapshot.hosters} == {
        "free.example": True, "hoster.example": False, "unsaid.example": None}
    applicability = DebridLinkRequestApplicability(snapshot)
    assert applicability.host_free(TransferRequest("https", "https://unsaid.example/f/1")) is False
    assert decode_host_snapshot(encode_host_snapshot(snapshot)) == snapshot
    client, transport = service({("GET", "downloader/hosts"): [ok([])]})
    await client.hosts()
    assert "isFree" in transport.calls[0]["params"]["keys"].split(",")


async def test_a_free_account_begins_only_links_of_free_hosters():
    provider = _with_account({"accountType": 0, "premiumLeft": 0})
    await provider.account.refresh_now()
    assert provider.entitlement_for(TransferRequest("https", "https://free.example/f/1")) is True
    assert provider.entitlement_for(TransferRequest("https", "https://hoster.example/f/1")) is False
    assert provider.entitlement_for(TransferRequest("https", "https://unsaid.example/f/1")) is False
    assert provider.entitlement_for(TransferRequest("magnet", MAGNET)) is False


async def test_a_premium_account_begins_every_supported_hoster_and_unknown_truth_stays_unknown():
    premium = _with_account({"accountType": 1, "premiumLeft": 86400})
    await premium.account.refresh_now()
    assert premium.entitlement_for(TransferRequest("https", "https://hoster.example/f/1")) is True
    assert premium.entitlement_for(TransferRequest("magnet", MAGNET)) is True
    unknown = _with_account({"accountType": 1, "premiumLeft": 86400})  # never refreshed: no account truth
    assert unknown.entitlement_for(TransferRequest("https", "https://hoster.example/f/1")) is None


async def test_an_undated_paid_account_uses_every_hoster_but_begins_no_seedbox_acquisition():
    paid = _with_account({"accountType": 2, "premiumLeft": -1})
    await paid.account.refresh_now()
    assert paid.entitlement_for(TransferRequest("https", "https://hoster.example/f/1")) is True  # not isFree-bound
    assert paid.entitlement_for(TransferRequest("https", "https://free.example/f/1")) is True
    assert paid.entitlement_for(TransferRequest("magnet", MAGNET)) is False
    assert paid.entitlement_for(TransferRequest("torrent", b"d4:infoe", "x.torrent")) is False
    assert (paid.entitlements.degraded, paid.entitlements.expires_at) == (False, None)
    registry = IntegrationRegistry()
    _alldebrid, realdebrid, _torbox = _existing()
    registry.register_provider(realdebrid)
    registry.register_provider(paid)
    # Productive remote seedbox acquisition goes to a provider that can prove it.
    assert registry.provider_for(TransferRequest("magnet", MAGNET, "Show")).descriptor.id == "realdebrid"


async def test_a_free_debridlink_never_takes_a_premium_only_hoster_from_another_provider():
    """The neutral order puts Debrid-Link before Real-Debrid and TorBox; a
    free account must yield a hoster it cannot use BEFORE acquisition."""
    free = _with_account({"accountType": 0, "premiumLeft": 0})
    await free.account.refresh_now()
    free_link = TransferRequest("https", "https://free.example/f/1")

    def claims(_request):
        return ProviderApplicability(specialized_hosts=tuple(
            HostClaim(host, HostClaimScope.EXACT, frozenset({"https"})) for host in ("hoster.example", "free.example")),
            specialized=True, readiness=ApplicabilityReadiness.READY)

    registry = IntegrationRegistry()
    _alldebrid, realdebrid, torbox = _existing()
    for provider in (realdebrid, torbox):
        provider.applicability_for = claims
        registry.register_provider(provider)
    registry.register_provider(free)
    assert registry.provider_for(HOSTER).descriptor.id == "realdebrid"
    assert registry.provider_for(free_link).descriptor.id == "debridlink"
    # A member of a route that already exists is not new acquisition.
    assert "debridlink" in [p.descriptor.id for p in registry.eligible_providers(HOSTER, acquisition=False)]


async def test_a_last_known_good_debridlink_row_restores_its_exact_meaning():
    client, _ = service({})
    store = Store()

    def owner():
        return AccountEntitlementMaintenance(
            DebridLinkProvider(client), accounts.DebridLinkAccountTranslation(client, clock=lambda: NOW),
            ScopedRuntimeStateStore(store, credential_scope("debridlink", KEY)), integration_id="debridlink",
            clock=lambda: NOW)

    await owner().observe({"accountType": 2, "premiumLeft": -1})
    restored = owner()
    await restored.start()                                  # restores only; fetches nothing
    truth = restored.entitlements
    assert (truth.service_class, truth.expires_at, truth.plan) == (AccountServiceClass.PREMIUM, None, "Premium")
    assert truth.request_types == frozenset({"http", "https"})
    assert set(truth.public()) == {"entitlement", "service_class", "functional", "request_types", "plan",
                                   "expires_at"}
