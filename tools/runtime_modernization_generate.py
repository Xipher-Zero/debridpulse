import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_one(path: str, old: str, new: str) -> None:
    p = ROOT / path
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match, found {count}: {old[:120]!r}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


def apply_changes() -> None:
    (ROOT / "backend/requirements.in").write_text(
        "fastapi==0.141.1\n"
        "uvicorn==0.52.1\n"
        "uvloop==0.22.1; sys_platform != \"win32\"\n"
        "httptools==0.8.0\n"
        "aiohttp==3.14.3\n"
        "aiosqlite==0.22.1\n"
        "pydantic==2.13.4\n"
        "bencode2==0.3.33\n"
        "python-multipart==0.0.32\n"
        "prometheus-client==0.26.0\n",
        encoding="utf-8",
    )
    (ROOT / "backend/requirements-dev.in").write_text(
        "-r requirements.in\n\n"
        "pytest==9.1.1\n"
        "pytest-asyncio==1.4.0\n"
        "pytest-cov==7.1.0\n",
        encoding="utf-8",
    )

    replace_one("Dockerfile", "FROM python:3.12.14-slim-bookworm", "FROM python:3.12.13-slim-trixie")
    replace_one("Dockerfile", "    p7zip-full \\\n", "    7zip \\\n")
    replace_one("backend/services/manager_v2.py", "import aiofiles\n", "")
    replace_one(
        "backend/core/config.py",
        "# Optional password applied to all archive extractions (7z -p and unrar -p).",
        "# Optional password applied to archive extraction where supported (7z -p).",
    )

    extractor = ROOT / "backend/services/extractor.py"
    text = extractor.read_text(encoding="utf-8")
    old_intro = '''Supports: .zip, .tar, .tar.gz, .tgz, .tar.bz2, .tar.xz, .gz,
          .bz2, .xz, .7z, .rar, multi-part .rar (*.part1.rar / *.r00),
          .tar.zst / .tar.lzma (via 7z binary)

Strategy:
  1. Python-native for zip / tar / gz / bz2 / xz (zero extra deps)
  2. System binary `7z` (from p7zip-full) for .7z, .tar.zst, .tar.lzma, and RAR
  3. RAR extraction fails closed unless a 7z-compatible binary is available
'''
    new_intro = '''Supports: .zip, .tar, .tar.gz, .tgz, .tar.bz2, .tar.xz, .gz,
          .bz2, .xz, .7z, .tar.zst / .tar.lzma (via 7z binary)

Strategy:
  1. Python-native for zip / tar / gz / bz2 / xz (zero extra deps)
  2. System binary `7z` (from Debian 7zip) for .7z, .tar.zst, .tar.lzma
  3. RAR is intentionally not advertised or auto-extracted by the official image;
     Debian's RAR plugin is non-free and is not bundled.
'''
    if text.count(old_intro) != 1:
        raise SystemExit("extractor intro did not match expected source")
    text = text.replace(old_intro, new_intro, 1)

    rar_constants_start = text.index('_RAR_EXTS  = {".rar", ".r00", ".r01", ".r02"}\n')
    suffix_start = text.index("\ndef _suffix(path: Path) -> str:", rar_constants_start)
    text = text[:rar_constants_start] + text[suffix_start + 1:]

    rar_detect = '''    if s in _RAR_EXTS:
        # Skip non-first parts of multi-part RAR sets
        if _MULTIPART_RAR.search(path.name):
            return _MULTIPART_FIRST.search(path.name) is not None
        return True
'''
    if text.count(rar_detect) != 1:
        raise SystemExit("RAR archive detection block did not match expected source")
    text = text.replace(rar_detect, "", 1)

    rar_func_start = text.index("\ndef _extract_rar_to(")
    sync_start = text.index("\ndef _extract_sync(", rar_func_start)
    text = text[:rar_func_start] + text[sync_start:]

    rar_dispatch = '''    elif s in _RAR_EXTS:
        _extract_rar(archive, dest)
'''
    if text.count(rar_dispatch) != 1:
        raise SystemExit("RAR extraction dispatch block did not match expected source")
    text = text.replace(rar_dispatch, "", 1)
    old_7z_error = '    raise RuntimeError("No 7z binary found (install p7zip-full in the container)")\n'
    if text.count(old_7z_error) != 1:
        raise SystemExit("old 7z missing-binary error did not match expected source")
    text = text.replace(
        old_7z_error,
        '    raise RuntimeError("No 7z binary found (install 7zip in the container)")\n',
        1,
    )
    nested_rar = '                            nested_archives.extend(subdir.rglob("*.rar"))\n'
    if text.count(nested_rar) != 1:
        raise SystemExit("nested RAR scan did not match expected source")
    text = text.replace(nested_rar, "", 1)
    extractor.write_text(text, encoding="utf-8")

    test_extractor = ROOT / "backend/tests/test_extractor.py"
    text = test_extractor.read_text(encoding="utf-8")
    for old, new in (
        ('    ("archive.rar", True),\n', '    ("archive.rar", False),\n'),
        ('    ("archive.r00", True),   # first multi-part\n', '    ("archive.r00", False),\n'),
        ('    ("archive.part1.rar", True),   # first part\n', '    ("archive.part1.rar", False),\n'),
        ('    ("archive.part01.rar", True),  # first part (zero-padded)\n', '    ("archive.part01.rar", False),\n'),
    ):
        if text.count(old) != 1:
            raise SystemExit(f"test_extractor expected line missing: {old!r}")
        text = text.replace(old, new, 1)
    test_extractor.write_text(text, encoding="utf-8")

    replace_one(
        "README.md",
        "Configure optional archive extraction. DebridPulse enforces per-archive file-count, expanded-size, and compression-ratio limits. External 7z/RAR extraction is performed in an isolated staging directory and validated before files are merged into the download tree.",
        "Configure optional archive extraction. DebridPulse enforces per-archive file-count, expanded-size, and compression-ratio limits. External 7z extraction is performed in an isolated staging directory and validated before files are merged into the download tree. The official image deliberately does not bundle Debian's non-free RAR plugin, so RAR is not advertised as a supported extraction format.",
    )
    replace_one(
        "CHANGELOG.md",
        "- Added extraction file-count, expanded-size, and compression-ratio budgets; external 7z/RAR extraction now uses isolated staging plus pre/post validation before merging files into the destination.",
        "- Added extraction file-count, expanded-size, and compression-ratio budgets; external 7z extraction now uses isolated staging plus pre/post validation before merging files into the destination.",
    )
    change_heading = "## [1.0.6] — 2026-08-20\n\n"
    modernization = '''## [1.0.6] — 2026-08-20

### Runtime and dependency modernization

- Moved the official Python 3.12 container from Debian Bookworm to Trixie, upgrading bundled aria2 from 1.36 to Debian's 1.37 package and replacing the transitional p7zip package with Debian 7zip.
- Replaced `uvicorn[standard]` with explicit Uvicorn, uvloop, and httptools dependencies; removed unused aiofiles, pydantic-settings, and pycryptodome runtime packages.
- Updated the intentionally retained FastAPI/Uvicorn/multipart/Prometheus and test dependency set and refreshed the vendored Chart.js runtime.
- Stopped advertising RAR extraction because the official Debian image does not bundle the non-free RAR plugin; ZIP, tar-family, gzip/bzip2/xz, 7z, tar.zst, and tar.lzma extraction remain supported.

'''
    changelog = ROOT / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    if text.count(change_heading) != 1:
        raise SystemExit("CHANGELOG 1.0.6 heading not unique")
    changelog.write_text(text.replace(change_heading, modernization, 1), encoding="utf-8")

    test_modernization = '''from pathlib import Path

from services.extractor import is_archive


ROOT = Path(__file__).resolve().parents[2]


def test_runtime_requirements_are_intentional():
    requirements = (ROOT / "backend" / "requirements.in").read_text()
    assert "uvicorn[standard]" not in requirements
    assert "uvicorn==0.52.1" in requirements
    assert "httptools==0.8.0" in requirements
    for removed in ("aiofiles", "pydantic-settings", "pycryptodome"):
        assert removed not in requirements
    assert "prometheus-client==0.26.0" in requirements


def test_container_uses_python312_trixie_and_modern_7zip():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "FROM python:3.12.13-slim-trixie" in dockerfile
    assert "    7zip " in dockerfile
    assert "bookworm" not in dockerfile.lower()
    assert "p7zip-full" not in dockerfile


def test_rar_is_not_claimed_as_supported_by_official_runtime():
    assert is_archive(Path("archive.rar")) is False
    assert is_archive(Path("archive.part1.rar")) is False
    readme = (ROOT / "README.md").read_text()
    assert "does not bundle Debian's non-free RAR plugin" in readme
'''
    (ROOT / "backend/tests/test_runtime_modernization.py").write_text(
        test_modernization,
        encoding="utf-8",
    )

    workflow = ROOT / ".github/workflows/fork-image.yml"
    text = workflow.read_text(encoding="utf-8")
    marker = '''          if [ "$healthy" -ne 1 ]; then
            docker logs "$container_name"
            exit 1
          fi

          expected_version="${{ steps.version.outputs.version }}"
'''
    insertion = '''          if [ "$healthy" -ne 1 ]; then
            docker logs "$container_name"
            exit 1
          fi

          aria2_version="$(docker exec "$container_name" aria2c --version | head -n 1)"
          if ! grep -Eq '^aria2 version 1\\.37\\.' <<< "$aria2_version"; then
            echo "Bundled aria2 version mismatch: expected 1.37.x, got $aria2_version"
            exit 1
          fi

          docker exec "$container_name" 7z i >/dev/null
          if docker exec "$container_name" dpkg-query -W 7zip-rar >/dev/null 2>&1; then
            echo "Official image must not bundle non-free 7zip-rar"
            exit 1
          fi
          docker exec "$container_name" python -c 'from pathlib import Path; from services.extractor import is_archive; assert not is_archive(Path("archive.rar"))'
          docker exec "$container_name" python -c 'import subprocess,tempfile; from pathlib import Path; from services.extractor import _extract_sync; root=Path(tempfile.mkdtemp()); src=root/"payload.txt"; src.write_text("archive-ok"); archive=root/"sample.7z"; subprocess.run(["7z","a","-bd","-y",str(archive),"payload.txt"],cwd=root,check=True,stdout=subprocess.DEVNULL); src.unlink(); out=root/"out"; _extract_sync(archive,out); assert (out/"payload.txt").read_text()=="archive-ok"'

          expected_version="${{ steps.version.outputs.version }}"
'''
    if text.count(marker) != 1:
        raise SystemExit("fork-image smoke marker did not match expected source")
    workflow.write_text(text.replace(marker, insertion, 1), encoding="utf-8")


def reconcile_licenses() -> None:
    manifest_path = ROOT / "licenses/python-runtime.json"
    old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    def norm(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).casefold()

    licenses = {norm(item["name"]): item["license"] for item in old_manifest["packages"]}
    packages = []
    for raw in (ROOT / "backend/requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "--")):
            continue
        name, version = line.split("==", 1)
        version = version.split(";", 1)[0].strip()
        key = norm(name)
        if key not in licenses:
            raise SystemExit(f"New runtime package requires explicit license review: {name} {version}")
        packages.append({"name": name, "version": version, "license": licenses[key]})

    manifest = {
        "schema_version": 1,
        "generated_for": "backend/requirements.txt",
        "packages": packages,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    docs_path = ROOT / "docs/DEPENDENCY_LICENSES.md"
    docs = docs_path.read_text(encoding="utf-8")
    table_start = docs.index("| Package | Version | License |")
    container_start = docs.index("\n## Container components", table_start)
    rows = ["| Package | Version | License |", "|---|---:|---|"]
    for item in packages:
        license_text = item["license"]
        if norm(item["name"]) == "bencode2":
            license_text += " ([bundled notice](../licenses/bencode2-MIT.txt))"
        rows.append(f"| {item['name']} | {item['version']} | {license_text} |")
    docs = docs[:table_start] + "\n".join(rows) + "\n" + docs[container_start:]
    docs = docs.replace("`python:3.12.14-slim-bookworm`", "`python:3.12.13-slim-trixie`")
    docs = docs.replace(
        "| p7zip-full | LGPL-2.1-or-later and package-specific component terms |",
        "| 7zip | LGPL-2.1-or-later and package-specific component terms |",
    )
    docs = docs.replace(
        "| Chart.js | 4.4.1, vendored at `frontend/static/vendor/chart.umd.min.js` |",
        "| Chart.js | 4.5.1, vendored at `frontend/static/vendor/chart.umd.min.js` |",
    )
    source_offer = (
        "Package copyright files and common license texts remain installed in the\n"
        "image. `SOURCE_OFFER.md` explains how to request corresponding source for\n"
        "copyleft-covered binaries.\n"
    )
    replacement = source_offer + (
        "\nThe official image uses Debian Trixie's DFSG-free `7zip` package and does not\n"
        "install `7zip-rar` from Debian non-free. RAR extraction is therefore not a\n"
        "bundled or advertised capability of the official DebridPulse image.\n"
    )
    if docs.count(source_offer) != 1:
        raise SystemExit("dependency license container paragraph did not match expected source")
    docs = docs.replace(source_offer, replacement, 1)
    docs_path.write_text(docs, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("apply", "licenses"))
    args = parser.parse_args()
    if args.phase == "apply":
        apply_changes()
    else:
        reconcile_licenses()


if __name__ == "__main__":
    main()
