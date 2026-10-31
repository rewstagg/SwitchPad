#!/bin/sh
# Installs SwitchPad for the current user (no root needed).
set -e
cd "$(dirname "$0")"
ID=io.github.rewstagg.SwitchPad
D="${XDG_DATA_HOME:-$HOME/.local/share}"
install -Dm755 switchpad.py "$D/switchpad/switchpad.py"
install -d "$HOME/.local/bin"
ln -sf "$D/switchpad/switchpad.py" "$HOME/.local/bin/switchpad"
install -Dm644 "$ID.desktop" "$D/applications/$ID.desktop"
sed -i "s|^Exec=.*|Exec=$D/switchpad/switchpad.py|" "$D/applications/$ID.desktop"
install -Dm644 "$ID.svg" "$D/icons/hicolor/scalable/apps/$ID.svg"
# Remove files from versions that used the old app ID (io.switchpad.SwitchPad).
rm -f "$D/applications/io.switchpad.SwitchPad.desktop" \
  "$D/icons/hicolor/scalable/apps/io.switchpad.SwitchPad.svg"
gtk-update-icon-cache -q -t "$D/icons/hicolor" 2>/dev/null || true
update-desktop-database -q "$D/applications" 2>/dev/null || true
echo "Installed. Launch SwitchPad from the app grid or run: switchpad"
