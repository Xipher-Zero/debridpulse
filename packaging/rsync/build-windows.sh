#!/usr/bin/bash
# DebridPulse's rsync for Windows x64: upstream rsync built by MSYS2's own
# maintained recipe (the PKGBUILD and patch pinned in official.sha256), with one
# configure choice: rsync_cv_HAVE_SOCKETPAIR=no, so rsync talks to its remote
# shell -- DebridPulse's native services/ssh_channel.py -- over real pipes. An
# MSYS2 socketpair() is an emulated socket a native Windows program cannot read.
#
# Runs inside an MSYS2 environment extracted from the pinned base archive into a
# build directory (MSYSTEM=MSYS, HOME inside that MSYS2 root), after
#   pacman -Syuu (twice) and
#   pacman -S --needed base-devel gcc autotools libiconv-devel libidn2-devel \
#     liblz4-devel libxxhash-devel libzstd-devel openssl-devel popt-devel
# Usage: build-windows.sh <recipe-dir holding the pinned PKGBUILD + patch [+ rsync-3.5.2.tar.gz]> <out-dir>
# Produces <out-dir>/usr/bin/rsync.exe with the MSYS2 runtime DLLs it loads,
# <out-dir>/etc/fstab (MSYS2's default mount table: drives at /c, /d, ...,
# binary, no POSIX ACL emulation), <out-dir>/tmp, PACKAGES (each DLL's owning
# MSYS2 package, for its license) and SHA256SUMS.
# Upstream's test suite (check()) is not run here: it needs hours under MSYS2.
set -euo pipefail
case "$HOME" in /home/*) ;; *) echo "HOME is not inside this MSYS2 root" >&2; exit 1 ;; esac
recipe="$1"; out="$2"
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

mkdir -p "$out/usr/bin" "$out/etc" "$out/tmp"
install -m 0755 "$root/usr/bin/rsync.exe" "$out/usr/bin/rsync.exe"
: >"$out/PACKAGES"
for dll in $(ldd "$root/usr/bin/rsync.exe" | awk '$3 ~ "^/usr/bin/" {print $1}'); do
  cp "/usr/bin/$dll" "$out/usr/bin/"
  printf '%s %s\n' "$dll" "$(pacman -Qo "/usr/bin/$dll" | sed 's/^.* is owned by //')" >>"$out/PACKAGES"
done
printf '%s\n' "# The rsync helper's mount table (MSYS2's default): drives are /c, /d, ..." \
  'none / cygdrive binary,posix=0,noacl,user 0 0' >"$out/etc/fstab"
# The helper is run outside MSYS2, from a plain Windows PATH, with its own
# runtime: it cannot be started from here (this shell's MSYS2 runtime is
# already loaded, and a second copy refuses to start).
(cd "$out" && find . -type f ! -name SHA256SUMS | sort | xargs sha256sum >SHA256SUMS)
sha256sum "$package"
