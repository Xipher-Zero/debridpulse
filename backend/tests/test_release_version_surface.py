"""Final release version surface contract."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")




def test_release_container_metadata_uses_authoritative_version_file() -> None:
    workflow = read(ROOT / ".github" / "workflows" / "fork-image.yml")
    dockerfile = read(ROOT / "Dockerfile")

    assert 'version=$(tr -d \'\\r\\n\' < VERSION)' in workflow
    assert "org.opencontainers.image.version=${{ steps.version.outputs.version }}" in workflow
    assert "APP_VERSION=${{ steps.version.outputs.version }}" in workflow
    assert 'ARG APP_VERSION=unknown' in dockerfile
    assert 'org.opencontainers.image.version="${APP_VERSION}"' in dockerfile

    release_branch = "1.0.11"
    for workflow_name in ("tests.yml", "container-security.yml", "fork-image.yml", "codeql.yml"):
        workflow_text = read(ROOT / ".github" / "workflows" / workflow_name)
        assert release_branch in workflow_text
