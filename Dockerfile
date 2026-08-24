FROM python:3.12.14-slim-trixie

WORKDIR /app

ARG APP_VERSION=unknown
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="DebridPulse — AllDebrid + aria2 Download Manager"
LABEL org.opencontainers.image.version="${APP_VERSION}"
LABEL org.opencontainers.image.description="AllDebrid-backed download manager for direct links, magnets, and torrent files via aria2"
LABEL org.opencontainers.image.source="https://github.com/Xipher-Zero/debridpulse"
LABEL org.opencontainers.image.revision="${VCS_REF}"
LABEL org.opencontainers.image.licenses="GPL-2.0-or-later"

# System deps + gosu (for PUID/PGID user-switching).
# Debian's RAR codec is in non-free and plugs into the 7zip `7z` binary.
# The slim base excludes most /usr/share/doc content, so explicitly re-include
# the 7zip-rar notices needed to ship its licensing terms with the image. The
# zz- prefix ensures these last-match-wins dpkg rules sort after the base image's
# docker filter configuration.
#
# Upgrade the base layer before installing application packages. A pinned Python
# slim tag can retain Debian packages that already have security fixes available;
# the runtime vulnerability gate must evaluate the current patched Trixie package
# set rather than the package snapshot baked into that base layer.
RUN printf '%s\n' \
      'path-include=/usr/share/doc/7zip-rar/copyright' \
      'path-include=/usr/share/doc/7zip-rar/unRarLicense.txt' \
      > /etc/dpkg/dpkg.cfg.d/zz-debridpulse-license-notices && \
    sed -ri 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
    aria2 \
    curl \
    gosu \
    7zip \
    7zip-rar && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App
COPY backend/ /app/
COPY frontend/ /app/frontend/
COPY CHANGELOG.md /app/CHANGELOG.md
COPY VERSION /app/VERSION
COPY LICENSE NOTICE SOURCE_OFFER.md /app/
COPY LICENSES/ /app/LICENSES/
COPY licenses/ /app/licenses/
COPY docs/DEPENDENCY_LICENSES.md /app/docs/DEPENDENCY_LICENSES.md

# Entrypoint (handles PUID/PGID + chown)
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Directories — owned by nobody:users (65534:100) by default
# Override at runtime via PUID / PGID environment variables
RUN mkdir -p /app/data /app/config /download && \
    chown -R 99:100 /app /download

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD curl -f http://localhost:8080/api/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
