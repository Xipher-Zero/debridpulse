# Runtime dependency license inventory

> **Scope:** This is the dependency/license inventory for the `1.0.13` release tree — the Universal Transfer Core with the AllDebrid, Real-Debrid, TorBox, Debrid-Link and Premiumize providers, the General HTTP(S), FTP/SFTP, SCP, rsync and WebDAV sources, Metalink, Usenet and Media Downloads, executed by aria2, rsync, the bundled Usenet acquisition service and the Media Downloads executor. Further `1.0.13` expansion work must trigger a fresh third-party/license review if it adds libraries, executors, protocol dependencies, copied/derived code, or other attribution obligations.
This inventory covers every Python package pinned in
`backend/requirements.txt`. Package names and versions are enforced by
`backend/tests/test_license_policy.py`; a dependency change must update both
the lock file and `licenses/python-runtime.json`.

| Package | Version | License |
|---|---:|---|
| aiohappyeyeballs | 2.6.1 | PSF-2.0 |
| aiohttp | 3.14.4 | Apache-2.0 AND MIT |
| aiosignal | 1.4.0 | Apache-2.0 |
| aiosqlite | 0.22.1 | MIT |
| annotated-doc | 0.0.4 | MIT |
| annotated-types | 0.7.0 | MIT |
| anyio | 4.14.2 | MIT |
| apprise | 1.12.0 | BSD-2-Clause |
| argon2-cffi | 25.1.0 | MIT |
| argon2-cffi-bindings | 26.1.0 | MIT; vendored Argon2/BLAKE2 components are CC0-1.0 |
| asyncssh | 2.24.0 | EPL-2.0 OR GPL-2.0-or-later (used under GPL-2.0-or-later) |
| attrs | 26.1.0 | MIT |
| authlib | 1.9.1 | BSD-3-Clause |
| babelfish | 0.6.1 | BSD-3-Clause |
| bcrypt | 5.0.0 | Apache-2.0 |
| bencode2 | 0.3.33 | MIT ([bundled notice](../licenses/bencode2-MIT.txt)) |
| certifi | 2026.10.19 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.1 | MIT |
| cheroot | 11.1.2 | BSD-3-Clause |
| cherrypy | 18.10.0 | BSD-3-Clause |
| click | 8.3.3 | BSD-3-Clause |
| configobj | 5.0.9 | BSD-3-Clause |
| cryptography | 50.0.0 | Apache-2.0 OR BSD-3-Clause |
| ct3 | 3.4.0.post5 | MIT |
| deno | 2.9.8 | MIT |
| fastapi | 0.141.1 | MIT |
| feedparser | 6.0.12 | BSD-2-Clause |
| frozenlist | 1.8.0 | Apache-2.0 |
| google-re2 | 1.1.20251105 | BSD-3-Clause |
| guessit | 4.1.0 | LGPL-3.0-or-later (bundled service dependency; see the copyleft review below) |
| h11 | 0.16.0 | MIT |
| hachoir | 3.3.0 | GPL-2.0-only (bundled service dependency; see the copyleft review below) |
| httpcore | 1.0.9 | BSD-3-Clause |
| httptools | 0.8.0 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| idna | 3.15 | BSD-3-Clause |
| jaraco-classes | 3.4.0 | MIT |
| jaraco-collections | 5.0.0 | MIT |
| jaraco-context | 4.3.0 | MIT |
| jaraco-functools | 4.6.0 | MIT |
| jaraco-text | 3.8.1 | MIT |
| joserfc | 1.7.6 | BSD-3-Clause |
| markdown | 3.10.3 | BSD-3-Clause |
| more-itertools | 11.1.0 | MIT |
| multidict | 6.9.1 | Apache-2.0 |
| oauthlib | 4.0.1 | BSD-3-Clause |
| orjson | 3.11.9 | MPL-2.0 AND (Apache-2.0 OR MIT) |
| portend | 3.2.1 | MIT |
| prometheus-client | 0.26.0 | Apache-2.0 AND BSD-2-Clause |
| propcache | 0.5.2 | Apache-2.0 |
| puremagic | 2.2.0 | MIT |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.4 | MIT |
| pydantic-core | 2.46.4 | MIT |
| pysocks | 1.7.1 | BSD-3-Clause |
| python-dateutil | 2.9.0.post0 | Apache-2.0 AND BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| pytz | 2026.2 | MIT |
| pyyaml | 6.0.3 | MIT |
| rarfile | 4.3 | ISC |
| rebulk | 6.0.1 | MIT |
| requests | 2.34.2 | Apache-2.0 |
| requests-oauthlib | 2.0.0 | ISC |
| sabctools | 9.6.3 | GPL-2.0-or-later (bundled service dependency; see the copyleft review below) |
| setuptools | 84.1.0 | MIT |
| sgmllib3k | 1.0.0 | BSD (variant unspecified upstream) AND PSF-2.0 (derived from CPython sgmllib) ([bundled notice](../licenses/sgmllib3k-BSD.txt)) |
| six | 1.17.0 | MIT |
| starlette | 1.3.1 | BSD-3-Clause |
| tempora | 5.8.1 | MIT |
| typing-extensions | 4.15.0 | PSF-2.0 |
| typing-inspection | 0.4.2 | MIT |
| ujson | 5.13.0 | BSD-3-Clause AND TCL |
| urllib3 | 2.8.0 | MIT |
| uvicorn | 0.52.4 | BSD-3-Clause |
| uvloop | 0.22.1 | MIT OR Apache-2.0 |
| yarl | 1.25.1 | Apache-2.0 |
| yt-dlp | 2026.10.21 | Unlicense |
| yt-dlp-ejs | 0.8.0 | Unlicense AND MIT AND ISC |
| zc-lockfile | 4.0 | ZPL-2.1 |

Native authentication directly depends on `argon2-cffi` for
Argon2id local-password verification, `authlib` for OpenID Connect/JWT protocol
handling, and `httpx` for bounded outbound OIDC discovery/token/JWKS requests.
Provider-runtime hardening directly depends on `google-re2` so
externally supplied AllDebrid applicability expressions execute with RE2's
linear-time matching semantics instead of Python backtracking regex behavior.
Universal evidence acquisition directly depends on `asyncssh`
for bounded SFTP evidence reads (host identity confirmed before authentication,
offset reads only) over connections the downloader egress guard authorizes.
Its only dependencies, `cryptography` and `typing-extensions`, were already
locked. `asyncssh` is dual-licensed EPL-2.0 OR GPL-2.0-or-later; DownloadPulse,
itself GPL-2.0-or-later, uses it under GPL-2.0-or-later. This is the one
reviewed copyleft runtime dependency named in `backend/tests/test_license_policy.py`.
`bcrypt` (Apache-2.0, no Python dependencies) is `asyncssh`'s own backend for
passphrase-encrypted OpenSSH-format private keys -- the default format of
`ssh-keygen` -- which the generalized `username_private_key` sign-in accepts.
The Media Downloads integration directly depends on `yt-dlp` (Unlicense,
installed without extras: its HTTP dependencies were already locked) for the
explicit extractor registry and native HTTP(S)/HLS/DASH downloaders it runs in
its sandboxed worker, on `yt-dlp-ejs` (Unlicense AND MIT AND ISC) for the
JavaScript challenge-solver scripts yt-dlp would otherwise fetch remotely, and on
`deno` (MIT), the wheel that ships the Deno binary those scripts run in, with no
network, file or remote-module permission. None of them is updated or extended
at run time.
Their transitive cryptographic/HTTP dependencies are included in the table and
machine-readable runtime manifest above. Package/license pairs are cross-checked
against the corresponding upstream/PyPI metadata when the lock is generated.

### Deno: a native runtime, and what its MIT declaration does not cover

The `deno` 2.9.8 wheel is not Python code: it redistributes Deno's native
runtime, a single statically linked executable (`/usr/local/bin/deno` in the
image, about 96 MB). That executable reports the components it embeds as
V8 `15.0.245.2` (`15.0.245.2-rusty`) and TypeScript `6.0.3`
(`deno --version`). The wheel's one licence file declares MIT for the Deno
project itself, matching Deno's version-matched upstream licence
(<https://github.com/denoland/deno/blob/v2.9.8/LICENSE.md>). That MIT
declaration is Deno's own; it is not an inventory of the components compiled
into the executable. V8 carries its own BSD-style licence, which it states
covers V8 and not V8's externally maintained libraries
(<https://chromium.googlesource.com/v8/v8/+/refs/tags/15.0.245.2/LICENSE>), and
no other notice for an embedded component ships in the wheel or the image.
**The complete inventory of components embedded in the Deno executable, their
notices, and the redistribution obligations they carry have not been
independently established**; this document therefore does not record the
executable as MIT-covered as a whole, and supplies no notices on its behalf.

### uvloop and google-re2: libraries compiled into their extension modules

Two wheels in the table compile third-party C/C++ libraries statically into their
native extension module, so those libraries ship in the image although they
appear in no package list of their own. This was verified in the hash-pinned
`linux/amd64` and `linux/arm64` wheels of `backend/requirements.txt`. Neither
extension links a shared copy of the library: the symbols are present, and
`ldd` shows only the C/C++ runtime.

| Wheel (licence of the wheel) | Embedded component | Version evidence | Upstream attribution | Licence |
|---|---|---|---|---|
| `uvloop` 0.22.1 (MIT OR Apache-2.0) | libuv | 1.48.0: the version string in `uvloop/loop*.so`, and `vendor/libuv/include/uv/version.h` in the pinned uvloop 0.22.1 sdist | "Copyright (c) 2015-present libuv project contributors." Parts originating from joyent/libuv: "Copyright Joyent, Inc. and other Node contributors." | MIT (both notices), per `vendor/libuv/LICENSE` and `LICENSE-extra`. `LICENSE-extra` also names two externally maintained parts: `tree.h` from FreeBSD (copyright Niels Provos, BSD-2-Clause) and the `inet_pton`/`inet_ntop` implementations in `src/inet.c` (copyright Internet Systems Consortium, Inc., ISC). |
| `google-re2` 1.1.20251105 (BSD-3-Clause, RE2's own licence) | Abseil (abseil-cpp) | LTS `20250512`: the `absl::lts_20250512` namespace in `re2/_re2*.so` | The Abseil Authors (<https://github.com/abseil/abseil-cpp>) | Apache-2.0; abseil-cpp has no NOTICE file at tag `20250512.1` (checked: `NOTICE` and `NOTICE.txt` are absent) |
| `google-re2` 1.1.20251105 | pybind11 | The release is not identifiable from the binary. Its internals ABI identifier is `__pybind11_internals_v5` | "Copyright (c) 2016 Wenzel Jakob, All rights reserved." (<https://github.com/pybind/pybind11>) | BSD-3-Clause (pybind11's `LICENSE`; the text was checked at v2.13.6) |

None of these components' notices ships inside its wheel: uvloop's
`dist-info/licenses` holds only uvloop's own `LICENSE-MIT` and `LICENSE-APACHE`,
and google-re2's holds only RE2's `LICENSE`. Their notices and licence texts
therefore ship in the image under `/app/licenses/`, reproduced verbatim from the
upstream sources named above:

- `libuv-MIT.txt`: libuv's `LICENSE` and `LICENSE-extra`, plus the BSD-2-Clause
  notice of `include/uv/tree.h` and the ISC notice of `src/inet.c`, all from the
  hash-pinned uvloop 0.22.1 sdist's `vendor/libuv/`;
- `abseil-cpp-Apache-2.0.txt`: the Apache License 2.0 text from abseil-cpp at
  tag `20250512.1`;
- `pybind11-BSD-3-Clause.txt`: pybind11's `LICENSE`. The embedded release
  cannot be identified from the binary, but that file is byte-identical in every
  release checked from v2.10.4 to v2.13.6 (sha256 `83965b84…`).

These texts supply the notices the components' licences ask to accompany a
binary distribution. They are not a general legal review of the image; the
copyleft review below records its own disposition.

### macOS desktop runtime additions

The macOS desktop application installs `packaging/desktop/macos/requirements.txt`:
every pin of `backend/requirements.txt` unchanged, plus the packages below: the
two the bundled Usenet service (SABnzbd 5.1.3) imports on macOS, and truststore,
through which the backend verifies certificates with the system's own trust. They ship
only in the macOS desktop bundle, never in the container image, so they are not
in the table or the machine-readable manifest above.

| Package | Version | License |
|---|---:|---|
| pyobjc-core | 12.2.1 | MIT ([bundled notice](../licenses/pyobjc-MIT.txt)) |
| pyobjc-framework-Cocoa | 12.2.1 | MIT ([bundled notice](../licenses/pyobjc-MIT.txt)) |
| truststore | 0.10.4 | MIT (its wheel installs its licence under `truststore-0.10.4.dist-info/licenses/LICENSE`) |

`licenses/pyobjc-MIT.txt` is pyobjc-core 12.2.1's `License.txt` from its
sdist, byte-identical to the `LICENSE.txt` in the pyobjc-framework-Cocoa 12.2.1
wheel (sha256 `0ca04b07…`). It is shipped because the pyobjc-core wheel
carries no licence file. Both wheels link macOS's own `/usr/lib/libffi.dylib`
and bundle no libffi.

### Windows desktop runtime additions

The Windows desktop application installs `packaging/desktop/windows/requirements.txt`:
every pin of `backend/requirements.txt` except `uvloop` (no Windows build),
plus the Windows-only packages below. They ship only in the Windows desktop
installer, never in the container image. Each wheel installs its own licence
files under its `*.dist-info/licenses/`, which the installer ships unchanged.

| Package | Version | License |
|---|---:|---|
| colorama | 0.4.6 | BSD-3-Clause (needed by click on Windows) |
| pywin32 | 312 | PSF (its package metadata); its win32, COM and Pythonwin files carry Mark Hammond's BSD-style notice, and it also bundles adodbapi (LGPL-2.1-or-later), Microsoft's MAPI stub library (MIT) and Scintilla (its own permissive licence), each with its licence file in the wheel. Needed by the bundled Usenet service (SABnzbd) on Windows. |
| truststore | 0.10.4 | MIT (the backend verifies certificates with Windows' own trust) |
| tzdata | 2026.6 | Apache-2.0 (needed by apprise on Windows) |

### Linux desktop runtime additions

The Linux desktop application (the Flatpak) installs
`packaging/desktop/linux/requirements.txt`: every pin of `backend/requirements.txt`
unchanged, plus the package below, the desktop supervisor's session-bus client
(its tray, notices and opening the running instance). It ships only in the
Flatpak, never in the container image. Its wheel installs its own licence file
under `jeepney-0.9.0.dist-info/licenses/LICENSE`, which the Flatpak ships
unchanged.

| Package | Version | License |
|---|---:|---|
| jeepney | 0.9.0 | MIT (pure Python, no dependencies) |

### Desktop native components: licences, redistribution terms, notices and source

What the desktop installers carry besides the Python packages above. "Notice"
is where the installed application holds the licence text; "Source" is where
its corresponding source is handed over (the `*-desktop-sources-*.tar` beside
every installer, `SOURCE_OFFER.md`, "The desktop applications"). Terms marked
[D] were read from the vendor's documentation on 2026-11-15; the rest from the
licence text the component itself ships.

| Component | Packages | Licence | Redistribution condition (as verified) | Notice | Source |
|---|---|---|---|---|---|
| CPython 3.12.14 runtime (python-build-standalone 20260901) and the libraries built into it (OpenSSL, libffi, SQLite, XZ, bzip2, zlib, mpdecimal, expat, Tcl/Tk, ncurses, libedit, libuuid, Berkeley DB) | all | PSF-2.0; Apache-2.0, MIT, BSD, public domain, TCL, X11 | reproduce the notices | `runtime/python/lib/python3.12/LICENSE.txt`, `licenses/desktop/python-build-standalone/` | not copyleft; upstream release |
| Neutralino 6.10.0 shell and the libraries built into it | all | MIT; Boost (asio), MIT (several), WTFPL (portable-file-dialogs), Apache-2.0 (mbedtls), BSD-3-Clause (websocketpp), Microsoft BSD-style (WebView2 loader, Windows) | reproduce the notices; the bundled base64 header states no licence (an earlier form of an MIT project: provenance inferred) | `licenses/desktop/neutralino/` | not copyleft; upstream tag |
| aria2 1.37.0 (Debian's source, DownloadPulse's patch, no BitTorrent) | all | GPL-2.0-or-later with the OpenSSL exception | provide the corresponding source | `tools/copyright/aria2` | sources tar |
| OpenSSL 3.5.7 libcrypto, libssh2 1.11.1, c-ares 1.34.9 (built into aria2; zlib and expat too on Windows) | all | Apache-2.0, BSD-3-Clause, MIT, zlib, MIT | reproduce the notices | `tools/copyright/{openssl,libssh2,c-ares}` (+ `zlib`, `expat` on Windows) | sources tar |
| FFmpeg/ffprobe 7.1.5 (DownloadPulse's capability set, no GPL parts) | all | LGPL-2.1-or-later | provide the corresponding source | `tools/copyright/ffmpeg` | sources tar |
| par2cmdline 0.8.1 (par2cmdline-turbo on Windows) | all | GPL-2.0-or-later | provide the corresponding source | `tools/copyright/par2` | sources tar |
| zstd 1.5.7 | all | BSD-3-Clause OR GPL-2.0 (used under BSD) | reproduce the notice | `tools/copyright/zstd` | sources tar |
| rsync 3.5.2 | all | GPL-3.0-or-later | provide the corresponding source | `tools/copyright/rsync`; Windows `tools/rsync/licenses/rsync/` | sources tar |
| MSYS2 runtime `msys-2.0.dll` (Cygwin) | Windows | LGPL-3.0-or-later with Cygwin's linking exception (its GPL-3.0 parts are not in the DLL) | provide the corresponding source; the DLL stays replaceable beside `rsync.exe` | `tools/rsync/licenses/msys2-runtime/` (`COPYING`, `CYGWIN_LICENSE`, the upstream licence texts) | MSYS2 source package, sources tar |
| MSYS2 libraries beside it: libiconv, libintl (gettext), libidn2, libunistring, liblz4, libxxhash, libzstd, popt, libopenssl | Windows | LGPL-2.1+/LGPL-3+/GPL-2+ (libiconv, libintl, libidn2, libunistring), BSD-2-Clause (lz4, xxhash), BSD (zstd), MIT-style (popt), Apache-2.0 (OpenSSL) | provide the source of the LGPL ones; reproduce every notice | `tools/rsync/licenses/<package>/` (packaged and upstream texts) | each MSYS2 source package, sources tar |
| 7-Zip 25.01 (`7z.exe`, `7z.dll`, `7za.exe`; `7zz`), official releases, unmodified | all | LGPL-2.1-or-later, parts with the unRAR licence restriction, BSD-3-Clause and BSD-2-Clause parts | "Redistributions in binary form must reproduce related license information from this file" (its `License.txt`); provide the LGPL source | `tools/copyright/7-zip` | `7z2501-src.tar.xz`, sources tar |
| UnRAR 7.23 (`UnRAR.exe`; macOS/Linux `unrar` from RARLAB's RAR package), official releases, unmodified | all | RARLAB freeware licence (Windows `UnRAR.exe`); the RAR package's licence (macOS, Linux) | the RAR licence lets "the UnRAR components" be distributed separately (3a); no RAR or UnRAR code may be used to re-create the RAR compression algorithm. Not open source: Flathub publication remains an open question | `tools/copyright/unrar` (+ `unrar-acknowledgments`, macOS/Linux) | none owed (freeware) |
| sabctools 9.6.3 (on Windows built with DownloadPulse's patch; elsewhere PyPI's wheel) | all | GPL-2.0-or-later | provide the corresponding source | its installed licence file | sdist (+ the Windows patch), sources tar |
| SABnzbd 5.1.3 (the Usenet service; patched on macOS) | all | GPL-2.0-or-later | provide the corresponding source (shipped as source; `tests/` omitted) | `backend/usenet/LICENSE.txt` | release tarball, sources tar |
| NSIS 3.11 installer stub (upstream's NSIS 3.11 for Windows, pinned; LZMA compressor) | Windows | zlib/libpng; its LZMA module CPL-1.0 with NSIS's special exception | [D] NSIS's licence page covers NSIS and its compression modules, not the installers built with it; CPL-1.0 object-code distribution states where the source is: upstream's `nsis-3.11-src.tar.bz2` | `licenses/desktop/nsis/copyright` | sources tar (`windows-tools/nsis/`) |
| Microsoft Visual C++ x64 Redistributable 14.51 or newer (`vc_redist.x64.exe`, inside the Windows installer, run only when missing) | Windows | Microsoft Software License Terms (its own, shown by its installer) | **Bundled, unmodified, as Distributable Code.** [D] "Distribution of the Visual C++ Runtime Redistributable package ... is limited to licensed Visual Studio users" (learn.microsoft.com/cpp/windows/redistributing-visual-cpp-files, 2026-04-13): the maintainer is a licensed Visual Studio (Community) user, and `vc_redist.x64.exe` is on Visual Studio's redistributable list. The build takes Microsoft's own file (`https://aka.ms/vc14/vc_redist.x64.exe`), refuses it unless Microsoft's valid Authenticode signature is on it, and carries it byte for byte (its version and SHA-256 in `PREREQUISITES.txt`). An interactive install runs it with its own interface, so the person reads and accepts Microsoft's licence terms themselves; a silent install does not run it. It is a separate program under Microsoft's terms, not covered by DownloadPulse's GPL, and nothing in DownloadPulse names Microsoft as its source or endorser | -- | proprietary; distributed unmodified under Microsoft's terms |
| Microsoft Edge WebView2 Evergreen Bootstrapper (`MicrosoftEdgeWebview2Setup.exe`, inside the Windows installer, run only when missing) | Windows | Microsoft | [D] packaging the bootstrapper with an app is a documented distribution option: "download the bootstrapper and package it with your WebView2 app" (learn.microsoft.com/microsoft-edge/webview2/concepts/distribution, updated 2026-10-02) | -- | proprietary |

The Flatpak's runtime (GNOME 49 from Flathub) is not in the bundle: Flatpak
installs it from Flathub. The Deno executable's embedded components remain the
open item recorded above.

## Copyleft review — the facts and the recorded disposition

DownloadPulse bundles the Usenet acquisition service (SABnzbd 5.1.3,
GPL-2.0-or-later) inside the image, so that service's Python runtime closure is
part of the shipped runtime and appears in the table above. Three of those
packages are copyleft. **DownloadPulse imports none of them; it reaches the
service over a private loopback HTTP API.**

| Package | Licence | Why it ships | Obligation |
|---|---|---|---|
| sabctools | GPL-2.0-or-later | The service's own yEnc/NNTP helper. | Same licence as this project. Corresponding source offer applies. |
| hachoir | GPL-2.0-only | **Required.** A hard, unguarded top-level import in the service's `sabnzbd/misc.py`; with it removed the service does not start (`ModuleNotFoundError` before `sabnzbd/__init__.py` finishes loading). It is therefore required by the supported acquisition + PAR2-repair path, not merely present in upstream's broad requirements file. | Corresponding source offer applies. GPL-2.0-**only**, so it does not permit relicensing to GPL-3.0. |
| guessit | LGPL-3.0-or-later | The service's release-name parser, imported by its sorting/post-processing modules. | Corresponding source offer applies. LGPL-3.0 is incompatible with GPL-2.0-**only**. |

### What engineering characterization established

Established by engineering characterization:

- exactly which copyleft packages ship, at which versions, under which declared
  licence expressions (the table above and `licenses/python-runtime.json`);
- that DownloadPulse's own code links to none of them;
- that `hachoir` and `guessit` are load-bearing for the bundled service rather
  than optional extras;
- that the same closure is what upstream SABnzbd already distributes, so
  bundling it does not assemble a combination that did not previously exist.

### Separate programs: SABnzbd and rsync

The image bundles two programs that DownloadPulse runs but does not combine with
its own code.

- **SABnzbd 5.1.3** (GPL-2.0-or-later, (c) The SABnzbd-Team).
  - It ships as its complete upstream source release,
    `SABnzbd-5.1.3-src.tar.gz` (sha256
    `12a01e30ce166297a375ffc3a761f98bf7d93260e040391497f643f8a3525fed`),
    unpacked unchanged to `/app/usenet` with its own `LICENSE.txt`,
    `COPYRIGHT.txt`, `GPL2.txt`, `GPL3.txt` and `licenses/`.
  - DownloadPulse starts it as a separate process and talks to it only through
    its HTTP API on the loopback interface. DownloadPulse's application code
    imports no SABnzbd code.
    - One test-evidence generator, `backend/tests/fixtures/sab_server_report/gen.py`,
      imports SABnzbd modules when someone runs it by hand inside the image. It
      is not part of the running application.
  - Its Python dependencies are the packages its own `requirements.txt` names,
    at the versions DownloadPulse's lock pins, installed with their licence
    files.
  - `hachoir` (GNU GPL v2) and `guessit` (LGPL-3.0-or-later) are used by
    SABnzbd itself (`sabnzbd/misc.py`, `sabnzbd/sorting.py`,
    `sabnzbd/interface.py`), exactly as in upstream's own releases, and by no
    DownloadPulse code.
- **rsync** (GPL-3.0-or-later, Debian package `rsync`; see Container
  components below). It runs as a separate program over its command line.
  DownloadPulse links none of its code.

Shipping these programs in the same container image as DownloadPulse places them
side by side; it does not make them, or DownloadPulse, a derivative of one
another.

### Recorded disposition

The project's licensing review (2026-10-21)
characterized this architecture from the source and the qualified image. It
found no demonstrated DownloadPulse-level copyleft incompatibility arising from
bundling these separate programs: DownloadPulse's application code
(GPL-2.0-or-later) imports none of SABnzbd, `hachoir`, `guessit` or
`sabctools`, and links nothing of rsync.
The operator has accepted that finding and closed the DownloadPulse-level
copyleft-combination question on this separate-program evidence.

The remaining question belongs to upstream SABnzbd, not to DownloadPulse: how
GPL-2.0-**only** `hachoir` and LGPL-3.0-or-later `guessit` combine within
SABnzbd's own program. DownloadPulse ships that program as upstream distributes
it, keeps upstream's notices and the dependencies' own licence files, and does
not rewrite or replace its dependencies.

This is a recorded project disposition, not a legal certification of the
distribution as a whole, and no statement here is legal advice.

Two further packages carry weak/file-level copyleft that imposes no obligation
on the combined work: `orjson` (MPL-2.0 AND (Apache-2.0 OR MIT)) and `certifi`
(MPL-2.0). Both ship unmodified.

Deliberately excluded from the shipped closure, and therefore absent from the
table above: the service's own test tooling (pytest, selenium, black, tavern,
flask, lxml and friends), its win32/darwin-only packages, and `notify2` — the
bundled service is headless and private, DownloadPulse owns notifications, and
`notify2` publishes no licence expression.

## Container components

The official image is built from `python:3.12.14-slim-trixie`. The base image
contains Python under the Python Software Foundation License and Debian system
components under their package-specific terms. DownloadPulse directly installs the
following Debian packages; resolved binary versions and transitive packages are
recorded in the image's SBOM attestation.

| Direct package | License summary |
|---|---|
| aria2 | GPL-2.0-or-later |
| rsync | GPL-3.0-or-later |
| curl | curl |
| gosu | Apache-2.0 |
| zstd | BSD-3-Clause |
| par2 | GPL-2.0-or-later (Debian source package `par2cmdline`) |
| unrar | Debian non-free (source package `unrar-nonfree`); UnRAR freeware terms |
| 7zip | LGPL-2.1-or-later and package-specific component terms |
| 7zip-rar | Debian non-free RAR codec; UnRAR restricted freeware terms |
| ffmpeg | LGPL-2.1-or-later (DownloadPulse's `+dp1` build; no GPL component enabled) |

Package copyright files and common license texts remain installed in the
image. `SOURCE_OFFER.md` explains how to request corresponding source for
copyleft-covered binaries.

`zstd` is installed as the exact outer decoder for `.tar.zst`/`.tzst`; the resulting TAR stream is validated by DownloadPulse before extraction.

`rsync` (the Debian trixie package, taken at its current stable update when the
image is built -- `3.5.0+ds1-0+deb13u1` as of 2026-09-28, previously
`3.4.1+ds1-5+deb13u4` -- licensed GPL-3.0-or-later per its Debian copyright
file, which stays installed in the image) is the rsync executor's native
client. DownloadPulse runs it only as a separate program, over argv, and links
none of its code; its rsync-over-SSH transport is DownloadPulse's own SSH
channel on the already-shipped `asyncssh`, so no OpenSSH client is added. That
a GPL-3.0-or-later program now ships beside this GPL-2.0-or-later project is
recorded here as a fact. It is a separate program, covered by the disposition
recorded in the copyleft section above. Its corresponding source is located in
`SOURCE_OFFER.md`.

`ffmpeg` (`ffmpeg` and `ffprobe`, package `7:7.1.5-0+deb13u1+dp1`) is the
Media Downloads executor's one local finalizer: DownloadPulse runs it only as a
separate program, over argv, on files already inside an attempt's private
workspace, and only to copy native streams into the planned container (`-c
copy`, Matroska included) -- never to re-encode, and never with a network
address. It is Debian's ffmpeg source rebuilt by `packaging/ffmpeg/` with no
encoder, device, network protocol, external library or GPL component (its
configuration refuses `CONFIG_GPL`), so it is LGPL-2.1-or-later; Debian's
copyright file stays installed at `/usr/share/doc/ffmpeg/copyright`, and the
package image carries the exact corresponding source (Debian's source package
plus the build script; `docs/SUPPLY_CHAIN_POLICY.md` section 4b).

`par2` (`0.8.1-3` in the qualified image) performs the bundled Usenet
service's verification and repair of a posting. It is a separate program the
service runs; DownloadPulse links none of its code. Its Debian copyright file
(`License: GPL-2+`) stays installed at `/usr/share/doc/par2/copyright`, and
`SOURCE_OFFER.md` applies to it as to every copyleft-covered binary in the image.

`unrar` (`1:7.1.8-1`, Debian `non-free`) is present only because the bundled
Usenet service refuses to start acquiring without it; DownloadPulse submits every
posting repair-only, with the service's own unpackers disabled, so it is never
invoked for DownloadPulse work -- DownloadPulse owns archive extraction, through
`7z` and `7zip-rar`. It is not free software: its Debian copyright file states
the UnRAR freeware licence (the utility "may be freely distributed" and "inside
of other software packages"; its source "cannot be used to develop RAR
(WinRAR) compatible archiver and to re-create RAR compression algorithm"), and
that file is kept in the image at `/usr/share/doc/unrar/copyright` (the Docker
build re-includes it explicitly). That licence text contains no
corresponding-source requirement; upstream publishes the source at
`https://www.rarlab.com/rar_add.htm`.

`7zip-rar` is installed from Debian's `non-free` component solely to provide
RAR extraction through the external `7z` process. Because the slim base filters
most package documentation, the Docker build explicitly re-includes the
`7zip-rar` Debian copyright notice and
`/usr/share/doc/7zip-rar/unRarLicense.txt` so those terms remain in the shipped
image.

Python packages retain their installed `.dist-info` license and notice files.
`bencode2` 0.3.33 is the exception: its wheel omits the upstream MIT text, so
DownloadPulse explicitly packages that tagged notice at
`licenses/bencode2-MIT.txt`.

`argon2-cffi-bindings` includes the upstream Argon2 implementation and BLAKE2
code in its distribution. Those vendored components are published under CC0;
the Python binding package itself is MIT-licensed. Their installed package
metadata/license files remain in the image.

## SBOM and provenance

Published multi-architecture images are built with BuildKit provenance and
`sbom: true`. The resulting image attestation is expected to enumerate the
resolved Python and Debian runtime components actually shipped by the image.
The repository's `licenses/python-runtime.json` is the source-controlled
license inventory for the Python lock and is checked in CI; the image SBOM is
an additional build artifact, not a replacement for that inventory.

A dependency update is incomplete until all of the following agree:

1. `backend/requirements.in` direct dependencies;
2. the generated `backend/requirements.txt` lock;
3. `licenses/python-runtime.json` package/version/license entries;
4. this human-readable inventory;
5. the successfully built image/SBOM for the resulting commit.

## Vendored browser resources

| Resource | Version/source | License |
|---|---|---|
| Chart.js | 4.5.1, vendored at `frontend/static/vendor/chart.umd.min.js` | MIT ([bundled notice](../licenses/Chart.js-MIT.txt)) |
| FormatJS `intl-messageformat` | 12.1.4, vendored byte-for-byte (the package's self-contained `intl-messageformat.iife.js` ES-module build; npm `sha512-JUkmv3cAEyijN3oiRiV3ctel7yVMuUCgfh7Pu8pj1/z6L1DSEPRyhuMaLjVw3pGmGJ5RDH54WVFxfMHZWTwyHw==`) at `frontend/static/vendor/intl-messageformat.esm.js`; it inlines `@formatjs/fast-memoize` 3.1.7, `@formatjs/icu-messageformat-parser` 3.5.22 and `@formatjs/icu-skeleton-parser` 2.1.12 | BSD-3-Clause (intl-messageformat); MIT (the three inlined FormatJS parts) ([bundled notices](../licenses/FormatJS-intl-messageformat-BSD-3-Clause-MIT.txt)) |
| Lucide Icons UI subset | Source geometry pinned to `lucide-icons/lucide` commit `23f9abc4ed0146cffededd3d7f94c1018bfdf693`; only the shell/navigation glyphs required by DownloadPulse are embedded locally in `frontend/static/operator-title.js` | ISC; Feather-derived icons retain MIT terms ([bundled notice](../licenses/Lucide-ISC-MIT.txt)) |

The Lucide subset is intentionally local: DownloadPulse does not load Lucide from
a runtime CDN. The bundled subset covers the application shell, the individual
icon files under `frontend/static/icons/lucide/`, and the login page's
ShieldUser mark, which is drawn from one of those files (its own header names
the upstream commit above). It may be extended with additional upstream glyph geometry as further pages
are migrated. Any added Lucide glyph remains subject to the same bundled
upstream notices.

## Third-party artwork

The provider marks and host icons in the web interface are third-party
artwork. They are not covered by DownloadPulse's GPL grant and are not licensed
by DownloadPulse to anyone; see the `NOTICE` section "Third-party trademarks and
artwork". Their rights, provenance and modification questions are recorded per
file in the repository's `docs/THIRD_PARTY_ARTWORK.md`.

Two of those files are copies from openly licensed icon collections. Their
collections' licences require the following attribution:

| File in the image | Copied unchanged from | Licence |
|---|---|---|
| `frontend/static/icons/hosts/mega.svg` | `svg/mega.svg` of selfh.st icons, <https://github.com/selfhst/icons> | CC BY 4.0, <https://creativecommons.org/licenses/by/4.0/> ([licence text](../licenses/selfhst-icons-CC-BY-4.0.txt)) |
| `frontend/static/icons/providers/real-debrid.svg` | `svg/real-debrid.svg` of homarr-labs/dashboard-icons, <https://github.com/homarr-labs/dashboard-icons>, Copyright (c) 2024 Bjorn Lammers, Meier Lukas, Thomas Camlong and Homarr Labs | Apache-2.0 ([licence text](../licenses/dashboard-icons-Apache-2.0.txt)) |

Neither file was modified. Neither collection has a NOTICE file, and selfh.st
asks for no particular form of attribution, so the collections are credited by
name and link; neither collection names the individual creator of these two
icons. These licences cover only the collections' own contribution: the MEGA and Real-Debrid marks belong to their owners and are
not licensed by them.

## Language flags

The flag beside each language in the language list
(`frontend/static/icons/flags/<region>.svg`, 19 files, drawn by
`frontend/static/ui-language-picker.js`) is the waved flag of Noto's
region-flags, copied unchanged from `third_party/region-flags/waved-svg/` of
googlefonts/noto-emoji at commit `b3e3051a088047d19fd4d49b1c3ac42fb8c3aaf8`,
<https://github.com/googlefonts/noto-emoji> (each file named
`emoji_u<code points>.svg` there, renamed to its region code here). Those flags
were taken from Wikimedia Commons and checked to be in the public domain or
otherwise exempt from copyright ([licence text](../licenses/noto-region-flags-public-domain.txt)).
They are shown as artwork so a flag is drawn the same on every platform: an
emoji flag is not (Windows shows its two letters).

## Colour palettes

The alternative interface palettes (`frontend/static/ui-theme-palettes.css`;
catalog in `backend/services/account_appearance.py`) are DownloadPulse's own
mappings of colour values published by the projects below onto DownloadPulse's
design tokens. Only colour values are used, several adapted for legibility
(each marked in the stylesheet); no theme file, code, artwork, logo or font of
these projects is copied into the repository or the image. They are credited
here with their licences as recorded at the source:

| Palette(s) | Source | Licence |
|---|---|---|
| Catppuccin Mocha, Catppuccin Latte | <https://github.com/catppuccin/palette> | MIT |
| Dracula | <https://github.com/dracula/dracula-theme> | MIT |
| Gruvbox Dark, Gruvbox Light | <https://github.com/morhetz/gruvbox> | MIT/X11 (stated in its README) |
| Nord Light (Nord's Snow Storm, Polar Night and Frost; Nord has no official light theme) | <https://github.com/nordtheme/nord> | MIT |
| Everforest Dark, Everforest Light | <https://github.com/sainnhe/everforest> (`autoload/everforest.vim`, medium contrast) | MIT |
| Rosé Pine, Rosé Pine Dawn | <https://github.com/rose-pine/rose-pine-palette> (`palette.json`, main and dawn) | MIT |
| Kanagawa Wave | <https://github.com/rebelot/kanagawa.nvim> (`lua/kanagawa/colors.lua`, wave theme) | MIT |
| Ayu Mirage, Ayu Light | <https://github.com/ayu-theme/ayu-colors> (`themes/mirage.yaml`, `themes/light.yaml`) | MIT |
| SynthWave '84 | <https://github.com/robb0wen/synthwave-vscode> (colour values only; its glow extension is not used) | MIT |
| Oxocarbon Dark, Oxocarbon Light | <https://github.com/nyoom-engineering/oxocarbon.nvim> (`lua/oxocarbon/init.lua`), with the IBM Carbon grays of <https://github.com/nyoom-engineering/base16-oxocarbon> for the light surfaces | MIT (oxocarbon.nvim; base16-oxocarbon states no licence) |
| Flexoki Light | <https://stephango.com/flexoki>, <https://github.com/kepano/flexoki> | MIT |
| Bluloco Light | <https://github.com/uloco/theme-bluloco-light> (`themes/bluloco-light-color-theme.json`, `colors.json`) | LGPL-3.0 (the theme package; only its colour values are used) |

## Browser-loaded resources

These font resources are requested by the browser from third-party CDNs and are not
copied into the repository or container image:

| Resource | Version/source | License |
|---|---|---|
| Outfit | Google Fonts | OFL-1.1 |
| JetBrains Mono | Google Fonts | OFL-1.1 |
| Bricolage Grotesque | Google Fonts (project landing page) | OFL-1.1 |
| DM Mono | Google Fonts (project landing page) | OFL-1.1 |