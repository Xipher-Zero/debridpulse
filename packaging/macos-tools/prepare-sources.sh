#!/usr/bin/env bash
# The macOS builds' sources, prepared where Debian's signed source packages can
# be fetched and unpacked (a container of the packaging recipes' pinned Debian
# base) for a Mac that has neither apt nor autotools:
#   Debian's ffmpeg, c-ares, libssh2, openssl, par2cmdline and libzstd source
#   packages, unpacked with Debian's patches applied; Debian's aria2 source with
#   DownloadPulse's connect-tunnel-exact-read.patch, autoreconf'd; plus the
#   official downloads pinned by official.sha256. Each tree is one tarball.
# Usage (repository root):
#   podman run --rm -v "$PWD/packaging:/dp:ro" -v <out-dir>:/out <pinned base image> \
#     bash /dp/macos-tools/prepare-sources.sh /work /out
set -euo pipefail
work="$1"; out="$2"
# Absolute paths: the recipe changes directory before it writes into them.
mkdir -p "$work" "$out"; work="$(cd "$work" && pwd)"; out="$(cd "$out" && pwd)"
sed -ri 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends dpkg-dev patch curl ca-certificates \
  autoconf automake autopoint libtool gettext pkg-config >/dev/null
mkdir -p "$work" "$out"; cd "$work"
for source in ffmpeg=7:7.1.5-0+deb13u1 c-ares=1.34.9-1~deb13u1 libssh2=1.11.1-1+deb13u2 openssl=3.5.7-1~deb13u3 \
              par2cmdline=0.8.1-3 libzstd=1.5.7+dfsg-1 aria2=1.37.0+debian-3; do
  apt-get source --only-source -qq "$source" >/dev/null
done
(cd aria2-*/ && patch -p1 --forward --fuzz=0 < /dp/aria2/connect-tunnel-exact-read.patch >/dev/null && autoreconf -fi >/dev/null 2>&1)
tar -czf "$out/aria2-macos-source.tar.gz" aria2-*/
tar -czf "$out/openssl-source.tar.gz" openssl-*/
for tree in ffmpeg-* c-ares-* libssh2-* par2cmdline-* libzstd-*; do tar -czf "$out/$tree.tar.gz" "$tree"; done
cp ./*.dsc "$out/"
# RARLAB's RAR package is a trial distribution of RAR, whose licence permits
# passing on only its UnRAR components separately: it is never prepared here
# (this output is cached and handed over as source); build-macos.sh fetches it
# on the build machine and takes UnRAR from it.
grep -vE '^(#|$)' /dp/macos-tools/official.sha256 | while read -r digest name url; do
  case "$name" in rar*) continue ;; esac
  curl -sSfL -o "$out/$name" "$url"; echo "$digest  $out/$name" | sha256sum -c --quiet
done
(cd "$out" && sha256sum ./*.tar.* ./*.dsc > SHA256SUMS)
