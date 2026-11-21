"""DP 1.0.14 installed-app metadata: one browser Web App Manifest.

``frontend/static/manifest.webmanifest`` asks a browser that installs
DownloadPulse for a ``standalone`` window, with the application's own identity
(``/``) and its established icon at 192 and 512 pixels. The application page
and the sign-in pages both link it; the sign-in pages' strict policy admits
exactly their own origin's manifest and icons. Those files are public
static bytes -- the same for every visitor, read before anyone has signed in
and fetched by browsers without credentials -- and nothing else becomes
public. Safari ignores the manifest's icons for the home screen and the Dock
and fetches ``apple-touch-icon.png`` itself, so that icon is one of them, and
both the application page and the sign-in pages link it. Every icon is the
current logo (``logo.svg``) and a PNG that decodes: a truncated
apple-touch-icon.png made Safari draw its own placeholder. The icon catalog ``icons/dp/manifest.json`` is not a Web App
Manifest and is untouched."""

import json
import struct
from pathlib import Path

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

import auth.middleware as auth_middleware
from api import auth_routes
from auth.middleware import enforce_authentication
from auth.policy import PUBLIC_PATHS, is_public_path
from core.config import AppSettings
from main import request_id_middleware

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
MANIFEST = STATIC / "manifest.webmanifest"
INSTALL_PATHS = {"/manifest.webmanifest", "/favicon-192.png", "/favicon.png", "/apple-touch-icon.png"}


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def test_the_manifest_requests_a_standalone_window_with_the_applications_own_identity():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["name"] == manifest["short_name"] == "DownloadPulse"
    # Root-relative, the same at every origin: no host, account or cache-busting value.
    assert manifest["id"] == manifest["start_url"] == manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    # The default theme's application background (design-tokens.css --dp-bg-app).
    assert manifest["theme_color"] == manifest["background_color"] == "#050814"
    assert "#050814" in (STATIC / "design-tokens.css").read_text(encoding="utf-8")
    assert not manifest.get("prefer_related_applications", False)
    assert set(manifest) == {"id", "name", "short_name", "start_url", "scope", "display", "background_color", "theme_color", "icons"}


def test_its_icons_are_the_established_artwork_at_their_declared_sizes():
    icons = json.loads(MANIFEST.read_text(encoding="utf-8"))["icons"]
    assert [(icon["src"], icon["sizes"], icon["type"], icon["purpose"]) for icon in icons] == [
        ("/favicon-192.png", "192x192", "image/png", "any"),
        ("/favicon.png", "512x512", "image/png", "any"),
    ]
    for icon in icons:
        width, height = map(int, icon["sizes"].split("x"))
        assert png_size(STATIC / icon["src"].lstrip("/")) == (width, height)


def test_the_application_and_sign_in_pages_link_it_once():
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert index.count('rel="manifest"') == 1 and '<link rel="manifest" href="/manifest.webmanifest">' in index
    # Safari's icon, on the application page and the sign-in pages alike.
    assert index.count('rel="apple-touch-icon"') == 1
    assert '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png?v=6">' in index
    assert "apple-mobile-web-app" not in index           # the manifest names the application
    source = Path(auth_routes.__file__).read_text(encoding="utf-8")
    assert source.count('<link rel="manifest" href="/manifest.webmanifest">') == 1
    assert '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png?v=6">' in auth_routes._AUTH_FAVICON_HTML


def test_every_install_icon_is_a_png_that_decodes_and_safaris_is_opaque():
    import zlib
    for name, size, opaque in (("apple-touch-icon.png", 180, True), ("favicon-192.png", 192, False), ("favicon.png", 512, False)):
        data, at, image, colour = (STATIC / name).read_bytes(), 8, b"", None
        assert data[:8] == b"\x89PNG\r\n\x1a\n", name
        while at < len(data):
            length, kind = struct.unpack(">I", data[at:at + 4])[0], data[at + 4:at + 8]
            body, crc = data[at + 8:at + 8 + length], data[at + 8 + length:at + 12 + length]
            assert len(crc) == 4 and zlib.crc32(kind + body) & 0xFFFFFFFF == struct.unpack(">I", crc)[0], (name, kind)
            if kind == b"IHDR":
                assert struct.unpack(">II", body[:8]) == (size, size), name
                colour = body[9]
            image += body if kind == b"IDAT" else b""
            at += 12 + length
        zlib.decompress(image)                              # the pixels themselves inflate
        if opaque:
            assert colour == 2, name                        # RGB: iOS fills transparency with black


@pytest.mark.parametrize("allow_form", [True, False])
def test_the_sign_in_policy_admits_only_its_own_origins_manifest_and_images(allow_form):
    directives = dict(item.split(" ", 1) for item in auth_routes._auth_csp(allow_form=allow_form).split("; "))
    assert directives["default-src"] == "'none'"
    assert directives["manifest-src"] == "'self'"
    assert directives["img-src"] == "data: 'self'"
    assert "connect-src" not in directives and "script-src" in directives


def test_exactly_the_install_files_become_public():
    assert INSTALL_PATHS <= PUBLIC_PATHS
    assert PUBLIC_PATHS - INSTALL_PATHS == {
        "/api/health", "/api/version", "/api/avatar", "/api/auth/status", "/login", "/auth/oidc/start", "/auth/oidc/callback",
        # Starting and polling a browser pairing (BROWSER_CLIENT.md): no credential exists yet.
        "/api/browser/v1/pairings", "/api/browser/v1/pairings/poll"}
    for path in ("/", "/index.html", "/favicon-32.png", "/icons/dp/manifest.json", "/api/settings", "/api/stats"):
        assert is_public_path(path) is False


def test_with_sign_in_enabled_a_visitor_reads_the_install_files_and_nothing_else(monkeypatch):
    cfg = AppSettings(auth_password_enabled=True)
    monkeypatch.setattr(auth_middleware, "get_settings", lambda: cfg)
    app = FastAPI()

    @app.middleware("http")
    async def authentication_boundary(request, call_next):
        return await enforce_authentication(request, call_next)

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    client = TestClient(app)
    expected = {"/manifest.webmanifest": "application/manifest+json", "/favicon-192.png": "image/png", "/favicon.png": "image/png",
                "/apple-touch-icon.png": "image/png"}
    for path, media_type in expected.items():
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].split(";")[0] == media_type
        assert response.content == (STATIC / path.lstrip("/")).read_bytes()
    for path in ("/", "/index.html", "/favicon-32.png", "/icons/dp/manifest.json", "/app.js"):
        assert client.get(path).status_code == 401, path


@pytest.mark.asyncio
async def test_the_manifest_is_revalidated_like_the_pages_that_link_it():
    request = Request({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
                       "path": "/manifest.webmanifest", "raw_path": b"/manifest.webmanifest", "query_string": b"",
                       "headers": [(b"host", b"localhost")], "client": ("127.0.0.1", 12345), "server": ("localhost", 80)})

    async def call_next(_request):
        return Response(content="{}", media_type="application/manifest+json")

    response = await request_id_middleware(request, call_next)
    assert response.headers["Cache-Control"] == "no-cache, must-revalidate"
