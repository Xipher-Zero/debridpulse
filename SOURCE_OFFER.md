# Corresponding source offer

The complete source code for DownloadPulse is available from the repository identified
by the container image's `org.opencontainers.image.source` label. The exact
revision used to build an image is recorded in its
`org.opencontainers.image.revision` label.

For GPL- or LGPL-covered third-party binaries distributed in an official DownloadPulse
container image or desktop installer, Chris Moore offers to provide the complete
corresponding source code, including the material needed to rebuild that
software, to any third party for no more than the reasonable cost of physically
providing the source. This offer is valid for three years after the last
distribution of the applicable image version or installer.

Request source using the repository's dedicated source-request form:
<https://github.com/Xipher-Zero/downloadpulse/issues/new?template=source_request.yml>.
Include:

- the full container image name and digest, or the desktop installer's file
  name and SHA-256 (both are in its `SHA256SUMS` and `MANIFEST.md`);
- the target architecture;
- the package name, if known; and
- a preferred machine-readable delivery method.

The image preserves Debian package copyright files under `/usr/share/doc` and
common license texts under `/usr/share/common-licenses`. Its published SBOM
attestation records the installed package versions needed to identify the
corresponding source precisely.

## Where the corresponding source is

The written offer above remains available for any of the following.

- **DownloadPulse:** the repository named by the image's
  `org.opencontainers.image.source` label, at the exact commit in its
  `org.opencontainers.image.revision` label.
- **SABnzbd (the bundled Usenet service):** its complete upstream source
  release is unpacked unchanged in the image at `/app/usenet`. It is also
  published upstream at `https://github.com/sabnzbd/sabnzbd/releases/tag/<version>`.
  - **Modified in the macOS desktop application only.** That copy, in
    `DownloadPulse.app/Contents/Resources/app/backend/usenet`, is the same
    hash-checked release (without its `tests/` folder) with one change by
    DownloadPulse (2026-11-15). SABnzbd's macOS menu-bar runner is removed from
    `SABnzbd.py`, so it runs headless through its own `main()`, and the
    changed file says so where it was changed. The change is
    `packaging/desktop/macos/sabnzbd-5.1.3-headless.patch` in the DownloadPulse
    repository, and the same patch ships beside the modified source as
    `DOWNLOADPULSE-MODIFICATIONS.patch`. The modified copy is installed as
    source, so it is its own corresponding source. The container image
    carries SABnzbd unmodified.
- **Python packages:** each package and version is listed in
  `licenses/python-runtime.json` under its PyPI name and version, where its
  published source can be retrieved. Pure-Python packages are installed as source in the image
  (`/usr/local/lib/python3.12/site-packages`).
- **Debian packages, including rsync:** the exact version of every installed
  Debian package is recorded in the image's SBOM attestation. Its source
  package is available from Debian's archive, either with
  `apt-get source <package>=<version>` on Debian trixie or from
  `https://snapshot.debian.org/package/<source package>/<version>/`.
  - The rsync binary, `/usr/bin/rsync` (GPL-3.0-or-later), is installed from
    Debian's `rsync` package. In the image built from commit
    `a78055bdfb96bbc59b32a42150c5b0d648085c26` its version is
    `3.5.0+ds1-0+deb13u1`.
  - The corresponding source for that version is
    `https://snapshot.debian.org/package/rsync/3.5.0%2Bds1-0%2Bdeb13u1/`
    (`rsync_3.5.0+ds1-0+deb13u1.dsc`, `rsync_3.5.0+ds1.orig.tar.xz` and
    `rsync_3.5.0+ds1-0+deb13u1.debian.tar.xz`), or
    `apt-get source rsync=3.5.0+ds1-0+deb13u1`.
  - An image built later may carry a newer Debian stable update; its SBOM
    names that version.
- **DownloadPulse's aria2 and FFmpeg packages:** these are rebuilt from Debian's
  source. Each package artifact the Dockerfile names by digest carries its
  complete corresponding source in `/source/`: Debian's source package plus
  DownloadPulse's patches or build script, which are also in this repository
  under `packaging/aria2/` and `packaging/ffmpeg/`
  (`docs/SUPPLY_CHAIN_POLICY.md`, sections 4a and 4b).

## The desktop applications

Every desktop installer the Desktop Artifacts workflow builds (the Windows
EXE, the macOS DMG and the Linux Flatpak) is handed over together with its
complete corresponding source, built from the same commit in the same run:
`DownloadPulse-<version>-desktop-sources-<first 12 characters of the commit>.tar`,
in the same download, kept as long as the installers, and listed with them in
`SHA256SUMS` and `MANIFEST.md`. Its `SOURCES.md` lists every file with its
SHA-256. It holds:

- `downloadpulse/`: DownloadPulse at that commit, including every packaging recipe
  and patch (`packaging/`), the desktop shell glue and the macOS SABnzbd patch;
- `macos-and-flatpak-tools/`: the macOS and Flatpak helpers' sources as their
  recipes use them -- Debian's signed source packages (`.dsc` and the trees
  with Debian's and DownloadPulse's patches applied) of aria2, OpenSSL, c-ares,
  libssh2, FFmpeg, par2cmdline and zstd; rsync 3.5.2; and the 7-Zip 25.01
  source (sources only: the official 7-Zip and RAR binary releases the recipes
  take unmodified are not in it);
- `windows-tools/`: each Windows recipe's own `source/` -- the source packages
  and upstream tarballs of aria2 and the libraries built into it, of FFmpeg,
  par2 and zstd, each recipe script, the netless launcher's C source and the
  pinned digests of the official 7-Zip and UnRAR releases (the 7-Zip source is
  in `macos-and-flatpak-tools/`; UnRAR is freeware without a source obligation)
  --, sabctools (its sdist and DownloadPulse's patch), and
  `rsync-and-msys2-runtime/`: the rsync helper's pinned recipe inputs and the
  MSYS2 source package of every MSYS2 runtime DLL shipped beside it
  (`SOURCES` names each one and where MSYS2 publishes it), and `nsis/`: the
  source of NSIS 3.11, whose installer stub every Windows installer carries;
- `sabnzbd/`: the SABnzbd release every desktop package bundles, as published;
- `python/`: the exact source of every copyleft-licensed Python package the
  desktop applications install, hash-checked against the locks
  (`packaging/desktop/python-sources.py`; `PYTHON-SOURCES.txt` lists them):
  the sdists of asyncssh (used under GPL-2.0-or-later), certifi (MPL-2.0),
  guessit (LGPL-3.0-or-later), hachoir (GPL-2.0-only), orjson (MPL-2.0 parts)
  and sabctools (GPL-2.0-or-later), and, for Windows, adodbapi
  (LGPL-2.1-or-later) exactly as pywin32 ships it.

The other Python packages, the bundled Python runtime (python-build-standalone)
and the Neutralino shell are under permissive licences and are not in it; each
Python package's source is on PyPI under the name and version it records.
The two Microsoft components the Windows installer carries -- the WebView2
bootstrapper and the Visual C++ x64 Redistributable installer -- are not open
source, are carried unmodified under Microsoft's own terms (not DownloadPulse's
GPL), and have no corresponding source to offer. The notices of everything an
installed application carries are inside it: `LICENSE`, `NOTICE`, this file,
`docs/DEPENDENCY_LICENSES.md` (shown in Help), `LICENSES/`, `licenses/`,
`licenses/desktop/` (the Python runtime's and the native shell's components),
`tools/copyright/` (each native helper and what is built into it),
`tools/rsync/licenses/` on Windows (rsync and each MSYS2 runtime package), and
each Python package's own licence files in its installed metadata.

If you received a desktop installer without its source archive, or after the
download expired, the written offer above applies: name the installer's file
and SHA-256 in the request.
