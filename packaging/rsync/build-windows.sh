#!/usr/bin/bash
# DownloadPulse's rsync for Windows x64: upstream rsync built by MSYS2's own
# maintained recipe (the PKGBUILD and patch pinned in official.sha256), with one
# configure choice: rsync_cv_HAVE_SOCKETPAIR=no, so rsync talks to its remote
# shell -- DownloadPulse's native services/ssh_channel.py -- over real pipes. An
# MSYS2 socketpair() is an emulated socket a native Windows program cannot read.
#
# Runs inside an MSYS2 environment extracted from the pinned base archive into a
# build directory (MSYSTEM=MSYS, HOME inside that MSYS2 root), after
#   pacman -Syuu (twice) and
#   pacman -S --needed base-devel gcc autotools libiconv-devel libidn2-devel \
#     liblz4-devel libxxhash-devel libzstd-devel openssl-devel popt-devel
# Usage: build-windows.sh <recipe-dir holding the pinned PKGBUILD + patch [+ rsync-3.5.2.tar.gz]> <out-dir> [<sources-dir>]
#   <sources-dir>: where each MSYS2 source package named in SOURCES is downloaded (the
#   corresponding source of the DLLs, kept out of <out-dir>, which ships); without it
#   the licence texts come from the binary packages alone, and a package that ships
#   none fails the build.
# Produces <out-dir>/usr/bin/rsync.exe with the MSYS2 runtime DLLs it loads,
# <out-dir>/etc/fstab (MSYS2's default mount table: drives at /c, /d, ...,
# binary, no POSIX ACL emulation), <out-dir>/tmp, PACKAGES (each DLL's owning
# MSYS2 package), licenses/<package>/ (each package's own licence files),
# SOURCES (each package's MSYS2 source package URL) and SHA256SUMS.
# Upstream's test suite (check()) is not run here: it needs hours under MSYS2.
set -euo pipefail
case "$HOME" in /home/*) ;; *) echo "HOME is not inside this MSYS2 root" >&2; exit 1 ;; esac
recipe="$1"; out="$2"; sources="${3:-}"
here="$(cd "$(dirname "$0")" && pwd)"
grep -vE '^(#|$)' "$here/official.sha256" | while read -r digest name _url; do
  case "$name" in *.tar.xz) continue ;; esac
  echo "$digest  $recipe/$name" | sha256sum -c --quiet
done

build="$(mktemp -d "$HOME/rsync-build.XXXXXX")"
cp "$recipe/PKGBUILD" "$recipe/cygwin-fchmod-writable-handle.patch" "$build/"
# Upstream's source, if supplied beside the recipe, saves its download; makepkg
# verifies it (or the download) against the PKGBUILD's own sha256sums either way.
if [ -f "$recipe/rsync-3.5.2.tar.gz" ]; then cp "$recipe/rsync-3.5.2.tar.gz" "$build/"; fi
export rsync_cv_HAVE_SOCKETPAIR=no
(cd "$build" && makepkg --noconfirm --nocheck >makepkg.log 2>&1) || { tail -20 "$build/makepkg.log"; exit 1; }
if grep -qE '^#define HAVE_SOCKETPAIR' "$build"/src/rsync-*/config.h; then
  echo "rsync was configured with socketpair(): a native remote shell cannot read it" >&2; exit 1
fi
package="$(ls "$build"/rsync-*.pkg.tar.zst | head -1)"
root="$build/root"; mkdir -p "$root"; tar --zstd -xf "$package" -C "$root"

mkdir -p "$out/usr/bin" "$out/etc" "$out/tmp" "$out/licenses"
install -m 0755 "$root/usr/bin/rsync.exe" "$out/usr/bin/rsync.exe"
mkdir -p "$out/licenses/rsync" && cp "$build"/src/rsync-*/COPYING "$out/licenses/rsync/"
# Licence texts from an MSYS2 source package's upstream tarball (COPYING*,
# LICENSE*, at most three levels deep): several runtime packages ship none.
upstream_licences() {
  local unpacked; unpacked="$(mktemp -d)"; tar --zstd -xf "$1" -C "$unpacked"
  find "$unpacked" -maxdepth 2 -type f \( -name '*.tar.gz' -o -name '*.tar.xz' -o -name '*.tar.bz2' -o -name '*.tar.zst' \) |
    while read -r tarball; do
      { tar -tf "$tarball" | grep -E '^[^/]+/([^/]+/){0,2}(COPYING|LICEN[CS]E)[^/]*$' | grep -vE '\.(sig|in|am)$' || true; } |
        while read -r file; do mkdir -p "$2/$(dirname "$file")"; tar -xf "$tarball" -C "$2" "$file"; done
    done
  rm -rf "$unpacked"
}
: >"$out/PACKAGES"
# Each runtime DLL's MSYS2 package: its licence files (shipped beside the
# helper, licenses/<package>/) and the exact MSYS2 source package it was built
# from (SOURCES: MSYS2 publishes every one at repo.msys2.org/msys/sources/).
printf '%s\n' "# The corresponding source of rsync.exe and of each MSYS2 runtime DLL beside it." >"$out/SOURCES"
printf '%s\n' "rsync $(pacman -Qip "$package" | sed -n 's/^Version *: //p') this recipe's PKGBUILD, cygwin-fchmod-writable-handle.patch and rsync-3.5.2.tar.gz (packaging/rsync)" >>"$out/SOURCES"
for dll in $(ldd "$root/usr/bin/rsync.exe" | awk '$3 ~ "^/usr/bin/" {print $1}'); do
  cp "/usr/bin/$dll" "$out/usr/bin/"
  owner="$(pacman -Qo "/usr/bin/$dll" | sed 's/^.* is owned by //')"; name="${owner% *}"; version="${owner#* }"
  printf '%s %s\n' "$dll" "$owner" >>"$out/PACKAGES"
  base="$(sed -n '/^%BASE%$/{n;p;}' "/var/lib/pacman/local/$name-$version/desc")"
  mkdir -p "$out/licenses/$name"
  { pacman -Qlq "$name" | grep -E '^/usr/share/(licenses|doc)/.*/(COPYING|LICEN[CS]E|CYGWIN_LICENSE)[^/]*$' || true; } \
    | while read -r file; do cp "$file" "$out/licenses/$name/"; done
  printf '%s %s https://repo.msys2.org/msys/sources/%s-%s.src.tar.zst\n' "$name" "$version" "$base" "$version" >>"$out/SOURCES"
  if [ -n "$sources" ]; then
    mkdir -p "$sources"; source_package="$sources/$base-$version.src.tar.zst"
    [ -f "$source_package" ] || curl -sSfL -o "$source_package" "https://repo.msys2.org/msys/sources/$base-$version.src.tar.zst"
    upstream_licences "$source_package" "$out/licenses/$name/upstream"
  fi
  [ -n "$(find "$out/licenses/$name" -type f)" ] || { echo "$name: no licence text found" >&2; exit 1; }
done
printf '%s\n' "# The rsync helper's mount table (MSYS2's default): drives are /c, /d, ..." \
  'none / cygdrive binary,posix=0,noacl,user 0 0' >"$out/etc/fstab"
# The helper is run outside MSYS2, from a plain Windows PATH, with its own
# runtime: it cannot be started from here (this shell's MSYS2 runtime is
# already loaded, and a second copy refuses to start).
(cd "$out" && find . -type f ! -name SHA256SUMS | sort | xargs sha256sum >SHA256SUMS)
sha256sum "$package"
