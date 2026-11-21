#!/usr/bin/env bash
# DownloadPulse's FFmpeg for Windows x64 (docs/SUPPLY_CHAIN_POLICY.md section 4b):
# the same Debian source package and the same capability set (build-package.sh's
# configure options) as the Linux package, cross-compiled with Debian's
# mingw-w64 toolchain (win32 threads, everything static), so the two
# executables need only DLLs every Windows has.
#
# Usage: build-windows.sh <work-dir> <out-dir>
#   FFMPEG_SOURCE_VERSION  exact source version to fetch (as build-package.sh)
# Produces <out-dir>/bin/{ffmpeg,ffprobe}.exe, <out-dir>/source/ (Debian's
# source package as fetched + this script + build-package.sh), the copyright
# file, the imported DLLs of each executable (imports.txt) and VERSION.
set -euo pipefail

DP_SUFFIX=dp1
here="$(cd "$(dirname "$0")" && pwd)"
work="$1"
out="$2"
mkdir -p "$work" "$out/bin" "$out/source"

cd "$work"
apt-get source --only-source "ffmpeg${FFMPEG_SOURCE_VERSION:+=$FFMPEG_SOURCE_VERSION}"
tree="$(find "$work" -mindepth 1 -maxdepth 1 -type d -name 'ffmpeg-*')"
test "$(printf '%s\n' "$tree" | wc -l)" -eq 1

cd "$tree"
source_version="$(dpkg-parsechangelog -SVersion)"
if [ -n "${FFMPEG_SOURCE_VERSION:-}" ] && [ "$source_version" != "$FFMPEG_SOURCE_VERSION" ]; then
  echo "fetched ffmpeg $source_version, expected $FFMPEG_SOURCE_VERSION" >&2
  exit 1
fi
export SOURCE_DATE_EPOCH="$(dpkg-parsechangelog -STimestamp)"

# The capability set is build-package.sh's: the options of its one configure
# line (after --prefix and --extra-version), read from it, never restated.
options=()
while IFS= read -r option; do options+=("$option"); done < <(
  sed -n '/^\.\/configure --prefix=\/usr /,/[^\\]$/p' "$here/build-package.sh" | tr -d '\\' | tr -s ' \n' '\n' | tail -n +4)
case " ${options[*]} " in *" --disable-everything "*" --disable-network "*) ;; *)
  echo "could not read the capability set from build-package.sh" >&2; exit 1 ;; esac
./configure --extra-version="${source_version##*-}+${DP_SUFFIX}" "${options[@]}" \
  --target-os=mingw32 --arch=x86_64 --enable-cross-compile --cross-prefix=x86_64-w64-mingw32- \
  --pkg-config=false --extra-ldflags=-static
grep -qx '#define CONFIG_GPL 0' config.h && grep -qx '#define CONFIG_NETWORK 0' config.h
make -j"$(nproc)"

for name in ffmpeg ffprobe; do
  install -m 0755 "$name.exe" "$out/bin/$name.exe"
  x86_64-w64-mingw32-strip --strip-unneeded "$out/bin/$name.exe"
done
# Every DLL either executable imports: only Windows' own may appear.
for exe in "$out/bin/"*.exe; do
  printf '%s: ' "$(basename "$exe")"
  x86_64-w64-mingw32-objdump -p "$exe" | sed -n 's/^\s*DLL Name: //p' | tr '\n' ' '
  echo
done > "$out/imports.txt"
cat "$out/imports.txt"
if grep -viE '^[a-z]+\.exe:( (KERNEL32|USER32|ADVAPI32|SHELL32|OLE32|BCRYPT|msvcrt|WS2_32|SHLWAPI|api-ms-win-[a-z0-9-]+)\.dll)+ ?$' "$out/imports.txt"; then
  echo "an FFmpeg executable imports a non-system DLL" >&2
  exit 1
fi
install -m 0644 debian/copyright "$out/copyright"
find "$work" -maxdepth 1 -type f \( -name 'ffmpeg_*.dsc' -o -name 'ffmpeg_*.tar.*' \) -exec cp {} "$out/source/" \;
cp "$here/build-windows.sh" "$here/build-package.sh" "$out/source/"
printf '%s\n' "${source_version}+${DP_SUFFIX}" > "$out/VERSION"
echo "built ffmpeg ${source_version}+${DP_SUFFIX} for Windows x64"
