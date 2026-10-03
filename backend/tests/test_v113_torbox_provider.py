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
    API, TORRENT, USENET, WEBDL, RawResponse, TorBoxAPIError, TorBoxService, member_address, member_source_host,
    parse_member_address,
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
from transfers.errors import Category, Domain, Recovery, Retryability, TransferError
from transfers.file_selection import ManifestInvalid
from transfers.models import (
    CleanupAuthority, CleanupDirective, DeliveryKind, OutcomeKind, Ownership, ResourceState, SourceIdentity,
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
        status, payload = self.script[(method, url.removeprefix(API + "/"))].pop(0)
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return RawResponse(status, {}, body)


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

    async def create_webdl(self, link):
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
