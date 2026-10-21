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
import tarfile
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterable, Iterator

from core.config import get_settings

# The longest path the filesystem can create (Linux ``PATH_MAX``): a member
# name past it can never be materialized, so it is refused before anything is.
MAX_MEMBER_NAME_BYTES = 4096
# One TAR metadata record (a PAX extended header or a GNU long name/link)
# holds a path, a link target and a few attributes. Python's tarfile reads a
# record whole into memory before any member is seen, so a record is refused
# past this size, and the records of one archive past this many bytes per
# admissible entry.
MAX_TAR_METADATA_RECORD_BYTES = 1024 * 1024
TAR_METADATA_BYTES_PER_ENTRY = 8 * 1024
# One line of a 7-Zip technical listing: ``Path = `` and a longest path.
MAX_LISTING_LINE_CHARS = MAX_MEMBER_NAME_BYTES + 64


class ExtractionBudgetExceeded(ValueError):
    """A resource budget refusal: too many entries, too many bytes, too much
    amplification or too much metadata. Within one extraction job it is a
    job-wide safety violation, never an ordinary per-archive parse failure."""


@dataclass(frozen=True)
class ExtractionLimits:
    max_files: int
    max_bytes: int
    max_ratio: float
    # Directories are bounded by the same configured count as files, counted
    # separately: never as a file, and never as bytes.
    max_directories: int


# The budget still available to the extraction job running in this thread,
# set only by ``job_limits`` for a nested archive the job extracts.
_JOB_LIMITS: ContextVar[ExtractionLimits | None] = ContextVar("extraction_job_limits", default=None)


@contextmanager
def job_limits(remaining: ExtractionLimits) -> Iterator[None]:
    """Run one step of an extraction job under the job's REMAINING budget, so
    every check of that step -- listing, metadata, streamed bytes, live
    staging and the staged tree -- counts against what the job has left."""
    token = _JOB_LIMITS.set(remaining)
    try:
        yield
    finally:
        _JOB_LIMITS.reset(token)


def configured_limits() -> ExtractionLimits:
    cfg = get_settings()
    max_files = max(1, int(getattr(cfg, "extract_max_files", 20_000) or 20_000))
    max_gb = max(0.001, float(getattr(cfg, "extract_max_expanded_gb", 250.0) or 250.0))
    max_ratio = max(1.0, float(getattr(cfg, "extract_max_compression_ratio", 1000.0) or 1000.0))
    return ExtractionLimits(
        max_files=max_files,
        max_bytes=max(1, int(max_gb * 1024 * 1024 * 1024)),
        max_ratio=max_ratio,
        max_directories=max_files,
    )


def limits() -> ExtractionLimits:
    """The budget that applies here: the running job's remaining budget, or
    the configured one."""
    return _JOB_LIMITS.get() or configured_limits()


def job_byte_limit(archive: Path) -> int:
    """The most one extraction job may write in total for ``archive``: the
    configured expanded-size limit, or its amplification limit measured
    against the ORIGINAL input, whichever is smaller."""
    cfg = configured_limits()
    return min(cfg.max_bytes, max(1, int(_archive_size(archive) * cfg.max_ratio)))


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
        raise ExtractionBudgetExceeded(
            f"Archive contains {file_count} files; limit is {cfg.max_files}"
        )
    if expanded_bytes > cfg.max_bytes:
        raise ExtractionBudgetExceeded(
            f"Archive expands to {expanded_bytes} bytes; limit is {cfg.max_bytes}"
        )
    ratio = expanded_bytes / _archive_size(archive)
    if expanded_bytes and ratio > cfg.max_ratio:
        raise ExtractionBudgetExceeded(
            f"Archive expansion ratio {ratio:.1f}:1 exceeds limit {cfg.max_ratio:.1f}:1"
        )


def _validate_other_entries(count: int) -> None:
    """Entries that are not files (directories and the like) are bounded by
    the same configured count, separately from files, so a listing can never
    grow without limit."""
    cfg = limits()
    if count > cfg.max_directories:
        raise ExtractionBudgetExceeded(
            f"Archive contains more than {cfg.max_directories} non-file entries; limit is {cfg.max_directories}"
        )


def _validate_directories(count: int) -> None:
    cfg = limits()
    if count > cfg.max_directories:
        raise ExtractionBudgetExceeded(
            f"Archive creates more than {cfg.max_directories} directories; limit is {cfg.max_directories}"
        )


class _ArchiveDirectories:
    """The distinct directories an archive's members would create: every
    directory entry and every directory a member's path implies, counted as
    they are found and refused at the first one past the budget. Ancestors are
    walked from the deepest and stop at one already counted, so a member
    costs at most its own new directories."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def member(self, parts: tuple[str, ...], *, is_dir: bool) -> None:
        for end in range(len(parts) if is_dir else len(parts) - 1, 0, -1):
            directory = "/".join(parts[:end])
            if directory in self._seen:
                return
            self._seen.add(directory)
            _validate_directories(len(self._seen))


def validate_member_name(name: str) -> tuple[str, ...]:
    if len(str(name or "").encode("utf-8", "surrogateescape")) > MAX_MEMBER_NAME_BYTES:
        raise ValueError("Unsafe archive member path: longer than the filesystem allows")
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
    directories = _ArchiveDirectories()
    for member in member_list:
        name = str(getattr(member, "filename", "") or "")
        parts = validate_member_name(name)
        unix_mode = int(getattr(member, "external_attr", 0) or 0) >> 16
        if stat.S_ISLNK(unix_mode):
            raise ValueError(f"Unsafe ZIP symlink member: {name!r}")
        is_dir = bool(getattr(member, "is_dir")())
        directories.member(parts, is_dir=is_dir)
        if not is_dir:
            file_count += 1
            expanded += max(0, int(getattr(member, "file_size", 0) or 0))
    validate_budget(archive=archive, file_count=file_count, expanded_bytes=expanded)


def validate_tar_members(archive: Path, members: Iterable[object]) -> list:
    """Validate TAR members AS THEY ARE READ and return them. Pass the open
    archive itself: each header is checked when it is read, and an archive
    over budget is refused at the first member past it, never after its
    whole member list has been loaded."""
    cfg = limits()
    accepted = []
    file_count = 0
    other_count = 0
    expanded = 0
    directories = _ArchiveDirectories()
    for member in members:
        name = str(getattr(member, "name", "") or "")
        parts = validate_member_name(name)
        if any(
            bool(getattr(member, method)())
            for method in ("issym", "islnk", "isdev", "isfifo")
            if hasattr(member, method)
        ):
            raise ValueError(f"Unsafe TAR special/link member: {name!r}")
        directories.member(parts, is_dir=bool(getattr(member, "isdir")()))
        if bool(getattr(member, "isfile")()):
            file_count += 1
            expanded += max(0, int(getattr(member, "size", 0) or 0))
            if file_count > cfg.max_files or expanded > cfg.max_bytes:
                validate_budget(archive=archive, file_count=file_count, expanded_bytes=expanded)
        else:
            other_count += 1
            _validate_other_entries(other_count)
        accepted.append(member)
    validate_budget(archive=archive, file_count=file_count, expanded_bytes=expanded)
    return accepted


class _BoundedTarInfo(tarfile.TarInfo):
    """A TAR header whose metadata records are size-checked before tarfile
    reads them into memory (through tarfile's own ``tarinfo`` hook)."""

    def _proc_member(self, archive):
        if self.type in (tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK,
                         tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE):
            archive.metadata_bytes += self.size
            if self.size > MAX_TAR_METADATA_RECORD_BYTES or archive.metadata_bytes > archive.metadata_limit:
                raise ExtractionBudgetExceeded("Archive metadata exceeds the safe limit")
        return super()._proc_member(archive)


class _BoundedTarFile(tarfile.TarFile):
    tarinfo = _BoundedTarInfo
    metadata_bytes = 0

    @property
    def metadata_limit(self) -> int:
        return 2 * limits().max_files * TAR_METADATA_BYTES_PER_ENTRY


def open_tar(path: Path, mode: str = "r:*") -> tarfile.TarFile:
    """Open a TAR archive for reading with bounded header metadata."""
    return _BoundedTarFile.open(path, mode)


def is_tar(path: Path) -> bool:
    """``tarfile.is_tarfile`` through the bounded reader: its probe reads the
    first header, metadata records included. A metadata refusal propagates."""
    try:
        with open_tar(path):
            return True
    except tarfile.TarError:
        return False


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
            raise ExtractionBudgetExceeded(
                f"Archive expansion exceeded safe limit after {written} bytes"
            )
        output.write(chunk)
    validate_budget(archive=archive, file_count=1, expanded_bytes=written)
    return written


_LISTING_FIELDS = frozenset({"Path", "Attributes", "Size"})


def validate_7z_listing(archive: Path, output: str | Iterable[str]) -> None:
    """Validate `7z l -slt` member metadata before external extraction.

    ``output`` is the listing's lines AS THEY ARE EMITTED (or the whole text).
    Each line is bounded, each record keeps only the fields it needs, and the
    member counts and bytes are checked as records complete, so an oversized
    listing is refused at the first line or record past a limit -- never
    after the whole of it has been held."""
    lines = output.splitlines(keepends=True) if isinstance(output, str) else output
    cfg = limits()
    started = False
    file_count = 0
    other_count = 0
    expanded = 0
    fields: dict[str, str] = {}
    directories = _ArchiveDirectories()

    def finish(record: dict[str, str]) -> None:
        nonlocal file_count, other_count, expanded
        name = record.get("Path", "")
        if not name:
            return
        parts = validate_member_name(name)
        directories.member(parts, is_dir=record.get("Attributes", "").startswith("D"))
        if record.get("Attributes", "").startswith("D"):
            other_count += 1
            _validate_other_entries(other_count)
            return
        try:
            size = max(0, int(record.get("Size", "0") or 0))
        except ValueError as exc:
            raise ValueError(f"Invalid archive member size for {name!r}") from exc
        file_count += 1
        expanded += size
        if file_count > cfg.max_files or expanded > cfg.max_bytes:
            validate_budget(archive=archive, file_count=file_count, expanded_bytes=expanded)

    for raw in lines:
        if len(raw) > MAX_LISTING_LINE_CHARS:
            raise ExtractionBudgetExceeded("Archive listing line exceeds the safe limit")
        line = raw.rstrip("\r\n")
        if not started:
            started = line == "----------"
            continue
        if not line.strip():
            finish(fields)
            fields = {}
        elif " = " in line:
            key, value = line.split(" = ", 1)
            if key.strip() in _LISTING_FIELDS:
                fields[key.strip()] = value.strip()
    if not started:
        raise ValueError("Archive listing did not contain member metadata")
    finish(fields)
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
    directories = 0
    for current in root.rglob("*"):
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"Extracted archive created a symlink: {current.name!r}")
        if stat.S_ISDIR(mode):
            directories += 1
            _validate_directories(directories)
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


def validate_extracted_tree(root: Path, archive: Path) -> tuple[int, int, int]:
    """Validate a staged tree and return its files, bytes and directories."""
    root = root.resolve()
    file_count = 0
    expanded = 0
    directories = 0
    for current in root.rglob("*"):
        if current.is_symlink():
            raise ValueError(f"Extracted archive created a symlink: {current.name!r}")
        resolved = current.resolve(strict=False)
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"Extracted path escapes staging directory: {current!s}")
        mode = current.lstat().st_mode
        if stat.S_ISDIR(mode):
            directories += 1
            _validate_directories(directories)
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(f"Extracted archive created a special file: {current.name!r}")
        if current.lstat().st_nlink > 1:
            raise ValueError(f"Extracted archive created a hard link: {current.name!r}")
        file_count += 1
        expanded += max(0, int(current.stat().st_size))
    validate_budget(archive=archive, file_count=file_count, expanded_bytes=expanded)
    return file_count, expanded, directories


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


_COMPARE_CHUNK = 1024 * 1024


def _already_satisfied(source: Path, target: Path) -> bool:
    """Whether an existing ``target`` already IS the staged ``source``: a
    regular file (never a symlink or special file, observed without following
    links) of the same size whose every byte is identical.

    That is the one existing target a commit may accept without writing:
    re-extracting delivered archives over their own earlier outputs (an
    operator Retry of a completed transfer, a later file selection) finds
    exactly the bytes it would have written, so the material is already in
    place and nothing about it changes -- it is neither relinked nor
    rewritten. Anything else (different bytes, a truncated or partial file, a
    link, a device, a FIFO) is never accepted as this extraction's output.
    Hard-linked targets (``st_nlink > 1``, e.g. an *arr import) are compared
    like any other regular file: their bytes, not their link count, decide."""
    try:
        expected = source.lstat()
        # Never open a link or special file; the descriptor is re-checked
        # below, so a target swapped after this observation still fails.
        if not stat.S_ISREG(target.lstat().st_mode):
            return False
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return False
    with os.fdopen(fd, "rb") as existing:
        observed = os.fstat(existing.fileno())
        if not stat.S_ISREG(observed.st_mode) or observed.st_size != expected.st_size:
            return False
        with source.open("rb") as staged:
            while True:
                left = staged.read(_COMPARE_CHUNK)
                right = existing.read(_COMPARE_CHUNK)
                if left != right:
                    return False
                if not left:
                    return True


def _merge_staged_tree(stage: Path, dest: Path) -> list[Path]:
    """Commit one validated staged tree without overwriting existing files.

    Returns the tree's outputs in ``dest``: every file this operation
    committed, then every existing target it found already satisfied
    (``_already_satisfied``: byte-identical regular file). Satisfied files are
    outputs -- a consumer of the result (the recorded final file set, an *arr
    import) must see the whole tree, not only what happened to be missing --
    but never this operation's writes: a rollback removes only what it
    committed, and a satisfied file is never touched."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    entries = sorted(
        stage.rglob("*"),
        key=lambda p: (len(p.relative_to(stage).parts), 0 if p.is_dir() else 1),
    )
    directory_targets: list[Path] = []
    file_moves: list[tuple[Path, Path]] = []
    satisfied: list[Path] = []

    # Preflight the entire commit before moving a single file. Existing
    # directories may be shared, but any existing file target is outside this
    # extraction operation's ownership and therefore makes the commit fail --
    # unless it already holds exactly the staged bytes, which needs no write.
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
            if _already_satisfied(source, target):
                satisfied.append(target)
                continue
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
        # Only this operation's own links; a satisfied file was never written.
        for target in reversed(committed):
            target.unlink(missing_ok=True)
        for directory in sorted(created_dirs, key=lambda p: len(p.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    return [*committed, *satisfied]


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
