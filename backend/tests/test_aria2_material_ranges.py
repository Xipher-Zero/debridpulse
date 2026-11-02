"""aria2 completed-piece evidence: ``translation.material_ranges`` is linear in the piece count.

The conversion used to turn the whole hexadecimal bitfield into one integer and
shift that integer once per piece, which is quadratic in the piece count and ran
on every observation of a job. These tests hold the replacement to the exact
answer of that earlier conversion, kept here as the oracle only, and to ranges
written out by hand.
"""
from __future__ import annotations

import itertools
import random
from types import SimpleNamespace

import pytest

from executors.aria2.translation import material_ranges


def _oracle(native):
    """The conversion as it stood before (DebridPulse 1.0.14, 523d3c7b), verbatim."""
    bitfield = str(getattr(native, "bitfield", "") or "")
    piece = int(getattr(native, "piece_length", 0) or 0)
    pieces = int(getattr(native, "num_pieces", 0) or 0)
    total = int(getattr(native, "total_length", 0) or 0)
    if not bitfield or piece <= 0 or pieces <= 0 or total <= 0 or len(getattr(native, "files", None) or ()) > 1:
        return None
    try:
        bits = int(bitfield, 16)
    except ValueError:
        return None
    width = len(bitfield) * 4
    ranges = []
    for index in range(min(pieces, width)):
        if bits >> (width - 1 - index) & 1:
            start, end = index * piece, min((index + 1) * piece, total)
            if ranges and ranges[-1][1] == start:
                ranges[-1] = (ranges[-1][0], end)
            elif end > start:
                ranges.append((start, end))
    return tuple(ranges)


def _native(bitfield, piece=4, pieces=8, total=30, files=({"path": "/d/f"},)):
    return SimpleNamespace(bitfield=bitfield, piece_length=piece, num_pieces=pieces, total_length=total, files=list(files))


@pytest.mark.parametrize(("native", "expected"), [
    # piece 0 is the most significant bit of the first hex digit
    (_native("80"), ((0, 4),)),
    (_native("01"), ((28, 30),)),                       # the last piece is partial: it ends at the total
    (_native("c3"), ((0, 8), (24, 30))),                # a hole keeps two ranges apart
    (_native("ff"), ((0, 30),)),
    (_native("00"), ()),                                # evidence of no complete piece
    (_native("ff", pieces=3), ((0, 12),)),              # bits past the job's own pieces are not pieces
    (_native("ff", total=10), ((0, 10),)),              # pieces wholly past the total add nothing
    (_native("7f", total=10), ((4, 10),)),
    (_native("0f", total=10), ()),
    (_native("ff00", pieces=16, total=64), ((0, 32),)),
    (_native("ff", pieces=12), ((0, 30),)),             # a bitfield shorter than the job
], ids=lambda value: getattr(value, "bitfield", None) or str(value))
def test_ranges_are_exact_half_open_byte_ranges(native, expected):
    assert material_ranges(native) == expected


@pytest.mark.parametrize("native", [
    _native(""), _native(None), _native("zz"), _native("0x"), _native("f f"), _native("-"),
    _native("ff", piece=0), _native("ff", pieces=0), _native("ff", total=0), _native("ff", piece=-4),
    _native("ff", files=({"path": "/d/a"}, {"path": "/d/b"})),   # a multi-file job states no file's ranges
    SimpleNamespace(),
])
def test_no_evidence_is_none_never_an_empty_claim(native):
    assert material_ranges(native) is None


def test_every_short_bitfield_agrees_with_the_earlier_conversion():
    """Exhaustive over every bitfield of one to three hex digits, both letter
    cases, and piece geometries that end exactly on, inside and before the
    last piece."""
    digits = "0123456789abcdefABCDEF"
    shapes = [(4, pieces, total) for pieces in (1, 3, 4, 8, 11, 12, 13) for total in (1, 4, 17, 30, 32, 44, 48, 60)]
    shapes += [(1, 12, 12), (7, 12, 80), (1 << 20, 12, 12 * (1 << 20) - 1)]
    checked = 0
    for length in (1, 2, 3):
        for text in map("".join, itertools.product(digits, repeat=length)):
            if length == 3 and text.lower() != text:
                continue                                    # one case is enough at three digits
            for piece, pieces, total in shapes:
                native = _native(text, piece, pieces, total)
                assert material_ranges(native) == _oracle(native), (text, piece, pieces, total)
                checked += 1
    assert checked > 100_000


def test_inputs_int_accepts_beyond_plain_hex_agree_with_the_earlier_conversion():
    """``int(text, 16)`` also accepts a sign, a ``0x`` prefix, digit-group
    underscores and surrounding whitespace; aria2 sends none of them, and their
    answer stays exactly what it was."""
    for text in ("0xff", "0XfF", "+f0", "-1", "-ff", "-0f0", " ff", "ff ", "f_f", "0x_f_f", "\tf0\n", "-0", "00f"):
        for piece, pieces, total in ((4, 8, 30), (4, 24, 96), (3, 5, 14)):
            native = _native(text, piece, pieces, total)
            assert material_ranges(native) == _oracle(native), (text, piece, pieces, total)


def test_random_large_bitfields_agree_with_the_earlier_conversion():
    rng = random.Random(20261102)
    for _ in range(300):
        pieces = rng.randint(1, 3000)
        piece = rng.choice((1, 3, 16384, 1 << 20))
        total = max(1, pieces * piece - rng.randint(0, piece - 1)) if rng.random() < 0.8 else rng.randint(1, pieces * piece * 2)
        hex_digits = -(-pieces // 4) + rng.choice((0, 0, 0, 1, -1 if pieces > 4 else 0))
        density = rng.random()
        value = sum(1 << bit for bit in range(hex_digits * 4) if rng.random() < density)
        text = format(value, f"0{hex_digits}x")
        native = _native(text, piece, pieces, total)
        assert material_ranges(native) == _oracle(native), (text[:16], piece, pieces, total)
