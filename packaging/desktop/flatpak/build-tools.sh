#!/usr/bin/env bash
# DownloadPulse's native helper tools for the Linux Flatpak (x86_64), built by
# flatpak-builder inside the GNOME SDK against the runtime the application
# runs on, from the same inputs as the macOS recipes:
#   aria2 -- Debian's aria2 source with Debian's patches and DownloadPulse's
#     connect-tunnel-exact-read.patch (prepared, autoreconf'd, by
#     packaging/macos-tools/prepare-sources.sh), configured without BitTorrent
#     and, as Debian's own build (packaging/aria2), with GnuTLS and the
#     system CA bundle; SFTP is Debian's libssh2 on Debian's OpenSSL libcrypto
#     (the backend Debian's libssh2 uses), asynchronous DNS Debian's c-ares --
#     those three static; zlib, expat and GnuTLS are the runtime's;
#   FFmpeg -- Debian's source with packaging/ffmpeg/build-package.sh's one
#     configure line (read from it, never restated);
#   par2 (par2cmdline) and zstd -- Debian's source packages;
#   GNU rsync 3.5.2 -- upstream's source (pinned in macos-tools/official.sha256);
#   7-Zip 7zz and RARLAB unrar -- the official Linux releases, unmodified
#     (pinned by sha256 in the Flatpak manifest).
# Usage (flatpak-builder's build directory, holding those inputs):
#   build-tools.sh <install-dir>
set -euo pipefail
out="$1"; work="$PWD/work"; prefix="$work/prefix"; jobs="$(nproc)"
mkdir -p "$work" "$prefix/lib" "$prefix/include" "$out/copyright"
for archive in openssl-source c-ares-1.34.9 libssh2-1.11.1 aria2-macos-source ffmpeg-7.1.5 \
               par2cmdline-0.8.1 libzstd-1.5.7+dfsg rsync-3.5.2; do
  tar --no-same-owner -xzf "$archive.tar.gz" -C "$work"
done

# aria2: libcrypto only (for libssh2), c-ares and libssh2, all static.
(cd "$work"/openssl-*/ && ./Configure linux-x86_64 no-shared no-module no-apps no-tests no-docs \
   --prefix="$prefix" --libdir=lib >/dev/null && make -j"$jobs" build_libs >/dev/null && make install_dev >/dev/null)
(cd "$work/c-ares-1.34.9" && ./configure --disable-shared --enable-static --disable-tests --prefix="$prefix" >/dev/null \
   && make -j"$jobs" install >/dev/null)
(cd "$work/libssh2-1.11.1" && ./configure --disable-shared --enable-static --disable-examples-build --disable-tests \
   --with-crypto=openssl --with-libssl-prefix="$prefix" --prefix="$prefix" >/dev/null \
   && grep -q '^#define LIBSSH2_OPENSSL 1' src/libssh2_config.h && make -j"$jobs" install >/dev/null)
(cd "$work"/aria2-*/
 ./configure --prefix=/app --disable-bittorrent --disable-nls --without-included-gettext \
   --with-gnutls --without-openssl --without-libnettle --without-libgcrypt --without-libgmp \
   --with-ca-bundle=/etc/ssl/certs/ca-certificates.crt \
   --without-sqlite3 --without-libxml2 --with-libexpat --with-libz --with-libcares --with-libssh2 \
   LIBCARES_CFLAGS="-I$prefix/include -DCARES_STATICLIB" LIBCARES_LIBS="$prefix/lib/libcares.a" \
   LIBSSH2_CFLAGS="-I$prefix/include" LIBSSH2_LIBS="$prefix/lib/libssh2.a $prefix/lib/libcrypto.a -ldl -pthread" >/dev/null
 grep -q '^#define ENABLE_SSL 1' config.h && grep -q '^#define HAVE_LIBSSH2 1' config.h && grep -q '^#define HAVE_LIBGNUTLS 1' config.h
 if grep -q '^#define ENABLE_BITTORRENT 1' config.h; then echo "BitTorrent is configured" >&2; exit 1; fi
 make -j"$jobs" >/dev/null
 install -m 0755 src/aria2c "$out/aria2c"; strip "$out/aria2c"; cp debian/copyright "$out/copyright/aria2")
# The notices of what is built into aria2c statically.
cp "$work"/openssl-*/debian/copyright "$out/copyright/openssl"
cp "$work/c-ares-1.34.9/debian/copyright" "$out/copyright/c-ares"
cp "$work/libssh2-1.11.1/debian/copyright" "$out/copyright/libssh2"
features="$("$out/aria2c" --version | sed -n 's/^Enabled Features: //p')"
echo "aria2c enabled features: $features"
case ", $features," in *", BitTorrent,"*) echo "aria2c was built with BitTorrent" >&2; exit 1 ;; esac
for feature in HTTPS SFTP Metalink; do
  case ", $features," in *", $feature,"*) ;; *) echo "aria2c lacks $feature" >&2; exit 1 ;; esac
done

# FFmpeg: build-package.sh's capability set (the options of its one configure
# line after --prefix and --extra-version).
(cd "$work/ffmpeg-7.1.5"
 options=()
 while IFS= read -r option; do options+=("$option"); done < <(
   sed -n '/^\.\/configure --prefix=\/usr /,/[^\\]$/p' "$OLDPWD/build-package.sh" | tr -d '\\' | tr -s ' \n' '\n' | tail -n +4)
 case " ${options[*]} " in *" --disable-everything "*" --disable-network "*) ;; *)
   echo "could not read the capability set from build-package.sh" >&2; exit 1 ;; esac
 ./configure --cc=gcc --extra-version="0+deb13u1+dp1" "${options[@]}" >/dev/null
 grep -qx '#define CONFIG_GPL 0' config.h && grep -qx '#define CONFIG_NETWORK 0' config.h
 make -j"$jobs" >/dev/null
 for name in ffmpeg ffprobe; do install -m 0755 "$name" "$out/$name"; strip "$out/$name"; done
 cp debian/copyright "$out/copyright/ffmpeg")

(cd "$work/par2cmdline-0.8.1" && ./configure --disable-openmp >/dev/null && make -j"$jobs" par2 >/dev/null)
install -m 0755 "$work/par2cmdline-0.8.1/par2" "$out/par2"; cp "$work/par2cmdline-0.8.1/debian/copyright" "$out/copyright/par2"
make -C "$work/libzstd-1.5.7+dfsg/programs" -j"$jobs" zstd HAVE_ZLIB=0 HAVE_LZMA=0 HAVE_LZ4=0 >/dev/null
install -m 0755 "$work/libzstd-1.5.7+dfsg/programs/zstd" "$out/zstd"; cp "$work/libzstd-1.5.7+dfsg/debian/copyright" "$out/copyright/zstd"
(cd "$work/rsync-3.5.2" && ./configure --disable-xxhash --disable-zstd --disable-lz4 --disable-openssl --disable-md2man --disable-idn \
   --with-included-popt --with-included-zlib >/dev/null && make -j"$jobs" rsync >/dev/null)
install -m 0755 "$work/rsync-3.5.2/rsync" "$out/rsync"; cp "$work/rsync-3.5.2/COPYING" "$out/copyright/rsync"
mkdir -p "$work/7zip" && tar --no-same-owner -xJf 7z2501-linux-x64.tar.xz -C "$work/7zip" 7zz License.txt
install -m 0755 "$work/7zip/7zz" "$out/7zz"; cp "$work/7zip/License.txt" "$out/copyright/7-zip"
tar --no-same-owner -xzf rarlinux-x64-723.tar.gz -C "$work" rar/unrar rar/license.txt rar/acknow.txt
install -m 0755 "$work/rar/unrar" "$out/unrar"; cp "$work/rar/license.txt" "$out/copyright/unrar"
cp "$work/rar/acknow.txt" "$out/copyright/unrar-acknowledgments"

# Every executable links only the runtime's own libraries.
for exe in aria2c ffmpeg ffprobe par2 zstd rsync 7zz unrar; do
  printf '%s: ' "$exe"; ldd "$out/$exe" | awk '{print ($3 ~ /^\// ? $3 : $1)}' | tr '\n' ' '; echo
done | tee "$out/links.txt"
if ldd "$out"/* 2>/dev/null | grep -q 'not found'; then echo "a tool needs a library the runtime lacks" >&2; exit 1; fi
if tr ' ' '\n' < "$out/links.txt" | grep -E '^/' | grep -vE '^/(usr/)?lib(64|/x86_64-linux-gnu)?/'; then
  echo "a tool links a library outside the runtime" >&2; exit 1
fi
(cd "$out" && sha256sum aria2c ffmpeg ffprobe par2 zstd rsync 7zz unrar > SHA256SUMS && cat SHA256SUMS)
"$out/rsync" --version | head -1
