#!/usr/bin/env bash
# DownloadPulse's aria2 for Windows x64 (docs/SUPPLY_CHAIN_POLICY.md section 4a):
# the same Debian aria2 source package, with Debian's patches and DownloadPulse's
# connect-tunnel-exact-read.patch, configured without BitTorrent, as upstream's
# own mingw-config builds it: TLS is Windows' own (WinTLS/SChannel, the system
# certificate store); SFTP is libssh2 on OpenSSL's libcrypto, the backend
# Debian's libssh2 uses, so aria2 verifies the same host key types (ECDSA,
# Ed25519, RSA) as on Linux -- libssh2's Windows CNG backend has no Ed25519 and,
# by default, no ECDSA, and would negotiate an RSA key DownloadPulse's evidence
# never confirmed (refused: "Unexpected SSH host key"). Every library is
# Debian's signed source package built static with Debian's mingw-w64
# toolchain -- so aria2c.exe needs only DLLs every Windows has. Not built in:
# sqlite3, which aria2 uses only to import a browser's cookie database
# (--load-cookies of a Firefox/Chromium file), which DownloadPulse never does.
#
# Usage: build-windows.sh <work-dir> <out-dir>
#   ARIA2_SOURCE_VERSION  exact aria2 source version to fetch (as build-package.sh)
# Produces <out-dir>/bin/aria2c.exe, <out-dir>/source/ (every Debian source
# package as fetched + both DownloadPulse aria2 files + this script), the
# copyright files, the executable's imported DLLs (imports.txt) and VERSION.
set -euo pipefail

DP_SUFFIX=dp2
HOST=x86_64-w64-mingw32
here="$(cd "$(dirname "$0")" && pwd)"
work="$1"
out="$2"
prefix="$work/prefix"
build="$(dpkg-architecture -qDEB_BUILD_GNU_TYPE)"
mkdir -p "$work" "$out/bin" "$out/source" "$out/copyright" "$prefix"
static=(--disable-shared --enable-static --prefix="$prefix" --host="$HOST" --build="$build")

fetch() {  # fetch <source package>: Debian's signed source, unpacked; prints its tree
  (cd "$work" && apt-get source --only-source -qq "$1" >/dev/null)
  find "$work" -mindepth 1 -maxdepth 1 -type d -name "$2" | head -1
}

tree="$(fetch zlib 'zlib-*')"
(cd "$tree" && CC=$HOST-gcc AR=$HOST-ar RANLIB=$HOST-ranlib ./configure --prefix="$prefix" --static \
   && make -j"$(nproc)" install)
cp "$tree/debian/copyright" "$out/copyright/zlib"

tree="$(fetch expat 'expat-*')"
(cd "$tree"/expat 2>/dev/null || cd "$tree"; ./buildconf.sh >/dev/null && ./configure "${static[@]}" --without-docbook \
   --without-examples --without-tests && make -j"$(nproc)" install)
cp "$tree/debian/copyright" "$out/copyright/expat"

tree="$(fetch c-ares 'c-ares-*')"
(cd "$tree" && ./configure "${static[@]}" --disable-tests LIBS="-lws2_32" && make -j"$(nproc)" install)
cp "$tree/debian/copyright" "$out/copyright/c-ares"

# libcrypto only, for libssh2: aria2 itself is built without OpenSSL (TLS is WinTLS).
tree="$(fetch openssl 'openssl-*')"
(cd "$tree" && ./Configure mingw64 no-shared no-module no-apps no-tests no-docs \
   --cross-compile-prefix="$HOST-" --prefix="$prefix" --libdir=lib >/dev/null \
   && make -j"$(nproc)" build_libs >/dev/null && make install_dev >/dev/null)
cp "$tree/debian/copyright" "$out/copyright/openssl"

tree="$(fetch libssh2 'libssh2-*')"
(cd "$tree" && ./configure "${static[@]}" --with-crypto=openssl --with-libssl-prefix="$prefix" \
   --disable-examples-build --disable-tests LIBS="-lws2_32 -lcrypt32 -lbcrypt" && make -j"$(nproc)" install)
grep -q '^#define LIBSSH2_OPENSSL 1' "$tree/src/libssh2_config.h"   # OpenSSL's libcrypto, as Debian's libssh2
cp "$tree/debian/copyright" "$out/copyright/libssh2"

tree="$(fetch "aria2${ARIA2_SOURCE_VERSION:+=$ARIA2_SOURCE_VERSION}" 'aria2-*')"
cd "$tree"
source_version="$(dpkg-parsechangelog -SVersion)"
if [ -n "${ARIA2_SOURCE_VERSION:-}" ] && [ "$source_version" != "$ARIA2_SOURCE_VERSION" ]; then
  echo "fetched aria2 $source_version, expected $ARIA2_SOURCE_VERSION" >&2
  exit 1
fi
patch -p1 --forward --fuzz=0 < "$here/connect-tunnel-exact-read.patch"
autoreconf -fi >/dev/null
./configure --host="$HOST" --build="$build" --prefix="$prefix" \
  --disable-bittorrent --disable-nls --without-included-gettext \
  --without-gnutls --without-openssl --without-libnettle --without-libgcrypt --without-libgmp \
  --without-sqlite3 --without-libxml2 --with-libexpat --with-libz --with-libcares --with-libssh2 \
  ARIA2_STATIC=yes CPPFLAGS="-I$prefix/include" LDFLAGS="-L$prefix/lib" \
  LIBSSH2_CFLAGS="-I$prefix/include" LIBSSH2_LIBS="-L$prefix/lib -lssh2 -lcrypto -lbcrypt -lcrypt32 -lws2_32 -luser32 -ladvapi32 -lz" \
  PKG_CONFIG=/usr/bin/pkg-config PKG_CONFIG_PATH="$prefix/lib/pkgconfig" PKG_CONFIG_LIBDIR="$prefix/lib/pkgconfig"
# SSL and SFTP are on, BitTorrent off.
grep -q '^#define ENABLE_SSL 1' config.h && grep -q '^#define HAVE_LIBSSH2 1' config.h
if grep -q '^#define ENABLE_BITTORRENT 1' config.h; then echo "BitTorrent is configured" >&2; exit 1; fi
make -j"$(nproc)"
# The TLS session compiled in is WinTLS's, and no other.
test -n "$(find src -name '*WinTLSSession.o')"
test -z "$(find src \( -name '*LibgnutlsTLSSession.o' -o -name '*LibsslTLSSession.o' -o -name '*AppleTLSSession.o' \))"
install -m 0755 src/aria2c.exe "$out/bin/aria2c.exe"
$HOST-strip --strip-unneeded "$out/bin/aria2c.exe"
printf 'aria2c.exe: ' > "$out/imports.txt"
$HOST-objdump -p "$out/bin/aria2c.exe" | sed -n 's/^\s*DLL Name: //p' | tr '\n' ' ' >> "$out/imports.txt"
echo >> "$out/imports.txt"
cat "$out/imports.txt"
if grep -viE '^aria2c\.exe:( (KERNEL32|USER32|ADVAPI32|SHELL32|OLE32|BCRYPT|CRYPT32|SECUR32|msvcrt|WS2_32|WSOCK32|IPHLPAPI|SHLWAPI|api-ms-win-[a-z0-9-]+)\.dll)+ ?$' "$out/imports.txt"; then
  echo "aria2c.exe imports a non-system DLL" >&2
  exit 1
fi
cp debian/copyright "$out/copyright/aria2"
find "$work" -maxdepth 1 -type f \( -name '*.dsc' -o -name '*.tar.*' \) -exec cp {} "$out/source/" \;
cp "$here/connect-tunnel-exact-read.patch" "$here/build-windows.sh" "$out/source/"
printf '%s\n' "${source_version}+${DP_SUFFIX}" > "$out/VERSION"
echo "built aria2 ${source_version}+${DP_SUFFIX} for Windows x64"
