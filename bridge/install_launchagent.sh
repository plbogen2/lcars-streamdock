#!/usr/bin/env bash
set -euo pipefail

DEST_DIR="$HOME/.lcars_dock"
PLIST="$HOME/Library/LaunchAgents/com.plbogen.lcarsdock.plist"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$DEST_DIR" "$HOME/Library/Logs" "$HOME/Library/LaunchAgents"
cp "$SCRIPT_DIR/lcars_dock_bridge.py" "$DEST_DIR/lcars_dock_bridge.py"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.plbogen.lcarsdock</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>-u</string>
    <string>$DEST_DIR/lcars_dock_bridge.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$HOME/Library/Logs/lcars_dock.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/Library/Logs/lcars_dock.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/com.plbogen.lcarsdock" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed and started com.plbogen.lcarsdock on http://127.0.0.1:15890"
