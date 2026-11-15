#!/usr/bin/env bash
# Build DebridPulse's per-user Windows x64 installer from an assembled layout
# (packaging/desktop/assemble.py run on Windows) with NSIS 3.11:
#   - MAKENSIS set (the hosted Windows build): that makensis, i.e. upstream's
#     NSIS 3.11 for Windows, verified against official.sha256 beside this
#     script, run natively on the runner that assembled the layout;
#   - otherwise: the pinned Debian base's nsis 3.11-1, in a container (podman);
#     nothing is installed on the host.
#   build-installer.sh <stage-dir> <prerequisites-dir> <out-dir> [extra makensis -D...]
# <prerequisites-dir> holds Microsoft's Evergreen MicrosoftEdgeWebview2Setup.exe
# bootstrapper, whose hash is recorded in <out-dir>/PREREQUISITES.txt. The
# Visual C++ Redistributable is not bundled (Microsoft limits redistributing it
# to Visual Studio licensees): the installer requires it and points the user
# to Microsoft's own download (installer.nsi).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
stage="$(cd "$1" && pwd)"; prereq="$(cd "$2" && pwd)"; mkdir -p "$3"; out="$(cd "$3" && pwd)"; shift 3
version="$(tr -d '[:space:]' < "$here/../../../VERSION")"
mkdir -p "$stage/prerequisites"
rm -f "$stage/prerequisites/vc_redist.x64.exe"
cp "$prereq/MicrosoftEdgeWebview2Setup.exe" "$stage/prerequisites/"
(cd "$stage/prerequisites" && sha256sum MicrosoftEdgeWebview2Setup.exe) > "$out/PREREQUISITES.txt"
if [ -n "${MAKENSIS:-}" ]; then
  native() { if command -v cygpath >/dev/null; then cygpath -w "$1"; else printf '%s' "$1"; fi; }
  echo "makensis $("$MAKENSIS" -VERSION)"
  "$MAKENSIS" -V2 -DVERSION="$version" -DSTAGE="$(native "$stage")" \
    -DOUTFILE="$(native "$out/DebridPulse-$version-windows-x64.exe")" "$@" "$(native "$here/installer.nsi")"
else
podman run --rm -v "$here:/nsis:ro,Z" -v "$stage:/stage:ro,Z" -v "$out:/out:Z" \
  docker.io/library/python:3.12.14-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea \
  sh -c "apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends nsis >/dev/null \
         && makensis -V2 -DVERSION=$version -DSTAGE=/stage -DOUTFILE=/out/DebridPulse-$version-windows-x64.exe $* /nsis/installer.nsi \
         && dpkg-query -W nsis"
fi
(cd "$out" && sha256sum "DebridPulse-$version-windows-x64.exe" | tee -a SHA256SUMS)
