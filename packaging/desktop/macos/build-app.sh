#!/usr/bin/env bash
# DebridPulse.app and its DMG, from a layout assembled on this Mac
# (packaging/desktop/assemble.py), with Apple's own tools only: iconutil (the
# .icns from the derived icons), hdiutil (the DMG) and, when an identity is
# given, codesign + notarytool.
#   build-app.sh <stage-dir> <out-dir> [<Developer ID Application identity>]
# Without an identity the bundle is signed ad hoc: it runs on this Mac but is
# NOT notarizable and is not presented as trusted. With one, every Mach-O is
# signed with the hardened runtime and entitlements.plist (deno's V8 needs JIT;
# the bundled Python loads the wheels' extension modules), then the DMG is
# submitted with `xcrun notarytool submit --keychain-profile "$NOTARY_PROFILE"
# --wait` and stapled -- credentials the operator provides, never this script.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
stage="$(cd "$1" && pwd)"; mkdir -p "$2"; out="$(cd "$2" && pwd)"; identity="${3:-}"
version="$(tr -d '[:space:]' < "$stage/VERSION")"
app="$out/DebridPulse.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp -R "$stage" "$app/Contents/Resources/app"
# The application icon: an iconset from the canonical logo's derived PNG.
iconset="$out/DebridPulse.iconset"; rm -rf "$iconset"; mkdir "$iconset"
for size in 16 32 128 256 512; do
  sips -z $size $size "$stage/desktop/shell/resources/icons/app.png" --out "$iconset/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2)); [ $double -le 1024 ] && sips -z $double $double "$stage/desktop/shell/resources/icons/app.png" \
    --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset" -o "$app/Contents/Resources/DebridPulse.icns"; rm -rf "$iconset"
cat > "$app/Contents/MacOS/DebridPulse" <<'LAUNCH'
#!/bin/sh
# The bundle's executable: the supervisor on the bundled Python, nothing else.
here="$(cd "$(dirname "$0")/../Resources/app" && pwd)"
exec "$here/runtime/python/bin/python3" -I "$here/desktop/supervisor.py"
LAUNCH
chmod 755 "$app/Contents/MacOS/DebridPulse"
sed "s/@VERSION@/$version/g" "$here/Info.plist.in" > "$app/Contents/Info.plist"
plutil -lint "$app/Contents/Info.plist" >/dev/null
if [ -n "$identity" ]; then
  find "$app/Contents/Resources/app" -type f \( -perm -u+x -o -name '*.so' -o -name '*.dylib' \) -print0 |
    xargs -0 -I{} codesign --force --timestamp --options runtime --entitlements "$here/entitlements.plist" -s "$identity" {}
  codesign --force --timestamp --options runtime --entitlements "$here/entitlements.plist" -s "$identity" "$app"
else
  codesign --force --deep -s - "$app"
fi
codesign --verify --deep --strict "$app"
dmg="$out/DebridPulse-$version-macos-$(uname -m).dmg"
rm -f "$dmg"
hdiutil create -quiet -volname "DebridPulse" -srcfolder "$app" -fs HFS+ -format UDZO "$dmg"
[ -n "$identity" ] && codesign --force --timestamp -s "$identity" "$dmg"
(cd "$out" && shasum -a 256 "$(basename "$dmg")" | tee -a SHA256SUMS)
