"""
AllDebrid API v4 / v4.1 client.

Auth: Authorization: Bearer <apikey> header only.
All mutating calls use POST.

Key endpoints:
  POST /v4.1/magnet/status  — status (id=X for single, no id for all)
  POST /v4/magnet/status    — deprecated fallback for "get all"
  POST /v4/magnet/files     — get download links for ready magnets
  POST /v4/magnet/upload    — upload magnet URI
  POST /v4/magnet/upload/file — upload .torrent file
  POST /v4/magnet/delete    — delete magnet
  POST /v4/link/unlock      — unlock a debrid link

statusCode: 0-3=Processing, 4=Ready, 5-15=Error

NOTE: Uses a fresh aiohttp session per request to avoid ServerDisconnected
errors from AllDebrid closing keep-alive connections.
"""

import asyncio
import hashlib
import ipaddress
import json
import aiohttp
import bencode2
import logging
import socket
from typing import Optional, List, Dict, Any
from urllib.parse import urlsplit

from core.logging_utils import sanitize_exception
from core.branding import APP_SHORT_NAME
from services.rate_limit import acquire_alldebrid_request_slot

logger = logging.getLogger("alldebrid.api")

API_V4  = "https://api.alldebrid.com/v4"
API_V41 = "https://api.alldebrid.com/v4.1"

TIMEOUT = aiohttp.ClientTimeout(total=30)


class AllDebridAPIError(Exception):
    """AllDebrid API failure with a machine-readable provider error code."""

    def __init__(self, code: str, message: str):
        self.code = str(code or "UNKNOWN")
        self.message = str(message or "")
        super().__init__(f"AllDebrid [{self.code}]: {self.message}")


def validate_provider_download_url(value: object, *, context: str = "download link") -> str:
    """Validate a provider-issued URL before handing it to the local downloader.

    AllDebrid is trusted to broker the remote object, but the returned capability
    URL still crosses a network trust boundary: aria2 will resolve and connect to
    it from the DebridPulse host. Keep that boundary explicit and reject schemes,
    credentials, and literal/local destinations that should never be necessary
    for an AllDebrid download URL.
    """
    raw = str(value or "").strip()
    if not raw:
        raise Exception(f"AllDebrid returned an empty {context}")
    try:
        parsed = urlsplit(raw)
        port = parsed.port  # force validation of malformed/out-of-range ports
    except ValueError as exc:
        raise Exception(f"AllDebrid returned an invalid {context}") from exc

    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise Exception(f"AllDebrid returned a non-HTTP(S) {context}")
    if parsed.username is not None or parsed.password is not None:
        raise Exception(f"AllDebrid returned a credential-bearing {context}")
    if port is not None and not (1 <= port <= 65535):
        raise Exception(f"AllDebrid returned an invalid {context}")

    host = parsed.hostname.rstrip(".").casefold()
    if not host or "%" in host:
        raise Exception(f"AllDebrid returned an invalid {context} host")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise Exception(f"AllDebrid returned a local {context} host")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # inet_aton also recognizes legacy numeric IPv4 spellings such as
        # 2130706433 or 0x7f000001 that strict ipaddress intentionally rejects
        # but some network stacks still resolve as loopback/private addresses.
        try:
            address = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            address = None
    if address is not None and not address.is_global:
        raise Exception(f"AllDebrid returned a non-public {context} address")

    return raw


class AllDebridService:
    def __init__(self, api_key: str, agent: str = APP_SHORT_NAME):
        self.api_key = api_key
        self.agent   = agent

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _decode_json_body(self, body: str, endpoint: str) -> Dict[str, Any]:
        payload = (body or "").strip()
        if not payload:
            raise Exception(f"AllDebrid returned an empty response for {endpoint}")
        try:
            result = json.loads(payload)
        except json.JSONDecodeError as exc:
            snippet = payload[:160].replace("\n", " ").replace("\r", " ")
            raise Exception(
                f"AllDebrid returned invalid JSON for {endpoint}: {snippet or '<empty>'}"
            ) from exc
        if not isinstance(result, dict):
            raise Exception(f"AllDebrid returned unexpected payload type for {endpoint}")
        return result

    async def _post(self, base: str, endpoint: str,
                    data: Optional[Dict] = None,
                    retries: int = 1) -> Dict[str, Any]:
        url = f"{base}/{endpoint}"
        last_error: Optional[Exception] = None
        attempts = max(1, int(retries or 1))
        for attempt in range(1, attempts + 1):
            await acquire_alldebrid_request_slot()
            result = None
            try:
                async with aiohttp.ClientSession(headers=self._headers()) as s:
                    async with s.post(url, data=data or {}, timeout=TIMEOUT) as resp:
                        body = await resp.text()
                        if resp.status >= 500:
                            raise Exception(f"AllDebrid HTTP {resp.status} for {endpoint}")
                        result = self._decode_json_body(body, endpoint)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = Exception(f"Network error: {exc}")
            except Exception as exc:
                last_error = exc

            if result is not None:
                if result.get("status") != "success":
                    err  = result.get("error", {})
                    code = err.get("code", "UNKNOWN") if isinstance(err, dict) else "UNKNOWN"
                    msg  = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    raise AllDebridAPIError(code, msg)
                return result.get("data", {})

            if attempt >= attempts:
                break
            await asyncio.sleep(min(attempt, 3))

        raise last_error or Exception(f"Unknown AllDebrid error for {endpoint}")

    async def _multipart(self, endpoint: str, form: aiohttp.FormData) -> Dict[str, Any]:
        await acquire_alldebrid_request_slot()
        url = f"{API_V4}/{endpoint}"
        try:
            async with aiohttp.ClientSession(headers=self._headers()) as s:
                async with s.post(url, data=form,
                                  timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    result = self._decode_json_body(await resp.text(), endpoint)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise Exception(f"Network error uploading: {e}")
        if result.get("status") != "success":
            err = result.get("error", {})
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise Exception(f"AllDebrid upload error: {msg}")
        return result.get("data", {})

    # ── User ──────────────────────────────────────────────────────────────────

    async def get_user(self) -> Dict:
        return await self._post(API_V4, "user", retries=3)

    # ── Magnets ───────────────────────────────────────────────────────────────

    async def upload_magnet(self, magnet: str) -> Dict:
        data = await self._post(API_V4, "magnet/upload", {"magnets[]": magnet})
        magnets = data.get("magnets", [])
        if not magnets:
            raise Exception("AllDebrid returned no magnet data")
        m = magnets[0]
        if not isinstance(m, dict):
            raise Exception(f"AllDebrid returned unexpected magnet response type: {type(m).__name__}")
        err = m.get("error")
        if err:
            if isinstance(err, dict):
                code = err.get("code") or "UNKNOWN"
                raw_msg = str(err.get("message") or "")
                msg = raw_msg if not raw_msg.startswith("magnet:") else f"AllDebrid rejected the magnet (code: {code})"
            else:
                code = "UNKNOWN"
                msg = "AllDebrid rejected the magnet" if str(err).startswith("magnet:") else str(err)
            raise Exception(f"AllDebrid [{code}]: {msg}")
        if not m.get("id"):
            raise Exception("AllDebrid returned magnet data without an ID")
        return m

    async def upload_torrent_file(self, file_bytes: bytes, filename: str) -> Dict:
        form = aiohttp.FormData()
        form.add_field("files[]", file_bytes, filename=filename,
                       content_type="application/x-bittorrent")
        data = await self._multipart("magnet/upload/file", form)
        files = data.get("files", [])
        if not files:
            raise Exception("AllDebrid returned no file data after upload")
        f = files[0]
        if not isinstance(f, dict):
            raise Exception(f"AllDebrid returned unexpected file response type: {type(f).__name__}")
        err = f.get("error")
        if err:
            raise Exception(f"AllDebrid [{err.get('code')}]: {err.get('message')}")
        if not f.get("id"):
            raise Exception("AllDebrid returned file data without an ID")
        return f

    async def get_magnet_status(self, magnet_id: Optional[str] = None) -> List[Dict]:
        """
        Get status for one magnet (with id) or all magnets (no id).
        Falls back to deprecated v4 endpoint if v4.1 is discontinued.
        """
        payload = {}
        if magnet_id:
            payload["id"] = str(magnet_id)

        try:
            data = await self._post(API_V41, "magnet/status", payload, retries=3)
            raw = data.get("magnets", [])
            if isinstance(raw, dict):
                return [raw]
            return raw if isinstance(raw, list) else []
        except Exception as e:
            if magnet_id:
                raise
            err = str(e)
            if not any(kw in err for kw in
                       ("DISCONTINUED", "discontinued", "deprecated", "migrate")):
                raise
            logger.debug(f"v4.1 get-all unavailable, trying v4: {err}")

        try:
            data = await self._post(API_V4, "magnet/status", payload, retries=3)
            raw = data.get("magnets", [])
            if isinstance(raw, dict):
                return [raw]
            return raw if isinstance(raw, list) else []
        except Exception as e2:
            if any(kw in str(e2) for kw in ("DISCONTINUED", "discontinued")):
                raise Exception(
                    "AllDebrid has disabled 'list all magnets' for your account."
                )
            raise

    async def get_magnet_files(self, magnet_ids: List[str]) -> List[Dict]:
        if not magnet_ids:
            return []
        payload = {f"id[{i}]": str(mid) for i, mid in enumerate(magnet_ids)}
        data = await self._post(API_V4, "magnet/files", payload, retries=3)
        return data.get("magnets", [])

    async def delete_magnet(self, magnet_id: str) -> bool:
        try:
            await self._post(API_V4, "magnet/delete", {"id": str(magnet_id)})
            return True
        except Exception as e:
            logger.error("Delete magnet %s: %s", magnet_id, sanitize_exception(e))
            return False

    async def restart_magnet(self, magnet_id: str) -> bool:
        try:
            await self._post(API_V4, "magnet/restart", {"id": str(magnet_id)})
            return True
        except Exception as e:
            logger.error("Restart magnet %s: %s", magnet_id, sanitize_exception(e))
            return False

    async def unlock_link(self, link: str) -> Dict:
        result = await self._post(
            API_V4, "link/unlock", {"link": link}, retries=3
        )
        immediate_link = str(result.get("link") or "").strip()
        if immediate_link:
            return {
                **result,
                "link": validate_provider_download_url(
                    immediate_link, context="unlocked download link"
                ),
            }

        delayed_id = result.get("delayed")
        if delayed_id in (None, "", 0, "0"):
            if result.get("streams"):
                raise Exception(
                    "AllDebrid returned a streaming selection instead of a direct download link"
                )
            raise Exception("AllDebrid returned no download link or delayed generation ID")

        for _attempt in range(120):
            await asyncio.sleep(5)
            delayed = await self._post(
                API_V4,
                "link/delayed",
                {"id": str(delayed_id)},
                retries=3,
            )
            status = int(delayed.get("status") or 0)
            generated_link = str(delayed.get("link") or "").strip()
            if status == 2 and generated_link:
                return {
                    **result,
                    **delayed,
                    "link": validate_provider_download_url(
                        generated_link, context="delayed download link"
                    ),
                }
            if status == 3:
                raise Exception("AllDebrid delayed link generation failed")

        raise Exception("AllDebrid delayed link generation timed out after 10 minutes")

    async def close(self):
        pass


def flatten_files(nodes: List[Dict], prefix: str = "") -> List[Dict]:
    """Recursively flatten the nested file tree from /v4/magnet/files."""
    result = []
    for node in nodes:
        if node is None:
            continue
        name = node.get("n", "")
        current = f"{prefix}/{name}".strip("/") if name else prefix
        if "l" in node:
            result.append({
                "name": name,
                "path": current or name,
                "size": node.get("s", 0),
                "link": validate_provider_download_url(
                    node["l"], context="magnet file download link"
                ),
            })
        elif "e" in node and isinstance(node["e"], list):
            result.extend(flatten_files(node["e"], current))
    return result


def extract_hash_from_torrent(data: bytes) -> str:
    """
    Return the BitTorrent v1 info-hash from a validated metainfo payload.

    BitTorrent defines the v1 info-hash as SHA-1 over the bencoded ``info``
    dictionary. ``bencode2`` preserves byte strings and validates the complete
    metainfo structure before the dictionary is encoded for hashing. Invalid or
    incomplete payloads return an empty string and are never approximated with
    a byte-slicing fallback.
    """
    try:
        metainfo = bencode2.bdecode(data)
        if not isinstance(metainfo, dict):
            return ""
        info = metainfo.get(b"info")
        if not isinstance(info, dict):
            return ""
        info_bytes = bencode2.bencode(info)
        return hashlib.sha1(info_bytes, usedforsecurity=False).hexdigest()
    except Exception:
        return ""
