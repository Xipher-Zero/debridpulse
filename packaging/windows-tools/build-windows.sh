#!/usr/bin/env bash
# DownloadPulse's archive and Usenet helper tools for Windows x64:
#   par2 (par2cmdline-turbo, pinned upstream source) and zstd (Debian's signed
#     source package), built static
#     with Debian's mingw-w64 toolchain (only system DLLs);
#   7-Zip (7z.exe + 7z.dll, whose RAR decoder DownloadPulse's extraction uses;
#     the standalone 7za.exe, which SABnzbd runs) and
#     RARLAB UnRAR -- the official releases, unmodified, refused unless they
#     match official.sha256.
# Usage: build-windows.sh <work-dir> <out-dir>
# Produces <out-dir>/bin/{par2,zstd,7z,7za,UnRAR,netless-launcher}.exe + 7z.dll, <out-dir>/source/
# (each Debian source package as fetched + this script + official.sha256),
# <out-dir>/copyright/ (each tool's license), imports.txt and VERSIONS.
set -euo pipefail

HOST=x86_64-w64-mingw32
here="$(cd "$(dirname "$0")" && pwd)"
work="$1"
out="$2"
mkdir -p "$work" "$out/bin" "$out/source" "$out/copyright"

fetch() {  # fetch <source package> <tree glob>: Debian's signed source, unpacked; prints its tree
  (cd "$work" && apt-get source --only-source -qq "$1" >/dev/null)
  find "$work" -mindepth 1 -maxdepth 1 -type d -name "$2" | head -1
}
versions=()

# The pinned downloads: refused unless they match official.sha256.
grep -vE '^(#|$)' "$here/official.sha256" | while read -r digest name url; do
  curl -sSfL -o "$work/$name" "$url"
  echo "$digest  $work/$name" | sha256sum -c --quiet
done

# par2: par2cmdline-turbo, the par2 SABnzbd ships for Windows. SABnzbd passes
# every path in Windows' \\?\ long-path form; par2cmdline 0.8.1 (Debian's)
# refuses those as wildcards, par2cmdline-turbo accepts them on Windows.
tar -xzf "$work/par2cmdline-turbo-1.5.0.tar.gz" -C "$work"
tree="$work/par2cmdline-turbo-1.5.0"
# MinGW GCC can leave the stack misaligned for 32-byte AVX values (GCC bug
# 54412; upstream's own Windows release is built with ClangCL instead): the
# assembler is told to emit every vector move unaligned, which a misaligned
# stack cannot fault, and the binary is refused if an aligned AVX move remains.
vector="-Wa,-muse-unaligned-vector-move"
(cd "$tree" && autoreconf -fi >/dev/null 2>&1 \
   && ./configure --host="$HOST" CFLAGS="-O2 $vector" CXXFLAGS="-O2 $vector" LDFLAGS="-static" >/dev/null \
   && make -j"$(nproc)" par2.exe >/dev/null)
if $HOST-objdump -d "$tree/par2.exe" | grep -qE '\svmov(aps|apd|dqa|dqa32|dqa64)\s'; then
  echo "par2.exe still contains aligned AVX moves" >&2; exit 1
fi
install -m 0755 "$tree/par2.exe" "$out/bin/par2.exe"
cp "$tree/COPYING" "$out/copyright/par2"
versions+=("par2 par2cmdline-turbo 1.5.0 (upstream source tag v1.5.0)")

tree="$(fetch libzstd 'libzstd-*')"
make -C "$tree/programs" -j"$(nproc)" zstd EXT=.exe CC="$HOST-gcc" AR="$HOST-ar" WINDRES="$HOST-windres" \
  LDFLAGS="-static" ZSTD_LEGACY_SUPPORT=0 HAVE_THREAD=1 HAVE_ZLIB=0 HAVE_LZMA=0 HAVE_LZ4=0 >/dev/null
install -m 0755 "$tree/programs/zstd.exe" "$out/bin/zstd.exe"
cp "$tree/debian/copyright" "$out/copyright/zstd"
versions+=("zstd $(cd "$tree" && dpkg-parsechangelog -SVersion)")

# netless-launcher: DownloadPulse's own "#!" stand-in for the Media Downloads
# helper wrappers (its source says why), static, kernel32 only.
$HOST-gcc -O2 -Wall -Werror -municode -static -o "$out/bin/netless-launcher.exe" "$here/netless-launcher.c"
versions+=("netless-launcher $(sha256sum "$here/netless-launcher.c" | cut -c1-16) (DownloadPulse source)")

for exe in "$out/bin/par2.exe" "$out/bin/zstd.exe" "$out/bin/netless-launcher.exe"; do $HOST-strip --strip-unneeded "$exe"; done

7z x -y -o"$work/7zip" "$work/7z2501-x64.exe" 7z.exe 7z.dll License.txt >/dev/null
install -m 0755 "$work/7zip/7z.exe" "$work/7zip/7z.dll" "$out/bin/"
7z x -y -o"$work/7zip-extra" "$work/7z2501-extra.7z" x64/7za.exe >/dev/null
install -m 0755 "$work/7zip-extra/x64/7za.exe" "$out/bin/7za.exe"
cp "$work/7zip/License.txt" "$out/copyright/7-zip"
versions+=("7-zip 25.01 (official x64 release)")
7z x -y -o"$work/unrar" "$work/unrarw64.exe" UnRAR.exe license.txt >/dev/null
install -m 0755 "$work/unrar/UnRAR.exe" "$out/bin/"
cp "$work/unrar/license.txt" "$out/copyright/unrar"
versions+=("unrar $(sha256sum "$out/bin/UnRAR.exe" | cut -c1-16) (official RARLAB x64 release)")

for exe in "$out/bin/"*.exe "$out/bin/"*.dll; do
  printf '%s: ' "$(basename "$exe")"
  $HOST-objdump -p "$exe" | sed -n 's/^\s*DLL Name: //p' | tr '\n' ' '
  echo
done > "$out/imports.txt"
cat "$out/imports.txt"
if grep -E '^(par2|zstd|netless-launcher)\.exe:' "$out/imports.txt" | grep -viE '^[a-z0-9-]+\.exe: ((KERNEL32|USER32|ADVAPI32|SHELL32|msvcrt|api-ms-win-[a-z0-9-]+)\.dll ?)+$'; then
  echo "a DownloadPulse-built tool imports a non-system DLL" >&2
  exit 1
fi
find "$work" -maxdepth 1 -type f \( -name '*.dsc' -o -name '*.tar.*' \) -exec cp {} "$out/source/" \;
cp "$here/build-windows.sh" "$here/official.sha256" "$here/netless-launcher.c" "$out/source/"
printf '%s\n' "${versions[@]}" > "$out/VERSIONS"
cat "$out/VERSIONS"
