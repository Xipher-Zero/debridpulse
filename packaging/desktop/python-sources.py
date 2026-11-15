"""The corresponding source of the copyleft-licensed Python packages the desktop
applications install, exactly as the hash locks pin them.

* Every package licences/python-runtime.json records under a GPL, LGPL, MPL or
  EPL licence (asyncssh, certifi, guessit, hachoir, orjson, sabctools, ...):
  its sdist from PyPI, refused unless its SHA-256 is one the lock
  (backend/requirements.txt) pins for that version.
* pywin32 (Windows only) bundles adodbapi under the LGPL; pywin32 publishes no
  sdist, and adodbapi ships as Python source: its files are taken from the
  exact wheel packaging/desktop/windows/requirements.txt pins (hash-checked),
  with its licence and readme.

Usage: python -I packaging/desktop/python-sources.py <out-dir>
Writes <out-dir>/<sdist>, <out-dir>/pywin32-<version>-adodbapi/ and
<out-dir>/PYTHON-SOURCES.txt (package, version, licence, file, sha256, URL).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COPYLEFT = re.compile(r"\b(?:L?GPL|MPL|EPL)-")


def locked_hashes(lock: Path) -> dict[tuple[str, str], set[str]]:
    pins: dict[tuple[str, str], set[str]] = {}
    current = None
    for line in lock.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", line)
        if match:
            current = (match.group(1).lower().replace("_", "-"), match.group(2))
            pins[current] = set()
        for digest in re.findall(r"--hash=sha256:([0-9a-f]{64})", line):
            if current:
                pins[current].add(digest)
    return pins


def fetch(url: str, digests: set[str], target: Path) -> str:
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest not in digests:
        raise SystemExit(f"{url}: sha256 {digest} is not pinned by the lock")
    target.write_bytes(data)
    return digest


def release_files(name: str, version: str) -> list[dict]:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=60) as response:
        return json.load(response)["urls"]


def main() -> int:
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    lock = locked_hashes(ROOT / "backend" / "requirements.txt")
    manifest = json.loads((ROOT / "licenses" / "python-runtime.json").read_text(encoding="utf-8"))
    rows = []
    for package in manifest["packages"]:
        if not COPYLEFT.search(package["license"]):
            continue
        key = (package["name"].lower().replace("_", "-"), package["version"])
        sdists = [entry for entry in release_files(*key) if entry["packagetype"] == "sdist"]
        if len(sdists) != 1:
            raise SystemExit(f"{key}: expected one sdist on PyPI, found {len(sdists)}")
        digest = fetch(sdists[0]["url"], lock.get(key, set()), out / sdists[0]["filename"])
        rows.append((*key, package["license"], sdists[0]["filename"], digest, sdists[0]["url"]))
    # pywin32's adodbapi (LGPL-2.1-or-later), as the Windows desktop installs it.
    windows = locked_hashes(ROOT / "packaging" / "desktop" / "windows" / "requirements.txt")
    version = next(v for (n, v) in windows if n == "pywin32")
    wheels = [entry for entry in release_files("pywin32", version)
              if entry["filename"] == f"pywin32-{version}-cp312-cp312-win_amd64.whl"]
    if len(wheels) != 1:
        raise SystemExit("the locked pywin32 cp312 win_amd64 wheel is not on PyPI")
    wheel = out / wheels[0]["filename"]
    digest = fetch(wheels[0]["url"], windows[("pywin32", version)], wheel)
    target = out / f"pywin32-{version}-adodbapi"
    with zipfile.ZipFile(wheel) as archive:
        members = [member for member in archive.namelist() if member.startswith("adodbapi/")]
        if not any(member.endswith("license.txt") for member in members):
            raise SystemExit("adodbapi's licence is not in the locked pywin32 wheel")
        for member in members:
            archive.extract(member, target)
    wheel.unlink()
    (target / "SOURCE.txt").write_text(
        f"adodbapi exactly as pywin32 {version} ships it (Python source, LGPL-2.1-or-later: adodbapi/license.txt),\n"
        f"extracted from {wheels[0]['filename']} (sha256 {digest}, pinned by\n"
        f"packaging/desktop/windows/requirements.txt). Upstream: https://github.com/mhammond/pywin32 tag b{version}.\n",
        encoding="utf-8")
    rows.append(("pywin32 (adodbapi)", version, "LGPL-2.1-or-later", target.name + "/", digest, wheels[0]["url"]))
    (out / "PYTHON-SOURCES.txt").write_text(
        "# package  version  licence  file  sha256  url\n" + "".join("\t".join(row) + "\n" for row in rows),
        encoding="utf-8")
    print("".join("\t".join(row[:5]) + "\n" for row in rows), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
