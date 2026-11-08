#!/usr/bin/env bash
# Sign dist/Tamis.app with a Developer ID, wrap it in a DMG, and notarize and
# staple the DMG, so that macOS opens it on any Mac. Run scripts/build_app.sh first.
#
#   TAMIS_SIGN_IDENTITY   "Developer ID Application: Name (TEAMID)", in the keychain (required)
#   TAMIS_NOTARY_PROFILE  a keychain profile made with `xcrun notarytool store-credentials`, or else
#   TAMIS_NOTARY_KEY, TAMIS_NOTARY_KEY_ID, TAMIS_NOTARY_ISSUER  an App Store Connect API key (.p8 path, ids)
#
# Without notary credentials the DMG is signed but not notarized.
set -euo pipefail
cd "$(dirname "$0")/.."

app=dist/Tamis.app
if [[ ! -d "$app" ]]; then
    echo "$app not found: run scripts/build_app.sh first" >&2
    exit 1
fi
: "${TAMIS_SIGN_IDENTITY:?set TAMIS_SIGN_IDENTITY to a Developer ID Application identity}"
version=$(plutil -extract CFBundleShortVersionString raw "$app/Contents/Info.plist")
dmg="dist/Tamis-$version.dmg"

# Hardened runtime, required for notarization. Every binary inside is signed, then the bundle.
sign() {
    codesign --force --timestamp --options runtime --entitlements scripts/entitlements.plist \
        --sign "$TAMIS_SIGN_IDENTITY" "$@"
}
find "$app/Contents" -type f \( -name "*.dylib" -o -name "*.so" -o -perm -u+x \) -print0 |
    while IFS= read -r -d '' file; do
        if file "$file" | grep -q "Mach-O"; then
            sign "$file"
        fi
    done
sign "$app"
codesign --verify --deep --strict "$app"

staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
cp -R "$app" "$staging/"
ln -s /Applications "$staging/Applications"
rm -f "$dmg"
hdiutil create -volname "Tamis $version" -srcfolder "$staging" -ov -format UDZO "$dmg"
codesign --force --timestamp --sign "$TAMIS_SIGN_IDENTITY" "$dmg"

if [[ -n "${TAMIS_NOTARY_PROFILE:-}" ]]; then
    xcrun notarytool submit "$dmg" --keychain-profile "$TAMIS_NOTARY_PROFILE" --wait
elif [[ -n "${TAMIS_NOTARY_KEY:-}" ]]; then
    xcrun notarytool submit "$dmg" --key "$TAMIS_NOTARY_KEY" --key-id "$TAMIS_NOTARY_KEY_ID" \
        --issuer "$TAMIS_NOTARY_ISSUER" --wait
else
    echo "No notary credentials: $dmg is signed but not notarized" >&2
    exit 0
fi
xcrun stapler staple "$dmg"
spctl --assess --type open --context context:primary-signature --verbose "$dmg"
echo "Notarized $dmg"
