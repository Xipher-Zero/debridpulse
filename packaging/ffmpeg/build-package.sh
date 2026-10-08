#!/usr/bin/env bash
# THE one build of DebridPulse's FFmpeg (docs/SUPPLY_CHAIN_POLICY.md section 4b).
#
# Debian's own ffmpeg source package -- Debian's patches included -- fetched
# from the configured (signed) deb-src repository and configured for exactly
# what Media Downloads' lossless finalization does: read local component files
# and write one container by stream copy. Compiled in, and nothing else:
#   protocols    file and pipe only; no network protocol exists in the binary
#   demuxers     the containers and subtitle formats yt-dlp's native
#                downloaders produce (MP4/M4A/MOV, WebM/Matroska, MPEG-TS,
#                FLV, MP3, Ogg/Opus, FLAC, ADTS AAC, AC-3/E-AC-3, WAV, SRT,
#                WebVTT, ASS)
#   muxers       the planned final containers (MP4, M4A, MOV, WebM,
#                Matroska, MP3, Ogg, Opus, FLAC) and the null sink
#   parsers      the stream families' parsers, which fill codec parameters
#   bsfs         every bitstream filter (ADTS-to-MP4 AAC and friends)
#   decoders     the native decoders of those stream families, so probing a
#                component never depends on what a parser alone can tell
# No encoder, device, GPU, network or external library; ffplay is not built.
# The result is LGPL-2.1-or-later (nothing GPL is enabled).
#
# Packaged as Debian's binary package name `ffmpeg`, version
# <source version>+dpN, recording `Source: ffmpeg (<source version>)`, so dpkg,
# the SBOM and vulnerability scanning keep resolving it as Debian's FFmpeg.
#
# Usage: build-package.sh <work-dir> <out-dir>
#   FFMPEG_SOURCE_VERSION  exact source version to fetch (default: the
#                          distribution's own candidate)
# Produces <out-dir>/packages/ffmpeg_<version without epoch>_<arch>.deb,
# <out-dir>/source/ (Debian's source package as fetched + this script) and
# <out-dir>/VERSION. Needs deb-src enabled and a C toolchain (build-essential,
# pkg-config, dpkg-dev).
set -euo pipefail

DP_SUFFIX=dp1
here="$(cd "$(dirname "$0")" && pwd)"
work="$1"
out="$2"
mkdir -p "$work" "$out/packages" "$out/source"

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
version="${source_version}+${DP_SUFFIX}"
export SOURCE_DATE_EPOCH="$(dpkg-parsechangelog -STimestamp)"

./configure --prefix=/usr --extra-version="${source_version##*-}+${DP_SUFFIX}" \
  --disable-everything --disable-autodetect --disable-network \
  --disable-doc --disable-debug --disable-ffplay --disable-avdevice --disable-x86asm --enable-small \
  --enable-protocol=file,pipe \
  --enable-demuxer=mov,matroska,mpegts,flv,mp3,ogg,flac,aac,ac3,eac3,wav,webvtt,srt,ass \
  --enable-muxer=mp4,ipod,mov,webm,matroska,mp3,ogg,opus,flac,null \
  --enable-parser=h264,hevc,aac,aac_latm,av1,vp8,vp9,mpeg4video,opus,vorbis,flac,mpegaudio,ac3 \
  --enable-bsfs \
  --enable-decoder=h264,hevc,vp8,vp9,av1,mpeg4,aac,aac_latm,mp3,opus,vorbis,flac,ac3,eac3,subrip,webvtt,ass,mov_text
grep -qx '#define CONFIG_GPL 0' config.h && grep -qx '#define CONFIG_NETWORK 0' config.h
make -j"$(nproc)"

stage="$work/stage"
rm -rf "$stage"
install -D -m 0755 ffmpeg "$stage/usr/bin/ffmpeg"
install -D -m 0755 ffprobe "$stage/usr/bin/ffprobe"
strip --strip-unneeded "$stage/usr/bin/ffmpeg" "$stage/usr/bin/ffprobe"
install -D -m 0644 debian/copyright "$stage/usr/share/doc/ffmpeg/copyright"
{
  printf 'ffmpeg (%s) %s; urgency=medium\n\n' "$version" "$(dpkg-parsechangelog -SDistribution)"
  printf '  * DebridPulse: stream-copy tools only (local file/pipe protocols, the\n'
  printf '    demuxers, muxers, parsers, bitstream filters and decoders of\n'
  printf '    packaging/ffmpeg/build-package.sh); no encoder, device, network\n'
  printf '    protocol or external library; ffplay not built.\n\n'
  printf ' -- DebridPulse <noreply@github.com>  %s\n\n' "$(date -R -u -d "@$SOURCE_DATE_EPOCH")"
  cat debian/changelog
} | gzip -9n > "$stage/usr/share/doc/ffmpeg/changelog.Debian.gz"
chmod 0644 "$stage/usr/share/doc/ffmpeg/changelog.Debian.gz"

# Runtime dependencies are whatever the binaries actually link (libc, libm),
# computed by dpkg-shlibdeps exactly as a Debian build computes them.
shlibs="$work/shlibs"
mkdir -p "$shlibs/debian"
printf 'Source: ffmpeg\n\nPackage: ffmpeg\nArchitecture: any\n' > "$shlibs/debian/control"
depends="$(cd "$shlibs" && dpkg-shlibdeps -O "$stage/usr/bin/ffmpeg" "$stage/usr/bin/ffprobe" \
  | sed -n 's/^shlibs:Depends=//p')"
test -n "$depends"
arch="$(dpkg --print-architecture)"
mkdir -p "$stage/DEBIAN"
cat > "$stage/DEBIAN/control" <<EOF
Package: ffmpeg
Source: ffmpeg (${source_version})
Version: ${version}
Architecture: ${arch}
Maintainer: DebridPulse <noreply@github.com>
Installed-Size: $(du -sk --exclude=DEBIAN "$stage" | cut -f1)
Depends: ${depends}
Section: video
Priority: optional
Homepage: https://ffmpeg.org/
Description: FFmpeg stream-copy tools for DebridPulse
 Debian's FFmpeg source built with only what lossless remuxing of local
 media needs: local file and pipe protocols, container demuxers and muxers,
 parsers, bitstream filters and native decoders. No encoder, device, network
 protocol or external library is compiled in, and ffplay is not built.
EOF
deb="$out/packages/ffmpeg_${version#*:}_${arch}.deb"
dpkg-deb --root-owner-group -Zxz --build "$stage" "$deb"
test "$(find "$out/packages" -name '*.deb' | wc -l)" -eq 1

# The exact corresponding source of the shipped binaries (LGPL-2.1-or-later):
# Debian's signed source package as fetched, plus this build script.
find "$work" -maxdepth 1 -type f \( -name 'ffmpeg_*.dsc' -o -name 'ffmpeg_*.tar.*' \) -exec cp {} "$out/source/" \;
cp "$here/build-package.sh" "$out/source/"
printf '%s\n' "$version" > "$out/VERSION"
echo "built ffmpeg $version"
