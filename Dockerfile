FROM python:3.12.14-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

WORKDIR /app

ARG APP_VERSION=unknown
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="DebridPulse: Universal Transfer Manager"
LABEL org.opencontainers.image.version="${APP_VERSION}"
LABEL org.opencontainers.image.description="Universal transfer orchestration with AllDebrid and General HTTP(S) providers plus aria2 execution"
LABEL org.opencontainers.image.source="https://github.com/Xipher-Zero/debridpulse"
LABEL org.opencontainers.image.revision="${VCS_REF}"
LABEL org.opencontainers.image.licenses="GPL-2.0-or-later"

# System deps + gosu (for PUID/PGID user-switching).
# Debian's RAR codec is in non-free and plugs into the 7zip `7z` binary.
# zstd is the exact outer decoder for .tar.zst/.tzst composite archives.
# The slim base excludes most /usr/share/doc content, so explicitly re-include
# the 7zip-rar notices needed to ship its licensing terms with the image. The
# zz- prefix ensures these last-match-wins dpkg rules sort after the base image's
# docker filter configuration.
#
# DP 1.0.12 leveling remediation (DEP-001): the base image is now pinned by
# verified multiarch manifest digest (see docs/SUPPLY_CHAIN_POLICY.md), so the
# base filesystem candidate is fixed at build time -- a blanket `apt-get
# upgrade` is deliberately NOT run here. Security freshness for the apt layer
# comes from deliberate base/package-pin refresh followed by full
# requalification of the resulting new image digest, not from silently
# floating package versions inside an otherwise-pinned build.
#
# Post-push exact-SHA Container Security qualification (docs/
# SUPPLY_CHAIN_POLICY.md section 4) found the pinned base digest above
# already carries fixable HIGH/CRITICAL CVEs in four base-layer packages this
# Dockerfile never explicitly installs (gzip CVE-2026-41992, libpcre2-8-0
# CVE-2026-86145/CVE-2026-89161, libsqlite3-0 CVE-2026-11822/CVE-2026-11824,
# perl-base CVE-2026-13221/CVE-2026-42496/CVE-2026-8376/CVE-2026-42497/
# CVE-2026-48962/CVE-2026-57432/CVE-2026-57433). Re-querying the registry
# confirmed no newer manifest-list digest is published for this tag yet, so
# this is a deliberate, NAMED, --only-upgrade of exactly those four packages
# to whatever the current apt snapshot serves -- not a blanket upgrade of the
# whole base layer, and not a version pin (Debian's repository is a moving
# target regardless; see policy section 2). If a future base-digest refresh
# already carries these fixes, this line becomes a no-op and should be
# dropped in that same change rather than carried forward indefinitely.
RUN printf '%s\n' \
      'path-include=/usr/share/doc/7zip-rar/copyright' \
      'path-include=/usr/share/doc/unrar/copyright' \
      'path-include=/usr/share/doc/7zip-rar/unRarLicense.txt' \
      > /etc/dpkg/dpkg.cfg.d/zz-debridpulse-license-notices && \
    sed -ri 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends --only-upgrade \
    gzip \
    libpcre2-8-0 \
    libsqlite3-0 \
    perl-base && \
    apt-get install -y --no-install-recommends \
    aria2 \
    curl \
    gosu \
    zstd \
    par2 \
    unrar \
    7zip \
    7zip-rar && \
    rm -rf /var/lib/apt/lists/*

# Python deps. DP 1.0.12 leveling remediation (DEP-001): requirements.txt is
# hash-pinned (pip-compile --generate-hashes); --require-hashes makes pip
# refuse to install anything whose downloaded artifact does not match one of
# the recorded hashes for every package in the closure, including transitive
# dependencies.
#
# This is the ONE Python install transaction in the image. The bundled Usenet
# acquisition service runs on this same interpreter, so its runtime closure is
# part of this lock (see the Usenet section of requirements.in) rather than a
# second install: a second transaction would escape --require-hashes and would
# be free to replace packages this one already selected.
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# `par2` performs the posting's verification/repair. `unrar` is present only
# because the service refuses to start acquiring without it ("Essential modules
# are missing"); it is never invoked for DebridPulse work, which is always
# submitted repair-only and runs with the service's own unpackers disabled --
# DebridPulse owns archive extraction.
# The Usenet acquisition service (SABnzbd) is bundled as a DebridPulse-private
# component: it binds loopback only, its port is never published, and its web
# application is never exposed or proxied. Operators configure Usenet, never
# this service. The version is pinned and its checksum verified.
# 5.1.3 is the version characterized and qualified for this release; the
# checksum is verified before anything is unpacked. Only the source tree is
# unpacked here -- its Python dependencies were already installed, hashed, by
# the single locked transaction above, so this step installs nothing.
ARG USENET_SERVICE_VERSION=5.1.3
ARG USENET_SERVICE_SHA256=12a01e30ce166297a375ffc3a761f98bf7d93260e040391497f643f8a3525fed
RUN set -eux; \
    curl -fsSL -o /tmp/usenet-service.tar.gz \
      "https://github.com/sabnzbd/sabnzbd/releases/download/${USENET_SERVICE_VERSION}/SABnzbd-${USENET_SERVICE_VERSION}-src.tar.gz"; \
    echo "${USENET_SERVICE_SHA256}  /tmp/usenet-service.tar.gz" | sha256sum -c -; \
    mkdir -p /app/usenet; \
    tar -xzf /tmp/usenet-service.tar.gz --strip-components=1 -C /app/usenet; \
    rm -f /tmp/usenet-service.tar.gz; \
    test -f /app/usenet/SABnzbd.py; \
    python -c "import sabctools, cheroot, cherrypy, feedparser, configobj, apprise, guessit, puremagic, portend, rarfile"

# App
COPY backend/ /app/
COPY frontend/ /app/frontend/
RUN python - <<'PY'
from base64 import b64decode
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from shutil import rmtree
from zipfile import ZipFile

parts = Path('/app/frontend/host-icons.parts')
encoded = ''.join(path.read_text(encoding='ascii') for path in sorted(parts.iterdir()))
archive_bytes = b64decode(encoded, validate=True)
expected_sha256 = '2bfb7cadf647f6d4093ce4ad7d13159e137a190925a1b840f8a50a7f579be90f'
if sha256(archive_bytes).hexdigest() != expected_sha256:
    raise RuntimeError('Host artwork archive checksum mismatch')

expected_names = {
    '1fichier.png', '4shared.png', 'alfafile.png', 'fastbit.png', 'file-upload.png',
    'fileal.png', 'filedot.png', 'filefactory.png', 'filespace.png', 'gigapeta.png',
    'hexupload.png', 'hitfile.png', 'isra-cloud.png', 'katfile.png', 'mediafire.png',
    'mega.svg', 'modsbase.png', 'mp4upload.png', 'prefiles.png', 'rapidgator.png',
    'scribd.png', 'sendit.png', 'simfileshare.png', 'streamtape.png', 'turbobit.png',
    'upload42.png', 'uploadhaven.png', 'uploadrar.png', 'world-files.png',
}
target = Path('/app/frontend/static/icons/hosts')
target.mkdir(parents=True, exist_ok=True)
with ZipFile(BytesIO(archive_bytes)) as package:
    names = {member.filename for member in package.infolist() if not member.is_dir()}
    if names != expected_names:
        raise RuntimeError('Unexpected host artwork archive contents')
    for member in package.infolist():
        if member.is_dir():
            continue
        name = Path(member.filename)
        if name.name != member.filename or name.suffix.lower() not in {'.png', '.svg'}:
            raise RuntimeError('Unexpected host artwork archive member')
        (target / name.name).write_bytes(package.read(member))
rmtree(parts)
PY
COPY CHANGELOG.md /app/CHANGELOG.md
COPY VERSION /app/VERSION
COPY LICENSE NOTICE SOURCE_OFFER.md /app/
COPY LICENSES/ /app/LICENSES/
COPY licenses/ /app/licenses/
COPY docs/DEPENDENCY_LICENSES.md /app/docs/DEPENDENCY_LICENSES.md

# Entrypoint (handles PUID/PGID + chown)
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Directories - owned by 99:100 by default
# Override at runtime via PUID / PGID environment variables
RUN mkdir -p /app/data /app/data/usenet /app/config /download && \
    chown -R 99:100 /app /download

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD curl -f http://localhost:8080/api/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
