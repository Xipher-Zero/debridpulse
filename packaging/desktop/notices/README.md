# Desktop notices

The licence and notice texts of components every DownloadPulse desktop
application carries, beyond those in `licenses/`, `LICENSES/` and each bundled
tool's own `tools/copyright/`. `packaging/desktop/assemble.py` installs this
directory as `licenses/desktop/` in every desktop layout. Each file below is
byte-for-byte what its source states, fetched from the pinned origin shown.
Nothing here is a DownloadPulse licence.

## python-build-standalone 20260901 (the bundled CPython 3.12.14 runtime)

The runtime archives DownloadPulse installs (`install_only_stripped`) carry only
CPython's own licence. python-build-standalone ships the licences of
everything it builds into the runtime (OpenSSL, libffi, SQLite, XZ, bzip2,
zlib, mpdecimal, expat, Tcl/Tk, ncurses, libedit, libuuid, Berkeley DB, ...)
in `python/licenses/` of its `full` archives; these are those files, from
`cpython-3.12.14+20260901-aarch64-apple-darwin-pgo+lto-full.tar.zst`
(GitHub release asset digest
`sha256:dbefa04d4107b449e17022f9c78d3eacbd37a1aa8c166c6220dbd95addf3f318`).
The Windows `pgo-full` archive (`sha256:b7cf8be5cd5222d1e456fbf71257efe558d84fe092757b6892c16584e5df01c2`)
carries the same 19 files, differing only in line endings. Which of them
apply to which platform's runtime is recorded in that release's `PYTHON.json`.

| File | sha256 |
|---|---|
| `python-build-standalone/LICENSE.bdb.txt` | `3bdff06e69991c94664f2ef5c5f8096f60b7dbec071756ea6cc26b445e06ec5b` |
| `python-build-standalone/LICENSE.bzip2.txt` | `1f38bbc7caacafd65169276d759c0d88c991b753b643ce35d0e45ea1971dd441` |
| `python-build-standalone/LICENSE.cpython.txt` | `86e61415828a8b5b06ec8d024e6f086ce155a8b85fd0c419c0ba4dc004e74fdd` |
| `python-build-standalone/LICENSE.expat.txt` | `122f2c27000472a201d337b9b31f7eb2b52d091b02857061a8880371612d9534` |
| `python-build-standalone/LICENSE.libX11.txt` | `2daec087a88e7c9b8082557cdeebad5bbb8155a4137472f0b22e269cd99d0c1e` |
| `python-build-standalone/LICENSE.libXau.txt` | `56abe29bb1d9806a9e04fa9f80fed2c0f18027594df3f098148d814aef6bddfa` |
| `python-build-standalone/LICENSE.libedit.txt` | `29cea33c32bbc9785142386377915612a2fa786482c46843383384aded2e09b1` |
| `python-build-standalone/LICENSE.libffi.txt` | `deaf3a42effb551a5b140fa9afefed183a27f1341c6d1bf430d106a5e6931fc0` |
| `python-build-standalone/LICENSE.liblzma.txt` | `9a4062de0a2c388a98cf35a35d348b62fa97c838a71c3c28ee1a2d7d0a565b02` |
| `python-build-standalone/LICENSE.libuuid.txt` | `122ee1f7e258f2c3c0e538a75c037684f420454bf3850ddc74ce750bbf5fe86b` |
| `python-build-standalone/LICENSE.libxcb.txt` | `c5ffbfeaa501071ceeb97b7de2c0d703fdaa35de01c0fb6cbac1c28453a3e9fd` |
| `python-build-standalone/LICENSE.mpdecimal.txt` | `669512af7219f58be03a398766d7c9da11a3b3df9d3f05cb74c5ceca25c8da3b` |
| `python-build-standalone/LICENSE.ncurses.txt` | `87a4c4442337b8968ef956031c406b74f9cb7149b7ba87311bdaba534816201c` |
| `python-build-standalone/LICENSE.openssl-1.1.txt` | `9c04cce50c4989d5601dd8b07f6ab922c40388b66ac736c9007cb1ed9d9dd560` |
| `python-build-standalone/LICENSE.openssl-3.txt` | `7d5450cb2d142651b8afa315b5f238efc805dad827d91ba367d8516bc9d49e7a` |
| `python-build-standalone/LICENSE.sqlite.txt` | `38bef3d28b24f145ea293bd3b6eb4b20396982abc8303128fb493986ea5bc719` |
| `python-build-standalone/LICENSE.tcl.txt` | `c0a69a2bfd757361ec7e6143973b103c90409316b49e9c88db26ad6388e79f16` |
| `python-build-standalone/LICENSE.tix.txt` | `3ac5cdd0bef6c43ce34c6a7ced452081d9e5a0bf94082b9f9147d23ec9e214f5` |
| `python-build-standalone/LICENSE.zlib.txt` | `818922b2620f12801a12bf78e399644a30990e66824abd8ca8ec24d451d6f92c` |

## Neutralino v6.10.0 (the native shell, `desktop/shell/`)

The release zip DownloadPulse installs (`neutralinojs-v6.10.0.zip`) carries no
licence file. These are Neutralino's own licence and those of the libraries
built into its executable, from the source at tag `v6.10.0` (commit
`4361b63eb1157fc7dedb29b92815a08ec0002767`). Where a library states its licence only in its source header, that
header block is reproduced; where the bundled copy states none, the licence of
its upstream project is given, at the commit shown.

| File | Origin | sha256 |
|---|---|---|
| `neutralino/LICENSE` | https://raw.githubusercontent.com/neutralinojs/neutralinojs/4361b63eb1157fc7dedb29b92815a08ec0002767/LICENSE | `b860f62b9354b256468430e862053c84c40f6de491c02529bf1cf5c40f688ea7` |
| `neutralino/lib-asio-COPYING` | https://raw.githubusercontent.com/neutralinojs/neutralinojs/4361b63eb1157fc7dedb29b92815a08ec0002767/lib/asio/COPYING | `73217f94e4d10db741243b1f313b453db4186a1ad636178ecfafe028a8f24c0e` |
| `neutralino/lib-asio-LICENSE_1_0.txt` | https://raw.githubusercontent.com/neutralinojs/neutralinojs/4361b63eb1157fc7dedb29b92815a08ec0002767/lib/asio/LICENSE_1_0.txt | `c9bff75738922193e67fa726fa225535870d2aa1059f91452c411736284ad566` |
| `neutralino/lib-base64-LICENSE` | https://raw.githubusercontent.com/tobiaslocker/base64/8d96a2a737ac1396304b1de289beb3a5ea0cb752/LICENSE (Neutralino's `lib/base64/base64.hpp` states no licence; it is an earlier form of this project's header -- provenance inferred from its API and structure) | `dadaa58fd6c6d429427ad9fc5fae5f056ddf9a6d3cdffb6ba567822cb4fc889e` |
| `neutralino/lib-clip-LICENSE.txt` | https://raw.githubusercontent.com/neutralinojs/neutralinojs/4361b63eb1157fc7dedb29b92815a08ec0002767/lib/clip/LICENSE.txt | `10adc4e9d353d4f640b6150ba70107638bfa89dd3f944b5ded66f6dfd2fa6ad5` |
| `neutralino/lib-easylogging-LICENSE` | https://raw.githubusercontent.com/amrayn/easyloggingpp/1272515122ce559e77144a8404f419fee0423b81/LICENSE (the bundled header states MIT and links this file) | `0c801c70fc327cf979aa87d2f55c218041711c4130d31e8cef4ae65f9ba5bbfd` |
| `neutralino/lib-efsw-LICENSE` | https://raw.githubusercontent.com/neutralinojs/neutralinojs/4361b63eb1157fc7dedb29b92815a08ec0002767/lib/efsw/LICENSE | `e5e2219b5885a7fa7c02a26aa8405e2f802d5af86e0485e30429b62c7fc8c9ce` |
| `neutralino/lib-filedialogs-header-licence.txt` | header block of the bundled source at the tag (the file's first line names it) | `1e6c549604dcd2fe270ea0dfef25c2662c3acd0cbc785b7100ea9b4bc62e870f` |
| `neutralino/lib-httplib-LICENSE` | https://raw.githubusercontent.com/neutralinojs/neutralinojs/4361b63eb1157fc7dedb29b92815a08ec0002767/lib/httplib/LICENSE | `4b45cbe16d7b71b89ae6127e26e0d90a029198ca5e958ad8e3d0b8bbed364d8b` |
| `neutralino/lib-hwinfo-LICENSE` | https://raw.githubusercontent.com/neutralinojs/neutralinojs/4361b63eb1157fc7dedb29b92815a08ec0002767/lib/hwinfo/LICENSE | `d57a8bbbf9786567525ca0a8e998551a635762b6091413f4973e0c5abfe57892` |
| `neutralino/lib-infoware-LICENSE` | https://raw.githubusercontent.com/neutralinojs/neutralinojs/4361b63eb1157fc7dedb29b92815a08ec0002767/lib/infoware/LICENSE | `36ffd9dc085d529a7e60e1276d73ae5a030b020313e6c5408593a6ae2af39673` |
| `neutralino/lib-json-LICENSE.MIT` | https://raw.githubusercontent.com/nlohmann/json/v3.11.2/LICENSE.MIT (the bundled header: SPDX MIT, 2013-2022) | `86b998c792894ccb911a1cb7994f7a9652894e7a094c0b5e45be2f553f45cf14` |
| `neutralino/lib-mbedtls-LICENSE` | https://raw.githubusercontent.com/neutralinojs/neutralinojs/4361b63eb1157fc7dedb29b92815a08ec0002767/lib/mbedtls/LICENSE | `9b405ef4c89342f5eae1dd828882f931747f71001cfba7d114801039b52ad09b` |
| `neutralino/lib-platformfolders-header-licence.txt` | header block of the bundled source at the tag (the file's first line names it) | `60007d50e11489a7f2051995eb033e73ad862672dd9acf2b0c8dd093dbeec5a3` |
| `neutralino/lib-postject-LICENSE` | https://raw.githubusercontent.com/nodejs/postject/3c4f2080ee56025716c3add0f6c03b16e2af54ff/LICENSE (the bundled copy states no licence) | `6546657539feb5b454f400db317e71fa39ff5cd048fa5d78ae0ada2df72f1ea7` |
| `neutralino/lib-tinyprocess-LICENSE` | https://raw.githubusercontent.com/eidheim/tiny-process-library/5bc47531a97f80ac07092ac3ed56a26139c748b3/LICENSE (the bundled copy states no licence) | `b8b35d627492f972b4472eff980855766cf362e27c6e5681413ddb1d096940bc` |
| `neutralino/lib-trashcan-header-licence.txt` | header block of the bundled source at the tag (the file's first line names it) | `d57a5a4a26e471a8364818f7c3ac587858048cad4b8b0e0ad6863859a416ba77` |
| `neutralino/lib-tray-LICENSE` | https://raw.githubusercontent.com/zserge/tray/8dd1358b92562faf7c032cf5362fa97cbc7e13e9/LICENSE (the bundled copy states no licence) | `393b56cb24bf96e6022f8732379d940b11dfd9542e3d098968b44bfb6b90b260` |
| `neutralino/lib-websocketpp-COPYING` | https://raw.githubusercontent.com/neutralinojs/neutralinojs/4361b63eb1157fc7dedb29b92815a08ec0002767/lib/websocketpp/COPYING | `b31e597bc777d884a39663c11d0557a0af40780e1f451de11ea5103924a69667` |
| `neutralino/lib-webview-header-licence.txt` | header block of the bundled source at the tag (the file's first line names it) | `c76785cfc2baebc6980befeb72f274659ad0877baf59ffc87a90019eee5f6e2c` |
| `neutralino/lib-webview-windows-WebView2-SDK-LICENSE.txt` | `LICENSE.txt` of Microsoft's `Microsoft.Web.WebView2` NuGet package (1.0.4365-prerelease, the licence text Microsoft ships with every WebView2 SDK; Neutralino's Windows build links the SDK's `WebView2LoaderStatic.lib`, whose exact SDK version the bundled copy does not name) | `0af8f1b807512aae39c2ac1aa4d0cae65cabecb6fd554b8439a5162a0d6eca55` |

## NSIS 3.11 (the Windows installer's own stub)

The Windows installer is built on the hosted Windows runner by upstream's NSIS
3.11 for Windows (`nsis-3.11.zip`, pinned in
`packaging/desktop/windows/official.sha256`; local builds use Debian's
`nsis 3.11-1`, the same upstream release), with its LZMA compressor
(`SetCompressor /SOLID lzma`): the installer's stub carries NSIS's code under
the zlib/libpng licence and its LZMA module under the Common Public License
1.0 with NSIS's special exception. `nsis/copyright` is Debian's
`/usr/share/doc/nsis/copyright` for NSIS 3.11, unchanged; it reproduces the
same licences as upstream's own `COPYING` in the pinned zip (zlib/libpng,
bzip2, CPL-1.0 and the LZMA exception), which is kept out of the repository
only because its CRLF line endings would not pass the whitespace check. NSIS's
source for that version is upstream's `nsis-3.11-src.tar.bz2` (pinned beside
the zip; handed over in every installer's corresponding source as
`windows-tools/nsis/`), also at <https://nsis.sourceforge.io/>.

| File | sha256 |
|---|---|
| `nsis/copyright` | `d2b03d11801736ff4fda2a1e215a13c324dca7fd76eb948ae7d835000fe339d3` |
