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
    Capability, CleanupAuthority, CleanupDirective, FileManifest, OutcomeKind, Ownership,
    ProviderResource, ResourceState, TransferRequest,
)


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
