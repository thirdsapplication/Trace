#!/usr/bin/env bash
# Sign, notarize and staple a Palza build made by desktop/build_mac.sh, and wrap it in
# a DMG that is itself signed, notarized and stapled. Run on the Mac that holds the
# "Developer ID Application" certificate. Nothing is rebuilt.
#
#   desktop/sign_mac.sh Palza-0.2.0-abc1234-unsigned.zip
#
# One-time setup on the signing Mac:
#   1. The Developer ID Application certificate in the login keychain
#      (check: security find-identity -v -p codesigning).
#   2. Notary credentials saved under the profile name "palza":
#      xcrun notarytool store-credentials palza \
#        --apple-id you@example.com --team-id TEAMID --password <app-specific password>
#
# Env: IDENTITY (default: the first Developer ID Application identity found),
#      NOTARY_PROFILE (default: palza).
#      ADHOC_TEST=1 signs with "-" and skips notarization: checks the signing steps
#      and that the node still starts under the hardened runtime, on any Mac.
#
# Output: Palza-<version>-<sha>.dmg next to the input. The script fails rather than
# produce a DMG that macOS would not accept as a notarized Developer ID app.
set -euo pipefail

IN="${1:?usage: sign_mac.sh <Palza-...-unsigned.zip | Palza.app>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ENTITLEMENTS="$HERE/entitlements.mac.plist"
NOTARY_PROFILE="${NOTARY_PROFILE:-palza}"
ADHOC="${ADHOC_TEST:-}"

if [ -n "$ADHOC" ]; then
  IDENTITY="-"
else
  IDENTITY="${IDENTITY:-$(security find-identity -v -p codesigning \
    | sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' | head -1)}"
  [ -n "$IDENTITY" ] || { echo "No Developer ID Application certificate in the keychain."; exit 1; }
fi
echo "== identity: $IDENTITY"

WORK="$(mktemp -d /tmp/palza-sign.XXXXXX)"
case "$IN" in
  *.zip) ditto -x -k "$IN" "$WORK" ;;
  *.app) ditto "$IN" "$WORK/Palza.app" ;;
  *) echo "expected a .zip or .app"; exit 1 ;;
esac
APP="$WORK/Palza.app"
[ -d "$APP" ] || { echo "no Palza.app inside $IN"; exit 1; }

INFO="$APP/Contents/Resources/BUILD_INFO"
VERSION="$(sed -n 's/^version=//p' "$INFO" 2>/dev/null || true)"
COMMIT="$(sed -n 's/^commit=//p' "$INFO" 2>/dev/null || true)"
echo "== build: version ${VERSION:-?}, commit ${COMMIT:-?}"

xattr -cr "$APP"

sign() {  # sign one item with the hardened runtime and a secure timestamp
  local ts="--timestamp"; [ -n "$ADHOC" ] && ts="--timestamp=none"
  codesign --force --options runtime $ts --sign "$IDENTITY" "$@"
}

# Inside-out: every Mach-O in the bundled node first, then the node's executable
# (with the Python entitlements), then the app. --deep is avoided on purpose: it
# signs nested code without entitlements and in no guaranteed order.
NODE_DIR="$APP/Contents/Resources/binaries/trace-node"
echo "== signing bundled libraries"
n=0
while IFS= read -r -d '' f; do
  [ "$f" = "$NODE_DIR/trace-node" ] && continue
  if file -b "$f" | grep -q "Mach-O"; then sign "$f"; n=$((n+1)); fi
done < <(find "$NODE_DIR" -type f -print0)
echo "   $n libraries"
sign --entitlements "$ENTITLEMENTS" "$NODE_DIR/trace-node"
sign "$APP"

echo "== verifying signature"
codesign --verify --deep --strict --verbose=2 "$APP"
if codesign -dv "$APP" 2>&1 | grep -q "Sealed Resources=none"; then echo "empty seal"; exit 1; fi

echo "== node starts under the hardened runtime"
"$NODE_DIR/trace-node" --help >/dev/null 2>&1 \
  || { echo "trace-node failed to start once signed (check entitlements)"; exit 1; }

if [ -z "$ADHOC" ]; then
  echo "== notarizing app (a few minutes)"
  ditto -c -k --keepParent "$APP" "$WORK/notarize.zip"
  xcrun notarytool submit "$WORK/notarize.zip" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$APP"
  verdict="$(spctl -a -vv "$APP" 2>&1 || true)"; echo "$verdict"
  [[ "$verdict" == *"Notarized Developer ID"* ]] || { echo "app is not accepted as notarized"; exit 1; }
fi

NAME="Palza-${VERSION:-unknown}-$(echo "${COMMIT:-nocommit}" | cut -c1-7)"
DMG="$(cd "$(dirname "$IN")" && pwd)/$NAME.dmg"
echo "== DMG"
hdiutil create -quiet -volname "Palza" -srcfolder "$APP" -ov -format UDZO "$DMG"

if [ -z "$ADHOC" ]; then
  codesign --force --timestamp --sign "$IDENTITY" "$DMG"
  echo "== notarizing DMG"
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
  verdict="$(spctl -a -vv -t open --context context:primary-signature "$DMG" 2>&1 || true)"
  echo "$verdict"
  [[ "$verdict" == *"Notarized Developer ID"* ]] || { echo "DMG is not accepted as notarized"; exit 1; }
fi

rm -rf "$WORK"
echo
echo "Ready: $DMG"
[ -n "$ADHOC" ] && echo "(ADHOC_TEST: signed with '-', NOT notarized — do not distribute)"
exit 0
