"""DP 1.0.13 WebDAV tunables: Maximum Files and Collection Scan Timeout.

Both are the WebDAV provider's own discovery policy. The provider issues them
in its ``DiscoveryRequest`` as the neutral ``DiscoveryLimits`` (exactly as it
issues its ``DiscoveryDepth``), core hands them to the executor that lists,
and the existing WebDAV reader (``services.artifact_sampling.webdav_discovery``)
enforces them:

* a collection with more regular files than the configured maximum FAILS
  (``too_many_entries``) -- it is never truncated into a manifest that looks
  complete -- and the neutral 10,000-entry ceiling still applies whatever the
  configuration says;
* the whole enumeration finishes inside the configured scan timeout or fails
  through the existing normalized ``timeout`` listing failure.

Neither reaches a candidate, execution work, continuation or materialization.
"""
from __future__ import annotations

from dataclasses import fields

import pytest
import pytest_asyncio
from pydantic import ValidationError

from services import artifact_sampling as sampling
from test_v113_transport_evidence_sampling import executor_for, guard_for, loopback  # noqa: F401
from transfers.errors import Category, Domain, Retryability, TransferError
from transfers.models import (
    DiscoveryDepth, DiscoveryLimits, DiscoveryRequest, Endpoint, ExecutionSubject, InputMethod, TransferCandidate,
    TransferRequest,
)
from webdav_origin import WebDavOrigin

TREE = {
    "/dav/": None,
    "/dav/a.txt": b"aaaa",
    "/dav/b.txt": b"bb",
    "/dav/sub/": None,
    "/dav/sub/c.txt": b"cc",
    "/dav/sub/deeper/": None,
    "/dav/sub/deeper/d.txt": b"d",
}


@pytest_asyncio.fixture
async def origin(loopback):  # noqa: F811
    server = await WebDavOrigin(TREE).start()
    yield server
    await server.close()


# ── the settings schema: provider-owned, bounded, defaults preserve behavior ─

def test_the_defaults_preserve_current_behavior():
    from providers.general_webdav.definition import GeneralWebdavOptions, build
    options = GeneralWebdavOptions()
    # No aggregate scan deadline by default: exactly what every collection had.
    assert (options.directory_depth, options.max_files, options.collection_scan_timeout_seconds) == (
        "current", 10_000, 0)
    provider = build(options, None)
    # The default maximum is the existing neutral entry ceiling itself.
    assert options.max_files == sampling.MAX_LISTED_ENTRIES
    assert provider.depth == DiscoveryDepth.CURRENT
    assert provider.limits == DiscoveryLimits(max_files=10_000, timeout_seconds=None)
    # A nonzero value explicitly enables the aggregate deadline.
    assert build(GeneralWebdavOptions(collection_scan_timeout_seconds=90), None).limits == DiscoveryLimits(
        max_files=10_000, timeout_seconds=90)


@pytest.mark.parametrize("field,valid,invalid", [
    ("max_files", (1, 10_000), (0, 10_001)),
    # 0 is No limit; a deadline is 10 to 3600 seconds.
    ("collection_scan_timeout_seconds", (0, 10, 3600), (-1, 1, 9, 3601)),
])
def test_each_tunable_is_bounded_by_the_backend_schema(field, valid, invalid):
    from providers.general_webdav.definition import GeneralWebdavOptions
    for value in valid:
        assert getattr(GeneralWebdavOptions(**{field: value}), field) == value
    for value in invalid:
        with pytest.raises(ValidationError):
            GeneralWebdavOptions(**{field: value})


def test_the_webdav_options_are_discovery_policy_only():
    from providers.general_webdav.definition import GeneralWebdavOptions
    assert set(GeneralWebdavOptions.model_fields) == {"directory_depth", "max_files",
                                                      "collection_scan_timeout_seconds"}


@pytest.mark.asyncio
async def test_the_provider_issues_its_limits_with_its_depth():
    from providers.general_webdav.definition import GeneralWebdavOptions, build
    provider = build(GeneralWebdavOptions(directory_depth="2", max_files=25, collection_scan_timeout_seconds=45),
                     None)
    result = await provider.resolve(TransferRequest("webdav", "webdav://h.example/dav/"))
    assert result.discovery.depth == DiscoveryDepth.of(2)
    assert result.discovery.limits == DiscoveryLimits(max_files=25, timeout_seconds=45)


def test_limits_are_neutral_discovery_policy_that_never_reach_a_candidate():
    assert DiscoveryRequest(Endpoint("https", "https://h/")).limits == DiscoveryLimits()
    assert DiscoveryLimits() == DiscoveryLimits(None, None)
    for value in ({"max_files": 0}, {"max_files": -1}, {"max_files": True}, {"timeout_seconds": 0},
                  {"timeout_seconds": -5}):
        with pytest.raises(ValueError):
            DiscoveryLimits(**value)
    names = {item.name for item in fields(TransferCandidate)}
    assert not any("limit" in name or "max_files" in name or "scan" in name for name in names)


# ── the reader: Maximum Files ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_collection_within_the_configured_maximum_is_listed_whole(origin):
    result = await sampling.webdav_discovery(origin.url("/dav/"), depth=DiscoveryDepth.UNLIMITED, max_files=4)
    assert isinstance(result, sampling.Listing)
    assert [name for name, _size in result.entries] == ["a.txt", "b.txt", "sub/c.txt", "sub/deeper/d.txt"]


@pytest.mark.asyncio
async def test_a_collection_past_the_configured_maximum_fails_and_is_never_truncated(origin):
    result = await sampling.webdav_discovery(origin.url("/dav/"), depth=DiscoveryDepth.UNLIMITED, max_files=3)
    assert result == sampling.ListingRefused("too_many_entries")
    # Exactly at the limit is still a complete listing.
    flat = await sampling.webdav_discovery(origin.url("/dav/"), depth=DiscoveryDepth.CURRENT, max_files=2)
    assert isinstance(flat, sampling.Listing) and len(flat.entries) == 2


@pytest.mark.asyncio
async def test_the_hard_entry_ceiling_cannot_be_raised_by_configuration(origin, monkeypatch):
    monkeypatch.setattr(sampling, "MAX_LISTED_ENTRIES", 3)
    result = await sampling.webdav_discovery(origin.url("/dav/"), depth=DiscoveryDepth.UNLIMITED,
                                             max_files=1_000_000)
    assert result == sampling.ListingRefused("too_many_entries")


# ── the reader: Collection Scan Timeout ──────────────────────────────────────

@pytest.mark.asyncio
async def test_no_limit_leaves_only_the_per_request_bound(origin, monkeypatch):
    """No aggregate deadline: a scan longer than any small total still
    completes while every request keeps the existing per-request timeout."""
    origin.delays = {"/dav/sub/": 0.6, "/dav/sub/deeper/": 0.6}
    timeouts = []
    real = sampling.aiohttp.ClientTimeout

    def recording(*args, **kwargs):
        timeouts.append(kwargs.get("total"))
        return real(*args, **kwargs)

    monkeypatch.setattr(sampling.aiohttp, "ClientTimeout", recording)
    result = await sampling.webdav_discovery(origin.url("/dav/"), depth=DiscoveryDepth.UNLIMITED)
    assert isinstance(result, sampling.Listing) and len(result.entries) == 4
    assert timeouts == [sampling.DEFAULT_TIMEOUT_SECONDS] == [20.0]


@pytest.mark.asyncio
async def test_the_whole_enumeration_is_bounded_by_the_scan_timeout(origin):
    # Every request answers well inside the per-request bound; only the whole
    # scan is too slow.
    origin.delays = {"/dav/sub/": 0.6, "/dav/sub/deeper/": 0.6}
    result = await sampling.webdav_discovery(origin.url("/dav/"), depth=DiscoveryDepth.UNLIMITED,
                                             scan_timeout_seconds=0.9)
    assert result == sampling.ListingRefused("timeout")
    finished = await sampling.webdav_discovery(origin.url("/dav/"), depth=DiscoveryDepth.UNLIMITED,
                                               scan_timeout_seconds=10)
    assert isinstance(finished, sampling.Listing) and len(finished.entries) == 4


# ── the executor: limits reach the reader; failures are the existing ones ────

def _candidate(url):
    return TransferCandidate("dir", (Endpoint(url.split(":", 1)[0], url),),
                             accepted_input_methods=(InputMethod.USERNAME_PASSWORD,),
                             request_kind=url.split(":", 1)[0])


@pytest.mark.asyncio
async def test_the_executor_hands_the_limits_to_the_webdav_reader(tmp_path, monkeypatch):
    import executors.aria2.executor as aria2
    seen = {}

    async def reader(address, **kwargs):
        seen.update(kwargs)
        return sampling.Listing((("a.txt", 1),), "/dav/")

    monkeypatch.setattr(aria2, "webdav_discovery", reader)
    executor = executor_for(tmp_path, guard_for())
    await executor.discover(ExecutionSubject.of(_candidate("http://dav.test/dav/")), depth=DiscoveryDepth.of(1),
                            limits=DiscoveryLimits(max_files=7, timeout_seconds=42))
    assert (seen["depth"], seen["max_files"], seen["scan_timeout_seconds"]) == (DiscoveryDepth.of(1), 7, 42)
    seen.clear()
    await executor.discover(ExecutionSubject.of(_candidate("http://dav.test/dav/")))
    # No limits asked: the reader's own bounds, exactly as before.
    assert "max_files" not in seen and "scan_timeout_seconds" not in seen


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["ftp", "sftp"])
async def test_a_listing_that_cannot_honor_limits_refuses_them(tmp_path, scheme):
    executor = executor_for(tmp_path, guard_for())
    with pytest.raises(TransferError) as raised:
        await executor.discover(ExecutionSubject.of(_candidate(f"{scheme}://h.example/dir/")),
                                limits=DiscoveryLimits(max_files=5))
    assert raised.value.error.category == Category.UNSUPPORTED_CAPABILITY


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,domain,category,retryability", [
    ("too_many_entries", Domain.REQUEST, Category.UNSUPPORTED_REQUEST, Retryability.NEVER),
    ("timeout", Domain.NETWORK, Category.CONNECTION_TIMEOUT, Retryability.BACKOFF),
])
async def test_limit_failures_are_the_existing_normalized_listing_failures(tmp_path, monkeypatch, reason, domain,
                                                                          category, retryability):
    import executors.aria2.executor as aria2

    async def reader(_address, **_kwargs):
        return sampling.ListingRefused(reason)

    monkeypatch.setattr(aria2, "webdav_discovery", reader)
    executor = executor_for(tmp_path, guard_for())
    with pytest.raises(TransferError) as raised:
        await executor.discover(ExecutionSubject.of(_candidate("https://dav.test/dav/")),
                                limits=DiscoveryLimits(max_files=1, timeout_seconds=10))
    error = raised.value.error
    assert (error.domain, error.category, error.retryability, error.diagnostic) == (
        domain, category, retryability, reason)


def test_core_passes_limits_only_when_a_provider_set_them():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "transfers/_engine_base.py").read_text()
    body = source.split("async def _discover(", 1)[1].split("\n    async def ", 1)[0]
    assert "if request.limits != DiscoveryLimits()" in body
    assert "webdav" not in body.casefold()


def test_the_core_contract_speaks_limits():
    import inspect
    from transfers.contracts import RemoteDiscovery
    parameters = inspect.signature(RemoteDiscovery.discover).parameters
    assert parameters["limits"].default == DiscoveryLimits()


@pytest.mark.asyncio
@pytest.mark.parametrize("options,accepted", [({"max_files": 250, "collection_scan_timeout_seconds": 60}, True),
                                              ({"collection_scan_timeout_seconds": 0}, True),
                                              ({"max_files": 10_001}, False),
                                              ({"collection_scan_timeout_seconds": 5}, False)])
async def test_the_tunables_persist_through_the_general_webdav_integration_scope(options, accepted):
    from unittest.mock import patch

    from api import routes
    from core.config import AppSettings
    from integrations.definition import IntegrationSettings
    from providers.general_webdav.definition import definition as general_webdav
    from test_settings_namespace_mutation import _application

    current = AppSettings(integrations={"general_webdav": IntegrationSettings(options={"directory_depth": "2"})})
    application = _application(current)
    application.definitions = (general_webdav,)
    saved = {}
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", return_value=current), \
         patch("api.routes.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings"):
        if not accepted:
            with pytest.raises(routes.HTTPException) as refused:
                await routes.patch_integration_configuration(
                    "general_webdav", routes.IntegrationConfigurationUpdate(options=options), application=application)
            assert refused.value.status_code == 400 and "cfg" not in saved
            return
        result = await routes.patch_integration_configuration(
            "general_webdav", routes.IntegrationConfigurationUpdate(options=options), application=application)
    for stored in (result["options"], saved["cfg"].integrations["general_webdav"].options):
        assert {key: stored[key] for key in ("directory_depth", *options)} == {"directory_depth": "2", **options}
