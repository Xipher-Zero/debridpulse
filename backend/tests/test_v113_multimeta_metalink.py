"""DP 1.0.13 Multimeta: Metalink4 (RFC 5854) descriptor interpretation.

The parser reads a document into neutral facts -- each file's safe relative
path, declared size, whole-file hashes and ordinary ``<url>`` sources in the
publisher's order of preference -- and nothing else. It is bounded, refuses
any document type or entity outright, and never truncates.
"""
from __future__ import annotations

import pytest

from providers.multimeta import metalink
from providers.multimeta.metalink import DescribedFile, InvalidDescriptor, parse

SHA256 = "c0" * 32
HEAD = '<?xml version="1.0" encoding="UTF-8"?>\n'


def document(*files: str, namespace: str = metalink.NAMESPACE, prolog: str = HEAD) -> bytes:
    return f'{prolog}<metalink xmlns="{namespace}">{"".join(files)}</metalink>'.encode()


def file(name: str, *children: str) -> str:
    return f'<file name="{name}">{"".join(children)}</file>'


def refused(data: bytes, **kwargs) -> str:
    with pytest.raises(InvalidDescriptor) as raised:
        parse(data, **kwargs)
    return raised.value.reason


def test_one_file_is_its_path_size_hash_and_sources_in_publisher_order():
    data = document(file(
        "release.iso",
        "<size>4</size>",
        f'<hash type="sha-256">{SHA256.upper()}</hash>',
        '<hash type="sha-384">' + "ab" * 48 + "</hash>",   # not a digest DP verifies: ignored
        '<url priority="20">https://late.example/release.iso</url>',
        "<url>http://undeclared.example/release.iso</url>",
        '<url priority="1" location="de">ftp://first.example/pub/release.iso</url>',
        '<url priority="20">https://tie.example/release.iso</url>',
        '<metaurl mediatype="torrent">https://meta.example/release.torrent</metaurl>',
        '<pieces length="262144" type="sha-1"><hash>' + "aa" * 20 + "</hash></pieces>",
        "<signature>opaque</signature><description>optional</description>",
    ))
    assert parse(data) == (DescribedFile(
        "release.iso", 4, (("sha256", SHA256),),
        ("ftp://first.example/pub/release.iso", "https://late.example/release.iso",
         "https://tie.example/release.iso", "http://undeclared.example/release.iso")),)


def test_several_files_keep_their_safe_nested_paths():
    data = document(file("disc/one.bin", "<url>https://a.example/one.bin</url>"),
                    file("disc/sub/two.bin", "<size>9</size><url>https://a.example/two.bin</url>"))
    assert [(item.path, item.size) for item in parse(data)] == [("disc/one.bin", 0), ("disc/sub/two.bin", 9)]


def test_a_relative_source_resolves_only_against_a_remote_base():
    data = document(file("x.bin", "<url>mirror/x.bin</url>", "<url>https://abs.example/x.bin</url>"))
    # An upload has no address of its own: the relative reference names nothing.
    assert parse(data)[0].sources == ("https://abs.example/x.bin",)
    remote = parse(data, base="https://moved.example/dir/list.meta4")
    assert remote[0].sources == ("https://moved.example/dir/mirror/x.bin", "https://abs.example/x.bin")


def test_a_file_without_a_usable_url_says_why_and_never_follows_a_metadata_reference():
    data = document(
        file("only-torrent.bin", '<metaurl mediatype="torrent">https://m.example/t.torrent</metaurl>'),
        file("unusable.bin", "<url>relative/only.bin</url>", "<url>https://u:p@cred.example/x</url>",
             "<url>https://next.example/another.meta4</url>"),
        file("good.bin", "<url>gopher://old.example/good.bin</url>"),
    )
    only, unusable, good = parse(data)
    assert (only.sources, only.unusable) == ((), "metaurl_only")
    assert (unusable.sources, unusable.unusable) == ((), "no_usable_url")
    # Whether a scheme is supported is routing's decision, never the parser's.
    assert (good.sources, good.unusable) == (("gopher://old.example/good.bin",), "")


@pytest.mark.parametrize("data,reason", [
    (b"<metalink", "malformed"),
    (b"not xml at all", "malformed"),
    (document(namespace="http://www.metalinker.org/"), "unsupported"),           # Metalink 3
    (HEAD.encode() + b'<metalink version="3.0" xmlns="http://www.metalinker.org/"><files/></metalink>', "unsupported"),
    (HEAD.encode() + b"<feed xmlns='http://www.w3.org/2005/Atom'/>", "unsupported"),
    (document(), "malformed"),                                                   # no file at all
    (document(file("a.bin")), "malformed"),                                      # a file names no source
    (document(file("a.bin", "<size>-1</size><url>https://a.example/a</url>")), "malformed"),
    (document(file("a.bin", "<size>1e3</size><url>https://a.example/a</url>")), "malformed"),
    (document(file("a.bin", '<url priority="0">https://a.example/a</url>')), "malformed"),
    (document(file("a.bin", '<url priority="high">https://a.example/a</url>')), "malformed"),
    (document(file("a.bin", '<hash type="sha-256">abc</hash><url>https://a.example/a</url>')), "malformed"),
    (document(file("a.bin", "<url>https://a.example/a</url>"),
              file("A.BIN", "<url>https://a.example/b</url>")), "malformed"),        # one name, one file
])
def test_a_document_dp_does_not_interpret_is_refused_whole(data, reason):
    assert refused(data) == reason


@pytest.mark.parametrize("prolog", [
    # External entity (XXE), internal entity expansion and a bare document type.
    HEAD + '<!DOCTYPE metalink [<!ENTITY x SYSTEM "file:///etc/passwd">]>',
    HEAD + '<!DOCTYPE metalink [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>',
    HEAD + '<!DOCTYPE metalink SYSTEM "http://evil.example/metalink.dtd">',
])
def test_no_document_type_or_entity_is_ever_resolved(prolog):
    assert refused(document(file("a.bin", "<url>https://a.example/a</url>"), prolog=prolog)) == "malformed"


@pytest.mark.parametrize("name", ["../escape.bin", "disc/../../escape.bin", "/etc/passwd", "a\\..\\..\\b"])
def test_a_path_that_could_escape_the_transfer_is_refused(name):
    assert refused(document(file(name, "<url>https://a.example/a</url>"))) == "unsafe_path"


def test_every_bound_refuses_rather_than_truncates(monkeypatch):
    two = document(file("a.bin", "<url>https://a.example/a</url>"), file("b.bin", "<url>https://a.example/b</url>"))
    monkeypatch.setattr(metalink, "MAX_FILES", 1)
    assert refused(two) == "too_large"
    monkeypatch.setattr(metalink, "MAX_FILES", 10)
    monkeypatch.setattr(metalink, "MAX_SOURCES_PER_FILE", 1)
    assert refused(document(file("a.bin", "<url>https://a.example/a</url><url>https://b.example/a</url>"))) \
        == "too_large"
    monkeypatch.setattr(metalink, "MAX_SOURCES_PER_FILE", 10)
    monkeypatch.setattr(metalink, "MAX_SOURCES", 1)
    assert refused(two) == "too_large"
    monkeypatch.setattr(metalink, "MAX_DESCRIPTOR_BYTES", 16)
    assert refused(two) == "too_large"
