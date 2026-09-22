"""Destination and local-possession policy independent of transfer mechanisms.

This module is the ONE materialization verification and cleanup owner. A FILE
plan is verified by the hardened single-file path below; a COLLECTION plan by
the collection branch of the same owner. Nothing here knows which executor
produced the material.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat

from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.models import (
    ExecutionFootprint, IntegrityMetadata, MaterializationKind, MaterializationPlan, MaterializationResult,
    MaterializedEntry, SizeKnowledge,
)
from transfers.size_evidence import SizeEvidenceKind, classify_size_evidence


def safe_name(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value))[:200].strip().lstrip(".") or "download"


def validate_target(root: str, target: str) -> Path:
    path = Path(target)
    try:
        base, resolved = Path(root).resolve(), path.resolve()
        valid = path.is_absolute() and not path.is_symlink() and resolved != base and resolved.is_relative_to(base)
    except (OSError, RuntimeError):
        valid = False
    if not valid:
        raise TransferError(NormalizedError(Domain.SECURITY, Category.PATH_POLICY_VIOLATION, Stage.VERIFICATION))
    return path


def destination(root: str, relative: str) -> Path:
    raw = PurePosixPath(str(relative).replace("\\", "/"))
    if raw.is_absolute() or ".." in raw.parts or not raw.parts:
        raise TransferError(NormalizedError(Domain.SECURITY, Category.PATH_POLICY_VIOLATION, Stage.CANDIDATE_PREPARATION))
    base = Path(root).resolve()
    path = base.joinpath(*(safe_name(part) for part in raw.parts))
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
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
            return False
        if expected_size and (len(os.pread(descriptor, 1, 0)) != 1 or len(os.pread(descriptor, 1, expected_size - 1)) != 1):
            return False
        for checksum in integrity:
            if checksum.algorithm not in {"sha256", "sha512", "sha1", "md5"}:
                return False
            digest = hashlib.new(checksum.algorithm, usedforsecurity=False)
            offset = 0
            while offset < expected_size:
                block = os.pread(descriptor, min(1024 * 1024, expected_size - offset), offset)
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
        if path.is_symlink() or not path.resolve().is_relative_to(base):
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
    """Pre-dispatch possession: an already stable, fully verified FILE payload
    needs no execution. A collection is never adopted without a verified
    execution result naming its members."""
    if plan.kind != MaterializationKind.FILE:
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
            if stat.S_ISLNK(info.st_mode):
                return None
            if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
                return None
        return info if stat.S_ISREG(info.st_mode) else None
    except OSError:
        return None


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


def retire_materialization(root: str, plan: MaterializationPlan, footprint: ExecutionFootprint, *,
                           owned: bool) -> None:
    """Retire an execution's invalid or superseded material inside download
    storage -- ONLY under positive durable ownership (``owned``: the
    execution's material authority, established at admission). Without it
    nothing is deleted: not the FILE target, not a COLLECTION root, not any
    declared transient file or tree. With it: the FILE target or the dedicated
    COLLECTION root (recursively), plus every declared transient file and
    tree. Removal never follows a symlink."""
    if not owned:
        return
    base = Path(root).resolve()
    transient = tuple(str(item) for item in footprint.transient_paths)
    trees = tuple(str(item) for item in footprint.transient_trees)
    boundary = _boundary(plan)
    for item in (boundary, *transient, *trees):
        path = Path(item)
        if path.is_symlink() or not path.resolve().is_relative_to(base) or path.resolve() == base:
            raise _policy_violation(Stage.CLEANUP)
    try:
        for item in transient:
            Path(item).unlink(missing_ok=True)
        for item in trees:
            _remove_tree(item)
        if plan.kind == MaterializationKind.FILE:
            Path(boundary).unlink(missing_ok=True)
        else:
            _remove_tree(boundary)
    except OSError as exc:
        raise TransferError(NormalizedError(Domain.LOCAL_RESOURCE, Category.LOCAL_CLEANUP_FAILED, Stage.CLEANUP,
                                            retryability=Retryability.AFTER_RESOURCE_CHANGE)) from exc
