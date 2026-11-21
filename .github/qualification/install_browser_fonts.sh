#!/usr/bin/env bash
# The fonts the browser specs measure, pinned.
#
# The specs block the web fonts (Outfit, JetBrains Mono), so the page is set in
# the system's default sans-serif, system-ui and monospace, and many of them
# measure that text: a title on one row at 320px, a short label only where the
# full one does not fit. Those widths are a font's, so the font must be the same
# everywhere the specs run. These are the exact upstream Noto builds the specs
# were written against (byte-identical to Fedora's google-noto packages of
# 2026-06), fetched from the Noto projects' own releases and verified by
# SHA-256, installed beside the runner's fonts and made the defaults; any older
# copy of the same families on the system is set aside so it cannot be chosen.
#
# usage: install_browser_fonts.sh <download cache dir>
# (root, or sudo for the system font directory and fontconfig).
set -euo pipefail

cache="${1:?usage: install_browser_fonts.sh <download cache dir>}"
target=/usr/local/share/fonts/downloadpulse-browser
SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO=sudo

# archive URL | SHA-256 | files to install (paths inside the archive, globs)
ARCHIVES=(
  "https://github.com/notofonts/latin-greek-cyrillic/releases/download/NotoSans-v2.015/NotoSans-v2.015.zip|0c34df072a3fa7efbb7cbf34950e1f971a4447cffe365d3a359e2d4089b958f5|NotoSans/hinted/ttf/NotoSans-*.ttf"
  "https://github.com/notofonts/latin-greek-cyrillic/releases/download/NotoSansMono-v2.014/NotoSansMono-v2.014.zip|090cf6c5e03f337a755630ca888b1fef463e64ae7b33ee134e9309c05f978732|NotoSansMono/hinted/ttf/NotoSansMono-*.ttf"
  "https://github.com/notofonts/noto-cjk/releases/download/Sans2.004/03_NotoSansCJK-OTC.zip|528f4e1b25ff3badb0321b38d015d954c4c0de926c7830ef50e4a1948f6a3eed|NotoSansCJK-*.ttc"
  "https://github.com/notofonts/arabic/releases/download/NotoSansArabic-v2.013/NotoSansArabic-v2.013.zip|1301aceaea84c501cf2e6dcfb3182e2328c8eae5725817fcb239672bda7154f1|NotoSansArabic/unhinted/slim-variable-ttf/NotoSansArabic[[]wght[]].ttf"
  "https://github.com/notofonts/devanagari/releases/download/NotoSansDevanagari-v2.006/NotoSansDevanagari-v2.006.zip|4c582c103f0a42836338df07148b23a0aa080cce8393ddc4364af87eb22ebd85|NotoSansDevanagari/unhinted/slim-variable-ttf/NotoSansDevanagari[[]wght[]].ttf"
)

mkdir -p "$cache"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
$SUDO rm -rf "$target"
$SUDO mkdir -p "$target"
for entry in "${ARCHIVES[@]}"; do
  IFS='|' read -r url sha files <<< "$entry"
  archive="$cache/$(basename "$url")"
  if [ ! -f "$archive" ] || ! echo "$sha  $archive" | sha256sum -c --status; then
    curl -fsSL --retry 3 -o "$archive.part" "$url"
    mv "$archive.part" "$archive"
  fi
  echo "$sha  $archive" | sha256sum -c --quiet
  unzip -q -o "$archive" -d "$work/$(basename "$url" .zip)"
  count=0
  for file in "$work/$(basename "$url" .zip)"/$files; do
    [ -f "$file" ] || continue
    $SUDO install -m 0644 "$file" "$target/"
    count=$((count + 1))
  done
  [ "$count" -gt 0 ] || { echo "no font files matched $files in $(basename "$url")" >&2; exit 1; }
  echo "$(basename "$url"): $count files"
done

$SUDO tee /etc/fonts/local.conf > /dev/null <<'XML'
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <!-- Older copies of the pinned families (a distribution's own Noto) are set aside. -->
  <selectfont>
    <rejectfont>
      <glob>/usr/share/fonts/*/NotoSans-*</glob>
      <glob>/usr/share/fonts/*/*/NotoSans-*</glob>
      <glob>/usr/share/fonts/*/NotoSansMono-*</glob>
      <glob>/usr/share/fonts/*/*/NotoSansMono-*</glob>
      <glob>/usr/share/fonts/*/NotoSansCJK*</glob>
      <glob>/usr/share/fonts/*/*/NotoSansCJK*</glob>
      <glob>/usr/share/fonts/*/NotoSansArabic*</glob>
      <glob>/usr/share/fonts/*/*/NotoSansArabic*</glob>
      <glob>/usr/share/fonts/*/NotoSansDevanagari*</glob>
      <glob>/usr/share/fonts/*/*/NotoSansDevanagari*</glob>
      <glob>/usr/share/fonts/*/NotoMono-*</glob>
      <glob>/usr/share/fonts/*/*/NotoMono-*</glob>
    </rejectfont>
  </selectfont>
  <!-- The defaults, then the script fonts a character falls back to, in this
       order (as Fedora's own configuration does): never a font that happens
       to be installed beside them (WenQuanYi, FreeSerif). -->
  <alias binding="strong"><family>sans-serif</family><prefer><family>Noto Sans</family><family>Noto Sans CJK JP</family><family>Noto Sans Arabic</family><family>Noto Sans Devanagari</family></prefer></alias>
  <alias binding="strong"><family>system-ui</family><prefer><family>Noto Sans</family><family>Noto Sans CJK JP</family><family>Noto Sans Arabic</family><family>Noto Sans Devanagari</family></prefer></alias>
  <alias binding="strong"><family>monospace</family><prefer><family>Noto Sans Mono</family><family>Noto Sans Mono CJK JP</family></prefer></alias>
  <!-- Han characters in the form of the page's language. -->
  <match target="pattern"><test name="lang" compare="contains"><string>ko</string></test><edit name="family" mode="prepend"><string>Noto Sans CJK KR</string></edit></match>
  <match target="pattern"><test name="lang" compare="contains"><string>zh-cn</string></test><edit name="family" mode="prepend"><string>Noto Sans CJK SC</string></edit></match>
  <match target="pattern"><test name="lang" compare="contains"><string>zh-tw</string></test><edit name="family" mode="prepend"><string>Noto Sans CJK TC</string></edit></match>
  <match target="pattern"><test name="lang" compare="contains"><string>zh-hk</string></test><edit name="family" mode="prepend"><string>Noto Sans CJK HK</string></edit></match>
</fontconfig>
XML
$SUDO fc-cache -f > /dev/null

# Each default resolves to the pinned file, or nothing runs.
for pattern in "sans-serif|NotoSans-Regular.ttf" "system-ui|NotoSans-Regular.ttf" \
    "monospace|NotoSansMono-Regular.ttf" "Noto Sans CJK JP|NotoSansCJK-Regular.ttc" \
    "Noto Sans Arabic|NotoSansArabic[wght].ttf" "Noto Sans Devanagari|NotoSansDevanagari[wght].ttf"; do
  IFS='|' read -r query expected <<< "$pattern"
  file="$(fc-match -f '%{file}' "$query")"
  echo "$query -> $file"
  if [ "$file" != "$target/$expected" ]; then
    echo "::error title=Browser fonts::$query resolves to $file, not the pinned $expected"
    exit 1
  fi
done
