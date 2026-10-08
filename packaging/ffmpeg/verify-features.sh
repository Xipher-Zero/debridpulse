#!/usr/bin/env bash
# THE one capability invariant of DebridPulse's FFmpeg, checked against the
# INSTALLED binaries (FFmpeg reports its own components), so the build command
# is never merely trusted:
#   - the package is exactly the expected version of Debian's `ffmpeg` source;
#   - only the local file and pipe protocols exist, for input and output;
#   - no encoder exists (finalization is stream copy only);
#   - the containers, subtitle formats and bitstream filter Media Downloads'
#     finalization relies on are present;
#   - the build is LGPL (nothing GPL enabled) and links nothing beyond libc/libm.
# Usage: verify-features.sh <expected package version>
set -euo pipefail

expected="$1"
installed="$(dpkg-query -W -f='${Version}' ffmpeg)"
if [ "$installed" != "$expected" ]; then
  echo "ffmpeg is $installed, expected $expected" >&2
  exit 1
fi
source="$(dpkg-query -W -f='${source:Package} ${source:Version}' ffmpeg)"
if [ "$source" != "ffmpeg ${expected%+dp*}" ]; then
  echo "ffmpeg records source '$source', expected 'ffmpeg ${expected%+dp*}'" >&2
  exit 1
fi

# The names listed under Input: and under Output:, each on one line.
protocols="$(ffmpeg -hide_banner -protocols | awk '/^Input:/{side="in"; next} /^Output:/{side="out"; next}
  side && NF {list[side]=list[side] " " $1} END{print list["in"] "|" list["out"]}')"
if [ "$protocols" != " file pipe| file pipe" ]; then
  echo "ffmpeg protocols are not exactly file and pipe: $protocols" >&2
  exit 1
fi

encoders="$(ffmpeg -hide_banner -encoders | awk 'started && NF {print $2} /^ *------/{started=1}')"
if [ -n "$encoders" ]; then
  echo "ffmpeg has encoders: $encoders" >&2
  exit 1
fi

has() {  # has <kind> <name>: the name is one of the binary's own components
  ffmpeg -hide_banner "-$1" | awk -v n="$2" '$2 == n || $2 ~ "(^|,)" n "(,|$)" {found=1} END{exit !found}'
}
for demuxer in mov matroska mpegts flv mp3 ogg flac aac webvtt srt ass; do
  has demuxers "$demuxer" || { echo "ffmpeg lacks the $demuxer demuxer" >&2; exit 1; }
done
for muxer in mp4 ipod mov webm matroska mp3 ogg opus flac; do
  has muxers "$muxer" || { echo "ffmpeg lacks the $muxer muxer" >&2; exit 1; }
done
ffmpeg -hide_banner -bsfs | grep -qx aac_adtstoasc || { echo "ffmpeg lacks aac_adtstoasc" >&2; exit 1; }

ffmpeg -hide_banner -L | grep -q 'GNU Lesser General Public' || { echo "ffmpeg is not an LGPL build" >&2; exit 1; }
for binary in /usr/bin/ffmpeg /usr/bin/ffprobe; do
  extra="$(ldd "$binary" | awk '{print $1}' | grep -vE '^(linux-vdso|linux-gate)\.so|^/lib.*/ld-linux|^libc\.so|^libm\.so' || true)"
  if [ -n "$extra" ]; then
    echo "$binary links more than libc/libm: $extra" >&2
    exit 1
  fi
done
ffprobe -hide_banner -version >/dev/null
echo "ffmpeg $expected capability set verified"
