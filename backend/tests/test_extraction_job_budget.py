"""1.0.13 WS4a: archive preflight is bounded before it allocates (SEC-001), and
one extraction job -- an archive and every nested archive it contains -- has
ONE budget, measured against the original input and committed once
(PA-PARSE-1).

Small, hand-built archives and a stand-in listing tool keep every case quick;
the configured limits are lowered so the same rules bite at test sizes.
"""
from __future__ import annotations

import io
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import services.extraction_safety as safety
from postprocessors.archive import extractor
from postprocessors.archive.extractor import Extractor, _extract_tar, _list_7z
from postprocessors.archive.secure import SecureExtractor
from services.extraction_safety import ExtractionBudgetExceeded, validate_7z_listing, validate_tar_members


@pytest.fixture
def limited(monkeypatch):
    def apply(files=12, gib=0.01, ratio=2000.0):
        monkeypatch.setattr(safety, "get_settings", lambda: SimpleNamespace(
            extract_max_files=files, extract_max_expanded_gb=gib, extract_max_compression_ratio=ratio))
    monkeypatch.setattr(extractor, "_get_extraction_passwords", lambda: [])
    apply()
    return apply


def _zip(members: dict[str, bytes]) -> bytes:
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return blob.getvalue()


def _tar(path: Path, members: dict[str, bytes], mode: str = "w:xz") -> Path:
    with tarfile.open(path, mode) as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def _files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


# ── PA-PARSE-1: one job, one budget, one commit ──────────────────────────────

def _bomb(tmp_path: Path) -> Path:
    """The packet's reproduction at test scale: a ~600-byte outer archive
    holding ten deflated zips of one MiB of zeros each -- each inner archive
    within every per-archive limit, the job ten MiB from a few hundred bytes."""
    inner = _zip({"payload.bin": b"\0" * (1 << 20)})
    return _tar(tmp_path / "outer.tar.xz", {f"sub/inner{i}.zip": inner for i in range(10)})


@pytest.mark.asyncio
@pytest.mark.parametrize("extractor_class", [SecureExtractor, Extractor])
async def test_nested_archives_share_the_jobs_one_budget_and_commit_nothing_past_it(
        tmp_path, limited, extractor_class):
    limited(files=40, gib=4 / 1024, ratio=2000.0)  # 4 MiB, far below the job's ten
    outer = _bomb(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "unrelated.txt").write_bytes(b"kept")
    created: list[Path] = []
    ok, message = await extractor_class(1).extract_archive(outer, dest, delete_after=False, created=created)
    assert ok is False and "Extraction failed" in message
    assert "bytes" in message or "ratio" in message
    # Nothing of the job was committed: no outer member, no nested output, no staging.
    assert _files(dest) == ["unrelated.txt"]
    assert created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("extractor_class", [SecureExtractor, Extractor])
async def test_nested_archives_within_the_budget_extract_into_their_own_folder(tmp_path, limited, extractor_class):
    limited(files=40, gib=0.01, ratio=2000.0)
    inner_a = _zip({"a.txt": b"alpha"})
    inner_b = _zip({"b.txt": b"beta"})
    outer = _tar(tmp_path / "outer.tar.xz", {"top.txt": b"top", "sub/a.zip": inner_a, "sub/b.zip": inner_b})
    dest = tmp_path / "dest"
    created: list[Path] = []
    ok, _message = await extractor_class(1).extract_archive(outer, dest, delete_after=False, created=created)
    assert ok is True
    assert _files(dest) == ["sub/a.txt", "sub/a.zip", "sub/b.txt", "sub/b.zip", "top.txt"]
    assert sorted(str(p.relative_to(dest)) for p in created) == _files(dest)


@pytest.mark.asyncio
async def test_a_consumed_nested_archive_is_removed_when_the_job_deletes_its_sources(tmp_path, limited):
    outer = _tar(tmp_path / "outer.tar.xz", {"sub/a.zip": _zip({"a.txt": b"alpha"})})
    dest = tmp_path / "dest"
    ok, _message = await SecureExtractor(1).extract_archive(outer, dest, delete_after=True)
    assert ok is True and _files(dest) == ["sub/a.txt"] and not outer.exists()


@pytest.mark.asyncio
async def test_an_ordinary_nested_failure_stays_an_ordinary_one(tmp_path, limited):
    """A corrupt inner archive is not a safety violation: it is kept as a
    file, the other nested archives still extract and the job succeeds."""
    outer = _tar(tmp_path / "outer.tar.xz", {"sub/broken.zip": b"not a zip at all",
                                             "sub/fine.zip": _zip({"fine.txt": b"fine"})})
    dest = tmp_path / "dest"
    ok, _message = await SecureExtractor(1).extract_archive(outer, dest, delete_after=False)
    assert ok is True
    assert _files(dest) == ["sub/broken.zip", "sub/fine.txt", "sub/fine.zip"]


@pytest.mark.asyncio
async def test_a_nested_archive_is_refused_against_what_the_job_has_left(tmp_path, limited, caplog):
    """The nested archive alone fits the configured limit, but not what the
    outer archive left of it: its own preflight refuses it, naming the
    remaining budget, before it writes."""
    limited(files=40, gib=2 / 1024, ratio=100000.0)  # 2 MiB job budget
    outer = _tar(tmp_path / "outer.tar.xz", {"sub/filler.bin": b"\1" * (1536 << 10),
                                             "sub/inner.zip": _zip({"payload.bin": b"\0" * (1 << 20)})})
    dest = tmp_path / "dest"
    ok, message = await SecureExtractor(1).extract_archive(outer, dest, delete_after=False)
    assert ok is False
    configured = 2 * 1024 * 1024
    limit = int(message.rsplit("limit is ", 1)[1])
    assert 0 < limit < configured - (1536 << 10)  # the remainder after the outer's own members
    assert not dest.exists() or _files(dest) == []


# ── SEC-001: TAR headers are checked as they are read ────────────────────────

def test_tar_members_are_refused_at_the_first_one_past_the_limit(limited):
    limited(files=50)
    consumed = 0

    def members():
        nonlocal consumed
        for index in range(100_000):
            consumed += 1
            info = tarfile.TarInfo(f"d/{index}")
            yield info

    with pytest.raises(ExtractionBudgetExceeded, match="51 files; limit is 50"):
        validate_tar_members(Path(__file__), members())
    assert consumed == 51


def test_tar_directories_are_bounded_too(tmp_path, limited):
    limited(files=50)
    archive = tmp_path / "dirs.tar.gz"
    with tarfile.open(archive, "w:gz") as out:
        for index in range(2000):
            info = tarfile.TarInfo(f"d{index}")
            info.type = tarfile.DIRTYPE
            out.addfile(info)
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(ExtractionBudgetExceeded, match="non-file entries"):
        _extract_tar(archive, dest)
    assert list(dest.iterdir()) == []


def test_many_tar_members_are_refused_without_extracting_any(tmp_path, limited):
    limited(files=50)
    archive = _tar(tmp_path / "many.tar.gz", {f"d/{i:05d}": b"" for i in range(5000)}, mode="w:gz")
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(ExtractionBudgetExceeded):
        _extract_tar(archive, dest)
    assert list(dest.iterdir()) == []


def _pax_archive(path: Path, headers: list[dict[str, str]]) -> Path:
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as out:
        for index, pax in enumerate(headers):
            info = tarfile.TarInfo(f"f{index}")
            info.pax_headers = pax
            out.addfile(info, io.BytesIO(b""))
    return path


def test_an_oversized_tar_metadata_record_is_refused_before_it_is_read(tmp_path, limited):
    archive = _pax_archive(tmp_path / "pax.tar.gz", [{"comment": "x" * (2 << 20)}])
    with pytest.raises(ExtractionBudgetExceeded, match="metadata"):
        _extract_tar(archive, tmp_path)
    single = tmp_path / "pax.gz"  # the single-file .gz route probes it as TAR first
    archive.rename(single)
    with pytest.raises(ExtractionBudgetExceeded, match="metadata"):
        extractor._extract_into_directory(single, tmp_path / "out")


def test_tar_metadata_is_bounded_in_total_by_the_entry_budget(tmp_path, limited):
    limited(files=2)  # 2 x 2 entries x 8 KiB of metadata at most
    archive = _pax_archive(tmp_path / "pax.tar.gz", [{"comment": "y" * 20_000} for _ in range(3)])
    with pytest.raises(ExtractionBudgetExceeded, match="metadata"):
        _extract_tar(archive, tmp_path)


def test_a_too_long_member_name_is_refused(limited):
    with pytest.raises(ValueError, match="longer than the filesystem allows"):
        safety.validate_member_name("a/" * 2049)


# ── SEC-001: a 7-Zip listing is validated as it is emitted ───────────────────

def _tool(script: str) -> list[str]:
    return [sys.executable, "-c", script]


_HEADER = "print('Path = archive.7z'); print('Type = 7z'); print(); print('----------')"


def test_an_overlong_listing_line_stops_the_tool_without_holding_it(tmp_path, limited):
    archive = tmp_path / "a.7z"
    archive.write_bytes(b"7z")
    script = _HEADER + "; import sys; sys.stdout.write('Path = ' + 'a' * (64 << 20) + '\\n'); sys.stdout.flush()"
    started = time.monotonic()
    with pytest.raises(ExtractionBudgetExceeded, match="listing line"):
        _list_7z(_tool(script), archive)
    assert time.monotonic() - started < 30


def test_a_listing_past_the_entry_budget_stops_the_tool_early(tmp_path, limited):
    limited(files=50)
    archive = tmp_path / "a.7z"
    archive.write_bytes(b"7z")
    marker = tmp_path / "finished"
    script = (_HEADER + "; import sys\n"
              "for i in range(200000):\n"
              "    print(f'Path = f{i}'); print('Size = 1'); print('Attributes = A'); print()\n"
              f"open({str(marker)!r}, 'w').close()")
    with pytest.raises(ExtractionBudgetExceeded, match="51 files; limit is 50"):
        _list_7z(_tool(script), archive)
    assert not marker.exists()  # the tool was stopped, not read to its end


def test_a_completed_bounded_listing_is_accepted_and_a_failed_one_never_is(tmp_path, limited):
    archive = tmp_path / "a.7z"
    archive.write_bytes(b"7z")
    good = _HEADER + "; print('Path = one'); print('Size = 3'); print('Attributes = A'); print()"
    assert _list_7z(_tool(good), archive)[0] == 0
    unsafe = _HEADER + "; print('Path = ../escape'); print('Size = 3'); print('Attributes = A')"
    with pytest.raises(ValueError, match="Unsafe archive member path"):
        _list_7z(_tool(unsafe), archive)
    failed = unsafe + "; raise SystemExit(2)"
    assert _list_7z(_tool(failed), archive)[0] == 2  # not authoritative: the next candidate is tried


def test_listing_text_and_streamed_lines_decide_alike(limited):
    limited(files=1)
    listing = "Header\n----------\nPath = one\nSize = 1\nAttributes = A\n\nPath = two\nSize = 1\nAttributes = A\n"
    for output in (listing, iter(listing.splitlines(keepends=True))):
        with pytest.raises(ExtractionBudgetExceeded, match="files"):
            validate_7z_listing(Path(__file__), output)


def test_extraction_tool_output_is_kept_only_as_a_short_tail(tmp_path):
    rc, output = extractor._run_tool(_tool("import sys; sys.stdout.write('z' * (32 << 20) + 'END')"))
    assert rc == 0 and output.endswith("END") and len(output) <= 4096
