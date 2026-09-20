"""Destination and local-possession policy independent of transfer mechanisms."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat

from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.models import IntegrityMetadata, SizeKnowledge
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
