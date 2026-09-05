FROM python:3.12.14-slim-trixie

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
    zstd \
    7zip \
    7zip-rar && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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

# Directories - owned by nobody:users (65534:100) by default
# Override at runtime via PUID / PGID environment variables
RUN mkdir -p /app/data /app/config /download && \
    chown -R 99:100 /app /download

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD curl -f http://localhost:8080/api/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
