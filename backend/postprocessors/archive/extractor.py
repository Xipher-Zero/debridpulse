"""
services/extractor.py — Post-download archive extraction service.

Supports: .zip, .tar, .tar.gz, .tgz, .tar.bz2, .tar.xz, .gz,
          .bz2, .xz, .7z, .rar, multi-part .rar (*.part1.rar / *.r00),
          .tar.zst / .tar.lzma (via 7z binary)

Strategy:
  1. Python-native for zip / tar / gz / bz2 / xz (zero extra deps)
  2. System binary `7z` (Debian 7zip; RAR via 7zip-rar) for .7z, .tar.zst, .tar.lzma, and RAR
  3. RAR extraction fails closed unless a 7z-compatible binary is available

After successful extraction the source archive is deleted.
"""
from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import functools
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Optional, Tuple

from services.extraction_safety import (
    MAX_LISTING_LINE_CHARS,
    ExtractionBudgetExceeded,
    ExtractionLimits,
    configured_limits,
    copy_limited,
    is_tar,
    job_byte_limit,
    job_limits,
    open_tar,
    staged_external_extract,
    validate_extracted_tree,
    validate_staging_tree,
    validate_7z_listing,
    validate_tar_members,
    validate_zip_members,
)

# The most of an external tool's own output kept for a diagnostic.
_TOOL_OUTPUT_TAIL_BYTES = 4096

logger = logging.getLogger("alldebrid.extractor")

# ---------------------------------------------------------------------------
# Archive detection
# ---------------------------------------------------------------------------

# Extension groups in priority order
_ZIP_EXTS  = {".zip"}
_TAR_EXTS  = {".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
               ".tar.xz", ".txz"}
# .tar.zst and .tar.lzma need 7z — keep separate so _extract_sync routes correctly
_TAR_7Z_EXTS = {".tar.zst", ".tzst", ".tar.lzma"}
_GZ_EXTS   = {".gz"}   # single-file gzip (not .tar.gz — that's handled by tar)
_BZ2_EXTS  = {".bz2"}
_XZ_EXTS   = {".xz"}
_7Z_EXTS   = {".7z"}
_RAR_EXTS  = {".rar", ".r00", ".r01", ".r02"}
_RAR4_MAGIC = b"Rar!\x1a\x07\x00"
_RAR5_MAGIC = b"Rar!\x1a\x07\x01\x00"

# Multi-part RAR detection: file.part1.rar, file.part01.rar, file.r00
_MULTIPART_RAR = re.compile(
    r"\.part\d+\.rar$|\.r\d{2}$", re.IGNORECASE
)
# Only the first part should be extracted; subsequent parts are auto-read
_MULTIPART_FIRST = re.compile(
    r"\.part0*1\.rar$|\.r00$", re.IGNORECASE
)


def _suffix(path: Path) -> str:
    """Normalised lower-case compound suffix, e.g. '.tar.gz'."""
    name = path.name.lower()
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.lzma",
                ".tgz", ".tbz2", ".txz", ".tzst"):
        if name.endswith(ext):
            return ext
    return path.suffix.lower()


def is_archive(path: Path) -> bool:
    """Return True if *path* looks like an extractable archive."""
    s = _suffix(path)
    if s in _ZIP_EXTS | _TAR_EXTS | _TAR_7Z_EXTS | _GZ_EXTS | _BZ2_EXTS | _XZ_EXTS | _7Z_EXTS:
        return True
    if s in _RAR_EXTS:
        # Skip non-first parts of multi-part RAR sets
        if _MULTIPART_RAR.search(path.name):
            return _MULTIPART_FIRST.search(path.name) is not None
        return True
    return False


def find_archives(folder: Path) -> List[Path]:
    """Walk *folder* recursively and return all extractable archives."""
    archives: List[Path] = []
    try:
        for root, _dirs, files in os.walk(folder):
            for f in sorted(files):
                p = Path(root) / f
                if is_archive(p):
                    archives.append(p)
    except OSError as exc:
        logger.warning("find_archives: cannot walk %s: %s", folder, exc)
    return archives


def archive_paths_from_downloads(paths: Iterable[str | Path]) -> List[Path]:
    """Return extractable archives from known downloaded file paths.

    This is intentionally non-recursive. Auto-extract already knows every file
    that belongs to a torrent via download_files, so walking the full download
    tree is wasted I/O on large media folders.
    """
    archives: List[Path] = []
    seen: set[str] = set()
    for raw in paths:
        if not raw:
            continue
        p = Path(raw)
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        if is_archive(p):
            archives.append(p)
    return archives


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _tool_available(name: str) -> bool:
    return shutil.which(name) is not None


def _run_tool(
    cmd: List[str],
    timeout: int = 3600,
    *,
    watch_dir: Path | None = None,
    watch_archive: Path | None = None,
) -> Tuple[int, str]:
    """Run an external command and optionally enforce a live staging budget."""
    kwargs = {}
    if os.name == "posix":
        kwargs["preexec_fn"] = lambda: os.nice(10)

    started = time.monotonic()
    try:
        with tempfile.TemporaryFile() as capture:
            process = subprocess.Popen(
                cmd,
                stdout=capture,
                stderr=subprocess.STDOUT,
                text=True,
                **kwargs,
            )
            guard_error = ""
            timed_out = False
            while process.poll() is None:
                if time.monotonic() - started > timeout:
                    timed_out = True
                    process.kill()
                    break
                if watch_dir is not None and watch_archive is not None:
                    try:
                        validate_staging_tree(watch_dir, watch_archive)
                    except ValueError as exc:
                        guard_error = str(exc)
                        process.kill()
                        break
                time.sleep(0.25)

            process.wait(timeout=10)
            capture.flush()
            # Only the tail is a diagnostic; the output itself is never held.
            size = capture.seek(0, os.SEEK_END)
            capture.seek(max(0, size - _TOOL_OUTPUT_TAIL_BYTES))
            output = capture.read().decode("utf-8", errors="replace").strip()
            if guard_error:
                return -1, f"Extraction safety limit: {guard_error}"
            if timed_out:
                return -1, f"Timeout after {timeout}s"
            return int(process.returncode or 0), output
    except FileNotFoundError as exc:
        return -1, str(exc)


def _extract_zip(archive: Path, dest: Path) -> None:
    root = dest.resolve()
    no_follow = getattr(os, "O_NOFOLLOW", 0)

    with zipfile.ZipFile(archive, "r") as zf:
        members = zf.infolist()
        validate_zip_members(archive, members)
        for member in members:
            # ZIP member names are POSIX paths. Treat backslashes as separators
            # as well so a Windows-style traversal remains unsafe on Linux.
            member_name = member.filename.replace("\\", "/")
            relative = PurePosixPath(member_name)
            parts = tuple(part for part in relative.parts if part not in ("", "."))
            unix_mode = member.external_attr >> 16

            if (
                not parts
                or relative.is_absolute()
                or ".." in parts
                or ":" in parts[0]
                or stat.S_ISLNK(unix_mode)
            ):
                raise ValueError(f"Unsafe ZIP member path: {member.filename!r}")

            target = root.joinpath(*parts)
            resolved_target = target.resolve(strict=False)
            if resolved_target != root and root not in resolved_target.parents:
                raise ValueError(f"ZIP member escapes extraction directory: {member.filename!r}")

            current = root
            for part in parts[:-1]:
                current = current / part
                if current.is_symlink():
                    raise ValueError(f"ZIP member traverses a symlink: {member.filename!r}")
                current.mkdir(exist_ok=True)

            if member.is_dir():
                if target.is_symlink():
                    raise ValueError(f"ZIP directory replaces a symlink: {member.filename!r}")
                target.mkdir(exist_ok=True)
                continue

            if target.is_symlink():
                raise ValueError(f"ZIP file replaces a symlink: {member.filename!r}")
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | no_follow
            # Create regular extracted files with standard non-writable group/
            # other permissions.  Do not rely on the process umask to remove
            # potentially dangerous write bits from a permissive mode.
            fd = os.open(target, flags, 0o644)
            with zf.open(member, "r") as source, os.fdopen(fd, "wb") as output:
                shutil.copyfileobj(source, output)


def _extract_tar(archive: Path, dest: Path) -> None:
    with open_tar(archive, "r:*") as tf:
        members = validate_tar_members(archive, tf)
        tf.extractall(dest, members=members, filter="data")


def _extract_gz_single(archive: Path, dest: Path) -> None:
    """Single-file .gz (not .tar.gz)."""
    import gzip
    out_name = archive.stem  # strip .gz
    out_path = dest / out_name
    try:
        with gzip.open(archive, "rb") as gz_in, open(out_path, "wb") as f_out:
            copy_limited(gz_in, f_out, archive=archive)
    except Exception:
        out_path.unlink(missing_ok=True)
        raise


def _extract_bz2_single(archive: Path, dest: Path) -> None:
    import bz2
    out_name = archive.stem
    out_path = dest / out_name
    try:
        with bz2.open(archive, "rb") as bz_in, open(out_path, "wb") as f_out:
            copy_limited(bz_in, f_out, archive=archive)
    except Exception:
        out_path.unlink(missing_ok=True)
        raise


def _extract_xz_single(archive: Path, dest: Path) -> None:
    import lzma
    out_name = archive.stem
    out_path = dest / out_name
    try:
        with lzma.open(archive, "rb") as xz_in, open(out_path, "wb") as f_out:
            copy_limited(xz_in, f_out, archive=archive)
    except Exception:
        out_path.unlink(missing_ok=True)
        raise


def _get_extraction_passwords() -> list[str]:
    """Return list of extraction passwords from config (newline-separated field)."""
    try:
        from core.config import get_settings
        raw = str(getattr(get_settings(), "extraction_password", "") or "").strip()
        return [p.strip() for p in raw.split("\n") if p.strip()]
    except Exception:
        return []


def _rar_version(archive: Path) -> int:
    """Validate RAR content by magic bytes and return major format version.

    7-Zip 25.x distinguishes its forced legacy RAR parser from RAR5.  Forcing
    ``-trar`` against a valid RAR5 archive makes 7-Zip reject it as "not archive".
    Validate the content ourselves, then allow 7-Zip to choose the RAR4/RAR5
    parser from the already-verified bytes.
    """
    try:
        with archive.open("rb") as source:
            header = source.read(max(len(_RAR4_MAGIC), len(_RAR5_MAGIC)))
    except OSError as exc:
        raise RuntimeError(f"Cannot read RAR header for {archive.name}: {exc}") from exc

    if header.startswith(_RAR5_MAGIC):
        return 5
    if header.startswith(_RAR4_MAGIC):
        return 4
    raise ValueError(f"RAR signature mismatch for {archive.name}")


def _seven_zip_type_switches(archive: Path) -> list[str]:
    """Constrain native 7-Zip parser selection for each supported input.

    Plain 7z/RAR inputs can be pinned to one parser. Composite tar.zst and
    tar.lzma inputs need 7-Zip's recursive open behavior to reach the inner tar,
    so retain that mode but explicitly exclude XZ. This prevents a mismatched
    file from being routed into the vulnerable/unneeded XZ decoder while keeping
    the existing composite archive behavior intact.
    """
    suffix = _suffix(archive)
    if suffix in _7Z_EXTS:
        return ["-t7z"]
    if suffix in _RAR_EXTS:
        _rar_version(archive)
        # Do not force -trar here.  7-Zip 25.x rejects valid RAR5 archives when
        # forced through that legacy parser, while auto-detection correctly
        # identifies the already magic-validated input as Rar5.
        return []
    if suffix in _TAR_7Z_EXTS:
        return ["-t*:r", "-stxxz"]
    raise ValueError(f"Unsupported 7-Zip input format: {archive.name}")


def _list_7z(cmd: List[str], archive: Path, timeout: int = 300) -> Tuple[int, str]:
    """Run a 7-Zip technical listing, validating it AS IT IS EMITTED.

    Lines are read one bounded line at a time and handed straight to the
    listing validator, which keeps only what it needs; a listing over budget
    stops the tool at once. Only a listing the tool completed with status 0
    is ever accepted. Returns the status and a short output tail."""
    kwargs = {}
    if os.name == "posix":
        kwargs["preexec_fn"] = lambda: os.nice(10)
    tail: collections.deque[str] = collections.deque(maxlen=8)
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", **kwargs,
    )
    timer = threading.Timer(timeout, process.kill)

    def lines():
        while True:
            line = process.stdout.readline(MAX_LISTING_LINE_CHARS + 1)
            if not line:
                return
            tail.append(line[-200:])
            yield line

    refusal: ValueError | None = None
    timer.start()
    try:
        try:
            validate_7z_listing(archive, lines())
        except ExtractionBudgetExceeded:
            raise
        except ValueError as exc:
            # Decided by the tool's status: an unsafe listing it completed is
            # refused; a listing it failed is not authoritative either way.
            refusal = exc
            for _line in lines():
                pass
        rc = process.wait()
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        timer.cancel()
        process.stdout.close()
    if rc == 0 and refusal is not None:
        raise refusal
    return int(rc), "".join(tail)


def _preflight_7z(archive: Path, binary: str, candidates: list[str]) -> None:
    last_output = ""
    type_switches = _seven_zip_type_switches(archive)
    for pw in candidates:
        cmd = [binary, "l", "-slt", *type_switches]
        if pw:
            cmd.append(f"-p{pw}")
        cmd.append(str(archive))
        rc, output = _list_7z(cmd, archive, timeout=300)
        last_output = output
        if rc == 0:
            return
    raise RuntimeError(
        f"{binary} could not safely inspect {archive.name}: {last_output[-160:]}"
    )


def _extract_7z_to(archive: Path, dest: Path) -> None:
    """Use system 7z inside an already-isolated staging directory."""
    passwords = _get_extraction_passwords()
    candidates = [""] + passwords if passwords else [""]
    type_switches = _seven_zip_type_switches(archive)
    for binary in ("7z", "7za", "7zz"):
        if not _tool_available(binary):
            continue
        _preflight_7z(archive, binary, candidates)
        for pw in candidates:
            cmd = [
                binary, "x", "-mmt=1", *type_switches,
                str(archive), f"-o{dest}", "-y",
            ]
            if pw:
                cmd.insert(-1, f"-p{pw}")
            rc, _out = _run_tool(cmd, watch_dir=dest, watch_archive=archive)
            if rc == 0:
                return
        raise RuntimeError(f"{binary} failed to extract {archive.name}")
    raise RuntimeError("No 7z binary found (install 7zip in the container)")


def _extract_7z(archive: Path, dest: Path) -> None:
    staged_external_extract(
        archive, dest, lambda stage: _extract_7z_to(archive, stage)
    )


def _extract_rar_to(archive: Path, dest: Path) -> None:
    """Extract RAR into an isolated staging directory."""
    passwords = _get_extraction_passwords()
    candidates = [""] + passwords if passwords else [""]
    type_switches = _seven_zip_type_switches(archive)

    for binary in ("7z", "7za", "7zz"):
        if not _tool_available(binary):
            continue
        _preflight_7z(archive, binary, candidates)
        for pw in candidates:
            cmd = [
                binary, "x", "-mmt=1", *type_switches,
                str(archive), f"-o{dest}", "-y",
            ]
            if pw:
                cmd.insert(-1, f"-p{pw}")
            rc, _out = _run_tool(cmd, watch_dir=dest, watch_archive=archive)
            if rc == 0:
                return
        raise RuntimeError(f"{binary} failed to extract {archive.name}")

    raise RuntimeError("Safe RAR extraction requires a 7z-compatible binary")


def _extract_rar(archive: Path, dest: Path) -> None:
    staged_external_extract(
        archive, dest, lambda stage: _extract_rar_to(archive, stage)
    )


def _extract_into_directory(archive: Path, dest: Path) -> None:
    """Extract into an operation-owned directory."""
    dest.mkdir(parents=True, exist_ok=True)
    s = _suffix(archive)

    if s in _ZIP_EXTS:
        _extract_zip(archive, dest)
    elif s in _TAR_EXTS:
        _extract_tar(archive, dest)
    elif s in _TAR_7Z_EXTS:
        # tar.zst and tar.lzma: Python tarfile cannot decompress these natively;
        # route through 7z which handles them correctly.
        _extract_7z(archive, dest)
    elif s in _GZ_EXTS:
        # Could be a .gz that is NOT a tar — check magic bytes via tarfile
        if is_tar(archive):
            _extract_tar(archive, dest)
        else:
            _extract_gz_single(archive, dest)
    elif s in _BZ2_EXTS:
        if is_tar(archive):
            _extract_tar(archive, dest)
        else:
            _extract_bz2_single(archive, dest)
    elif s in _XZ_EXTS:
        if is_tar(archive):
            _extract_tar(archive, dest)
        else:
            _extract_xz_single(archive, dest)
    elif s in _7Z_EXTS:
        _extract_7z(archive, dest)
    elif s in _RAR_EXTS:
        _extract_rar(archive, dest)
    else:
        raise ValueError(f"Unsupported archive format: {archive.name}")


def _extract_sync(archive: Path, dest: Path) -> list[Path]:
    """Transactionally extract every supported format into the live destination."""
    return staged_external_extract(
        archive,
        dest,
        lambda stage: _extract_into_directory(archive, stage),
    )


def _extract_job_sync(
    archive: Path,
    dest: Path,
    *,
    extract_into,
    extract_child,
    delete_nested: bool,
) -> list[Path]:
    """One extraction job: ``archive`` and the nested archives it contains,
    under ONE budget measured against the original input, committed ONCE.

    Nested archives are extracted inside the job's own staging tree before
    anything is committed, each under what the job has left, so no nested
    archive can write past the job's budget and a budget violation anywhere
    commits nothing at all. An ordinary failure of one nested archive (a
    corrupt or password-protected inner archive) is logged and leaves it as a
    file, as before; a budget violation is the job's failure, never hidden by
    a success."""

    def run(stage: Path) -> None:
        extract_into(archive, stage)
        nested_archives = sorted(
            (
                candidate
                for candidate in stage.rglob("*")
                if len(candidate.relative_to(stage).parts) > 1
                and not candidate.is_symlink()
                and candidate.is_file()
                and is_archive(candidate)
            ),
            key=lambda candidate: (len(candidate.relative_to(stage).parts), str(candidate)),
        )
        if not nested_archives:
            return
        logger.info("Found %d nested archive(s) inside %s", len(nested_archives), archive.name)
        configured = configured_limits()
        job_bytes = job_byte_limit(archive)
        for nested in nested_archives[:10]:
            used_files, used_bytes, used_directories = validate_extracted_tree(stage, archive)
            remaining = ExtractionLimits(
                max_files=configured.max_files - used_files,
                max_bytes=job_bytes - used_bytes,
                max_ratio=configured.max_ratio,
                max_directories=configured.max_directories - used_directories,
            )
            try:
                with job_limits(remaining):
                    extract_child(nested, nested.parent)
            except ExtractionBudgetExceeded:
                raise
            except Exception as nested_error:
                logger.warning("Nested extraction failed for %s: %s", nested, nested_error)
                continue
            if delete_nested:
                try:
                    nested.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    logger.warning("Nested archive cleanup failed for %s: %s", nested, cleanup_error)

    return staged_external_extract(archive, dest, run)


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------

class Extractor:
    """Async extraction service with concurrency limit."""

    def __init__(self, max_concurrent: int = 1) -> None:
        self._max_concurrent = max(1, int(max_concurrent or 1))
        self._sem = asyncio.Semaphore(self._max_concurrent)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_concurrent,
            thread_name_prefix="extract",
        )

    def update_max_concurrent(self, n: int) -> None:
        new_limit = max(1, int(n or 1))
        if new_limit == self._max_concurrent:
            return
        old_executor = self._executor
        self._max_concurrent = new_limit
        self._sem = asyncio.Semaphore(new_limit)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=new_limit,
            thread_name_prefix="extract",
        )
        old_executor.shutdown(wait=False, cancel_futures=False)

    async def extract_archive(
        self,
        archive: Path,
        dest: Path,
        *,
        delete_after: bool = True,
        created: Optional[List[Path]] = None,
    ) -> Tuple[bool, str]:
        """
        Extract *archive* into *dest* (async, respects concurrency semaphore).

        Supports:
          - Nested archives (archives inside extracted archives, up to 2 levels)
          - Retry on transient failures (1 retry by default)

        Returns (success, message).
        """
        async with self._sem:
            loop = asyncio.get_running_loop()  # get_event_loop() is deprecated in 3.10+
            retries = 1
            last_err: str = ""
            for attempt in range(retries + 1):
                try:
                    if attempt > 0:
                        logger.info("Retrying extraction of %s (attempt %d)", archive, attempt + 1)
                    logger.info("Extracting %s → %s", archive, dest)
                    # Nested extraction is provenance-bound and job-bound: only
                    # archives this job itself extracted, inside its own staging,
                    # under its one budget (``_extract_job_sync``).
                    created_files = await loop.run_in_executor(
                        self._executor,
                        functools.partial(
                            _extract_job_sync, archive, dest,
                            extract_into=_extract_into_directory, extract_child=_extract_sync,
                            delete_nested=delete_after,
                        ),
                    )
                    if created is not None:
                        # Outputs: every file this operation committed plus every
                        # existing byte-identical file it found already in place
                        # (``_merge_staged_tree``) -- the job's whole tree, never
                        # authority to remove anything.
                        created.extend(Path(item) for item in created_files)
                    if delete_after and archive.exists():
                        archive.unlink()
                        logger.debug("Deleted archive: %s", archive)
                    return True, f"Extracted {archive.name}"
                except Exception as exc:
                    last_err = f"Extraction failed for {archive.name}: {exc}"
                    logger.warning(last_err)
            logger.error(last_err)
            return False, last_err

    async def extract_folder(
        self,
        folder: Path,
        *,
        delete_after: bool = True,
    ) -> List[Tuple[Path, bool, str]]:
        """
        Find and extract all archives inside *folder*.

        Each archive is extracted into its parent directory (= the torrent
        download folder). Extractions run concurrently up to *max_concurrent*.
        """
        loop = asyncio.get_running_loop()
        archives = await loop.run_in_executor(None, find_archives, folder)
        if not archives:
            logger.debug("No archives found in %s", folder)
            return []

        # Create real Tasks so they run concurrently and the semaphore has effect.
        # Previously coroutines were awaited serially, making max_concurrent useless.
        tasks = [
            asyncio.create_task(
                self.extract_archive(archive, archive.parent, delete_after=delete_after)
            )
            for archive in archives
        ]
        results_raw = await asyncio.gather(*tasks, return_exceptions=True)

        results: List[Tuple[Path, bool, str]] = []
        for archive, raw in zip(archives, results_raw):
            if isinstance(raw, Exception):
                results.append((archive, False, f"Extraction failed for {archive.name}: {raw}"))
            else:
                ok, msg = raw
                results.append((archive, ok, msg))
        return results

    async def extract_archives(
        self,
        archives: Iterable[Path],
        *,
        delete_after: bool = True,
        created: Optional[List[Path]] = None,
    ) -> List[Tuple[Path, bool, str]]:
        """Extract a known archive list without walking the filesystem.
        ``created``, when given, collects every output of each extraction:
        what it committed and what it found already in place, byte-identical."""
        unique_archives = archive_paths_from_downloads(archives)
        if not unique_archives:
            return []
        scheduled: List[Path] = []
        results: List[Tuple[Path, bool, str]] = []
        for archive in unique_archives:
            if archive.exists():
                scheduled.append(archive)
            else:
                results.append(
                    (
                        archive,
                        False,
                        f"Archive path is not accessible to DebridPulse: {archive}",
                    )
                )

        tasks = [
            asyncio.create_task(
                self.extract_archive(archive, archive.parent, delete_after=delete_after, created=created)
            )
            for archive in scheduled
        ]
        if tasks:
            results_raw = await asyncio.gather(*tasks, return_exceptions=True)
            for archive, raw in zip(scheduled, results_raw):
                if isinstance(raw, Exception):
                    results.append((archive, False, f"Extraction failed for {archive.name}: {raw}"))
                else:
                    ok, msg = raw
                    results.append((archive, ok, msg))
        return results


# Module-level singleton — replaced by manager on startup
_extractor: Optional[Extractor] = None


def get_extractor() -> Extractor:
    global _extractor
    if _extractor is None:
        _extractor = Extractor(max_concurrent=1)
    return _extractor


def init_extractor(max_concurrent: int) -> Extractor:
    global _extractor
    _extractor = Extractor(max_concurrent=max_concurrent)
    return _extractor
