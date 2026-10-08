# DebridPulse's FFmpeg package image (docs/SUPPLY_CHAIN_POLICY.md section 4b).
#
# A package CARRIER, not an FFmpeg runtime: built once per architecture by the
# FFmpeg Package workflow and published write-once to
# ghcr.io/xipher-zero/debridpulse-ffmpeg, whose multi-arch manifest DIGEST the
# application Dockerfile consumes. It is rebuilt only when this directory
# changes (FFmpeg source version, build configuration or base/toolchain),
# never by an application build.
#
# Build context: the repository root.
#   docker buildx build -f packaging/ffmpeg/Dockerfile .
FROM python:3.12.14-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS build
ARG FFMPEG_SOURCE_VERSION=7:7.1.5-0+deb13u1
ARG FFMPEG_PACKAGE_VERSION=7:7.1.5-0+deb13u1+dp1
COPY packaging/ffmpeg/ /dp/ffmpeg/
# Build, then qualify the package as it will be installed: the exact version,
# its recorded Debian source, and the compiled capability set (local
# protocols only, no encoder, the finalization containers present).
RUN set -eux; \
    sed -ri 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends dpkg-dev build-essential pkg-config; \
    FFMPEG_SOURCE_VERSION="${FFMPEG_SOURCE_VERSION}" bash /dp/ffmpeg/build-package.sh /build /out; \
    test "$(cat /out/VERSION)" = "${FFMPEG_PACKAGE_VERSION}"; \
    apt-get install -y --no-install-recommends /out/packages/*.deb; \
    bash /dp/ffmpeg/verify-features.sh "${FFMPEG_PACKAGE_VERSION}"; \
    ffmpeg -hide_banner -version > /out/ffmpeg-version.txt; \
    cp /dp/ffmpeg/verify-features.sh /out/; \
    cd /out; find packages source -type f | sort | xargs sha256sum > SHA256SUMS

FROM scratch
ARG FFMPEG_PACKAGE_VERSION=7:7.1.5-0+deb13u1+dp1
LABEL org.opencontainers.image.title="DebridPulse FFmpeg packages"
LABEL org.opencontainers.image.description="Debian FFmpeg rebuilt for DebridPulse: local stream-copy tools only, no encoder, device or network protocol"
LABEL org.opencontainers.image.version="${FFMPEG_PACKAGE_VERSION}"
LABEL org.opencontainers.image.source="https://github.com/Xipher-Zero/debridpulse"
LABEL org.opencontainers.image.licenses="LGPL-2.1-or-later"
COPY --from=build /out/ /
