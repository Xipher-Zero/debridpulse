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


def known_positive_size(expected_bytes: int, observed_total: int) -> int | None:
    """Canonical size-knowledge fact for completion verification (DP 1.0.12
    canonical lifecycle/recovery/completion rework, Section 5.2/3.3).

    Returns the one affirmative positive size this engine currently has
    grounds to trust -- a known expected size, else an authoritative
    executor-reported total -- or ``None`` when neither is positive. ``0``
    from either source is never returned: a candidate that defaulted to 0 or
    an executor that reported ``totalLength=0`` is absence of size
    knowledge, never affirmative evidence of an intentionally empty payload
    (SIZE_UNKNOWN, distinct from SIZE_KNOWN(0)). Callers must not attempt
    completion verification when this returns ``None``.
    """
    if isinstance(expected_bytes, int) and not isinstance(expected_bytes, bool) and expected_bytes > 0:
        return expected_bytes
    if isinstance(observed_total, int) and not isinstance(observed_total, bool) and observed_total > 0:
        return observed_total
    return None


def size_knowledge(
    expected_bytes: int, observed_total: int, *, affirmative_zero: bool = False,
) -> tuple[SizeKnowledge, int]:
    """The one canonical resolver producing the three-state size-knowledge
    fact (DP 1.0.12 canonical lifecycle/recovery/completion rework, Section
    3.3/5.2): ``SizeKnowledge.UNKNOWN``, ``SizeKnowledge.KNOWN_ZERO``, or
    ``SizeKnowledge.KNOWN_POSITIVE``, paired with the size to verify against
    (``0`` for ``UNKNOWN``, meaningless -- callers must not verify).

    ``affirmative_zero`` is the ONLY channel through which a caller may
    assert genuine known-zero evidence: a positive, explicit confirmation
    that a resource is intentionally zero bytes (e.g. an HTTP response that
    itself carried ``Content-Length: 0``), never merely the absence of a
    positive report. No provider or executor currently wired into this
    codebase produces that evidence -- every one of them resolves a reported
    size through a ``value or 0``-shaped fallback that cannot distinguish an
    explicitly reported zero from a missing/omitted field, so every real
    caller today passes ``affirmative_zero=False`` and can only ever observe
    UNKNOWN or KNOWN_POSITIVE. This is a factual limitation of the current
    evidence sources, not a policy choice to forbid zero-byte payloads: the
    parameter exists so a future provider/executor with a genuine
    affirmative-zero signal has one canonical place to report it, without a
    second completion-truth rework. Do not set it from a default, an omitted
    header, or an executor's unknown-size report.
    """
    positive = known_positive_size(expected_bytes, observed_total)
    if positive is not None:
        return SizeKnowledge.KNOWN_POSITIVE, positive
    if affirmative_zero:
        return SizeKnowledge.KNOWN_ZERO, 0
    return SizeKnowledge.UNKNOWN, 0


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
