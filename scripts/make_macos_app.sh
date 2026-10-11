#!/bin/bash
# Build a small macOS app that opens the viewer in the default browser (no Terminal window).
#   scripts/make_macos_app.sh [destination folder] [app name] [config path]
# Defaults: ~/Applications, "Bot Viewer", config.json next to bot_viewer.py.
# Re-running replaces the old app (it is moved to the Trash).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="${2:-Bot Viewer}"
DEST="${1:-$HOME/Applications}/$NAME.app"
CONFIG="${3:-$ROOT/config.json}"
case "$CONFIG" in /*) ;; *) CONFIG="$PWD/$CONFIG" ;; esac
TMP="$(mktemp -d "${TMPDIR:-/tmp}/bot-viewer-app.XXXXXX")"
APP="$TMP/$NAME.app"

to_trash() {   # move to the Trash instead of deleting
  if command -v trash >/dev/null 2>&1; then trash "$1"
  else osascript -e "tell application \"Finder\" to delete POSIX file \"$1\"" >/dev/null; fi
}

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$(dirname "$DEST")"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>bot-viewer</string>
  <key>CFBundleIdentifier</key><string>local.bot-viewer</string>
  <key>CFBundleName</key><string>$NAME</string>
  <key>CFBundleDisplayName</key><string>$NAME</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/bot-viewer" <<SCRIPT
#!/bin/bash
# Launcher for $ROOT/bot_viewer.py (local server on 127.0.0.1, read-only)
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
mkdir -p "$ROOT/run"
if ! python3 "$ROOT/bot_viewer.py" --config "$CONFIG" >>"$ROOT/run/launcher.log" 2>&1; then
  osascript -e 'display alert "$NAME" message "The viewer did not start. See $ROOT/run/launcher.log"' >/dev/null 2>&1
fi
SCRIPT
chmod +x "$APP/Contents/MacOS/bot-viewer"

# Icon from assets/icon.svg (Quick Look renders the SVG, sips/iconutil build the .icns); skipped on failure
if qlmanage -t -s 1024 -o "$TMP" "$ROOT/assets/icon.svg" >/dev/null 2>&1 && [ -s "$TMP/icon.svg.png" ]; then
  SET="$TMP/AppIcon.iconset"; mkdir -p "$SET"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$TMP/icon.svg.png" --out "$SET/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) "$TMP/icon.svg.png" --out "$SET/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$SET" -o "$APP/Contents/Resources/AppIcon.icns" || echo "icon was not built" >&2
fi

[ -e "$DEST" ] && to_trash "$DEST"
mv "$APP" "$DEST"
touch "$DEST"                                   # make Finder reload the icon
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST" >/dev/null 2>&1 || true
echo "done: $DEST"
