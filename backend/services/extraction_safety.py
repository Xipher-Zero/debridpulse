"""Archive extraction safety primitives.

Quotas are deliberately generous defaults intended to stop pathological
archives rather than constrain normal media extraction. External extractors
write into an isolated sibling staging directory; validated regular files are
then merged into the requested destination.
"""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterable

from core.config import get_settings


@dataclass(frozen=True)
class ExtractionLimits:
    max_files: int
    max_bytes: int
    max_ratio: float


def limits() -> ExtractionLimits:
    cfg = get_settings()
    max_files = max(1, int(getattr(cfg, "extract_max_files", 20_000) or 20_000))
    max_gb = max(0.001, float(getattr(cfg, "extract_max_expanded_gb", 250.0) or 250.0))
    max_ratio = max(1.0, float(getattr(cfg, "extract_max_compression_ratio", 1000.0) or 1000.0))
    return ExtractionLimits(
        max_files=max_files,
        max_bytes=max(1, int(max_gb * 1024 * 1024 * 1024)),
        max_ratio=max_ratio,
    )


def _archive_size(archive: Path) -> int:
    try:
        return max(1, int(archive.stat().st_size))
    except OSError:
        return 1


def validate_budget(*, archive: Path, file_count: int, expanded_bytes: int) -> None:
    cfg = limits()
    file_count = max(0, int(file_count))
    expanded_bytes = max(0, int(expanded_bytes))
    if file_count > cfg.max_files:
        raise ValueError(
            f"Archive contains {file_count} files; limit is {cfg.max_files}"
        )
    if expanded_bytes > cfg.max_bytes:
        raise ValueError(
            f"Archive expands to {expanded_bytes} bytes; limit is {cfg.max_bytes}"
        )
    ratio = expanded_bytes / _archive_size(archive)
    if expanded_bytes and ratio > cfg.max_ratio:
        raise ValueError(
            f"Archive expansion ratio {ratio:.1f}:1 exceeds limit {cfg.max_ratio:.1f}:1"
        )


def validate_member_name(name: str) -> tuple[str, ...]:
    normalized = str(name or "").replace("\\", "/")
    relative = PurePosixPath(normalized)
    parts = tuple(part for part in relative.parts if part not in {"", "."})
    if (
        not parts
        or relative.is_absolute()
        or ".." in parts
        or ":" in parts[0]
    ):
        raise ValueError(f"Unsafe archive member path: {name!r}")
    return parts


def validate_zip_members(archive: Path, members: Iterable[object]) -> None:
    member_list = list(members)
    file_count = 0
    expanded = 0
    for member in member_list:
        name = str(getattr(member, "filename", "") or "")
        validate_member_name(name)
        unix_mode = int(getattr(member, "external_attr", 0) or 0) >> 16
        if stat.S_ISLNK(unix_mode):
            raise ValueError(f"Unsafe ZIP symlink member: {name!r}")
        is_dir = bool(getattr(member, "is_dir")())
        if not is_dir:
            file_count += 1
            expanded += max(0, int(getattr(member, "file_size", 0) or 0))
    validate_budget(archive=archive, file_count=file_count, expanded_bytes=expanded)


def validate_tar_members(archive: Path, members: Iterable[object]) -> None:
    member_list = list(members)
    file_count = 0
    expanded = 0
    for member in member_list:
        name = str(getattr(member, "name", "") or "")
        validate_member_name(name)
        if any(
            bool(getattr(member, method)())
            for method in ("issym", "islnk", "isdev", "isfifo")
            if hasattr(member, method)
        ):
            raise ValueError(f"Unsafe TAR special/link member: {name!r}")
        if bool(getattr(member, "isfile")()):
            file_count += 1
            expanded += max(0, int(getattr(member, "size", 0) or 0))
    validate_budget(archive=archive, file_count=file_count, expanded_bytes=expanded)


def copy_limited(source: BinaryIO, output: BinaryIO, *, archive: Path) -> int:
    cfg = limits()
    ratio_limit = max(1, int(_archive_size(archive) * cfg.max_ratio))
    byte_limit = min(cfg.max_bytes, ratio_limit)
    written = 0
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            break
        written += len(chunk)
        if written > byte_limit:
            raise ValueError(
                f"Archive expansion exceeded safe limit after {written} bytes"
            )
        output.write(chunk)
    validate_budget(archive=archive, file_count=1, expanded_bytes=written)
    return written


def validate_7z_listing(archive: Path, output: str) -> None:
    """Validate `7z l -slt` member metadata before external extraction."""
    text = str(output or "").replace("\r\n", "\n")
    if "----------" not in text:
        raise ValueError("Archive listing did not contain member metadata")
    records = text.split("----------", 1)[1].strip().split("\n\n")
    file_count = 0
    expanded = 0
    for record in records:
        fields: dict[str, str] = {}
        for line in record.splitlines():
            if " = " in line:
                key, value = line.split(" = ", 1)
                fields[key.strip()] = value.strip()
        name = fields.get("Path", "")
        if not name:
            continue
        validate_member_name(name)
        attrs = fields.get("Attributes", "")
        is_dir = attrs.startswith("D")
        if not is_dir:
            try:
                size = max(0, int(fields.get("Size", "0") or 0))
            except ValueError as exc:
                raise ValueError(f"Invalid archive member size for {name!r}") from exc
            file_count += 1
            expanded += size
    validate_budget(archive=archive, file_count=file_count, expanded_bytes=expanded)


def validate_staging_tree(root: Path, archive: Path) -> None:
    """Validate a live external-extractor staging tree.

    The scan is intentionally race-tolerant: files may disappear between
    rglob/lstat/stat while the extractor is active. Unsafe links/special files
    and budget overruns are still rejected as soon as a stable observation
    sees them.
    """
    root = root.resolve()
    file_count = 0
    expanded = 0
    for current in root.rglob("*"):
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"Extracted archive created a symlink: {current.name!r}")
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(f"Extracted archive created a special file: {current.name!r}")
        try:
            size = max(0, int(current.stat().st_size))
        except FileNotFoundError:
            continue
        file_count += 1
        expanded += size
    validate_budget(archive=archive, file_count=file_count, expanded_bytes=expanded)


def validate_extracted_tree(root: Path, archive: Path) -> None:
    root = root.resolve()
    file_count = 0
    expanded = 0
    for current in root.rglob("*"):
        if current.is_symlink():
            raise ValueError(f"Extracted archive created a symlink: {current.name!r}")
        resolved = current.resolve(strict=False)
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"Extracted path escapes staging directory: {current!s}")
        mode = current.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(f"Extracted archive created a special file: {current.name!r}")
        if current.lstat().st_nlink > 1:
            raise ValueError(f"Extracted archive created a hard link: {current.name!r}")
        file_count += 1
        expanded += max(0, int(current.stat().st_size))
    validate_budget(archive=archive, file_count=file_count, expanded_bytes=expanded)


def _ensure_safe_destination(root: Path, target: Path) -> None:
    root = root.resolve()
    resolved = target.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"Extraction target escapes destination: {target!s}")
    current = root
    for part in target.relative_to(root).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Extraction target traverses symlink: {current!s}")


def _merge_staged_tree(stage: Path, dest: Path) -> list[Path]:
    """Commit one validated staged tree without overwriting existing files."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    entries = sorted(
        stage.rglob("*"),
        key=lambda p: (len(p.relative_to(stage).parts), 0 if p.is_dir() else 1),
    )
    directory_targets: list[Path] = []
    file_moves: list[tuple[Path, Path]] = []

    # Preflight the entire commit before moving a single file. Existing
    # directories may be shared, but any existing file target is outside this
    # extraction operation's ownership and therefore makes the commit fail.
    for source in entries:
        relative = source.relative_to(stage)
        target = root.joinpath(*relative.parts)
        _ensure_safe_destination(root, target)
        if source.is_dir():
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise ValueError(f"Extraction directory collides with unsafe target: {target!s}")
            directory_targets.append(target)
            continue
        if target.exists() or target.is_symlink():
            raise ValueError(f"Extraction would overwrite existing file: {target!s}")
        file_moves.append((source, target))

    created_dirs: list[Path] = []
    committed: list[Path] = []
    try:
        for target in directory_targets:
            if target.exists():
                continue
            try:
                # Directory targets are depth-sorted, so their parent has
                # already been handled. If another extraction creates the same
                # benign directory concurrently, accept that race only after
                # re-validating that it is still a real directory.
                target.mkdir(exist_ok=False)
                created_dirs.append(target)
            except FileExistsError:
                if target.is_symlink() or not target.is_dir():
                    raise
        for source, target in file_moves:
            _ensure_safe_destination(root, target)
            # Staging is deliberately created under the destination, so source
            # and target share a filesystem. link() provides an atomic
            # no-clobber commit: if another extraction creates target after the
            # preflight, FileExistsError wins instead of overwriting its data.
            # The staging hard link is removed by the enclosing rmtree only
            # after the entire merge has succeeded.
            os.link(source, target, follow_symlinks=False)
            committed.append(target)
    except Exception:
        for target in reversed(committed):
            target.unlink(missing_ok=True)
        for directory in sorted(created_dirs, key=lambda p: len(p.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    return committed


def staged_external_extract(
    archive: Path,
    dest: Path,
    runner: Callable[[Path], None],
) -> list[Path]:
    """Run an extractor in isolated staging, validate, then commit safely."""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=".debridpulse-extract-",
            dir=str(dest),
        )
    )
    try:
        runner(stage)
        validate_extracted_tree(stage, archive)
        return _merge_staged_tree(stage, dest)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
