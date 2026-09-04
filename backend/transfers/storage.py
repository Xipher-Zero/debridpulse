"""Canonical two-domain storage health, admission, and fault normalization.

DiskCapacity began as the download low-space guard.  It remains the single
storage-capacity owner, but now models the application-state filesystem and the
download filesystem independently.  State is intentionally in-memory so a
failed SQLite filesystem is never required to persist its own failure state.
"""
from __future__ import annotations

import errno
import logging
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from transfers.errors import (
    Category,
    Domain,
    NormalizedError,
    Origin,
    Permanence,
    Recovery,
    Retryability,
    Stage,
    TransferError,
)

logger = logging.getLogger("debridpulse.storage")
_GIB = 1024 ** 3


class StorageDomain(StrEnum):
    APPLICATION_STATE = "application_state"
    DOWNLOAD = "download"


class StorageState(StrEnum):
    HEALTHY = "healthy"
    LOW_SPACE = "low_space"
    FULL = "full"
    READ_ONLY = "read_only"
    UNAVAILABLE = "unavailable"


class StorageReason(StrEnum):
    NONE = "none"
    LOW_SPACE = "low_space"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    QUOTA_EXHAUSTED = "quota_exhausted"
    READ_ONLY = "read_only"
    MISSING = "missing"
    INVALID_PATH = "invalid_path"
    INACCESSIBLE = "inaccessible"
    STAT_FAILED = "stat_failed"
    IO_ERROR = "io_error"
    SQLITE_IO_ERROR = "sqlite_io_error"
    SQLITE_OPEN_FAILED = "sqlite_open_failed"


_HARD_STATES = frozenset({StorageState.FULL, StorageState.READ_ONLY, StorageState.UNAVAILABLE})


@dataclass(frozen=True)
class StorageSnapshot:
    domain: StorageDomain
    configured_path: str
    resolved_path: str
    state: StorageState = StorageState.HEALTHY
    reason: StorageReason = StorageReason.NONE
    exists: bool | None = None
    is_directory: bool | None = None
    accessible: bool | None = None
    total_bytes: int | None = None
    free_bytes: int | None = None
    low_space_threshold_bytes: int | None = None
    recovery_threshold_bytes: int | None = None
    filesystem_id: str | None = None
    generation: int = 0
    transitioned_at: float | None = None
    probed_at: float | None = None

    def as_dict(self) -> dict:
        return {
            "domain": self.domain.value,
            "configured_path": self.configured_path,
            "resolved_path": self.resolved_path,
            "exists": self.exists,
            "is_directory": self.is_directory,
            "accessible": self.accessible,
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "free_gb": round(self.free_bytes / _GIB, 3) if self.free_bytes is not None else None,
            "low_space_threshold_bytes": self.low_space_threshold_bytes,
            "recovery_threshold_bytes": self.recovery_threshold_bytes,
            "state": self.state.value,
            "reason": self.reason.value,
            "generation": self.generation,
            "transitioned_at": self.transitioned_at,
            "probed_at": self.probed_at,
            "filesystem_id": self.filesystem_id,
        }


_STORAGE_ERROR_CATEGORY = {
    (StorageDomain.APPLICATION_STATE, StorageState.FULL): Category.APPLICATION_STORAGE_FULL,
    (StorageDomain.APPLICATION_STATE, StorageState.READ_ONLY): Category.APPLICATION_STORAGE_READ_ONLY,
    (StorageDomain.APPLICATION_STATE, StorageState.UNAVAILABLE): Category.APPLICATION_STORAGE_UNAVAILABLE,
    (StorageDomain.DOWNLOAD, StorageState.FULL): Category.DOWNLOAD_STORAGE_FULL,
    (StorageDomain.DOWNLOAD, StorageState.READ_ONLY): Category.DOWNLOAD_STORAGE_READ_ONLY,
    (StorageDomain.DOWNLOAD, StorageState.UNAVAILABLE): Category.DOWNLOAD_STORAGE_UNAVAILABLE,
}


class StorageHealthError(TransferError):
    """Structured rejection when a hard storage state makes work unsafe."""

    def __init__(self, snapshot: StorageSnapshot):
        if snapshot.state not in _HARD_STATES:
            raise ValueError("StorageHealthError requires a hard storage state")
        category = _STORAGE_ERROR_CATEGORY[(snapshot.domain, snapshot.state)]
        error = NormalizedError(
            Domain.LOCAL_RESOURCE,
            category,
            Stage.RECONCILIATION,
            retryability=Retryability.AFTER_RESOURCE_CHANGE,
            recovery=Recovery.BACKOFF,
            origin=Origin.LOCAL_SYSTEM,
            permanence=Permanence.TEMPORARY,
            context={
                "storage_domain": snapshot.domain.value,
                "storage_state": snapshot.state.value,
                "storage_reason": snapshot.reason.value,
                "generation": snapshot.generation,
            },
        )
        self.snapshot = snapshot
        self.status_code = 507 if snapshot.state == StorageState.FULL else 503
        super().__init__(error)


@dataclass(frozen=True)
class FaultClassification:
    state: StorageState
    reason: StorageReason


def _exception_chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def classify_storage_fault(exc: BaseException) -> FaultClassification | None:
    """Classify only recognized filesystem faults; generic exceptions stay generic."""
    for candidate in _exception_chain(exc):
        if not isinstance(candidate, OSError):
            continue
        code = getattr(candidate, "errno", None)
        if code == errno.ENOSPC:
            return FaultClassification(StorageState.FULL, StorageReason.CAPACITY_EXHAUSTED)
        edquot = getattr(errno, "EDQUOT", None)
        if edquot is not None and code == edquot:
            return FaultClassification(StorageState.FULL, StorageReason.QUOTA_EXHAUSTED)
        if code == errno.EROFS:
            return FaultClassification(StorageState.READ_ONLY, StorageReason.READ_ONLY)
        if code in {
            errno.ENOENT,
            errno.ENOTDIR,
            errno.EACCES,
            errno.EPERM,
            errno.EIO,
            getattr(errno, "ESTALE", -1),
            getattr(errno, "ENODEV", -1),
            getattr(errno, "ENXIO", -1),
        }:
            reason = StorageReason.IO_ERROR if code == errno.EIO else StorageReason.INACCESSIBLE
            return FaultClassification(StorageState.UNAVAILABLE, reason)
    return None


def classify_sqlite_storage_fault(exc: BaseException) -> FaultClassification | None:
    """Narrow SQLite storage classification; unrelated OperationalError is preserved."""
    filesystem = classify_storage_fault(exc)
    if filesystem is not None:
        return filesystem
    for candidate in _exception_chain(exc):
        if not isinstance(candidate, sqlite3.Error):
            continue
        message = str(candidate).strip().lower()
        if "database or disk is full" in message:
            return FaultClassification(StorageState.FULL, StorageReason.CAPACITY_EXHAUSTED)
        if "attempt to write a readonly database" in message or "readonly database" in message:
            return FaultClassification(StorageState.READ_ONLY, StorageReason.READ_ONLY)
        if "disk i/o error" in message:
            return FaultClassification(StorageState.UNAVAILABLE, StorageReason.SQLITE_IO_ERROR)
        if "unable to open database file" in message:
            return FaultClassification(StorageState.UNAVAILABLE, StorageReason.SQLITE_OPEN_FAILED)
    return None


class DiskCapacity:
    """Canonical application/download storage-health owner.

    The historical constructor remains source-compatible: ``root`` is the
    download root and the low-space threshold applies only to that domain.
    ``application_path`` is the configured SQLite file path; its parent
    filesystem is monitored independently.
    """

    def __init__(self, root, minimum_gb=0, hysteresis_gb=0.5, *, application_path=None, clock=time.time):
        self._lock = threading.RLock()
        self._clock = clock
        self.root = str(root)
        self.minimum_gb = max(0.0, float(minimum_gb or 0))
        self.hysteresis_gb = max(0.0, float(hysteresis_gb or 0))
        self.application_path = str(application_path or os.getenv("DB_PATH", "/app/data/debridpulse.db"))
        self._states: dict[StorageDomain, StorageSnapshot] = {}
        self._reset_snapshots()

    def _reset_snapshots(self) -> None:
        app_configured, app_resolved = self._paths(StorageDomain.APPLICATION_STATE)
        download_configured, download_resolved = self._paths(StorageDomain.DOWNLOAD)
        prior = getattr(self, "_states", {})
        self._states = {
            StorageDomain.APPLICATION_STATE: StorageSnapshot(
                StorageDomain.APPLICATION_STATE,
                app_configured,
                app_resolved,
                generation=prior.get(StorageDomain.APPLICATION_STATE, StorageSnapshot(StorageDomain.APPLICATION_STATE, "", "")).generation,
            ),
            StorageDomain.DOWNLOAD: StorageSnapshot(
                StorageDomain.DOWNLOAD,
                download_configured,
                download_resolved,
                low_space_threshold_bytes=int(self.minimum_gb * _GIB),
                recovery_threshold_bytes=int((self.minimum_gb + self.hysteresis_gb) * _GIB) if self.minimum_gb > 0 else None,
                generation=prior.get(StorageDomain.DOWNLOAD, StorageSnapshot(StorageDomain.DOWNLOAD, "", "")).generation,
            ),
        }

    def configure(self, root, minimum_gb=0, hysteresis_gb=0.5, *, application_path=None) -> None:
        with self._lock:
            self.root = str(root)
            self.minimum_gb = max(0.0, float(minimum_gb or 0))
            self.hysteresis_gb = max(0.0, float(hysteresis_gb or 0))
            if application_path is not None:
                self.application_path = str(application_path)
            self._reset_snapshots()

    def _paths(self, domain: StorageDomain) -> tuple[str, str]:
        if domain == StorageDomain.APPLICATION_STATE:
            configured = Path(self.application_path).expanduser()
            probe = configured.parent
        else:
            configured = Path(self.root).expanduser()
            probe = configured
        try:
            resolved = probe.resolve(strict=False)
        except OSError:
            resolved = probe.absolute()
        return str(configured), str(resolved)

    @staticmethod
    def _filesystem_identity(path: Path) -> str:
        return str(os.stat(path).st_dev)

    def _threshold_state(self, free_bytes: int, current: StorageSnapshot) -> tuple[StorageState, StorageReason]:
        if current.domain != StorageDomain.DOWNLOAD or self.minimum_gb <= 0:
            return StorageState.HEALTHY, StorageReason.NONE
        entry = int(self.minimum_gb * _GIB)
        recovery = int((self.minimum_gb + self.hysteresis_gb) * _GIB)
        if current.state == StorageState.LOW_SPACE and free_bytes < recovery:
            return StorageState.LOW_SPACE, StorageReason.LOW_SPACE
        if free_bytes <= entry:
            return StorageState.LOW_SPACE, StorageReason.LOW_SPACE
        return StorageState.HEALTHY, StorageReason.NONE

    def _probe(self, domain: StorageDomain) -> StorageSnapshot:
        now = self._clock()
        configured, resolved = self._paths(domain)
        current = self._states[domain]
        base = replace(
            current,
            configured_path=configured,
            resolved_path=resolved,
            low_space_threshold_bytes=int(self.minimum_gb * _GIB) if domain == StorageDomain.DOWNLOAD else None,
            recovery_threshold_bytes=(int((self.minimum_gb + self.hysteresis_gb) * _GIB)
                                      if domain == StorageDomain.DOWNLOAD and self.minimum_gb > 0 else None),
            probed_at=now,
        )
        path = Path(resolved)
        try:
            if not path.exists():
                return replace(base, state=StorageState.UNAVAILABLE, reason=StorageReason.MISSING,
                               exists=False, is_directory=None, accessible=False, filesystem_id=None,
                               total_bytes=None, free_bytes=None)
            is_directory = path.is_dir()
            if not is_directory:
                return replace(base, state=StorageState.UNAVAILABLE, reason=StorageReason.INVALID_PATH,
                               exists=True, is_directory=False, accessible=False, filesystem_id=None,
                               total_bytes=None, free_bytes=None)
            filesystem_id = self._filesystem_identity(path)
            accessible = os.access(path, os.R_OK | os.W_OK | os.X_OK)
            if not accessible:
                return replace(base, state=StorageState.UNAVAILABLE, reason=StorageReason.INACCESSIBLE,
                               exists=True, is_directory=True, accessible=False, filesystem_id=filesystem_id,
                               total_bytes=None, free_bytes=None)
            if hasattr(os, "statvfs"):
                flags = getattr(os.statvfs(path), "f_flag", 0)
                read_only_flag = getattr(os, "ST_RDONLY", 0)
                if read_only_flag and flags & read_only_flag:
                    usage = shutil.disk_usage(path)
                    return replace(base, state=StorageState.READ_ONLY, reason=StorageReason.READ_ONLY,
                                   exists=True, is_directory=True, accessible=True, filesystem_id=filesystem_id,
                                   total_bytes=int(usage.total), free_bytes=int(usage.free))
            usage = shutil.disk_usage(path)
            free_bytes = int(usage.free)
            if free_bytes <= 0:
                state, reason = StorageState.FULL, StorageReason.CAPACITY_EXHAUSTED
            else:
                state, reason = self._threshold_state(free_bytes, current)
            return replace(base, state=state, reason=reason, exists=True, is_directory=True, accessible=True,
                           filesystem_id=filesystem_id, total_bytes=int(usage.total), free_bytes=free_bytes)
        except OSError:
            return replace(base, state=StorageState.UNAVAILABLE, reason=StorageReason.STAT_FAILED,
                           accessible=False, total_bytes=None, free_bytes=None)

    def _apply(self, candidate: StorageSnapshot) -> StorageSnapshot:
        with self._lock:
            current = self._states[candidate.domain]
            changed = (
                current.state != candidate.state
                or current.reason != candidate.reason
                or current.generation == 0
            )
            generation = current.generation + 1 if changed else current.generation
            transitioned_at = self._clock() if changed else current.transitioned_at
            updated = replace(candidate, generation=generation, transitioned_at=transitioned_at)
            self._states[candidate.domain] = updated
        if changed and not (current.generation == 0 and updated.state == StorageState.HEALTHY):
            level = logging.INFO if updated.state == StorageState.HEALTHY else logging.WARNING
            logger.log(
                level,
                "Storage health transition: %s %s -> %s (%s)",
                updated.domain.value,
                current.state.value,
                updated.state.value,
                updated.reason.value,
            )
        return updated

    def probe(self, domain: StorageDomain) -> StorageSnapshot:
        domain = StorageDomain(domain)
        # Serialize the complete probe/update transaction with runtime fault
        # feedback so an older filesystem observation cannot overwrite a newer
        # ENOSPC/EROFS/EIO transition.
        with self._lock:
            return self._apply(self._probe(domain))

    def check(self) -> dict:
        self.probe(StorageDomain.APPLICATION_STATE)
        self.probe(StorageDomain.DOWNLOAD)
        return self.health()

    def snapshot(self, domain: StorageDomain) -> StorageSnapshot:
        with self._lock:
            return self._states[StorageDomain(domain)]

    @property
    def application_storage_permitted(self) -> bool:
        return self.snapshot(StorageDomain.APPLICATION_STATE).state not in _HARD_STATES

    @property
    def download_work_permitted(self) -> bool:
        return self.snapshot(StorageDomain.DOWNLOAD).state == StorageState.HEALTHY

    @property
    def active(self) -> bool:
        """Backward-compatible download admission signal."""
        return not self.download_work_permitted

    @property
    def shared_filesystem(self) -> bool | None:
        application = self.snapshot(StorageDomain.APPLICATION_STATE).filesystem_id
        download = self.snapshot(StorageDomain.DOWNLOAD).filesystem_id
        if application is None or download is None:
            return None
        return application == download

    def health(self) -> dict:
        application = self.snapshot(StorageDomain.APPLICATION_STATE)
        download = self.snapshot(StorageDomain.DOWNLOAD)
        result = {
            "enabled": self.minimum_gb > 0,
            "active": not self.download_work_permitted,
            "min_free_gb": self.minimum_gb,
            "free_gb": round(download.free_bytes / _GIB, 3) if download.free_bytes is not None else -1.0,
            "application_state": application.as_dict(),
            "download": download.as_dict(),
            "shared_filesystem": self.shared_filesystem,
        }
        if download.state in _HARD_STATES:
            result["error"] = "Download storage is unavailable"
        return result

    def _record_classification(self, domain: StorageDomain, classification: FaultClassification) -> StorageHealthError:
        domain = StorageDomain(domain)
        with self._lock:
            current = self._states[domain]
            candidate = replace(
                current,
                state=classification.state,
                reason=classification.reason,
                probed_at=self._clock(),
            )
            return StorageHealthError(self._apply(candidate))

    def report_fault(self, domain: StorageDomain, exc: BaseException) -> StorageHealthError | None:
        classification = classify_storage_fault(exc)
        if classification is None:
            return None
        return self._record_classification(StorageDomain(domain), classification)

    def report_application_exception(self, exc: BaseException) -> StorageHealthError | None:
        classification = classify_sqlite_storage_fault(exc)
        if classification is None:
            return None
        return self._record_classification(StorageDomain.APPLICATION_STATE, classification)

    def require_application_storage(self) -> None:
        snapshot = self.snapshot(StorageDomain.APPLICATION_STATE)
        if snapshot.state in _HARD_STATES:
            raise StorageHealthError(snapshot)


_registered_health: DiskCapacity | None = None
_registered_lock = threading.Lock()


def register_storage_health(health: DiskCapacity | None) -> None:
    global _registered_health
    with _registered_lock:
        _registered_health = health


def get_storage_health() -> DiskCapacity | None:
    with _registered_lock:
        return _registered_health


def normalize_sqlite_storage_exception(exc: BaseException) -> StorageHealthError | None:
    """Normalize a recognized SQLite storage failure through the canonical owner."""
    health = get_storage_health()
    if health is None:
        return None
    return health.report_application_exception(exc)
