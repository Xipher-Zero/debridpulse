"""A submitted file no structured upload owner claims is read as a list of
links: bounded, decoded as text, recognized by its structure (never its name),
and each link handed to the owner Quick Add would hand it to."""
from __future__ import annotations

import codecs
import dataclasses
import json

import pytest

import db.database as database
from transfers.requests import MAX_LINK_FILE_BYTES, link_file_entries

from test_file_selection_api import api  # noqa: F401  (shared fixture)

MAGNET = "magnet:?xt=urn:btih:" + "0123456789abcdef" * 2 + "01234567"
HTTP = "https://a.example/file-a.iso"
SFTP = "sftp://b.example/path/file-b"
LINKS = [MAGNET, HTTP, SFTP]


def values(data: bytes) -> list[str]:
    return [value for _location, value in link_file_entries(data)]


def refusal(data: bytes) -> str:
    with pytest.raises(ValueError) as refused:
        link_file_entries(data)
    return str(refused.value)


# --------------------------------------------------------------------------- #
# Grammar recognition
# --------------------------------------------------------------------------- #

def test_a_line_list_mixes_any_link_forms_ignoring_blanks_and_comment_lines():
    text = f"# my links\n\n{MAGNET}\n   {HTTP}  \n  # later\n{SFTP}\n{HTTP}\n"
    assert link_file_entries(text.encode()) == (("Line 3", MAGNET), ("Line 4", HTTP), ("Line 6", SFTP))


def test_the_grammar_holds_no_scheme_list_the_submission_owners_decide_support():
    # An unknown scheme is still one complete link value: refusing it is the
    # submission owner's decision, not the grammar's.
    assert values(b"foo://x.example/y\n") == ["foo://x.example/y"]


@pytest.mark.parametrize("data", [
    codecs.BOM_UTF8 + "\n".join(LINKS).encode("utf-8"),
    "\n".join(LINKS).encode("utf-16"),            # BOM-signalled, native order
    codecs.BOM_UTF16_BE + "\n".join(LINKS).encode("utf-16-be"),
    "\n".join(LINKS).encode("utf-16-le"),         # no BOM, unambiguous code units
    "\n".join(LINKS).encode("utf-16-be"),
    "\n".join(LINKS).encode("utf-32"),
], ids=["utf8-bom", "utf16-bom", "utf16be-bom", "utf16le", "utf16be", "utf32-bom"])
def test_supported_text_encodings_normalize_before_any_grammar(data):
    assert values(data) == LINKS


@pytest.mark.parametrize("document", [
    "url\n" + "\n".join(LINKS),
    'name,url,size\nm,"' + MAGNET + '",1\n"a, b",' + HTTP + ',2\nc,' + SFTP + ',3\n',
    "name\turl\nm\t" + MAGNET + "\na\t" + HTTP + "\nc\t" + SFTP + "\n",
    json.dumps(LINKS),
    json.dumps([{"name": "m", "url": MAGNET, "size": 1}, {"name": "a", "url": HTTP, "size": None},
                {"name": "c", "url": SFTP, "size": 3}]),
    "- " + "\n- ".join(LINKS),
    "---\n" + "".join(f"- link: '{link}'\n  name: x\n" for link in LINKS),
    "<links>" + "".join(f"<link>{link.replace('&', '&amp;')}</link>" for link in LINKS) + "</links>",
    '<?xml version="1.0"?>\n<items>\n' + "".join(
        f"  <item><name>x</name><uri>{link}</uri></item>\n" for link in LINKS) + "</items>\n",
], ids=["csv-column", "csv-table", "tsv-table", "json-links", "json-records", "yaml-links",
        "yaml-records", "xml-links", "xml-records"])
def test_structured_link_lists_reduce_to_the_same_links(document):
    assert values(document.encode()) == LINKS


@pytest.mark.parametrize("document, reason", [
    (f"Here is something I downloaded from {HTTP} yesterday.\n", "Line 1 is not a single link"),
    (f'<html><body><a href="{HTTP}">a</a></body></html>', "no link field"),
    (f'<!DOCTYPE html>\n<html><body><a href="{HTTP}">a</a></body></html>', "not a link list"),
    (f'<rss><channel><item><link>{HTTP}</link></item></channel></rss>', "not a link list"),
    (f'<metalink xmlns="urn:ietf:params:xml:ns:metalink"><file name="f"><url>{HTTP}</url></file></metalink>',
     "not a link list"),
    ('<!DOCTYPE l [<!ENTITY e SYSTEM "file:///etc/passwd">]><l><url>&e;</url></l>', "not a link list"),
    (json.dumps({"data": {"links": LINKS}}), "not a list of links"),
    (json.dumps([{"url": HTTP, "link": SFTP}]), "more than one link field"),
    (json.dumps([{"url": HTTP}, {"uri": SFTP}]), "do not share one shape"),
    (json.dumps([HTTP, {"url": SFTP}]), "mixes links and records"),
    (json.dumps([{"url": HTTP, "meta": {"mirror": SFTP}}]), "Entry 1 is not a link record"),
    (f"name,url,mirror\na,{HTTP},{SFTP}\n", "Line 2 has more than one link"),
    (f"name,size\na,{HTTP}\n", "no link field"),
    (f"{HTTP},{SFTP}\n", "Line 1 lists more than one link"),
    (f"{HTTP}\t{SFTP}\n", "Line 1 lists more than one link"),
    ("", "empty"),
    ("# nothing\n\n   \n", "contains no links"),
], ids=["prose", "html", "html-doctype", "feed", "metalink", "xxe", "json-nested", "json-two-fields",
        "json-heterogeneous", "json-mixed", "json-deep-record", "csv-two-link-columns", "csv-no-link-column",
        "csv-group", "tsv-group", "empty", "comments-only"])
def test_documents_that_would_need_guessing_are_refused_whole(document, reason):
    assert reason in refusal(document.encode())


@pytest.mark.parametrize("data", [
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + HTTP.encode(),
    b"%PDF-1.7\n\xe2\xe3\xcf\xd3\n" + HTTP.encode() + b"\n\xff\xfe\xfd",
    b"PK\x03\x04\x14\x00\x00\x00" + HTTP.encode(),
], ids=["image", "pdf", "archive"])
def test_binary_content_is_never_scanned_for_links(data):
    assert refusal(data) == "The file is not a text file"


def test_the_size_bound_applies_before_any_decoding():
    assert "1 MB" in refusal(b"\xff" * (MAX_LINK_FILE_BYTES + 1))
    many = "\n".join(f"https://a.example/{index}" for index in range(101)).encode()
    assert "A maximum of 100 links" in refusal(many)


# --------------------------------------------------------------------------- #
# Submission: each link reaches the owner Quick Add would use
# --------------------------------------------------------------------------- #

async def _roots(api):
    async with database.get_db() as db:
        return await db.fetchall("SELECT transfer_id FROM transfer_requests WHERE parent_id IS NULL")


async def _upload(api, name, document, **form):
    return await api.client.post("/api/links/add-file", files={"file": (name, document.encode(), "text/plain")},
                                 data=form)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["downloads", "whatever.xyz"])
async def test_a_link_file_is_one_direct_link_batch_plus_one_transfer_per_magnet(api, name):
    response = await _upload(api, name, json.dumps(LINKS), selection_mode="interactive")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] == 3
    batch, magnet = body["items"]
    batch_requests = [record.request for record in await api.repository.requests(batch["id"])]
    magnet_requests = [record.request for record in await api.repository.requests(magnet["id"])]
    # Exactly the transfers Quick Add creates: one submission of independent
    # direct links, and the magnet on its own through the magnet owner.
    assert [(request.kind, request.payload) for request in batch_requests] == [("https", HTTP), ("sftp", SFTP)]
    assert [(request.kind, request.payload) for request in magnet_requests] == [("magnet", MAGNET)]
    assert {request.selection_mode for request in batch_requests + magnet_requests} == {"interactive"}


@pytest.mark.asyncio
@pytest.mark.parametrize("document", [
    f"{HTTP}\nfoo://x.example/secret-token\n",
    f"{HTTP}\nmagnet:?xt=urn:btih:secret-token\n",
], ids=["unsupported-link", "invalid-magnet"])
async def test_one_unusable_link_admits_nothing_and_is_named_without_its_value(api, document):
    response = await _upload(api, "links.txt", document)
    assert response.status_code == 400
    assert "Line 2 is not a supported link" in response.text
    assert "secret-token" not in response.text
    assert await _roots(api) == []


@pytest.mark.asyncio
async def test_private_lan_links_ask_the_existing_confirmation_before_anything_is_admitted(api):
    api.engine.policy = dataclasses.replace(api.engine.policy, private_lan_connections=True)
    document = f"{MAGNET}\nhttp://192.168.1.20/a.bin\n"
    asked = await _upload(api, "links.txt", document)
    assert asked.status_code == 409
    assert asked.json()["detail"]["confirmation"] == "local_network"
    assert asked.json()["detail"]["hosts"] == ["192.168.1.20"]
    assert await _roots(api) == []                 # not even the magnet

    allowed = await _upload(api, "links.txt", document, allow_local_network="true")
    assert allowed.status_code == 200, allowed.text
    batch, _magnet = allowed.json()["items"]
    (record,) = await api.repository.requests(batch["id"])
    assert record.request.local_network_consent is True


@pytest.mark.asyncio
async def test_structured_files_keep_their_owners_and_never_become_link_lists(api):
    torrent = await api.client.post("/api/torrents/add-file",
                                    files={"file": ("bad.torrent", HTTP.encode(), "application/x-bittorrent")})
    assert torrent.status_code == 400 and "Invalid torrent metainfo" in torrent.text
    for document in (
        "d8:announce" + str(len(HTTP)) + ":" + HTTP + "4:infod4:name1:x6:lengthi1eee",
        '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"><file><groups><group>a.b</group></groups></file></nzb>',
        f'<metalink xmlns="urn:ietf:params:xml:ns:metalink"><file name="f"><url>{HTTP}</url></file></metalink>',
    ):
        assert (await _upload(api, "renamed.txt", document)).status_code == 400
    assert await _roots(api) == []
