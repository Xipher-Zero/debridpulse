# DebridPulse's aria2 package image (docs/SUPPLY_CHAIN_POLICY.md section 4a).
#
# A package CARRIER, not an aria2 runtime: built once per architecture by the
# aria2 Package workflow and published write-once to
# ghcr.io/xipher-zero/debridpulse-aria2, whose multi-arch manifest DIGEST the
# application Dockerfile consumes. It is rebuilt only when this directory
# changes (aria2 version, patch set, build configuration or base/toolchain),
# never by an application build.
#
# Build context: the repository root.
#   docker buildx build -f packaging/aria2/Dockerfile .
FROM python:3.12.14-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS build
ARG ARIA2_SOURCE_VERSION=1.37.0+debian-3
ARG ARIA2_PACKAGE_VERSION=1.37.0+debian-3+dp2
COPY packaging/aria2/ /dp/aria2/
# Build, then qualify the packages as they will be installed: the exact
# version and the compiled feature set (no BitTorrent; HTTPS, SFTP, Metalink).
RUN set -eux; \
    sed -ri 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends dpkg-dev build-essential fakeroot patch; \
    apt-get build-dep -y --no-install-recommends "aria2=${ARIA2_SOURCE_VERSION}"; \
    ARIA2_SOURCE_VERSION="${ARIA2_SOURCE_VERSION}" bash /dp/aria2/build-package.sh /build /out; \
    test "$(cat /out/VERSION)" = "${ARIA2_PACKAGE_VERSION}"; \
    apt-get install -y --no-install-recommends /out/packages/*.deb; \
    bash /dp/aria2/verify-features.sh "${ARIA2_PACKAGE_VERSION}"; \
    aria2c --version > /out/aria2c-version.txt; \
    cp /dp/aria2/verify-features.sh /out/; \
    cd /out; find packages source -type f | sort | xargs sha256sum > SHA256SUMS

FROM scratch
ARG ARIA2_PACKAGE_VERSION=1.37.0+debian-3+dp2
LABEL org.opencontainers.image.title="DebridPulse aria2 packages"
LABEL org.opencontainers.image.description="Debian aria2 rebuilt for DebridPulse: CONNECT exact-read patch, BitTorrent compiled out"
LABEL org.opencontainers.image.version="${ARIA2_PACKAGE_VERSION}"
LABEL org.opencontainers.image.source="https://github.com/Xipher-Zero/debridpulse"
LABEL org.opencontainers.image.licenses="GPL-2.0-or-later"
COPY --from=build /out/ /
