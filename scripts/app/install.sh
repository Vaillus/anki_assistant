#!/bin/bash
# Build "Anki Assistant.app" in ~/Applications: a real app bundle around the WKWebView
# host in main.swift, so the UI gets its own Dock icon and running dot instead of the
# browser's.
#
# The bundle stores this repo's path and calls launch.sh from it, so edits to that
# script take effect immediately — rebuild only after touching main.swift, the icon,
# or the install location.
#
#   scripts/app/install.sh [destination-dir]   (default: ~/Applications)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEST="${1:-$HOME/Applications}"
APP="$DEST/Anki Assistant.app"
BUNDLE_ID="local.anki-assistant"  # matches BUNDLE_ID in launch.sh

command -v swiftc >/dev/null || {
  echo "swiftc not found: install the Xcode command line tools (xcode-select --install)" >&2
  exit 1
}

cd "$PROJECT_DIR"
uv run python "$SCRIPT_DIR/make_icon.py" "$SCRIPT_DIR/img/icon.icns" > /dev/null

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -O -o "$APP/Contents/MacOS/AnkiAssistant" "$SCRIPT_DIR/main.swift"
cp "$SCRIPT_DIR/img/icon.icns" "$APP/Contents/Resources/icon.icns"

# NSAllowsLocalNetworking: App Transport Security blocks plain http, and the server
# speaks http on 127.0.0.1.
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Anki Assistant</string>
  <key>CFBundleDisplayName</key><string>Anki Assistant</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>AnkiAssistant</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>AnkiAssistantLauncher</key><string>$SCRIPT_DIR/launch.sh</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST

# An unsigned bundle will not launch; ad-hoc is enough for a local app.
codesign --force --sign - "$APP"

# The Dock and Finder cache an app's icon per bundle; a rebuilt bundle at the same
# path keeps showing the old one until they are re-registered and restarted.
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP" 2>/dev/null || true
killall Dock 2>/dev/null || true
killall Finder 2>/dev/null || true

echo "installed: $APP"
echo "open it from Finder, then keep it in the Dock (right-click > Options > Keep in Dock)."
