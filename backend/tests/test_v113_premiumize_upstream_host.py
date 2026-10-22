"""Premiumize: a cloud copy of a hoster link names that link's host as its
source, never Premiumize's own (transfer 1084: a Premiumize copy of a
rapidgator link was presented, and bound, as ``premiumize.me``).

The host is recorded once, when the cloud transfer is created from the link
(the resource context, beside ``nzb_name``), carried by each member's durable
address, and named by every link generated for the member. A member addressed
before it was recorded names Premiumize, as it always did: nothing persisted
is rewritten.
"""
from __future__ import annotations

import pytest
from test_v113_premiumize_provider import ok, provider_with, snapshot, with_hosts

from providers.premiumize.client import (
    API, API_HOST, cloud_member_address, member_source_host, parse_member_address,
)
from core.presentation_safety import safe_public_host
from providers.premiumize.translation import cloud_resource
from transfers.models import SourceIdentity, TransferRequest

pytestmark = pytest.mark.asyncio

RAPIDGATOR = TransferRequest("https", "https://rapidgator.net/file/abc/movie.mkv", "movie.mkv")
DDOWNLOAD = TransferRequest("https", "https://ddownload.com/f/xyz/movie.mkv", "movie.mkv")


def created(transfer_id):
    return {("POST", "transfer/create"): [ok(id=transfer_id)],
            ("GET", "transfer/list"): [ok(transfers=[{"id": transfer_id, "status": "running", "name": "movie"}])]}


async def acquired(request, transfer_id):
    host = request.payload.split("/")[2]
    provider, _transport = provider_with(created(transfer_id))
    with_hosts(provider, snapshot(directdl=(), queue=(host,)))
    result = await provider.resolve(request)
    return result.observation.resource


async def member_of(resource, file_id="f1"):
    provider, _transport = provider_with({
        ("GET", "transfer/list"): [ok(transfers=[{"id": resource.context["transfer_id"], "status": "finished",
                                                  "file_id": file_id}])],
        ("GET", "item/details"): [ok(id=file_id, name="movie.mkv", size=7, link="https://cdn.premiumize.example/x")]})
    (entry,) = await provider.manifest(resource)
    return entry.request


async def candidate_of(member, file_id="f1"):
    provider, _transport = provider_with({("GET", "item/details"): [
        ok(id=file_id, name="movie.mkv", size=7, link="https://cdn.premiumize.example/fresh")]})
    (candidate,) = (await provider.resolve(member)).candidates
    return candidate


async def test_a_hoster_links_cloud_copy_names_the_hoster_as_its_source():
    resource = await acquired(RAPIDGATOR, "T9")
    assert resource.context == {"mode": "cloud", "transfer_id": "T9", "source_host": "rapidgator.net"}
    assert resource.id == cloud_resource("T9").id                          # the host is not identity
    member = await member_of(resource)
    assert parse_member_address(member.payload) == ("cloud", "f1")
    assert member_source_host(member.payload) == "rapidgator.net"
    candidate = await candidate_of(member)
    assert candidate.source_identity == SourceIdentity("host", "rapidgator.net")
    # Every link generated for the member names the same source.
    assert (await candidate_of(candidate.refresh_request)).source_identity == SourceIdentity("host", "rapidgator.net")


async def test_copies_of_links_on_different_hosts_stay_different_sources():
    first = await candidate_of(await member_of(await acquired(RAPIDGATOR, "T1")))
    second = await candidate_of(await member_of(await acquired(DDOWNLOAD, "T2")))
    assert {first.source_identity.key, second.source_identity.key} == {"rapidgator.net", "ddownload.com"}


async def test_a_member_addressed_before_the_host_was_recorded_names_premiumize_as_before():
    old = TransferRequest("https", f"{API}/item/details?id=f1", "movie.mkv", preferred_provider="premiumize")
    assert member_source_host(old.payload) is None
    assert (await candidate_of(old)).source_identity == SourceIdentity("host", safe_public_host(API_HOST))
    resource = cloud_resource("T7")                                       # recorded without a host
    assert member_source_host((await member_of(resource)).payload) is None


@pytest.mark.parametrize("host", ["", "user@rapidgator.net", "rapidgator.net/path", "Rapidgator.NET", "www.rapidgator.net"])
async def test_only_a_bare_safe_hostname_is_ever_a_members_source(host):
    """The same bare-hostname rule TorBox's member addresses keep
    (``core.presentation_safety.safe_public_host``): never credentials, a
    path, or a host in any other spelling than the one that rule gives."""
    with pytest.raises(ValueError):
        cloud_member_address("f1", source_host=host)
    forged = f"{API}/item/details?id=f1&source_host={host}"
    assert parse_member_address(forged) is None and member_source_host(forged) is None
    resource = cloud_resource("T8")
    resource.context["source_host"] = host                                # a context never written this way
    assert member_source_host((await member_of(resource)).payload) is None


async def test_a_torrent_or_nzb_acquisition_records_no_host():
    from test_v113_premiumize_provider import MAGNET
    provider, _transport = provider_with({("POST", "cache/check"): [ok(response=[False])], **created("T3")})
    resource = (await provider.resolve(MAGNET)).observation.resource
    assert "source_host" not in resource.context
