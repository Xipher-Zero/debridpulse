from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD_IMAGE = "ghcr.io/xipher-zero/debridpulse:v1.0.6"
VERSION = (ROOT / "VERSION").read_text().strip()
NEW_IMAGE = f"ghcr.io/xipher-zero/debridpulse:v{VERSION}"

if VERSION != "1.0.10":
    raise RuntimeError(f"Unexpected freeze VERSION {VERSION!r}; expected 1.0.10")

compose_path = ROOT / "docker-compose.yml"
compose = compose_path.read_text()
if OLD_IMAGE in compose:
    compose = compose.replace(OLD_IMAGE, NEW_IMAGE)
if NEW_IMAGE not in compose:
    raise RuntimeError("docker-compose.yml does not track current VERSION")
compose_path.write_text(compose)

readme_path = ROOT / "README.md"
readme = readme_path.read_text()
if OLD_IMAGE in readme:
    readme = readme.replace(OLD_IMAGE, NEW_IMAGE)
if readme.count(NEW_IMAGE) < 2:
    raise RuntimeError("README.md does not contain both current release image examples")
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
if old_extract in readme:
    readme = readme.replace(old_extract, new_extract, 1)
if new_extract not in readme:
    raise RuntimeError("README extraction safety description is not current")
readme_path.write_text(readme)

test_path = ROOT / "backend/tests/test_v105_deployment_hardening.py"
test = test_path.read_text()
old_test = '''def test_compose_tracks_release_and_public_health_endpoint():\n    root = Path(__file__).resolve().parents[2]\n    compose = (root / "docker-compose.yml").read_text()\n    assert "ghcr.io/xipher-zero/debridpulse:v1.0.6" in compose\n    assert "http://localhost:8080/api/health" in compose\n    assert "http://localhost:8080/api/stats" not in compose\n'''
new_test = '''def test_compose_and_readme_track_current_release_and_public_health_endpoint():\n    root = Path(__file__).resolve().parents[2]\n    version = (root / "VERSION").read_text().strip()\n    expected_image = f"ghcr.io/xipher-zero/debridpulse:v{version}"\n    compose = (root / "docker-compose.yml").read_text()\n    readme = (root / "README.md").read_text()\n    assert expected_image in compose\n    assert readme.count(expected_image) >= 2\n    assert "http://localhost:8080/api/health" in compose\n    assert "http://localhost:8080/api/stats" not in compose\n'''
if old_test in test:
    test = test.replace(old_test, new_test, 1)
if "def test_compose_and_readme_track_current_release_and_public_health_endpoint():" not in test:
    raise RuntimeError("Deployment release-contract test is not current")
test_path.write_text(test)

# The inherited PostgreSQL migration guide describes routes, dependencies and
# DB_TYPE configuration removed from the SQLite-only V1 product. It is scope
# residue, not historical release documentation, so it must not ship.
migration_doc = ROOT / "docs/migration.md"
if migration_doc.exists():
    migration_doc.unlink()

scope_path = ROOT / "backend/tests/test_v1_scope.py"
scope = scope_path.read_text()
scope_test = '''\n\ndef test_removed_postgres_migration_documentation_is_not_shipped():\n    assert not (REPO_ROOT / "docs/migration.md").exists()\n'''
if "test_removed_postgres_migration_documentation_is_not_shipped" not in scope:
    scope += scope_test
scope_path.write_text(scope)

# Fail closed if the stale install image survives anywhere in normal source/docs.
ignored = {ROOT / "tools/tmp_release_surface_freeze.py"}
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
print("Removed inherited PostgreSQL migration documentation from SQLite-only V1")
