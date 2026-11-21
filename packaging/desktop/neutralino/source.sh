#!/usr/bin/env bash
# The native shell's corresponding source for the installers' source handover:
# Neutralino 6.10.0 exactly as built (the tag's tree at its verified commit),
# DownloadPulse's two patches (digests checked), Neutralino's licence and how
# each installer builds it.
#   source.sh <out-dir>
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$1"; out="$(cd "$1" && pwd)"
TAG=v6.10.0
COMMIT=4361b63eb1157fc7dedb29b92815a08ec0002767
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
git clone -q --depth 1 --branch "$TAG" https://github.com/neutralinojs/neutralinojs.git "$work/src"
[ "$(git -C "$work/src" rev-parse HEAD)" = "$COMMIT" ] || { echo "$TAG is not $COMMIT"; exit 1; }
git -C "$work/src" archive --format=tar.gz --prefix=neutralinojs-6.10.0/ -o "$out/neutralinojs-6.10.0-$COMMIT.tar.gz" "$COMMIT"
cp "$work/src/LICENSE" "$out/LICENSE"
sha256sum -c - <<SUMS
5c5b8ff90f62bb8474857e3128a5768ea8cf55273d9887c879c82f47abc4cc22  $here/linux-gtk-main-thread.patch
1a7ccc59b73a080c5f288fd5995071adeec5888318a57af21a5638d3af7c9896  $here/macos-main-thread-shutdown.patch
209ba63df784de9344708e4755d3b689ae177a1b1b095222bdf0f588c114430f  $here/macos-frame-theme.patch
SUMS
cp "$here/linux-gtk-main-thread.patch" "$here/macos-main-thread-shutdown.patch" "$here/macos-frame-theme.patch" \
   "$here/build-macos.sh" "$out/"
cat > "$out/BUILD.md" <<EOF
# The DownloadPulse desktop shell: Neutralino 6.10.0 (MIT, LICENSE)

Source: \`neutralinojs-6.10.0-$COMMIT.tar.gz\`, the tree of tag $TAG at commit $COMMIT
(https://github.com/neutralinojs/neutralinojs).

- **Linux (the Flatpak):** \`linux-gtk-main-thread.patch\` applied (\`patch -p1\`), then, in the
  GNOME 49 SDK: \`mkdir -p stubs && ar rc stubs/libstdc++fs.a\` (an empty archive for the legacy
  library Neutralino's CMake names), \`cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
  -DCMAKE_EXE_LINKER_FLAGS=-L\$PWD/stubs\`, \`cmake --build build\`; \`bin/neutralino-linux_x64\` is
  installed as the shell (packaging/desktop/flatpak/net.downloadpulse.desktop.yml, module neutralino).
- **macOS (the DMG):** \`build-macos.sh\` -- \`macos-main-thread-shutdown.patch\` then
  \`macos-frame-theme.patch\` applied, CMake Release build with Apple's clang for the Mac's
  architecture.
- **Windows:** the released \`neutralino-win_x64.exe\` of $TAG, unmodified.
EOF
ls -la "$out"
