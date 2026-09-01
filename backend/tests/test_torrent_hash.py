"""Regression coverage for validated .torrent info-hash extraction."""

import hashlib

import bencode2

from transfers.requests import extract_hash_from_torrent


def _metainfo(announce: bytes = b"https://tracker.invalid/announce") -> tuple[bytes, bytes]:
    info = {
        b"length": 1234,
        b"name": b"example.bin",
        b"piece length": 262144,
        b"pieces": b"\x11" * 20,
    }
    return bencode2.bencode({b"announce": announce, b"info": info}), bencode2.bencode(info)


def test_extracts_sha1_of_info_dictionary():
    torrent, encoded_info = _metainfo()

    assert extract_hash_from_torrent(torrent) == hashlib.sha1(encoded_info).hexdigest()


def test_top_level_announce_does_not_change_info_hash():
    first, _ = _metainfo(b"https://one.invalid/announce")
    second, _ = _metainfo(b"https://two.invalid/announce")

    assert extract_hash_from_torrent(first) == extract_hash_from_torrent(second)


def test_rejects_invalid_or_missing_info_dictionary():
    assert extract_hash_from_torrent(b"not-bencode") == ""
    assert extract_hash_from_torrent(bencode2.bencode({b"announce": b"x"})) == ""
    assert extract_hash_from_torrent(bencode2.bencode([b"not", b"a", b"dict"])) == ""
