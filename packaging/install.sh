#!/usr/bin/env bash
# install.sh -- install audalis as a desktop application.
#
#   ./packaging/install.sh             user install  -> ~/.local (no sudo)
#   ./packaging/install.sh --system    system install -> /usr (uses sudo)
#   ./packaging/install.sh --no-pip    skip the Python package step
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_ROOT="$(dirname "$SCRIPT_DIR")"

SYSTEM=0
NO_PIP=0

for arg in "$@"; do
  case "$arg" in
    --system) SYSTEM=1 ;;
    --no-pip) NO_PIP=1 ;;
    -h|--help)
      sed -n '2,9p' "$0"
      exit 0
      ;;
    *)
      echo "error: unknown option '$arg'" >&2
      exit 1
      ;;
  esac
done

run_root() {
  if [ "$(id -u)" = 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

if [ "$SYSTEM" = 1 ]; then
  PREFIX=/usr
  APPS_DIR="$PREFIX/share/applications"
  ICON_DIR="$PREFIX/share/icons/hicolor/scalable/apps"
else
  PREFIX="${XDG_DATA_HOME:-$HOME/.local}"
  APPS_DIR="$PREFIX/share/applications"
  ICON_DIR="$PREFIX/share/icons/hicolor/scalable/apps"
fi

LAUNCHER="$PREFIX/bin/audalis"

if [ "$NO_PIP" = 1 ]; then
  if ! command -v audalis >/dev/null 2>&1; then
    echo "error: 'audalis' not found in PATH and --no-pip was given" >&2
    exit 1
  fi
  LAUNCHER="$(command -v audalis)"
else
  echo "==> installing Python package"
  if [ "$SYSTEM" = 1 ]; then
    run_root python3 -m pip install --break-system-packages "$PKG_ROOT"
  else
    python3 -m pip install --break-system-packages "$PKG_ROOT"
  fi
  if command -v audalis >/dev/null 2>&1; then
    LAUNCHER="$(command -v audalis)"
  else
    for cand in "$PREFIX/bin/audalis" /usr/local/bin/audalis "$HOME/.local/bin/audalis"; do
      if [ -x "$cand" ]; then
        LAUNCHER="$cand"
        break
      fi
    done
  fi
fi

echo "==> installing desktop entry and icon"
mkdir -p "$APPS_DIR" "$ICON_DIR"

TMP_DESKTOP="$(mktemp)"
cat > "$TMP_DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Audalis
GenericName=Audio control
Comment=Audio control for Linux
Exec=$LAUNCHER
Icon=audalis
Terminal=false
Categories=AudioVideo;Audio;
Keywords=sound;audio;volume;headset;microphone;balance;boost;
StartupNotify=true
StartupWMClass=audalis
EOF

install -m 0644 "$TMP_DESKTOP" "$APPS_DIR/audalis.desktop"
rm -f "$TMP_DESKTOP"
install -m 0644 "$SCRIPT_DIR/audalis.svg" "$ICON_DIR/audalis.svg"

echo "==> refreshing desktop databases"
update-desktop-database "$APPS_DIR" 2>/dev/null || true
gtk-update-icon-cache -f -t "$PREFIX/share/icons/hicolor" >/dev/null 2>&1 || true

echo
echo "audalis installed:"
echo "  desktop entry : $APPS_DIR/audalis.desktop"
echo "  icon          : $ICON_DIR/audalis.svg"
echo "  launcher      : $LAUNCHER"
case "$SYSTEM" in
  0) echo "  scope         : user install (remove by deleting the files above)" ;;
  1) echo "  scope         : system install" ;;
esac