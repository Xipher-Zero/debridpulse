"""Release-candidate version surface contract."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_rc1_version_is_authoritative_and_user_visible_surfaces_follow_it() -> None:
    expected = "1.0.11rc1"
    expected_image = f"ghcr.io/xipher-zero/debridpulse:v{expected}"
    assert read(ROOT / "VERSION").strip() == expected

    version_source = read(ROOT / "backend" / "core" / "version.py")
    main = read(ROOT / "backend" / "main.py")
    routes = read(ROOT / "backend" / "api" / "routes.py")
    auth_routes = read(ROOT / "backend" / "api" / "auth_routes.py")
    index = read(STATIC / "index.html")
    app = read(STATIC / "app.js")
    help_page = read(STATIC / "ui-help-page.js")
    compose = read(ROOT / "docker-compose.yml")
    readme = read(ROOT / "README.md")
    project_page = read(ROOT / "index.html")

    assert "def read_version()" in version_source
    assert "version=read_version()" in main
    assert '"version": read_version()' in routes

    # The login page renders the authoritative backend version directly.
    assert "version = html.escape(read_version())" in auth_routes
    assert '<div class="version">v{version}</div>' in auth_routes

    # The main shell starts with a neutral placeholder and is hydrated from
    # /api/stats, whose version value is provided by read_version().
    assert 'id="sidebar-version">v…</div>' in index
    assert "const s = await api('GET', '/stats');" in app
    assert "const versionEl = document.getElementById('sidebar-version');" in app
    assert "versionEl.textContent = s.version ? `v${s.version}` : 'v—';" in app

    # Release-facing deployment examples must advance with the candidate too.
    assert expected_image in compose
    assert readme.count(expected_image) >= 2
    assert project_page.count(expected_image) >= 2

    for source in (index, app, auth_routes, help_page, compose, readme, project_page):
        assert "1.0.10" not in source


def test_rc1_container_metadata_uses_authoritative_version_file() -> None:
    workflow = read(ROOT / ".github" / "workflows" / "fork-image.yml")
    dockerfile = read(ROOT / "Dockerfile")

    assert 'version=$(tr -d \'\\r\\n\' < VERSION)' in workflow
    assert "org.opencontainers.image.version=${{ steps.version.outputs.version }}" in workflow
    assert "APP_VERSION=${{ steps.version.outputs.version }}" in workflow
    assert 'ARG APP_VERSION=unknown' in dockerfile
    assert 'org.opencontainers.image.version="${APP_VERSION}"' in dockerfile
