"""DP 1.0.13 WebDAV discovery: the executor-side read-only reader.

``services.artifact_sampling.webdav_discovery`` classifies one HTTP(S) path
through WebDAV and lists it to the neutral ``DiscoveryDepth`` using only
repeated ``PROPFIND Depth: 1`` requests -- never ``Depth: infinity``, which
the origin below refuses as many real servers do. Everything runs against a
deterministic in-process origin; no live service is contacted.
"""
from __future__ import annotations

import base64

import pytest
import pytest_asyncio

from services import artifact_sampling as sampling
from test_v113_transport_evidence_sampling import loopback  # noqa: F401
from transfers.models import DiscoveryDepth
from webdav_origin import MULTISTATUS, WebDavOrigin, entry

pytestmark = pytest.mark.asyncio

USER, PASSWORD = "dav-user-sentinel", "dav-password-sentinel"
TREE = {
    "/dav/": None,
    "/dav/a.txt": b"aaaa",
    "/dav/My File.bin": b"bbbbbbbb",
    "/dav/sub/": None,
    "/dav/sub/c.txt": b"cc",
    "/dav/sub/deeper/": None,
    "/dav/sub/deeper/d.txt": b"d",
    "/dav/sub/deeper/deepest/": None,
    "/dav/sub/deeper/deepest/e.txt": b"eeeee",
    "/dav/empty/": None,
}


@pytest_asyncio.fixture
async def origin(loopback):  # noqa: F811
    server = await WebDavOrigin(TREE).start()
    yield server
    await server.close()


def _propfinds(server):
    return [(path, depth) for method, path, depth, _auth in server.requests if method == "PROPFIND"]


async def _discover(server, path, depth=DiscoveryDepth.CURRENT, **kwargs):
    return await sampling.webdav_discovery(server.url(path), depth=depth, **kwargs)


# ── classification ────────────────────────────────────────────────────────────

async def test_a_file_is_one_regular_file_of_its_stated_size(origin):
    result = await _discover(origin, "/dav/My%20File.bin")
    assert result == sampling.RemoteFile(8)
    assert _propfinds(origin) == [("/dav/My File.bin", "1")]


async def test_a_file_without_a_stated_size_has_no_size(origin):
    origin.omit_length = True
    assert await _discover(origin, "/dav/a.txt") == sampling.RemoteFile(0)


async def test_current_lists_immediate_files_and_never_descends(origin):
    result = await _discover(origin, "/dav/")
    assert isinstance(result, sampling.Listing)
    assert result.entries == (("My File.bin", 8), ("a.txt", 4))
    assert result.directory == "/dav/"
    assert _propfinds(origin) == [("/dav/", "1")]


@pytest.mark.parametrize("levels,expected,listed", [
    (1, {"a.txt", "My File.bin", "sub/c.txt"}, ["/dav/", "/dav/empty/", "/dav/sub/"]),
    (2, {"a.txt", "My File.bin", "sub/c.txt", "sub/deeper/d.txt"},
     ["/dav/", "/dav/empty/", "/dav/sub/", "/dav/sub/deeper/"]),
])
async def test_a_finite_depth_stops_at_exactly_that_many_levels(origin, levels, expected, listed):
    result = await _discover(origin, "/dav/", DiscoveryDepth.of(levels))
    assert {name for name, _size in result.entries} == expected
    assert sorted(path for path, _depth in _propfinds(origin)) == listed
    assert {depth for _path, depth in _propfinds(origin)} == {"1"}


async def test_unlimited_reaches_the_whole_tree_with_depth_one_requests_only(origin):
    assert origin.forbid_infinity
    result = await _discover(origin, "/dav/", DiscoveryDepth.UNLIMITED)
    assert dict(result.entries) == {"a.txt": 4, "My File.bin": 8, "sub/c.txt": 2, "sub/deeper/d.txt": 1,
                                    "sub/deeper/deepest/e.txt": 5}
    assert {depth for _path, depth in _propfinds(origin)} == {"1"}
    assert len(_propfinds(origin)) == 5  # every collection exactly once


async def test_discovery_never_reads_content(origin):
    await _discover(origin, "/dav/", DiscoveryDepth.UNLIMITED)
    await _discover(origin, "/dav/a.txt")
    assert {method for method, *_rest in origin.requests} == {"PROPFIND"}


async def test_a_collection_redirected_to_its_slash_form_is_listed_there(origin):
    origin.redirects["/dav"] = "/dav/"
    result = await _discover(origin, "/dav")
    assert result.entries == (("My File.bin", 8), ("a.txt", 4))
    assert result.location == origin.url("/dav/")


# ── answers that are not WebDAV, and answers that are failures ────────────────

@pytest.mark.parametrize("status", [200, 204, 405, 501])
async def test_a_server_without_webdav_there_is_opaque(origin, status):
    origin.status = status
    assert isinstance(await _discover(origin, "/dav/"), sampling.Opaque)


@pytest.mark.parametrize("status,reason", [
    (403, "permission_denied"), (404, "not_found"), (410, "not_found"), (429, "rate_limited"),
    (500, "server_error"), (503, "server_error"), (400, "unsupported_listing"), (409, "unsupported_listing"),
])
async def test_every_other_answer_is_an_ordinary_refusal_never_opaque(origin, status, reason):
    origin.status = status
    assert await _discover(origin, "/dav/") == sampling.ListingRefused(reason)


async def test_a_listing_that_breaks_below_the_root_fails_the_whole_discovery(origin):
    origin.raw["/dav/sub/"] = "<broken"
    assert await _discover(origin, "/dav/", DiscoveryDepth.of(1)) == sampling.ListingRefused("unsupported_listing")


async def test_a_refused_subcollection_fails_rather_than_shrinks_the_listing(origin):
    origin.statuses["/dav/sub/"] = 403
    assert await _discover(origin, "/dav/", DiscoveryDepth.of(1)) == sampling.ListingRefused("permission_denied")
    origin.statuses["/dav/sub/"] = 405  # a collection that will not list is never "opaque" below the root
    assert await _discover(origin, "/dav/", DiscoveryDepth.of(1)) == sampling.ListingRefused("unsupported_listing")


async def test_connection_refused_is_a_failure(loopback):  # noqa: F811
    result = await sampling.webdav_discovery("http://dav.test:1/dav/", depth=DiscoveryDepth.CURRENT)
    assert result == sampling.ListingRefused("connection_refused")


# ── traversal safety ──────────────────────────────────────────────────────────

async def test_the_root_entry_is_structure_never_a_member(origin):
    result = await _discover(origin, "/dav/empty/")
    assert result.entries == ()


@pytest.mark.parametrize("href", [
    "/dav/",                       # the parent listed again as a member of its child: a cycle
    "/dav/sub/../a.txt",           # traversal
    "/elsewhere/x.txt",            # not below the listed collection
    "/dav/sub/deeper/d.txt",       # not an immediate member
    "/dav/sub/a%2Fb.txt",          # an encoded separator
    "/dav/sub/%ff.txt",            # not UTF-8
    "/dav/sub/x%0a.txt",           # a control character
    "/dav/sub/q.txt?token=1",      # a query
    "http://other.test/dav/sub/x.txt",  # another origin
    "/dav/sub/c.txt",              # a duplicate member
])
async def test_an_unusable_member_fails_closed_and_never_loops(origin, href):
    origin.extra["/dav/sub/"] = [entry(href, collection=href.endswith("/"), size=1)]
    result = await _discover(origin, "/dav/", DiscoveryDepth.UNLIMITED)
    assert result == sampling.ListingRefused("unsupported_listing")
    assert len(_propfinds(origin)) <= 3


async def test_percent_spellings_of_one_member_are_one_member(origin):
    origin.extra["/dav/"] = [entry("/dav/a%2etxt", collection=False, size=4)]
    assert await _discover(origin, "/dav/") == sampling.ListingRefused("unsupported_listing")


async def test_a_listing_past_the_neutral_entry_bound_is_refused_not_truncated(origin, monkeypatch):
    monkeypatch.setattr(sampling, "MAX_LISTED_ENTRIES", 3)
    assert await _discover(origin, "/dav/", DiscoveryDepth.UNLIMITED) == sampling.ListingRefused("too_many_entries")


async def test_an_oversized_listing_body_is_refused(origin, monkeypatch):
    monkeypatch.setattr(sampling, "_MAX_LISTING_BYTES", 64)
    assert await _discover(origin, "/dav/") == sampling.ListingRefused("too_many_entries")


# ── XML safety ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("prologue", [
    '<!DOCTYPE m [<!ENTITY x SYSTEM "file:///etc/passwd">]>',
    '<!DOCTYPE m [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>',
    '<!DOCTYPE m SYSTEM "http://attacker.test/dtd">',
])
async def test_no_document_type_or_entity_is_ever_processed(origin, prologue):
    body = MULTISTATUS.format(entry("/dav/", collection=True) + entry("/dav/x&b;", collection=False, size=1))
    origin.raw["/dav/"] = body.replace("\n", "\n" + prologue + "\n", 1)
    assert await _discover(origin, "/dav/") == sampling.ListingRefused("unsupported_listing")


# ── authentication and redirects ──────────────────────────────────────────────

def _basic(user=USER, password=PASSWORD):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


async def test_a_protected_collection_asks_and_then_accepts_once(origin):
    origin.credentials = (USER, PASSWORD)
    assert isinstance(await _discover(origin, "/dav/"), sampling.AccessRequired)
    accepted = []
    result = await _discover(origin, "/dav/", DiscoveryDepth.of(1), username=USER, password=PASSWORD,
                             on_authenticated=lambda: accepted.append(True))
    assert isinstance(result, sampling.Listing) and accepted == [True]
    assert isinstance(await _discover(origin, "/dav/", username=USER, password="wrong"), sampling.AccessRequired)


async def test_a_non_basic_challenge_is_unsupported(origin):
    origin.credentials = (USER, PASSWORD)
    origin.challenge = 'Digest realm="dav", nonce="x"'
    assert await _discover(origin, "/dav/") == sampling.ListingRefused("auth_method_unsupported")


async def test_a_same_origin_redirect_keeps_the_credential(origin):
    origin.credentials = (USER, PASSWORD)
    origin.redirects["/old/"] = "/dav/"
    result = await _discover(origin, "/old/", username=USER, password=PASSWORD)
    assert isinstance(result, sampling.Listing)
    assert [auth for method, path, _d, auth in origin.requests if path == "/dav/"] == [_basic()]


async def test_a_credential_never_follows_a_move_to_another_origin(loopback):  # noqa: F811
    first = await WebDavOrigin({}).start()
    second = await WebDavOrigin(TREE).start()
    try:
        first.redirects["/dav/"] = second.url("/dav/", host="mirror.test")
        result = await sampling.webdav_discovery(first.url("/dav/"), depth=DiscoveryDepth.CURRENT,
                                                 username=USER, password=PASSWORD)
        assert isinstance(result, sampling.Listing) and result.location == second.url("/dav/", host="mirror.test")
        assert [auth for *_rest, auth in first.requests] == [_basic()]
        assert [auth for *_rest, auth in second.requests] == [None]
        # The moved authority asks for its own credentials: the question names
        # the address that asked, and nothing of the first authority's reaches it.
        second.credentials = ("other", "secret")
        second.requests.clear()
        result = await sampling.webdav_discovery(first.url("/dav/"), depth=DiscoveryDepth.CURRENT,
                                                 username=USER, password=PASSWORD)
        assert result == sampling.AccessRequired(address=second.url("/dav/", host="mirror.test"))
        assert [auth for *_rest, auth in second.requests] == [None]
        # Its own answer, given for its own authority, is sent there and only there.
        second.requests.clear()
        first.requests.clear()
        from transfers.requests import auth_scope
        result = await sampling.webdav_discovery(first.url("/dav/"), depth=DiscoveryDepth.CURRENT,
                                                 username="other", password="secret",
                                                 credential_scope=auth_scope(second.url("/dav/", host="mirror.test")))
        assert isinstance(result, sampling.Listing)
        assert [auth for *_rest, auth in first.requests] == [None]
        assert [auth for *_rest, auth in second.requests] == [_basic("other", "secret")]
    finally:
        await first.close()
        await second.close()


class _Answer:
    def __init__(self, status, location=""):
        self.status = status
        self.headers = {"Location": location} if location else {}

    def release(self):
        pass


class _Session:
    """Records the headers each hop was sent with; answers from a script."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    async def request(self, method, uri, *, headers, data, allow_redirects):
        assert allow_redirects is False
        self.sent.append((method, uri, dict(headers)))
        return self.script.pop(0)


async def test_https_to_http_never_carries_the_credential(loopback):  # noqa: F811
    session = _Session([_Answer(302, "http://dav.test/dav/"), _Answer(207)])
    response, _reason, answered = await sampling._guarded_request(
        session, "https://dav.test/dav/", {"Authorization": _basic(), "Depth": "1"}, method="PROPFIND",
        data=b"x", carried=frozenset({"depth"}))
    assert response.status == 207 and answered == "http://dav.test/dav/"
    assert "Authorization" in session.sent[0][2] and "Authorization" not in session.sent[1][2]
    assert session.sent[1][2] == {"Depth": "1"} and session.sent[1][0] == "PROPFIND"


async def test_a_credential_is_not_restored_when_a_redirect_returns(loopback):  # noqa: F811
    session = _Session([_Answer(302, "https://other.test/x/"), _Answer(302, "https://dav.test/dav/"),
                        _Answer(207)])
    await sampling._guarded_request(session, "https://dav.test/old/", {"Authorization": _basic()},
                                    method="PROPFIND", data=b"x", carried=frozenset())
    assert ["Authorization" in headers for _method, _uri, headers in session.sent] == [True, False, False]
