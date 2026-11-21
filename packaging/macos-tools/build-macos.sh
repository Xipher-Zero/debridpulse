#!/usr/bin/env bash
# DownloadPulse's archive, Usenet and rsync helper tools for macOS (arm64):
#   par2 (par2cmdline) and zstd -- Debian's source packages (prepared by
#     prepare-sources.sh), built natively with Apple's Command Line Tools;
#   GNU rsync 3.5.2 -- upstream's source, built natively (macOS's own
#     /usr/bin/rsync is openrsync, "2.6.9 compatible", below DownloadPulse's 3.4);
#   7-Zip 7zz and RARLAB unrar -- the official releases, unmodified.
# Every download is refused unless it matches official.sha256.
# Usage: build-macos.sh <sources-dir> <work-dir> <out-dir>
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
src="$1"; work="$2"; out="$3"
# Absolute paths: the recipe changes directory before it installs into them.
mkdir -p "$work" "$out"; src="$(cd "$src" && pwd)"; work="$(cd "$work" && pwd)"; out="$(cd "$out" && pwd)"
jobs="$(sysctl -n hw.ncpu)"
mkdir -p "$work" "$out/bin" "$out/copyright"
# Every pinned download is verified; RARLAB's RAR package (a RAR trial
# distribution, of which only UnRAR is used) is fetched here, never prepared
# in <sources-dir> (prepare-sources.sh).
grep -vE '^(#|$)' "$here/official.sha256" | while read -r digest name url; do
  case "$name" in rar*) curl -sSfL -o "$work/$name" "$url"; file="$work/$name" ;; *) file="$src/$name" ;; esac
  echo "$digest  $file" | shasum -a 256 -c --quiet
done
tar -xzf "$src/par2cmdline-0.8.1.tar.gz" -C "$work"
(cd "$work/par2cmdline-0.8.1" && ./configure --disable-openmp >/dev/null && make -j"$jobs" par2 >/dev/null)
install -m 0755 "$work/par2cmdline-0.8.1/par2" "$out/bin/par2"; cp "$work/par2cmdline-0.8.1/debian/copyright" "$out/copyright/par2"
tar -xzf "$src/libzstd-1.5.7+dfsg.tar.gz" -C "$work"
make -C "$work/libzstd-1.5.7+dfsg/programs" -j"$jobs" zstd HAVE_ZLIB=0 HAVE_LZMA=0 HAVE_LZ4=0 >/dev/null
install -m 0755 "$work/libzstd-1.5.7+dfsg/programs/zstd" "$out/bin/zstd"; cp "$work/libzstd-1.5.7+dfsg/debian/copyright" "$out/copyright/zstd"
tar -xzf "$src/rsync-3.5.2.tar.gz" -C "$work"
(cd "$work/rsync-3.5.2" && ./configure --disable-xxhash --disable-zstd --disable-lz4 --disable-openssl --disable-md2man --disable-idn \
   --with-included-popt --with-included-zlib >/dev/null && make -j"$jobs" rsync >/dev/null)
install -m 0755 "$work/rsync-3.5.2/rsync" "$out/bin/rsync"; cp "$work/rsync-3.5.2/COPYING" "$out/copyright/rsync"
mkdir -p "$work/7zip" && tar -xJf "$src/7z2501-mac.tar.xz" -C "$work/7zip" 7zz License.txt
install -m 0755 "$work/7zip/7zz" "$out/bin/7zz"; cp "$work/7zip/License.txt" "$out/copyright/7-zip"
tar -xzf "$work/rarmacos-arm-723.tar.gz" -C "$work" rar/unrar rar/license.txt rar/acknow.txt
install -m 0755 "$work/rar/unrar" "$out/bin/unrar"; cp "$work/rar/license.txt" "$out/copyright/unrar"
cp "$work/rar/acknow.txt" "$out/copyright/unrar-acknowledgments"
for exe in "$out/bin/"*; do printf '%s: ' "$(basename "$exe")"; otool -L "$exe" | tail -n +2 | awk '{print $1}' | tr '\n' ' '; echo; done | tee "$out/links.txt"
"$out/bin/rsync" --version | head -1
