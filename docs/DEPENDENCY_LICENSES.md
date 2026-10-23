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
| authlib | 1.7.2 | BSD-3-Clause |
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
| joserfc | 1.7.4 | BSD-3-Clause |
| markdown | 3.10.3 | BSD-3-Clause |
| more-itertools | 11.1.0 | MIT |
| multidict | 6.9.1 | Apache-2.0 |
| oauthlib | 4.0.0 | BSD-3-Clause |
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

The 1.0.6 native-authentication work directly depends on `argon2-cffi` for
Argon2id local-password verification, `authlib` for OpenID Connect/JWT protocol
handling, and `httpx` for bounded outbound OIDC discovery/token/JWKS requests.
The v1.0.12 provider-runtime hardening directly depends on `google-re2` so
externally supplied AllDebrid applicability expressions execute with RE2's
linear-time matching semantics instead of Python backtracking regex behavior.
The 1.0.13 universal evidence acquisition work directly depends on `asyncssh`
for bounded SFTP evidence reads (host identity confirmed before authentication,
offset reads only) over connections the downloader egress guard authorizes.
Its only dependencies, `cryptography` and `typing-extensions`, were already
locked. `asyncssh` is dual-licensed EPL-2.0 OR GPL-2.0-or-later; DebridPulse,
itself GPL-2.0-or-later, uses it under GPL-2.0-or-later. This is the one
reviewed copyleft runtime dependency named in `backend/tests/test_license_policy.py`.
`bcrypt` (Apache-2.0, no Python dependencies) is `asyncssh`'s own backend for
passphrase-encrypted OpenSSH-format private keys -- the default format of
`ssh-keygen` -- which the generalized `username_private_key` sign-in accepts.
The 1.0.13 Media Downloads integration directly depends on `yt-dlp` (Unlicense,
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

## Copyleft review — the facts and the recorded disposition

DebridPulse 1.0.13 bundles the Usenet acquisition service (SABnzbd 5.1.3,
GPL-2.0-or-later) inside the image, so that service's Python runtime closure is
part of the shipped runtime and appears in the table above. Three of those
packages are copyleft. **DebridPulse imports none of them; it reaches the
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
- that DebridPulse's own code links to none of them;
- that `hachoir` and `guessit` are load-bearing for the bundled service rather
  than optional extras;
- that the same closure is what upstream SABnzbd already distributes, so
  bundling it does not assemble a combination that did not previously exist.

### Separate programs: SABnzbd and rsync

The image bundles two programs that DebridPulse runs but does not combine with
its own code.

- **SABnzbd 5.1.3** (GPL-2.0-or-later, (c) The SABnzbd-Team).
  - It ships as its complete upstream source release,
    `SABnzbd-5.1.3-src.tar.gz` (sha256
    `12a01e30ce166297a375ffc3a761f98bf7d93260e040391497f643f8a3525fed`),
    unpacked unchanged to `/app/usenet` with its own `LICENSE.txt`,
    `COPYRIGHT.txt`, `GPL2.txt`, `GPL3.txt` and `licenses/`.
  - DebridPulse starts it as a separate process and talks to it only through
    its HTTP API on the loopback interface. DebridPulse's application code
    imports no SABnzbd code.
    - One test-evidence generator, `backend/tests/fixtures/sab_server_report/gen.py`,
      imports SABnzbd modules when someone runs it by hand inside the image. It
      is not part of the running application.
  - Its Python dependencies are the packages its own `requirements.txt` names,
    at the versions DebridPulse's lock pins, installed with their licence
    files.
  - `hachoir` (GNU GPL v2) and `guessit` (LGPL-3.0-or-later) are used by
    SABnzbd itself (`sabnzbd/misc.py`, `sabnzbd/sorting.py`,
    `sabnzbd/interface.py`), exactly as in upstream's own releases, and by no
    DebridPulse code.
- **rsync** (GPL-3.0-or-later, Debian package `rsync`; see Container
  components below). It runs as a separate program over its command line.
  DebridPulse links none of its code.

Shipping these programs in the same container image as DebridPulse places them
side by side; it does not make them, or DebridPulse, a derivative of one
another.

### Recorded disposition (1.0.13)

The project's licensing review of the 1.0.13 release candidate (2026-10-21)
characterized this architecture from the source and the qualified image. It
found no demonstrated DebridPulse-level copyleft incompatibility arising from
bundling these separate programs: DebridPulse's application code
(GPL-2.0-or-later) imports none of SABnzbd, `hachoir`, `guessit` or
`sabctools`, and links nothing of rsync.
The operator has accepted that finding and closed the DebridPulse-level
copyleft-combination question on this separate-program evidence.

The remaining question belongs to upstream SABnzbd, not to DebridPulse: how
GPL-2.0-**only** `hachoir` and LGPL-3.0-or-later `guessit` combine within
SABnzbd's own program. DebridPulse ships that program as upstream distributes
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
bundled service is headless and private, DebridPulse owns notifications, and
`notify2` publishes no licence expression.

## Container components

The official image is built from `python:3.12.14-slim-trixie`. The base image
contains Python under the Python Software Foundation License and Debian system
components under their package-specific terms. DebridPulse directly installs the
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
| ffmpeg | LGPL-2.1-or-later (DebridPulse's `+dp1` build; no GPL component enabled) |

Package copyright files and common license texts remain installed in the
image. `SOURCE_OFFER.md` explains how to request corresponding source for
copyleft-covered binaries.

`zstd` is installed as the exact outer decoder for `.tar.zst`/`.tzst`; the resulting TAR stream is validated by DebridPulse before extraction.

`rsync` (the Debian trixie package, taken at its current stable update when the
image is built -- `3.5.0+ds1-0+deb13u1` as of 2026-09-28, previously
`3.4.1+ds1-5+deb13u4` -- licensed GPL-3.0-or-later per its Debian copyright
file, which stays installed in the image) is the rsync executor's native
client. DebridPulse runs it only as a separate program, over argv, and links
none of its code; its rsync-over-SSH transport is DebridPulse's own SSH
channel on the already-shipped `asyncssh`, so no OpenSSH client is added. That
a GPL-3.0-or-later program now ships beside this GPL-2.0-or-later project is
recorded here as a fact. It is a separate program, covered by the disposition
recorded in the copyleft section above. Its corresponding source is located in
`SOURCE_OFFER.md`.

`ffmpeg` (`ffmpeg` and `ffprobe`, package `7:7.1.5-0+deb13u1+dp1`) is the
Media Downloads executor's one local finalizer: DebridPulse runs it only as a
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
service runs; DebridPulse links none of its code. Its Debian copyright file
(`License: GPL-2+`) stays installed at `/usr/share/doc/par2/copyright`, and
`SOURCE_OFFER.md` applies to it as to every copyleft-covered binary in the image.

`unrar` (`1:7.1.8-1`, Debian `non-free`) is present only because the bundled
Usenet service refuses to start acquiring without it; DebridPulse submits every
posting repair-only, with the service's own unpackers disabled, so it is never
invoked for DebridPulse work -- DebridPulse owns archive extraction, through
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
DebridPulse explicitly packages that tagged notice at
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
| Lucide Icons UI subset | Source geometry pinned to `lucide-icons/lucide` commit `23f9abc4ed0146cffededd3d7f94c1018bfdf693`; only the shell/navigation glyphs required by DebridPulse are embedded locally in `frontend/static/operator-title.js` | ISC; Feather-derived icons retain MIT terms ([bundled notice](../licenses/Lucide-ISC-MIT.txt)) |

The Lucide subset is intentionally local: DebridPulse does not load Lucide from
a runtime CDN. The bundled subset covers the application shell, the individual
icon files under `frontend/static/icons/lucide/`, and the login page's
ShieldUser mark, which is drawn from one of those files (its own header names
the upstream commit above). It may be extended with additional upstream glyph geometry as later v1.0.11 pages
are migrated. Any added Lucide glyph remains subject to the same bundled
upstream notices.

## Third-party artwork

The provider marks and host icons in the web interface are third-party
artwork. They are not covered by DebridPulse's GPL grant and are not licensed
by DebridPulse to anyone; see the `NOTICE` section "Third-party trademarks and
artwork". Their rights, provenance and modification questions are recorded per
file in the repository's `docs/THIRD_PARTY_ARTWORK.md`, and remain deferred to
1.0.14 as an accepted risk for 1.0.13.

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

## Browser-loaded resources

These font resources are requested by the browser from third-party CDNs and are not
copied into the repository or container image:

| Resource | Version/source | License |
|---|---|---|
| Outfit | Google Fonts | OFL-1.1 |
| JetBrains Mono | Google Fonts | OFL-1.1 |
| Bricolage Grotesque | Google Fonts (project landing page) | OFL-1.1 |
| DM Mono | Google Fonts (project landing page) | OFL-1.1 |