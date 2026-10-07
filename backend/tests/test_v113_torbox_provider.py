"""TorBox provider: native REST mechanics behind neutral contracts.

Native HTTP is replaced at the client boundary (an injected transport) or the
provider's client is a fake; nothing here talks to TorBox. Routing, failover,
placement and NZB ingress are the existing neutral owners, consumed unchanged.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

import db.database as database
from providers.torbox import admin
from providers.torbox.client import (
    API, TORRENT, USENET, WEBDL, RawResponse, TorBoxAPIError, TorBoxService, aiohttp_transport, member_address,
    member_source_host, parse_member_address,
)
from providers.torbox.definition import TorBoxOptions, definition
from providers.torbox.host_runtime import (
    HOST_SCHEMA_VERSION, TorBoxHostMaintenance, TorBoxHostSnapshotError, TorBoxRequestApplicability,
    applicability_facts, decode_host_snapshot, encode_host_snapshot, parse_native_host_snapshot,
)
from providers.torbox.provider import TorBoxProvider
from providers.torbox.translation import identity, native_members, observation, resource, translate_error
from providers.usenet.provider import UsenetProvider
from transfers.applicability import ApplicabilityReadiness
from transfers.errors import Category, Domain, MutationOutcome, Recovery, Retryability, TransferError
from transfers.file_selection import ManifestInvalid
from transfers.models import (
    CachePresence, CleanupAuthority, CleanupDirective, DeliveryKind, OutcomeKind, Ownership, ResourceState, SourceIdentity,
    TransferRequest,
)
from transfers.policy import TransferPolicy, provider_attributable
from transfers.registry import IntegrationRegistry
from transfers.staged_input import StagedInputStore

pytestmark = pytest.mark.asyncio

TOKEN = "b1a3f2e4-0000-4000-8000-acc0un7t0k3n"
NZB = (b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
       b'<file poster="p@e.net" date="1700000000" subject="show [1/1] - &quot;show.mkv&quot; yEnc (1/1)">'
       b"<groups><group>alt.binaries.test</group></groups>"
       b'<segments><segment bytes="1024" number="1">a@e.net</segment></segments></file></nzb>')
MAGNET = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Show"


def ok(data, status=200):
    return status, {"success": True, "error": None, "detail": "ok", "data": data}


def refused(code, status=400, detail="refused"):
    return status, {"success": False, "error": code, "detail": detail, "data": None}


class Transport:
    """Scripted native HTTP: each call takes the next response for its route."""

    def __init__(self, script):
        self.script = {key: list(value) for key, value in script.items()}
        self.calls = []

    async def __call__(self, method, url, *, headers=None, params=None, data=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "params": dict(params or {}), "data": data, "timeout": timeout})
        status, payload, *headers = self.script[(method, url.removeprefix(API + "/"))].pop(0)
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return RawResponse(status, {key.casefold(): value for key, value in (headers[0] if headers else {}).items()},
                           body)


class NoLimit:
    async def acquire(self):
        return None


def service(script, *, token=TOKEN):
    transport = Transport(script)
    return TorBoxService(token, rate_limiter=NoLimit(), transport=transport), transport


def form_fields(data):
    """``(name, value)`` of an aiohttp FormData the client built."""
    return {field[0]["name"]: field[2] for field in data._fields}


def torrent(native_id=7, *, state="downloading", present=False, files=None, name="Show"):
    return {"id": native_id, "hash": "a" * 40, "name": name, "size": 3000, "progress": 0.5,
            "download_state": state, "download_present": present, "download_speed": 10,
            "files": files if files is not None else [
                {"id": 0, "name": f"{name}/e01.mkv", "size": 1000},
                {"id": 1, "name": f"{name}/Extras/e02.mkv", "size": 2000}]}


# -- client, auth and secrets ---------------------------------------------------------

async def test_the_token_travels_as_bearer_except_where_torbox_defines_requestdl():
    client, transport = service({
        ("GET", "user/me"): [ok({"email": "a@e.net", "plan": 2, "premium_expires_at": "2099-01-01T00:00:00Z"})],
        ("GET", "torrents/requestdl"): [ok("https://store-1.tb-cdn.st/dld/abc?token=link-token")],
    })
    await client.user()
    link = await client.requestdl(TORRENT, "7", "1")
    assert transport.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert "token" not in transport.calls[0]["params"]
    assert transport.calls[1]["params"] == {"token": TOKEN, "torrent_id": "7", "file_id": "1"}
    assert "Authorization" not in transport.calls[1]["headers"]
    assert link.endswith("link-token")


async def test_a_refusal_never_carries_the_token():
    client, _ = service({("GET", "user/me"): [refused("BAD_TOKEN", 403, detail=f"token {TOKEN} invalid")]})
    with pytest.raises(TorBoxAPIError) as caught:
        await client.user()
    error = translate_error(caught.value, secrets=client.secrets())
    assert error.category == Category.CREDENTIAL_INVALID
    assert TOKEN not in json.dumps(error.as_dict(diagnostics=True), default=str)
    assert TOKEN not in repr(TorBoxOptions(api_token=TOKEN))


async def test_device_authorization_start_pending_completion_and_expiry():
    clock = [1000.0]
    client, transport = service({
        ("GET", "user/auth/device/start"): [ok({"device_code": "DEV", "code": "ABC123", "interval": 0,
                                                "verification_url": "https://torbox.app/oauth/device",
                                                "friendly_verification_url": "https://tor.box/link",
                                                "expires_at": "1970-01-01T00:26:40Z"})] * 3,
        ("POST", "user/auth/device/token"): [refused("DEVICE_CODE_NOT_USED"), ok({"access_token": "NEW-TOKEN"}),
                                             refused("ITEM_NOT_FOUND")],
    }, token="")
    started = await admin.start_authorization(service=client, clock=lambda: clock[0])
    assert started == {"state": "pending", "user_code": "ABC123", "verification_url": "https://torbox.app/oauth/device",
                       "interval": 5, "expires_in": 600}
    assert "DEV" not in json.dumps(started)
    assert transport.calls[0]["params"] == {"app": "DebridPulse"}
    assert (await admin.poll_authorization(service=client, clock=lambda: clock[0]))["state"] == "pending"
    clock[0] += 5
    assert (await admin.poll_authorization(service=client, clock=lambda: clock[0]))["state"] == "pending"
    clock[0] += 5
    outcome = await admin.poll_authorization(service=client, clock=lambda: clock[0])
    assert isinstance(outcome, admin.Authorized) and outcome.token == "NEW-TOKEN"
    assert admin.authorization_state()["state"] == "idle"

    await admin.start_authorization(service=client, clock=lambda: clock[0])
    clock[0] += 5
    assert (await admin.poll_authorization(service=client, clock=lambda: clock[0]))["state"] == "expired"
    await admin.start_authorization(service=client, clock=lambda: 0.0)
    assert admin.authorization_state(clock=lambda: 2000.0)["state"] == "expired"
    await admin.cancel_authorization()


async def test_account_facts_come_from_the_account_never_a_token_lifetime():
    facts = admin.account_facts({"email": "a@e.net", "plan": 2, "premium_expires_at": "2099-02-18T04:08:43Z"})
    neutral = facts.pop("account")
    assert facts == {"email": "a@e.net", "plan": 2, "plan_name": "Pro", "premium": True,
                     "premium_expires_at": "2099-02-18T04:08:43Z"}
    assert (neutral["service_class"], neutral["plan"], neutral["functional"]) == ("premium", "Pro", "usable")
    free = admin.account_facts({"email": "f@e.net", "plan": 0, "premium_expires_at": "2099-02-18T04:08:43Z"})
    assert (free["plan_name"], free["premium"]) == ("Free", False)
    lapsed = admin.account_facts({"plan": 1, "premium_expires_at": "2001-01-01T00:00:00Z"})
    assert lapsed["premium"] is False


@pytest.mark.parametrize("answer, state", [
    (ok({"email": "a@e.net", "plan": 3, "premium_expires_at": "2099-01-01T00:00:00Z"}), "healthy"),
    (refused("BAD_TOKEN", 403), "auth_required"),
    (refused("NO_AUTH", 403), "auth_required"),
    (refused("DATABASE_ERROR", 500), "unhealthy"),
])
async def test_runtime_status_is_probed_truth(answer, state):
    client, _ = service({("GET", "user/me"): [answer]})
    status = await admin.runtime_status(SimpleNamespace(client=client), enabled=True)
    assert status["state"] == state
    assert TOKEN not in json.dumps(status)
    if state == "healthy":
        assert (status["plan_name"], status["premium"]) == ("Standard", True)


async def test_runtime_status_without_io_for_disabled_and_unconfigured():
    assert (await admin.runtime_status(None, enabled=False))["state"] == "disabled"
    client, transport = service({}, token="")
    assert (await admin.runtime_status(SimpleNamespace(client=client), enabled=True))["state"] == "unconfigured"
    assert transport.calls == []


# -- creation, observation, manifest, material -----------------------------------------

async def test_creation_requests_per_family():
    client, transport = service({
        ("POST", "torrents/createtorrent"): [ok({"torrent_id": 7, "hash": "a" * 40}),
                                             ok({"torrent_id": 8, "hash": "b" * 40})],
        ("POST", "webdl/createwebdownload"): [ok({"webdownload_id": 9})],
        ("POST", "usenet/createusenetdownload"): [ok({"usenetdownload_id": 11})],
    })
    assert await client.create_torrent(magnet=MAGNET) == "7"
    assert await client.create_torrent(metainfo=b"d4:infoe", name="x.torrent") == "8"
    assert await client.create_webdl("https://hoster.example/f/1") == "9"
    assert await client.create_usenet(NZB, name="show.nzb") == "11"
    magnet_form, file_form = form_fields(transport.calls[0]["data"]), form_fields(transport.calls[1]["data"])
    assert magnet_form == {"magnet": MAGNET, "allow_zip": "false"}
    assert file_form["file"] == b"d4:infoe" and file_form["allow_zip"] == "false"
    assert transport.calls[2]["data"] == {"link": "https://hoster.example/f/1"}
    assert form_fields(transport.calls[3]["data"])["file"] == NZB
    assert transport.calls[1]["timeout"] is client.upload_timeout
    assert transport.calls[3]["timeout"] is client.upload_timeout


@pytest.mark.parametrize("native, state", [
    (torrent(present=True, state="uploading"), ResourceState.AVAILABLE),
    (torrent(present=True, state="cached"), ResourceState.AVAILABLE),
    (torrent(state="completed"), ResourceState.PREPARING),  # not yet requestable
    (torrent(state="metaDL"), ResourceState.PREPARING),
    (torrent(state="stalled (no seeds)"), ResourceState.PREPARING),
    (torrent(state="paused"), ResourceState.PREPARING),
    (torrent(state="queuedDL"), ResourceState.PREPARING),
    (torrent(state="Repairing"), ResourceState.PREPARING),
    (torrent(state="Extracting"), ResourceState.PREPARING),
    (torrent(state="missing"), ResourceState.UNAVAILABLE),
    (torrent(state="error"), ResourceState.UNAVAILABLE),
    (torrent(state="failed (no articles)"), ResourceState.UNAVAILABLE),
    (torrent(state="teleporting"), ResourceState.UNKNOWN),
    (torrent(present=True, files=[]), ResourceState.PREPARING),
])
async def test_one_status_translator(native, state):
    observed = observation(TORRENT, native)
    assert observed.state == state
    if state == ResourceState.UNAVAILABLE:
        # TorBox's own acquisition failed: a provider-final failure another
        # provider of the same request may still satisfy.
        assert observed.error.domain == Domain.PROVIDER and provider_attributable(observed.error)
        assert TransferPolicy().retry_resolution(observed.error, 1, 0).action == Recovery.TRY_ALTERNATE_PROVIDER
    if state == ResourceState.UNKNOWN:
        assert observed.error.category == Category.UNMAPPED_PROVIDER_ERROR


async def test_a_webdl_error_is_a_failure_and_identity_is_family_scoped():
    observed = observation(WEBDL, {"id": 7, "name": "f", "size": 1, "error": "Hoster said no", "files": []})
    assert observed.state == ResourceState.UNAVAILABLE
    assert resource(TORRENT, "7").id != resource(WEBDL, "7").id != resource(USENET, "7").id
    assert resource(USENET, "7").context == {"family": USENET, "id": "7"}


async def test_member_paths_are_root_relative_safe_and_ordered():
    members = native_members(torrent())
    assert [(m.file_id, m.relative_path, m.expected_bytes) for m in members] == [
        ("0", "e01.mkv", 1000), ("1", "Extras/e02.mkv", 2000)]
    single = native_members(torrent(files=[{"id": 3, "name": "movie.mkv", "size": 5}], name="movie.mkv"))
    assert [m.relative_path for m in single] == ["movie.mkv"]
    with pytest.raises(ManifestInvalid):
        native_members(torrent(files=[{"id": 0, "name": "Show/../../etc/passwd", "size": 1}]))


async def test_member_addresses_are_durable_and_credential_free():
    address = member_address(USENET, "11", "4")
    assert address == f"{API}/usenet/requestdl?usenet_id=11&file_id=4"
    assert parse_member_address(address) == (USENET, "11", "4")
    assert TOKEN not in address
    for other in (f"{API}/usenet/requestdl?usenet_id=11&file_id=4&token=x", "https://evil.example/v1/api/usenet/"
                  "requestdl?usenet_id=11&file_id=4", f"{API}/torrents/requestdl?torrent_id=a&file_id=1"):
        assert parse_member_address(other) is None


class FakeClient:
    """A TorBox account: objects by family, scripted states, recorded calls."""

    def __init__(self, token=TOKEN):
        self.token = token
        self.objects = {TORRENT: {}, WEBDL: {}, USENET: {}}
        self.calls = []
        self.links = 0
        self.next_id = 100
        self.refusal = None
        self.hoster_list = [{"domains": ["hoster.example"], "status": True}]
        # Links TorBox's web-download cache holds (``webdl_cached``); empty: nothing cached.
        self.cache = set()
        # TorBox's queue of torrent submissions it has not started, by queued_id
        # -- the queue's own ids, never torrent ids.
        self.queued = {}

    @property
    def configured(self):
        return bool(self.token)

    def secrets(self):
        return (self.token,) if self.token else ()

    def _created(self, family, native):
        if self.refusal:
            raise self.refusal
        self.next_id += 1
        self.objects[family][str(self.next_id)] = {**native, "id": self.next_id}
        return str(self.next_id)

    async def create_torrent(self, *, magnet="", metainfo=None, name=""):
        self.calls.append(("create_torrent", magnet or metainfo))
        return self._created(TORRENT, torrent(state="downloading"))

    async def webdl_cached(self, links):
        self.calls.append(("webdl_cached", tuple(links)))
        return {link: {"name": "file.bin", "size": 9, "files": []} for link in links if link in self.cache}

    async def create_webdl(self, link, *, cached_only=False):
        if cached_only:
            self.calls.append(("create_webdl_cached", link))
            if link not in self.cache:
                return None
            return self._created(WEBDL, {"name": "file.bin", "size": 9, "download_state": "cached",
                                         "download_present": True, "files": [{"id": 0, "name": "file.bin", "size": 9}],
                                         "original_url": link})
        self.calls.append(("create_webdl", link))
        return self._created(WEBDL, {"name": "file.bin", "size": 9, "download_state": "downloading",
                                     "download_present": False, "files": [], "original_url": link})

    async def create_usenet(self, posting, *, name):
        content = posting if isinstance(posting, bytes) else posting.read()
        self.calls.append(("create_usenet", content, name))
        return self._created(USENET, {"name": "show", "size": 9, "download_state": "downloading",
                                      "download_present": False, "files": []})

    async def item(self, family, native_id):
        self.calls.append(("item", family, native_id))
        if native_id not in self.objects[family]:
            raise TorBoxAPIError("ITEM_NOT_FOUND", "", 404)
        return dict(self.objects[family][native_id])

    async def queued_torrents(self, offset, limit=1000):
        self.calls.append(("queued_torrents", offset))
        return list(self.queued.values())[offset:offset + limit]

    async def items(self, family, offset, limit=1000):
        values = list(self.objects[family].values())
        return values[offset:offset + limit]

    async def requestdl(self, family, native_id, file_id):
        self.calls.append(("requestdl", family, native_id, file_id))
        self.links += 1
        return f"https://store-1.tb-cdn.st/dld/{family}-{native_id}-{file_id}?token=link{self.links}"

    async def delete(self, family, native_id):
        self.calls.append(("delete", family, native_id))
        self.objects[family].pop(native_id, None)

    async def user(self):
        return {"email": "a@e.net", "plan": 2, "premium_expires_at": "2099-01-01T00:00:00Z"}

    async def hosters(self):
        return self.hoster_list


async def test_resolution_creates_and_observes_each_family(tmp_path):
    client = FakeClient()
    staged = StagedInputStore(str(tmp_path / "staged"))
    provider = TorBoxProvider(client, usenet=True, staged_input=staged)
    magnet = await provider.resolve(TransferRequest("magnet", MAGNET, "Show", "a" * 40))
    webdl = await provider.resolve(TransferRequest("https", "https://hoster.example/f/1", "file.bin"))
    usenet = await provider.resolve(TransferRequest("nzb", staged.stage_bytes(NZB), "show.nzb"))
    for result, family in ((magnet, TORRENT), (webdl, WEBDL), (usenet, USENET)):
        assert result.state == ResourceState.PREPARING
        assert result.observation.resource.context["family"] == family
        assert result.observation.resource.ownership == Ownership.CREATED
    assert ("create_usenet", NZB, "show.nzb") in client.calls


async def test_the_canonical_nzb_is_one_path_whatever_its_representation(tmp_path):
    client = FakeClient()
    staged = StagedInputStore(str(tmp_path / "staged"))
    provider = TorBoxProvider(client, usenet=True, staged_input=staged)
    await provider.resolve(TransferRequest("nzb", staged.stage_bytes(NZB), "show.nzb"))
    await provider.resolve(TransferRequest("nzb", NZB, "show.nzb"))
    uploads = [call for call in client.calls if call[0] == "create_usenet"]
    assert uploads == [("create_usenet", NZB, "show.nzb")] * 2
    assert not any(call[0] in {"create_webdl"} for call in client.calls)


async def test_manifest_members_resolve_to_fresh_material_for_the_exact_file():
    client = FakeClient()
    provider = TorBoxProvider(client)
    native_id = client._created(TORRENT, torrent(present=True, state="uploading"))
    entries = await provider.manifest(resource(TORRENT, native_id, ownership=Ownership.CREATED))
    assert [(entry.relative_path, entry.expected_bytes) for entry in entries] == [
        ("e01.mkv", 1000), ("Extras/e02.mkv", 2000)]
    member = entries[1].request
    assert (member.kind, member.preferred_provider) == ("https", "torbox")
    assert parse_member_address(member.payload) == (TORRENT, native_id, "1")
    assert TOKEN not in member.payload

    first = (await provider.resolve(member)).candidates[0]
    second = (await provider.refresh(first)).candidates[0]
    assert client.calls[-1] == ("requestdl", TORRENT, native_id, "1")
    assert first.endpoints[0].address != second.endpoints[0].address  # regenerated, never reused
    assert first.refresh_request == member and first.delivery == DeliveryKind.PROVIDER_ISSUED
    assert first.expires_at is not None


async def test_a_web_download_names_the_hoster_it_came_from_never_torbox():
    client = FakeClient()
    provider = TorBoxProvider(client)
    submitted = "https://WWW.Hoster.Example/d/abc123/file.bin?sig=S3CR3T&expires=9#frag"
    root = await provider.resolve(TransferRequest("https", submitted, "file.bin"))
    native_id = identity(root.observation.resource)[1]
    assert identity(root.observation.resource) == (WEBDL, native_id)  # resource identity unchanged
    client.objects[WEBDL][native_id].update(download_present=True, download_state="completed",
                                            files=[{"id": 3, "name": "file.bin", "size": 9}])
    member = (await provider.manifest(root.observation.resource))[0].request
    assert parse_member_address(member.payload) == (WEBDL, native_id, "3")  # member identity unchanged
    for secret in ("abc123", "S3CR3T", "sig", "expires", "frag", "file.bin", "/d/"):
        assert secret not in member.payload
    first = (await provider.resolve(member)).candidates[0]
    refreshed = (await provider.refresh(first)).candidates[0]
    for candidate in (first, refreshed):
        assert candidate.source_identity == SourceIdentity("host", "hoster.example")
        assert candidate.delivery == DeliveryKind.PROVIDER_ISSUED
        assert urlsplit(candidate.endpoints[0].address).hostname == "store-1.tb-cdn.st"  # execution material
    assert client.calls[-1] == ("requestdl", WEBDL, native_id, "3")  # the member, never the hoster URL
    assert [call[0] for call in client.calls].count("create_webdl") == 1  # refresh never resubmits


async def test_a_web_download_with_no_safe_hoster_names_no_source_rather_than_torbox():
    client = FakeClient()
    provider = TorBoxProvider(client)
    for original in (None, "", "ftp://hoster.example/x", "https://user:pw@[::1]/x", "not a url", 7):
        native_id = client._created(WEBDL, {"name": "f", "size": 1, "download_present": True,
                                            "files": [{"id": 0, "name": "f", "size": 1}], "original_url": original})
        member = (await provider.manifest(resource(WEBDL, native_id)))[0].request
        assert parse_member_address(member.payload) == (WEBDL, native_id, "0")
        assert (await provider.resolve(member)).candidates[0].source_identity is None


async def test_torrent_and_nzb_members_keep_their_source_identity(tmp_path):
    client = FakeClient()
    provider = TorBoxProvider(client)
    for family, native in ((TORRENT, torrent(present=True, state="cached")),
                           (USENET, {"name": "s", "size": 1, "download_present": True,
                                     "files": [{"id": 0, "name": "s.mkv", "size": 1}],
                                     "original_url": "https://indexer.example/get?apikey=K"})):
        native_id = client._created(family, native)
        member = (await provider.manifest(resource(family, native_id)))[0].request
        assert "source_host" not in member.payload and "indexer" not in member.payload
        candidate = (await provider.resolve(member)).candidates[0]
        assert candidate.source_identity == SourceIdentity("host", "api.torbox.app")


async def test_the_source_host_field_is_strict_and_web_download_only():
    address = member_address(WEBDL, "5", "1", source_host="hoster.example")
    assert address == f"{API}/webdl/requestdl?web_id=5&file_id=1&source_host=hoster.example"
    assert (parse_member_address(address), member_source_host(address)) == ((WEBDL, "5", "1"), "hoster.example")
    assert member_source_host(member_address(WEBDL, "5", "1")) is None
    for host in ("Hoster.Example", "hoster.example/x", "a@b.example", "", "hoster.example:8080"):
        with pytest.raises(ValueError):
            member_address(WEBDL, "5", "1", source_host=host)
    with pytest.raises(ValueError):
        member_address(TORRENT, "5", "1", source_host="hoster.example")
    for other in (f"{API}/torrents/requestdl?torrent_id=5&file_id=1&source_host=hoster.example",
                  f"{API}/webdl/requestdl?web_id=5&file_id=1&source_host=EVIL.example",
                  f"{API}/webdl/requestdl?web_id=5&file_id=1&source_host=a.example&source_host=b.example",
                  f"{API}/webdl/requestdl?web_id=5&file_id=1#source_host=hoster.example"):
        assert parse_member_address(other) is None and member_source_host(other) is None


async def test_material_carrying_the_account_token_is_transient_execution_material():
    """Transfer 478: TorBox's requestdl may issue a link that embeds the account
    token. That is not a protocol violation -- it is execution material, which
    is transient: usable now, never durable."""
    from transfers import codec
    client = FakeClient()

    async def tokenized(family, native_id, file_id):
        return f"https://store-1.tb-cdn.st/dld/x?token={TOKEN}"
    client.requestdl = tokenized
    member = TransferRequest("https", member_address(TORRENT, "1", "0"), "x")
    [candidate] = (await TorBoxProvider(client).resolve(member)).candidates
    [endpoint] = candidate.endpoints
    assert endpoint.transient and TOKEN in endpoint.address          # usable by the executor now
    durable = codec.dump(candidate)
    assert TOKEN not in durable and "tb-cdn" not in durable          # never durable
    restored = codec.candidate(codec.load(durable))
    assert restored.endpoints[0].transient and restored.endpoints[0].address == ""
    assert restored.refresh_request == member and TOKEN not in json.dumps(codec.load(durable)["refresh_request"])


async def test_material_still_crosses_network_safety():
    client = FakeClient()

    async def private(family, native_id, file_id):
        return "http://10.0.0.5/dld/x?token=link"
    client.requestdl = private
    with pytest.raises(TransferError) as caught:
        await TorBoxProvider(client).resolve(TransferRequest("https", member_address(TORRENT, "1", "0"), "x"))
    assert caught.value.error.domain == Domain.SECURITY


async def test_observe_by_identity_after_restart_and_absent_when_deleted():
    client = FakeClient()
    native_id = client._created(TORRENT, torrent(present=True, state="cached"))
    rebuilt = TorBoxProvider(client)
    durable = resource(TORRENT, native_id, ownership=Ownership.CREATED)
    assert (await rebuilt.observe(durable)).state == ResourceState.AVAILABLE
    client.objects[TORRENT].clear()
    gone = await rebuilt.observe(durable)
    assert gone.state == ResourceState.ABSENT and gone.error.category == Category.RESOURCE_NOT_FOUND


@pytest.mark.parametrize("ownership, deleted", [
    (Ownership.CREATED, True), (Ownership.ADOPTED, True), (Ownership.OBSERVED, False),
])
async def test_cleanup_respects_ownership(ownership, deleted):
    client = FakeClient()
    native_id = client._created(TORRENT, torrent())
    outcome = await TorBoxProvider(client).cleanup(
        CleanupDirective(resource(TORRENT, native_id, ownership=ownership), CleanupAuthority.OWNED))
    assert ((native_id not in client.objects[TORRENT]), outcome.kind) == (
        (True, OutcomeKind.SUCCESS) if deleted else (False, OutcomeKind.SKIPPED))


async def test_a_duplicate_torrent_is_adopted_only_by_its_exact_hash():
    client = FakeClient()
    existing = client._created(TORRENT, torrent(present=True, state="cached"))
    client.refusal = TorBoxAPIError("DUPLICATE_ITEM", "exists", 400)
    result = await TorBoxProvider(client).resolve(TransferRequest("magnet", MAGNET, "Show", "a" * 40))
    assert result.observation.resource.ownership == Ownership.ADOPTED
    assert result.observation.resource.context == {"family": TORRENT, "id": existing}
    with pytest.raises(TransferError):
        await TorBoxProvider(client).resolve(TransferRequest("magnet", MAGNET, "Show", "c" * 40))


async def test_inventory_merges_every_family():
    client = FakeClient()
    client._created(TORRENT, torrent())
    client._created(WEBDL, {"name": "w", "size": 1, "download_state": "downloading", "files": []})
    client._created(USENET, {"name": "u", "size": 1, "download_state": "downloading", "files": []})
    snapshot = await TorBoxProvider(client).inventory()
    assert snapshot.complete and sorted(item.resource.context["family"] for item in snapshot.observations) == [
        TORRENT, USENET, WEBDL]


@pytest.mark.parametrize("code, category, attributable", [
    ("BAD_TOKEN", Category.CREDENTIAL_INVALID, True), ("NO_AUTH", Category.CREDENTIAL_MISSING, True),
    ("PLAN_RESTRICTED_FEATURE", Category.ACCOUNT_LIMITED, True), ("ACTIVE_LIMIT", Category.CONCURRENCY_LIMITED, True),
    ("MONTHLY_LIMIT", Category.QUOTA_EXCEEDED, True), ("TOO_MUCH_DATA", Category.ACCOUNT_LIMITED, True),
    ("UNSUPPORTED_SITE", Category.UNSUPPORTED_REQUEST, True), ("DATABASE_ERROR", Category.PROVIDER_UNAVAILABLE, True),
    ("ITEM_NOT_FOUND", Category.RESOURCE_NOT_FOUND, True), ("BOZO_NZB", Category.INVALID_REQUEST, False),
    ("BOZO_TORRENT", Category.INVALID_REQUEST, False), ("INVALID_LINK", Category.INVALID_REQUEST, False),
    ("LINK_OFFLINE", Category.SOURCE_NOT_FOUND, False), ("SOMETHING_NEW", Category.UNMAPPED_PROVIDER_ERROR, True),
])
async def test_error_translation(code, category, attributable):
    error = translate_error(TorBoxAPIError(code, "detail", 400))
    assert error.category == category and error.integration_id == "torbox"
    assert provider_attributable(error) is attributable
    assert translate_error(TorBoxAPIError("", "", 429)).retryability == Retryability.BACKOFF
    assert translate_error(asyncio.TimeoutError()).category == Category.CONNECTION_TIMEOUT


# -- applicability ---------------------------------------------------------------------

async def test_positive_inventory_claims_only():
    snapshot = parse_native_host_snapshot([{"domains": ["hoster.example", "rg.to"], "status": True},
                                           {"domains": ["bad/host", 7], "status": True}, "junk"])
    assert snapshot.domains == ("hoster.example", "rg.to")
    facts = TorBoxRequestApplicability(snapshot)
    claimed = facts(TransferRequest("https", "https://cdn.hoster.example/f/1"))
    assert [claim.host for claim in claimed.specialized_hosts] == ["cdn.hoster.example"]
    assert facts(TransferRequest("https", "https://unlisted.example/f/1")).specialized_hosts == ()
    unresolved = TorBoxRequestApplicability(None)(TransferRequest("https", "https://hoster.example/f/1"))
    assert unresolved.readiness == ApplicabilityReadiness.UNRESOLVED
    own = TorBoxRequestApplicability(None)(TransferRequest("https", member_address(TORRENT, "1", "0")))
    assert own.readiness == ApplicabilityReadiness.READY and own.specialized_hosts[0].host == "api.torbox.app"
    with pytest.raises(TorBoxHostSnapshotError):
        parse_native_host_snapshot([{"domains": ["not a domain"], "status": True}])


def claimed(snapshot, url):
    facts = TorBoxRequestApplicability(snapshot)(TransferRequest("https", url))
    return facts.readiness, [claim.host for claim in facts.specialized_hosts]


async def test_only_hosters_torbox_says_are_usable_now_are_claimed():
    # TorBox's documented, source-verified shape: ``status`` is a JSON boolean,
    # true when the hoster can be used on TorBox at the current time.
    usable = parse_native_host_snapshot([{"domains": ["up.example"], "status": True}])
    assert claimed(usable, "https://up.example/f") == (ApplicabilityReadiness.READY, ["up.example"])
    down = parse_native_host_snapshot([{"domains": ["down.example"], "status": False}])
    assert claimed(down, "https://down.example/f") == (ApplicabilityReadiness.READY, [])
    mixed = parse_native_host_snapshot([{"domains": ["up.example", "up2.example"], "status": True},
                                        {"domains": ["down.example"], "status": False}])
    assert mixed.domains == ("up.example", "up2.example")
    assert claimed(mixed, "https://down.example/f")[1] == []
    assert claimed(mixed, "https://up2.example/f")[1] == ["up2.example"]


@pytest.mark.parametrize("status", [None, "true", 1, 0, "false", [], {}])
async def test_an_unreadable_status_is_never_a_usable_one(status):
    record = {"domains": ["odd.example"]} if status is None else {"domains": ["odd.example"], "status": status}
    snapshot = parse_native_host_snapshot([record, {"domains": ["up.example"], "status": True}])
    assert snapshot.domains == ("up.example",)  # contained: skipped, never claimed
    with pytest.raises(TorBoxHostSnapshotError):
        parse_native_host_snapshot([record])  # nothing well formed: not a snapshot at all


async def test_every_hoster_unavailable_is_resolved_data_not_malformed_data():
    snapshot = parse_native_host_snapshot([{"domains": ["a.example"], "status": False},
                                           {"domains": ["b.example"], "status": False}])
    assert snapshot.domains == ()
    facts = applicability_facts(snapshot)
    assert facts.readiness == ApplicabilityReadiness.READY
    assert [claim.host for claim in facts.specialized_hosts] == ["api.torbox.app"]  # nothing fabricated
    assert claimed(snapshot, "https://a.example/f") == (ApplicabilityReadiness.READY, [])
    assert decode_host_snapshot(encode_host_snapshot(snapshot)) == snapshot
    for malformed in ([], [{"status": True}], [{"domains": [], "status": True}], {"data": []}):
        with pytest.raises(TorBoxHostSnapshotError):
            parse_native_host_snapshot(malformed)


class MemoryStore:
    def __init__(self):
        self.records = {}

    async def load(self, integration_id, state_key):
        return self.records.get((integration_id, state_key))

    async def replace(self, integration_id, payload, *, schema_version, state_key, observed_at, successful_at,
                      stale_after, expected_generation):
        generation = expected_generation + 1
        record = SimpleNamespace(payload=payload, schema_version=schema_version, generation=generation,
                                 is_stale=lambda now=None: now >= stale_after)
        self.records[(integration_id, state_key)] = record
        return record


async def test_host_maintenance_refreshes_restores_lkg_and_announces_changes():
    store, announced = MemoryStore(), []
    provider = TorBoxProvider(FakeClient())
    maintenance = TorBoxHostMaintenance(provider, store, notify=announced.append, clock=lambda: 1000.0)
    assert provider.applicability.readiness == ApplicabilityReadiness.UNRESOLVED
    await maintenance.start()
    await maintenance.maintain()
    assert provider.applicability.readiness == ApplicabilityReadiness.READY and announced == ["torbox"]
    assert store.records[("torbox", "supported-hosts")].schema_version == HOST_SCHEMA_VERSION

    restarted = TorBoxProvider(FakeClient())
    restarted.client.hosters = None  # a restart restores; it fetches nothing
    await TorBoxHostMaintenance(restarted, store, clock=lambda: 1000.0).start()
    assert restarted.applicability_for(TransferRequest("https", "https://hoster.example/x")).specialized_hosts


async def test_a_failed_refresh_keeps_the_last_valid_applicability():
    store, now = MemoryStore(), [1000.0]
    provider = TorBoxProvider(FakeClient())
    provider.client.hoster_list = [{"domains": ["up.example"], "status": True},
                                   {"domains": ["down.example"], "status": False}]
    maintenance = TorBoxHostMaintenance(provider, store, clock=lambda: now[0], refresh_seconds=60, retry_seconds=1)
    await maintenance.start()
    await maintenance.maintain()
    before = (provider.applicability, claimed_by(provider, "https://up.example/f"),
              claimed_by(provider, "https://down.example/f"))
    assert before[1:] == (["up.example"], [])
    for failure in ([{"domains": ["up.example"], "status": "yes"}], [], RuntimeError("unreachable")):
        async def hosters(failure=failure):
            if isinstance(failure, Exception):
                raise failure
            return failure
        provider.client.hosters = hosters
        now[0] += 120
        await maintenance.maintain()
        assert (provider.applicability, claimed_by(provider, "https://up.example/f"),
                claimed_by(provider, "https://down.example/f")) == before
    assert decode_host_snapshot(store.records[("torbox", "supported-hosts")].payload).domains == ("up.example",)


def claimed_by(provider, url):
    return [claim.host for claim in provider.applicability_for(TransferRequest("https", url)).specialized_hosts]


async def test_the_catalogue_carries_no_account_truth():
    client, transport = service({("GET", "webdl/hosters"): [ok([{"domains": ["hoster.example"]}])]})
    await client.hosters()
    assert "Authorization" not in transport.calls[0]["headers"]


# -- routing through the existing neutral owners ---------------------------------------

def nzb_registry(*, torbox_usenet: bool, torbox_enabled: bool = True, native: bool = True, staged=None):
    registry = IntegrationRegistry()
    provider = TorBoxProvider(FakeClient(token=TOKEN if torbox_enabled else ""), usenet=torbox_usenet,
                              staged_input=staged)
    TorBoxHostMaintenance(provider, MemoryStore())
    registry.register_provider(provider)
    if native:
        registry.register_provider(UsenetProvider(staged_input=staged))
    return registry, provider


@pytest.mark.parametrize("torbox_enabled, toggle, native, expected", [
    (True, True, True, ["torbox", "usenet"]),
    (True, True, False, ["torbox"]),
    (True, False, True, ["usenet"]),
    (True, False, False, []),
    (False, True, True, ["usenet"]),
    (False, False, False, []),
])
async def test_the_nzb_route_matrix_emerges_from_participation(torbox_enabled, toggle, native, expected):
    registry, _ = nzb_registry(torbox_usenet=toggle, torbox_enabled=torbox_enabled, native=native)
    eligible = registry.eligible_providers(TransferRequest("nzb", NZB, "show.nzb"))
    assert [provider.descriptor.id for provider in eligible] == expected


async def test_torbox_claims_magnets_and_positively_listed_hosts_only():
    registry, provider = nzb_registry(torbox_usenet=False, native=False)
    assert registry.provider_for(TransferRequest("magnet", MAGNET, "Show", "a" * 40)) is provider
    store = MemoryStore()
    maintenance = TorBoxHostMaintenance(provider, store, clock=lambda: 1000.0)
    await maintenance.maintain()
    assert registry.provider_for(TransferRequest("https", "https://hoster.example/f/1")) is provider
    with pytest.raises(TransferError):
        registry.provider_for(TransferRequest("https", "https://unlisted.example/f/1"))


async def test_a_torbox_failure_reaches_native_usenet_through_neutral_failover(tmp_path, monkeypatch):
    from fake_integrations import MemoryExecutor
    from transfers.convergence_engine import TransferEngine
    from transfers.recovery_repository import TransferRepository

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    staged = StagedInputStore(str(tmp_path / "staged"))
    registry, torbox = nzb_registry(torbox_usenet=True, staged=staged)
    registry.register_executor(MemoryExecutor(TransferRepository().authorize_execution))
    repository = TransferRepository()
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "downloads"),
                            policy=TransferPolicy(retry_delay=0.0), clock=lambda: 1000.0)
    await engine.initialize()
    transfer = await engine.submit((TransferRequest("nzb", staged.stage_bytes(NZB), "show.nzb"),), name="show")

    await engine.resolve_pending()
    [created] = torbox.client.objects[USENET].values()
    created["download_state"] = "missing"  # TorBox could not get every article
    for _ in range(6):
        await engine.resolve_pending()

    root = next(item for item in await repository.requests(transfer.id) if item.parent_id is None)
    routes = (await repository.presentation(transfer.id, details=True))["route_attempts"]
    assert [(item["provider_id"], item["resolution_state"]) for item in routes][:2] == [
        ("torbox", "exhausted"), ("usenet", "succeeded")]
    assert (await repository.get(transfer.id)).id == transfer.id
    assert await repository.exhausted_route_providers(root.id) == frozenset({"torbox"})
    # The neutral handoff to native Usenet is untouched by collection generic
    # closure: no generic route is ever involved and the request is not pinned.
    assert {item["provider_id"] for item in routes} == {"torbox", "usenet"}
    assert await repository.bound_route_provider(root.id) == "usenet"
    # Owned TorBox remote object cleaned up through the one cleanup cadence.
    assert ("delete", USENET, str(created["id"])) in torbox.client.calls


async def test_definition_and_registration_contract():
    assert definition.id == "torbox" and definition.default_enabled is False
    assert definition.secret_fields == frozenset({"api_token"})
    assert definition.presentation.premium and definition.presentation.status_tier_label == "Premium Services"
    assert definition.presentation.status_endpoint == "/integration-status/torbox"
    options = TorBoxOptions()
    assert (options.usenet_enabled, options.rate_limit_per_minute) == (False, 240)
    with pytest.raises(Exception):
        TorBoxOptions(rate_limit_per_minute=301)
    entry = SimpleNamespace(options={"api_token": TOKEN, "usenet_enabled": True}, enabled=True, priority=0)
    built = definition.build(entry, SimpleNamespace(staged_input=None, commands=None))
    assert "nzb" in built.descriptor.request_types and built.descriptor.enabled
    plain = SimpleNamespace(options={"api_token": TOKEN}, enabled=True, priority=0)
    assert "nzb" not in definition.build(plain, SimpleNamespace()).descriptor.request_types


# -- web-download cache first -------------------------------------------------------
#
# TorBox's cache answer is a safe pre-acquisition fact; a cached link is added
# only with ``add_only_if_cached`` (no hoster acquisition), an uncached link is
# created only by an ordinary resolution, and nothing is ever queued as a
# stand-in for "not authorized yet": an unauthorized miss has NO remote object.

HOSTER = "https://hoster.example/f/1"


async def test_the_cache_is_read_by_url_key_in_one_batched_call_that_creates_nothing():
    import hashlib
    other = "https://hoster.example/f/2"
    hit = hashlib.md5(HOSTER.encode()).hexdigest()
    client, transport = service({("POST", "webdl/checkcached"): [
        ok({hit: {"name": "file.bin", "size": 9, "hash": hit, "files": [{"id": 0, "name": "file.bin", "size": 9}]}}),
        ok(None),
    ]})
    found = await client.webdl_cached((HOSTER, other))
    assert list(found) == [HOSTER] and found[HOSTER]["files"][0]["name"] == "file.bin"
    call = transport.calls[0]
    assert call["params"] == {"format": "object", "list_files": "true"}
    assert json.loads(call["data"]) == {"hashes": [hit, hashlib.md5(other.encode()).hexdigest()]}
    assert await client.webdl_cached((other,)) == {}  # TorBox answers an all-miss with no data
    assert [item["url"].removeprefix(API + "/") for item in transport.calls] == ["webdl/checkcached"] * 2


@pytest.mark.parametrize("answer", [ok(["not", "an", "object"]), ok({"k": "not an entry"})])
async def test_a_malformed_cache_answer_is_a_protocol_failure(answer):
    from providers.torbox.client import TorBoxProtocolError, webdl_cache_key
    if isinstance(answer[1]["data"], dict):
        answer[1]["data"] = {webdl_cache_key(HOSTER): "not an entry"}
    client, _ = service({("POST", "webdl/checkcached"): [answer]})
    with pytest.raises(TorBoxProtocolError):
        await client.webdl_cached((HOSTER,))


async def test_a_failed_cache_check_fails_resolution_and_creates_nothing():
    client = FakeClient()

    async def failing(links):
        raise TorBoxAPIError("UNKNOWN_ERROR", "Failed to retrieve web download cache status.", 500)

    client.webdl_cached = failing
    provider = TorBoxProvider(client)
    with pytest.raises(TransferError):
        await provider.resolve(TransferRequest("https", HOSTER, "file.bin"))
    assert client.objects[WEBDL] == {}
    with pytest.raises(TransferError):
        await provider.cache_presence((TransferRequest("https", HOSTER, "file.bin"),))


async def test_cached_only_creation_shape_and_its_not_cached_answer():
    client, transport = service({("POST", "webdl/createwebdownload"): [
        ok({"webdownload_id": 4}),
        refused("DOWNLOAD_NOT_CACHED", detail="not found in cache"),
        refused("UNSUPPORTED_SITE"),
    ]})
    assert await client.create_webdl(HOSTER, cached_only=True) == "4"
    assert transport.calls[0]["data"] == {"link": HOSTER, "add_only_if_cached": "true"}
    assert await client.create_webdl(HOSTER, cached_only=True) is None  # nothing was added
    with pytest.raises(TorBoxAPIError):
        await client.create_webdl(HOSTER, cached_only=True)  # any other refusal is a refusal
    assert all("as_queued" not in (call["data"] or {}) for call in transport.calls)


async def test_a_cached_link_is_added_only_from_the_cache_and_a_miss_creates_it_ordinarily():
    client = FakeClient()
    client.cache.add(HOSTER)
    provider = TorBoxProvider(client)
    cached = await provider.resolve(TransferRequest("https", HOSTER, "file.bin"))
    assert [call[0] for call in client.calls if call[0] != "item"] == ["webdl_cached", "create_webdl_cached"]
    assert cached.observation.resource.ownership == Ownership.CREATED
    client.calls.clear()
    await provider.resolve(TransferRequest("https", "https://hoster.example/f/2", "file.bin"))
    assert [call[0] for call in client.calls if call[0] != "item"] == ["webdl_cached", "create_webdl"]


async def test_a_stale_cache_answer_never_falls_through_to_productive_creation():
    client = FakeClient()
    client.cache.add(HOSTER)
    real = client.create_webdl

    async def gone(link, *, cached_only=False):
        client.cache.discard(link)  # evicted between the check and the add
        return await real(link, cached_only=cached_only)

    client.create_webdl = gone
    provider = TorBoxProvider(client)
    with pytest.raises(TransferError) as failure:
        await provider.resolve(TransferRequest("https", HOSTER, "file.bin"))
    assert failure.value.error.retryability == Retryability.BACKOFF
    assert "create_webdl" not in [call[0] for call in client.calls]
    assert client.objects[WEBDL] == {}


async def test_presence_and_cached_resolution_without_authorization_create_no_remote_object():
    client = FakeClient()
    client.cache.add(HOSTER)
    provider = TorBoxProvider(client)
    miss = TransferRequest("https", "https://hoster.example/f/2", "file.bin")
    member = TransferRequest("https", member_address(WEBDL, "5", "0"), "file.bin")
    presence = await provider.cache_presence((TransferRequest("https", HOSTER), miss, member,
                                              TransferRequest("magnet", MAGNET)))
    assert [item.value for item in presence] == ["hit", "miss", "unknown", "unknown"]
    assert [call[0] for call in client.calls] == ["webdl_cached"]  # one batched read
    # DP authorization absent: a miss is resolved from the cache only -- and
    # there is then NO remote object at all, not a queued or placeholder one.
    assert await provider.resolve_cached(miss) is None
    assert client.objects[WEBDL] == {}
    assert "create_webdl" not in [call[0] for call in client.calls]
    held = await provider.resolve_cached(TransferRequest("https", HOSTER, "file.bin"))
    assert held.observation.resource.ownership == Ownership.CREATED and len(client.objects[WEBDL]) == 1


async def test_a_cache_key_is_never_integrity_evidence():
    client = FakeClient()
    client.cache.add(HOSTER)
    provider = TorBoxProvider(client)
    root = await provider.resolve_cached(TransferRequest("https", HOSTER, "file.bin"))
    native_id = identity(root.observation.resource)[1]
    entries = await provider.manifest(root.observation.resource)
    assert all(entry.integrity == () for entry in entries)
    member = (await provider.resolve(entries[0].request)).candidates[0]
    assert member.integrity == () and member.content_evidence is None
    assert native_id and root.observation.fingerprint == ""


async def test_cleanup_of_a_cache_created_object_is_owned_and_an_observed_one_is_retained():
    client = FakeClient()
    client.cache.add(HOSTER)
    provider = TorBoxProvider(client)
    root = await provider.resolve_cached(TransferRequest("https", HOSTER, "file.bin"))
    owned = await provider.cleanup(CleanupDirective(root.observation.resource, CleanupAuthority.OWNED))
    assert owned.kind == OutcomeKind.SUCCESS and ("delete", WEBDL, identity(root.observation.resource)[1]) in client.calls
    observed = replace_ownership(root.observation.resource, Ownership.OBSERVED)
    retained = await provider.cleanup(CleanupDirective(observed, CleanupAuthority.OWNED))
    assert retained.kind == OutcomeKind.SKIPPED


def replace_ownership(resource_value, ownership):
    from dataclasses import replace as _replace
    return _replace(resource_value, ownership=ownership)


# -- the native wire, freshness, the queue and unreadable answers ------------------------
#
# Transfer 526: TorBox named two real torrents, yet a fresh per-id read of
# each sometimes came back "TorBox returned invalid JSON" with every fact of
# the answer discarded. These prove the documented native contract and that
# such an answer now carries what it actually was. None of them claims to be
# the cause of 526's unreadable answer: that is TorBox's wire to tell.

async def on_the_wire(data):
    """What aiohttp actually sends for ``data``, received by a local server
    through this client's own transport: the Content-Type and the fields."""
    from aiohttp import web
    seen = {}

    async def receive(request):
        seen["content_type"] = request.headers.get("Content-Type", "")
        form = await request.post()
        seen["fields"] = {key: value.file.read() if hasattr(value, "file") else value for key, value in form.items()}
        return web.json_response(ok({"torrent_id": 7})[1])

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


async def test_a_magnet_creation_is_multipart_on_the_wire():
    """TB-FB1 (contract hygiene): createtorrent is documented
    multipart/form-data; a form of plain strings is not multipart by itself."""
    client, transport = service({("POST", "torrents/createtorrent"): [ok({"torrent_id": 7}), ok({"torrent_id": 8})],
                                 ("POST", "webdl/createwebdownload"): [ok({"webdownload_id": 9})]})
    assert await client.create_torrent(magnet=MAGNET) == "7"
    assert await client.create_torrent(metainfo=b"d4:infoe", name="x.torrent") == "8"
    assert await client.create_webdl("https://hoster.example/f/1") == "9"
    magnet = await on_the_wire(transport.calls[0]["data"])
    assert magnet["content_type"].startswith("multipart/form-data; boundary=")
    assert magnet["fields"] == {"magnet": MAGNET, "allow_zip": "false"}
    upload = await on_the_wire(transport.calls[1]["data"])
    assert upload["content_type"].startswith("multipart/form-data; boundary=")
    assert upload["fields"] == {"file": b"d4:infoe", "allow_zip": "false"}
    # createwebdownload is documented application/x-www-form-urlencoded: unchanged.
    webdl = await on_the_wire(transport.calls[2]["data"])
    assert webdl["content_type"] == "application/x-www-form-urlencoded"


def queued(queued_id=7, **extra):
    """One entry of TorBox's queue: its ``id`` is a queued_id."""
    return {"id": queued_id, "created_at": "2026-10-07T00:33:36Z", "magnet": MAGNET, "hash": "a" * 40,
            "name": "Show", "type": "torrent", **extra}


def observing(script):
    client, transport = service(script)
    return TorBoxProvider(client), transport


def asked(transport):
    return [(call["url"].removeprefix(API + "/"), call["params"]) for call in transport.calls]


CURRENT = ("torrents/mylist", {"id": "7", "bypass_cache": "true"})
QUEUE = ("queued/getqueued", {"type": "torrent", "offset": 0, "limit": 1000, "bypass_cache": "true"})


async def test_a_torrent_id_is_never_read_as_a_queued_id():
    """TB-ID1: torrent ids and queued ids are separate namespaces. A bound
    torrent TorBox no longer holds is absent under the existing semantics --
    the queue entry that happens to carry the same number is another object
    and is never consulted, returned or reported PREPARING for it."""
    provider, transport = observing({("GET", "torrents/mylist"): [refused("ITEM_NOT_FOUND", 404)],
                                     ("GET", "queued/getqueued"): [ok(queued(7))]})
    bound = resource(TORRENT, "7", ownership=Ownership.CREATED)
    gone = await provider.observe(bound)
    assert gone.state == ResourceState.ABSENT and gone.error.category == Category.RESOURCE_NOT_FOUND
    assert gone.resource == bound and gone.name == "" and gone.fingerprint == ""
    assert asked(transport) == [CURRENT]


async def test_a_queued_create_answer_binds_no_queued_id():
    """TB-ID4: TorBox accepted the torrent into its queue and answered only a
    queued_id. The creation happened, so it stays UNCERTAIN for the existing
    reconciliation; nothing is bound, and the queued_id is never read as a
    torrent id or named as one."""
    provider, transport = observing({("POST", "torrents/createtorrent"): [
        ok({"queued_id": 55, "hash": "a" * 40, "auth_id": "x", "active_limit": 1, "current_active_downloads": 1})]})
    with pytest.raises(TransferError) as caught:
        await provider.resolve(TransferRequest("magnet", MAGNET, "Show", "a" * 40))
    error = caught.value.error
    assert error.mutation == MutationOutcome.UNCERTAIN
    assert "accepted the torrent into its queue without a current torrent_id" in error.diagnostic
    assert "55" not in error.diagnostic
    assert [url for url, _params in asked(transport)] == ["torrents/createtorrent"]


async def test_a_current_create_answer_binds_its_torrent_id_and_never_asks_the_queue():
    """TB-ID5."""
    provider, transport = observing({("POST", "torrents/createtorrent"): [ok({"torrent_id": 7, "hash": "a" * 40})],
                                     ("GET", "torrents/mylist"): [ok(torrent(7))]})
    result = await provider.resolve(TransferRequest("magnet", MAGNET, "Show", "a" * 40))
    assert result.state == ResourceState.PREPARING
    assert result.observation.resource.context == {"family": TORRENT, "id": "7"}
    assert result.observation.resource.ownership == Ownership.CREATED
    assert asked(transport) == [("torrents/createtorrent", {}), CURRENT]


async def test_a_stale_list_that_lacks_a_torrent_never_makes_it_absent():
    """TB-P1: TorBox refreshes its list only every 600 s unless asked to bypass
    that cache, so a torrent just created may be missing from the cached list
    yet present in a fresh read. Only fresh reads decide presence."""
    calls = []

    async def transport(method, url, *, headers=None, params=None, data=None, timeout=None):
        calls.append(dict(params or {}))
        fresh = (params or {}).get("bypass_cache") == "true"
        status, payload = ok(torrent(present=True, state="cached") if fresh else None)
        return RawResponse(status, {}, json.dumps(payload).encode())

    provider = TorBoxProvider(TorBoxService(TOKEN, rate_limiter=NoLimit(), transport=transport))
    observed = await provider.observe(resource(TORRENT, "7", ownership=Ownership.CREATED))
    assert observed.state == ResourceState.AVAILABLE
    assert all(call.get("bypass_cache") == "true" for call in calls) and len(calls) == 1


@pytest.mark.parametrize("unreadable", [
    (200, b"<html>busy</html>", {"Content-Type": "text/html"}),
    (302, b"<html>moved</html>", {"Content-Type": "text/html", "Location": "https://api.torbox.app/x"}),
    (200, b"", {}),
    ok(["not", "an", "object"]),
])
async def test_an_unreadable_current_answer_is_a_protocol_failure_never_absence(unreadable):
    provider, transport = observing({("GET", "torrents/mylist"): [unreadable]})
    with pytest.raises(TransferError) as caught:
        await provider.observe(resource(TORRENT, "7", ownership=Ownership.CREATED))
    assert caught.value.error.category == Category.PROVIDER_PROTOCOL_VIOLATION
    assert asked(transport) == [CURRENT]


async def test_a_redirect_is_an_explicit_safe_protocol_fact_never_invalid_json():
    """HTTP-FB1: a 302 is never decoded as JSON. Its safe facts -- method,
    endpoint path, status, media type, length, the Location's scheme, host and
    path, a bounded body prefix -- survive; its query, the token and any
    credential never do."""
    location = f"https://api.torbox.app/v1/api/torrents/mylist?id=7&token={TOKEN}#frag"
    provider, transport = observing({("GET", "torrents/mylist"): [
        (302, b"<html><body>Moved</body></html>", {"Content-Type": "text/html; charset=utf-8",
                                                    "Location": location})]})
    with pytest.raises(TransferError) as caught:
        await provider.observe(resource(TORRENT, "7", ownership=Ownership.CREATED))
    error = caught.value.error
    assert error.category == Category.PROVIDER_PROTOCOL_VIOLATION
    text = error.diagnostic
    assert "invalid JSON" not in text
    for fact in ("GET /v1/api/torrents/mylist", "redirect", "HTTP 302", "content-type=text/html; charset=utf-8",
                 "length=31", "location-scheme=https", "location-host=api.torbox.app",
                 "location-path=/v1/api/torrents/mylist", "body-prefix=", "Moved"):
        assert fact in text, fact
    rendered = json.dumps(error.as_dict(diagnostics=True), default=str)
    assert TOKEN not in rendered and "token=" not in rendered and "frag" not in rendered and "id=7" not in text
    assert len(transport.calls) == 1   # read once; nothing retried


async def test_a_malformed_success_keeps_bounded_safe_evidence():
    body = b"<html>" + b"x" * 5000 + f"Bearer {TOKEN}".encode() + b"</html>"
    provider, _ = observing({("GET", "torrents/mylist"): [(200, body, {"Content-Type": "text/html"})]})
    with pytest.raises(TransferError) as caught:
        await provider.observe(resource(TORRENT, "7", ownership=Ownership.CREATED))
    text = caught.value.error.diagnostic
    assert "HTTP 200" in text and "content-type=text/html" in text and f"length={len(body)}" in text
    assert "not JSON" in text and "<html>xxx" in text and len(text) <= 500
    assert TOKEN not in text and "x" * 200 not in text


async def test_a_creation_answered_with_a_redirect_stays_uncertain():
    provider, transport = observing({("POST", "torrents/createtorrent"): [
        (307, b"", {"Location": "https://api.torbox.app/v1/api/torrents/createtorrent"})]})
    with pytest.raises(TransferError) as caught:
        await provider.resolve(TransferRequest("magnet", MAGNET, "Show", "a" * 40))
    assert caught.value.error.category == Category.PROVIDER_PROTOCOL_VIOLATION
    assert caught.value.error.mutation == MutationOutcome.UNCERTAIN
    assert "HTTP 307" in caught.value.error.diagnostic and len(transport.calls) == 1


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
    app.router.add_route("*", "/create", moved)
    app.router.add_route("*", "/elsewhere", elsewhere)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        for method in ("POST", "GET"):
            answer = await aiohttp_transport(method, f"http://127.0.0.1:{port}/create", data=None)
            assert answer.status == 302 and answer.headers["location"] == "/elsewhere"
    finally:
        await runner.cleanup()
    assert followed == []


# -- the queue is evidence for the account's inventory, never an observation ---------
#
# The neutral creation reconciliation trusts a complete inventory without the
# request's info-hash as proof a create never happened. A torrent TorBox holds
# in its queue has only a queued_id -- no torrent id yet -- so it can be no
# observation; while the queue holds anything, the inventory is not complete.

EMPTY = {("GET", "torrents/mylist"): [ok([])], ("GET", "webdl/mylist"): [ok([])], ("GET", "usenet/mylist"): [ok([])]}


async def test_the_queue_is_read_first_and_only_an_empty_queue_completes_the_inventory():
    """TB-ID7: queue first (fresh, to its end), then every current collection;
    complete only when the queue is empty."""
    provider, transport = observing({**EMPTY, ("GET", "queued/getqueued"): [ok([])]})
    snapshot = await provider.inventory()
    assert snapshot.complete and snapshot.observations == ()
    assert [url for url, _params in asked(transport)] == [
        "queued/getqueued", "torrents/mylist", "webdl/mylist", "usenet/mylist"]
    assert asked(transport)[0] == QUEUE

    provider, _ = observing({**EMPTY, ("GET", "torrents/mylist"): [ok([torrent(9)])],
                             ("GET", "queued/getqueued"): [ok([queued(7)])]})
    snapshot = await provider.inventory()
    assert not snapshot.complete
    assert [item.resource.context for item in snapshot.observations] == [{"family": TORRENT, "id": "9"}]


async def test_a_queued_entry_is_never_an_observation_even_when_its_number_is_a_torrent_id():
    """TB-ID2 / TB-ID1 in the inventory: no ``resource(TORRENT, queued_id)``,
    no deduplication across the two namespaces by number."""
    client = FakeClient()
    current = client._created(TORRENT, torrent(present=True, state="cached"))
    client.queued = {"7": queued(7), current: queued(int(current), hash="b" * 40, name="Other")}
    snapshot = await TorBoxProvider(client).inventory()
    assert not snapshot.complete
    (only,) = snapshot.observations
    assert only.resource == resource(TORRENT, current)
    assert (only.name, only.fingerprint, only.state) == ("Show", "a" * 40, ResourceState.AVAILABLE)


@pytest.mark.parametrize("unreadable", [
    (200, b"<html>busy</html>", {"Content-Type": "text/html"}), refused("DATABASE_ERROR", 500), ok({"id": 7}),
    ok(["not an object"]),
])
async def test_an_unreadable_queue_never_yields_a_complete_inventory(unreadable):
    provider, _ = observing({**EMPTY, ("GET", "queued/getqueued"): [unreadable]})
    with pytest.raises(TransferError):
        await provider.inventory()


async def test_an_unreadable_current_collection_never_yields_a_complete_inventory():
    provider, _ = observing({**EMPTY, ("GET", "queued/getqueued"): [ok([])],
                             ("GET", "torrents/mylist"): [(200, b"<html>busy</html>", {"Content-Type": "text/html"})]})
    with pytest.raises(TransferError):
        await provider.inventory()


async def test_a_duplicate_refusal_adopts_only_a_current_torrent_never_a_queued_entry():
    """TB-ID6: the one inventory adopts the unique current match by its real
    torrent id; a match only in the queue is no torrent, so the refusal
    stands."""
    client = FakeClient()
    existing = client._created(TORRENT, torrent(present=True, state="cached"))
    client.queued = {"7": queued(7)}
    client.refusal = TorBoxAPIError("DUPLICATE_ITEM", "exists", 400)
    result = await TorBoxProvider(client).resolve(TransferRequest("magnet", MAGNET, "Show", "a" * 40))
    assert result.observation.resource.context == {"family": TORRENT, "id": existing}
    assert result.observation.resource.ownership == Ownership.ADOPTED

    queued_only = FakeClient()
    queued_only.queued = {"7": queued(7)}
    queued_only.refusal = TorBoxAPIError("DUPLICATE_ITEM", "exists", 400)
    with pytest.raises(TransferError) as caught:
        await TorBoxProvider(queued_only).resolve(TransferRequest("magnet", MAGNET, "Show", "a" * 40))
    assert caught.value.error.category == Category.RESOURCE_STATE_CONFLICT
    assert not [call for call in queued_only.calls if call[0] in {"item", "delete", "requestdl"}]


class QueuedCreate(FakeClient):
    """TorBox accepting every torrent into its queue: it answers no torrent id
    (TB-ID4's answer, as the client raises it). ``promote`` starts the queued
    submission right after the next read of the queue, as a torrent under a
    new torrent id of its own."""

    def __init__(self, *, promote=False):
        super().__init__()
        self.promote = promote
        self.next_queued = 500

    async def create_torrent(self, *, magnet="", metainfo=None, name=""):
        from providers.torbox.client import TorBoxProtocolError
        self.calls.append(("create_torrent", magnet or metainfo))
        self.next_queued += 1
        self.queued[str(self.next_queued)] = queued(self.next_queued)
        raise TorBoxProtocolError("TorBox accepted the torrent into its queue without a current torrent_id")

    async def queued_torrents(self, offset, limit=1000):
        page = await super().queued_torrents(offset, limit)
        if self.promote and self.queued:
            self.queued.clear()
            self.started = self._created(TORRENT, torrent(present=True, state="cached"))
        return page


async def queued_create_lab(tmp_path, monkeypatch, client):
    from test_v113_collection_route_generic_closure import Clock
    from test_v113_standby_preparation import lab, magnet, torbox

    from transfers.policy import TransferPolicy

    clock = Clock()
    repository, _registry, engine = await lab(tmp_path, monkeypatch, torbox(client, on=False), clock=clock,
                                              policy=TransferPolicy(retry_delay=60.0, max_attempts=3))
    transfer = await engine.submit((magnet(),), name="Show", deduplicate=False)
    await engine.resolve_pending()
    return repository, engine, clock, transfer


async def test_a_queue_only_create_is_never_adopted_settled_absent_or_created_again(tmp_path, monkeypatch):
    """TB-ID2: while the submission is only in the queue, the existing
    reconciliation cannot prove absence (the inventory is not complete) and
    has nothing to adopt; the creation stays owed and nothing is created
    again."""
    from test_v113_standby_preparation import creates, root_of
    from test_v113_uncertain_creation import owed

    client = QueuedCreate()
    repository, engine, clock, transfer = await queued_create_lab(tmp_path, monkeypatch, client)
    root = await root_of(repository, transfer)
    assert root.resource is None and root.error.mutation == MutationOutcome.UNCERTAIN
    for _ in range(4):
        clock.now += 61
        await engine.resolve_pending()
    root = await root_of(repository, transfer)
    assert root.resource is None and await owed(root.id)
    assert len(creates(client)) == 1
    assert not [call for call in client.calls if call[0] in {"item", "delete", "requestdl"}]


async def test_a_submission_started_between_the_queue_and_current_reads_is_adopted_by_its_torrent_id(
        tmp_path, monkeypatch):
    """TB-ID3: TorBox starts the queued submission (queued_id Q) right after
    the queue is read; reading the current collections afterwards sees it
    under its real torrent id T (T != Q), and the existing reconciliation
    adopts T by info-hash. Q never becomes a resource; nothing is created
    again."""
    from test_v113_standby_preparation import creates, root_of

    client = QueuedCreate(promote=True)
    repository, engine, clock, transfer = await queued_create_lab(tmp_path, monkeypatch, client)
    clock.now += 61
    await engine.resolve_pending()
    root = await root_of(repository, transfer)
    assert root.resource is not None and root.resource.ownership == Ownership.ADOPTED
    assert root.resource.context == {"family": TORRENT, "id": client.started}
    assert client.started != str(client.next_queued)
    assert len(creates(client)) == 1


# -- an error status whose body is not TorBox's answer keeps its safe facts -------------

@pytest.mark.parametrize("status, category, mutation", [
    (503, Category.PROVIDER_UNAVAILABLE, MutationOutcome.UNCERTAIN),
    (403, Category.CREDENTIAL_INVALID, MutationOutcome.NOT_COMMITTED),
])
async def test_a_malformed_error_status_keeps_its_classification_and_safe_evidence(status, category, mutation):
    page = f"<html><title>{status}</title>Bearer {TOKEN}</html>".encode()
    provider, transport = observing({("POST", "torrents/createtorrent"): [
        (status, page, {"Content-Type": "text/html"})]})
    with pytest.raises(TransferError) as caught:
        await provider.resolve(TransferRequest("magnet", MAGNET, "Show", "a" * 40))
    error = caught.value.error
    assert (error.category, error.mutation, error.native_code) == (category, mutation, str(status))
    for fact in ("POST /v1/api/torrents/createtorrent", f"HTTP {status}", "content-type=text/html",
                 f"length={len(page)}", "body-prefix=", f"<title>{status}</title>"):
        assert fact in error.diagnostic, fact
    assert TOKEN not in json.dumps(error.as_dict(diagnostics=True), default=str)
    assert len(transport.calls) == 1


# -- TorBox JSON that Python's strict parser refuses -----------------------------------
#
# Live: TorBox answered 200 application/json with a sound envelope, and DP's
# strict json.loads refused it. A literal control character inside a JSON
# string is what strict parsing refuses and what TorBox's other clients'
# parsers accept; only that is tolerated, and everything after decoding is
# validated exactly as before.

def raw(text):
    return text.encode("utf-8")


def envelope_text(data_text):
    return '{"success":true,"error":null,"detail":"Torrent list retrieved successfully","data":' + data_text + "}"


def torrent_text(*, name="Show", files='[{"id":0,"name":"Show/e01.mkv","size":1000}]', native_id="7",
                 hash_text="a" * 40):
    return ('{"id":' + native_id + ',"hash":"' + hash_text + '","name":"' + name + '","size":1000,"progress":1,'
            '"download_state":"cached","download_present":true,"download_speed":0,"files":' + files + "}")


def current(body, status=200):
    return {("GET", "torrents/mylist"): [(status, body, {"Content-Type": "application/json"})]}


async def test_strict_json_is_decoded_unchanged():
    """TB-J1."""
    provider, _ = observing(current(raw(envelope_text(torrent_text()))))
    observed = await provider.observe(resource(TORRENT, "7", ownership=Ownership.CREATED))
    assert observed.state == ResourceState.AVAILABLE and observed.name == "Show"
    assert [entry.relative_path for entry in observed.file_manifest.entries] == ["e01.mkv"]


async def test_a_literal_control_character_in_a_string_is_tolerated_and_still_validated():
    """TB-J2: a literal tab in TorBox's detail and in the torrent's name."""
    body = raw(envelope_text(torrent_text(name="Show\tSeason")).replace("retrieved", "retrieved\t"))
    with pytest.raises(json.JSONDecodeError):
        json.loads(body.decode())
    provider, _ = observing(current(body))
    observed = await provider.observe(resource(TORRENT, "7", ownership=Ownership.CREATED))
    assert observed.state == ResourceState.AVAILABLE and observed.name == "Show\tSeason"
    assert observed.fingerprint == "a" * 40


async def test_a_tolerated_consumed_field_still_faces_semantic_validation():
    """TB-J3: tolerated syntax never vouches for a value -- an id carrying a
    control character is not the torrent's id, and an info-hash carrying one
    is no info-hash."""
    provider, _ = observing(current(raw(envelope_text(torrent_text(native_id='"7\x01"')))))
    with pytest.raises(TransferError) as caught:
        await provider.observe(resource(TORRENT, "7", ownership=Ownership.CREATED))
    assert caught.value.error.category == Category.PROVIDER_PROTOCOL_VIOLATION

    provider, _ = observing(current(raw(envelope_text(torrent_text(hash_text="a" * 39 + "\x01")))))
    observed = await provider.observe(resource(TORRENT, "7", ownership=Ownership.CREATED))
    assert observed.fingerprint == ""


async def test_a_tolerated_control_character_in_a_member_path_stays_path_safe(tmp_path):
    """TB-J3A: the neutral manifest/destination boundary -- not TorBox --
    sanitizes a control character in a member path, and two paths that only
    the sanitizer makes equal fail closed."""
    from transfers.file_selection import ManifestInvalid, canonicalize_manifest
    from transfers.filesystem import destination

    one = '[{"id":0,"name":"Show/e\x0101.mkv","size":1000}]'
    provider, _ = observing(current(raw(envelope_text(torrent_text(files=one)))))
    observed = await provider.observe(resource(TORRENT, "7", ownership=Ownership.CREATED))
    (entry,) = observed.file_manifest.entries
    canonical = canonicalize_manifest(observed.resource.id, observed.file_manifest)
    assert [item.relative_path for item in canonical.entries] == ["e_01.mkv"]
    target = destination(str(tmp_path), entry.relative_path)
    assert target.name == "e_01.mkv" and "\x01" not in str(target)

    two = ('[{"id":0,"name":"Show/e\x0101.mkv","size":1000},'
           '{"id":1,"name":"Show/e\x0201.mkv","size":1000}]')
    provider, _ = observing(current(raw(envelope_text(torrent_text(files=two)))))
    observed = await provider.observe(resource(TORRENT, "7", ownership=Ownership.CREATED))
    with pytest.raises(ManifestInvalid) as collided:
        canonicalize_manifest(observed.resource.id, observed.file_manifest)
    assert collided.value.reason == "duplicate_path"


@pytest.mark.parametrize("broken", [
    envelope_text(torrent_text())[:-40],                                  # truncated
    envelope_text(torrent_text(name="Sh\x01ow")).replace('"size":1000,', '"size":1000,,'),  # broken delimiter
])
async def test_structurally_broken_json_still_fails_with_bounded_location(broken):
    """TB-J4: strict=False tolerates string content only; structure still
    fails, now saying where, with control characters shown escaped."""
    body = raw(broken)
    for strict in (True, False):
        with pytest.raises(json.JSONDecodeError):
            json.loads(body.decode(), strict=strict)
    provider, transport = observing(current(body))
    with pytest.raises(TransferError) as caught:
        await provider.observe(resource(TORRENT, "7", ownership=Ownership.CREATED))
    error = caught.value.error
    assert error.category == Category.PROVIDER_PROTOCOL_VIOLATION
    text = error.diagnostic
    for fact in ("GET /v1/api/torrents/mylist", "HTTP 200", "json-error=", "line 1", "col ", "json-context="):
        assert fact in text, fact
    assert "\x01" not in text and len(text) <= 500 and TOKEN not in text
    assert len(transport.calls) == 1


async def test_tolerant_decoding_never_turns_an_error_status_into_success():
    """TB-J6."""
    body = raw('{"success":true,"error":null,"detail":"busy\there","data":{"torrent_id":7}}')
    provider, _ = observing({("POST", "torrents/createtorrent"): [(503, body, {"Content-Type": "application/json"})]})
    with pytest.raises(TransferError) as caught:
        await provider.resolve(TransferRequest("magnet", MAGNET, "Show", "a" * 40))
    assert caught.value.error.category == Category.PROVIDER_UNAVAILABLE
    assert caught.value.error.mutation == MutationOutcome.UNCERTAIN
