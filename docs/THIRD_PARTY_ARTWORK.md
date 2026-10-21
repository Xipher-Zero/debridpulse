# Third-party artwork in DebridPulse

DebridPulse's web interface shows small images that identify the services it
works with:

- **Provider marks:** on each Settings → Services card.
- **Host icons:** beside a transfer's source host in the Downloads and Dashboard
  lists.

**These files are not DebridPulse's own work, and they are not licensed under
DebridPulse's GPL-2.0-or-later grant:**

- they remain the property of their respective rights holders;
- they appear only to identify the service a transfer comes from or goes through;
- their presence implies no affiliation with, sponsorship by or endorsement from
  any service;
- DebridPulse licenses them to no one.

This document records what is known about each file. **It does not grant
anyone a right to any of them, and it is not a finding that DebridPulse may
redistribute them.**

## Status for 1.0.13: a recorded, accepted risk

A licensing and provenance investigation of the qualified 1.0.13 release
candidate (commit `a78055bdfb96bbc59b32a42150c5b0d648085c26`, 2026-10-21)
looked for an authoritative source and published terms for every shipped
artwork file.

**It found no grant of permission to redistribute any of them.** Its
classifications are kept unchanged in the register below:

| Classification | Meaning | Retained files |
|---|---|---|
| **SUPPORTED** | A redistribution basis was found | 0 |
| **CONDITIONAL** | Published terms allow this kind of use under conditions; redistribution of the file is not addressed | 2 |
| **CORRECTION REQUIRED** | Published terms prohibit copying without permission, a file was modified against its brand rules, or a licence condition is unmet | 10 |
| **UNRESOLVED** | No permission and no prohibition was found | 22 |

**For 1.0.13, the operator:**

- removed the three files listed below;
- kept every other file and its existing presentation unchanged;
- accepted the remaining publication risk for this interim release, and deferred
  the rights, provenance, attribution and modification questions of the
  retained artwork to **1.0.14**.

**That is a risk acceptance and a deferral, not a finding that redistribution
rights exist.** No file below has been reclassified as permitted.

### Removed in 1.0.13 (rows 35, 36 and 37)

- `frontend/static/icons/dp/settings/openid-connect.svg`: an unused, recoloured
  reproduction of the OpenID logo.
- `frontend/static/icons/dp/settings/discord-notifications.svg`: an unused icon
  alluding to Discord's mark.
- `frontend/static/authentik-oidc.svg`: a glyph of unestablished origin that
  marked the login page's OpenID Connect button. The investigation recorded it
  as unreferenced, but the login page loaded it. That button now shows
  Lucide's ShieldUser icon, from the Lucide subset DebridPulse already ships
  (ISC; `licenses/Lucide-ISC-MIT.txt`).

The 34 retained files are the host icons and provider marks (rows 1–34).

### Licence conditions met in 1.0.13

Two retained files are exact copies from openly licensed icon collections:

| File | Copied unchanged from | Licence | Attribution and licence copy |
|---|---|---|---|
| `icons/hosts/mega.svg` | [selfh.st icons](https://github.com/selfhst/icons), `svg/mega.svg` (identical at commit `1d65d45a42e6323b54b8096967b1ea4b65321f97`) | CC BY 4.0 | Given in `docs/DEPENDENCY_LICENSES.md`; licence text at `licenses/selfhst-icons-CC-BY-4.0.txt` |
| `icons/providers/real-debrid.svg` | [homarr-labs/dashboard-icons](https://github.com/homarr-labs/dashboard-icons), `svg/real-debrid.svg` (identical at commit `320c819da84048d5a3066e2578bfa83be9019fdf`) | Apache-2.0 | Given in `docs/DEPENDENCY_LICENSES.md`; licence text at `licenses/dashboard-icons-Apache-2.0.txt` |

Those attributions are intended to meet the collections' own licence conditions. Neither collection has a NOTICE file. selfh.st asks for no particular form of attribution. Neither collection names the individual creator of these two icons, so the collections are credited. They cover
only what the collections' contributors could license. **They do not license
the MEGA or Real-Debrid marks, whose questions remain deferred with the rest.**

## Register

How to read it:

- **Hashes** are of the shipped bytes. All 37 files were found byte-identical
  in the qualified image; the 34 retained files are unchanged since.
- **Host icons** ship inside the `frontend/host-icons.parts` archive. It is
  unpacked to `frontend/static/icons/hosts/` when the image is built; it was not
  changed in 1.0.13.
- **Rights holders** are as each service states them; "UNVERIFIED" means none
  was established.
- **"Permission / operator position"** in the last column means no
  redistribution basis was found. The choices are written permission, an
  explicit operator position on identifying services by their marks, or not
  distributing the file.

Research quotes and URLs for every row are kept with the investigation
evidence (`dp-1.0.13-licensing-provenance-investigation-a78055bd`).

| # | File (repository path) | SHA-256 | Service | Rights holder (as stated by the service) | Where the file came from | Terms found | Kind | Where it appears | Classification | 1.0.13 status | Open item |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `frontend/static/icons/providers/alldebrid.svg` | `4f86d956c41a234fa18cb0f130bc44998b6e068f146536553df7447f7bfe54b2` | AllDebrid | Vulnerator SARL (https://alldebrid.com/legal/) | None official as vector; emblem matches https://cdn.alldebrid.com/lib/images/default/logo_alldebrid.png | Legal page: "Reproduction of all or part of this site … strictly prohibited unless … express permission" (incl. "iconographic … representations"). No brand policy. | Recreation: monochrome raster-to-vector trace of AllDebrid's emblem; commit 55d15f3b "supplied" (source unrecorded) | Settings → Services provider card identity chip (`ui-settings-page.js`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Operator: record who supplied it and on what basis; absent permission, treat like the explicit-prohibition set. |
| 2 | `frontend/static/icons/providers/debrid-link.svg` | `af4d765fa5dc55bdfe47f9064da3b8525c88a6254aae004adc1e8bc4a21ff366` | Debrid-Link | SoftLink SASU (https://debrid-link.com/legal) | https://debrid-link.com/img/brand/dl-blue.svg (brand page https://debrid-link.com/brand) | Brand page: "for non-commercial use only … must not be modified in any way. This includes altering colors … Only the official colors ( #00506d and #fafafa )". | Derivative: geometry identical to official dl-blue.svg, **fill recoloured #00506d → #264e70** | Settings → Services provider card identity chip (`ui-settings-page.js`) | **CORRECTION REQUIRED** | Retained (1.0.14 deferral) | Ship the unmodified official file (#00506d), or remove; confirm DebridPulse's use is "non-commercial" (operator). |
| 3 | `frontend/static/icons/providers/premiumize.svg` | `acdbd146aab30ee4bb799258d399703e987aa1b37712a03f4ea2db0c7df2928a` | Premiumize.me | Meta Connect Ltd. d/b/a Premiumize.me (https://www.premiumize.me/legal) | https://www.premiumize.me/icon.svg (**byte-identical**) | "no … Marks [incl. logos] may be … reproduced … distributed … for any commercial purpose … without our express prior written permission" (resellers excepted). | Official, unmodified | Settings → Services provider card identity chip (`ui-settings-page.js`) | **CONDITIONAL** | Retained (1.0.14 deferral) | Holds only if the use is non-commercial (operator determination) — otherwise written permission. |
| 4 | `frontend/static/icons/providers/real-debrid.svg` | `d4d5b8d2789c13ba7488f1794be4c10619dd698ce970bbfc2fb86f5b2111f434` | Real-Debrid | XT Network SAS (https://real-debrid.com/legal) | Official vector: https://cdn.real-debrid.com/images/logo.svg (different file). Shipped file is **byte-identical** to homarr-labs/dashboard-icons `svg/real-debrid.svg` (Apache-2.0) | Legal page: electronic reproduction "without written authorization from the editor, is forbidden". dashboard-icons: Apache-2.0 + "trademarks … property of their respective owners". | Third-party recreation copied from an Apache-2.0 icon collection | Settings → Services provider card identity chip (`ui-settings-page.js`) | **CORRECTION REQUIRED** | Retained (1.0.14 deferral) | The collection-licence attribution and licence copy were added in 1.0.13 (see "Licence conditions met in 1.0.13"). Mark rights remain open: permission or operator position. |
| 5 | `frontend/static/icons/providers/torbox.svg` | `bb7a95799250d1bd5b1a626d781fc5033cef31930a9d74f8a4ab8fd54b892160` | TorBox | Anonymous Systems FZ-LLC, UAE (https://torbox.app/terms) | https://torbox.app/assets/logo-bb7a9579.svg (**byte-identical**) | Terms §29: "You shall not copy, modify, distribute … any TorBox intellectual property except as expressly permitted … or by TorBox in writing" (logos named). | Official, unmodified | Settings → Services provider card identity chip (`ui-settings-page.js`) | **CORRECTION REQUIRED** | Retained (1.0.14 deferral) | Written permission from TorBox, or stop distributing the file. |
| 6 | `frontend/host-icons.parts → icons/hosts/1fichier.png` | `cde9c5ac420f167109c1ff6b6a7b212fd8aa2ec55243e8a204028791a5950092` | 1fichier | DStorage SASU (https://1fichier.com/cgu.html) | https://img.1fichier.com/favicon.png (pixel-identical, re-encoded) | None found (French CGU not separately checked). | Official favicon, re-encoded | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Permission, or operator nominative-identification position (see decisions). |
| 7 | `frontend/host-icons.parts → icons/hosts/4shared.png` | `d7cbd744951017cdc9898dfb49d1c6b30370eb82f2da259a4d1de47f841332e4` | 4shared | New IT Ltd., Cyprus (https://www.4shared.com/terms.jsp) | Not established (not a current 4shared mark) | "logos … may not be copied, imitated or used, in whole or in part, without the prior written permission of 4shared"; also "button icons". | Origin not established; not a current 4shared mark | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **CORRECTION REQUIRED** | Retained (1.0.14 deferral) | Written permission, or stop distributing. |
| 8 | `frontend/host-icons.parts → icons/hosts/alfafile.png` | `fc6ab0e0e47a3631974ea801724b8e51ca2e9da6ea09671d1b6fb101fc20f2fa` | Alfafile | "Alfafile.net … a limited company" (https://alfafile.net/terms) | UNVERIFIED (older/alternate all-green variant of its folder mark) | "All Alfafile.net emblems … logos … may not be used, imitated or copied without express written consent". | Older/alternate official mark | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **CORRECTION REQUIRED** | Retained (1.0.14 deferral) | Written consent, or stop distributing. |
| 9 | `frontend/host-icons.parts → icons/hosts/fastbit.png` | `78b80fb340f771559318bfd75ebac5108478c462a40dacc65c30591540e068a0` | FastBit | UNVERIFIED ("FastBit.cc") | Not established (current FastBit favicon differs) | None found. | Origin not established; background removed | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Permission / operator position; provenance unknown. |
| 10 | `frontend/host-icons.parts → icons/hosts/file-upload.png` | `0bde05dd74427bc75797179bfadc637e433a40471dde61574c0c1745a23d9aba` | File-Upload | UNVERIFIED (site down) | file-upload.com favicon via Google favicon cache (visual) | None found (site unreachable). | Official favicon, re-encoded | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Permission / operator position. |
| 11 | `frontend/host-icons.parts → icons/hosts/fileal.png` | `afbac0e23209b1f67fc7b7514eb644fe24665b65c1392afe31b93910fe51f172` | File.AL (mapped `fileal.com`) | "File.AL" (https://file.al/) | https://file.al/images_xfsTheme/logo.png (scaled) | None found. | Official site logo, scaled | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Permission / operator position. (The icon is mapped to `fileal.com`, but the service's domain is `file.al`; recorded only, unchanged.) |
| 12 | `frontend/host-icons.parts → icons/hosts/filedot.png` | `c0c1b34b7b33690a92a5c8769ce7baf66bd3ac90169a86c991b4b145f1f7a17f` | Filedot | Fullcloud Corp, Belize (https://filedot.to/?op=page&tmpl=tos) | https://filedot.to/favicon.ico (scaled) | None found ("No Rights Reserved" footer is not a licence). | Official favicon, scaled | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Permission / operator position. |
| 13 | `frontend/host-icons.parts → icons/hosts/filefactory.png` | `852861051adc219caec75a135d9830ed9a0b7b935439376ba0cc2c9e3a962eee` | FileFactory | Gameworld Industries Pty Ltd (https://www.filefactory.com/legal/terms) | https://www.filefactory.com/favicon/android-chrome-192x192.png (scaled) | "Elements of the Website … may not be copied or imitated in whole or in part. … the FileFactory.com logo … are trademarks". | Official icon, scaled | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **CORRECTION REQUIRED** | Retained (1.0.14 deferral) | Written permission, or stop distributing. |
| 14 | `frontend/host-icons.parts → icons/hosts/filespace.png` | `77eba6662cd0820728a1d1f2749863bcd4fdd0c3447aafd4911d992127c97d55` | Filespace | Bayside Ltd, St. Vincent & the Grenadines (https://filespace.com/?op=page&tmpl=tos) | Crop of https://filespace.com/images/logo.png | Licence to reproduce site content "solely for Your personal use", non-sub-licensable. | Derivative (cropped, upscaled) | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **CORRECTION REQUIRED** | Retained (1.0.14 deferral) | Written permission, or stop distributing. |
| 15 | `frontend/host-icons.parts → icons/hosts/gigapeta.png` | `2fa4a7940ada239095b423fd3fd2b648691f2b9bc47cc5f61e0915ef7839414c` | GigaPeta | UNVERIFIED | https://gigapeta.com/favicon.ico (upscaled) | None found. | Official favicon, upscaled | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Permission / operator position. |
| 16 | `frontend/host-icons.parts → icons/hosts/hexupload.png` | `a61fd26099a558fb094093042dd83c6f10dbc728b150d63bac918d9bacf3b9f5` | HexUpload | UNVERIFIED entity ("Hexupload.net") | Mark seen in https://hexupload.net/nav/top_logo.png (different render) | "Elements of the Web site … may not be copied or imitated in whole or in part. … the Hexupload.net logo … are trademarks". | Official mark, re-rendered | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **CORRECTION REQUIRED** | Retained (1.0.14 deferral) | Written permission, or stop distributing. |
| 17 | `frontend/host-icons.parts → icons/hosts/hitfile.png` | `910bcfd9e894bf86bafa183838036ed0a7cebed967e6a40eb612e544e277efc2` | Hitfile | UNVERIFIED | https://hitfile.net/apple-touch-icon.png (scaled) | None found (rules not retrievable). | Official icon, scaled | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Permission / operator position. |
| 18 | `frontend/host-icons.parts → icons/hosts/isra-cloud.png` | `043258152964f4a1dd77d933ee1aabf16c18183cd02e2fca9740f1df058dab52` | Isra.cloud | "ISRACLOUD" (https://isra.cloud/?op=page&tmpl=tos) | https://isra.cloud/isracloud_style/images/favicon.png (scaled) | Claims all IP rights; no logo clause. | Official favicon, scaled | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Permission / operator position. |
| 19 | `frontend/host-icons.parts → icons/hosts/katfile.png` | `05c350f229b1d91d9b54c266a46cb7dcc73cf68af7f4171eb72a073cc583501c` | KatFile | UNVERIFIED ("KatFile") | https://katfile.co/images/nlogo.png (pixel-identical after trim/resize; seasonal Santa-hat variant) | None found. | Derivative of a seasonal official logo | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Permission / operator position. |
| 20 | `frontend/host-icons.parts → icons/hosts/mediafire.png` | `7ee208fd42e25080c98c86eb65dae345c01128926c8338b5c74481eba2589e11` | MediaFire | MediaFire (trademark statement, https://www.mediafire.com/developers/brand_assets) | https://www.mediafire.com/images/logos/mf_logo250x250.png (scaled; RMSE 1.1%) | Brand guidelines allow the logo "to show that your product is compatible with MediaFire"; forbid implying endorsement, use as an app icon, and modifying the assets; redistribution not addressed. | Official, scaled | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **CONDITIONAL** | Retained (1.0.14 deferral) | Use matches the compatibility allowance; keep unmodified, no endorsement wording; redistribution of the file itself not addressed by the guidelines (operator acceptance or permission). |
| 21 | `frontend/host-icons.parts → icons/hosts/mega.svg` | `c5b9d2eb95425d454d13094c584d20f8f944805332372378f6c912b8b0d52bda` | MEGA | Mega Privacy (VMC: registered mark IPONZ 1047205; https://mega.io/terms) | Official https://mega.nz/mega_logo.svg differs; shipped file is **byte-identical** to selfh.st icons `svg/mega.svg` (CC BY 4.0) | Terms: no copying/reproducing IP without written permission. selfh.st: CC BY 4.0 (attribution required). | Third-party recreation copied from a CC BY 4.0 collection | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **CORRECTION REQUIRED** | Retained (1.0.14 deferral) | The collection-licence attribution and licence copy were added in 1.0.13 (see "Licence conditions met in 1.0.13"). Mark rights remain open: permission or operator position. |
| 22 | `frontend/host-icons.parts → icons/hosts/modsbase.png` | `e1f68b61b9ee439155c9115fa111055b25409a0bd01d0460012e1b5e136aeca7` | modsBase | UNVERIFIED | Glyph cropped from https://modsbase.com/images/logo-modsbase.png | None found. | Derivative (cropped) | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Permission / operator position. |
| 23 | `frontend/host-icons.parts → icons/hosts/mp4upload.png` | `407a2e3317fe6d656e9891deb51d6cd69c09baee4ea595b7be0f355da4dd9f7b` | MP4Upload | UNVERIFIED | https://www.mp4upload.com/favicon.png (visual) | None found. | Derivative of official favicon | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Permission / operator position. |
| 24 | `frontend/host-icons.parts → icons/hosts/prefiles.png` | `0377b2058fc344ce9e94592545339b9e9df508a0f1be2901eb59bd47441820c0` | Prefiles | UNVERIFIED | Not established (matches no Prefiles mark found) | None found. | Not established | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Establish origin; if stock art, its licence. |
| 25 | `frontend/host-icons.parts → icons/hosts/rapidgator.png` | `b3b31f677c3c195bd724b790c1e46d9a7ca142e588e5776789f5f20c293352f1` | Rapidgator | UNVERIFIED | UNVERIFIED (matches no current or historical Rapidgator icon found) | None found. | Not established | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Establish origin. |
| 26 | `frontend/host-icons.parts → icons/hosts/scribd.png` | `ed55a100d47727fcb565ba86ee5228ace95ff641f8674c0531b26af9517c98aa` | Scribd | Scribd, Inc. (Global Terms of Use) | https://s-f.scribdassets.com/scribd_rebrand.ico (scaled) | Press kit: "editorial purposes only … Any other use … strictly prohibited"; Terms: marks "may not be used in connection with products or services that are not related to, associated with, or sponsored by the Company". | Official favicon, scaled | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **CORRECTION REQUIRED** | Retained (1.0.14 deferral) | Written permission, or stop distributing. |
| 27 | `frontend/host-icons.parts → icons/hosts/sendit.png` | `acd24033eb04e05f5b36799f88937df6a11470e431f820986ab9aa2f84a8a733` | Sendit | UNVERIFIED | sendit.cloud favicon (96 px frame, resized; pixel-identical) | None found. | Derivative (resize) of official favicon | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Permission / operator position. |
| 28 | `frontend/host-icons.parts → icons/hosts/simfileshare.png` | `32eac3ec3865c13772f7438926ff11e4b299ff08d77c8e7ff320c7bc12370279` | SimFileShare | UNVERIFIED | https://static.simfileshare.net/assets/img/box.png (visual match) | None found. | Derivative of site image | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Permission / operator position. |
| 29 | `frontend/host-icons.parts → icons/hosts/streamtape.png` | `c3ae425852b7f8343bac3f02d01b19794b94b336f65ed8bbf676e4c0c36722d4` | Streamtape | UNVERIFIED | Glyph cropped from https://streamtape.com/images/Logo@2x.png | None found. | Derivative (cropped) | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Permission / operator position. |
| 30 | `frontend/host-icons.parts → icons/hosts/turbobit.png` | `7b0fc21c4ea727d17b85324b6485073d86ecef9a7bafa7215352e2d92a45c57a` | Turbobit | UNVERIFIED | https://turbobit.net/apple-touch-icon.png (pixel-identical after resize) | None found. | Derivative (resize) of official icon | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Permission / operator position. |
| 31 | `frontend/host-icons.parts → icons/hosts/upload42.png` | `510c6967aa8e6cc17f98502d6e53de88625040371ebc5158a60e7a50a248cd35` | UPLOAD42 | UNVERIFIED | Glyph cropped from https://www.upload42.com/ds2/img/logo.png | None found. | Derivative (cropped) | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Permission / operator position. |
| 32 | `frontend/host-icons.parts → icons/hosts/uploadhaven.png` | `7f534e2b4c12cf1c8f457679946ff5b901a9664543a770741a8e3135a9006ef5` | UploadHaven | UNVERIFIED | Historical apple-touch-icon (Wayback 2018-10-19, 2024-08-05); superseded | None found. | Derivative of a superseded official icon | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Permission / operator position. |
| 33 | `frontend/host-icons.parts → icons/hosts/uploadrar.png` | `b62082cc4d2816de4b27db853760a463eb1fc4ed2aba52130346f9608ac48c3b` | UploadRAR | UNVERIFIED | Not established (matches no UploadRAR mark found) | None found. | Not established | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Establish origin. |
| 34 | `frontend/host-icons.parts → icons/hosts/world-files.png` | `7ee8d164e228afdbd829627d8273a0dd2f43d175b6db73d96d7d11c115d04f29` | World-Files | UNVERIFIED | https://world-files.com/favicon.ico (pixel-identical after resize) | None found. | Derivative (resize) of official favicon | Host column of Downloads / Dashboard (beside a transfer's source host, mapped in `ui-transfer-source-presentation.js` `HOST_ASSETS`) | **UNRESOLVED** | Retained (1.0.14 deferral) | Permission / operator position. |
| 35 | `frontend/static/authentik-oidc.svg` | `1adfd82439678a210b8011b18b3451bf032d2f39bdb00a54029e8c021757ff59` | (name only: authentik) | Not established. It is not authentik's official mark (an orange-red key tile, goauthentik.io/press) | Not established (generic hexagon-and-person glyph; added in commit 7f479a9e) | None found | Generic glyph of unknown origin | Login page: the mark on the "Sign in with …" OpenID Connect button, embedded by `backend/api/auth_routes.py` until 1.0.13. The investigation recorded it as unreferenced; that was wrong. | **UNRESOLVED** | **Removed in 1.0.13** | Removed; the button now shows Lucide ShieldUser. No further action. |
| 36 | `frontend/static/icons/dp/settings/discord-notifications.svg` | `815bca1ad8ad624a040b19888421163530f8f294758d46d0a389f5cd47654134` | Discord (evoked) | Discord Inc. (https://discord.com/branding) — if treated as its mark | DebridPulse-made pixel icon (commit e2242769) evoking Discord's "Clyde" | "You must have permission from Discord before using any of the Discord Marks …"; "do not edit, change, distort, recolor … the Discord logo." | DebridPulse-made allusion (not the official geometry) | Not referenced by the application | **UNRESOLVED** | **Removed in 1.0.13** | Removed; no further action. |
| 37 | `frontend/static/icons/dp/settings/openid-connect.svg` | `95e259d6df249a14ed356606550e74f03e5678469cb993103c2f52be876049ce` | OpenID Connect | OpenID Foundation (Trademark Usage Policy 2026-06-04) | DebridPulse-made pixel icon (commit e2242769) **reproducing the OpenID logo, recoloured** | §4: "a Non-Licensed User may not use the OpenID Logo … in any manner, in whole or in part"; §2.5: marks "may not be revised or altered in any way". | Recoloured derivative of the OpenID Logo | Not referenced by the application | **CORRECTION REQUIRED** | **Removed in 1.0.13** | Removed; no further action. |

## Requests

A rights holder who does not want its mark shown in DebridPulse can ask for its
removal at <https://github.com/Xipher-Zero/debridpulse/issues>.
