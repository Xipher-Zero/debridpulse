"""Release-hardened extraction boundary for composite TAR archives.

7-Zip remains the constrained parser for validated 7z/RAR inputs, but composite
``.tar.zst``/``.tzst``/``.tar.lzma`` inputs are decompressed by their exact
outer codec and then passed to Python's validated TAR path.  This prevents a
mislabeled composite archive from reaching 7-Zip's broad recursive format
probe before DebridPulse has established the intended container type.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import lzma
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from services.extraction_safety import (
    copy_limited,
    staged_external_extract,
    validate_tar_members,
)
from services.extractor import Extractor, _extract_sync, _suffix, is_archive

logger = logging.getLogger("debridpulse.extractor_secure")

_COMPOSITE_TAR_EXTS = frozenset({".tar.zst", ".tzst", ".tar.lzma"})


def _extract_validated_tar(
    tar_path: Path,
    dest: Path,
    *,
    budget_archive: Path,
) -> None:
    with tarfile.open(tar_path, "r:") as archive:
        members = archive.getmembers()
        validate_tar_members(budget_archive, members)
        archive.extractall(dest, members=members, filter="data")


def _decompress_lzma(archive: Path, output: Path) -> None:
    with lzma.open(archive, "rb") as source, output.open("wb") as target:
        copy_limited(source, target, archive=archive)


def _decompress_zstd(archive: Path, output: Path) -> None:
    binary = shutil.which("zstd")
    if not binary:
        raise RuntimeError("Safe .tar.zst extraction requires the zstd decoder")

    with tempfile.TemporaryFile() as stderr, output.open("wb") as target:
        process = subprocess.Popen(
            [binary, "-d", "-c", "--no-progress", "--", str(archive)],
            stdout=subprocess.PIPE,
            stderr=stderr,
        )
        try:
            if process.stdout is None:
                raise RuntimeError("zstd did not expose a decompression stream")
            copy_limited(process.stdout, target, archive=archive)
            process.stdout.close()
            rc = process.wait(timeout=300)
        except BaseException:
            process.kill()
            try:
                process.wait(timeout=5)
            except Exception:
                pass
            raise

        if rc != 0:
            stderr.seek(0)
            detail = stderr.read(4096).decode("utf-8", errors="replace").strip()
            detail = " ".join(detail.split())[-320:]
            raise RuntimeError(
                f"zstd could not decompress {archive.name}"
                + (f": {detail}" if detail else "")
            )


def _extract_composite_into_stage(archive: Path, stage: Path) -> None:
    intermediate = stage / ".debridpulse-composite.tar"
    try:
        suffix = _suffix(archive)
        if suffix in {".tar.zst", ".tzst"}:
            _decompress_zstd(archive, intermediate)
        elif suffix == ".tar.lzma":
            _decompress_lzma(archive, intermediate)
        else:
            raise ValueError(f"Unsupported composite archive format: {archive.name}")
        _extract_validated_tar(intermediate, stage, budget_archive=archive)
    finally:
        intermediate.unlink(missing_ok=True)


def _extract_secure_sync(archive: Path, dest: Path) -> list[Path]:
    if _suffix(archive) not in _COMPOSITE_TAR_EXTS:
        return _extract_sync(archive, dest)
    return staged_external_extract(
        archive,
        dest,
        lambda stage: _extract_composite_into_stage(archive, stage),
    )


class SecureExtractor(Extractor):
    """Extractor that keeps nested composite archives on the pinned parser path."""

    async def extract_archive(
        self,
        archive: Path,
        dest: Path,
        *,
        delete_after: bool = True,
    ) -> Tuple[bool, str]:
        async with self._sem:
            loop = asyncio.get_running_loop()
            retries = 1
            last_err = ""
            for attempt in range(retries + 1):
                try:
                    if attempt > 0:
                        logger.info(
                            "Retrying extraction of %s (attempt %d)",
                            archive,
                            attempt + 1,
                        )
                    logger.info("Extracting %s → %s", archive, dest)
                    created_files = await loop.run_in_executor(
                        self._executor,
                        _extract_secure_sync,
                        archive,
                        dest,
                    )

                    try:
                        dest_root = dest.resolve()
                        nested_archives = []
                        for created in created_files:
                            candidate = Path(created)
                            try:
                                relative = candidate.resolve().relative_to(dest_root)
                            except (OSError, ValueError):
                                continue
                            if len(relative.parts) > 1 and is_archive(candidate):
                                nested_archives.append(candidate)
                        if nested_archives:
                            logger.info(
                                "Found %d nested archive(s) inside %s",
                                len(nested_archives),
                                archive.name,
                            )
                            for nested in nested_archives[:10]:
                                try:
                                    await loop.run_in_executor(
                                        self._executor,
                                        _extract_secure_sync,
                                        nested,
                                        nested.parent,
                                    )
                                except Exception as nested_error:
                                    logger.warning(
                                        "Nested extraction failed for %s: %s",
                                        nested,
                                        nested_error,
                                    )
                                    continue

                                if delete_after:
                                    try:
                                        nested.unlink(missing_ok=True)
                                        logger.debug("Removed nested archive %s", nested)
                                    except OSError as cleanup_error:
                                        logger.warning(
                                            "Nested archive cleanup failed for %s: %s",
                                            nested,
                                            cleanup_error,
                                        )
                    except Exception as scan_error:
                        logger.debug("Nested archive scan failed: %s", scan_error)

                    # Extraction success and source cleanup are separate truths.
                    # A cleanup failure must not relabel already-materialized data
                    # as an extraction failure; ExtractionService owns the durable
                    # transfer-level cleanup audit and retries DB-known source paths.
                    if delete_after and archive.exists():
                        try:
                            archive.unlink()
                            logger.debug("Deleted archive: %s", archive)
                        except OSError as cleanup_error:
                            logger.warning(
                                "Archive cleanup deferred for %s: %s",
                                archive,
                                cleanup_error,
                            )
                    return True, f"Extracted {archive.name}"
                except Exception as exc:
                    last_err = f"Extraction failed for {archive.name}: {exc}"
                    logger.warning(last_err)

            logger.error(last_err)
            return False, last_err


_extractor: Optional[SecureExtractor] = None


def get_secure_extractor() -> SecureExtractor:
    global _extractor
    if _extractor is None:
        _extractor = SecureExtractor(max_concurrent=1)
    return _extractor
