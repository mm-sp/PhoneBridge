#!/usr/bin/env bash
# Installs PhoneBridge for the current user — no root required.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${PHONEBRIDGE_HOME:-$HOME/.local/share/phonebridge}/venv"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"

echo "==> Checking for GTK4/libadwaita system libraries"
python3 -c "import gi; gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1'); from gi.repository import Gtk, Adw" 2>/dev/null \
  || { echo "ERROR: python3-gobject with GTK4 + libadwaita bindings not found. Install your distro's gtk4/libadwaita + python3-gobject packages first." >&2; exit 1; }

echo "==> Creating virtualenv at $VENV_DIR"
mkdir -p "$(dirname "$VENV_DIR")"
python3 -m venv --system-site-packages "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install "$REPO_ROOT/linux" --quiet

echo "==> Installing launcher to $BIN_DIR/phonebridge"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/phonebridge" <<EOF
#!/usr/bin/env bash
exec "$VENV_DIR/bin/phonebridge" "\$@"
EOF
chmod +x "$BIN_DIR/phonebridge"

echo "==> Installing desktop entry and icon"
mkdir -p "$DATA_HOME/applications" "$DATA_HOME/icons/hicolor/scalable/apps"
cp "$REPO_ROOT/linux/data/dev.phonebridge.Linux.desktop" "$DATA_HOME/applications/"
cp "$REPO_ROOT/linux/data/icons/dev.phonebridge.Linux.svg" "$DATA_HOME/icons/hicolor/scalable/apps/"

command -v update-desktop-database >/dev/null && update-desktop-database "$DATA_HOME/applications" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q "$DATA_HOME/icons/hicolor" || true

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "NOTE: $BIN_DIR is not on your PATH — add it to your shell profile to run 'phonebridge' directly." ;;
esac

echo "==> Done. Launch from your app menu (PhoneBridge) or run: phonebridge"
