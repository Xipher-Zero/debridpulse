"""Deterministic durable-state fixtures for universal file-selection tests.

These helpers seed only the canonical rows a file-selection window depends on
(a transfer, a root request, a provider resource) so repository/policy contracts
can be exercised directly with an injected clock and no real timing.
"""
from __future__ import annotations

from types import SimpleNamespace

from db.database import get_db
from transfers import codec
from transfers.models import (
    FileManifest, FileManifestEntry, Ownership, ProviderResource, RequestRecord,
    SourceEntry, TransferRequest,
)


class Clock:
    """Injected core time source. Advance by assignment; never a real sleep."""

    def __init__(self, value: float = 1000.0) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> "Clock":
        self.value += float(seconds)
        return self

    def set(self, value: float) -> "Clock":
        self.value = float(value)
        return self


async def seed_window(
    *,
    transfer_hash: str,
    provider_id: str = "parcel-lab",
    request_kind: str = "parcel",
    request_payload: str = "box",
    transfer_status: str = "processing",
) -> SimpleNamespace:
    """Create torrent + root request + provider resource; return their identities."""
    request = TransferRequest(request_kind, request_payload, name="payload")
    resource_id = f"{provider_id}:{transfer_hash}"
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        transfer_id = await db.execute_returning_id(
            "INSERT INTO torrents(hash,name,status) VALUES(?,?,?)",
            (transfer_hash, "payload", transfer_status),
        )
        request_id = f"req-{transfer_hash}"
        await db.execute(
            "INSERT INTO transfer_requests(id,transfer_id,ordinal,payload,state) VALUES(?,?,?,?,'resolved')",
            (request_id, transfer_id, 0, codec.dump(request)),
        )
        resource = ProviderResource(provider_id, {"box_ticket": transfer_hash}, Ownership.CREATED, id=resource_id)
        await db.execute(
            "INSERT INTO provider_resources(id,transfer_id,provider_id,payload,state) VALUES(?,?,?,?,'available')",
            (resource_id, transfer_id, provider_id, codec.dump(resource)),
        )
        await db.commit()
    record = RequestRecord(request_id, transfer_id, request, "resolved", None, resource, 0, 0.0, None, None)
    return SimpleNamespace(
        transfer_id=transfer_id,
        request_id=request_id,
        provider_id=provider_id,
        provider_resource_id=resource_id,
        resource=resource,
        record=record,
    )


async def rebind_resource(seed, *, suffix: str) -> SimpleNamespace:
    """Simulate a re-resolution: the same durable request bound to a NEW provider
    resource. Returns a namespace with the new resource id + a RequestRecord
    carrying it (same request_id, same transfer_id)."""
    resource_id = f"{seed.provider_id}:{suffix}"
    resource = ProviderResource(seed.provider_id, {"box_ticket": suffix}, Ownership.CREATED, id=resource_id)
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "INSERT INTO provider_resources(id,transfer_id,provider_id,payload,state) VALUES(?,?,?,?,'available')",
            (resource_id, seed.transfer_id, seed.provider_id, codec.dump(resource)),
        )
        await db.execute(
            "UPDATE transfer_requests SET resource=? WHERE id=?",
            (codec.dump(resource), seed.request_id),
        )
        await db.commit()
    record = RequestRecord(seed.request_id, seed.transfer_id, seed.record.request, "resolved",
                           None, resource, 0, 0.0, None, None)
    return SimpleNamespace(
        transfer_id=seed.transfer_id,
        request_id=seed.request_id,
        provider_id=seed.provider_id,
        provider_resource_id=resource_id,
        resource=resource,
        record=record,
    )


def file_manifest(*entries: tuple[str, str, int]) -> FileManifest:
    """Build a FileManifest from ``(name, relative_path, expected_bytes)`` tuples."""
    return FileManifest(tuple(FileManifestEntry(name, path, size) for name, path, size in entries))


def executable(*entries: tuple[str, str, int]) -> tuple[SourceEntry, ...]:
    """Build the full executable SourceEntry list a provider returns later."""
    return tuple(
        SourceEntry(name, size, path, TransferRequest("parcel-member", f"member:{path}", name=name))
        for name, path, size in entries
    )
