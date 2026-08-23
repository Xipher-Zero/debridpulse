"""Final V1 transfer-boundary guards.

This layer deliberately wraps the validated transfer-integrity engine instead of
editing its manifest reconciliation algorithm. It closes boundaries that need
cross-operation authority:

* a historical completed row never blocks an explicit same-hash re-submit;
* Delete and provider-ready materialization are serialized per transfer so a
  background task cannot resurrect an operator-deleted transaction;
* distinct provider files must map to distinct sanitized local destinations;
* provider-issued download hostnames are resolved immediately before aria2
  dispatch and rejected when any current answer is non-public;
* an explicit operator request to delete from AllDebrid is honored independently
  of automatic ownership-based completion cleanup.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import urlsplit

from core.config import get_settings
from db.database import get_db
from services.alldebrid import validate_provider_download_url
from services.aria2_runtime import effective_rpc_config
from services.manager_v2 import (
    DIRECT_LINK_SOURCE,
    READY_CODE,
    extract_hash,
    is_blocked,
    safe_name,
    safe_rel_path,
)
from services.transfer_integrity import (
    TransferIntegrityAria2Service,
    TransferIntegrityManager,
)

logger = logging.getLogger("debridpulse.transfer_runtime_guard")

_MATERIALIZATION_CONTEXT: ContextVar[tuple[int, str] | None] = ContextVar(
    "debridpulse_materialization_context", default=None
)


def _public_ip(address: str) -> bool:
    """Return True only for globally routable IP literals."""
    normalized = str(address or "").split("%", 1)[0].strip()
    try:
        return bool(normalized) and ipaddress.ip_address(normalized).is_global
    except ValueError:
        return False


def reject_non_public_resolution(addresses: Iterable[str], *, host: str) -> None:
    """Fail closed when DNS returns no address or any non-global destination."""
    normalized = {str(address or "").strip() for address in addresses if str(address or "").strip()}
    if not normalized:
        raise ValueError(f"Provider download host {host!r} did not resolve to an address")
    blocked = sorted(address for address in normalized if not _public_ip(address))
    if blocked:
        raise ValueError(
            f"Provider download host {host!r} resolved to non-public address(es): "
            + ", ".join(blocked[:4])
        )


async def validate_resolved_public_destination(uri: str) -> str:
    """Validate provider URL syntax plus the hostname's current DNS answers.

    The provider URL validator already rejects local IP literals and unsafe
    schemes. This additional check closes split-DNS/private-resolution cases at
    the last application-controlled boundary before aria2 resolves the host for
    its own connection.
    """
    validated = validate_provider_download_url(uri, context="aria2 download link")
    parsed = urlsplit(validated)
    host = str(parsed.hostname or "").rstrip(".").casefold()
    if not host:
        raise ValueError("Provider download URL has no hostname")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if not literal.is_global:
            raise ValueError(f"Provider download host {host!r} is not public")
        return validated

    port = int(parsed.port or (443 if parsed.scheme.casefold() == "https" else 80))
    loop = asyncio.get_running_loop()
    try:
        answers = await loop.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise ValueError(f"Provider download host {host!r} could not be resolved") from exc

    reject_non_public_resolution(
        (entry[4][0] for entry in answers if entry and len(entry) >= 5 and entry[4]),
        host=host,
    )
    return validated


class GuardedTransferIntegrityAria2Service(TransferIntegrityAria2Service):
    """Apply destination-network policy immediately before aria2 dispatch."""

    async def ensure_download(self, uri: str, *args, **kwargs) -> str:
        validated = await validate_resolved_public_destination(uri)
        return await super().ensure_download(validated, *args, **kwargs)


class GuardedTransferIntegrityManager(TransferIntegrityManager):
    """Transfer-integrity engine with explicit cross-operation lifecycle guards."""

    def __init__(self):
        super().__init__()
        self._transfer_lifecycle_locks: dict[int, asyncio.Lock] = {}

    def aria2(self):
        if self._aria2 is None:
            cfg = get_settings()
            url, secret = effective_rpc_config(cfg)
            self._aria2 = GuardedTransferIntegrityAria2Service(
                url,
                secret,
                cfg.aria2_operation_timeout_seconds,
            )
        return self._aria2

    def _lifecycle_lock(self, torrent_id: int) -> asyncio.Lock:
        torrent_id = int(torrent_id)
        lock = self._transfer_lifecycle_locks.get(torrent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._transfer_lifecycle_locks[torrent_id] = lock
        return lock

    async def _load_transfer_row(self, torrent_id: int):
        async with get_db() as db:
            return await db.fetchone(
                "SELECT id, hash, name, status, source, alldebrid_id "
                "FROM torrents WHERE id=?",
                (int(torrent_id),),
            )

    async def _completed_transfer_by_hash(self, hash_value: str):
        if not hash_value:
            return None
        async with get_db() as db:
            return await db.fetchone(
                "SELECT id, hash, name, status, source, alldebrid_id "
                "FROM torrents WHERE hash=? AND status='completed' LIMIT 1",
                (str(hash_value).lower(),),
            )

    @staticmethod
    def _history_duplicate_payload(row) -> dict:
        return {
            "is_duplicate": True,
            "confidence": 1.0,
            "action": "warn",
            "reason": "same_infohash_completed_history",
            "matches": [
                {
                    "torrent_id": int(row["id"]),
                    "name": str(row["name"] or ""),
                    "status": "completed",
                    "hash": str(row["hash"] or ""),
                    "reason": "same_infohash",
                    "confidence": 1.0,
                }
            ],
        }

    async def _clear_historical_completion(self, torrent_id: int) -> None:
        async with get_db() as db:
            await db.execute(
                """UPDATE torrents
                   SET completed_at=NULL, progress=0, error_message=NULL,
                       polling_failures=0, updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status!='deleted'""",
                (int(torrent_id),),
            )
            await db.commit()

    async def _reacquire_completed_magnet(
        self,
        magnet: str,
        hash_value: str,
        source: str,
        history_row,
    ) -> dict:
        """Explicit re-submit revives completed history instead of treating it as possession."""
        if self.is_paused():
            result = await self._persist_deferred_magnet(magnet, hash_value, source)
            result["_duplicate"] = self._history_duplicate_payload(history_row)
            return result

        async with self._upload_sem:
            if self.is_paused():
                result = await self._persist_deferred_magnet(magnet, hash_value, source)
                result["_duplicate"] = self._history_duplicate_payload(history_row)
                return result
            provider_result = await self.ad().upload_magnet(magnet)

        ad_id = str(provider_result.get("id", ""))
        name = (
            provider_result.get("name")
            or provider_result.get("filename")
            or hash_value[:16]
        )
        normalized_hash = str(provider_result.get("hash") or hash_value).lower()
        row = await self._upsert(normalized_hash, magnet, name, ad_id, source)
        torrent_id = int(row["id"])
        await self._clear_historical_completion(torrent_id)
        row = {**row, "completed_at": None, "progress": 0, "error_message": None}
        row["_duplicate"] = self._history_duplicate_payload(history_row)

        if get_settings().discord_notify_added:
            await self.notify().send_added(name, source=source, alldebrid_id=ad_id)

        status_code = int(
            provider_result.get("statusCode")
            or provider_result.get("status_code")
            or 0
        )
        if status_code == READY_CODE:
            self._schedule_ready_parent_download(torrent_id, ad_id, str(name))
        return row

    async def add_magnet_direct(self, magnet: str, source: str = "manual") -> dict:
        hash_value = extract_hash(magnet)
        if not hash_value:
            raise ValueError("Invalid magnet: no btih hash found")
        history_row = await self._completed_transfer_by_hash(hash_value)
        if history_row is not None:
            logger.info(
                "Explicit same-hash re-submit reviving completed transfer %s",
                history_row["id"],
            )
            return await self._reacquire_completed_magnet(
                magnet,
                hash_value,
                source,
                history_row,
            )
        return await super().add_magnet_direct(magnet, source=source)

    async def add_torrent_file_direct(
        self,
        file_bytes: bytes,
        filename: str,
        source: str = "manual",
        preferred_hash: str | None = None,
    ) -> dict:
        if not file_bytes or not get_settings().alldebrid_api_key:
            return await super().add_torrent_file_direct(
                file_bytes,
                filename,
                source=source,
                preferred_hash=preferred_hash,
            )

        local_hash = str(preferred_hash or "").strip().lower()
        if not local_hash:
            try:
                from services.alldebrid import extract_hash_from_torrent

                local_hash = str(extract_hash_from_torrent(file_bytes) or "").lower()
            except Exception:
                local_hash = ""

        history_row = await self._completed_transfer_by_hash(local_hash)
        if history_row is None:
            return await super().add_torrent_file_direct(
                file_bytes,
                filename,
                source=source,
                preferred_hash=preferred_hash,
            )

        logger.info(
            "Explicit same-hash .torrent re-submit reviving completed transfer %s",
            history_row["id"],
        )
        row = await self._upload_torrent_file_provider(
            file_bytes,
            filename,
            source,
            local_hash,
        )
        if row.get("id"):
            await self._clear_historical_completion(int(row["id"]))
            row = {
                **row,
                "completed_at": None,
                "progress": 0,
                "error_message": None,
            }
        row["_duplicate"] = self._history_duplicate_payload(history_row)
        return row

    @staticmethod
    def _validate_manifest_destinations(name: str, flat_files: list[dict]) -> None:
        """Require a one-to-one mapping from required provider files to local paths."""
        cfg = get_settings()
        destination_root = Path(cfg.download_folder) / safe_name(name)
        torrent_root = safe_name(name)
        claimed: dict[str, tuple[str, str, int]] = {}

        for file_info in flat_files:
            relative_path = (
                file_info.get("path")
                or file_info.get("name")
                or "download.bin"
            )
            display_name = str(
                PurePosixPath(str(relative_path).replace("\\", "/"))
            )
            file_size = int(file_info.get("size", 0) or 0)
            blocked, _reason = is_blocked(display_name, cfg, file_size)
            if blocked:
                continue

            relative_target = safe_rel_path(display_name)
            if relative_target.parts and relative_target.parts[0] == torrent_root:
                remaining = relative_target.parts[1:]
                relative_target = (
                    Path(*remaining) if remaining else Path("download.bin")
                )
            local_path = destination_root / relative_target
            path_key = str(
                PurePosixPath(str(local_path).replace("\\", "/"))
            )
            identity = (
                display_name,
                str(file_info.get("link") or "").strip(),
                file_size,
            )
            previous = claimed.get(path_key)
            if previous is not None and previous != identity:
                raise ValueError(
                    "Provider manifest path collision after sanitization: "
                    f"{previous[0]!r} and {display_name!r} both map to {path_key!r}"
                )
            claimed[path_key] = identity

    async def _fetch_ready_files(self, ad_id: str):
        flat_files = await super()._fetch_ready_files(ad_id)
        context = _MATERIALIZATION_CONTEXT.get()
        if context is not None:
            self._validate_manifest_destinations(context[1], flat_files)
        return flat_files

    async def _engine_download(self, torrent_id: int, ad_id: str, name: str):
        """Serialize materialization against Delete without changing engine ordering."""
        async with self._lifecycle_lock(torrent_id):
            row = await self._load_transfer_row(torrent_id)
            if row is None or str(row["status"] or "") == "deleted":
                return

            token = _MATERIALIZATION_CONTEXT.set((int(torrent_id), str(name or "")))
            try:
                return await super()._engine_download(torrent_id, ad_id, name)
            finally:
                _MATERIALIZATION_CONTEXT.reset(token)

    async def delete_torrent(self, torrent_id: int, delete_from_ad: bool = True):
        """Make explicit operator deletion the final authority for this transfer."""
        torrent_id = int(torrent_id)
        async with self._lifecycle_lock(torrent_id):
            row = await self._load_transfer_row(torrent_id)
            if row is None:
                raise ValueError("Torrent not found")

            ad_id = str(row["alldebrid_id"] or "").strip()
            source = str(row["source"] or "").strip()
            status = str(row["status"] or "").strip()
            if delete_from_ad and ad_id and source != DIRECT_LINK_SOURCE:
                deleted = await self.ad().delete_magnet(ad_id)
                already_cleaned_owned_completion = (
                    status == "completed"
                    and self._provider_delete_authorized(source)
                )
                if not deleted and not already_cleaned_owned_completion:
                    raise RuntimeError(
                        f"AllDebrid deletion was not confirmed for transfer {torrent_id}"
                    )

            return await super().delete_torrent(
                torrent_id,
                delete_from_ad=False,
            )


manager = GuardedTransferIntegrityManager()
