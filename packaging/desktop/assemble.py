"""Assemble DebridPulse's desktop application layout for the platform it runs on.

Run on the target system (Windows x64, macOS, Linux) from a clean checkout with
any Python 3.12; it builds the one layout every desktop package carries:

    <out>/runtime/python/   python-build-standalone CPython 3.12.14 (pinned, hash-checked)
                            + backend/requirements.txt installed --require-hashes
    <out>/backend/ <out>/frontend/static/   the application, unchanged
    <out>/desktop/          the supervisor + the Neutralino shell (pinned v6.10.0,
                            hash-checked) + its derived icons
    <out>/tools/            the bundled native tools (``--tools``: the WS3/WS4
                            recipe outputs, copied as built)
    <out>/LICENSE, NOTICE, SOURCE_OFFER.md, LICENSES/, licenses/  (both licence
                            trees kept, whatever the file system's case rules)

``--wheels`` names a directory of locally built wheels (for example Windows'
sabctools from packaging/sabctools) that stand in for the lock's sdists;
each is installed --no-deps and recorded with its hash in BUILD-INFO.json.
Usage: python assemble.py --out DIR [--tools DIR] [--wheels DIR] [--icons DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
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
    # The canonical lock; on Windows its Windows resolution (same pins, plus
    # that platform's own dependencies): packaging/desktop/windows/requirements.txt.
    requirements = args.requirements or ROOT / ("packaging/desktop/windows/requirements.txt" if os.name == "nt"
                                                else "backend/requirements.txt")
    lock = {"path": str(requirements), "sha256": hashlib.sha256(requirements.read_bytes()).hexdigest()}
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
    copytree(ROOT / "frontend" / "static", out / "frontend" / "static")
    (out / "desktop").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "desktop" / "supervisor.py", out / "desktop" / "supervisor.py")
    copytree(ROOT / "desktop" / "shell", out / "desktop" / "shell")
    tag, neu_digest = NEUTRALINO
    zipped = fetch(f"https://github.com/neutralinojs/neutralinojs/releases/download/{tag}/neutralinojs-{tag}.zip",
                   neu_digest, args.cache / f"neutralinojs-{tag}.zip")
    with zipfile.ZipFile(zipped) as bundle:
        target = out / "desktop" / "shell" / ("neutralino.exe" if os.name == "nt" else "neutralino")
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
            "licence_files": len(files)}
    (out / "BUILD-INFO.json").write_text(json.dumps(info, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(info, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
