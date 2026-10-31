#!/bin/sh
# Installs SwitchPad for the current user (no root needed).
set -e
cd "$(dirname "$0")"
D="${XDG_DATA_HOME:-$HOME/.local/share}"
install -Dm755 switchpad.py "$D/switchpad/switchpad.py"
install -d "$HOME/.local/bin"
ln -sf "$D/switchpad/switchpad.py" "$HOME/.local/bin/switchpad"
install -Dm644 io.switchpad.SwitchPad.desktop "$D/applications/io.switchpad.SwitchPad.desktop"
sed -i "s|^Exec=.*|Exec=$D/switchpad/switchpad.py|" "$D/applications/io.switchpad.SwitchPad.desktop"
install -Dm644 io.switchpad.SwitchPad.svg "$D/icons/hicolor/scalable/apps/io.switchpad.SwitchPad.svg"
gtk-update-icon-cache -q -t "$D/icons/hicolor" 2>/dev/null || true
update-desktop-database -q "$D/applications" 2>/dev/null || true
echo "Installed. Launch SwitchPad from the app grid or run: switchpad"
