"""WS2 S1 read-only directory-browser backend contracts."""
from __future__ import annotations

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import api.settings_validation_routes as directory_routes
import auth.middleware as auth_middleware
import transfers.storage as storage
from auth.middleware import enforce_authentication
from auth.passwords import hash_password
from core.config import AppSettings
from transfers.storage import DiskCapacity, StorageDomain, StorageReason, StorageState


def _capacity(tmp_path: Path) -> tuple[DiskCapacity, Path, Path]:
    app_dir = tmp_path / "app"
    download_dir = tmp_path / "download"
    app_dir.mkdir(parents=True)
    download_dir.mkdir(parents=True)
    capacity = DiskCapacity(
        download_dir,
        minimum_gb=0,
        application_path=app_dir / "debridpulse.db",
    )
    return capacity, app_dir, download_dir


def _detail(exc: HTTPException) -> dict:
    assert isinstance(exc.detail, dict)
    return exc.detail


def _api_app(capacity: DiskCapacity) -> FastAPI:
    app = FastAPI()
    app.state.application = SimpleNamespace(capacity=capacity)
    app.include_router(directory_routes.router, prefix="/api")
    return app


def test_directory_browser_returns_directories_only_hidden_and_stable_order(tmp_path):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    for name in ("zeta", "Alpha", "beta", ".hidden"):
        (download_dir / name).mkdir()
    (download_dir / "ordinary.txt").write_text("secret-file-name")
    (download_dir / "payload.bin").write_bytes(b"payload")

    result = directory_routes._browse_directory(download_dir.resolve(), capacity)
    payload = result.model_dump()

    assert payload["current"]["path"] == str(download_dir.resolve())
    assert payload["current"]["accessible"] is True
    assert payload["current"]["writable"] is True
    assert payload["current"]["selectable"] is True
    assert [item["name"] for item in payload["children"]] == [
        ".hidden", "Alpha", "beta", "zeta"
    ]
    text = repr(payload)
    assert "ordinary.txt" not in text
    assert "payload.bin" not in text
    assert "secret-file-name" not in text


def test_directory_browser_http_contract_and_default_location(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    child = download_dir / "child"
    child.mkdir()
    (download_dir / "not-exposed.txt").write_text("x")
    monkeypatch.setattr(
        directory_routes,
        "get_settings",
        lambda: SimpleNamespace(download_folder=str(download_dir)),
    )

    response = TestClient(_api_app(capacity)).get("/api/settings/directories")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert set(payload) == {"current", "parent", "children"}
    assert payload["current"]["path"] == str(download_dir.resolve())
    assert set(payload["current"]) == {
        "name", "path", "accessible", "writable", "selectable", "reason", "capacity"
    }
    assert set(payload["current"]["capacity"]) == {"total_bytes", "free_bytes"}
    assert payload["children"] == [
        {
            "name": "child",
            "path": str(child.resolve()),
            "accessible": True,
            "writable": None,
            "selectable": None,
            "reason": "not_validated",
        }
    ]
    assert "not-exposed.txt" not in response.text


def test_directory_browser_http_relative_path_error_is_structured(tmp_path):
    capacity, _app_dir, _download_dir = _capacity(tmp_path)
    response = TestClient(_api_app(capacity)).get(
        "/api/settings/directories",
        params={"path": "relative/child"},
    )
    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "code": "relative_path",
            "message": "Relative directory paths are not supported",
        }
    }


def test_directory_browser_normalizes_absolute_paths_and_parent(tmp_path):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    nested = download_dir / "one" / "two"
    nested.mkdir(parents=True)
    raw = f"{download_dir}//one/./two/../two/"

    resolved = directory_routes._resolve_requested_directory(raw)
    result = directory_routes._browse_directory(resolved, capacity)

    assert resolved == nested.resolve()
    assert result.current.path == str(nested.resolve())
    assert result.parent == str(nested.parent.resolve())


def test_directory_browser_root_parent_is_null(tmp_path):
    capacity, _app_dir, _download_dir = _capacity(tmp_path)
    root = Path("/").resolve(strict=True)
    result = directory_routes._browse_directory(root, capacity)
    assert result.current.path == str(root)
    assert result.parent is None


def test_directory_browser_rejects_relative_path_without_using_cwd():
    with pytest.raises(HTTPException) as caught:
        directory_routes._resolve_requested_directory("relative/child")
    assert caught.value.status_code == 400
    assert _detail(caught.value)["code"] == "relative_path"


def test_directory_browser_rejects_malformed_path():
    with pytest.raises(HTTPException) as caught:
        directory_routes._resolve_requested_directory("\x00")
    assert caught.value.status_code == 400
    assert _detail(caught.value)["code"] == "invalid_path"


def test_directory_browser_missing_and_file_paths_use_structured_errors(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(HTTPException) as caught:
        directory_routes._resolve_requested_directory(str(missing))
    assert caught.value.status_code == 404
    assert _detail(caught.value) == {
        "code": "path_unavailable",
        "message": "Directory path does not exist",
    }

    regular = tmp_path / "regular-file"
    regular.write_text("x")
    resolved = directory_routes._resolve_requested_directory(str(regular))
    capacity, _app_dir, _download_dir = _capacity(tmp_path / "capacity")
    with pytest.raises(HTTPException) as caught:
        directory_routes._browse_directory(resolved, capacity)
    assert caught.value.status_code == 400
    assert _detail(caught.value)["code"] == "not_directory"


def test_default_directory_prefers_configured_folder_then_nearest_browsable_ancestor(tmp_path):
    configured = tmp_path / "mounted" / "download"
    configured.mkdir(parents=True)
    assert directory_routes._default_directory_path(str(configured)) == configured.resolve()

    missing_child = configured / "missing" / "child"
    assert directory_routes._default_directory_path(str(missing_child)) == configured.resolve()


def test_default_relative_configuration_never_resolves_against_process_cwd():
    assert directory_routes._default_directory_path("relative/download") == Path("/").resolve(strict=True)


def test_many_children_trigger_only_one_authoritative_write_probe(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    for index in range(25):
        (download_dir / f"child-{index:02d}").mkdir()

    original = capacity.validate_download_path
    calls = []

    def counted(root, *, apply_if_active=False):
        calls.append((str(root), apply_if_active))
        return original(root, apply_if_active=apply_if_active)

    monkeypatch.setattr(capacity, "validate_download_path", counted)
    result = directory_routes._browse_directory(download_dir.resolve(), capacity)

    assert calls == [(str(download_dir.resolve()), False)]
    assert len(result.children) == 25
    assert all(child.accessible is True for child in result.children)
    assert all(child.writable is None for child in result.children)
    assert all(child.selectable is None for child in result.children)
    assert all(child.reason == "not_validated" for child in result.children)


def test_failed_candidate_validation_is_detached_from_active_download_health(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    capacity.check()
    before = capacity.snapshot(StorageDomain.DOWNLOAD)
    original = storage.tempfile.mkstemp

    def fail_candidate(*args, **kwargs):
        probe_dir = kwargs.get("dir")
        if probe_dir is not None and Path(probe_dir).resolve() == candidate.resolve():
            raise OSError(errno.EROFS, "raw read-only diagnostic")
        return original(*args, **kwargs)

    monkeypatch.setattr(storage.tempfile, "mkstemp", fail_candidate)
    result = directory_routes._browse_directory(candidate.resolve(), capacity)

    assert result.current.accessible is True
    assert result.current.writable is False
    assert result.current.selectable is False
    assert result.current.reason == StorageReason.READ_ONLY.value
    after = capacity.snapshot(StorageDomain.DOWNLOAD)
    assert after.configured_path == before.configured_path == str(download_dir)
    assert after.state == before.state == StorageState.HEALTHY
    assert after.generation == before.generation


def test_capacity_failure_returns_unknown_not_zero_and_hides_raw_error(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    original = storage.shutil.disk_usage

    def fail(path):
        if Path(path).resolve() == download_dir.resolve():
            raise OSError(errno.EIO, "raw capacity secret")
        return original(path)

    monkeypatch.setattr(storage.shutil, "disk_usage", fail)
    result = directory_routes._browse_directory(download_dir.resolve(), capacity)
    payload = result.model_dump()

    assert payload["current"]["capacity"] == {"total_bytes": None, "free_bytes": None}
    assert payload["current"]["selectable"] is False
    assert "raw capacity secret" not in repr(payload)


def test_inaccessible_child_does_not_fail_parent_listing(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    good = download_dir / "good"
    restricted = download_dir / "restricted"
    good.mkdir()
    restricted.mkdir()
    original = os.scandir

    def guarded(path):
        if Path(path).resolve() == restricted.resolve():
            raise PermissionError(errno.EACCES, "raw permission detail")
        return original(path)

    monkeypatch.setattr(os, "scandir", guarded)
    result = directory_routes._browse_directory(download_dir.resolve(), capacity)
    rows = {item.name: item for item in result.children}

    assert rows["good"].accessible is True
    assert rows["restricted"].accessible is False
    assert rows["restricted"].selectable is False
    assert rows["restricted"].reason == StorageReason.INACCESSIBLE.value
    assert "raw permission detail" not in repr(result.model_dump())


def test_child_disappearing_after_enumeration_is_omitted(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    stable = download_dir / "stable"
    racing = download_dir / "racing"
    stable.mkdir()
    racing.mkdir()
    original_resolve = Path.resolve

    def resolve_with_race(self, *args, **kwargs):
        if self == racing:
            raise FileNotFoundError(errno.ENOENT, "gone")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_with_race)
    result = directory_routes._browse_directory(download_dir, capacity)
    assert [item.name for item in result.children] == ["stable"]


def test_symlink_policy_follows_directory_and_excludes_file_broken_and_loop(tmp_path):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    target = download_dir / "target"
    target.mkdir()
    regular = download_dir / "regular"
    regular.write_text("x")
    (download_dir / "dir-link").symlink_to(target, target_is_directory=True)
    (download_dir / "file-link").symlink_to(regular)
    (download_dir / "broken-link").symlink_to(download_dir / "missing", target_is_directory=True)
    (download_dir / "loop-link").symlink_to(download_dir / "loop-link", target_is_directory=True)

    result = directory_routes._browse_directory(download_dir.resolve(), capacity)
    rows = {item.name: item for item in result.children}

    assert "target" in rows
    assert rows["dir-link"].path == str(target.resolve())
    assert "file-link" not in rows
    assert "broken-link" not in rows
    assert "loop-link" not in rows

    linked = directory_routes._resolve_requested_directory(str(download_dir / "dir-link"))
    linked_result = directory_routes._browse_directory(linked, capacity)
    assert linked_result.current.path == str(target.resolve())
    assert linked_result.parent == str(target.parent.resolve())


def test_explicit_symlink_loop_is_structured_error(tmp_path):
    loop = tmp_path / "loop"
    loop.symlink_to(loop, target_is_directory=True)
    with pytest.raises(HTTPException) as caught:
        directory_routes._resolve_requested_directory(str(loop))
    assert caught.value.status_code == 400
    assert _detail(caught.value)["code"] == "symlink_loop"


def test_directory_browser_route_is_get_only_and_not_public_auth_exception():
    route = next(
        route
        for route in directory_routes.router.routes
        if getattr(route, "path", None) == "/settings/directories"
    )
    assert route.methods == {"GET"}
    assert auth_middleware.is_public_path("/api/settings/directories") is False


def test_enabled_auth_rejects_unauthenticated_directory_enumeration(monkeypatch):
    cfg = AppSettings(auth_password_enabled=True, auth_username="operator")
    cfg.auth_password_hash = hash_password("correct horse battery staple")
    monkeypatch.setattr(auth_middleware, "get_settings", lambda: cfg)

    app = FastAPI()
    app.include_router(directory_routes.router, prefix="/api")

    @app.middleware("http")
    async def authentication_boundary(request, call_next):
        return await enforce_authentication(request, call_next)

    response = TestClient(app).get("/api/settings/directories", params={"path": "/"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_directory_response_contract_exposes_no_file_or_low_level_metadata():
    exposed = set(directory_routes.DirectoryBrowserEntry.model_fields)
    assert exposed == {"name", "path", "accessible", "writable", "selectable", "reason"}
    forbidden = {
        "content", "size", "mime_type", "hash", "inode", "device", "uid", "gid", "mode", "mtime"
    }
    assert not exposed.intersection(forbidden)
    assert set(directory_routes.DirectoryCapacity.model_fields) == {"total_bytes", "free_bytes"}
