import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_one(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match, found {count}: {old[:100]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


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

    replace_one("Dockerfile", "FROM python:3.12.14-slim-bookworm", "FROM python:3.12.14-slim-trixie")
    replace_one(
        "Dockerfile",
        "RUN apt-get update && apt-get install -y --no-install-recommends \\\n    aria2 \\\n    curl \\\n    gosu \\\n    p7zip-full \\\n",
        "RUN sed -ri 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \\\n    && apt-get update && apt-get install -y --no-install-recommends \\\n    aria2 \\\n    curl \\\n    gosu \\\n    7zip \\\n    7zip-rar \\\n",
    )
    replace_one("backend/services/manager_v2.py", "import aiofiles\n", "")
    replace_one(
        "backend/services/extractor.py",
        "  2. System binary `7z` (from p7zip-full) for .7z, .tar.zst, .tar.lzma, and RAR",
        "  2. System binary `7z` (Debian 7zip + 7zip-rar) for .7z, .tar.zst, .tar.lzma, and RAR",
    )
    replace_one(
        "backend/services/extractor.py",
        "No 7z binary found (install p7zip-full in the container)",
        "No 7z binary found (install 7zip in the container)",
    )
    replace_one(
        "backend/core/config.py",
        "# Optional password applied to all archive extractions (7z -p and unrar -p).",
        "# Optional password applied to archive extraction through the bundled 7z-compatible tooling.",
    )

    heading = "## [1.0.6] — 2026-08-20\n\n"
    section = '''## [1.0.6] — 2026-08-20

### Runtime and dependency modernization

- Moved the official Python 3.12.14 container from Debian Bookworm to Trixie, upgrading bundled aria2 from 1.36 to Debian's 1.37 package.
- Replaced `uvicorn[standard]` with explicit Uvicorn, uvloop, and httptools dependencies; removed unused aiofiles, pydantic-settings, and pycryptodome runtime packages while retaining the live Prometheus exporter dependency.
- Replaced legacy p7zip packaging with Debian 7zip and deliberately retained RAR extraction through Trixie's `7zip-rar` plugin; its non-free unRAR license restriction is documented in the runtime license inventory.
- Updated the intentionally retained FastAPI/Uvicorn/multipart/Prometheus and test dependency set and refreshed the vendored Chart.js runtime.

'''
    changelog = ROOT / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    if text.count(heading) != 1:
        raise SystemExit("CHANGELOG 1.0.6 heading not unique")
    changelog.write_text(text.replace(heading, section, 1), encoding="utf-8")

    test_source = '''from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_runtime_requirements_are_intentional():
    requirements = (ROOT / "backend" / "requirements.in").read_text()
    assert "uvicorn[standard]" not in requirements
    assert "uvicorn==0.52.1" in requirements
    assert "httptools==0.8.0" in requirements
    for removed in ("aiofiles", "pydantic-settings", "pycryptodome"):
        assert removed not in requirements
    assert "prometheus-client==0.26.0" in requirements


def test_container_uses_python312_trixie_and_rar_plugin():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "FROM python:3.12.14-slim-trixie" in dockerfile
    assert "Components: main non-free" in dockerfile
    assert "    7zip " in dockerfile
    assert "    7zip-rar " in dockerfile
    assert "bookworm" not in dockerfile.lower()
    assert "p7zip-full" not in dockerfile
'''
    (ROOT / "backend/tests/test_runtime_modernization.py").write_text(test_source, encoding="utf-8")

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

          docker exec "$container_name" dpkg-query -W 7zip 7zip-rar >/dev/null
          docker exec "$container_name" 7z i | grep -Eiq 'rar'
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

    license_map = {norm(item["name"]): item["license"] for item in old_manifest["packages"]}
    packages = []
    for raw in (ROOT / "backend/requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "--")):
            continue
        name, version = line.split("==", 1)
        version = version.split(";", 1)[0].strip()
        key = norm(name)
        if key not in license_map:
            raise SystemExit(f"New runtime package requires explicit license review: {name} {version}")
        packages.append({"name": name, "version": version, "license": license_map[key]})

    manifest_path.write_text(
        json.dumps({"schema_version": 1, "generated_for": "backend/requirements.txt", "packages": packages}, indent=2) + "\n",
        encoding="utf-8",
    )

    docs_path = ROOT / "docs/DEPENDENCY_LICENSES.md"
    docs = docs_path.read_text(encoding="utf-8")
    table_start = docs.index("| Package | Version | License |")
    container_start = docs.index("\n## Container components", table_start)
    rows = ["| Package | Version | License |", "|---|---:|---|"]
    for item in packages:
        lic = item["license"]
        if norm(item["name"]) == "bencode2":
            lic += " ([bundled notice](../licenses/bencode2-MIT.txt))"
        rows.append(f"| {item['name']} | {item['version']} | {lic} |")
    docs = docs[:table_start] + "\n".join(rows) + "\n" + docs[container_start:]
    docs = docs.replace("`python:3.12.14-slim-bookworm`", "`python:3.12.14-slim-trixie`")
    docs = docs.replace(
        "| p7zip-full | LGPL-2.1-or-later and package-specific component terms |",
        "| 7zip | LGPL-2.1-or-later and package-specific component terms |\n| 7zip-rar | unRAR restricted freeware license (Debian non-free); RAR extraction only |",
    )
    docs = docs.replace(
        "| Chart.js | 4.4.1, vendored at `frontend/static/vendor/chart.umd.min.js` |",
        "| Chart.js | 4.5.1, vendored at `frontend/static/vendor/chart.umd.min.js` |",
    )
    marker = "Package copyright files and common license texts remain installed in the\nimage. `SOURCE_OFFER.md` explains how to request corresponding source for\ncopyleft-covered binaries.\n"
    addition = marker + "\nRAR extraction is provided by Debian Trixie's `7zip-rar` package from the non-free\ncomponent. Its RAR codec is covered by the unRAR license restriction rather than\nthe LGPL terms of ordinary 7zip. The authoritative license text is preserved in\nthe image at `/usr/share/doc/7zip-rar/copyright` and the package is represented\nin the image SBOM.\n"
    if docs.count(marker) != 1:
        raise SystemExit("container license paragraph did not match expected source")
    docs = docs.replace(marker, addition, 1)
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
