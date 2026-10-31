#!/bin/sh
D="${XDG_DATA_HOME:-$HOME/.local/share}"
rm -rf "$D/switchpad" "$HOME/.local/bin/switchpad" \
  "$D/applications/io.switchpad.SwitchPad.desktop" \
  "$D/icons/hicolor/scalable/apps/io.switchpad.SwitchPad.svg"
echo "Removed. (Credentials stay in your keyring; cache in ~/.cache/switchpad.)"
