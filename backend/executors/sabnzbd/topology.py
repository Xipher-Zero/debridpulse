"""The filesystem seam between SAB's working area and DebridPulse's material.

SAB writes only inside a hidden working area beneath the DebridPulse global
Download Folder. DebridPulse owns the visible namespace: a completed job's
repaired payload is moved into the core-allocated COLLECTION root by an atomic
same-filesystem rename. No separate SAB download root exists, and nothing may
escape the authorized root.
"""
from __future__ import annotations

import os
from pathlib import Path

# The hidden working area beneath the DebridPulse global Download Folder.
WORKING_DIRECTORY_NAME = ".dpwork"
INCOMPLETE_DIRECTORY_NAME = "incomplete"
COMPLETE_DIRECTORY_NAME = "complete"


def working_root(download_root: str) -> str:
    return str(Path(download_root).resolve() / WORKING_DIRECTORY_NAME)


def incomplete_root(download_root: str) -> str:
    return str(Path(working_root(download_root)) / INCOMPLETE_DIRECTORY_NAME)


def complete_root(download_root: str) -> str:
    return str(Path(working_root(download_root)) / COMPLETE_DIRECTORY_NAME)


def contained(root: str, candidate: str) -> Path | None:
    """``candidate`` resolved, only if it lies strictly beneath ``root``.

    Rejects relative paths, symlinked leaves and any escape through a parent
    symlink (``resolve()`` collapses those before the containment test).
    """
    try:
        base = Path(root).resolve()
        path = Path(candidate)
        if not path.is_absolute() or path.is_symlink():
            return None
        resolved = path.resolve()
    except (OSError, ValueError):
        return None
    if resolved == base or not resolved.is_relative_to(base):
        return None
    return resolved


def deliver(source: str, destination: str) -> bool:
    """Atomically move a completed job's directory into the core plan root.

    Idempotent: a destination that already exists counts as delivered. Returns
    ``False`` when neither side can satisfy delivery, which the caller reports
    as uncertainty -- never as success and never as a terminal failure.
    """
    target = Path(destination)
    origin = Path(source)
    if target.exists():
        return True
    if not origin.is_dir():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Same filesystem (both beneath the download root) -> atomic.
        os.rename(origin, target)
    except OSError:
        return False
    return True


def collection_entries(root: str) -> tuple[tuple[str, int], ...]:
    """Every regular file beneath ``root``, relative and sorted.

    A symlink anywhere in the tree makes the collection unreportable.
    """
    base = Path(root)
    found: list[tuple[str, int]] = []
    for current, directories, files in os.walk(base, followlinks=False):
        for name in directories:
            if os.path.islink(os.path.join(current, name)):
                return ()
        for name in files:
            path = os.path.join(current, name)
            if os.path.islink(path):
                return ()
            try:
                size = os.stat(path).st_size
            except OSError:
                return ()
            found.append((Path(path).relative_to(base).as_posix(), size))
    return tuple(sorted(found))
