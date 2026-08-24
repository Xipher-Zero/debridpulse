from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD_IMAGE = "ghcr.io/xipher-zero/debridpulse:v1.0.6"
VERSION = (ROOT / "VERSION").read_text().strip()
NEW_IMAGE = f"ghcr.io/xipher-zero/debridpulse:v{VERSION}"

if VERSION != "1.0.10":
    raise RuntimeError(f"Unexpected freeze VERSION {VERSION!r}; expected 1.0.10")

compose_path = ROOT / "docker-compose.yml"
compose = compose_path.read_text()
if compose.count(OLD_IMAGE) != 1:
    raise RuntimeError(f"docker-compose.yml expected one stale image pin, found {compose.count(OLD_IMAGE)}")
compose_path.write_text(compose.replace(OLD_IMAGE, NEW_IMAGE, 1))

readme_path = ROOT / "README.md"
readme = readme_path.read_text()
if readme.count(OLD_IMAGE) != 2:
    raise RuntimeError(f"README.md expected two stale image pins, found {readme.count(OLD_IMAGE)}")
readme = readme.replace(OLD_IMAGE, NEW_IMAGE)
old_extract = (
    "Configure optional archive extraction. DebridPulse enforces per-archive file-count, expanded-size, "
    "and compression-ratio limits. External 7z/RAR extraction is performed in an isolated staging "
    "directory and validated before files are merged into the download tree."
)
new_extract = (
    "Configure optional archive extraction. DebridPulse enforces per-archive file-count, expanded-size, "
    "and compression-ratio limits. Every supported archive format is extracted into an isolated staging "
    "directory, validated, and committed to the download tree with no-clobber semantics."
)
if readme.count(old_extract) != 1:
    raise RuntimeError("README extraction safety paragraph no longer matches expected pre-freeze text")
readme_path.write_text(readme.replace(old_extract, new_extract, 1))

test_path = ROOT / "backend/tests/test_v105_deployment_hardening.py"
test = test_path.read_text()
old_test = '''def test_compose_tracks_release_and_public_health_endpoint():\n    root = Path(__file__).resolve().parents[2]\n    compose = (root / "docker-compose.yml").read_text()\n    assert "ghcr.io/xipher-zero/debridpulse:v1.0.6" in compose\n    assert "http://localhost:8080/api/health" in compose\n    assert "http://localhost:8080/api/stats" not in compose\n'''
new_test = '''def test_compose_and_readme_track_current_release_and_public_health_endpoint():\n    root = Path(__file__).resolve().parents[2]\n    version = (root / "VERSION").read_text().strip()\n    expected_image = f"ghcr.io/xipher-zero/debridpulse:v{version}"\n    compose = (root / "docker-compose.yml").read_text()\n    readme = (root / "README.md").read_text()\n    assert expected_image in compose\n    assert readme.count(expected_image) >= 2\n    assert "http://localhost:8080/api/health" in compose\n    assert "http://localhost:8080/api/stats" not in compose\n'''
if test.count(old_test) != 1:
    raise RuntimeError("Deployment release-contract test no longer matches expected pre-freeze text")
test_path.write_text(test.replace(old_test, new_test, 1))

# Fail closed if the stale install image survives anywhere in normal source/docs.
ignored = {
    ROOT / "tools/tmp_release_surface_freeze.py",
}
remaining = []
for path in ROOT.rglob("*"):
    if not path.is_file() or path in ignored or ".git" in path.parts:
        continue
    try:
        text = path.read_text()
    except (UnicodeDecodeError, OSError):
        continue
    if OLD_IMAGE in text:
        remaining.append(str(path.relative_to(ROOT)))
if remaining:
    raise RuntimeError(f"Stale v1.0.6 image pin remains in: {remaining}")

print(f"Release surfaces aligned to {VERSION}: {NEW_IMAGE}")
