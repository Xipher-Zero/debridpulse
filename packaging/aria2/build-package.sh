#!/usr/bin/env bash
# THE one build of DebridPulse's aria2 (docs/SUPPLY_CHAIN_POLICY.md section 4a).
#
# Debian's own aria2 source package, fetched from the configured (signed)
# deb-src repository, rebuilt with exactly the repo-owned changes in this
# directory and nothing else:
#   - connect-tunnel-exact-read.patch   appended to debian/patches/series
#   - disable-bittorrent.rules.patch    applied to debian/rules (fuzz 0)
# and versioned <source version>+dp2, which sorts after the archive version it
# derives from. The aria2 package image (packaging/aria2/Dockerfile) and the
# hosted runtime test layer (.github/workflows/tests.yml) both run this script,
# so neither restates the patch set, the configuration or the version suffix.
#
# Usage: build-package.sh <work-dir> <out-dir>
#   ARIA2_SOURCE_VERSION  exact source version to fetch (default: the
#                         distribution's own candidate)
# Produces <out-dir>/packages/{aria2,libaria2-0}_<version>_<arch>.deb,
# <out-dir>/source/ (Debian source package + both DebridPulse patches) and
# <out-dir>/VERSION. Needs deb-src enabled and the build dependencies of the
# aria2 source package installed.
set -euo pipefail

DP_SUFFIX=dp2
here="$(cd "$(dirname "$0")" && pwd)"
work="$1"
out="$2"
mkdir -p "$work" "$out/packages" "$out/source"

cd "$work"
apt-get source --only-source "aria2${ARIA2_SOURCE_VERSION:+=$ARIA2_SOURCE_VERSION}"
tree="$(find "$work" -mindepth 1 -maxdepth 1 -type d -name 'aria2-*')"
test "$(printf '%s\n' "$tree" | wc -l)" -eq 1

cd "$tree"
source_version="$(dpkg-parsechangelog -SVersion)"
if [ -n "${ARIA2_SOURCE_VERSION:-}" ] && [ "$source_version" != "$ARIA2_SOURCE_VERSION" ]; then
  echo "fetched aria2 $source_version, expected $ARIA2_SOURCE_VERSION" >&2
  exit 1
fi
version="${source_version}+${DP_SUFFIX}"

mkdir -p debian/patches
cp "$here/connect-tunnel-exact-read.patch" debian/patches/
echo connect-tunnel-exact-read.patch >> debian/patches/series
patch -p1 --forward --fuzz=0 < "$here/disable-bittorrent.rules.patch"
grep -qx $'\t\t--disable-bittorrent' debian/rules

{
  printf 'aria2 (%s) %s; urgency=medium\n\n' "$version" "$(dpkg-parsechangelog -SDistribution)"
  printf '  * DebridPulse: read a CONNECT response no further than its header\n'
  printf '    (debian/patches/connect-tunnel-exact-read.patch).\n'
  printf '  * DebridPulse: build without BitTorrent (--disable-bittorrent).\n\n'
  printf ' -- DebridPulse <noreply@github.com>  %s\n\n' "$(date -R -u -d @0)"
  cat debian/changelog
} > debian/changelog.new
mv debian/changelog.new debian/changelog

DEB_BUILD_OPTIONS="nocheck parallel=$(nproc)" dpkg-buildpackage -b -uc -us

cp "../aria2_${version}_"*.deb "../libaria2-0_${version}_"*.deb "$out/packages/"
test "$(find "$out/packages" -name '*.deb' | wc -l)" -eq 2
# The exact corresponding source of the shipped binaries (GPL-2.0-or-later):
# Debian's signed source package as fetched, plus the two DebridPulse changes.
find "$work" -maxdepth 1 -type f \( -name 'aria2_*.dsc' -o -name 'aria2_*.tar.*' \) -exec cp {} "$out/source/" \;
cp "$here/connect-tunnel-exact-read.patch" "$here/disable-bittorrent.rules.patch" "$out/source/"
printf '%s\n' "$version" > "$out/VERSION"
echo "built aria2 $version"
