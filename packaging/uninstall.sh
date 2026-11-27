#!/usr/bin/env bash
# Removes everything packaging/install.sh created.
set -euo pipefail

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"

# install.sh only creates venv/ inside its folder. PHONEBRIDGE_HOME may be a folder the user keeps other things in, so
# remove just that, and the folder itself only if nothing else is left in it.
APP_HOME="${PHONEBRIDGE_HOME:-$HOME/.local/share/phonebridge}"   # the same default as install.sh
rm -rf "$APP_HOME/venv"
rmdir "$APP_HOME" 2>/dev/null || true
rm -rf "$DATA_HOME/phonebridge"   # the app's own data folder (clipboard history)
rm -f "$BIN_DIR/phonebridge"
rm -f "$DATA_HOME/applications/dev.phonebridge.Linux.desktop"
rm -f "$DATA_HOME/icons/hicolor/scalable/apps/dev.phonebridge.Linux.svg"

command -v update-desktop-database >/dev/null && update-desktop-database "$DATA_HOME/applications" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q "$DATA_HOME/icons/hicolor" || true

echo "==> PhoneBridge uninstalled."
