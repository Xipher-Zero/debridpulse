#!/usr/bin/env bash
# DownloadPulse's aria2 for macOS (arm64): the same Debian aria2 source with
# Debian's patches and DownloadPulse's connect-tunnel-exact-read.patch (prepared,
# autoreconf'd, by packaging/macos-tools/prepare-sources.sh), configured without
# BitTorrent. TLS is Apple's own (AppleTLS, the system trust store); SFTP is
# Debian's libssh2 on Debian's OpenSSL libcrypto, the backend Debian's libssh2
# uses, so aria2 verifies the same host key types (ECDSA, Ed25519, RSA) as on
# Linux (libssh2's mbedTLS backend has no Ed25519); asynchronous DNS is Debian's
# c-ares -- all three static. zlib and expat are the system's. sqlite3 is not built in
# (aria2 uses it only to import a browser's cookie database, which DownloadPulse
# never does). Built natively with Apple's Command Line Tools.
# Usage: build-macos.sh <sources-dir> <work-dir> <out-dir>
set -euo pipefail
DP_SUFFIX=dp2
src="$1"; work="$2"; out="$3"
# Absolute paths: the recipe changes directory before it installs into them.
mkdir -p "$work" "$out"; src="$(cd "$src" && pwd)"; work="$(cd "$work" && pwd)"; out="$(cd "$out" && pwd)"
prefix="$work/prefix"
sdk="$(xcrun --show-sdk-path)"
jobs="$(sysctl -n hw.ncpu)"
mkdir -p "$work" "$out/bin" "$prefix/lib" "$prefix/include"
for archive in openssl-source c-ares-1.34.9 libssh2-1.11.1 aria2-macos-source; do tar -xzf "$src/$archive.tar.gz" -C "$work"; done

# libcrypto only, for libssh2: aria2 itself is built without OpenSSL (TLS is AppleTLS).
(cd "$work"/openssl-*/ && ./Configure darwin64-arm64-cc no-shared no-module no-apps no-tests no-docs \
   --prefix="$prefix" --libdir=lib >/dev/null && make -j"$jobs" build_libs >/dev/null && make install_dev >/dev/null)
(cd "$work/c-ares-1.34.9" && ./configure --disable-shared --enable-static --disable-tests --prefix="$prefix" >/dev/null \
   && make -j"$jobs" install >/dev/null)
(cd "$work/libssh2-1.11.1" && ./configure --disable-shared --enable-static --disable-examples-build --disable-tests \
   --with-crypto=openssl --with-libssl-prefix="$prefix" --prefix="$prefix" >/dev/null \
   && grep -q '^#define LIBSSH2_OPENSSL 1' src/libssh2_config.h && make -j"$jobs" install >/dev/null)

cd "$work"/aria2-*/
./configure --prefix="$prefix" --disable-bittorrent --disable-nls --without-included-gettext \
  --with-appletls --without-gnutls --without-openssl --without-libnettle --without-libgcrypt --without-libgmp \
  --without-sqlite3 --without-libxml2 --with-libexpat --with-libz --with-libcares --with-libssh2 \
  PKG_CONFIG=/usr/bin/false \
  LIBCARES_CFLAGS="-I$prefix/include -DCARES_STATICLIB" LIBCARES_LIBS="$prefix/lib/libcares.a -lresolv" \
  LIBSSH2_CFLAGS="-I$prefix/include" \
  LIBSSH2_LIBS="$prefix/lib/libssh2.a $prefix/lib/libcrypto.a" \
  ZLIB_CFLAGS="-I$sdk/usr/include" ZLIB_LIBS="-lz" EXPAT_CFLAGS="-I$sdk/usr/include" EXPAT_LIBS="-lexpat"
grep -q '^#define ENABLE_SSL 1' config.h && grep -q '^#define HAVE_LIBSSH2 1' config.h
if grep -q '^#define ENABLE_BITTORRENT 1' config.h; then echo "BitTorrent is configured" >&2; exit 1; fi
make -j"$jobs"
test -n "$(find src -name '*AppleTLSSession.o')"
test -z "$(find src \( -name '*LibgnutlsTLSSession.o' -o -name '*LibsslTLSSession.o' -o -name '*WinTLSSession.o' \))"
install -m 0755 src/aria2c "$out/bin/aria2c"; strip "$out/bin/aria2c"
printf 'aria2c: ' > "$out/links.txt"; otool -L "$out/bin/aria2c" | tail -n +2 | awk '{print $1}' | tr '\n' ' ' >> "$out/links.txt"; echo >> "$out/links.txt"
cat "$out/links.txt"
# The system's libraries and Apple frameworks only.
if tr ' ' '\n' < "$out/links.txt" | tail -n +2 | grep -vE '^$|^/usr/lib/lib(System\.B|z\.1|expat\.1|resolv\.9|c\+\+\.1|iconv\.2)\.dylib$|^/System/Library/Frameworks/(Security|CoreFoundation)\.framework/'; then
  echo "aria2c links an unexpected library" >&2; exit 1
fi
echo "built aria2 1.37.0+debian-3+${DP_SUFFIX} for macOS $(uname -m)"
