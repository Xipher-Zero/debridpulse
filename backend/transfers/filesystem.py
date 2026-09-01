"""Destination and local-possession policy independent of transfer mechanisms."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat

from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.models import IntegrityMetadata


def safe_name(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value))[:200].strip().lstrip(".") or "download"


def destination(root: str, relative: str) -> Path:
    raw = PurePosixPath(str(relative).replace("\\", "/"))
    if raw.is_absolute() or ".." in raw.parts or not raw.parts:
        raise TransferError(NormalizedError(Domain.SECURITY, Category.PATH_POLICY_VIOLATION, Stage.CANDIDATE_PREPARATION))
    base = Path(root).resolve()
    path = base.joinpath(*(safe_name(part) for part in raw.parts))
    if path.is_symlink() or not path.resolve().is_relative_to(base):
        raise TransferError(NormalizedError(Domain.SECURITY, Category.PATH_POLICY_VIOLATION, Stage.CANDIDATE_PREPARATION))
    return path


def directory_contains(path: Path) -> bool:
    try:
        with os.scandir(path.parent) as entries:
            return any(entry.name == path.name for entry in entries)
    except OSError:
        return False


def payload_matches(path: str, expected_size: int, sidecars=(), integrity: tuple[IntegrityMetadata, ...] = ()) -> bool:
    target = Path(path)
    if expected_size <= 0 or not directory_contains(target) or any(directory_contains(Path(item)) for item in sidecars):
        return False
    descriptor = None
    try:
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
            return False
        if len(os.pread(descriptor, 1, 0)) != 1 or len(os.pread(descriptor, 1, expected_size - 1)) != 1:
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


async def stable_payload(path: str, expected_size: int, *, sidecars=(), integrity=(), delay=3.25) -> bool:
    if not await asyncio.to_thread(payload_matches, path, expected_size, sidecars, integrity):
        return False
    await asyncio.sleep(delay)
    return await asyncio.to_thread(payload_matches, path, expected_size, sidecars, integrity)


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
