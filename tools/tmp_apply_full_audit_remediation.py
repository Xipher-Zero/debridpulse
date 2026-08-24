from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text()


def write(path: str, text: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# Shared public-network guard + sampled mirror fingerprinting
# ---------------------------------------------------------------------------
network_safety = '''"""Network destination policy for provider-issued download capabilities."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
from typing import Iterable
from urllib.parse import urlsplit

import aiohttp

from services.alldebrid import validate_provider_download_url


def _public_ip(address: str) -> bool:
    normalized = str(address or "").split("%", 1)[0].strip()
    try:
        return bool(normalized) and ipaddress.ip_address(normalized).is_global
    except ValueError:
        return False


def reject_non_public_resolution(addresses: Iterable[str], *, host: str) -> None:
    normalized = {
        str(address or "").strip()
        for address in addresses
        if str(address or "").strip()
    }
    if not normalized:
        raise ValueError(f"Provider download host {host!r} did not resolve to an address")
    blocked = sorted(address for address in normalized if not _public_ip(address))
    if blocked:
        raise ValueError(
            f"Provider download host {host!r} resolved to non-public address(es): "
            + ", ".join(blocked[:4])
        )


async def validate_resolved_public_destination(uri: str) -> str:
    """Validate syntax and current DNS answers before any provider capability use."""
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


def _content_range_total(value: str) -> int:
    match = re.fullmatch(r"bytes\\s+\\d+-\\d+/(\\d+)", str(value or "").strip(), re.I)
    return int(match.group(1)) if match else 0


async def sampled_public_artifact_fingerprint(
    uri: str,
    *,
    sample_bytes: int = 64 * 1024,
    timeout_seconds: float = 20.0,
) -> str | None:
    """Return a bounded first+last-range fingerprint without following redirects.

    This is used only to strengthen near-size cross-hoster mirror identity. A
    server that redirects, ignores Range for a large object, changes total size
    between samples, or otherwise cannot be sampled safely returns ``None`` and
    the candidates remain independent physical downloads.
    """
    validated = await validate_resolved_public_destination(uri)
    sample_bytes = max(4096, int(sample_bytes))
    timeout = aiohttp.ClientTimeout(total=max(5.0, float(timeout_seconds)))

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            validated,
            headers={"Range": f"bytes=0-{sample_bytes - 1}"},
            allow_redirects=False,
        ) as response:
            if 300 <= response.status < 400:
                return None
            if response.status == 200:
                length = int(response.headers.get("Content-Length") or 0)
                if length <= 0 or length > sample_bytes:
                    return None
                body = await response.read()
                if len(body) != length:
                    return None
                digest = hashlib.sha256()
                digest.update(str(length).encode("ascii"))
                digest.update(b"\\0")
                digest.update(body)
                return digest.hexdigest()
            if response.status != 206:
                return None
            total = _content_range_total(response.headers.get("Content-Range", ""))
            if total <= 0:
                return None
            first = await response.content.read(sample_bytes + 1)
            if not first or len(first) > sample_bytes:
                return None

        if total <= len(first):
            digest = hashlib.sha256()
            digest.update(str(total).encode("ascii"))
            digest.update(b"\\0")
            digest.update(first[:total])
            return digest.hexdigest()

        last_start = max(0, total - sample_bytes)
        async with session.get(
            validated,
            headers={"Range": f"bytes={last_start}-{total - 1}"},
            allow_redirects=False,
        ) as response:
            if response.status != 206:
                return None
            if _content_range_total(response.headers.get("Content-Range", "")) != total:
                return None
            last = await response.content.read(sample_bytes + 1)
            if not last or len(last) > sample_bytes:
                return None

    digest = hashlib.sha256()
    digest.update(str(total).encode("ascii"))
    digest.update(b"\\0")
    digest.update(first)
    digest.update(b"\\0")
    digest.update(last)
    return digest.hexdigest()
'''
write("backend/services/network_safety.py", network_safety)

runtime = read("backend/services/transfer_runtime_guard.py")
runtime = runtime.replace("import ipaddress\n", "")
runtime = runtime.replace("import socket\n", "")
runtime = runtime.replace("from typing import Iterable\n", "")
runtime = runtime.replace("from urllib.parse import urlsplit\n", "")
runtime = runtime.replace("from services.alldebrid import validate_provider_download_url\n", "")
runtime = runtime.replace(
    "from services.aria2_runtime import effective_rpc_config\n",
    "from services.aria2_runtime import effective_rpc_config\nfrom services.network_safety import (\n    reject_non_public_resolution,\n    validate_resolved_public_destination,\n)\n",
)
start = runtime.index("def _public_ip(address: str) -> bool:\n")
end = runtime.index("class GuardedTransferIntegrityAria2Service", start)
runtime = runtime[:start] + runtime[end:]
write("backend/services/transfer_runtime_guard.py", runtime)

# ---------------------------------------------------------------------------
# Extraction: every format stages first; staged commit never clobbers existing
# regular files; nested extraction is restricted to files produced by the
# current archive instead of scanning unrelated destination subdirectories.
# ---------------------------------------------------------------------------
safety = read("backend/services/extraction_safety.py")
old_merge_start = safety.index("def _merge_staged_tree(stage: Path, dest: Path) -> None:\n")
old_merge_end = safety.index("\n\ndef staged_external_extract(", old_merge_start)
new_merge = '''def _merge_staged_tree(stage: Path, dest: Path) -> list[Path]:
    """Commit one validated staged tree without overwriting existing files."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    entries = sorted(
        stage.rglob("*"),
        key=lambda p: (len(p.relative_to(stage).parts), 0 if p.is_dir() else 1),
    )
    directory_targets: list[Path] = []
    file_moves: list[tuple[Path, Path]] = []

    # Preflight the entire commit before moving a single file. Existing
    # directories may be shared, but any existing file target is outside this
    # extraction operation's ownership and therefore makes the commit fail.
    for source in entries:
        relative = source.relative_to(stage)
        target = root.joinpath(*relative.parts)
        _ensure_safe_destination(root, target)
        if source.is_dir():
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise ValueError(f"Extraction directory collides with unsafe target: {target!s}")
            directory_targets.append(target)
            continue
        if target.exists() or target.is_symlink():
            raise ValueError(f"Extraction would overwrite existing file: {target!s}")
        file_moves.append((source, target))

    created_dirs: list[Path] = []
    committed: list[Path] = []
    try:
        for target in directory_targets:
            if not target.exists():
                target.mkdir(parents=True, exist_ok=False)
                created_dirs.append(target)
        for source, target in file_moves:
            target.parent.mkdir(parents=True, exist_ok=True)
            _ensure_safe_destination(root, target)
            os.replace(source, target)
            committed.append(target)
    except Exception:
        for target in reversed(committed):
            target.unlink(missing_ok=True)
        for directory in sorted(created_dirs, key=lambda p: len(p.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    return committed
'''
safety = safety[:old_merge_start] + new_merge + safety[old_merge_end:]
safety = safety.replace(
    ") -> None:\n    \"\"\"Run an external extractor in isolated staging, validate, then merge.\"\"\"",
    ") -> list[Path]:\n    \"\"\"Run an extractor in isolated staging, validate, then commit safely.\"\"\"",
    1,
)
safety = replace_once(
    safety,
    "        _merge_staged_tree(stage, dest)\n",
    "        return _merge_staged_tree(stage, dest)\n",
    label="staged extraction return manifest",
)
write("backend/services/extraction_safety.py", safety)

extractor = read("backend/services/extractor.py")
extractor = replace_once(
    extractor,
    "def _extract_sync(archive: Path, dest: Path) -> None:\n    \"\"\"Synchronous extraction dispatcher.\"\"\"",
    "def _extract_into_directory(archive: Path, dest: Path) -> None:\n    \"\"\"Extract into an operation-owned directory.\"\"\"",
    label="rename raw extraction dispatcher",
)
marker = "\n\n# ---------------------------------------------------------------------------\n# Public async API\n# ---------------------------------------------------------------------------\n"
transactional = '''\n\ndef _extract_sync(archive: Path, dest: Path) -> list[Path]:
    """Transactionally extract every supported format into the live destination."""
    return staged_external_extract(
        archive,
        dest,
        lambda stage: _extract_into_directory(archive, stage),
    )
'''
extractor = replace_once(
    extractor,
    marker,
    transactional + marker,
    label="insert transactional extraction wrapper",
)
old_nested = '''                    await loop.run_in_executor(self._executor, _extract_sync, archive, dest)
                    # Nested archive support: scan sub-directories of dest for more archives.
                    # We only scan SUBDIRECTORIES (not dest itself) to avoid treating
                    # sibling archives in the same folder as "nested" archives.
                    try:
                        nested_archives = []
                        for subdir in [d for d in dest.iterdir() if d.is_dir()]:
                            nested_archives.extend(subdir.rglob("*.rar"))
                            nested_archives.extend(subdir.rglob("*.zip"))
                            nested_archives.extend(subdir.rglob("*.7z"))
                        nested_archives = [a for a in nested_archives if a != archive]
                        if nested_archives:
                            logger.info("Found %d nested archive(s) inside %s",
                                        len(nested_archives), archive.name)
                            for nested in nested_archives[:10]:
                                try:
                                    await loop.run_in_executor(
                                        self._executor, _extract_sync, nested, nested.parent
                                    )
                                    if delete_after:
                                        nested.unlink(missing_ok=True)
                                        logger.debug("Removed nested archive %s", nested)
                                except Exception as ne:
                                    logger.warning(
                                        "Nested extraction failed for %s: %s", nested, ne
                                    )
                    except Exception as ne_scan:
                        logger.debug("Nested archive scan failed: %s", ne_scan)
'''
new_nested = '''                    created_files = await loop.run_in_executor(
                        self._executor, _extract_sync, archive, dest
                    )
                    # Nested extraction is provenance-bound: inspect only archive
                    # files committed by this extraction operation. Never walk the
                    # surrounding download tree where unrelated transfers live.
                    try:
                        dest_root = dest.resolve()
                        nested_archives = []
                        for created in created_files:
                            candidate = Path(created)
                            try:
                                relative = candidate.resolve().relative_to(dest_root)
                            except (OSError, ValueError):
                                continue
                            if len(relative.parts) > 1 and is_archive(candidate):
                                nested_archives.append(candidate)
                        if nested_archives:
                            logger.info("Found %d nested archive(s) inside %s",
                                        len(nested_archives), archive.name)
                            for nested in nested_archives[:10]:
                                try:
                                    await loop.run_in_executor(
                                        self._executor, _extract_sync, nested, nested.parent
                                    )
                                    if delete_after:
                                        nested.unlink(missing_ok=True)
                                        logger.debug("Removed nested archive %s", nested)
                                except Exception as ne:
                                    logger.warning(
                                        "Nested extraction failed for %s: %s", nested, ne
                                    )
                    except Exception as ne_scan:
                        logger.debug("Nested archive scan failed: %s", ne_scan)
'''
extractor = replace_once(
    extractor,
    old_nested,
    new_nested,
    label="replace broad nested archive scan",
)
write("backend/services/extractor.py", extractor)

# ---------------------------------------------------------------------------
# Mirror identity: exact provider size remains conservative metadata identity;
# near-size tolerance now additionally requires matching bounded content samples.
# ---------------------------------------------------------------------------
dispatch = read("backend/services/dispatch_coordinator.py")
dispatch = dispatch.replace(
    "from services.manager_v2 import DIRECT_LINK_SOURCE\n",
    "from services.manager_v2 import DIRECT_LINK_SOURCE\nfrom services.network_safety import sampled_public_artifact_fingerprint\n",
)
dispatch = dispatch.replace(
    "_MAX_MIRROR_SIZE_DELTA_BYTES = 512 * 1024 * 1024",
    "_MAX_MIRROR_SIZE_DELTA_BYTES = 4 * 1024 * 1024",
)
dispatch = dispatch.replace(
    "A much\n    larger absolute <=512 MiB ceiling exists only as a catastrophe guard for\n    very large same-name payloads; it is not intended to govern normal matching.",
    "A strict absolute <=4 MiB ceiling prevents the relative tolerance from\n    becoming permissive on very large same-name payloads. Near-size candidates\n    are additionally content-sampled before they can be collapsed.",
)
dispatch = replace_once(
    dispatch,
    "                      f.size_bytes, f.source_url, f.status, f.download_id,\n",
    "                      f.size_bytes, f.source_url, f.download_url, f.status, f.download_id,\n",
    label="select generated URLs for mirror verification",
)
old_plan = "        plan = plan_direct_link_mirror_suppression(rows)\n        if not plan:\n            return 0\n\n"
new_plan = '''        plan = plan_direct_link_mirror_suppression(rows)
        if not plan:
            return 0

        # Exact byte-size matches retain the established conservative provider
        # metadata identity. The tolerance path is stronger: small hoster size
        # variance is accepted only when bounded first+last content samples from
        # both generated capabilities produce the same fingerprint. If either
        # source cannot be sampled safely, keep both physical downloads.
        fingerprint_cache: dict[int, str | None] = {}

        async def _fingerprint(row: dict) -> str | None:
            file_id = int(row.get("file_id") or 0)
            if file_id in fingerprint_cache:
                return fingerprint_cache[file_id]
            url = str(row.get("download_url") or "").strip()
            if not url:
                fingerprint_cache[file_id] = None
                return None
            try:
                value = await sampled_public_artifact_fingerprint(url)
            except Exception as exc:
                logger.info(
                    "near-size mirror verification unavailable for file %s: %s",
                    file_id,
                    exc,
                )
                value = None
            fingerprint_cache[file_id] = value
            return value

        verified_plan: list[tuple[dict, dict]] = []
        for duplicate, primary in plan:
            primary_size = int(primary.get("size_bytes") or 0)
            duplicate_size = int(duplicate.get("size_bytes") or 0)
            if primary_size == duplicate_size:
                verified_plan.append((duplicate, primary))
                continue
            primary_fp, duplicate_fp = await asyncio.gather(
                _fingerprint(primary), _fingerprint(duplicate)
            )
            if primary_fp and duplicate_fp and primary_fp == duplicate_fp:
                duplicate["_mirror_sample_verified"] = True
                verified_plan.append((duplicate, primary))
            else:
                logger.info(
                    "near-size mirror retained independently: transfer=%s primary=%s alternate=%s",
                    duplicate.get("torrent_id"),
                    primary.get("file_id"),
                    duplicate.get("file_id"),
                )
        plan = verified_plan
        if not plan:
            return 0

'''
dispatch = replace_once(dispatch, old_plan, new_plan, label="strengthen near-size mirror identity")
dispatch = replace_once(
    dispatch,
    "                f\"size variance {delta_bytes} bytes ({delta_percent:.4f}%)\"\n",
    "                f\"size variance {delta_bytes} bytes ({delta_percent:.4f}%); \"\n                + (\"sample fingerprint matched\" if duplicate.get(\"_mirror_sample_verified\") else \"exact provider size matched\")\n",
    label="mirror evidence reason",
)
write("backend/services/dispatch_coordinator.py", dispatch)

# ---------------------------------------------------------------------------
# Failover taxonomy + redirect refusal at aria2 boundary
# ---------------------------------------------------------------------------
manager = read("backend/services/manager_v2.py")
insert_after = 'MAX_DIRECT_LINKS_PER_BATCH = 100\n\n'
helper = '''MAX_DIRECT_LINKS_PER_BATCH = 100


def _direct_link_unlock_failure_prefix(error: Exception) -> str:
    """Distinguish source-specific failures from provider/systemic failures."""
    code = str(getattr(error, "code", "") or "").strip().upper()
    if code.startswith("LINK_"):
        return "source-unlock"
    text = str(error or "").casefold()
    source_markers = (
        "delayed link generation failed",
        "returned no download link or delayed generation id",
        "streaming selection instead of a direct download link",
    )
    if any(marker in text for marker in source_markers):
        return "source-unlock"
    return "provider-unlock"

'''
manager = replace_once(manager, insert_after, helper, label="insert direct-link failure taxonomy")
manager = replace_once(
    manager,
    '                            reason=f"source-unlock: {error_text}",\n',
    '                            reason=f"{_direct_link_unlock_failure_prefix(error)}: {error_text}",\n',
    label="classify unlock failure",
)
manager = replace_once(
    manager,
    '            "allow-overwrite": "true",\n            "auto-file-renaming": "false",\n',
    '            "allow-overwrite": "true",\n            "auto-file-renaming": "false",\n            # The application validates the initial public destination. Do not\n            # let aria2 silently cross that boundary through an HTTP redirect.\n            "max-http-redirection": "0",\n',
    label="disable aria2 redirects",
)
write("backend/services/manager_v2.py", manager)

# ---------------------------------------------------------------------------
# Database migrations fail closed and verify every required runtime column.
# ---------------------------------------------------------------------------
database = read("backend/db/database.py")
old_ensure = '''async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, definition: str):
    try:
        cur = await db.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in await cur.fetchall()}
        if column not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            await db.commit()
            logger.debug("Added column %s.%s (%s)", table, column, definition)
    except Exception as exc:
        logger.warning("_ensure_column %s.%s failed (ignored): %s", table, column, exc)
'''
new_ensure = '''async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, definition: str):
    """Ensure one required runtime column exists or fail startup explicitly."""
    try:
        cur = await db.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in await cur.fetchall()}
        if column not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            await db.commit()
            logger.debug("Added column %s.%s (%s)", table, column, definition)
    except Exception as exc:
        logger.error("Required schema migration failed for %s.%s: %s", table, column, exc)
        raise RuntimeError(
            f"Required schema migration failed for {table}.{column}"
        ) from exc
'''
database = replace_once(database, old_ensure, new_ensure, label="fail closed schema migration")
old_verify = '''    async with aiosqlite.connect(DB_PATH) as verify_db:
        cur = await verify_db.execute("PRAGMA table_info(torrents)")
        cols = {row[1] for row in await cur.fetchall()}
        critical = {"priority", "label", "provider_status", "polling_failures"}
        missing = critical - cols
        if missing:
            logger.error("CRITICAL: columns still missing after migration: %s", missing)
        else:
            logger.info("SQLite schema verified — all critical columns present")
'''
new_verify = '''    async with aiosqlite.connect(DB_PATH) as verify_db:
        required = {
            "torrents": {"id", "hash", "status"} | {name for name, _ in _SCHEMA_COLUMNS_TORRENTS},
            "download_files": {"id", "torrent_id", "status", "blocked"}
            | {name for name, _ in _SCHEMA_COLUMNS_FILES},
        }
        missing_by_table: dict[str, list[str]] = {}
        for table, expected in required.items():
            cur = await verify_db.execute(f"PRAGMA table_info({table})")
            cols = {row[1] for row in await cur.fetchall()}
            missing = sorted(expected - cols)
            if missing:
                missing_by_table[table] = missing
        if missing_by_table:
            logger.error("CRITICAL: required schema remains incomplete: %s", missing_by_table)
            raise RuntimeError(f"Required SQLite schema is incomplete: {missing_by_table}")
        logger.info("SQLite schema verified — all required runtime columns present")
'''
database = replace_once(database, old_verify, new_verify, label="verify complete required schema")
write("backend/db/database.py", database)

# ---------------------------------------------------------------------------
# API inherited defects: stale progress column and post-dispatch block mutation.
# ---------------------------------------------------------------------------
routes = read("backend/api/routes.py")
routes = replace_once(
    routes,
    '                "SELECT id, filename, size_bytes, status, blocked, progress "\n',
    '                "SELECT id, filename, size_bytes, status, blocked "\n',
    label="remove nonexistent file progress column",
)
old_block = '''    async with get_db() as db:
        row = await db.fetchone(
            "SELECT id FROM download_files WHERE id=? AND torrent_id=?",
            (file_id, torrent_id),
        )
        if not row:
            raise HTTPException(404, "File not found")
        await db.execute(
            "UPDATE download_files SET blocked=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (1 if blocked else 0, file_id),
        )
        await db.commit()
    return {"ok": True, "file_id": file_id, "blocked": blocked}
'''
new_block = '''    async with get_db() as db:
        row = await db.fetchone(
            "SELECT id, status, download_id, blocked FROM download_files "
            "WHERE id=? AND torrent_id=?",
            (file_id, torrent_id),
        )
        if not row:
            raise HTTPException(404, "File not found")
        requested = bool(blocked)
        current = bool(row.get("blocked"))
        if requested == current:
            return {"ok": True, "file_id": file_id, "blocked": requested}
        status = str(row.get("status") or "").strip().lower()
        download_id = str(row.get("download_id") or "").strip()
        if download_id or status not in {"pending", "paused", "blocked", "unlocking"}:
            raise HTTPException(
                409,
                "File selection can only change before physical aria2 dispatch",
            )
        await db.execute(
            "UPDATE download_files SET blocked=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (1 if requested else 0, file_id),
        )
        await db.commit()
    return {"ok": True, "file_id": file_id, "blocked": requested}
'''
routes = replace_once(routes, old_block, new_block, label="protect post-dispatch block control")
write("backend/api/routes.py", routes)

# ---------------------------------------------------------------------------
# Tests for the newly closed audit boundaries.
# ---------------------------------------------------------------------------
tests = '''from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db.database as database
import services.direct_link_result_guard as result_guard
import services.dispatch_coordinator as dispatch_module
from core.config import AppSettings, apply_settings
from services.direct_link_result_guard import DirectLinkResultGuardManager
from services.dispatch_coordinator import collapse_direct_link_mirrors
from services.extractor import Extractor
from services.manager_v2 import _direct_link_unlock_failure_prefix
from services.alldebrid import AllDebridAPIError


@pytest.mark.asyncio
async def test_extraction_refuses_to_overwrite_existing_regular_file(tmp_path):
    dest = tmp_path / "download"
    dest.mkdir()
    existing = dest / "owned.txt"
    existing.write_text("keep-me")
    archive = dest / "incoming.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("owned.txt", "replace-me")

    extractor = Extractor()
    ok, message = await extractor.extract_archive(archive, dest, delete_after=False)

    assert ok is False
    assert "overwrite existing file" in message
    assert existing.read_text() == "keep-me"
    assert archive.exists()


@pytest.mark.asyncio
async def test_extraction_never_scans_unrelated_preexisting_nested_archive(tmp_path):
    dest = tmp_path / "download"
    dest.mkdir()
    unrelated_dir = dest / "other-transfer"
    unrelated_dir.mkdir()
    unrelated = unrelated_dir / "keep.zip"
    with zipfile.ZipFile(unrelated, "w") as zf:
        zf.writestr("unrelated.txt", "preserve")

    archive = dest / "incoming.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("created/payload.txt", "new")

    extractor = Extractor()
    ok, _message = await extractor.extract_archive(archive, dest, delete_after=True)

    assert ok is True
    assert unrelated.exists()
    assert not (unrelated_dir / "unrelated.txt").exists()
    assert (dest / "created" / "payload.txt").read_text() == "new"


@pytest.mark.asyncio
async def test_nested_archive_created_by_current_extraction_still_extracts(tmp_path):
    dest = tmp_path / "download"
    dest.mkdir()
    nested_bytes = io.BytesIO()
    with zipfile.ZipFile(nested_bytes, "w") as nested:
        nested.writestr("inside.txt", "nested-ok")
    archive = dest / "incoming.zip"
    with zipfile.ZipFile(archive, "w") as outer:
        outer.writestr("created/nested.zip", nested_bytes.getvalue())

    extractor = Extractor()
    ok, _message = await extractor.extract_archive(archive, dest, delete_after=True)

    assert ok is True
    assert (dest / "created" / "inside.txt").read_text() == "nested-ok"
    assert not (dest / "created" / "nested.zip").exists()


def test_systemic_provider_unlock_failure_is_not_source_specific():
    assert _direct_link_unlock_failure_prefix(Exception("Network error: timeout")) == "provider-unlock"
    assert _direct_link_unlock_failure_prefix(Exception("AllDebrid HTTP 503 for link/unlock")) == "provider-unlock"


def test_link_specific_provider_code_remains_source_specific():
    assert _direct_link_unlock_failure_prefix(
        AllDebridAPIError("LINK_DOWN", "resource unavailable")
    ) == "source-unlock"


@pytest.mark.asyncio
async def test_provider_unlock_failure_without_gid_is_not_failover_eligible():
    manager = DirectLinkResultGuardManager()
    eligible, reason = await manager._mirror_failure_is_failover_eligible(
        {
            "download_id": None,
            "block_reason": "provider-unlock: AllDebrid HTTP 503 for link/unlock",
            "download_url": None,
        }
    )
    assert eligible is False
    assert reason == "AllDebrid HTTP 503 for link/unlock"


def test_aria2_jobs_refuse_http_redirects():
    manager = DirectLinkResultGuardManager()
    apply_settings(AppSettings())
    assert manager._aria2_job_options()["max-http-redirection"] == "0"


class _Cursor:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount


class _MirrorDb:
    def __init__(self, rows):
        self.rows = rows

    async def fetchall(self, _sql, _params=()):
        return self.rows

    async def fetchone(self, sql, _params=()):
        if "SUM(size_bytes)" in sql:
            return {"total": sum(int(r.get("size_bytes") or 0) for r in self.rows if r.get("blocked") == 0)}
        return None

    async def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        if "mirror_state=CASE" in normalized:
            group_id, file_id = params
            row = next(r for r in self.rows if r["file_id"] == int(file_id))
            row["mirror_group_id"] = int(group_id)
            row["mirror_state"] = "active"
            return _Cursor(1)
        if "status='duplicate'" in normalized:
            reason, group_id, file_id = params
            row = next(r for r in self.rows if r["file_id"] == int(file_id))
            row.update(status="duplicate", blocked=None, block_reason=reason,
                       mirror_group_id=int(group_id), mirror_state="standby",
                       download_url=None, local_path=None)
            return _Cursor(1)
        return _Cursor(1)

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_near_size_mirror_requires_matching_sample_fingerprint(monkeypatch):
    rows = [
        {"file_id": 1, "torrent_id": 7, "filename": "a.rar", "size_bytes": 1000000,
         "source_url": "https://one.example/a", "download_url": "https://cap.example/1",
         "status": "pending", "download_id": None, "blocked": 0, "mirror_group_id": None,
         "mirror_state": "", "local_path": "/download/a.rar"},
        {"file_id": 2, "torrent_id": 7, "filename": "a.rar", "size_bytes": 1000500,
         "source_url": "https://two.example/a", "download_url": "https://cap.example/2",
         "status": "pending", "download_id": None, "blocked": 0, "mirror_group_id": None,
         "mirror_state": "", "local_path": "/download/a (2).rar"},
    ]
    db = _MirrorDb(rows)

    class _Ctx:
        async def __aenter__(self): return db
        async def __aexit__(self, *_args): return False

    monkeypatch.setattr(dispatch_module, "get_db", lambda: _Ctx())
    monkeypatch.setattr(
        dispatch_module,
        "sampled_public_artifact_fingerprint",
        AsyncMock(side_effect=["same", "same"]),
    )

    assert await collapse_direct_link_mirrors() == 1
    assert rows[1]["status"] == "duplicate"
    assert "sample fingerprint matched" in rows[1]["block_reason"]


@pytest.mark.asyncio
async def test_near_size_mirror_remains_independent_without_matching_sample(monkeypatch):
    rows = [
        {"file_id": 1, "torrent_id": 7, "filename": "a.rar", "size_bytes": 1000000,
         "source_url": "https://one.example/a", "download_url": "https://cap.example/1",
         "status": "pending", "download_id": None, "blocked": 0, "mirror_group_id": None,
         "mirror_state": "", "local_path": "/download/a.rar"},
        {"file_id": 2, "torrent_id": 7, "filename": "a.rar", "size_bytes": 1000500,
         "source_url": "https://two.example/a", "download_url": "https://cap.example/2",
         "status": "pending", "download_id": None, "blocked": 0, "mirror_group_id": None,
         "mirror_state": "", "local_path": "/download/a (2).rar"},
    ]
    db = _MirrorDb(rows)

    class _Ctx:
        async def __aenter__(self): return db
        async def __aexit__(self, *_args): return False

    monkeypatch.setattr(dispatch_module, "get_db", lambda: _Ctx())
    monkeypatch.setattr(
        dispatch_module,
        "sampled_public_artifact_fingerprint",
        AsyncMock(side_effect=["first", "different"]),
    )

    assert await collapse_direct_link_mirrors() == 0
    assert rows[1]["status"] == "pending"


def test_current_schema_contract_includes_extraction_and_mirror_columns():
    required_torrent = {name for name, _ in database._SCHEMA_COLUMNS_TORRENTS}
    required_files = {name for name, _ in database._SCHEMA_COLUMNS_FILES}
    assert {"extraction_status", "extraction_error"} <= required_torrent
    assert {"mirror_group_id", "mirror_state"} <= required_files
'''
write("backend/tests/test_full_audit_remediation_20260824.py", tests)

# Add API source contract assertions without needing a TestClient lifecycle.
v1 = read("backend/tests/test_v1_scope.py")
append = '''\n\ndef test_inherited_file_preview_and_block_routes_are_hardened():
    routes = (REPO_ROOT / "backend/api/routes.py").read_text()
    assert "size_bytes, status, blocked, progress" not in routes
    block_route = routes.split('async def block_file(torrent_id: int, file_id: int, blocked: bool = True):', 1)[1].split('@router.get("/torrents/{torrent_id}")', 1)[0]
    assert "download_id" in block_route
    assert "status not in" in block_route
    assert "409" in block_route
'''
if "test_inherited_file_preview_and_block_routes_are_hardened" not in v1:
    v1 += append
write("backend/tests/test_v1_scope.py", v1)

# Release identity: this branch is now materially beyond 1.0.6.
write("VERSION", "1.0.7\n")
changelog = read("CHANGELOG.md")
section = '''# Changelog

## [1.0.7] — 2026-08-24

### Semantic transfer integrity and audit remediation

- Added cross-hoster mirror normalization with retained automatic failover standbys, logical result authority, source-exhaustion history, and plain-success semantics when an alternate completes the artifact.
- Added visible post-download extraction lifecycle reporting, RAR5 support, operator-visible extraction failures, and delete/retain archive policy handling.
- Made extraction transactional across all supported archive formats, rejected collisions with pre-existing regular files, and restricted nested extraction to archives produced by the current extraction operation.
- Split direct-link unlock failures into source-specific versus provider/systemic classes so an AllDebrid outage or local provider-connectivity problem cannot consume valid mirror standbys.
- Strengthened near-size mirror identity with a 4 MiB absolute variance ceiling plus bounded first/last content fingerprints; unverifiable near-size candidates remain independent downloads.
- Disabled HTTP redirects for DebridPulse-created aria2 jobs after public-destination validation, preventing a provider capability from silently redirecting aria2 into a different network destination.
- Made required SQLite migrations fail closed and verify extraction/mirror runtime columns before startup succeeds.
- Fixed inherited file-preview schema drift and prevented file-selection mutations after physical aria2 dispatch.
- Preserved the existing external-aria2 ownership boundary, pause/resume semantics, browser capability redaction, and V1 scope while adding regression coverage for the new audit boundaries.

'''
if "## [1.0.7]" not in changelog:
    changelog = changelog.replace("# Changelog\n\n", section, 1)
write("CHANGELOG.md", changelog)

print("Full audit remediation patch applied")
