"""Native AllDebrid semantics are translated before entering orchestration."""
from unittest.mock import AsyncMock

import pytest

from providers.alldebrid.client import AllDebridAPIError
from providers.alldebrid.provider import AllDebridProvider
from providers.alldebrid.translation import (
    file_manifest_from_files_response, observation_from_native, translate_error,
)
from transfers.errors import Category, Retryability, TransferError
from transfers.models import (
    CachePresence, Capability, CleanupAuthority, CleanupDirective, DeliveryKind, FileManifest,
    OutcomeKind, Ownership, ProviderResource, ResourceState, TransferRequest,
)
from transfers.models import ExecutionSubject


@pytest.mark.parametrize("code,description,state,category", [
    (3, "Uploading", ResourceState.PREPARING, None),
    (3, "Expired - files removed from cache", ResourceState.EXPIRED, Category.RESOURCE_EXPIRED),
    (4, "Ready", ResourceState.AVAILABLE, None),
    (8, "File too big", ResourceState.UNAVAILABLE, Category.ACCOUNT_LIMITED),
    (8, "No peer after 30 minutes", ResourceState.UNAVAILABLE, Category.SOURCE_TEMPORARILY_UNAVAILABLE),
    (15, "File not available - no peer", ResourceState.UNAVAILABLE, Category.SOURCE_TEMPORARILY_UNAVAILABLE),
    (99, "Future native state", ResourceState.UNKNOWN, Category.UNMAPPED_PROVIDER_ERROR),
])
def test_status_translation_disambiguates_documented_and_observed_descriptions(code, description, state, category):
    result = observation_from_native({"id": "123", "statusCode": code, "status": description})
    assert result.state == state
    assert (result.error.category if result.error else None) == category


@pytest.mark.parametrize("code,category,retry", [
    ("LINK_DOWN", Category.SOURCE_NOT_FOUND, Retryability.NEVER),
    ("AUTH_BAD_APIKEY", Category.CREDENTIAL_INVALID, Retryability.AFTER_REAUTH),
    ("LINK_HOST_LIMIT_REACHED", Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),
    ("LINK_TOO_MANY_DOWNLOADS", Category.CONCURRENCY_LIMITED, Retryability.BACKOFF),
    ("FUTURE_UNDOCUMENTED", Category.UNMAPPED_PROVIDER_ERROR, Retryability.UNKNOWN),
])
def test_native_error_translation(code, category, retry):
    result = translate_error(AllDebridAPIError(code, "native secretvalue"), secrets=("secretvalue",))
    assert result.category == category
    assert result.retryability == retry
    assert "secretvalue" not in result.diagnostic


@pytest.mark.asyncio
async def test_direct_resolution_returns_usable_canonical_candidate_and_retains_refresh_source():
    client = AsyncMock()
    client.unlock_link.return_value = {"link": "https://example.org/file", "filename": "file.bin", "filesize": 128}
    provider = AllDebridProvider(client=client)
    request = TransferRequest("https", "https://source.example/file")
    result = await provider.resolve(request)
    assert result.state == ResourceState.AVAILABLE
    candidate = result.candidates[0]
    assert candidate.expected_bytes == 128
    assert candidate.endpoints[0].address == "https://example.org/file"
    assert candidate.refresh_request == request
    assert not hasattr(candidate, "statusCode")


@pytest.mark.asyncio
async def test_direct_resolution_emits_resolver_attested_identity_evidence():
    """DP 1.0.12 canonical architecture correction, Workstream B: the
    provider emits a neutral resolver-attested fact -- never a duplicate or
    standby decision (that remains ``transfers.mirrors`` policy)."""
    client = AsyncMock()
    client.unlock_link.return_value = {"link": "https://example.org/file", "filename": "file.bin", "filesize": 128}
    provider = AllDebridProvider(client=client)
    result = await provider.resolve(TransferRequest("https", "https://source.example/file"))
    evidence = result.candidates[0].resolver_identity_evidence
    assert evidence is not None
    assert evidence.resolved_name == "file.bin"
    assert evidence.exact_bytes == 128
    assert not hasattr(evidence, "duplicate")
    assert not hasattr(evidence, "standby")


@pytest.mark.asyncio
async def test_direct_resolution_without_native_filename_emits_no_resolver_evidence():
    """No resolver-asserted name must never fall back to inferring evidence
    from the submitted request/URL (specification section 8.2, 8.4)."""
    client = AsyncMock()
    client.unlock_link.return_value = {"link": "https://example.org/file", "filesize": 128}
    provider = AllDebridProvider(client=client)
    request = TransferRequest("https", "https://source.example/some-file.bin", name="some-file.bin")
    result = await provider.resolve(request)
    candidate = result.candidates[0]
    # The submitted request name still names the candidate (existing
    # fallback behavior, unchanged); it is simply never asserted as resolver
    # EVIDENCE.
    assert candidate.name == "some-file.bin"
    assert candidate.resolver_identity_evidence is None


@pytest.mark.asyncio
async def test_direct_resolution_with_zero_size_emits_no_resolver_evidence():
    """Gate 9 revision-2 rejection finding, specification section 8.1: the
    evidence object must represent a resolver-attested name AND an exact
    POSITIVE byte size -- a native filename with no (or a zero) size is not
    sufficient, even though ``transfers.mirrors`` already independently
    refuses a non-positive size as proof and so this never currently causes
    a false consolidation. The evidence object itself must not overstate
    what AllDebrid actually asserted."""
    client = AsyncMock()
    client.unlock_link.return_value = {"link": "https://example.org/file", "filename": "file.bin", "filesize": 0}
    provider = AllDebridProvider(client=client)
    result = await provider.resolve(TransferRequest("https", "https://source.example/file"))
    candidate = result.candidates[0]
    assert candidate.name == "file.bin"
    assert candidate.resolver_identity_evidence is None


@pytest.mark.asyncio
async def test_upload_resource_identity_is_separate_from_transfer_and_native_id():
    client = AsyncMock()
    client.upload_magnet.return_value = {"id": "123", "statusCode": 4, "name": "payload"}
    result = await AllDebridProvider(client=client).resolve(TransferRequest("magnet", "magnet:?xt=urn:btih:test"))
    assert result.observation.resource.id != "123"
    assert result.observation.resource.context == {"id": "123"}
    assert result.observation.resource.ownership == Ownership.CREATED


@pytest.mark.asyncio
async def test_bulk_absence_is_not_authoritative_and_failed_lookup_is_not_absence():
    client = AsyncMock()
    client.get_magnet_status.return_value = []
    provider = AllDebridProvider(client=client)
    assert not (await provider.inventory()).complete
    resource = ProviderResource("alldebrid", {"id": "123"})
    assert (await provider.observe(resource)).state == ResourceState.ABSENT
    client.get_magnet_status.side_effect = AllDebridAPIError("FUTURE_ERROR", "new behavior")
    with pytest.raises(TransferError) as failure:
        await provider.observe(resource)
    assert failure.value.error.category == Category.UNMAPPED_PROVIDER_ERROR


@pytest.mark.asyncio
async def test_observed_resource_requires_explicit_user_cleanup_authority():
    client = AsyncMock()
    resource = ProviderResource("alldebrid", {"id": "123"}, Ownership.OBSERVED)
    provider = AllDebridProvider(client=client)
    assert (await provider.cleanup(CleanupDirective(resource))).kind == OutcomeKind.SKIPPED
    client._post.assert_not_called()
    assert (await provider.cleanup(CleanupDirective(resource, CleanupAuthority.USER_REQUEST))).kind == OutcomeKind.SUCCESS
    client._post.assert_awaited_once()


# --------------------------------------------------------------------------- #
# FILE_MANIFEST capability + neutral early file-tree translation (sections 10-12)
# --------------------------------------------------------------------------- #

_NESTED_FILES = [
    {"n": "Season 1", "e": [
        {"n": "e01.mkv", "s": 1024, "l": "https://alldebrid.example/dl/e01?token=secret"},
        {"n": "e02.mkv", "s": 0, "l": "https://alldebrid.example/dl/e02?token=secret"},
    ]},
    {"n": "readme.txt", "s": 12, "l": "https://alldebrid.example/dl/readme?token=secret"},
]


def test_provider_declares_file_manifest_capability():
    provider = AllDebridProvider(client=AsyncMock())
    assert Capability.FILE_MANIFEST in provider.descriptor.capabilities


@pytest.mark.parametrize("ready,expected", [
    (True, ResourceState.AVAILABLE),
    (False, ResourceState.PREPARING),
])
def test_upload_ready_flag_sets_initial_availability_when_no_status_code(ready, expected):
    result = observation_from_native({"id": "9", "name": "payload", "ready": ready})
    assert result.state == expected


def test_explicit_status_code_wins_over_ready_flag():
    # An explicit statusCode always uses the existing status-code translation.
    result = observation_from_native({"id": "9", "statusCode": 1, "ready": True})
    assert result.state == ResourceState.PREPARING


def test_status_with_nested_files_becomes_neutral_manifest_without_links():
    result = observation_from_native({"id": "9", "statusCode": 4, "files": _NESTED_FILES})
    manifest = result.file_manifest
    assert isinstance(manifest, FileManifest)
    by_path = {e.relative_path: e for e in manifest.entries}
    assert set(by_path) == {"Season 1/e01.mkv", "Season 1/e02.mkv", "readme.txt"}
    assert by_path["Season 1/e01.mkv"].expected_bytes == 1024
    assert by_path["Season 1/e02.mkv"].expected_bytes == 0            # unknown size preserved
    blob = repr(manifest).casefold()
    for token in ("http", "token", "secret", "://", "/dl/"):
        assert token not in blob


def test_status_without_file_tree_fabricates_no_manifest():
    assert observation_from_native({"id": "9", "statusCode": 4}).file_manifest is None
    assert observation_from_native({"id": "9", "statusCode": 1}).file_manifest is None


@pytest.mark.asyncio
async def test_available_without_inline_tree_uses_provider_local_file_list_fallback():
    client = AsyncMock()
    client.get_magnet_status.return_value = [{"id": "123", "statusCode": 4, "name": "payload"}]
    client.get_magnet_files.return_value = [{"id": "123", "files": _NESTED_FILES}]
    provider = AllDebridProvider(client=client)
    observation = await provider.observe(ProviderResource("alldebrid", {"id": "123"}))
    assert observation.state == ResourceState.AVAILABLE
    assert {e.relative_path for e in observation.file_manifest.entries} == {
        "Season 1/e01.mkv", "Season 1/e02.mkv", "readme.txt",
    }
    blob = repr(observation.file_manifest).casefold()
    assert "http" not in blob and "token" not in blob


@pytest.mark.asyncio
async def test_late_executable_manifest_still_returns_ordinary_source_entries():
    client = AsyncMock()
    client.get_magnet_files.return_value = [{
        "id": "123", "files": [
            {"n": "a.bin", "s": 5, "l": "https://alldebrid.example/dl/a?token=secret"},
        ],
    }]
    provider = AllDebridProvider(client=client)
    entries = await provider.manifest(ProviderResource("alldebrid", {"id": "123"}))
    assert len(entries) == 1
    entry = entries[0]
    assert entry.name == "a.bin" and entry.expected_bytes == 5
    assert entry.request.kind == "https"
    assert entry.request.payload == "https://alldebrid.example/dl/a?token=secret"


def test_file_manifest_from_files_response_ignores_foreign_ids_and_discards_links():
    manifest = file_manifest_from_files_response(
        [{"id": "999", "files": _NESTED_FILES}, {"id": "123", "files": _NESTED_FILES}], "123",
    )
    assert manifest is not None
    assert "http" not in repr(manifest).casefold()
    assert file_manifest_from_files_response([{"id": "999", "files": _NESTED_FILES}], "123") is None


# --------------------------------------------------------------------------- #
# Canonical torrent cache fact (DP 1.0.12): native upload ``ready`` -> neutral
# CachePresence, orthogonal to ResourceState.
# --------------------------------------------------------------------------- #

_UPLOADS = {
    "magnet": (TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40),
               "upload_magnet"),
    "torrent": (TransferRequest("torrent", b"d4:infod4:name1:xee", "payload.torrent"),
                "upload_torrent_file"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["magnet", "torrent"])
@pytest.mark.parametrize("native_ready,cache,state", [
    ({"ready": True}, CachePresence.HIT, ResourceState.AVAILABLE),
    ({"ready": False}, CachePresence.MISS, ResourceState.PREPARING),
])
async def test_upload_ready_maps_to_cache_fact_and_keeps_resource_state(kind, native_ready, cache, state):
    request, method = _UPLOADS[kind]
    client = AsyncMock()
    getattr(client, method).return_value = {"id": "77", "name": "payload", **native_ready}
    result = await AllDebridProvider(client=client).resolve(request)
    # Two orthogonal facts from one native boolean, translated once at the boundary.
    assert result.observation.cache_presence == cache
    assert result.observation.state == state
    assert result.state == state


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["magnet", "torrent"])
@pytest.mark.parametrize("native", [
    {},                                  # missing
    {"ready": None},                     # unusable
    {"ready": "true"},                   # ambiguous string, not a boolean
    {"ready": 1},                        # truthy non-boolean
    {"ready": 0},
    {"ready": ""},
])
async def test_upload_without_a_boolean_ready_is_unknown_never_guessed(kind, native):
    request, method = _UPLOADS[kind]
    client = AsyncMock()
    getattr(client, method).return_value = {"id": "77", "name": "payload", **native}
    result = await AllDebridProvider(client=client).resolve(request)
    assert result.observation.cache_presence == CachePresence.UNKNOWN


@pytest.mark.asyncio
async def test_status_code_four_on_upload_is_not_inferred_as_a_cache_hit():
    # statusCode 4 => ResourceState.AVAILABLE, yet no native ready boolean was
    # given, so nothing may claim the torrent was already cached.
    client = AsyncMock()
    client.upload_magnet.return_value = {"id": "77", "statusCode": 4, "name": "payload"}
    result = await AllDebridProvider(client=client).resolve(_UPLOADS["magnet"][0])
    assert result.observation.state == ResourceState.AVAILABLE
    assert result.observation.cache_presence == CachePresence.UNKNOWN


@pytest.mark.asyncio
async def test_later_status_readiness_never_becomes_a_cache_hit():
    """An earlier MISS is a historical fact; a later AVAILABLE poll (statusCode 4,
    speed, files) is provider readiness only and carries no cache claim."""
    client = AsyncMock()
    client.upload_magnet.return_value = {"id": "77", "name": "payload", "ready": False}
    provider = AllDebridProvider(client=client)
    uploaded = await provider.resolve(_UPLOADS["magnet"][0])
    assert uploaded.observation.cache_presence == CachePresence.MISS

    client.get_magnet_status.return_value = [{
        "id": "77", "statusCode": 4, "status": "Ready", "size": 9, "downloaded": 9,
        "downloadSpeed": 123456, "files": _NESTED_FILES,
    }]
    later = await provider.observe(uploaded.observation.resource)
    assert later.state == ResourceState.AVAILABLE
    assert later.cache_presence == CachePresence.UNKNOWN
    # ...even if a status record carried a stray ``ready`` flag.
    stray = observation_from_native({"id": "77", "statusCode": 4, "ready": True})
    assert stray.cache_presence == CachePresence.UNKNOWN
    inventory = await _inventory_of(client, {"id": "77", "statusCode": 4, "ready": True})
    assert inventory.observations[0].cache_presence == CachePresence.UNKNOWN


async def _inventory_of(client, record):
    client.get_magnet_status.return_value = [record]
    return await AllDebridProvider(client=client).inventory()


def test_cache_presence_is_a_distinct_neutral_type_not_a_boolean_or_resource_state():
    assert {item.name for item in CachePresence} == {"HIT", "MISS", "UNKNOWN"}
    assert not isinstance(CachePresence.HIT, (bool, ResourceState))
    assert {item.value for item in CachePresence}.isdisjoint({item.value for item in ResourceState} - {"unknown"})


@pytest.mark.asyncio
async def test_direct_unlock_candidate_is_provider_issued_delivery_with_upstream_source_identity():
    client = AsyncMock()
    client.unlock_link.return_value = {"link": "https://f8g9h0.debrid.it/dl/abc/file.bin", "filename": "file.bin", "filesize": 8}
    result = await AllDebridProvider(client=client).resolve(TransferRequest("https", "https://www.1fichier.com/?abc"))
    candidate = result.candidates[0]
    assert candidate.delivery == DeliveryKind.PROVIDER_ISSUED
    assert (candidate.source_identity.scope, candidate.source_identity.key) == ("host", "1fichier.com")


# ── DP 1.0.13 provider-wide evidence conformance (Path A / Path B) ───────────
# AllDebrid needed no production change: it already emits exactly the neutral
# facts the universal evidence contract consumes.

async def _unlocked(filename="file.bin", size=128, link="https://delivery.example/file", source="https://host-a.example/f"):
    client = AsyncMock()
    client.unlock_link.return_value = {"link": link, "filename": filename, "filesize": size}
    return (await AllDebridProvider(client=client).resolve(TransferRequest("https", source))).candidates[0]


def _aria2(tmp_path):
    from types import SimpleNamespace
    from executors.aria2.executor import Aria2Configuration, Aria2Executor
    return Aria2Executor(SimpleNamespace(url="http://aria2.invalid/jsonrpc"), Aria2Configuration(str(tmp_path)),
                         AsyncMock(return_value=True))


@pytest.mark.asyncio
async def test_resolver_attested_identity_still_bypasses_sampling(tmp_path):
    from transfers.mirrors import EvidenceKind, shared_evidence
    from transfers.registry import IntegrationRegistry
    left = await _unlocked(source="https://host-a.example/f")
    right = await _unlocked(source="https://host-b.example/f", link="https://delivery.example/other")
    executor = _aria2(tmp_path)
    sampled = []

    async def forbidden(subject):
        candidate = subject.candidate
        sampled.append(candidate)
        raise AssertionError("authoritative resolver evidence must never be byte-sampled")

    executor.fingerprint = forbidden
    registry = IntegrationRegistry()
    registry.register_executor(executor)
    evidence = await shared_evidence(left, right, registry)
    assert evidence.kind == EvidenceKind.RESOLVER_ATTESTED and sampled == []


@pytest.mark.asyncio
async def test_provider_issued_capability_never_becomes_an_operator_challenge(tmp_path, monkeypatch):
    import executors.aria2.executor as executor_module
    from services.artifact_sampling import AccessRequired
    from transfers.models import ArtifactFingerprint, FingerprintKind, InputRequirement

    async def unauthorized(address, **kwargs):
        return AccessRequired()

    monkeypatch.setattr(executor_module, "sampled_public_artifact_fingerprint", unauthorized)
    candidate = await _unlocked(filename="", size=0)  # no resolver identity: generalized HTTP evidence applies
    assert candidate.delivery == DeliveryKind.PROVIDER_ISSUED and candidate.accepted_input_methods == ()
    result = await _aria2(tmp_path).fingerprint(ExecutionSubject.of(candidate))
    assert not isinstance(result, InputRequirement)
    assert result == ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "range_unsupported")


@pytest.mark.asyncio
async def test_delivery_candidate_without_resolver_identity_uses_the_generalized_http_evidence(tmp_path, monkeypatch):
    import executors.aria2.executor as executor_module
    from transfers.models import FingerprintKind
    seen = []

    async def sampled(address, **kwargs):
        seen.append((address, kwargs.get("headers")))
        return (128, "full", FingerprintKind.FULL_CONTENT_SAMPLE, "", "prefix")

    monkeypatch.setattr(executor_module, "sampled_public_artifact_fingerprint", sampled)
    candidate = await _unlocked(filename="", size=0)
    assert candidate.resolver_identity_evidence is None
    result = await _aria2(tmp_path).fingerprint(ExecutionSubject.of(candidate))
    assert result.kind == FingerprintKind.FULL_CONTENT_SAMPLE
    assert seen == [("https://delivery.example/file", {})]  # the delivery capability, no operator credential


def test_alldebrid_owns_no_sampling_or_evidence_input_code():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "providers" / "alldebrid"
    for path in root.glob("*.py"):
        text = path.read_text()
        # (Its own "auth_required" health STATE is account/API configuration,
        # deliberately outside the evidence INPUT_REQUIRED lifecycle.)
        # ("fingerprint" alone is the torrent infohash request field.)
        for token in ("ArtifactFingerprint", "def fingerprint", "CandidateSampling", "sampled_public", "artifact_sampling", "InputRequirement",
                      "auth_required(", "server_identity_required(", "transfers.input_required", "accepted_input_methods"):
            assert token not in text, (path.name, token)


@pytest.mark.asyncio
async def test_torrent_roots_resolve_to_observations_never_sampled_candidates():
    client = AsyncMock()
    client.upload_magnet.return_value = {"id": 7, "name": "Root", "ready": False}
    result = await AllDebridProvider(client=client).resolve(TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40))
    assert result.candidates == () and result.observation is not None
