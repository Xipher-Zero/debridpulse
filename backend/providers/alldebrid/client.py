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
import json
import aiohttp
import logging
from typing import Optional, List, Dict, Any

from services.network_safety import validate_provider_download_url
from core.logging_utils import sanitize_exception
from core.branding import APP_SHORT_NAME
from providers.alldebrid.rate_limit import acquire_alldebrid_request_slot

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
                    data: Optional[Dict] = None) -> Dict[str, Any]:
        """One native operation; retry and ambiguous outcomes belong to core."""
        url = f"{base}/{endpoint}"
        await acquire_alldebrid_request_slot()
        async with aiohttp.ClientSession(headers=self._headers()) as session:
            async with session.post(url, data=data or {}, timeout=TIMEOUT) as response:
                body = await response.text()
                if response.status >= 500:
                    raise Exception(f"AllDebrid HTTP {response.status} for {endpoint}")
                result = self._decode_json_body(body, endpoint)
        if result.get("status") != "success":
            err = result.get("error", {})
            code = err.get("code", "UNKNOWN") if isinstance(err, dict) else "UNKNOWN"
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise AllDebridAPIError(code, msg)
        return result.get("data", {})

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
        return await self._post(API_V4, "user")

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
                # AllDebrid sometimes echoes the magnet URL as the error message — replace with a clear description
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
            data = await self._post(API_V41, "magnet/status", payload)
            raw = data.get("magnets", [])
            if isinstance(raw, dict):
                return [raw]
            return raw if isinstance(raw, list) else []
        except Exception as e:
            if magnet_id:
                raise  # per-ID failure is a real error
            err = str(e)
            if not any(kw in err for kw in
                       ("DISCONTINUED", "discontinued", "deprecated", "migrate")):
                raise
            logger.debug(f"v4.1 get-all unavailable, trying v4: {err}")

        # Fallback: deprecated /v4/magnet/status
        try:
            data = await self._post(API_V4, "magnet/status", payload)
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
        data = await self._post(API_V4, "magnet/files", payload)
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
            API_V4, "link/unlock", {"link": link}
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

        # AllDebrid requires delayed generations to be polled no faster than
        # every five seconds. Keep the filename/size returned by link/unlock
        # and merge the final URL when generation completes.
        for _attempt in range(120):
            await asyncio.sleep(5)
            delayed = await self._post(
                API_V4,
                "link/delayed",
                {"id": str(delayed_id)},
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
        pass  # no persistent session to close


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

