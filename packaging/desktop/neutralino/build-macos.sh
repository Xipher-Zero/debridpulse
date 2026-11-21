#!/usr/bin/env bash
# The macOS native shell: Neutralino 6.10.0 built from its pinned source (the
# tag's commit, verified) with DownloadPulse's macOS patch (its digest checked),
# for this Mac's architecture, with Apple's clang and CMake.
#   build-macos.sh <out-dir>
# Writes <out-dir>/downloadpulse-shell and <out-dir>/PROVENANCE.json (source
# commit, patch digest, toolchain, binary SHA-256), which
# packaging/desktop/assemble.py --shell installs and records. Linux builds the
# same source with its own patch in the Flatpak recipe; Windows keeps the
# released binary.
#
# macos-main-thread-shutdown.patch: every macOS shutdown path -- app.exit (the
# supervisor's Quit, for the tray and Cmd+Q), SIGINT/SIGTERM, and the
# application's own termination (the Dock's Quit) -- tears the status item and
# the window down on the main thread; a termination signal is a GCD event on
# the main queue, never AppKit work in a signal handler; the tray keeps no
# autorelease pool of its own (draining it was objc's badPop abort).
# macos-frame-theme.patch: window.setFrameTheme {caption "#RRGGBB", dark} --
# the title bar drawn transparent over the window background, set to the
# theme's frame colour, in its dark or light appearance (the supervisor calls
# it with the page's theme); the traffic lights and dragging stay the system's.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$1"; out="$(cd "$1" && pwd)"
TAG=v6.10.0
COMMIT=4361b63eb1157fc7dedb29b92815a08ec0002767
PATCH=macos-main-thread-shutdown.patch
PATCH_SHA256=1a7ccc59b73a080c5f288fd5995071adeec5888318a57af21a5638d3af7c9896
FRAME_PATCH=macos-frame-theme.patch
FRAME_PATCH_SHA256=209ba63df784de9344708e4755d3b689ae177a1b1b095222bdf0f588c114430f
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
git clone -q --depth 1 --branch "$TAG" https://github.com/neutralinojs/neutralinojs.git "$work/src"
[ "$(git -C "$work/src" rev-parse HEAD)" = "$COMMIT" ] || { echo "$TAG is not $COMMIT"; exit 1; }
echo "$PATCH_SHA256  $here/$PATCH" | shasum -a 256 -c -
echo "$FRAME_PATCH_SHA256  $here/$FRAME_PATCH" | shasum -a 256 -c -
git -C "$work/src" apply "$here/$PATCH"
git -C "$work/src" apply "$here/$FRAME_PATCH"
arch="$(uname -m)"; name="neutralino-mac_$([ "$arch" = arm64 ] && echo arm64 || echo x64)"
cmake -S "$work/src" -B "$work/build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_OSX_ARCHITECTURES="$arch" >/dev/null
cmake --build "$work/build" -j "$(sysctl -n hw.ncpu)" >/dev/null
install -m 755 "$work/src/bin/$name" "$out/downloadpulse-shell"
digest="$(shasum -a 256 "$out/downloadpulse-shell" | cut -d' ' -f1)"
cat > "$out/PROVENANCE.json" <<JSON
{"neutralino": "$TAG", "commit": "$COMMIT", "patches": {"$PATCH": "$PATCH_SHA256", "$FRAME_PATCH": "$FRAME_PATCH_SHA256"},
 "architecture": "$arch", "cmake": "$(cmake --version | head -1)", "compiler": "$(clang --version | head -1)",
 "sdk": "$(xcrun --show-sdk-version)", "binary_sha256": "$digest"}
JSON
cat "$out/PROVENANCE.json"
