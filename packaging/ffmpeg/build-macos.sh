#!/usr/bin/env bash
# DownloadPulse's FFmpeg for macOS (arm64): the same Debian source package (as
# prepared by packaging/macos-tools/prepare-sources.sh) and the same capability
# set (build-package.sh's configure options) as the Linux package, built natively with Apple's
# Command Line Tools. --disable-autodetect keeps every system framework out.
# Usage: build-macos.sh <sources-dir> <work-dir> <out-dir>
set -euo pipefail
DP_SUFFIX=dp1
here="$(cd "$(dirname "$0")" && pwd)"
src="$1"; work="$2"; out="$3"
# Absolute paths: the recipe changes directory before it installs into them.
mkdir -p "$work" "$out"; src="$(cd "$src" && pwd)"; work="$(cd "$work" && pwd)"; out="$(cd "$out" && pwd)"
mkdir -p "$work" "$out/bin"
tar -xzf "$src/ffmpeg-7.1.5.tar.gz" -C "$work"
cd "$work/ffmpeg-7.1.5"
# The capability set is build-package.sh's: the options of its one configure
# line (after --prefix and --extra-version), read from it, never restated.
options=()
while IFS= read -r option; do options+=("$option"); done < <(
  sed -n '/^\.\/configure --prefix=\/usr /,/[^\\]$/p' "$here/build-package.sh" | tr -d '\\' | tr -s ' \n' '\n' | tail -n +4)
case " ${options[*]} " in *" --disable-everything "*" --disable-network "*) ;; *)
  echo "could not read the capability set from build-package.sh" >&2; exit 1 ;; esac
./configure --cc=clang --extra-version="0+deb13u1+${DP_SUFFIX}" "${options[@]}"
grep -qx '#define CONFIG_GPL 0' config.h && grep -qx '#define CONFIG_NETWORK 0' config.h
make -j"$(sysctl -n hw.ncpu)"
for name in ffmpeg ffprobe; do install -m 0755 "$name" "$out/bin/$name"; strip "$out/bin/$name"; done
# Linked libraries: macOS itself only -- libSystem, and three system
# frameworks FFmpeg's libavutil links whenever macOS has them (its
# avutil_suggest takes them as soon as configure detects them, and FFmpeg 7.1
# offers no switch to refuse them); every component that would use them
# (VideoToolbox, AudioToolbox, AVFoundation, CoreImage, Metal) is compiled
# out, so they are linked, never called. No third-party library, as on Linux.
for exe in "$out/bin/"*; do
  printf '%s: ' "$(basename "$exe")"; otool -L "$exe" | tail -n +2 | awk '{print $1}' | tr '\n' ' '; echo
done | tee "$out/links.txt"
if grep -vE '^[a-z]+: (/usr/lib/libSystem\.B\.dylib ?|/System/Library/Frameworks/(CoreFoundation|CoreVideo|CoreMedia)\.framework/Versions/A/(CoreFoundation|CoreVideo|CoreMedia) ?)+$' "$out/links.txt"; then
  echo "an FFmpeg executable links more than macOS itself" >&2; exit 1
fi
for component in VIDEOTOOLBOX AUDIOTOOLBOX AVFOUNDATION COREIMAGE METAL APPKIT SECURETRANSPORT; do
  grep -qx "#define CONFIG_$component 0" config.h
done
echo "built ffmpeg 7:7.1.5-0+deb13u1+${DP_SUFFIX} for macOS $(uname -m)"
