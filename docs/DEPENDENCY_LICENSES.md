# Runtime dependency license inventory

> **Checkpoint scope:** This dependency/license inventory applies to the current qualified v1.0.12 **two-provider development tree**. It is not the final eventual v1.0.12 dependency/license closure. Deferred Items 12–16 must trigger a fresh third-party/license audit if they add libraries, executors, protocol dependencies, copied/derived code, or other attribution obligations.
This inventory covers every Python package pinned in
`backend/requirements.txt`. Package names and versions are enforced by
`backend/tests/test_license_policy.py`; a dependency change must update both
the lock file and `licenses/python-runtime.json`.

| Package | Version | License |
|---|---:|---|
| aiohappyeyeballs | 2.6.1 | PSF-2.0 |
| aiohttp | 3.14.3 | Apache-2.0 AND MIT |
| aiosignal | 1.4.0 | Apache-2.0 |
| aiosqlite | 0.22.1 | MIT |
| annotated-doc | 0.0.4 | MIT |
| annotated-types | 0.7.0 | MIT |
| anyio | 4.13.0 | MIT |
| argon2-cffi | 25.1.0 | MIT |
| argon2-cffi-bindings | 26.1.0 | MIT; vendored Argon2/BLAKE2 components are CC0-1.0 |
| attrs | 26.1.0 | MIT |
| authlib | 1.7.2 | BSD-3-Clause |
| bencode2 | 0.3.33 | MIT ([bundled notice](../licenses/bencode2-MIT.txt)) |
| certifi | 2026.7.22 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| click | 8.3.3 | BSD-3-Clause |
| cryptography | 50.0.0 | Apache-2.0 OR BSD-3-Clause |
| fastapi | 0.141.1 | MIT |
| frozenlist | 1.8.0 | Apache-2.0 |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httptools | 0.8.0 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| idna | 3.15 | BSD-3-Clause |
| joserfc | 1.7.4 | BSD-3-Clause |
| multidict | 6.7.1 | Apache-2.0 |
| prometheus-client | 0.26.0 | Apache-2.0 AND BSD-2-Clause |
| propcache | 0.5.2 | Apache-2.0 |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.4 | MIT |
| pydantic-core | 2.46.4 | MIT |
| python-multipart | 0.0.32 | Apache-2.0 |
| starlette | 1.3.1 | BSD-3-Clause |
| typing-extensions | 4.15.0 | PSF-2.0 |
| typing-inspection | 0.4.2 | MIT |
| uvicorn | 0.52.4 | BSD-3-Clause |
| uvloop | 0.22.1 | MIT OR Apache-2.0 |
| yarl | 1.23.0 | Apache-2.0 |

The 1.0.6 native-authentication work directly depends on `argon2-cffi` for
Argon2id local-password verification, `authlib` for OpenID Connect/JWT protocol
handling, and `httpx` for bounded outbound OIDC discovery/token/JWKS requests.
Their transitive cryptographic/HTTP dependencies are included in the table and
machine-readable runtime manifest above. The package/license pairs for the new
stack were cross-checked against the corresponding upstream/PyPI metadata when
the lock was generated.

## Container components

The official image is built from `python:3.12.14-slim-trixie`. The base image
contains Python under the Python Software Foundation License and Debian system
components under their package-specific terms. DebridPulse directly installs the
following Debian packages; resolved binary versions and transitive packages are
recorded in the image's SBOM attestation.

| Direct package | License summary |
|---|---|
| aria2 | GPL-2.0-or-later |
| curl | curl |
| gosu | Apache-2.0 |
| zstd | BSD-3-Clause |
| 7zip | LGPL-2.1-or-later and package-specific component terms |
| 7zip-rar | Debian non-free RAR codec; UnRAR restricted freeware terms |

Package copyright files and common license texts remain installed in the
image. `SOURCE_OFFER.md` explains how to request corresponding source for
copyleft-covered binaries.

`zstd` is installed as the exact outer decoder for `.tar.zst`/`.tzst`; the resulting TAR stream is validated by DebridPulse before extraction.

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
a runtime CDN. The bundled subset currently covers the application shell and
may be extended with additional upstream glyph geometry as later v1.0.11 pages
are migrated. Any added Lucide glyph remains subject to the same bundled
upstream notices.

## Browser-loaded resources

These font resources are requested by the browser from third-party CDNs and are not
copied into the repository or container image:

| Resource | Version/source | License |
|---|---|---|
| Outfit | Google Fonts | OFL-1.1 |
| JetBrains Mono | Google Fonts | OFL-1.1 |
| Bricolage Grotesque | Google Fonts (project landing page) | OFL-1.1 |
| DM Mono | Google Fonts (project landing page) | OFL-1.1 |
