# Runtime dependency license inventory

> **Scope:** This is the dependency/license inventory for the current `1.0.13` development tree — the Universal Transfer Core with the AllDebrid, General HTTP(S), FTP/SFTP and Usenet providers, the aria2 executor and the bundled Usenet acquisition service. Further `1.0.13` expansion work (SCP, rsync, WebDAV, additional debrid providers, additional executor implementations) must trigger a fresh third-party/license review if it adds libraries, executors, protocol dependencies, copied/derived code, or other attribution obligations.
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
| anyio | 4.14.2 | MIT |
| apprise | 1.12.0 | BSD-2-Clause |
| argon2-cffi | 25.1.0 | MIT |
| argon2-cffi-bindings | 26.1.0 | MIT; vendored Argon2/BLAKE2 components are CC0-1.0 |
| asyncssh | 2.24.0 | EPL-2.0 OR GPL-2.0-or-later (used under GPL-2.0-or-later) |
| attrs | 26.1.0 | MIT |
| authlib | 1.7.2 | BSD-3-Clause |
| babelfish | 0.6.1 | BSD-3-Clause |
| bencode2 | 0.3.33 | MIT ([bundled notice](../licenses/bencode2-MIT.txt)) |
| certifi | 2026.7.22 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.1 | MIT |
| cheroot | 11.1.2 | BSD-3-Clause |
| cherrypy | 18.10.0 | BSD-3-Clause |
| click | 8.3.3 | BSD-3-Clause |
| configobj | 5.0.9 | BSD-3-Clause |
| cryptography | 50.0.0 | Apache-2.0 OR BSD-3-Clause |
| ct3 | 3.4.0.post5 | MIT |
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
| multidict | 6.7.1 | Apache-2.0 |
| oauthlib | 3.3.1 | BSD-3-Clause |
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
| setuptools | 84.0.0 | MIT |
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
| yarl | 1.23.0 | Apache-2.0 |
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
Their transitive cryptographic/HTTP dependencies are included in the table and
machine-readable runtime manifest above. Package/license pairs are cross-checked
against the corresponding upstream/PyPI metadata when the lock is generated.

## Copyleft review — the facts, and what still needs project/licence review

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

### What this project has established, and what it has not

Established by engineering characterization:

- exactly which copyleft packages ship, at which versions, under which declared
  licence expressions (the table above and `licenses/python-runtime.json`);
- that DebridPulse's own code links to none of them;
- that `hachoir` and `guessit` are load-bearing for the bundled service rather
  than optional extras;
- that the same closure is what upstream SABnzbd already distributes, so
  bundling it does not assemble a combination that did not previously exist.

**Not established here:** whether the combined work may be distributed on these
terms. The relevant question — DebridPulse is GPL-2.0-**or-later**, so a
distribution under GPL-3.0-or-later terms would accommodate guessit's LGPL-3.0,
while hachoir is GPL-2.0-**only** — is a licensing judgment about the combined
work, not an engineering fact, and **requires project/licence review before
release**. Nothing in this repository should be read as that review having
happened, and no statement here is legal advice.

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