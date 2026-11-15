#!/usr/bin/env bash
# Build DebridPulse's per-user Windows x64 installer from an assembled layout
# (packaging/desktop/assemble.py run on Windows) with NSIS from the pinned
# Debian base (nsis 3.11-1), in a container; nothing is installed on the host.
#   build-installer.sh <stage-dir> <prerequisites-dir> <out-dir> [extra makensis -D...]
# <prerequisites-dir> holds Microsoft's own vc_redist.x64.exe (the "latest
# supported" https://aka.ms/vc14/vc_redist.x64.exe) and the Evergreen
# MicrosoftEdgeWebview2Setup.exe bootstrapper; their hashes and versions are
# recorded in <out-dir>/PREREQUISITES.txt.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
stage="$(cd "$1" && pwd)"; prereq="$(cd "$2" && pwd)"; mkdir -p "$3"; out="$(cd "$3" && pwd)"; shift 3
version="$(tr -d '[:space:]' < "$here/../../../VERSION")"
mkdir -p "$stage/prerequisites"
cp "$prereq/vc_redist.x64.exe" "$prereq/MicrosoftEdgeWebview2Setup.exe" "$stage/prerequisites/"
(cd "$stage/prerequisites" && sha256sum vc_redist.x64.exe MicrosoftEdgeWebview2Setup.exe) > "$out/PREREQUISITES.txt"
podman run --rm -v "$here:/nsis:ro,Z" -v "$stage:/stage:ro,Z" -v "$out:/out:Z" \
  docker.io/library/python:3.12.14-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea \
  sh -c "apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends nsis >/dev/null \
         && makensis -V2 -DVERSION=$version -DSTAGE=/stage -DOUTFILE=/out/DebridPulse-$version-windows-x64.exe $* /nsis/installer.nsi \
         && dpkg-query -W nsis"
(cd "$out" && sha256sum "DebridPulse-$version-windows-x64.exe" | tee -a SHA256SUMS)
