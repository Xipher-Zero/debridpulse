from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def fsync_parent_directory(path: Path) -> None:
    """Best-effort fsync of the directory containing a credential file."""
    target = Path(path)
    dir_fd = -1
    try:
        dir_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        if dir_fd >= 0:
            os.close(dir_fd)


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    indent: int | None = None,
    separators: tuple[str, str] | None = None,
) -> None:
    """Atomically write sensitive JSON with owner-only creation permissions."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(target.parent, 0o700)
    except OSError:
        pass

    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    tmp = Path(raw_tmp)
    try:
        # ``mkstemp`` already creates the file 0600 on POSIX; this re-asserts
        # it whatever the umask. Windows has no ``os.fchmod`` and no POSIX
        # modes: there the file is protected only by the ACL it inherits from
        # its directory (the per-user data root), which this function neither
        # sets nor claims.
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(payload, handle, indent=indent, separators=separators)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except OSError:
            pass
        raise

    # Persist the directory entry update where the host filesystem supports it.
    fsync_parent_directory(target)
