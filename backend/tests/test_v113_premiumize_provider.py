"""Premiumize provider: native REST mechanics behind neutral contracts.

Native HTTP is replaced at the client boundary (an injected transport), or
served by a local aiohttp server where the wire itself is the fact under
test; nothing here talks to Premiumize. Routing, failover, placement, refresh,
evidence and file selection are the existing neutral owners, consumed
unchanged.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest

from integrations.catalog import definitions
from integrations.runtime_state import credential_scope
from providers.premiumize import admin
from providers.premiumize import client as client_module
from providers.premiumize.account import entitlement
from providers.premiumize.client import (
    API, PremiumizeAPIError, PremiumizeService, RawResponse, aiohttp_transport, parse_member_address,
)
from providers.premiumize.definition import PremiumizeOptions, definition
from providers.premiumize.host_runtime import (
    PremiumizeHostMaintenance, PremiumizeHostSnapshot, PremiumizeRequestApplicability, decode_host_snapshot,
    encode_host_snapshot, parse_native_host_snapshot,
)
from providers.premiumize.provider import PremiumizeProvider
from providers.premiumize.translation import IMMEDIATE, error_from_native, transfer_state, translate_error
from transfers import codec
from transfers.applicability import ApplicabilityReadiness
from transfers.entitlement import AccountServiceClass
from transfers.errors import Category, MutationOutcome, Retryability, TransferError
from transfers.models import (
    CachePresence, CleanupAuthority, CleanupDirective, OutcomeKind, Ownership, ResourceState, TransferRequest,
)

pytestmark = pytest.mark.asyncio

KEY = "pm-api-key-0123456789abcdef"
NOW = 1_800_000_000.0
HASH = "e" * 40
HOSTER = TransferRequest("https", "https://rapid.example/f/abc", "movie.mkv")
MAGNET = TransferRequest("magnet", f"magnet:?xt=urn:btih:{HASH}&dn=Show", "Show", HASH)


def ok(**fields):
    return 200, {"status": "success", **fields}


def refused(code, status=200):
    return status, {"status": "error", "code": code, "message": f"refused: {code}"}


class Transport:
    """Scripted native HTTP: each call takes the next response for its route."""

    def __init__(self, script):
        self.script = {key: list(value) for key, value in script.items()}
        self.calls = []

    async def __call__(self, method, url, *, headers=None, params=None, data=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "params": dict(params or {}), "data": data, "timeout": timeout})
        response = self.script[(method, url.removeprefix(API + "/"))].pop(0)
        if isinstance(response, Exception):
            raise response
        status, payload = response
        return RawResponse(status, {"content-type": "application/json"},
                           payload if isinstance(payload, bytes) else json.dumps(payload).encode())


def provider_with(script, **options):
    transport = Transport(script)
    provider = PremiumizeProvider(PremiumizeService(KEY, transport=transport), **options)
    return provider, transport


def snapshot(directdl=("rapid.example",), queue=("rapid.example",), cache=()):
    return PremiumizeHostSnapshot(tuple(directdl), tuple(queue), tuple(cache))


def with_hosts(provider, value=None):
    provider.hosts = SimpleNamespace(snapshot=value or snapshot())
    return provider


def routes(transport):
    return [(call["method"], call["url"].removeprefix(API + "/")) for call in transport.calls]


CONTENT = [{"path": "Show/e01.mkv", "size": 1000, "link": "https://cdn.premiumize.example/a"},
           {"path": "Show/Extras/e02.mkv", "size": 2000, "link": "https://cdn.premiumize.example/b"}]


# -- 32.1 client / protocol ----------------------------------------------------------

async def test_the_key_is_a_bearer_header_only_never_in_a_url_body_or_error():
    provider, transport = provider_with({("GET", "account/info"): [refused("authentication_failed", 401)]})
    with pytest.raises(PremiumizeAPIError) as caught:
        await provider.client.account_info()
    call = transport.calls[0]
    assert call["headers"] == {"Authorization": f"Bearer {KEY}"}
    assert KEY not in call["url"] and KEY not in json.dumps(call["params"]) and call["data"] is None
    error = translate_error(caught.value, secrets=provider.client.secrets())
    assert error.category == Category.CREDENTIAL_INVALID and KEY not in codec.dump(error)
    assert KEY not in repr(PremiumizeOptions(api_key=KEY))


async def test_the_answer_is_read_by_its_status_not_by_the_http_status():
    provider, _transport = provider_with({
        ("GET", "account/info"): [refused("service_down", 200), (200, {"premium_until": None}), ok(premium_until=1)],
        ("GET", "services/list"): [(200, {"directdl": ["rapid.example"]})]})
    with pytest.raises(PremiumizeAPIError) as caught:                 # a business refusal on HTTP 200
        await provider.client.account_info()
    assert (caught.value.code, caught.value.status) == ("service_down", 200)
    with pytest.raises(Exception, match="without a status"):          # no status is no answer
        await provider.client.account_info()
    assert (await provider.client.account_info())["premium_until"] == 1
    with pytest.raises(Exception, match="without a status"):          # the catalogue too
        await provider.client.services()


class LocalPremiumize:
    """A real HTTP server standing in for Premiumize: what aiohttp actually
    sends, and answers written in parts, cut off, or too large."""

    def __init__(self, answer):
        self.answer, self.seen = answer, []

    async def handle(self, request):
        form = await request.post() if request.method == "POST" else {}
        self.seen.append({"path": request.path, "content_type": request.headers.get("Content-Type", ""),
                          "authorization": request.headers.get("Authorization", ""), "query": request.query_string,
                          "fields": {key: (value.file.read(), value.filename) if hasattr(value, "file") else value
                                     for key, value in form.items()} if form else {},
                          "multi": [key for key in form] if form else []})
        return await self.answer(request)

    async def __aenter__(self):
        from aiohttp import web
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/api"

        async def transport(method, url, **kwargs):
            return await aiohttp_transport(method, url.replace(API, base), **kwargs)
        self.client = PremiumizeService(KEY, transport=transport)
        return self

    async def __aexit__(self, *_exc):
        await self.runner.cleanup()


def answering(payload):
    from aiohttp import web

    async def answer(_request):
        return web.json_response(payload)
    return answer


async def test_src_operations_are_forms_and_uploads_are_multipart_src_on_the_wire():
    async with LocalPremiumize(answering({"status": "success", "id": "T1", "content": [],
                                          "response": [True]})) as server:
        await server.client.directdl("https://rapid.example/f/abc")
        await server.client.create_transfer(source=MAGNET.payload)
        await server.client.create_transfer(upload=b"d4:infoe", name="show.torrent")
        await server.client.cache_check((HASH,))
    direct, link, upload, cache = server.seen
    for sent in (direct, link, cache):
        assert sent["content_type"] == "application/x-www-form-urlencoded"
    assert direct["fields"] == {"src": "https://rapid.example/f/abc"}
    assert link["fields"] == {"src": MAGNET.payload}
    assert upload["content_type"].startswith("multipart/form-data; boundary=")
    assert upload["fields"] == {"src": (b"d4:infoe", "show.torrent")}
    assert cache["fields"] == {"items[]": HASH}
    assert {sent["authorization"] for sent in server.seen} == {f"Bearer {KEY}"}
    assert all(KEY not in sent["query"] for sent in server.seen)


async def test_an_upload_uses_the_upload_timeout_and_a_form_the_request_timeout():
    transport = Transport({("POST", "transfer/create"): [ok(id="T1"), ok(id="T2")]})
    client = PremiumizeService(KEY, request_timeout_seconds=7, upload_timeout_seconds=333, transport=transport)
    await client.create_transfer(source="magnet:?xt=urn:btih:" + HASH)
    await client.create_transfer(upload=b"nzb", name="p.nzb")
    assert [call["timeout"].total for call in transport.calls] == [7, 333]


async def test_an_answer_is_read_to_its_end_and_one_past_the_bound_is_refused(monkeypatch):
    from aiohttp import web
    body = json.dumps({"status": "success", "premium_until": 5, "pad": "x" * 3000}).encode()

    async def in_parts(request):
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        for start in range(0, len(body), 700):
            await response.write(body[start:start + 700])
            await asyncio.sleep(0)
        await response.write_eof()
        return response
    async with LocalPremiumize(in_parts) as server:
        assert (await server.client.account_info())["premium_until"] == 5
        monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 1024)
        with pytest.raises(Exception, match="oversized"):
            await server.client.account_info()


async def test_a_body_cut_off_is_a_network_failure_for_a_read_and_uncertain_for_a_create():
    from aiohttp import web

    async def cut_off(request):
        response = web.StreamResponse(headers={"Content-Type": "application/json", "Content-Length": "500"})
        await response.prepare(request)
        await response.write(b'{"status": "success", "id": "T')
        request.transport.close()
        return response
    async with LocalPremiumize(cut_off) as server:
        with pytest.raises(aiohttp.ClientError):
            await server.client.directdl("https://rapid.example/f/abc")
        provider = PremiumizeProvider(server.client)
        with pytest.raises(TransferError) as created:
            await provider.resolve(MAGNET)
    assert created.value.error.mutation == MutationOutcome.UNCERTAIN
    error = translate_error(aiohttp.ClientPayloadError("cut"))
    assert error.category == Category.CONNECTION_FAILED and error.mutation != MutationOutcome.UNCERTAIN
    # One exchange each: the client never retried.
    assert [sent["path"] for sent in server.seen] == ["/api/transfer/directdl", "/api/cache/check",
                                                      "/api/transfer/create"]


async def test_a_creation_refusal_is_definitive_and_an_answer_without_an_id_is_uncertain():
    provider, transport = provider_with({("POST", "cache/check"): [ok(response=[False])] * 2,
                                         ("POST", "transfer/create"): [refused("account_limit_reached"), ok()]})
    owner = SimpleNamespace(contract=lambda *_args: pytest.fail("a temporary limit contracts nothing"),
                            entitlements=None)
    provider.account = owner
    with pytest.raises(TransferError) as limited:
        await provider.resolve(MAGNET)
    assert limited.value.error.category == Category.QUOTA_EXCEEDED
    assert limited.value.error.mutation != MutationOutcome.UNCERTAIN          # nothing was created
    with pytest.raises(TransferError) as unbound:
        await provider.resolve(MAGNET)
    assert unbound.value.error.mutation == MutationOutcome.UNCERTAIN          # may have been created
    assert routes(transport).count(("POST", "transfer/create")) == 2           # once each, never repeated


async def test_an_unstructured_error_status_after_create_is_uncertain_never_not_committed():
    """Only a complete ``status: "error"`` answer with a code proves nothing was
    created: a bare HTTP 400 page states nothing about the creation."""
    provider, transport = provider_with({("POST", "cache/check"): [ok(response=[False])],
                                         ("POST", "transfer/create"): [(400, b"<html>Bad Request</html>")]})
    with pytest.raises(TransferError) as caught:
        await provider.resolve(MAGNET)
    assert caught.value.error.mutation == MutationOutcome.UNCERTAIN
    assert routes(transport).count(("POST", "transfer/create")) == 1


async def test_native_ids_are_opaque_and_kept_exactly():
    from providers.premiumize.client import cloud_member_address
    from providers.premiumize.translation import cloud_resource
    odd = "Ab-9_./+:=?&#% x~"
    assert parse_member_address(cloud_member_address(odd)) == ("cloud", odd)
    assert cloud_resource(odd).context["transfer_id"] == odd
    provider, transport = provider_with({
        ("GET", "transfer/list"): [ok(transfers=[{"id": odd, "status": "finished", "file_id": odd}])],
        ("GET", "item/details"): [ok(id=odd, name="m.mkv", size=3, link="https://cdn.premiumize.example/x")]})
    (entry,) = await provider.manifest(cloud_resource(odd))
    assert parse_member_address(entry.request.payload) == ("cloud", odd)
    assert transport.calls[1]["params"] == {"id": odd}


@pytest.mark.parametrize("code", ["unknown_error", "transient_error", "a_future_code"])
async def test_a_generic_or_unknown_create_refusal_is_uncertain(code):
    """Only documented refusals of the request itself prove nothing was created."""
    provider, _transport = provider_with({("POST", "cache/check"): [ok(response=[False])],
                                          ("POST", "transfer/create"): [refused(code)]})
    with pytest.raises(TransferError) as caught:
        await provider.resolve(MAGNET)
    assert caught.value.error.mutation == MutationOutcome.UNCERTAIN


# -- 32.2 translation ------------------------------------------------------------------

@pytest.mark.parametrize("code, category, retry", [
    ("transient_error", Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF),
    ("account_limit_reached", Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),
    ("rate_limit_reached", Category.RATE_LIMITED, Retryability.BACKOFF),
    ("service_unsupported", Category.UNSUPPORTED_REQUEST, Retryability.NEVER),
    ("authentication_failed", Category.CREDENTIAL_INVALID, Retryability.AFTER_REAUTH),
    ("not_found", Category.RESOURCE_NOT_FOUND, Retryability.AFTER_RERESOLUTION),
    ("invalid_request", Category.INVALID_REQUEST, Retryability.NEVER),
    ("unknown_error", Category.UNMAPPED_PROVIDER_ERROR, Retryability.UNKNOWN),
    ("a_new_code", Category.UNMAPPED_PROVIDER_ERROR, Retryability.UNKNOWN),
])
async def test_each_class_of_native_code_normalizes_by_its_code_never_its_message(code, category, retry):
    error = error_from_native(PremiumizeAPIError(code, "authentication_failed in a message means nothing", 200))
    assert (error.category, error.integration_id, error.native_code) == (category, "premiumize", code)
    if category not in {Category.UNMAPPED_PROVIDER_ERROR}:
        assert error.retryability == retry


@pytest.mark.parametrize("native, state", [
    ({"status": "queued"}, ResourceState.PREPARING), ({"status": "running"}, ResourceState.PREPARING),
    ({"status": "finished", "folder_id": "F1"}, ResourceState.AVAILABLE),
    ({"status": "seeding", "file_id": "f1"}, ResourceState.AVAILABLE),
    ({"status": "error", "message": "dead"}, ResourceState.UNAVAILABLE),
    ({"status": "finished"}, ResourceState.UNKNOWN), ({"status": 7}, ResourceState.UNKNOWN),
])
async def test_transfer_states_translate_in_one_place(native, state):
    assert transfer_state(native)[0] == state


# -- 32.3 provider contract --------------------------------------------------------------

async def test_an_immediate_hoster_result_is_frozen_without_any_cloud_transfer_or_link():
    provider, transport = provider_with({("POST", "transfer/directdl"): [
        ok(content=[{"path": "movie.mkv", "size": 4096, "link": "https://cdn.premiumize.example/m"}])]})
    result = await with_hosts(provider).resolve(HOSTER)
    observed = result.observation
    assert result.state == ResourceState.AVAILABLE and observed.resource.ownership == Ownership.OBSERVED
    assert observed.resource.context["mode"] == IMMEDIATE and observed.name == "movie.mkv"
    assert routes(transport) == [("POST", "transfer/directdl")]
    assert "cdn.premiumize" not in codec.dump(observed.resource)
    (entry,) = await provider.manifest(observed.resource)
    assert (entry.relative_path, entry.expected_bytes, entry.request.preferred_provider) == (
        "movie.mkv", 4096, "premiumize")
    assert parse_member_address(entry.request.payload) == ("immediate", HOSTER.payload, "movie.mkv", "4096")
    cleanup = await provider.cleanup(CleanupDirective(observed.resource, authority=CleanupAuthority.OWNED))
    assert cleanup.kind == OutcomeKind.SKIPPED and len(transport.calls) == 1


async def test_a_held_torrent_is_resolved_immediately_as_its_complete_tree():
    provider, transport = provider_with({("POST", "cache/check"): [ok(response=[True])],
                                         ("POST", "transfer/directdl"): [ok(content=CONTENT)]})
    observed = (await provider.resolve(MAGNET)).observation
    assert [entry.relative_path for entry in observed.file_manifest.entries] == ["e01.mkv", "Extras/e02.mkv"]
    assert [entry.expected_bytes for entry in observed.file_manifest.entries] == [1000, 2000]
    assert routes(transport) == [("POST", "cache/check"), ("POST", "transfer/directdl")]


@pytest.mark.parametrize("held, direct", [(False, None), (True, refused("not_found"))], ids=["miss", "no-result"])
async def test_without_an_immediate_result_the_source_is_acquired_into_the_cloud_once(held, direct):
    script = {("POST", "cache/check"): [ok(response=[held])], ("POST", "transfer/create"): [ok(id="T9")],
              ("GET", "transfer/list"): [ok(transfers=[{"id": "T9", "status": "running", "name": "Show"}])]}
    if direct:
        script[("POST", "transfer/directdl")] = [direct]
    provider, transport = provider_with(script)
    result = await provider.resolve(MAGNET)
    assert result.state == ResourceState.PREPARING
    assert result.observation.resource.context == {"mode": "cloud", "transfer_id": "T9"}
    assert result.observation.resource.ownership == Ownership.CREATED
    assert routes(transport).count(("POST", "transfer/create")) == 1


async def test_a_created_transfer_whose_first_observation_failed_keeps_its_identity():
    provider, _transport = provider_with({("POST", "cache/check"): [ok(response=[False])],
                                          ("POST", "transfer/create"): [ok(id="T9")],
                                          ("GET", "transfer/list"): [refused("transient_error")]})
    result = await provider.resolve(MAGNET)
    assert result.state == ResourceState.UNKNOWN
    assert result.observation.resource.context["transfer_id"] == "T9"


async def test_finished_transfers_publish_their_one_file_or_their_whole_folder_tree():
    provider, transport = provider_with({
        ("GET", "transfer/list"): [ok(transfers=[{"id": "T1", "status": "finished", "file_id": "f1"},
                                                 {"id": "T2", "status": "seeding", "folder_id": "F0", "name": "Show"}])] * 2,
        ("GET", "item/details"): [ok(id="f1", name="movie.mkv", size=7, link="https://cdn.premiumize.example/f1")],
        ("GET", "folder/list"): [
            ok(folder_id="F0", content=[{"type": "file", "id": "a1", "name": "e01.mkv", "size": 1},
                                        {"type": "folder", "id": "F1", "name": "Extras"}]),
            ok(folder_id="F1", content=[{"type": "file", "id": "b2", "name": "e02.mkv", "size": 2}])]})
    from providers.premiumize.translation import cloud_resource
    (single,) = await provider.manifest(cloud_resource("T1"))
    assert (single.relative_path, single.expected_bytes) == ("movie.mkv", 7)
    assert parse_member_address(single.request.payload) == ("cloud", "f1")
    tree = await provider.manifest(cloud_resource("T2"))
    assert [(entry.relative_path, parse_member_address(entry.request.payload)) for entry in tree] == [
        ("Extras/e02.mkv", ("cloud", "b2")), ("e01.mkv", ("cloud", "a1"))]
    assert "cdn.premiumize" not in json.dumps([entry.request.payload for entry in (single, *tree)])
    assert ("GET", "item/details") in routes(transport)


@pytest.mark.parametrize("listing, diagnostic", [
    (ok(folder_id="F9", content=[{"type": "folder", "id": "F1", "name": "Extras"}]), "folder identity mismatch"),
    (ok(content=[{"type": "folder", "id": "F1", "name": "Extras"}]), "folder identity mismatch"),
    (ok(folder_id="F0", content=[{"type": "file", "id": "a1", "name": "e01.mkv", "size": 1},
                                 {"type": "file", "id": "a1", "name": "e01-copy.mkv", "size": 1}]), None),
], ids=["other-folder", "no-folder-id", "duplicate-file-id"])
async def test_a_cloud_tree_with_unproven_folder_or_file_identity_fails_closed(listing, diagnostic):
    provider, transport = provider_with({
        ("GET", "transfer/list"): [ok(transfers=[{"id": "T2", "status": "finished", "folder_id": "F0"}])],
        ("GET", "folder/list"): [listing]})
    from providers.premiumize.translation import cloud_resource
    with pytest.raises(TransferError) as refused_tree:
        await provider.manifest(cloud_resource("T2"))
    assert refused_tree.value.error.category in {Category.PROVIDER_PROTOCOL_VIOLATION,
                                                 Category.INVALID_ADAPTER_RESPONSE}
    if diagnostic:
        assert diagnostic in refused_tree.value.error.diagnostic
    assert routes(transport).count(("GET", "folder/list")) == 1     # the foreign folder's subfolder never read


async def test_a_repeating_or_too_wide_folder_graph_ends_without_unbounded_reads(monkeypatch):
    from providers.premiumize import provider as provider_module
    from providers.premiumize.translation import cloud_resource
    finished = ok(transfers=[{"id": "T2", "status": "finished", "folder_id": "F0"}])
    cyclic, transport = provider_with({("GET", "transfer/list"): [finished], ("GET", "folder/list"): [
        ok(folder_id="F0", content=[{"type": "folder", "id": "F0", "name": "again"}])]})
    with pytest.raises(TransferError) as repeated:
        await cyclic.manifest(cloud_resource("T2"))
    assert "repeats a folder" in repeated.value.error.diagnostic
    assert routes(transport).count(("GET", "folder/list")) == 1          # never reread
    monkeypatch.setattr(provider_module, "_MAX_CLOUD_FOLDERS", 3)
    wide, transport = provider_with({("GET", "transfer/list"): [finished], ("GET", "folder/list"): [
        ok(folder_id="F0", content=[{"type": "folder", "id": f"S{n}", "name": f"s{n}"} for n in range(5)]),
        *[ok(folder_id=f"S{n}", content=[]) for n in (4, 3)]]})
    with pytest.raises(TransferError) as bounded:
        await wide.manifest(cloud_resource("T2"))
    assert "too many folders" in bounded.value.error.diagnostic
    assert routes(transport).count(("GET", "folder/list")) == 3


async def test_a_cloud_member_is_reminted_from_its_file_id():
    provider, transport = provider_with({("GET", "item/details"): [
        ok(id="f1", name="movie.mkv", size=7, link="https://cdn.premiumize.example/fresh")]})
    member = TransferRequest("https", f"{API}/item/details?id=f1", "movie.mkv", preferred_provider="premiumize")
    (candidate,) = (await provider.resolve(member)).candidates
    assert candidate.endpoints[0].address == "https://cdn.premiumize.example/fresh"
    assert candidate.endpoints[0].transient and candidate.refresh_request == member
    assert transport.calls[0]["params"] == {"id": "f1"}


@pytest.mark.parametrize("content, outcome", [
    (CONTENT, "https://cdn.premiumize.example/b"),
    ([*CONTENT, {"path": "Show/Extras/e02.mkv", "size": 2000, "link": "https://cdn.premiumize.example/c"}],
     "ambiguous"),
    ([{**CONTENT[1], "size": 2001}], "not in the result"),
    ([{"path": "other.mkv", "size": 2000, "link": "https://cdn.premiumize.example/only"}], "not in the result"),
], ids=["exact", "ambiguous", "size-differs", "only-link-is-not-the-member"])
async def test_an_immediate_member_is_refreshed_by_its_exact_identity_or_fails_closed(content, outcome):
    provider, _transport = provider_with({("POST", "transfer/directdl"): [ok(content=content)]})
    member = TransferRequest("https", f"{API}/transfer/directdl?src=magnet%3A&path=Show%2FExtras%2Fe02.mkv&size=2000",
                             "e02.mkv", preferred_provider="premiumize")
    if outcome.startswith("https://"):
        (candidate,) = (await provider.resolve(member)).candidates
        assert candidate.endpoints[0].address == outcome and candidate.expected_bytes == 2000
        return
    with pytest.raises(TransferError) as refused_member:
        await provider.resolve(member)
    assert refused_member.value.error.category == Category.PROVIDER_PROTOCOL_VIOLATION
    assert outcome in refused_member.value.error.diagnostic


async def test_cleanup_deletes_only_the_transfer_dp_owns_and_never_cloud_files():
    from providers.premiumize.translation import cloud_resource
    provider, transport = provider_with({("POST", "transfer/delete"): [ok()]})
    assert (await provider.cleanup(CleanupDirective(cloud_resource("T1", ownership=Ownership.OBSERVED),
                                                    authority=CleanupAuthority.OWNED))).kind == OutcomeKind.SKIPPED
    assert (await provider.cleanup(CleanupDirective(cloud_resource("T1"),
                                                    authority=CleanupAuthority.OWNED))).kind == OutcomeKind.SUCCESS
    assert routes(transport) == [("POST", "transfer/delete")] and transport.calls[0]["data"] == {"id": "T1"}


async def test_backups_follow_the_option_and_only_for_torrents():
    off = PremiumizeProvider(PremiumizeService(KEY))
    on = PremiumizeProvider(PremiumizeService(KEY), prepare_backup_torrents=True)
    nzb = TransferRequest("nzb", b"<nzb/>", "p.nzb")
    assert [off.speculative_preparation_allowed(r) for r in (MAGNET, HOSTER, nzb)] == [False] * 3
    assert [on.speculative_preparation_allowed(r) for r in (MAGNET, HOSTER, nzb)] == [True, False, False]
    # No synthetic active capacity: Premiumize states no applicable maximum.
    assert not hasattr(on, "active_capacity") and "max_active" not in json.dumps(PremiumizeOptions.model_json_schema())


async def test_an_nzb_is_uploaded_to_the_cloud_and_never_asked_for_immediately():
    provider, transport = provider_with({("POST", "transfer/create"): [ok(id="N1")],
                                         ("GET", "transfer/list"): [ok(transfers=[{"id": "N1", "status": "queued"}])]})
    await provider.resolve(TransferRequest("nzb", b"<nzb/>", "posting.nzb"))
    assert routes(transport) == [("POST", "transfer/create"), ("GET", "transfer/list")]
    assert isinstance(transport.calls[0]["data"], aiohttp.FormData)


async def test_an_unadmitted_group_alternative_is_never_acquired():
    provider, transport = provider_with({("POST", "cache/check"): [ok(response=[False, True])],
                                         ("POST", "transfer/directdl"): [refused("not_found")]})
    with_hosts(provider, snapshot(cache=("rapid.example",)))
    assert await provider.cache_presence((MAGNET, HOSTER)) == (CachePresence.MISS, CachePresence.HIT)
    assert await provider.resolve_cached(MAGNET) is None          # cache miss: nothing asked, nothing created
    assert await provider.resolve_cached(HOSTER) is None          # no immediate result: nothing created
    assert ("POST", "transfer/create") not in routes(transport)


# -- 32.4 applicability / account --------------------------------------------------------

async def test_the_catalogue_claims_direct_and_queue_services_only_and_its_own_members():
    # Service NAMES: domains come from a hostname name and aliases; a pattern
    # adds EXACT hosts only in the documented whole-host shape -- read, never run.
    native = {"status": "success", "directdl": ["rapid.example", "nitro", "pathy"],
              "queue": ["slow.example", "opaque", "redirector"],
              "cache": ["cacheonly.example"], "aliases": {"rapid.example": ["rapid-mirror.example"]},
              "regexpatterns": {"nitro": [r"^https?:\/\/(?:www\.)?nitro\.example\/.*$"],
                                "pathy": [r"^https?://(?:www\.)?pathy\.example/view/.*$"],
                                "redirector": [r"^https?://files\.example/redirect/evil\.example/.*$"],
                                "opaque": ["(a+)+$", r"[a-z]+\.parent\.example\/"]}}
    parsed = parse_native_host_snapshot(native)
    assert decode_host_snapshot(encode_host_snapshot(parsed)) == parsed
    applies = PremiumizeRequestApplicability(parsed)
    claimed = {host: bool(applies(TransferRequest("https", f"https://{host}/f", "f")).specialized_hosts)
               for host in ("rapid.example", "rapid-mirror.example", "slow.example", "nitro.example",
                            "www.nitro.example", "cdn.nitro.example", "pathy.example", "files.example",
                            "evil.example", "parent.example", "cacheonly.example")}
    assert claimed == {"rapid.example": True, "rapid-mirror.example": True, "slow.example": True,
                       "nitro.example": True, "www.nitro.example": True,        # 4: the broad shape, exactly
                       "cdn.nitro.example": False,                              # 1: optional www, no subdomains
                       "pathy.example": False,                                  # 2: a path restriction claims no host
                       "files.example": False, "evil.example": False,           # 3: nothing taken from a path
                       "parent.example": False, "cacheonly.example": False}
    unresolved = PremiumizeRequestApplicability(None)
    assert unresolved(HOSTER).readiness == ApplicabilityReadiness.UNRESOLVED
    member = TransferRequest("https", f"{API}/item/details?id=f1", "f")
    assert unresolved(member).readiness == ApplicabilityReadiness.READY


async def test_a_failed_catalogue_refresh_is_never_unsupportedness():
    class Store:
        async def load(self, *_args):
            return None
    provider = PremiumizeProvider(PremiumizeService(KEY, transport=Transport(
        {("GET", "services/list"): [refused("service_down")]})))
    hosts = PremiumizeHostMaintenance(provider, Store(), clock=lambda: NOW)
    await hosts.maintain()
    assert provider.applicability.readiness == ApplicabilityReadiness.UNRESOLVED and hosts.snapshot is None


async def test_account_truth_is_premium_until_its_end_and_degraded_otherwise():
    offered = frozenset({"https", "magnet", "nzb"})
    premium = entitlement({"premium_until": NOW + 86400}, offered=offered, now=NOW)
    assert (premium.service_class, premium.expires_at, premium.degraded) == (
        AccountServiceClass.PREMIUM, NOW + 86400, False)
    assert premium.admits("nzb") and premium.plan == ""
    for until in (None, NOW - 1):
        free = entitlement({"premium_until": until}, offered=offered, now=NOW)
        assert free.service_class == AccountServiceClass.STANDARD and free.degraded
        assert not any(free.admits(kind) for kind in offered)
    assert credential_scope("premiumize", KEY) != credential_scope("premiumize", KEY + "x")


async def test_status_makes_no_call_while_disabled_or_unconfigured_and_registers_once():
    for enabled, key in ((False, KEY), (True, "")):
        transport = Transport({})
        provider = PremiumizeProvider(PremiumizeService(key, transport=transport))
        state = (await admin.runtime_status(provider, enabled=enabled))["state"]
        assert state == ("disabled" if not enabled else "unconfigured") and transport.calls == []
    assert [item.id for item in definitions].count("premiumize") == 1
    assert definition.default_enabled is False and definition.secret_fields == frozenset({"api_key"})


# -- NZB cloud filename recovery (transfers 549/550) ----------------------------------------------
#
# A synthetic posting shaped like the live one: its first file a clean PAR2
# (the useful name), its payload posted under an obfuscated name.

HASHED = "85d29188f4f2beb2d3aefd6add70bfe3fcc13c61.mp4"


def posting(*subjects):
    from html import escape
    files = "".join(f'<file subject="{escape(subject, quote=True)}" poster="p" date="1"><groups><group>a.b</group></groups>'
                    f'<segments><segment bytes="10" number="1">m{index}@x</segment></segments></file>'
                    for index, subject in enumerate(subjects))
    return f'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">{files}</nzb>'.encode()


LIVE_SHAPE = posting('[2/3] - "Release.Name.vol-01.par2" yEnc (1/1)',
                     '[1/3] - "3yUxAde1oJ2keIb4gKg1A40k32KEgF6l.mp4" yEnc (1/9)')


def nzb_transfer(*content, single=None, folders=None, payload=LIVE_SHAPE, created=True):
    """Upload ``payload`` as an NZB and finish its cloud transfer as one file
    (``single``: name, size), a folder holding ``content``, or the scripted
    ``folders`` listings."""
    finished = {"id": "N1", "status": "finished", **({"file_id": "f1"} if single else {"folder_id": "F0"})}
    script = {("POST", "transfer/create"): [ok(id="N1")],
              ("GET", "transfer/list"): [ok(transfers=[{"id": "N1", "status": "queued"}])] * created
              + [ok(transfers=[finished])] * 4}
    if single:
        script[("GET", "item/details")] = [ok(id="f1", name=single[0], size=single[1],
                                              link="https://cdn.premiumize.example/f1")] * 4
    else:
        script[("GET", "folder/list")] = folders or [ok(folder_id="F0", content=list(content))] * 4
    provider, transport = provider_with(script)
    return provider, transport, payload


async def resolved_nzb(provider, payload):
    return (await provider.resolve(TransferRequest("nzb", payload, "Some.Upload.nzb"))).observation.resource


def opaque(file_id, name, size):
    return {"type": "file", "id": file_id, "name": name, "size": size}


async def test_a_the_dominant_obfuscated_payload_takes_the_postings_useful_name():
    """A (transfer 549): the hash-named cloud file is named as native Usenet names it."""
    provider, _transport, payload = nzb_transfer(single=(HASHED, 4_035_560_403))
    resource = await resolved_nzb(provider, payload)
    assert resource.context["nzb_name"] == "Release.Name.vol-01"
    (entry,) = await provider.manifest(resource)
    assert (entry.relative_path, entry.name, entry.expected_bytes) == ("Release.Name.vol-01.mp4",
                                                                       "Release.Name.vol-01.mp4", 4_035_560_403)
    assert parse_member_address(entry.request.payload) == ("cloud", "f1")          # identity unchanged


async def test_b_a_meaningful_cloud_name_is_kept():
    provider, _transport, payload = nzb_transfer(single=("Episode.One.1080p.mkv", 900))
    (entry,) = await provider.manifest(await resolved_nzb(provider, payload))
    assert entry.relative_path == "Episode.One.1080p.mkv"


async def test_c_a_non_nzb_cloud_transfer_keeps_the_hash_name():
    from providers.premiumize.translation import cloud_resource
    provider, _transport = provider_with({
        ("GET", "transfer/list"): [ok(transfers=[{"id": "T1", "status": "finished", "file_id": "f1"}])],
        ("GET", "item/details"): [ok(id="f1", name=HASHED, size=7, link="https://cdn.premiumize.example/f1")]})
    (entry,) = await provider.manifest(cloud_resource("T1"))
    assert entry.relative_path == HASHED


@pytest.mark.parametrize("sizes", [(300, 100), (101, 100), (100, 100)], ids=["exactly-3x", "one-byte", "equal"])
async def test_d_without_a_strictly_dominant_payload_every_name_is_kept(sizes):
    provider, _transport, payload = nzb_transfer(opaque("a1", HASHED, sizes[0]),
                                                 opaque("b2", "0f1e2d3c4b5a69788796a5b4c3d2e1f0aa.mkv", sizes[1]))
    entries = await provider.manifest(await resolved_nzb(provider, payload))
    assert sorted(entry.relative_path for entry in entries) == sorted([HASHED, "0f1e2d3c4b5a69788796a5b4c3d2e1f0aa.mkv"])


async def test_e_only_the_dominant_payload_is_renamed_and_sidecars_keep_their_names():
    provider, _transport, payload = nzb_transfer(folders=[
        ok(folder_id="F0", content=[{"type": "folder", "id": "S", "name": "Sub"}, opaque("b2", "a1b2c3.nfo", 100)]),
        ok(folder_id="S", content=[opaque("a1", HASHED, 301)])])
    entries = await provider.manifest(await resolved_nzb(provider, payload))
    assert {entry.relative_path: parse_member_address(entry.request.payload)[1] for entry in entries} == {
        "Sub/Release.Name.vol-01.mp4": "a1", "a1b2c3.nfo": "b2"}      # at its own location; sidecar kept


async def test_f_a_name_that_would_collide_keeps_every_cloud_name():
    provider, _transport, payload = nzb_transfer(opaque("a1", HASHED, 1000), opaque("b2", "Release.Name.vol-01.mp4", 10))
    entries = await provider.manifest(await resolved_nzb(provider, payload))
    assert sorted(entry.relative_path for entry in entries) == sorted([HASHED, "Release.Name.vol-01.mp4"])


async def test_f_a_name_that_would_share_a_normalized_destination_keeps_every_cloud_name():
    """``Release_Name`` is distinct from ``Release?Name`` as raw text, but both
    materialize at ``Release_Name``: the rename is abandoned, not a conflict."""
    sibling = "Release?Name.vol-01.mp4"
    provider, _transport, payload = nzb_transfer(
        opaque("a1", HASHED, 1000), opaque("b2", sibling, 10),
        payload=posting('[2/3] - "Release_Name.vol-01.par2" yEnc (1/1)', '[1/3] - "x.mp4" yEnc (1/9)'))
    entries = await provider.manifest(await resolved_nzb(provider, payload))
    assert sorted(entry.relative_path for entry in entries) == sorted([HASHED, sibling])


async def test_g_the_name_survives_restart_and_refresh_from_the_retained_fact():
    provider, _transport, payload = nzb_transfer(single=(HASHED, 4096))
    # What a restart reloads: the persisted resource, read by a new provider.
    resource = codec.resource(codec.load(codec.dump(await resolved_nzb(provider, payload))))
    restarted, _transport, _payload = nzb_transfer(single=(HASHED, 4096), created=False)
    observed = await restarted.observe(resource)
    assert [entry.relative_path for entry in observed.file_manifest.entries] == ["Release.Name.vol-01.mp4"]
    (entry,) = await restarted.manifest(observed.resource)
    assert entry.relative_path == "Release.Name.vol-01.mp4"
    (candidate,) = (await restarted.resolve(entry.request)).candidates            # refresh: item/details(file id)
    assert candidate.endpoints[0].address == "https://cdn.premiumize.example/f1"


# -- "Use Premiumize Before Usenet" is an entitlement-gated preference (G) ------------------------

class _Saved:
    """One in-memory saved configuration holding the Premiumize namespace."""

    def __init__(self, **options):
        from core.config import AppSettings
        from integrations.definition import IntegrationSettings
        self.cfg = AppSettings(integrations={"premiumize": IntegrationSettings(enabled=True, options=options)})

    def read(self):
        return self.cfg.model_copy(deep=True)

    def write(self, cfg):
        self.cfg = cfg


async def premium_application(premium_until):
    """A real application over a Premiumize provider whose real account owner
    holds ``premium_until``'s account truth."""
    from unittest.mock import AsyncMock

    from application.service import ApplicationService
    from integrations.account_entitlement import AccountEntitlementMaintenance
    from integrations.runtime_state import ScopedRuntimeStateStore
    from providers.premiumize.account import PremiumizeAccountTranslation
    from test_v113_account_entitlement import Store
    from transfers.registry import IntegrationRegistry

    account = {"status": "success", "premium_until": premium_until}
    transport = Transport({("GET", "account/info"): [ok(**{k: v for k, v in account.items() if k != "status"})] * 8})
    provider = PremiumizeProvider(PremiumizeService(KEY, transport=transport))
    provider.account = AccountEntitlementMaintenance(
        provider, PremiumizeAccountTranslation(provider.client, clock=lambda: NOW),
        ScopedRuntimeStateStore(Store(), credential_scope("premiumize", KEY)), integration_id="premiumize",
        clock=lambda: NOW)
    provider.lifecycle = provider.account
    await provider.account.refresh_now()
    registry = IntegrationRegistry()
    registry.register_provider(provider)
    application = ApplicationService(SimpleNamespace(registry=registry, repository=None))
    application.definitions = (definition,)
    application.configure = lambda: None
    application.apply_integration_configuration = AsyncMock(return_value=None)
    return application, provider, transport


@pytest.mark.parametrize("premium_until, entitled", [(NOW + 86400, True), (None, False), (NOW - 1, False)],
                         ids=["premium", "free", "expired"])
async def test_g_the_usenet_preference_needs_an_active_premium_account(premium_until, entitled):
    from fastapi import HTTPException

    from api.routes import IntegrationConfigurationUpdate, patch_integration_configuration
    from test_v113_torbox_routes import _settings_owner
    application, provider, _transport = await premium_application(premium_until)
    saved = _Saved(api_key=KEY)
    with _settings_owner(saved):
        change = patch_integration_configuration("premiumize", IntegrationConfigurationUpdate(
            options={"use_before_usenet": True}), application)
        if entitled:
            await change
        else:
            with pytest.raises(HTTPException) as refused_on:
                await change
            assert (refused_on.value.status_code, refused_on.value.detail) == (
                409, "Requires an active Premiumize premium account.")
    assert saved.cfg.integrations["premiumize"].options.get("use_before_usenet", False) is entitled
    assert provider.descriptor.priority_for("nzb") == -1        # +1/-1 routing itself is unchanged


async def test_g_a_lapsed_premium_turns_the_preference_off_and_renewal_never_restores_it():
    application, provider, transport = await premium_application(NOW + 86400)
    saved = _Saved(api_key=KEY, use_before_usenet=True)
    from test_v113_torbox_routes import _settings_owner
    with _settings_owner(saved):
        assert await application.converge_entitled_options() == ()
        transport.script[("GET", "account/info")] = [ok(premium_until=None)] * 4      # lapsed
        await application.refresh_account_entitlement("premiumize")
        assert await application.converge_entitled_options() == ("premiumize",)
        assert saved.cfg.integrations["premiumize"].options["use_before_usenet"] is False
        transport.script[("GET", "account/info")] = [ok(premium_until=NOW + 86400)] * 4   # renewed
        await application.refresh_account_entitlement("premiumize")
        await application.converge_entitled_options()
    assert saved.cfg.integrations["premiumize"].options["use_before_usenet"] is False
