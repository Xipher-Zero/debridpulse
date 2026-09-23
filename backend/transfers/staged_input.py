"""The one owner of durable, large, user-submitted request input.

A submitted request payload is ordinarily small enough to live inside the
request itself. Some are not: a manifest describing a terabyte-scale source can
itself be hundreds of megabytes. Carrying such a payload inside the canonical
request/candidate JSON costs a base64 expansion plus a JSON string plus a
database row plus every decoded copy along the way -- measured at 1.7 GiB of
peak resident memory for one 256 MiB submitted manifest, and ~358 MB of context
text per candidate.

This module is the neutral alternative, and the whole of it:

    payload bytes  ->  StagedInputStore.stage(...)  ->  StagedPayload
                                                        (id, sha256, length)

The reference is small, non-secret and restart-safe, so it travels wherever the
payload used to -- the durable request, the candidate context -- while the bytes
stay on disk and are read by streaming.

It is deliberately protocol-neutral. Nothing here knows what any request class,
provider or executor is, or what the bytes mean; anything with a large
submitted input uses it unchanged. It is *input*, and never the execution
footprint, the download output, an executor working directory, the final
materialization or a post-processing workspace.

Integrity is not advisory. A reference records the digest and length of exactly
the bytes that were staged, and every read re-verifies both BEFORE the caller
may consume them. Altered or truncated input fails closed rather than being
submitted.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

# The finite infrastructure ceiling for one staged request input.
#
# The product contract is "at least 256 MiB". This bound is higher, and is a
# resource guard rather than a product limit: streamed staging and bounded
# parsing were measured at ~18 MiB of peak resident memory for a 256 MiB input,
# effectively constant in input size, so the ceiling exists only to keep a
# pathological or runaway upload finite. It bounds the SUBMITTED MANIFEST, and
# has nothing to do with the size of the payload that manifest describes.
MAX_STAGED_INPUT_BYTES = 1024 * 1024 * 1024

# How long a staged input that no durable request references is kept before the
# sweep may reclaim it. It covers the window between staging an upload and the
# transfer that owns it becoming durable.
ORPHAN_GRACE_SECONDS = 3600

_READ_CHUNK = 1024 * 1024
_IDENTITY = re.compile(r"^[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class StagedInputError(Exception):
    """Staged input could not be created, located or proven intact."""


@dataclass(frozen=True)
class StagedPayload:
    """A durable reference to one staged request payload.

    Carries no path. ``id`` is an opaque token that the store alone resolves
    against its own root, so a reference can never express a filesystem
    location and can never be made to escape one.
    """
    id: str
    sha256: str
    byte_length: int

    def __post_init__(self):
        if not _IDENTITY.match(str(self.id or "")):
            raise StagedInputError("staged input identity is not well formed")
        if not _DIGEST.match(str(self.sha256 or "")):
            raise StagedInputError("staged input digest is not well formed")
        if int(self.byte_length) < 0:
            raise StagedInputError("staged input length is negative")

    def as_context(self) -> dict:
        """The neutral, non-secret projection carried on a candidate.

        A plain JSON object of three scalars. It embeds no payload, names no
        provider or executor, and asks no owner to interpret it.
        """
        return {"id": self.id, "sha256": self.sha256, "byte_length": int(self.byte_length)}

    @classmethod
    def from_context(cls, value) -> "StagedPayload":
        if not isinstance(value, dict):
            raise StagedInputError("staged input reference is not an object")
        try:
            return cls(str(value["id"]), str(value["sha256"]), int(value["byte_length"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise StagedInputError("staged input reference is incomplete") from exc


class StagedInputStore:
    """The lifecycle owner: creation, retrieval, integrity, and reclamation."""

    def __init__(self, root: str, *, max_bytes: int = MAX_STAGED_INPUT_BYTES):
        self._root = Path(root)
        self._max_bytes = max(1, int(max_bytes))

    @property
    def root(self) -> Path:
        return self._root

    # --- location -------------------------------------------------------

    def _path(self, identity: str) -> Path:
        """Resolve a reference to its file. The ONLY path construction here.

        ``identity`` is validated as 32 hex characters, so no separator, no
        parent segment and no absolute path can survive into the join.
        """
        if not _IDENTITY.match(str(identity or "")):
            raise StagedInputError("staged input identity is not well formed")
        return self._root / f"{identity}.input"

    # --- creation -------------------------------------------------------

    def _writer(self) -> "_StagedWriter":
        self._root.mkdir(parents=True, exist_ok=True)
        identity = secrets.token_hex(16)
        final = self._path(identity)
        return _StagedWriter(identity, final, final.with_suffix(".partial"), self._max_bytes)

    async def stage(self, chunks) -> StagedPayload:
        """Stream ``chunks`` (an async iterable of bytes) into durable storage.

        The payload is never accumulated in memory: it is written through, and
        its digest computed, one chunk at a time. The file becomes visible under
        its final name only once it is complete and flushed, so a crash
        mid-upload can leave a temporary file -- which the sweep reclaims -- but
        never a short one that looks whole.
        """
        writer = self._writer()
        try:
            async for chunk in chunks:
                writer.write(chunk)
            return writer.commit()
        except BaseException:
            writer.abort()
            raise

    def stage_bytes(self, payload: bytes) -> StagedPayload:
        """Stage a payload a caller already holds whole.

        The same writer, so staged bytes are produced identically however they
        arrived. Synchronous, because the write itself always was.
        """
        writer = self._writer()
        try:
            writer.write(bytes(payload))
            return writer.commit()
        except BaseException:
            writer.abort()
            raise

    # --- reading --------------------------------------------------------

    def verify(self, reference: StagedPayload) -> None:
        """Prove the staged bytes are exactly the bytes that were staged.

        Streams the file once, comparing length and digest. Raises unless both
        match, so a caller can never consume altered or truncated input.
        """
        path = self._path(reference.id)
        digest = hashlib.sha256()
        length = 0
        try:
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(_READ_CHUNK)
                    if not chunk:
                        break
                    length += len(chunk)
                    digest.update(chunk)
        except OSError as exc:
            raise StagedInputError("staged input is no longer available") from exc
        if length != int(reference.byte_length):
            raise StagedInputError("staged input length does not match its reference")
        if digest.hexdigest() != reference.sha256:
            raise StagedInputError("staged input digest does not match its reference")

    @contextlib.contextmanager
    def opened(self, reference: StagedPayload):
        """Yield a readable file positioned at the start, proven intact first.

        Verification happens BEFORE the handle is yielded, so nothing altered
        is ever partially consumed by a caller that streams as it sends.
        """
        self.verify(reference)
        handle = open(self._path(reference.id), "rb")
        try:
            yield handle
        finally:
            handle.close()

    def read(self, reference: StagedPayload) -> bytes:
        """The whole payload, proven intact. Only for callers that need it all."""
        with self.opened(reference) as handle:
            return handle.read()

    # --- reclamation ----------------------------------------------------

    def discard(self, reference: StagedPayload | str) -> None:
        """Remove one staged input. Idempotent."""
        identity = reference.id if isinstance(reference, StagedPayload) else reference
        with contextlib.suppress(OSError, StagedInputError):
            self._path(identity).unlink()

    def sweep(self, referenced_ids, *, grace_seconds: int = ORPHAN_GRACE_SECONDS,
              now: float | None = None) -> int:
        """Reclaim every staged input no live request still references.

        This is the single cleanup owner for every terminal path -- success,
        permanent failure, cancellation, deletion, retry -- and for the crash
        that leaves an input staged before any transfer ever owned it. Deriving
        the survivors from the durable reference set rather than from a
        per-path callback is what makes it impossible to miss one path or to
        reclaim an input that is still needed.

        ``grace_seconds`` protects the window between staging an upload and the
        transfer that references it becoming durable.
        """
        try:
            entries = list(self._root.iterdir())
        except OSError:
            return 0
        live = {str(item) for item in referenced_ids}
        moment = time.time() if now is None else now
        reclaimed = 0
        for entry in entries:
            if entry.suffix not in (".input", ".partial"):
                continue
            identity = entry.name.split(".", 1)[0]
            if entry.suffix == ".input" and identity in live:
                continue
            try:
                if moment - entry.stat().st_mtime < max(0, int(grace_seconds)):
                    continue
                entry.unlink()
                reclaimed += 1
            except OSError:
                continue
        return reclaimed


class _StagedWriter:
    """The one place staged bytes are written, hashed, counted and published."""

    def __init__(self, identity: str, final: Path, temporary: Path, max_bytes: int):
        self._identity = identity
        self._final = final
        self._temporary = temporary
        self._max_bytes = max_bytes
        self._digest = hashlib.sha256()
        self._length = 0
        self._handle = open(temporary, "wb")

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._length += len(chunk)
        if self._length > self._max_bytes:
            raise StagedInputError(
                f"staged input exceeds the {self._max_bytes} byte ceiling")
        self._digest.update(chunk)
        self._handle.write(chunk)

    def commit(self) -> StagedPayload:
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        if not self._length:
            raise StagedInputError("staged input is empty")
        os.replace(self._temporary, self._final)
        os.chmod(self._final, 0o600)
        return StagedPayload(self._identity, self._digest.hexdigest(), self._length)

    def abort(self) -> None:
        with contextlib.suppress(OSError):
            self._handle.close()
        with contextlib.suppress(OSError):
            self._temporary.unlink()
