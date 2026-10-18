"""
AllDebrid API v4 / v4.1 client.

Auth: Authorization: Bearer <apikey> header only.
All mutating calls use POST.

Key endpoints:
  POST /v4.1/user/hosts     — account-usable supported-host inventory
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
from providers.alldebrid.rate_limit import TokenBucketRateLimiter

logger = logging.getLogger("alldebrid.api")

API_V4  = "https://api.alldebrid.com/v4"
API_V41 = "https://api.alldebrid.com/v4.1"

TIMEOUT = aiohttp.ClientTimeout(total=30)
# A native response is decoded only up to this size, the bound every other
# debrid client keeps: a complete account status or host inventory is far
# smaller, so anything larger is malformed, not data.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# The most one read of a response body asks for.
_READ_CHUNK_BYTES = 64 * 1024


class AllDebridAPIError(Exception):
    """AllDebrid API failure with a machine-readable provider error code."""

    def __init__(self, code: str, message: str):
        self.code = str(code or "UNKNOWN")
        self.message = str(message or "")
        super().__init__(f"AllDebrid [{self.code}]: {self.message}")


async def _bounded_body(stream: aiohttp.StreamReader) -> bytes:
    """The whole (decoded) body: read until it ends, or until it is past
    ``MAX_RESPONSE_BYTES`` -- all an oversized answer needs to show. One read
    returns only what has arrived so far, never the body. A failure before
    the end raises, so a partial body is never taken for an answer; the
    request's total timeout bounds every read here."""
    body = bytearray()
    while len(body) <= MAX_RESPONSE_BYTES:
        chunk = await stream.read(min(_READ_CHUNK_BYTES, MAX_RESPONSE_BYTES + 1 - len(body)))
        if not chunk:
            break
        body += chunk
    return bytes(body)


async def _response_text(response: aiohttp.ClientResponse, endpoint: str) -> str:
    """The text of one AllDebrid answer, never a redirect's and never more
    than ``MAX_RESPONSE_BYTES``. Redirects are not followed: following one
    would replay the request -- its body included -- to wherever the
    ``Location`` points and take that origin's answer as AllDebrid's."""
    if 300 <= response.status < 400:
        raise Exception(
            f"AllDebrid returned an unexpected payload for {endpoint}: a redirect (HTTP {response.status}), "
            "which is never followed"
        )
    body = await _bounded_body(response.content)
    if len(body) > MAX_RESPONSE_BYTES:
        raise Exception(f"AllDebrid returned an unexpected payload for {endpoint}: an oversized response")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        raise Exception(f"AllDebrid returned invalid JSON for {endpoint}: not UTF-8") from None


class AllDebridService:
    def __init__(self, api_key: str, agent: str = APP_SHORT_NAME, *,
                 rate_limit_per_minute: int = 60, rate_limiter=None):
        self.api_key = api_key
        self.agent   = agent
        self._rate_limiter = rate_limiter or TokenBucketRateLimiter(
            rate=rate_limit_per_minute, window=60.0,
        )

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _decode_json_body(self, body: str, endpoint: str) -> Dict[str, Any]:
        payload = (body or "").strip()
        if not payload:
            raise Exception(f"AllDebrid returned an empty response for {endpoint}")
        try:
            result = json.loads(payload)
        except json.JSONDecodeError:
            # The body itself is never echoed: it is not AllDebrid's to vouch for.
            raise Exception(
                f"AllDebrid returned invalid JSON for {endpoint} ({len(payload)} characters)"
            ) from None
        if not isinstance(result, dict):
            raise Exception(f"AllDebrid returned unexpected payload type for {endpoint}")
        return result

    async def _post(self, base: str, endpoint: str,
                    data: Optional[Dict] = None) -> Dict[str, Any]:
        """One native operation; retry and ambiguous outcomes belong to core."""
        url = f"{base}/{endpoint}"
        await self._rate_limiter.acquire()
        async with aiohttp.ClientSession(headers=self._headers()) as session:
            async with session.post(url, data=data or {}, timeout=TIMEOUT, allow_redirects=False) as response:
                if response.status >= 500:
                    raise Exception(f"AllDebrid HTTP {response.status} for {endpoint}")
                result = self._decode_json_body(await _response_text(response, endpoint), endpoint)
        if result.get("status") != "success":
            err = result.get("error", {})
            code = err.get("code", "UNKNOWN") if isinstance(err, dict) else "UNKNOWN"
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise AllDebridAPIError(code, msg)
        return result.get("data", {})

    async def _multipart(self, endpoint: str, form: aiohttp.FormData) -> Dict[str, Any]:
        await self._rate_limiter.acquire()
        url = f"{API_V4}/{endpoint}"
        try:
            async with aiohttp.ClientSession(headers=self._headers()) as s:
                async with s.post(url, data=form, allow_redirects=False,
                                  timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    result = self._decode_json_body(await _response_text(resp, endpoint), endpoint)
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

    async def get_user_hosts(self) -> Dict:
        """Return AllDebrid's account-usable supported-host inventory."""
        return await self._post(API_V41, "user/hosts")

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
            data = await self._post(API_V41, "magnet/status", payload)
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

    async def account_magnet_status_codes(self) -> List[int]:
        """The statusCode of every magnet on the account, read strictly from
        one v4.1 ``magnet/status`` listing: a ``magnets`` array whose every
        record carries a non-negative integer ``statusCode``. Anything else
        raises ``AllDebridAPIError("MALFORMED_RESPONSE")`` -- a response that
        does not say what is on the account is never read as an empty one."""
        data = await self._post(API_V41, "magnet/status")
        magnets = data.get("magnets") if isinstance(data, dict) else None
        if not isinstance(magnets, list):
            raise AllDebridAPIError("MALFORMED_RESPONSE", "magnet/status returned no magnets array")
        codes = []
        for magnet in magnets:
            code = magnet.get("statusCode") if isinstance(magnet, dict) else None
            if not isinstance(code, int) or isinstance(code, bool) or code < 0:
                raise AllDebridAPIError("MALFORMED_RESPONSE", "magnet/status returned a magnet without a statusCode")
            codes.append(code)
        return codes

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
        pass
