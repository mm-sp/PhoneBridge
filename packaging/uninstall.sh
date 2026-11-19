#!/usr/bin/env bash
# Removes everything packaging/install.sh created.
set -euo pipefail

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"

rm -rf "${PHONEBRIDGE_HOME:-$DATA_HOME/phonebridge}"
rm -f "$BIN_DIR/phonebridge"
rm -f "$DATA_HOME/applications/dev.phonebridge.Linux.desktop"
rm -f "$DATA_HOME/icons/hicolor/scalable/apps/dev.phonebridge.Linux.svg"

command -v update-desktop-database >/dev/null && update-desktop-database "$DATA_HOME/applications" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q "$DATA_HOME/icons/hicolor" || true

echo "==> PhoneBridge uninstalled."
