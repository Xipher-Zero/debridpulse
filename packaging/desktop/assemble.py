"""Assemble DownloadPulse's desktop application layout for the platform it runs on.

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
                            DownloadPulse's one recorded headless patch applied; on
                            Windows with its helpers (from --tools) where it runs them
    <out>/LICENSE, NOTICE, SOURCE_OFFER.md, docs/DEPENDENCY_LICENSES.md, LICENSES/,
                            licenses/ (+ licenses/desktop/ from packaging/desktop/notices) (both licence
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
                 "Darwin": "packaging/desktop/macos/requirements.txt",
                 "Linux": "packaging/desktop/linux/requirements.txt"}
# docs/DEPENDENCY_LICENSES.md: the licence inventory Help shows as its third-party document.
LEGAL = ("LICENSE", "NOTICE", "SOURCE_OFFER.md", "VERSION", "CHANGELOG.md", "docs/DEPENDENCY_LICENSES.md")


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


# DownloadPulse's one modification of the bundled Usenet service, for the macOS
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
    "sha256": "dab8ee2916022f2517adf4efdd91402c85f67c797b9b0111fa6d05a70ec76fe3",
    "file": "SABnzbd.py",
    "file_sha256_before": "f20b0a7fa96b66856afb851bc63e361f01b9ddea2c19891e5cc1c3a4b644bd4e",
    "file_sha256_after": "7ac22288ef89dee77da2f70db329c715c49e9b75e1921c190d2edadaeb8461a9",
}
USENET_PATCH_COPY = "DOWNLOADPULSE-MODIFICATIONS.patch"
# SABnzbd on Windows runs its helpers only from fixed places under its own
# directory (sabnzbd/newsunpack.py find_programs), never from PATH, and
# refuses to download without par2: the bundled tools are copied there too.
USENET_WINDOWS_HELPERS = {"par2.exe": "win/par2/par2.exe", "UnRAR.exe": "win/unrar/UnRAR.exe",
                          "7za.exe": "win/7zip/7za.exe"}


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
    # Linux: the tray icon as StatusNotifierItem pixmaps -- the favicon's raw
    # RGBA at the sizes a tray host picks from (the supervisor sends them).
    for size in (22, 32, 48, 64):
        subprocess.run([magick, "-background", "none", "-density", "384", str(static / "favicon.svg"),
                        "-resize", f"{size}x{size}", "-depth", "8", f"RGBA:{out / f'tray-{size}.rgba'}"], check=True)
    # macOS: the menu bar's template image (the menu bar tints it for its
    # appearance), derived from the same favicon: its mark alone, the arrow
    # and the bucket, without the tile behind them, in black. 36 pixels
    # marked 144 dpi: an 18-point image, sharp at Retina scale.
    template = out.parent / "trayTemplate.svg"
    template.write_text(template_svg((static / "favicon.svg").read_text(encoding="utf-8")), encoding="utf-8")
    subprocess.run([magick, "-background", "none", "-density", "768", str(template), "-trim", "+repage",
                    "-resize", "32x32", "-gravity", "center", "-extent", "36x36",
                    "-units", "PixelsPerInch", "-density", "144", str(out / "trayTemplate.png")], check=True)
    template.unlink()
    # macOS: the DMG window's background (build-app.sh), at 1x and 2x.
    background = out.parent / "dmg-background.svg"
    background.write_text(dmg_background_svg(), encoding="utf-8")
    width, height = DMG_WINDOW
    for scale, name in ((1, "dmg-background.png"), (2, "dmg-background@2x.png")):
        subprocess.run([magick, "-background", "none", "-density", "384", str(background),
                        "-resize", f"{width * scale}x{height * scale}!",
                        "-units", "PixelsPerInch", "-density", str(72 * scale), str(out / name)], check=True)
    background.unlink()
    installer_bitmaps(magick, out, static)


# Windows installer (packaging/desktop/windows/installer.nsi, Modern UI 2): the
# Welcome and Finish pages' side picture and the inner pages' header picture,
# each at twice the size Modern UI lays out at 96 dpi (164 x 314 and 150 x 57)
# so a scaled display draws them sharp; 24-bit BMP, as Modern UI reads them.
INSTALLER_SIDE, INSTALLER_HEADER = (328, 628), (300, 114)


def installer_bitmaps(magick: str, out: Path, static: Path) -> None:
    """The installer's pictures, from the sign-in page's composition: the
    deep field with its sparse stars and soft glow, the application's mark,
    and the page title's EKG beneath it. No words, so nothing to localize and
    no font to find. Layered by ImageMagick from the canonical logo and pulse."""
    width, height = INSTALLER_SIDE
    work = out.parent / "installer-work"
    work.mkdir(exist_ok=True)
    field = work / "field.svg"
    field.write_text(installer_field_svg(width, height), encoding="utf-8")
    run = lambda *args: subprocess.run([magick, *map(str, args)], check=True)  # noqa: E731
    run("-background", "none", "-density", "96", field, "-resize", f"{width}x{height}!", work / "field.png")
    run("-background", "none", "-density", "768", static / "logo.svg", "-resize", "176x176", work / "mark.png")
    run("-background", "none", "-density", "1536", static / "icons" / "dp" / "shell-pulse.svg",
        "-resize", "296x", work / "pulse.png")
    run(work / "field.png", work / "mark.png", "-gravity", "north", "-geometry", "+0+150", "-composite",
        work / "pulse.png", "-gravity", "north", "-geometry", "+0+352", "-composite",
        "-background", "#0b0d1c", "-alpha", "remove", "-type", "TrueColor", f"BMP3:{out / 'installer-side.bmp'}")
    # The header: the mark at its right end, on the window's own white.
    hw, hh = INSTALLER_HEADER
    run("-background", "none", "-density", "768", static / "logo.svg", "-resize", f"{hh - 18}x{hh - 18}",
        work / "mark-header.png")
    run("-size", f"{hw}x{hh}", "xc:#ffffff", work / "mark-header.png", "-gravity", "east", "-geometry", "+12+0",
        "-composite", "-alpha", "remove", "-type", "TrueColor", f"BMP3:{out / 'installer-header.bmp'}")
    shutil.rmtree(work)


def installer_field_svg(width: int, height: int) -> str:
    """The side picture's field: the sign-in page's deep blue-violet, a soft
    glow where the mark stands, and a sparse, fixed scatter of stars."""
    stars = []
    seed = 7
    for index in range(46):
        seed = (seed * 1103515245 + 12345) % (1 << 31)
        x = seed % width
        seed = (seed * 1103515245 + 12345) % (1 << 31)
        y = seed % height
        radius = (0.6, 0.9, 1.3)[index % 3]
        opacity = (0.35, 0.55, 0.8)[(index // 3) % 3]
        stars.append(f'<circle cx="{x}" cy="{y}" r="{radius}" fill="#d9ccff" fill-opacity="{opacity}"/>')
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <defs>
    <linearGradient id="field" x1="0" y1="0" x2="0.35" y2="1">
      <stop offset="0" stop-color="#151033"/><stop offset=".55" stop-color="#0d0f24"/><stop offset="1" stop-color="#090b1a"/>
    </linearGradient>
    <radialGradient id="glow" cx="{width / 2}" cy="238" r="170" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="#8a4dff" stop-opacity=".42"/><stop offset="1" stop-color="#8a4dff" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="{width}" height="{height}" fill="url(#field)"/>
  <rect width="{width}" height="{height}" fill="url(#glow)"/>
  {''.join(stars)}
</svg>
"""


# The DMG window (points) and where its two icons stand, shared with
# packaging/desktop/macos/dmg-settings.py.
DMG_WINDOW, DMG_APP, DMG_APPLICATIONS = (660, 400), (170, 190), (490, 190)


def dmg_background_svg() -> str:
    """The DMG window's background: the application's slot and the
    Applications folder's, each in a soft bordered box, and between them an
    arrow in the favicon's own mark gradient -- drag one onto the other. No
    words, so nothing to localize; light, so Finder's labels read on it."""
    (width, height), (ax, ay), (bx, by) = DMG_WINDOW, DMG_APP, DMG_APPLICATIONS
    box = lambda cx, cy: (f'<rect x="{cx - 92}" y="{cy - 96}" width="184" height="200" rx="22" '  # noqa: E731
                          f'fill="#ffffff" fill-opacity=".72" stroke="#c9c8da" stroke-width="2"/>')
    start, end, y = ax + 106, bx - 106, ay - 12
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <defs>
    <linearGradient id="field" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#f6f5fb"/><stop offset="1" stop-color="#e9e8f3"/>
    </linearGradient>
    <linearGradient id="mark" x1="{start}" y1="0" x2="{end}" y2="0" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="#b42cff"/><stop offset=".48" stop-color="#4f67ff"/><stop offset="1" stop-color="#00d4ee"/>
    </linearGradient>
  </defs>
  <rect width="{width}" height="{height}" fill="url(#field)"/>
  {box(ax, ay)}
  {box(bx, by)}
  <path d="M{start} {y}h{end - start - 22}" fill="none" stroke="url(#mark)" stroke-width="8" stroke-linecap="round"/>
  <path d="M{end - 26} {y - 15}l26 15-26 15z" fill="#00d4ee" stroke="#00d4ee" stroke-width="4" stroke-linejoin="round"/>
</svg>
"""


def template_svg(favicon: str) -> str:
    """The favicon's mark as a template: its tile (every ``rect``) and filter
    removed, each painted mark black. Fails if the favicon no longer has the
    shape this expects, rather than deriving a wrong icon."""
    import re
    without_tile, tiles = re.subn(r"\s*<rect\b[^>]*/>", "", favicon)
    marks = len(re.findall(r'(?:fill|stroke)="url\(#mark\)"', without_tile))
    if tiles != 3 or marks != 2 or len(re.findall(r"<path\b", without_tile)) != 2:
        raise SystemExit("favicon.svg changed shape: its menu-bar template must be derived anew")
    return re.sub(r'="url\(#mark\)"', '="#000"', without_tile)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--tools", type=Path)
    parser.add_argument("--wheels", type=Path)
    parser.add_argument("--requirements", type=Path, help="another hash lock (an interim test system only, "
                                                          "e.g. the Intel mac-vm's cryptography substitution; "
                                                          "recorded in BUILD-INFO.json)")
    parser.add_argument("--shell", type=Path, help="a native shell DownloadPulse built (downloadpulse-shell and "
                        "PROVENANCE.json: packaging/desktop/neutralino/build-macos.sh), in place of the released one")
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
    if usenet and key[0] == "Windows":
        if not args.tools:
            raise SystemExit("--usenet on Windows needs --tools: the service's helpers come from them")
        for name, place in USENET_WINDOWS_HELPERS.items():
            target = out / "backend" / "usenet" / place
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(args.tools / name, target)
        usenet["windows_helpers"] = {place: hashlib.sha256((out / "backend" / "usenet" / place).read_bytes()).hexdigest()
                                     for place in USENET_WINDOWS_HELPERS.values()}
    copytree(ROOT / "frontend" / "static", out / "frontend" / "static")
    # The host icons, as the server image unpacks them (packaging/host_icons.py, their one owner).
    sys.path.insert(0, str(ROOT / "packaging"))
    from host_icons import unpack
    unpack(ROOT / "frontend" / "host-icons.parts", out / "frontend" / "static" / "icons" / "hosts")
    (out / "desktop").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "desktop" / "supervisor.py", out / "desktop" / "supervisor.py")
    copytree(ROOT / "desktop" / "shell", out / "desktop" / "shell")
    tag, neu_digest = NEUTRALINO
    zipped = fetch(f"https://github.com/neutralinojs/neutralinojs/releases/download/{tag}/neutralinojs-{tag}.zip",
                   neu_digest, args.cache / f"neutralinojs-{tag}.zip")
    # DownloadPulse's own name: the webview keys its profile on the program name.
    target = out / "desktop" / "shell" / ("downloadpulse-shell.exe" if os.name == "nt" else "downloadpulse-shell")
    if args.shell:   # built from the pinned source with DownloadPulse's patch (macOS)
        target.write_bytes((args.shell / "downloadpulse-shell").read_bytes())
        shell = json.loads((args.shell / "PROVENANCE.json").read_text(encoding="utf-8"))
        if hashlib.sha256(target.read_bytes()).hexdigest() != shell["binary_sha256"]:
            raise SystemExit("the built shell is not the one its provenance names")
    else:            # the released binary of the pinned tag (Windows; Linux, where the Flatpak builds its own)
        with zipfile.ZipFile(zipped) as bundle:
            target.write_bytes(bundle.read(SHELLS[key]))
        shell = {"neutralino": tag, "released_zip_sha256": neu_digest, "binary": SHELLS[key],
                 "binary_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    target.chmod(0o755)
    if args.icons:
        copytree(args.icons, out / "desktop" / "shell" / "resources" / "icons")
    else:
        icons(out / "desktop" / "shell")
    if args.tools:
        copytree(args.tools, out / "tools")
    for item in LEGAL:
        if (ROOT / item).exists():
            (out / item).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / item, out / item)
    # Both licence trees, merged only where the file system folds case; no file
    # may be lost to that merge. The desktop's own notices (the bundled runtime's
    # and the native shell's components: packaging/desktop/notices/README.md)
    # go to licenses/desktop/.
    files = []
    for tree, target in (("LICENSES", "LICENSES"), ("licenses", "licenses"),
                         ("packaging/desktop/notices", "licenses/desktop")):
        if (ROOT / tree).is_dir():
            for path in sorted((ROOT / tree).rglob("*")):
                if path.is_file():
                    destination = out / target / path.relative_to(ROOT / tree)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)
                    files.append((path, destination))
    lost = [str(source) for source, destination in files if not destination.exists()
            or destination.read_bytes() != source.read_bytes()]
    if lost:
        raise SystemExit(f"licence files lost to case folding: {lost}")
    info = {"python": PYTHON, "python_build_standalone": PBS, "runtime": name, "runtime_sha256": digest,
            "neutralino": tag, "neutralino_zip_sha256": neu_digest, "shell": shell, "platform": list(key), "lock": lock,
            "local_wheels": {wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest() for wheel in built},
            "licence_files": len(files), "usenet_service": usenet}
    (out / "BUILD-INFO.json").write_text(json.dumps(info, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(info, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
