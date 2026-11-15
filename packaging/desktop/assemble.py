"""Assemble DebridPulse's desktop application layout for the platform it runs on.

Run on the target system (Windows x64, macOS, Linux) from a clean checkout with
any Python 3.12; it builds the one layout every desktop package carries:

    <out>/runtime/python/   python-build-standalone CPython 3.12.14 (pinned, hash-checked)
                            + the platform's runtime lock installed --require-hashes
                            (DESKTOP_LOCKS, else backend/requirements.txt; the lock
                            used is recorded in BUILD-INFO.json)
    <out>/backend/ <out>/frontend/static/   the application, unchanged
    <out>/desktop/          the supervisor + the Neutralino shell (pinned v6.10.0,
                            hash-checked) + its derived icons
    <out>/tools/            the bundled native tools (``--tools``: the WS3/WS4
                            recipe outputs, copied as built)
    <out>/backend/usenet/   the bundled Usenet service (``--usenet``): SABnzbd's
                            source at the Dockerfile's pin, hash-checked, where
                            executors.sabnzbd.runtime resolves it; on macOS with
                            DebridPulse's one recorded headless patch applied
    <out>/LICENSE, NOTICE, SOURCE_OFFER.md, LICENSES/, licenses/  (both licence
                            trees kept, whatever the file system's case rules)

``--wheels`` names a directory of locally built wheels (for example Windows'
sabctools from packaging/sabctools) that stand in for the lock's sdists;
each is installed --no-deps and recorded with its hash in BUILD-INFO.json.
Usage: python assemble.py --out DIR [--tools DIR] [--wheels DIR] [--icons DIR] [--usenet]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PBS = "20260901"
PYTHON = "3.12.14"
RUNTIMES = {   # python-build-standalone install_only_stripped, from its SHA256SUMS
    ("Windows", "AMD64"): ("x86_64-pc-windows-msvc", "7c45c9622400d578709a9b2cddbe8124cc21d382409d9f13406d706d28e31b14"),
    ("Darwin", "arm64"): ("aarch64-apple-darwin", "81a359f1cfadd4da11766534c5913791cea55f26e1bb902cacd2a531bb1e4b2b"),
    ("Darwin", "x86_64"): ("x86_64-apple-darwin", "65b195c9cedc1fef6767f044f9822069adbd1bd9204d424ece4628776fdc04bb"),
    ("Linux", "x86_64"): ("x86_64-unknown-linux-gnu", "72748da13197c1fb161e3afeef20a6a385ff24f2165e6e2758e47008e7faba4c"),
}
NEUTRALINO = ("v6.10.0", "e633f480737f229f582d8ec6165a52d789340950aef062560702122389f08329")
SHELLS = {("Windows", "AMD64"): "neutralino-win_x64.exe", ("Darwin", "arm64"): "neutralino-mac_arm64",
          ("Darwin", "x86_64"): "neutralino-mac_x64", ("Linux", "x86_64"): "neutralino-linux_x64"}
# The desktop runtime lock each system installs (others: the canonical lock).
DESKTOP_LOCKS = {"Windows": "packaging/desktop/windows/requirements.txt",
                 "Darwin": "packaging/desktop/macos/requirements.txt"}
LEGAL = ("LICENSE", "NOTICE", "SOURCE_OFFER.md", "VERSION", "CHANGELOG.md")


def fetch(url: str, digest: str, into: Path) -> Path:
    into.parent.mkdir(parents=True, exist_ok=True)
    if not into.exists() or hashlib.sha256(into.read_bytes()).hexdigest() != digest:
        try:   # the lock's CA bundle where it is installed (a Windows Python may not chain the issuer)
            import certifi
            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            context = ssl.create_default_context()
        with urllib.request.urlopen(url, timeout=300, context=context) as response:   # verified TLS
            into.write_bytes(response.read())
    actual = hashlib.sha256(into.read_bytes()).hexdigest()
    if actual != digest:
        into.unlink()
        raise SystemExit(f"{url}: sha256 {actual} is not the pinned {digest}")
    return into


# DebridPulse's one modification of the bundled Usenet service, for the macOS
# bundle only: SABnzbd's macOS menu-bar runner is removed, so the service runs
# headless in the foreground through its own main(), as on every other system
# (with PyObjC importable it would otherwise always start that runner, which
# needs a notification centre the bundled interpreter has none of). Written for
# exactly this release; every identity below is checked and any mismatch stops
# the assembly. The patch ships beside the modified source (corresponding
# source: SOURCE_OFFER.md).
USENET_MACOS_PATCH = {
    "version": "5.1.3",
    "path": "packaging/desktop/macos/sabnzbd-5.1.3-headless.patch",
    "sha256": "d5ad15b5f5799e2feed709b3f886400ab7056a345483d1d6bf25b21b1d67638c",
    "file": "SABnzbd.py",
    "file_sha256_before": "f20b0a7fa96b66856afb851bc63e361f01b9ddea2c19891e5cc1c3a4b644bd4e",
    "file_sha256_after": "dfcf164eb31ed3975414c0ba459c4ec51e266f9b006bb78e38e289d555068206",
}
USENET_PATCH_COPY = "DEBRIDPULSE-MODIFICATIONS.patch"


def usenet_service(cache: Path, into: Path, headless_patch: bool = False) -> dict:
    """The Usenet service at the image's own pin (the Dockerfile's
    USENET_SERVICE_VERSION and USENET_SERVICE_SHA256, never restated here),
    unpacked as the image unpacks it -- the source tree, top directory
    stripped -- without the service's own test suite (``tests/``: data files
    only, whose non-ASCII names a macOS HFS+ image renormalizes, breaking the
    bundle's code seal). Its Python dependencies come from the lock. With
    ``headless_patch`` (macOS), USENET_MACOS_PATCH is applied to that copy."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    version = re.search(r"^ARG USENET_SERVICE_VERSION=(\d+\.\d+\.\d+)$", dockerfile, re.M).group(1)
    digest = re.search(r"^ARG USENET_SERVICE_SHA256=([0-9a-f]{64})$", dockerfile, re.M).group(1)
    name = f"SABnzbd-{version}-src.tar.gz"
    archive = fetch(f"https://github.com/sabnzbd/sabnzbd/releases/download/{version}/{name}", digest, cache / name)
    into.mkdir(parents=True)
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if len(parts) < 2 or parts[1] == "tests":
                continue
            member.name = str(Path(*parts[1:]))
            tar.extract(member, into, filter="data")
    if not (into / "SABnzbd.py").is_file():
        raise SystemExit(f"{name}: no SABnzbd.py at its top")
    applied = apply_usenet_patch(version, into) if headless_patch else None
    return {"version": version, "source": name, "sha256": digest, "patch": applied}


def apply_usenet_patch(version: str, into: Path) -> dict:
    pin = USENET_MACOS_PATCH
    if version != pin["version"]:
        raise SystemExit(f"the Usenet service pin is {version}; {pin['path']} is written for {pin['version']} only")
    patch = ROOT / pin["path"]
    target = into / pin["file"]
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()  # noqa: E731
    for path, expected in ((patch, pin["sha256"]), (target, pin["file_sha256_before"])):
        if sha(path) != expected:
            raise SystemExit(f"{path}: sha256 {sha(path)}, expected {expected}")
    subprocess.run(["patch", "-p1", "-N", "-s", "-d", str(into), "-i", str(patch)], check=True)
    leftovers = sorted(str(path.relative_to(into)) for pattern in ("*.orig", "*.rej") for path in into.rglob(pattern))
    if leftovers or sha(target) != pin["file_sha256_after"]:
        raise SystemExit(f"{pin['path']} did not apply exactly: sha256 {sha(target)}, leftovers {leftovers}")
    shutil.copyfile(patch, into / USENET_PATCH_COPY)
    return {"path": pin["path"], "sha256": pin["sha256"], "file": pin["file"],
            "file_sha256_before": pin["file_sha256_before"], "file_sha256_after": pin["file_sha256_after"],
            "shipped_as": f"backend/usenet/{USENET_PATCH_COPY}"}


def copytree(source: Path, target: Path) -> None:
    shutil.copytree(source, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "tests", "node_modules"))


def icons(shell: Path) -> None:
    """The application icon from the canonical logo, the tray icon from the
    canonical favicon (frontend/static), at the sizes each system asks for."""
    out = shell / "resources" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    magick = shutil.which("magick") or shutil.which("convert")
    if not magick:
        raise SystemExit("ImageMagick is needed to derive the icons")
    static = ROOT / "frontend" / "static"
    render = lambda source, size, target: subprocess.run(  # noqa: E731
        [magick, "-background", "none", "-density", "384", str(source), "-resize", f"{size}x{size}", str(target)],
        check=True)
    render(static / "logo.svg", 512, out / "app.png")
    render(static / "favicon.svg", 64, out / "tray.png")
    render(static / "logo.svg", 256, out / "app-256.png")
    subprocess.run([magick, str(out / "app-256.png"), "-define", "icon:auto-resize=256,128,64,48,32,16",
                    str(out / "app.ico")], check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--tools", type=Path)
    parser.add_argument("--wheels", type=Path)
    parser.add_argument("--requirements", type=Path, help="another hash lock (an interim test system only, "
                                                          "e.g. the Intel mac-vm's cryptography substitution; "
                                                          "recorded in BUILD-INFO.json)")
    parser.add_argument("--icons", type=Path, help="icons already derived by this script elsewhere "
                                                   "(a system without ImageMagick)")
    parser.add_argument("--usenet", action="store_true", help="bundle the Usenet service (SABnzbd) at the image's pin")
    parser.add_argument("--cache", type=Path, default=Path(tempfile.gettempdir()) / "dp-desktop-cache")
    args = parser.parse_args()
    key = (platform.system(), platform.machine())
    if key not in RUNTIMES:
        raise SystemExit(f"no desktop runtime is pinned for {key}")
    out = args.out.resolve()
    if out.exists():
        shutil.rmtree(out)
    triple, digest = RUNTIMES[key]
    name = f"cpython-{PYTHON}+{PBS}-{triple}-install_only_stripped.tar.gz"
    archive = fetch(f"https://github.com/astral-sh/python-build-standalone/releases/download/{PBS}/{name.replace('+', '%2B')}",
                    digest, args.cache / name)
    (out / "runtime").mkdir(parents=True)
    with tarfile.open(archive) as tar:
        tar.extractall(out / "runtime", filter="data")
    python = out / "runtime" / "python" / ("python.exe" if os.name == "nt" else "bin/python3")
    # The canonical lock; on Windows and macOS that platform's desktop
    # resolution of it (the same pins, plus the platform's own dependencies):
    # packaging/desktop/{windows,macos}/requirements.txt.
    platform_lock = DESKTOP_LOCKS.get(key[0], "backend/requirements.txt")
    requirements = (args.requirements or ROOT / platform_lock).resolve()
    lock = {"path": (requirements.relative_to(ROOT).as_posix() if requirements.is_relative_to(ROOT)
                     else str(requirements)),
            "sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
            "override": args.requirements is not None}
    built = sorted(args.wheels.glob("*.whl")) if args.wheels else []
    if built:
        # A locally built wheel replaces its project's line in the lock.
        names = {wheel.name.split("-")[0].replace("_", "-").lower() for wheel in built}
        lines, skipping = [], False
        for line in requirements.read_text(encoding="utf-8").splitlines():
            head = line.split("==")[0].strip().lower() if "==" in line and not line.startswith(" ") else None
            if head is not None:
                skipping = head in names
            if not skipping:
                lines.append(line)
        filtered = args.cache / "requirements-desktop.txt"
        filtered.write_text("\n".join(lines) + "\n", encoding="utf-8")
        requirements = filtered
    subprocess.run([str(python), "-I", "-m", "pip", "install", "--no-cache-dir", "--require-hashes", "--no-deps",
                    "-r", str(requirements)], check=True)
    for wheel in built:
        subprocess.run([str(python), "-I", "-m", "pip", "install", "--no-cache-dir", "--no-deps", str(wheel)],
                       check=True)
    subprocess.run([str(python), "-I", "-m", "pip", "check"], check=True)
    copytree(ROOT / "backend", out / "backend")
    usenet = (usenet_service(args.cache, out / "backend" / "usenet", headless_patch=key[0] == "Darwin")
              if args.usenet else None)
    copytree(ROOT / "frontend" / "static", out / "frontend" / "static")
    (out / "desktop").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "desktop" / "supervisor.py", out / "desktop" / "supervisor.py")
    copytree(ROOT / "desktop" / "shell", out / "desktop" / "shell")
    tag, neu_digest = NEUTRALINO
    zipped = fetch(f"https://github.com/neutralinojs/neutralinojs/releases/download/{tag}/neutralinojs-{tag}.zip",
                   neu_digest, args.cache / f"neutralinojs-{tag}.zip")
    with zipfile.ZipFile(zipped) as bundle:
        # DebridPulse's own name: the webview keys its profile on the program name.
        target = out / "desktop" / "shell" / ("debridpulse-shell.exe" if os.name == "nt" else "debridpulse-shell")
        target.write_bytes(bundle.read(SHELLS[key]))
        target.chmod(0o755)
    if args.icons:
        copytree(args.icons, out / "desktop" / "shell" / "resources" / "icons")
    else:
        icons(out / "desktop" / "shell")
    if args.tools:
        copytree(args.tools, out / "tools")
    for item in LEGAL:
        if (ROOT / item).exists():
            shutil.copy2(ROOT / item, out / item)
    # Both licence trees, merged only where the file system folds case; no file
    # may be lost to that merge.
    files = []
    for tree in ("LICENSES", "licenses"):
        if (ROOT / tree).is_dir():
            for path in sorted((ROOT / tree).rglob("*")):
                if path.is_file():
                    destination = out / tree / path.relative_to(ROOT / tree)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)
                    files.append((path, destination))
    lost = [str(source) for source, destination in files if not destination.exists()
            or destination.read_bytes() != source.read_bytes()]
    if lost:
        raise SystemExit(f"licence files lost to case folding: {lost}")
    info = {"python": PYTHON, "python_build_standalone": PBS, "runtime": name, "runtime_sha256": digest,
            "neutralino": tag, "neutralino_zip_sha256": neu_digest, "platform": list(key), "lock": lock,
            "local_wheels": {wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest() for wheel in built},
            "licence_files": len(files), "usenet_service": usenet}
    (out / "BUILD-INFO.json").write_text(json.dumps(info, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(info, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
