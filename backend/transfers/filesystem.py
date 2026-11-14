"""Destination and local-possession policy independent of transfer mechanisms.

This module is the ONE materialization verification and cleanup owner. A FILE
plan is verified by the hardened single-file path below; a COLLECTION plan by
the collection branch of the same owner. Nothing here knows which executor
produced the material. It also owns the read-only observation of material
(``observe_path``/``observe_material``), which reports what is on disk without
touching it.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import errno
import functools
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat

from core.settings_constraints import native_absolute, path_within, windows_reserved_name
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.models import (
    ExecutionFootprint, IntegrityMetadata, MaterializationKind, MaterializationPlan, MaterializationResult,
    MaterializedEntry, SizeKnowledge,
)
from transfers.size_evidence import SizeEvidenceKind, classify_size_evidence

# Windows has neither ``O_NOFOLLOW`` nor descriptor-relative operations, and
# ``is_symlink`` is False for a junction there. The link and no-follow
# guarantees of this module hold on Windows through its native equivalents
# (``_Win32``: a reparse-point-refusing open, handle-relative removal). On
# every other platform this is False and none of that code runs.
_WINDOWS = os.name == "nt"
# ``st_reparse_tag`` of a junction (or volume mount point), which ``lstat``
# reports as a plain directory on Windows. ``stat`` names it only on Windows.
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


def safe_name(value: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value))[:200].strip().lstrip(".") or "download"
    if _WINDOWS:
        # Windows drops a trailing dot or space and opens ``NUL``, ``con.txt``
        # ... as devices: the bytes would land in another file or nowhere.
        name = name.rstrip(". ") or "download"
        if windows_reserved_name(name):
            name = f"_{name}"
    return name


def is_link(path: Path) -> bool:
    """A symlink, or on Windows also a junction (``is_symlink`` is False for
    one): an entry that is never followed, traversed or removed as what it
    points at. On POSIX exactly ``is_symlink``."""
    return path.is_symlink() or path.is_junction()


def _is_link_info(info: os.stat_result) -> bool:
    """``is_link`` for an ``lstat`` result already in hand."""
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT


def open_no_follow(path, flags: int, mode: int = 0o777) -> int:
    """``os.open`` that never follows a link at the final component and fails
    with ``ELOOP`` instead (``O_NOFOLLOW``). On Windows a native open of the
    entry itself, refused when it is a reparse point (symlink, junction or
    any other) before anything is read, written or truncated, always binary,
    never inherited by a child process, and sharing read, write and delete
    with every other opener as a POSIX descriptor does."""
    if not _WINDOWS:
        return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)
    return _win32().open_no_follow(os.fspath(path), flags)


def _read_at(fd: int, size: int, offset: int) -> bytes:
    """``os.pread`` where the platform has one; otherwise seek and read the
    same descriptor, which is equivalent because nothing else uses it."""
    pread = getattr(os, "pread", None)
    if pread is not None:
        return pread(fd, size, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, size)


def validate_target(root: str, target: str) -> Path:
    path = Path(target)
    try:
        base, resolved = Path(root).resolve(), path.resolve()
        valid = path.is_absolute() and not is_link(path) and resolved != base and resolved.is_relative_to(base)
    except (OSError, RuntimeError):
        valid = False
    if not valid:
        raise TransferError(NormalizedError(Domain.SECURITY, Category.PATH_POLICY_VIOLATION, Stage.VERIFICATION))
    return path


def relative_destination(relative: str) -> PurePosixPath:
    """The coordinate a member path is materialized at beneath any root:
    every segment made safe (``safe_name``). Raises for a path that would
    leave the root. Two member paths share a destination exactly when these
    agree (case-insensitively, as fan-out compares them)."""
    raw = PurePosixPath(str(relative).replace("\\", "/"))
    if raw.is_absolute() or ".." in raw.parts or not raw.parts:
        raise TransferError(NormalizedError(Domain.SECURITY, Category.PATH_POLICY_VIOLATION, Stage.CANDIDATE_PREPARATION))
    return PurePosixPath(*(safe_name(part) for part in raw.parts))


def destination(root: str, relative: str) -> Path:
    base = Path(root).resolve()
    path = base.joinpath(*relative_destination(relative).parts)
    return validate_target(root, str(path))


def directory_contains(path: Path) -> bool:
    try:
        with os.scandir(path.parent) as entries:
            return any(entry.name == path.name for entry in entries)
    except OSError:
        return False


def size_knowledge(
    reported_bytes: int, observed_total: int, *, recorded_bytes: int = 0, affirmative_zero: bool = False,
) -> tuple[SizeKnowledge, tuple[int, ...]]:
    """The one canonical projection of size evidence onto the three-state
    size-knowledge fact (DP 1.0.12 canonical lifecycle/recovery/completion
    rework, Section 3.3/5.2): ``SizeKnowledge.UNKNOWN``,
    ``SizeKnowledge.KNOWN_ZERO`` or ``SizeKnowledge.KNOWN_POSITIVE``, paired
    with the sizes a stable local payload may be verified against.

    How a provider-reported size, an executor-observed total and the
    artifact's recorded (bookkeeping) size relate is decided once, by
    ``transfers.size_evidence.classify_size_evidence`` -- no source is preferred
    here, and recorded bookkeeping never overrides a final total. The size tuple holds one size, or both
    asserted sizes for a bounded (compatible but unequal) conflict, which the
    stable local payload then decides between (``stable_material_size``). It is
    empty for ``UNKNOWN`` and for an incompatible conflict: callers must not
    verify against a size then, and never pick one of the conflicting numbers.

    ``0`` from either source is absence of size knowledge, never affirmative
    evidence of an intentionally empty payload (SIZE_UNKNOWN, distinct from
    SIZE_KNOWN(0)). ``affirmative_zero`` is the ONLY channel through which a
    caller may assert genuine known-zero evidence: a positive, explicit
    confirmation that a resource is intentionally zero bytes (e.g. an HTTP
    response that itself carried ``Content-Length: 0``), never merely the
    absence of a positive report. No provider or executor currently wired into
    this codebase produces that evidence -- every one of them resolves a
    reported size through a ``value or 0``-shaped fallback that cannot
    distinguish an explicitly reported zero from a missing/omitted field, so
    every real caller today passes ``affirmative_zero=False`` and can only ever
    observe UNKNOWN or KNOWN_POSITIVE. This is a factual limitation of the
    current evidence sources, not a policy choice to forbid zero-byte
    payloads: the parameter exists so a future provider/executor with a genuine
    affirmative-zero signal has one canonical place to report it. Do not set it
    from a default, an omitted header, or an executor's unknown-size report.
    """
    evidence = classify_size_evidence(reported_bytes, observed_total, recorded_bytes)
    if evidence.kind == SizeEvidenceKind.UNKNOWN:
        if affirmative_zero:
            return SizeKnowledge.KNOWN_ZERO, (0,)
        return SizeKnowledge.UNKNOWN, ()
    return SizeKnowledge.KNOWN_POSITIVE, evidence.verifiable_sizes


def payload_matches(path: str, expected_size: int, sidecars=(), integrity: tuple[IntegrityMetadata, ...] = (), *, allow_empty=False) -> bool:
    target = Path(path)
    if expected_size < 0 or (expected_size == 0 and not allow_empty) or not directory_contains(target) or any(directory_contains(Path(item)) for item in sidecars):
        return False
    descriptor = None
    try:
        descriptor = open_no_follow(target, os.O_RDONLY)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
            return False
        if expected_size and (len(_read_at(descriptor, 1, 0)) != 1 or len(_read_at(descriptor, 1, expected_size - 1)) != 1):
            return False
        for checksum in integrity:
            if checksum.algorithm not in {"sha256", "sha512", "sha1", "md5"}:
                return False
            digest = hashlib.new(checksum.algorithm, usedforsecurity=False)
            offset = 0
            while offset < expected_size:
                block = _read_at(descriptor, min(1024 * 1024, expected_size - offset), offset)
                if not block:
                    return False
                digest.update(block)
                offset += len(block)
            if digest.hexdigest().lower() != checksum.digest.lower():
                return False
        after = os.fstat(descriptor)
        return (info.st_size, info.st_mtime_ns, info.st_ino) == (after.st_size, after.st_mtime_ns, after.st_ino)
    except (OSError, ValueError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


async def stable_payload(path: str, expected_size: int, *, sidecars=(), integrity=(), delay=3.25, allow_empty=False) -> bool:
    if not await asyncio.to_thread(payload_matches, path, expected_size, sidecars, integrity, allow_empty=allow_empty):
        return False
    await asyncio.sleep(delay)
    return await asyncio.to_thread(payload_matches, path, expected_size, sidecars, integrity, allow_empty=allow_empty)


async def stable_material_size(
    path: str, reported_bytes: int, observed_total: int, *, recorded_bytes: int = 0, sidecars=(), integrity=(),
    delay=3.25, affirmative_zero=False,
) -> int | None:
    """The one canonical material-verification operation: the size the stable
    local payload proves, or ``None`` when nothing is verified.

    Provider-reported, executor-observed and recorded sizes are reconciled by
    ``size_knowledge``; no source is trusted over another. When the reported
    and observed sizes agree, or only one is known, that single size is
    verified; with no reported or observed size, the recorded size is. When they are
    unequal but compatible (a bounded conflict), the hardened stable-file
    verification decides which asserted size is real -- at most one can match
    the payload -- and that size is the accepted material size. An
    incompatible conflict, unknown size, and any integrity/sidecar/path/
    stability failure verify nothing. Every candidate size goes through the
    identical ``stable_payload`` checks (``O_NOFOLLOW``, regular file,
    first/last byte readability, sidecar exclusion, stable inode/mtime/size,
    strong integrity); a size that does not match the file fails fast before
    any hashing or stability delay.
    """
    knowledge, sizes = size_knowledge(
        reported_bytes, observed_total, recorded_bytes=recorded_bytes, affirmative_zero=affirmative_zero,
    )
    for size in sizes:
        if await stable_payload(path, size, sidecars=sidecars, integrity=integrity, delay=delay,
                                allow_empty=knowledge == SizeKnowledge.KNOWN_ZERO):
            return size
    return None


def retire_partial(root: str, target: str, sidecars=()) -> None:
    """Do not mix a failed candidate's partial bytes with an alternate source."""
    base = Path(root).resolve()
    paths = (target, *sidecars)
    for item in paths:
        path = Path(item)
        if is_link(path) or not path.resolve().is_relative_to(base):
            raise TransferError(NormalizedError(Domain.SECURITY, Category.PATH_POLICY_VIOLATION, Stage.CLEANUP))
    try:
        for item in paths:
            Path(item).unlink(missing_ok=True)
    except OSError as exc:
        raise TransferError(NormalizedError(Domain.LOCAL_RESOURCE, Category.LOCAL_CLEANUP_FAILED, Stage.CLEANUP,
                                            retryability=Retryability.AFTER_RESOURCE_CHANGE)) from exc


def _policy_violation(stage=Stage.VERIFICATION) -> TransferError:
    return TransferError(NormalizedError(Domain.SECURITY, Category.PATH_POLICY_VIOLATION, stage))


def materialization_plan(root: str, target: str, kind: MaterializationKind) -> MaterializationPlan:
    """Core output policy for one artifact: its durable target is the exact
    final file (FILE) or the dedicated collection directory (COLLECTION)."""
    if kind == MaterializationKind.COLLECTION:
        return MaterializationPlan(kind, str(target))
    return MaterializationPlan(MaterializationKind.FILE, str(Path(root).resolve()), str(target))


def _boundary(plan: MaterializationPlan) -> str:
    return plan.target if plan.kind == MaterializationKind.FILE else plan.root


def validate_plan(root: str, plan: MaterializationPlan, footprint: ExecutionFootprint) -> tuple[str, ...]:
    """Every path an execution may touch -- its material boundary and every
    declared native transient path -- lies inside download storage."""
    validate_target(root, _boundary(plan))
    transient = tuple(str(item) for item in (*footprint.transient_paths, *footprint.transient_trees))
    for item in transient:
        validate_target(root, item)
    return transient


def material_initially_absent(plan: MaterializationPlan, footprint: ExecutionFootprint) -> bool:
    """Observed at the final admission boundary: does the plan's material
    boundary, or any declared transient path, already hold anything? Present
    material pre-dates the execution and is never the execution's to retire.
    ``lexists`` so a dangling symlink still counts as present."""
    return not any(os.path.lexists(item) for item in (_boundary(plan), *footprint.transient_paths,
                                                     *footprint.transient_trees))


async def adoptable_material(plan: MaterializationPlan, footprint: ExecutionFootprint, expected_bytes: int,
                             integrity=(), *, delay=3.25) -> bool:
    """Pre-dispatch possession: an already stable FILE payload that STRONG
    integrity evidence verifies needs no execution. File length alone never
    proves possession (DebridPulse material truth is the other proof, owned by
    the engine). A collection is never adopted without a verified execution
    result naming its members."""
    if plan.kind != MaterializationKind.FILE or not integrity:
        return False
    return await stable_payload(plan.target, expected_bytes,
                                sidecars=(*footprint.transient_paths, *footprint.transient_trees),
                                integrity=integrity, delay=delay)


@dataclass(frozen=True)
class VerifiedMaterialization:
    """Core-verified material: the normalized result to persist, the absolute
    final paths for post-processing, and the proven size fact."""
    result: MaterializationResult
    paths: tuple[str, ...]
    total_bytes: int
    size_knowledge: SizeKnowledge


def _relative(value) -> PurePosixPath | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    raw = PurePosixPath(value.replace("\\", "/"))
    parts = tuple(part for part in raw.parts if part != ".")
    if raw.is_absolute() or not parts or ".." in parts:
        return None
    return PurePosixPath(*parts)


def _regular_beneath(root: Path, relative: PurePosixPath) -> os.stat_result | None:
    """lstat every component: nothing on the way may be a symlink, and the
    final entry must be a regular file inside ``root``."""
    current = root
    try:
        for index, part in enumerate(relative.parts):
            current = current / part
            info = os.lstat(current)
            if _is_link_info(info):
                return None
            if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
                return None
        return info if stat.S_ISREG(info.st_mode) else None
    except OSError:
        return None


def member_payload(root: str, relative: str) -> str | None:
    """The one member file of a COLLECTION an executor may report material
    for: a normalized relative path whose every component beneath ``root`` is
    a real directory and whose entry is a regular file (never a link).
    ``None`` refuses it -- nothing is flushed, committed or reconciled."""
    member = _relative(relative)
    if member is None or _regular_beneath(Path(root), member) is None:
        return None
    return str(Path(root).joinpath(*member.parts))


def observe_path(path: str) -> dict:
    """What is at ``path`` NOW, observed without following, opening, creating
    or repairing anything: one ``lstat``. ``type`` is ``file``, ``directory``,
    ``symlink``, ``other``, ``missing`` or ``unavailable`` (the observation
    itself failed, so presence is unknown -- never reported as missing)."""
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return {"exists": False, "type": "missing"}
    except (OSError, ValueError) as exc:
        return {"exists": None, "type": "unavailable",
                "error": errno.errorcode.get(getattr(exc, "errno", None) or 0, type(exc).__name__)}
    mode = info.st_mode
    kind = ("file" if stat.S_ISREG(mode) else "directory" if stat.S_ISDIR(mode)
            else "symlink" if stat.S_ISLNK(mode) else "other")
    return {"exists": True, "type": kind, "bytes": info.st_size if kind == "file" else None,
            "modified_at": info.st_mtime, "filesystem_id": str(info.st_dev)}


@dataclass(frozen=True)
class PayloadFacts:
    """Observable physical facts about one FILE payload, for reconciliation of
    DebridPulse material truth. Evidence only: nothing here ever makes a byte
    valid. ``available`` is False when the observation itself failed, which
    proves nothing either way. ``identity`` is ``"<st_dev>:<st_ino>"``;
    ``holes`` are unallocated ranges the filesystem reports inside the probed
    ranges."""
    available: bool
    exists: bool = False
    size: int = 0
    identity: str = ""
    holes: tuple[tuple[int, int], ...] = ()


def _holes(fd: int, size: int, probe) -> tuple[tuple[int, int], ...]:
    seek_hole = getattr(os, "SEEK_HOLE", None)
    seek_data = getattr(os, "SEEK_DATA", None)
    if seek_hole is None or seek_data is None:
        return ()
    found = []
    for start, end in probe:
        cursor, end = max(0, int(start)), min(int(end), size)
        while cursor < end:
            try:
                hole = os.lseek(fd, cursor, seek_hole)
            except OSError:
                return tuple(found)
            if hole >= end:
                break
            try:
                data = os.lseek(fd, hole, seek_data)
            except OSError as exc:
                if exc.errno != errno.ENXIO:
                    return tuple(found)
                data = size
            found.append((hole, min(data, end)))
            cursor = data
    return tuple(found)


def payload_facts(path: str, probe=()) -> PayloadFacts:
    """Observe a FILE payload without following a link or changing it."""
    try:
        fd = open_no_follow(path, os.O_RDONLY)
    except (FileNotFoundError, NotADirectoryError):
        return PayloadFacts(True, False)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            # A link where the payload belongs is not the payload.
            return PayloadFacts(True, False)
        return PayloadFacts(False)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return PayloadFacts(True, False)
        return PayloadFacts(True, True, info.st_size, f"{info.st_dev}:{info.st_ino}",
                            _holes(fd, info.st_size, probe))
    except OSError:
        return PayloadFacts(False)
    finally:
        os.close(fd)


def flush_payload(path: str) -> PayloadFacts | None:
    """The durability boundary of a checkpoint: flush everything already
    written to ``path`` to stable storage (``fsync`` covers the file's dirty
    data whichever descriptor wrote it) and report its facts. ``None`` when
    durability could not be established -- the caller then commits nothing.
    (Windows flushes only through a handle with write access, so there the
    payload is opened read-write; it is never written.)"""
    try:
        fd = open_no_follow(path, os.O_RDWR if _WINDOWS else os.O_RDONLY)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        os.fsync(fd)
        return PayloadFacts(True, True, info.st_size, f"{info.st_dev}:{info.st_ino}")
    except OSError:
        return None
    finally:
        os.close(fd)


def observe_material(target: str, member_paths=()) -> dict:
    """Read-only observation of one materialization target and the member
    paths already durably recorded beneath it. Never walks a directory: only
    the target and the named members are observed, each by ``observe_path``."""
    members = []
    for value in member_paths:
        relative = _relative(value)
        if relative is None:
            members.append({"relative_path": value, "exists": None, "type": "unavailable",
                            "error": "invalid_relative_path"})
            continue
        members.append({"relative_path": relative.as_posix(),
                        **observe_path(str(Path(target).joinpath(*relative.parts)))})
    return {"target": observe_path(target), "members": members}


def _collection_census(base: Path, footprint: ExecutionFootprint):
    """Every final file actually present beneath a dedicated collection root,
    as relative paths, excluding only declared transient files and trees.
    ``None`` when the tree holds anything that is not a plain directory or
    regular file (a symlink, device, fifo...) outside declared transients."""
    transient = {os.path.normpath(str(item)) for item in footprint.transient_paths}
    trees = tuple(os.path.normpath(str(item)) for item in footprint.transient_trees)

    def excluded(path: str) -> bool:
        path = os.path.normpath(path)
        return path in transient or any(path == tree or path.startswith(tree + os.sep) for tree in trees)

    found = set()
    for current, dirs, files in os.walk(base, followlinks=False):
        kept = []
        for name in dirs:
            path = os.path.join(current, name)
            if excluded(path):
                continue
            if stat.S_ISLNK(os.lstat(path).st_mode):
                return None
            kept.append(name)
        dirs[:] = kept
        for name in files:
            path = os.path.join(current, name)
            if excluded(path):
                continue
            if not stat.S_ISREG(os.lstat(path).st_mode):
                return None
            found.add(PurePosixPath(Path(path).relative_to(base).as_posix()))
    return found


def _collection_snapshot(root: str, plan: MaterializationPlan, result: MaterializationResult,
                         footprint: ExecutionFootprint):
    """The verified, complete state of a collection: every reported entry is a
    unique, non-symlinked regular file beneath the root, never transient, and
    matches any reported size -- AND the report accounts for exactly the final
    files actually present (an unreported produced file is a failure, not
    silently omitted from the durable record)."""
    base = Path(plan.root)
    try:
        validate_target(root, plan.root)
        info = os.lstat(base)
    except (TransferError, OSError):
        return None
    if not stat.S_ISDIR(info.st_mode) or result.kind != MaterializationKind.COLLECTION or not result.entries:
        return None
    transient = {os.path.normpath(str(item)) for item in footprint.transient_paths}
    trees = tuple(os.path.normpath(str(item)) for item in footprint.transient_trees)
    seen, snapshot = set(), []
    for entry in result.entries:
        if not isinstance(entry, MaterializedEntry):
            return None
        relative = _relative(entry.relative_path)
        if relative is None or relative in seen:
            return None
        seen.add(relative)
        path = os.path.normpath(str(base / relative))
        if path in transient or any(path == tree or path.startswith(tree + os.sep) for tree in trees):
            return None
        stat_info = _regular_beneath(base, relative)
        if stat_info is None:
            return None
        if entry.bytes is not None and (not isinstance(entry.bytes, int) or entry.bytes != stat_info.st_size):
            return None
        snapshot.append((relative.as_posix(), path, stat_info.st_size, stat_info.st_mtime_ns, stat_info.st_ino))
    try:
        census = _collection_census(base, footprint)
    except OSError:
        return None
    if census is None or census != seen:
        return None
    return tuple(sorted(snapshot))


async def verify_materialization(
    root: str, plan: MaterializationPlan, result: MaterializationResult | None, footprint: ExecutionFootprint, *,
    reported_bytes: int = 0, observed_total: int = 0, recorded_bytes: int = 0, integrity=(), delay=3.25,
) -> VerifiedMaterialization | None:
    """THE verification of what a succeeded execution claims it produced.

    The executor's report is a fact to verify, never to trust: FILE must name
    exactly the plan's target and passes the hardened stable-file verification
    (``stable_material_size``: size reconciliation, O_NOFOLLOW, regular file,
    integrity, stability delay, transient-path exclusion, explicit known-zero
    semantics); COLLECTION entries must each be a regular, non-symlinked file
    beneath the dedicated root, unique, never a declared transient path, match
    any reported size, and hold still across the stability delay."""
    if not isinstance(result, MaterializationResult) or result.kind != plan.kind:
        return None
    try:
        transient = validate_plan(root, plan, footprint)
    except TransferError:
        return None
    if plan.kind == MaterializationKind.FILE:
        if len(result.entries) != 1 or not isinstance(result.entries[0], MaterializedEntry):
            return None
        entry = result.entries[0]
        relative = _relative(entry.relative_path)
        if relative is None or os.path.normpath(str(Path(plan.root) / relative)) != os.path.normpath(plan.target):
            return None
        size = await stable_material_size(
            plan.target, reported_bytes, observed_total, recorded_bytes=recorded_bytes, sidecars=transient,
            integrity=integrity, delay=delay,
        )
        if size is None or (entry.bytes is not None and entry.bytes not in (0, size)):
            return None
        knowledge = SizeKnowledge.KNOWN_POSITIVE if size > 0 else SizeKnowledge.KNOWN_ZERO
        return VerifiedMaterialization(
            MaterializationResult(MaterializationKind.FILE, (MaterializedEntry(relative.as_posix(), size),)),
            (plan.target,), size, knowledge,
        )
    first = await asyncio.to_thread(_collection_snapshot, root, plan, result, footprint)
    if first is None:
        return None
    await asyncio.sleep(delay)
    if await asyncio.to_thread(_collection_snapshot, root, plan, result, footprint) != first:
        return None
    total = sum(item[2] for item in first)
    if total <= 0:
        return None
    return VerifiedMaterialization(
        MaterializationResult(MaterializationKind.COLLECTION, tuple(
            MaterializedEntry(item[0], item[2]) for item in first)),
        tuple(item[1] for item in first), total, SizeKnowledge.KNOWN_POSITIVE,
    )


def verified_material_paths(root: str, plan: MaterializationPlan, result: MaterializationResult | None,
                            footprint: ExecutionFootprint, *, expected_bytes: int, allow_empty=False,
                            integrity=()) -> tuple[str, ...] | None:
    """Delivery-time re-verification of already verified material, without a
    stability delay. FILE re-checks the exact payload; COLLECTION re-checks
    every durably recorded entry. ``None`` means the material is gone/changed."""
    if plan.kind == MaterializationKind.FILE:
        if payload_matches(plan.target, expected_bytes, (*footprint.transient_paths, *footprint.transient_trees),
                           integrity, allow_empty=allow_empty):
            return (plan.target,)
        return None
    if result is None:
        return None
    snapshot = _collection_snapshot(root, plan, result, footprint)
    return tuple(item[1] for item in snapshot) if snapshot is not None else None


def _remove_tree(path: str) -> None:
    """Remove a directory tree without following any symlink; a non-directory
    at a tree position is a policy violation, never silently unlinked."""
    if not os.path.lexists(path):
        return
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        raise _policy_violation(Stage.CLEANUP)
    shutil.rmtree(path)


def _prune_empty_parents(base: Path, target: Path) -> None:
    """Reclaim the empty directory scaffolding a retired FILE target leaves.

    ``rmdir`` semantics only, never recursive: the first directory that still
    holds anything at all -- a sibling artifact, another transfer's work,
    operator material -- ends the walk and is left exactly as it was. The
    configured download root is a boundary and is never removed.

    The ancestor chain is established LEXICALLY, before anything is removed,
    and a symlinked component ends it. Resolving first would defeat exactly
    that check: resolution has already traversed the link, so what is then
    inspected is the real directory the link points at, never the link -- and
    pruning that directory would delete material outside this plan's own path
    and strand the link. A chain that does not lexically reach the download
    root prunes nothing.
    """
    ancestors = []
    current = target.parent
    while current != base:
        if not current.is_relative_to(base) or current == current.parent or is_link(current):
            return
        ancestors.append(current)
        current = current.parent
    for directory in ancestors:
        try:
            directory.rmdir()
        except OSError:
            return


def retire_native_state(root: str, plan: MaterializationPlan, footprint: ExecutionFootprint) -> None:
    """Discard an execution's private native transient state (a resume/control
    file or partial tree) while leaving its materialization boundary -- the
    payload DebridPulse material truth describes -- untouched. The caller has
    already proven the execution owns it. Never follows a symlink."""
    base = Path(root).resolve()
    transient = tuple(str(item) for item in footprint.transient_paths)
    trees = tuple(str(item) for item in footprint.transient_trees)
    for item in (*transient, *trees):
        path = Path(item)
        if is_link(path) or not path.resolve().is_relative_to(base) or path.resolve() == base:
            raise _policy_violation(Stage.CLEANUP)
    try:
        for item in transient:
            Path(item).unlink(missing_ok=True)
        for item in trees:
            _remove_tree(item)
    except OSError as exc:
        raise TransferError(NormalizedError(Domain.LOCAL_RESOURCE, Category.LOCAL_CLEANUP_FAILED, Stage.CLEANUP,
                                            retryability=Retryability.AFTER_RESOURCE_CHANGE)) from exc


def retire_materialization(root: str, plan: MaterializationPlan, footprint: ExecutionFootprint, *,
                           owned: bool, prune_empty_parents: bool = False) -> None:
    """Retire an execution's invalid or superseded material inside download
    storage -- ONLY under positive durable ownership (``owned``: the
    execution's material authority, established at admission). Without it
    nothing is deleted: not the FILE target, not a COLLECTION root, not any
    declared transient file or tree. With it: the FILE target or the dedicated
    COLLECTION root (recursively), plus every declared transient file and
    tree. Removal never follows a symlink.

    ``prune_empty_parents`` additionally reclaims the empty path scaffolding a
    removed FILE target leaves behind (see ``_prune_empty_parents``). A
    COLLECTION plan already owns its dedicated root, which this function
    removes whole, so it is unaffected."""
    if not owned:
        return
    base = Path(root).resolve()
    transient = tuple(str(item) for item in footprint.transient_paths)
    trees = tuple(str(item) for item in footprint.transient_trees)
    boundary = _boundary(plan)
    for item in (boundary, *transient, *trees):
        path = Path(item)
        if is_link(path) or not path.resolve().is_relative_to(base) or path.resolve() == base:
            raise _policy_violation(Stage.CLEANUP)
    try:
        for item in transient:
            Path(item).unlink(missing_ok=True)
        for item in trees:
            _remove_tree(item)
        if plan.kind == MaterializationKind.FILE:
            Path(boundary).unlink(missing_ok=True)
            if prune_empty_parents:
                _prune_empty_parents(base, Path(boundary))
        else:
            _remove_tree(boundary)
    except OSError as exc:
        raise TransferError(NormalizedError(Domain.LOCAL_RESOURCE, Category.LOCAL_CLEANUP_FAILED, Stage.CLEANUP,
                                            retryability=Retryability.AFTER_RESOURCE_CHANGE)) from exc


# Outcomes of ``remove_empty_directory``.
DIRECTORY_REMOVED = "removed"
DIRECTORY_ABSENT = "absent"
DIRECTORY_NOT_EMPTY = "not_empty"
DIRECTORY_REFUSED = "refused"


def remove_empty_directory(root: str, target: str) -> str:
    """Remove ``target`` if, at that instant, it is an empty directory
    DebridPulse owns beneath ``root`` -- never anything else.

    Every component below ``root`` is opened relative to its parent's open
    descriptor with ``O_DIRECTORY | O_NOFOLLOW``, so no symlink anywhere on
    the way is followed and no component can be swapped behind a path
    string. The final name is checked through that parent descriptor
    (``lstat``: a real directory, on the parent's device -- never a mount
    point -- owned by this process's user) and removed with
    ``rmdir(name, dir_fd=parent)``: the kernel refuses a non-empty directory
    (``ENOTEMPTY``) and never follows or removes a symlink as a directory
    (``ENOTDIR``). Nothing is ever recursed into, unlinked or emptied.

    Returns ``"removed"``, ``"absent"`` (nothing there, or a path component
    gone), ``"not_empty"``, or ``"refused"`` (outside ``root``, the root
    itself, a symlink or non-directory anywhere, another owner or device,
    a permission error, or a platform without descriptor-relative
    ``rmdir``/``O_NOFOLLOW``). Never raises for any of those.

    On Windows the same walk uses handles: the root and each component below
    it are opened as the entry itself, never following a reparse point (a
    root that is one is refused), each component relative to its parent's
    open handle (``NtCreateFile`` with a root directory); the final directory
    must be a real directory on the root's volume owned by this process's
    user, and is deleted through that very handle, which Windows refuses for
    a non-empty directory (``_Win32.remove_empty_directory``).
    """
    if _WINDOWS:
        return _remove_empty_directory_windows(root, target)
    if (not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
            or os.rmdir not in os.supports_dir_fd or os.stat not in os.supports_dir_fd
            or os.open not in os.supports_dir_fd):
        return DIRECTORY_REFUSED
    base = os.path.normpath(str(root))
    path = str(target)
    # Only an exact, already-normal path: ".." or "." is never resolved
    # lexically (a lexical ".." after a symlinked component means elsewhere).
    if os.path.normpath(path) != path:
        return DIRECTORY_REFUSED
    if not os.path.isabs(base) or not os.path.isabs(path) or path == base \
            or not path.startswith(base.rstrip("/") + "/"):
        return DIRECTORY_REFUSED
    parts = path[len(base.rstrip("/")) + 1:].split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return DIRECTORY_REFUSED
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        parent = os.open(base, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return DIRECTORY_REFUSED
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, flags, dir_fd=parent)
            except FileNotFoundError:
                return DIRECTORY_ABSENT
            except OSError:
                return DIRECTORY_REFUSED          # a symlink (ELOOP), a non-directory, no access
            os.close(parent)
            parent = child
        name = parts[-1]
        try:
            facts = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return DIRECTORY_ABSENT
        except OSError:
            return DIRECTORY_REFUSED
        if (not stat.S_ISDIR(facts.st_mode) or facts.st_dev != os.fstat(parent).st_dev
                or facts.st_uid != os.geteuid()):
            return DIRECTORY_REFUSED
        try:
            os.rmdir(name, dir_fd=parent)
        except FileNotFoundError:
            return DIRECTORY_ABSENT
        except OSError as exc:
            return DIRECTORY_NOT_EMPTY if exc.errno in {errno.ENOTEMPTY, errno.EEXIST} else DIRECTORY_REFUSED
        return DIRECTORY_REMOVED
    finally:
        os.close(parent)


def _remove_empty_directory_windows(root: str, target: str) -> str:
    base = os.path.normpath(str(root))
    path = str(target)
    # Only an exact, already-normal, fully qualified path strictly beneath
    # the root (``native_absolute`` also refuses streams, devices and names
    # Windows would rewrite); the case-insensitive containment is Windows'.
    if (not native_absolute(base) or not native_absolute(path) or not path_within(base, path)
            or path_within(path, base)):
        return DIRECTORY_REFUSED
    parts = path[len(base.rstrip(os.sep)) + 1:].split(os.sep)
    if any(part in {"", ".", ".."} for part in parts):
        return DIRECTORY_REFUSED
    try:
        native = _win32()
    except OSError:
        return DIRECTORY_REFUSED
    return native.remove_empty_directory(base, parts)


@functools.cache
def _win32() -> "_Win32":
    return _Win32()


class _Win32:
    """The Windows primitives behind ``open_no_follow`` and
    ``remove_empty_directory``: documented kernel32/ntdll/advapi32 calls,
    loaded only on Windows."""

    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    DELETE, READ_CONTROL, SYNCHRONIZE = 0x00010000, 0x00020000, 0x00100000
    FILE_LIST_DIRECTORY, FILE_TRAVERSE, FILE_READ_ATTRIBUTES = 0x0001, 0x0020, 0x0080
    SHARE_ALL = 0x7                                   # read | write | delete
    CREATE_NEW, OPEN_EXISTING, OPEN_ALWAYS = 1, 3, 4
    FLAG_OPEN_REPARSE_POINT, FLAG_BACKUP_SEMANTICS = 0x00200000, 0x02000000
    ATTRIBUTE_DIRECTORY, ATTRIBUTE_REPARSE_POINT = 0x10, 0x400
    # NtCreateFile: open (never create) a directory, as the entry itself.
    NT_FILE_OPEN, NT_DIRECTORY_FILE, NT_SYNCHRONOUS_IO_NONALERT, NT_OPEN_REPARSE_POINT = 1, 0x1, 0x20, 0x00200000
    OBJ_CASE_INSENSITIVE = 0x40
    STATUS_MISSING = {0xC0000034, 0xC000003A}         # object name / path not found
    FILE_DISPOSITION_INFO = 4
    ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND, ERROR_DIR_NOT_EMPTY = 2, 3, 145

    def __init__(self):
        import ctypes
        import msvcrt
        from ctypes import wintypes

        self.ctypes, self.msvcrt = ctypes, msvcrt
        handle, dword, ulong = wintypes.HANDLE, wintypes.DWORD, wintypes.ULONG

        class UnicodeString(ctypes.Structure):
            _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", wintypes.LPWSTR)]

        class ObjectAttributes(ctypes.Structure):
            _fields_ = [("Length", ulong), ("RootDirectory", handle), ("ObjectName", ctypes.POINTER(UnicodeString)),
                        ("Attributes", ulong), ("SecurityDescriptor", ctypes.c_void_p),
                        ("SecurityQualityOfService", ctypes.c_void_p)]

        class IoStatusBlock(ctypes.Structure):
            _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_void_p)]

        class FileInformation(ctypes.Structure):          # BY_HANDLE_FILE_INFORMATION
            _fields_ = [("dwFileAttributes", dword), ("ftCreationTime", wintypes.FILETIME),
                        ("ftLastAccessTime", wintypes.FILETIME), ("ftLastWriteTime", wintypes.FILETIME),
                        ("dwVolumeSerialNumber", dword), ("nFileSizeHigh", dword), ("nFileSizeLow", dword),
                        ("nNumberOfLinks", dword), ("nFileIndexHigh", dword), ("nFileIndexLow", dword)]

        self.UnicodeString, self.ObjectAttributes = UnicodeString, ObjectAttributes
        self.IoStatusBlock, self.FileInformation = IoStatusBlock, FileInformation
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.CreateFileW = kernel32.CreateFileW
        self.CreateFileW.argtypes = [wintypes.LPCWSTR, dword, dword, ctypes.c_void_p, dword, dword, handle]
        self.CreateFileW.restype = handle
        self.CloseHandle = kernel32.CloseHandle
        self.CloseHandle.argtypes = [handle]
        self.GetFileInformationByHandle = kernel32.GetFileInformationByHandle
        self.GetFileInformationByHandle.argtypes = [handle, ctypes.POINTER(FileInformation)]
        self.GetFileInformationByHandle.restype = wintypes.BOOL
        self.SetFileInformationByHandle = kernel32.SetFileInformationByHandle
        self.SetFileInformationByHandle.argtypes = [handle, ctypes.c_int, ctypes.c_void_p, dword]
        self.SetFileInformationByHandle.restype = wintypes.BOOL
        self.LocalFree = kernel32.LocalFree
        self.LocalFree.argtypes = [ctypes.c_void_p]
        self.NtCreateFile = ntdll.NtCreateFile
        self.NtCreateFile.argtypes = [ctypes.POINTER(handle), ulong, ctypes.POINTER(ObjectAttributes),
                                      ctypes.POINTER(IoStatusBlock), ctypes.c_void_p, ulong, ulong, ulong, ulong,
                                      ctypes.c_void_p, ulong]
        self.NtCreateFile.restype = ctypes.c_long
        self.GetSecurityInfo = advapi32.GetSecurityInfo
        self.GetSecurityInfo.argtypes = [handle, ctypes.c_int, dword, ctypes.POINTER(ctypes.c_void_p),
                                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.POINTER(ctypes.c_void_p)]
        self.GetSecurityInfo.restype = dword
        self.EqualSid = advapi32.EqualSid
        self.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.EqualSid.restype = wintypes.BOOL
        self.INVALID_HANDLE = ctypes.c_void_p(-1).value
        self._user = self._process_user(kernel32, advapi32, wintypes)

    def _process_user(self, kernel32, advapi32, wintypes):
        """This process's user SID (the token's ``TokenUser``), kept alive in
        its buffer for ``EqualSid``."""
        ctypes = self.ctypes
        token = wintypes.HANDLE()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.DWORD)]
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            needed = wintypes.DWORD()
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))           # TokenUser
            buffer = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.CloseHandle(token)
        self._user_buffer = buffer
        return ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid

    def _error(self, path: str) -> OSError:
        code = self.ctypes.get_last_error()
        return OSError(0, self.ctypes.FormatError(code).strip(), path, code)

    def open_no_follow(self, path: str, flags: int) -> int:
        access_mode = flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR)
        access = {os.O_RDONLY: self.GENERIC_READ, os.O_WRONLY: self.GENERIC_WRITE}.get(
            access_mode, self.GENERIC_READ | self.GENERIC_WRITE)
        if flags & os.O_CREAT:
            disposition = self.CREATE_NEW if flags & os.O_EXCL else self.OPEN_ALWAYS
        else:
            disposition = self.OPEN_EXISTING
        # The entry itself (never a reparse point's target), directories too
        # (as ``os.open`` opens one on POSIX); not inheritable by children.
        handle = self.CreateFileW(path, access, self.SHARE_ALL, None, disposition,
                                  self.FLAG_OPEN_REPARSE_POINT | self.FLAG_BACKUP_SEMANTICS, None)
        if handle is None or handle == self.INVALID_HANDLE:
            raise self._error(path)
        try:
            fd = self.msvcrt.open_osfhandle(handle, access_mode | (flags & os.O_APPEND))
        except BaseException:
            self.CloseHandle(handle)
            raise
        try:
            info = os.fstat(fd)
            if info.st_file_attributes & self.ATTRIBUTE_REPARSE_POINT:
                raise OSError(errno.ELOOP, "A link is never followed", path)
            if flags & os.O_TRUNC and stat.S_ISREG(info.st_mode):
                os.ftruncate(fd, 0)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _facts(self, handle) -> tuple[int, int] | None:
        """``(attributes, volume serial)`` of an open handle."""
        info = self.FileInformation()
        if not self.GetFileInformationByHandle(handle, self.ctypes.byref(info)):
            return None
        return info.dwFileAttributes, info.dwVolumeSerialNumber

    def _open_relative(self, parent, name: str, access: int):
        """Open directory ``name`` relative to the open ``parent`` handle, as
        the entry itself. ``(handle, missing)``; ``handle`` is None on failure."""
        ctypes = self.ctypes
        size = len(name) * 2
        object_name = self.UnicodeString(size, size, name)
        attributes = self.ObjectAttributes(ctypes.sizeof(self.ObjectAttributes), parent, ctypes.pointer(object_name),
                                           self.OBJ_CASE_INSENSITIVE, None, None)
        handle, status_block = self.ctypes.c_void_p(), self.IoStatusBlock()
        status = self.NtCreateFile(ctypes.byref(handle), access | self.SYNCHRONIZE, ctypes.byref(attributes),
                                   ctypes.byref(status_block), None, 0, self.SHARE_ALL, self.NT_FILE_OPEN,
                                   self.NT_DIRECTORY_FILE | self.NT_SYNCHRONOUS_IO_NONALERT | self.NT_OPEN_REPARSE_POINT,
                                   None, 0)
        if status < 0:
            return None, (status & 0xFFFFFFFF) in self.STATUS_MISSING
        return handle.value, False

    def _owned_by_this_user(self, handle) -> bool:
        ctypes = self.ctypes
        owner, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
        if self.GetSecurityInfo(handle, 1, 0x1, ctypes.byref(owner), None, None, None,   # SE_FILE_OBJECT, OWNER
                                ctypes.byref(descriptor)) != 0:
            return False
        try:
            return bool(self.EqualSid(owner, self._user))
        finally:
            self.LocalFree(descriptor)

    def remove_empty_directory(self, base: str, parts: list[str]) -> str:
        # The root is opened as the entry itself too, so a root that is a
        # junction, directory symlink or other reparse point is refused by
        # its handle's own attributes, never followed to wherever it points.
        root = self.CreateFileW(base, self.FILE_LIST_DIRECTORY | self.FILE_TRAVERSE | self.FILE_READ_ATTRIBUTES,
                                self.SHARE_ALL, None, self.OPEN_EXISTING,
                                self.FLAG_OPEN_REPARSE_POINT | self.FLAG_BACKUP_SEMANTICS, None)
        if root is None or root == self.INVALID_HANDLE:
            return DIRECTORY_REFUSED
        handles = [root]
        try:
            facts = self._facts(root)
            if facts is None or facts[0] & self.ATTRIBUTE_REPARSE_POINT or not facts[0] & self.ATTRIBUTE_DIRECTORY:
                return DIRECTORY_REFUSED
            volume, current = facts[1], root
            for index, part in enumerate(parts):
                final = index == len(parts) - 1
                access = self.FILE_READ_ATTRIBUTES | (
                    self.DELETE | self.READ_CONTROL if final else self.FILE_LIST_DIRECTORY | self.FILE_TRAVERSE)
                child, missing = self._open_relative(current, part, access)
                if child is None:
                    return DIRECTORY_ABSENT if missing else DIRECTORY_REFUSED
                handles.append(child)
                facts = self._facts(child)
                # A reparse point (symlink, junction, mount point), a
                # non-directory or another volume: never traversed or removed.
                if (facts is None or facts[0] & self.ATTRIBUTE_REPARSE_POINT
                        or not facts[0] & self.ATTRIBUTE_DIRECTORY or facts[1] != volume):
                    return DIRECTORY_REFUSED
                current = child
            if not self._owned_by_this_user(current):
                return DIRECTORY_REFUSED
            delete = self.ctypes.c_ubyte(1)               # FILE_DISPOSITION_INFO.DeleteFile
            if self.SetFileInformationByHandle(current, self.FILE_DISPOSITION_INFO, self.ctypes.byref(delete), 1):
                return DIRECTORY_REMOVED                  # removed as this handle closes
            code = self.ctypes.get_last_error()
            if code == self.ERROR_DIR_NOT_EMPTY:
                return DIRECTORY_NOT_EMPTY
            return DIRECTORY_ABSENT if code in {self.ERROR_FILE_NOT_FOUND, self.ERROR_PATH_NOT_FOUND} else DIRECTORY_REFUSED
        finally:
            for handle in reversed(handles):
                self.CloseHandle(handle)
