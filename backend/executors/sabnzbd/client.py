"""The SABnzbd HTTP API boundary.

Everything SAB-native terminates here or in ``translation.py``. The client
distinguishes the two failure classes the executor's safety properties depend
on, characterized against a real SABnzbd 5.1.3:

``SabTransportError``  SAB could not be reached / did not answer. This is NEVER
                       evidence that a native job is absent or failed.
``SabApiError``        SAB answered, but not with a usable JSON body -- for
                       example the plain-text ``API Key Incorrect``. Also never
                       evidence of absence.

Only a successful JSON answer that positively lists (or omits) a job is truth.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp

# SAB post-processing selector. 1 = repair (PAR2 verify/repair) with NO unpack
# and NO delete -- exactly the DebridPulse ownership boundary. DebridPulse owns
# archive extraction; SAB must never unpack.
PP_REPAIR_ONLY = 1

# The SAB job-priority value meaning "paused on arrival".
NATIVE_PRIORITY_PAUSED = -2
NATIVE_PRIORITY_DEFAULT = -100


# SAB pages queue/history; this is the page size DebridPulse asks for. A page
# that does not cover everything SAB reports is explicitly not authoritative.
SNAPSHOT_LIMIT = 500


@dataclass(frozen=True)
class SabSnapshot:
    """A bulk queue/history page plus whether it is authoritative."""
    slots: tuple[dict, ...]
    complete: bool


def _reported_total(section: dict, fallback: int) -> int:
    """How many entries SAB says exist in total for this listing."""
    for key in ("noofslots_total", "noofslots"):
        value = section.get(key)
        if value is not None:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                continue
    return fallback


class SabTransportError(Exception):
    """SAB was unreachable or did not answer. Never proof of absence."""


class SabApiError(Exception):
    """SAB answered with an unusable/non-JSON body. Never proof of absence."""


@dataclass(frozen=True)
class SabEndpoint:
    base_url: str
    api_key: str
    timeout_seconds: int = 15


class SabnzbdClient:
    """Thin async client over SAB's single ``/api`` entry point."""

    def __init__(self, endpoint: SabEndpoint, *, session_factory=None):
        self.endpoint = endpoint
        self._session_factory = session_factory or self._default_session

    @property
    def secrets(self) -> tuple[str, ...]:
        """Exact values that must never appear in a surfaced diagnostic."""
        return (self.endpoint.api_key,) if self.endpoint.api_key else ()

    def _default_session(self):
        timeout = aiohttp.ClientTimeout(total=max(1, int(self.endpoint.timeout_seconds)))
        return aiohttp.ClientSession(timeout=timeout)

    def _url(self) -> str:
        """The endpoint, and nothing else.

        Every call -- control or upload -- carries its whole request in the
        body, so the URL never holds the internal API key or an operator's news
        credential. A request line travels through access logs, proxy logs,
        process listings, exception text and crash diagnostics; a body does
        not. SABnzbd 5.1.3 accepts each mode this way (characterized against
        the bundled service), so nothing is given up by refusing the query
        string entirely.
        """
        parts = urlsplit(self.endpoint.base_url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/api", "", ""))

    def _form(self, params: dict[str, Any]) -> bytes:
        """One control request, wholly encoded into a form body."""
        fields = dict(params)
        fields.setdefault("output", "json")
        fields["apikey"] = self.endpoint.api_key
        return urlencode({key: value for key, value in fields.items() if value is not None}).encode()

    @staticmethod
    def _decode(status: int, body: str):
        """The ONE place a raw SAB response becomes usable truth.

        Anything that is not a 2xx carrying a well-formed JSON object without an
        explicit error is an API failure. It must never decay into an empty but
        authoritative-looking answer -- that is how a reachable-but-broken SAB
        would be mistaken for "the job is gone".
        """
        if not 200 <= int(status) < 300:
            raise SabApiError(f"SAB answered HTTP {status}")
        try:
            value = json.loads(body)
        except ValueError as exc:
            # SAB reports API-key problems as plain text, not JSON.
            raise SabApiError("SAB returned a non-JSON response") from exc
        if not isinstance(value, dict):
            raise SabApiError("SAB returned an unexpected response shape")
        if value.get("error"):
            raise SabApiError("SAB reported an API error")
        if "status" in value and value["status"] is False:
            raise SabApiError("SAB rejected the request")
        return value

    @staticmethod
    def _section(payload: dict, name: str) -> dict:
        """One required top-level section of a SAB answer, shape-checked."""
        section = payload.get(name)
        if not isinstance(section, dict):
            raise SabApiError(f"SAB response is missing its {name} section")
        return section

    @staticmethod
    def _slots(section: dict, name: str) -> list[dict]:
        slots = section.get("slots")
        if not isinstance(slots, list) or any(not isinstance(item, dict) for item in slots):
            raise SabApiError(f"SAB {name} response has no usable slot list")
        return slots

    async def _call(self, params: dict[str, Any] | None, *, data=None):
        """Issue one request. Always a POST, always body-carried.

        ``data`` is a prepared multipart body (the NZB upload); otherwise the
        parameters are form-encoded here. The transport failure is raised
        WITHOUT chaining the original exception: aiohttp's errors quote the
        request they were attempting, and a chained cause is still rendered by
        tracebacks and by ``str(exc.__cause__)`` in diagnostics.
        """
        url = self._url()
        payload = data if data is not None else self._form(params or {})
        headers = None if data is not None else {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            async with self._session_factory() as session:
                async with session.post(url, data=payload, **({"headers": headers} if headers else {})) as response:
                    status, body = response.status, await response.text()
        except SabApiError:
            raise
        except Exception:  # connection refused, timeout, DNS, TLS...
            raise SabTransportError("SAB is unreachable") from None
        return self._decode(status, body)

    # --- operations ------------------------------------------------------

    async def version(self) -> str:
        return str((await self._call({"mode": "version"})).get("version") or "")

    async def addfile(self, source, *, job_name: str, pp: int = PP_REPAIR_ONLY,
                      priority: int = NATIVE_PRIORITY_DEFAULT,
                      upload_filename: str = "") -> str:
        """Submit an NZB and return the server-minted ``nzo_id``.

        ``source`` is either the manifest bytes or an open binary file. A file
        is streamed into the multipart body by the transport, so a large
        manifest is never re-read into one Python object here -- the point of
        staging it on disk would be lost if this boundary undid it.

        ``job_name`` becomes the job's native name, which is what the caller's
        naming contract is expressed in. This boundary neither invents it nor
        interprets it.
        """
        form = aiohttp.FormData()
        form.add_field("mode", "addfile")
        form.add_field("output", "json")
        form.add_field("apikey", self.endpoint.api_key)
        form.add_field("nzbname", job_name)
        form.add_field("pp", str(int(pp)))
        form.add_field("priority", str(int(priority)))
        form.add_field("nzbfile", source, filename=upload_filename or f"{job_name}.nzb",
                       content_type="application/x-nzb")
        # Every field travels in the multipart body, exactly as the control
        # calls travel in their form body.
        result = await self._call(None, data=form)
        ids = result.get("nzo_ids") or []
        if not result.get("status") or not ids or not str(ids[0]).strip():
            raise SabApiError("SAB did not return a native job identity")
        return str(ids[0])

    async def rename(self, nzo_id: str, name: str) -> bool:
        """Set a queued job's native name. Returns whether SAB accepted it.

        Characterized against SABnzbd 5.1.3: this changes ``final_name`` alone
        -- the job's incomplete working folder keeps the name it was created
        with -- takes effect immediately, is persisted across a service
        restart, and answers ``status: false`` for a job that is no longer in
        the queue. A refusal is therefore reported, never assumed.
        """
        try:
            result = await self._call({"mode": "queue", "name": "rename",
                                       "value": nzo_id, "value2": name})
        except SabApiError:
            # SAB reports a refused rename as ``status: false``, which the
            # decoder raises. That is a refusal, not a transport failure.
            return False
        return bool(result.get("status", True))

    async def queue_snapshot(self, limit: int = SNAPSHOT_LIMIT) -> SabSnapshot:
        """One authoritative bulk view of the native queue.

        ``complete`` says whether this page actually covers everything SAB
        holds; a caller may only conclude absence from a complete snapshot.
        """
        payload = await self._call({"mode": "queue", "limit": int(limit), "start": 0})
        section = self._section(payload, "queue")
        slots = self._slots(section, "queue")
        return SabSnapshot(tuple(slots), _reported_total(section, len(slots)) <= len(slots))

    async def history_snapshot(self, limit: int = SNAPSHOT_LIMIT) -> SabSnapshot:
        payload = await self._call({"mode": "history", "limit": int(limit), "start": 0})
        section = self._section(payload, "history")
        slots = self._slots(section, "history")
        return SabSnapshot(tuple(slots), _reported_total(section, len(slots)) <= len(slots))

    async def download_throughput(self) -> int:
        """Current SERVICE-WIDE download rate, in bytes per second.

        Characterized against SABnzbd 5.1.3: ``build_queue()`` publishes one
        meter for the whole service (``kbpersec``, KiB/s, from a single global
        byte-per-second meter) and NO per-slot rate at all -- a slot's
        ``timeleft`` is itself derived from that same global figure. There is
        therefore nothing per job to read, and splitting the global figure
        across jobs would be an invention rather than a measurement.

        Because the service is DebridPulse-private -- loopback only, never
        published, and every server it did not declare is deleted -- everything
        this meter counts is DebridPulse-owned acquisition.

        Units are normalized HERE, once, at the native boundary.
        """
        section = self._section(await self._call({"mode": "queue", "limit": 0}), "queue")
        try:
            return max(0, int(float(section.get("kbpersec") or 0) * 1024))
        except (TypeError, ValueError) as exc:
            raise SabApiError("SAB did not report a usable throughput figure") from exc

    async def queue_slots(self, search: str | None = None) -> list[dict]:
        """A narrow search, used only for correlation-token reconciliation."""
        params: dict[str, Any] = {"mode": "queue", "limit": SNAPSHOT_LIMIT}
        if search:
            params["search"] = search
        section = self._section(await self._call(params), "queue")
        return self._slots(section, "queue")

    async def history_slots(self, search: str | None = None, nzo_id: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"mode": "history", "limit": SNAPSHOT_LIMIT}
        if search:
            params["search"] = search
        if nzo_id:
            params["nzo_ids"] = nzo_id
        section = self._section(await self._call(params), "history")
        return self._slots(section, "history")

    async def pause(self, nzo_id: str):
        return await self._call({"mode": "queue", "name": "pause", "value": nzo_id})

    async def resume(self, nzo_id: str):
        return await self._call({"mode": "queue", "name": "resume", "value": nzo_id})

    async def delete(self, nzo_id: str):
        return await self._call({"mode": "queue", "name": "delete",
                                 "value": nzo_id, "del_files": 1})

    async def get_config(self, section: str) -> dict:
        return (await self._call({"mode": "get_config", "section": section})).get("config") or {}

    async def set_config(self, section: str, keyword: str, **values) -> dict:
        params: dict[str, Any] = {"mode": "set_config", "section": section, "keyword": keyword}
        params.update({key: value for key, value in values.items() if value is not None})
        return (await self._call(params)).get("config") or {}

    async def del_config(self, section: str, keyword: str):
        return await self._call({"mode": "del_config", "section": section, "keyword": keyword})

    async def set_speedlimit(self, value: str) -> int:
        """Assign the aggregate limit and return the EFFECTIVE bytes/sec.

        The effective value is read back from ``queue.speedlimit_abs``, which is
        SAB's own absolute byte figure -- never the requested string.
        """
        await self._call({"mode": "config", "name": "speedlimit", "value": value})
        section = self._section(await self._call({"mode": "queue", "limit": 0}), "queue")
        try:
            return max(0, int(section.get("speedlimit_abs") or 0))
        except (TypeError, ValueError) as exc:
            raise SabApiError("SAB did not report a usable effective speed limit") from exc

    async def test_server(self, **values) -> tuple[bool, str]:
        """Validate a prospective NNTP server WITHOUT persisting it.

        Proven at Gate 1: ``mode=config&name=test_server`` accepts ad-hoc
        connection parameters, performs a real NNTP connection, and leaves the
        persisted server list untouched.
        """
        params: dict[str, Any] = {"mode": "config", "name": "test_server"}
        params.update({key: value for key, value in values.items() if value is not None})
        result = (await self._call(params)).get("value") or {}
        return bool(result.get("result")), str(result.get("message") or "")
