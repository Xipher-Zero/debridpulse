#!/usr/bin/env bash
# Build DownloadPulse's per-user Windows x64 installer from an assembled layout
# (packaging/desktop/assemble.py run on Windows) with NSIS 3.11:
#   - MAKENSIS set (the hosted Windows build): that makensis, i.e. upstream's
#     NSIS 3.11 for Windows, verified against official.sha256 beside this
#     script, run natively on the runner that assembled the layout;
#   - otherwise: the pinned Debian base's nsis 3.11-1, in a container (podman);
#     nothing is installed on the host.
#   build-installer.sh <stage-dir> <prerequisites-dir> <out-dir> [extra makensis -D...]
# <prerequisites-dir> holds Microsoft's Evergreen MicrosoftEdgeWebview2Setup.exe
# bootstrapper and Microsoft's Visual C++ x64 Redistributable installer
# (vc_redist.x64.exe), each Microsoft's own file, unmodified -- the
# Redistributable as Distributable Code under the maintainer's Visual Studio
# licence (docs/DEPENDENCY_LICENSES.md). Their hashes are recorded in
# <out-dir>/PREREQUISITES.txt.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
stage="$(cd "$1" && pwd)"; prereq="$(cd "$2" && pwd)"; mkdir -p "$3"; out="$(cd "$3" && pwd)"; shift 3
version="$(tr -d '[:space:]' < "$here/../../../VERSION")"
mkdir -p "$stage/prerequisites"
cp "$prereq/MicrosoftEdgeWebview2Setup.exe" "$prereq/vc_redist.x64.exe" "$stage/prerequisites/"
(cd "$stage/prerequisites" && sha256sum MicrosoftEdgeWebview2Setup.exe vc_redist.x64.exe) > "$out/PREREQUISITES.txt"
if [ -n "${MAKENSIS:-}" ]; then
  native() { if command -v cygpath >/dev/null; then cygpath -w "$1"; else printf '%s' "$1"; fi; }
  echo "makensis $("$MAKENSIS" -VERSION)"
  "$MAKENSIS" -V2 -DVERSION="$version" -DSTAGE="$(native "$stage")" \
    -DOUTFILE="$(native "$out/DownloadPulse-$version-windows-x64.exe")" "$@" "$(native "$here/installer.nsi")"
else
podman run --rm -v "$here:/nsis:ro,Z" -v "$stage:/stage:ro,Z" -v "$out:/out:Z" \
  docker.io/library/python:3.12.14-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea \
  sh -c "apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends nsis >/dev/null \
         && makensis -V2 -DVERSION=$version -DSTAGE=/stage -DOUTFILE=/out/DownloadPulse-$version-windows-x64.exe $* /nsis/installer.nsi \
         && dpkg-query -W nsis"
fi
(cd "$out" && sha256sum "DownloadPulse-$version-windows-x64.exe" | tee -a SHA256SUMS)
