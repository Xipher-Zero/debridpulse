"""A connected browser's video sign-in, held for one job (1.0.14, operator 2026-11-19).

When a person sends a video from the DownloadPulse browser extension, the
extension may send their sign-in on that site with it: the cookies their
browser would send to the video's own address (the extension's opt-out
setting). They exist so the Media Downloads worker can fetch what that person
can watch. This module is their one owner, and they live nowhere else:

* only in this process's memory, keyed by the job's demand id -- never in the
  database, a file, a log, a job record, the idempotency digest or replay, an
  error detail or an attempt record; a restart simply loses them (the job then
  proceeds without);
* handed out (``reader``) only for a transfer that is not finished and whose
  every outstanding demand belongs to the cookie owner's account, while that
  browser's token is not revoked and within ``CEILING_SECONDS`` of the
  submission. Anything else -- an unknown transfer, a mixed or unattributed
  demand, a database error -- is "no cookies": it fails closed;
* released by ``release`` only, driven from durable state by ``settle``
  (``ExternalClients.maintain``) and, eagerly, by a withdrawal or a refused
  admission. The lookup applies the same rule, so nothing is used between a
  job's end and the next sweep.

The transfer a resolution unit serves comes from ``transfers.contracts
.served_transfer``; an execution attempt's, from its ``execution_attempts``
row. Nothing is ever matched by URL. Python cannot guarantee that released
memory is overwritten: a released entry is only unreferenced.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from db.database import get_db

#: Per job: at most this many cookies, and this many bytes of names, values,
#: domains and paths (the worker's instruction pipe holds 64 KiB in all).
MAX_COOKIES = 300
MAX_BYTES = 32 * 1024
#: At most this many jobs' cookies at once; past it a submission with cookies
#: is refused (retryable), never accepted without them silently.
CAPACITY = 64
#: Held at most this long after the submission, whatever the job's state.
CEILING_SECONDS = 24 * 3600
_TERMINAL = frozenset({"completed", "consolidated", "cancelled", "deleted"})
_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,256}$")
_UNSAFE_VALUE = re.compile(r"[\x00-\x1f\x7f;]")


class CookieRefused(ValueError):
    """A submission's ``cookies`` field is malformed or exceeds a bound. The
    message names the field, never a value; ``code`` is the browser protocol's
    refusal code it is answered with."""

    def __init__(self, message: str, *, code: str = "invalid_request"):
        super().__init__(message)
        self.code = code


class CookieCapacity(RuntimeError):
    """Every slot is in use; the same submission may be retried."""


@dataclass(frozen=True)
class Cookie:
    name: str
    value: str = field(repr=False)
    domain: str
    path: str
    secure: bool
    http_only: bool
    host_only: bool
    expires: int | None

    def as_instruction(self) -> dict:
        """The worker instruction's form (``executors.media.worker``)."""
        return {"name": self.name, "value": self.value, "domain": self.domain, "path": self.path,
                "secure": self.secure, "http_only": self.http_only, "host_only": self.host_only,
                "expires": self.expires}


@dataclass(frozen=True)
class _Entry:
    account_id: str
    credential_id: str
    cookies: tuple[Cookie, ...] = field(repr=False)
    registered_at: float

    def __repr__(self) -> str:   # never a value, nor how many
        return "<browser cookies: redacted>"


def _host(link: str) -> str:
    from urllib.parse import urlsplit
    try:
        return (urlsplit(link).hostname or "").lower()
    except ValueError:
        return ""


def parse(value, links: list[str]) -> tuple[Cookie, ...]:
    """A submission's ``cookies``: validated, each domain-matched to a link's
    host (a cookie for any other site is refused), within the bounds."""
    if not isinstance(value, list) or not value:
        raise CookieRefused("cookies must be a non-empty list", code="invalid_request")
    if len(value) > MAX_COOKIES:
        raise CookieRefused(f"cookies holds at most {MAX_COOKIES} entries", code="invalid_request")
    hosts = {host for host in map(_host, links) if host}
    cookies, size = [], 0
    for item in value:
        if not isinstance(item, dict) or set(item) - {"name", "value", "domain", "path", "secure", "http_only",
                                                       "host_only", "expires"}:
            raise CookieRefused("each cookie is an object of name, value, domain, path, secure, http_only, "
                                "host_only, expires", code="invalid_request")
        name, val, domain, path = item.get("name"), item.get("value"), item.get("domain"), item.get("path", "/")
        if not isinstance(name, str) or not _TOKEN.match(name):
            raise CookieRefused("a cookie's name is not a valid cookie name", code="invalid_request")
        if not isinstance(val, str) or len(val) > 8192 or _UNSAFE_VALUE.search(val):
            raise CookieRefused("a cookie's value is not a valid cookie value", code="invalid_request")
        if not isinstance(domain, str) or not domain or len(domain) > 253 or _UNSAFE_VALUE.search(domain):
            raise CookieRefused("a cookie's domain is not valid", code="invalid_request")
        if not isinstance(path, str) or not path.startswith("/") or len(path) > 1024 or _UNSAFE_VALUE.search(path):
            raise CookieRefused("a cookie's path is not valid", code="invalid_request")
        bare = domain.lower().lstrip(".")
        # Only the sent video's own site: the link's host is that domain or under it.
        if "." not in bare or not any(host == bare or host.endswith("." + bare) for host in hosts):
            raise CookieRefused("a cookie's domain does not match the sent link", code="invalid_request")
        flags = [item.get(key, False) for key in ("secure", "http_only", "host_only")]
        if not all(isinstance(flag, bool) for flag in flags):
            raise CookieRefused("secure, http_only and host_only are true or false", code="invalid_request")
        expires = item.get("expires")
        if expires is not None and (isinstance(expires, bool) or not isinstance(expires, int) or expires < 0):
            raise CookieRefused("a cookie's expires is whole seconds, or null", code="invalid_request")
        size += len(name) + len(val) + len(domain) + len(path)
        cookies.append(Cookie(name, val, domain, path, *flags, expires))
    if size > MAX_BYTES:
        raise CookieRefused(f"cookies hold at most {MAX_BYTES} bytes", code="invalid_request")
    return tuple(cookies)


class BrowserCookies:
    """The one holder (module singleton ``browser_cookies``)."""

    def __init__(self):
        self._entries: dict[str, _Entry] = {}

    def __repr__(self) -> str:
        return "<BrowserCookies: redacted>"

    def __len__(self) -> int:
        return len(self._entries)

    def register(self, demand_id: str, account_id: str, credential_id: str, cookies: tuple[Cookie, ...]) -> None:
        """Hold ``cookies`` for the job whose demand is ``demand_id`` (before its admission)."""
        if not cookies:
            return
        if demand_id not in self._entries and len(self._entries) >= CAPACITY:
            raise CookieCapacity("browser cookie capacity reached")
        self._entries[str(demand_id)] = _Entry(str(account_id), str(credential_id), cookies, time.time())

    def release(self, demand_id: str) -> None:
        """The only removal."""
        self._entries.pop(str(demand_id), None)

    def held(self, demand_id: str) -> bool:
        return str(demand_id) in self._entries

    async def settle(self) -> int:
        """Release every entry whose job has ended, by durable state; how many."""
        released = 0
        for demand_id in list(self._entries):
            if not await self._live(demand_id):
                self.release(demand_id)
                released += 1
        return released

    async def _live(self, demand_id: str) -> bool:
        entry = self._entries.get(demand_id)
        if entry is None or time.time() - entry.registered_at > CEILING_SECONDS:
            return False
        try:
            async with get_db() as db:
                demand = await db.fetchone(
                    """SELECT d.withdrawn_at, t.status FROM transfer_demands d JOIN torrents t ON t.id=d.transfer_id
                       WHERE d.id=?""", (demand_id,))
                if demand is None:
                    # Not admitted (yet): kept while its reservation may still become it.
                    reservation = await db.fetchone("SELECT 1 FROM external_jobs WHERE reserved_demand_id=?",
                                                    (demand_id,))
                    return reservation is not None
                token = await db.fetchone("SELECT revoked_at FROM account_tokens WHERE id=?", (entry.credential_id,))
        except Exception:
            return False
        return (demand["withdrawn_at"] is None and demand["status"] not in _TERMINAL
                and token is not None and token["revoked_at"] is None)

    async def for_transfer(self, transfer_id: int | None) -> tuple[Cookie, ...] | None:
        """The cookies for ``transfer_id``, only when every rule holds; else None."""
        if transfer_id is None or not self._entries:
            return None
        try:
            async with get_db() as db:
                status = await db.fetchone("SELECT status FROM torrents WHERE id=?", (int(transfer_id),))
            if status is None or status["status"] in _TERMINAL:
                return None
            # The transfer core's own reading of "every outstanding demand this transfer's work
            # serves" (consolidated demand included), not a second definition of it.
            from transfers.repository import TransferRepository
            async with get_db() as db:
                outstanding = await db.fetchall(TransferRepository._AFFECTED_DEMAND_SQL, {"t": int(transfer_id)})
        except Exception:
            return None
        accounts = {row.get("account_id") for row in outstanding}
        # Every outstanding demand is one account's (none unattributed), and one of them holds cookies.
        if len(accounts) != 1 or None in accounts or not outstanding:
            return None
        held = sorted((self._entries[row["id"]].registered_at, row["id"]) for row in outstanding
                      if row["id"] in self._entries and self._entries[row["id"]].account_id in accounts)
        for _, demand_id in held:
            if await self._live(demand_id):
                return self._entries[demand_id].cookies
        return None

    async def current(self) -> tuple[Cookie, ...] | None:
        """The cookies for the transfer the resolution unit in progress serves
        (``transfers.contracts.served_transfer``); None outside a unit."""
        from transfers.contracts import served_transfer
        return await self.for_transfer(served_transfer())

    async def for_attempt(self, attempt_id: str) -> tuple[Cookie, ...] | None:
        """The cookies for the transfer an execution attempt serves (its durable row), if any."""
        try:
            async with get_db() as db:
                row = await db.fetchone("SELECT transfer_id FROM execution_attempts WHERE id=?", (str(attempt_id),))
        except Exception:
            return None
        return await self.for_transfer(row["transfer_id"]) if row else None


browser_cookies = BrowserCookies()
