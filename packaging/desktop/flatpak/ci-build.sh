#!/usr/bin/env bash
# The Flatpak, built and checked as the Desktop Artifacts workflow does, in the
# packaging recipes' pinned Debian base (flatpak and flatpak-builder are its own
# packages; privileged, because flatpak-builder sandboxes each build step):
#   docker run --rm --privileged -v "$PWD:/src" -w /src -e VERSION=<version> <pinned base> \
#     bash packaging/desktop/flatpak/ci-build.sh
# Inputs (repository root): build/layout.tar.gz (assemble.py --usenet on Linux
# x86_64), build/flatpak-sources/ (prepare-sources.sh's tarballs) and
# build/usenet-start.py (the workflow's Usenet check).
# Outputs in build/out/: DownloadPulse-<version>-linux-x86_64.flatpak (a
# single-file bundle, unsigned), SHA256SUMS and RESULTS.txt -- the runtime and
# SDK commits, the installed bundle, each helper run inside the sandbox, the
# Usenet service started there by the existing executor (health, exit 0 on
# stop) and a first launch of the application itself on a virtual display
# (backend ready, shell running, flatpak-first-run.png, then flatpak kill).
set -euo pipefail
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends flatpak flatpak-builder ca-certificates \
  binutils elfutils patch xz-utils zstd git >/dev/null   # flatpak-builder's own Recommends it uses (strip, unpack; git: the shell's pinned source)
# The launch check's virtual display, session bus and screenshot.
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends xvfb xauth dbus-x11 procps imagemagick >/dev/null
out=build/out; mkdir -p "$out"; results="$out/RESULTS.txt"
dpkg-query -W flatpak flatpak-builder | tee "$results"
flatpak --user remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak --user install -y --noninteractive flathub org.gnome.Platform//49 org.gnome.Sdk//49 >/dev/null
for ref in org.gnome.Platform//49 org.gnome.Sdk//49; do
  echo "$ref commit $(flatpak --user info "$ref" | sed -n 's/^ *Commit: //p')"
done | tee -a "$results"
flatpak-builder --user --disable-rofiles-fuse --force-clean --repo=build/repo build/app \
  packaging/desktop/flatpak/net.downloadpulse.desktop.yml
bundle="$out/DownloadPulse-$VERSION-linux-x86_64.flatpak"
flatpak build-bundle build/repo "$bundle" net.downloadpulse.desktop
(cd "$out" && sha256sum "$(basename "$bundle")" > SHA256SUMS && cat SHA256SUMS)

# Installed from the bundle exactly as a user would, then run in its sandbox.
flatpak --user install -y --noninteractive --bundle "$bundle"
echo "installed: $(flatpak --user list --app --columns=application,version,branch,origin | grep net.downloadpulse.desktop)" | tee -a "$results"
tools=/app/lib/downloadpulse/tools
check() { name="$1"; banner="$2"; shift 2
  line="$(flatpak run --command="$tools/$name" net.downloadpulse.desktop "$@" 2>&1 | grep -m1 -E "$banner" || true)"
  [ -n "$line" ] || { echo "$name: no '$banner' inside the sandbox" | tee -a "$results"; exit 1; }
  echo "$name: $line" | tee -a "$results"; }
# The native shell as packaged: built in this recipe from Neutralino 6.10.0 with the Linux patch,
# and recorded as such in the installed BUILD-INFO.json (the same SHA-256 as the executable).
shell="$(flatpak run --command=sha256sum net.downloadpulse.desktop /app/lib/downloadpulse/desktop/shell/downloadpulse-shell | cut -d' ' -f1)"
echo "shell: $shell (/app/lib/downloadpulse/desktop/shell/downloadpulse-shell)" | tee -a "$results"
recorded="$(flatpak run --command=/app/lib/downloadpulse/runtime/python/bin/python3 net.downloadpulse.desktop -I -c \
  'import json; s = json.load(open("/app/lib/downloadpulse/BUILD-INFO.json"))["shell"]; print(s["binary_sha256"], s["commit"], *s["patches"].items())')"
echo "shell provenance (BUILD-INFO.json): $recorded" | tee -a "$results"
[ "${recorded%% *}" = "$shell" ] || { echo "BUILD-INFO.json names another shell than the one installed" | tee -a "$results"; exit 1; }
check aria2c '^aria2 version 1\.37\.0' --version
check ffmpeg '^ffmpeg version 7\.1\.5' -hide_banner -version
check ffprobe '^ffprobe version 7\.1\.5' -hide_banner -version
check rsync 'rsync +version 3\.5\.2' --version
check zstd 'Zstandard CLI' --version
check par2 'par2cmdline version 0\.8\.1' --version
check 7zz '7-Zip' i
check unrar '^UNRAR 7\.23'
features="$(flatpak run --command="$tools/aria2c" net.downloadpulse.desktop --version | sed -n 's/^Enabled Features: //p')"
echo "aria2c enabled features: $features" | tee -a "$results"
case ", $features," in *", BitTorrent,"*) echo "aria2c has BitTorrent" | tee -a "$results"; exit 1 ;; esac
flatpak run --filesystem="$PWD/build:ro" --env=PATH="$tools:/app/bin:/usr/bin" \
  --env=DB_PATH=/tmp/usenet-check/data/downloadpulse.db --env=USENET_CONFIG_DIR=/tmp/usenet-check/usenet \
  --command=sh net.downloadpulse.desktop -c \
  'mkdir -p /tmp/usenet-check/data /tmp/usenet-check/downloads && exec /app/lib/downloadpulse/runtime/python/bin/python3 -B -I "$0" /app/lib/downloadpulse/backend /tmp/usenet-check/downloads' \
  "$PWD/build/usenet-start.py" 2>&1 | tee -a "$results"
test "${PIPESTATUS[0]}" -eq 0

# The application itself, launched as installed (first run: no data root) on a
# virtual display and session bus; no desktop portal or tray host exists here.
rm -rf "$HOME/.var/app/net.downloadpulse.desktop"
export XDG_RUNTIME_DIR=/tmp/xdg-runtime; mkdir -p -m 700 "$XDG_RUNTIME_DIR"
xvfb-run -a -s "-screen 0 1280x800x24" dbus-run-session -- sh -c '
  flatpak run net.downloadpulse.desktop > /tmp/flatpak-launch.out 2>&1 &
  logs="$HOME/.var/app/net.downloadpulse.desktop/data/downloadpulse/logs"; ready=no
  for i in $(seq 1 120); do
    sleep 1; grep -qs "Scheduler started" "$logs/backend.log" && pgrep -f downloadpulse-shell >/dev/null && { ready=yes; break; }
  done
  echo "launch: backend ready and shell running: $ready (after ${i}s)"
  sleep 20; import -window root "$0/flatpak-first-run.png"
  echo "launch: running in the sandbox: $(ps -eo args | grep -E "supervisor\.py|uvicorn main:app|downloadpulse-shell|^aria2c" | grep -v grep | cut -d" " -f1-2 | tr "\n" ";")"
  flatpak kill net.downloadpulse.desktop; sleep 5
  echo "launch: processes left after flatpak kill: $(ps -eo args | grep -E "supervisor\.py|uvicorn main:app|downloadpulse-shell|^aria2c" | grep -vc grep)"
  [ "$ready" = yes ]
' "$PWD/$out" 2>&1 | grep '^launch:' | tee -a "$results"
test "${PIPESTATUS[0]}" -eq 0
