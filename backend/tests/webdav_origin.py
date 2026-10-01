"""A deterministic in-process WebDAV origin for DP 1.0.13 tests.

Serves ``PROPFIND`` (``Depth: 0``/``1``) and ``GET`` over an in-memory tree:
paths ending in ``/`` are collections, everything else is a file. It records
every request (method, path, ``Depth``, ``Authorization``) and can be told to
misbehave the ways real or hostile servers do, so discovery is proven without
any live service.
"""
from __future__ import annotations

import base64
from urllib.parse import quote

from aiohttp import web

MULTISTATUS = '<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:">{}</D:multistatus>'


def entry(href: str, *, collection: bool, size: int | None = None, status: str = "HTTP/1.1 200 OK") -> str:
    kind = "<D:resourcetype><D:collection/></D:resourcetype>" if collection else "<D:resourcetype/>"
    length = "" if size is None or collection else f"<D:getcontentlength>{size}</D:getcontentlength>"
    return (f"<D:response><D:href>{href}</D:href><D:propstat><D:prop>{kind}{length}</D:prop>"
            f"<D:status>{status}</D:status></D:propstat></D:response>")


class WebDavOrigin:
    def __init__(self, tree: dict[str, bytes | None], *, credentials=None, challenge='Basic realm="dav"'):
        # ``tree``: "/dav/" -> None (collection), "/dav/a.txt" -> bytes (file).
        self.tree = dict(tree)
        self.credentials = credentials
        self.challenge = challenge
        self.requests: list[tuple[str, str, str | None, str | None]] = []
        # Fault switches.
        self.status: int | None = None            # answer every PROPFIND with this status
        self.statuses: dict[str, int] = {}        # path -> status for that path only
        self.raw: dict[str, str] = {}             # path -> verbatim 207 body
        self.extra: dict[str, list[str]] = {}     # path -> extra <response> blocks appended
        self.redirects: dict[str, str] = {}       # path -> Location (302)
        self.forbid_infinity = True               # Depth: infinity is refused, as many servers do
        self.omit_length = False
        self.runner = None
        self.port = 0

    async def start(self):
        app = web.Application()
        app.router.add_route("PROPFIND", "/{tail:.*}", self._propfind)
        app.router.add_get("/{tail:.*}", self._get)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = self.runner.addresses[0][1]
        return self

    async def close(self):
        await self.runner.cleanup()

    def url(self, path: str, *, scheme: str = "http", host: str = "dav.test") -> str:
        return f"{scheme}://{host}:{self.port}{path}"

    def _authorized(self, request) -> bool:
        if self.credentials is None:
            return True
        token = "Basic " + base64.b64encode(":".join(self.credentials).encode()).decode()
        return request.headers.get("Authorization") == token

    def _record(self, request):
        self.requests.append((request.method, request.path, request.headers.get("Depth"),
                              request.headers.get("Authorization")))

    async def _propfind(self, request):
        self._record(request)
        path = request.path
        # A protected server authenticates before it answers anything --
        # including where a path has moved.
        if not self._authorized(request):
            return web.Response(status=401, headers={"WWW-Authenticate": self.challenge})
        if path in self.redirects:
            return web.Response(status=302, headers={"Location": self.redirects[path]})
        if self.status is not None or path in self.statuses:
            return web.Response(status=self.statuses.get(path, self.status), text="no")
        depth = request.headers.get("Depth", "infinity")
        if depth == "infinity" and self.forbid_infinity:
            return web.Response(status=403, text="infinite depth is disabled")
        if path in self.raw:
            return web.Response(status=207, text=self.raw[path], content_type="application/xml")
        if path not in self.tree:
            return web.Response(status=404)
        blocks = [self._entry(path)]
        if depth == "1" and self.tree[path] is None:
            for child in sorted(self.tree):
                rest = child[len(path):]
                if child != path and child.startswith(path) and rest and "/" not in rest.rstrip("/"):
                    blocks.append(self._entry(child))
        blocks += self.extra.get(path, [])
        return web.Response(status=207, text=MULTISTATUS.format("".join(blocks)), content_type="application/xml")

    def _entry(self, path: str) -> str:
        payload = self.tree[path]
        return entry(quote(path), collection=payload is None,
                     size=None if payload is None or self.omit_length else len(payload))

    async def _get(self, request):
        self._record(request)
        if not self._authorized(request):
            return web.Response(status=401, headers={"WWW-Authenticate": self.challenge})
        if request.path in self.redirects:
            return web.Response(status=302, headers={"Location": self.redirects[request.path]})
        payload = self.tree.get(request.path)
        if payload is None:
            return web.Response(status=404)
        return web.Response(body=payload)
