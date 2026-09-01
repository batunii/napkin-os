#!/usr/bin/env bash
set -euo pipefail

# Which repo's releases to install from. Overridable like CLAN_VERSION /
# CLAN_BIN_DIR below, so a fork can be installed without editing this file:
#   curl -fsSL .../install.sh | CLAN_REPO=owner/repo bash
REPO="${CLAN_REPO:-batunii/napkin-os}"
BIN_NAME="clan"
BIN_DIR="${CLAN_BIN_DIR:-/usr/local/bin}"

# Detect OS and arch
OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
  Darwin)
    case "$ARCH" in
      arm64)  TARGET="aarch64-apple-darwin" ;;
      x86_64) TARGET="x86_64-apple-darwin" ;;
      *) echo "error: unsupported architecture: $ARCH" && exit 1 ;;
    esac
    ;;
  Linux)
    case "$ARCH" in
      x86_64) TARGET="x86_64-unknown-linux-gnu" ;;
      *) echo "error: unsupported architecture: $ARCH" && exit 1 ;;
    esac
    ;;
  *)
    echo "error: unsupported OS: $OS"
    echo "Windows users: download the .msi from https://github.com/$REPO/releases"
    exit 1
    ;;
esac

# Resolve version — pin with CLAN_VERSION=1.2.3 or fetch latest
VERSION="${CLAN_VERSION:-}"
if [ -z "$VERSION" ]; then
  echo "Fetching latest release..."
  VERSION="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
    | sed -nE 's/.*"tag_name"[[:space:]]*:[[:space:]]*"v?([^"]+)".*/\1/p' | head -n1)"
  if [ -z "$VERSION" ]; then
    echo "error: could not determine latest version. Set CLAN_VERSION to install a specific version."
    exit 1
  fi
fi

# Every download URL in that release. The app bundle's filename comes from
# tauri's productName, which changed from "CLAN Viewer" to "Napkin Studio OS" —
# so a constructed filename 404s on whichever side of the rename it was not
# written for. Match by pattern instead and both names work.
ASSETS="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/tags/v${VERSION}" 2>/dev/null \
  | grep -oE '"browser_download_url"[[:space:]]*:[[:space:]]*"[^"]+"' \
  | sed -E 's/.*"(https[^"]+)"/\1/')"
asset() { printf '%s\n' "$ASSETS" | grep -E "$1" | head -n1; }

# ── CLI install ────────────────────────────────────────────────────────────────

TARBALL="clan-v${VERSION}-${TARGET}.tar.gz"
URL="$(asset "clan-v?${VERSION}-${TARGET}\.tar\.gz$")"
URL="${URL:-https://github.com/${REPO}/releases/download/v${VERSION}/${TARBALL}}"

echo "Installing clan v${VERSION} (${TARGET})..."

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

curl -fsSL --progress-bar "$URL" -o "$TMP/$TARBALL"
tar -xzf "$TMP/$TARBALL" -C "$TMP"

BIN_SRC="$TMP/clan-v${VERSION}-${TARGET}/clan"
if [ -w "$BIN_DIR" ]; then
  install -m 755 "$BIN_SRC" "$BIN_DIR/$BIN_NAME"
else
  sudo install -m 755 "$BIN_SRC" "$BIN_DIR/$BIN_NAME"
fi

if [ "$OS" = "Darwin" ]; then
  xattr -d com.apple.quarantine "$BIN_DIR/$BIN_NAME" 2>/dev/null || true
fi

echo ""
echo "✓ clan v${VERSION} installed to $BIN_DIR/$BIN_NAME"
echo ""
cat << 'EOF'
   ██████╗██╗      █████╗ ███╗   ██╗
  ██╔════╝██║     ██╔══██╗████╗  ██║
  ██║     ██║     ███████║██╔██╗ ██║
  ██║     ██║     ██╔══██║██║╚██╗██║
  ╚██████╗███████╗██║  ██║██║ ╚████║
   ╚═════╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═══╝

  Context and Live Agent Notation
  Any model. Any framework. One file.
EOF

# ── Viewer (interactive) ───────────────────────────────────────────────────────

echo ""
printf "Would you like to install the Napkin Studio OS (desktop app)? [y/N] "
read -r INSTALL_VIEWER </dev/tty

if [[ "$INSTALL_VIEWER" =~ ^[Yy]$ ]]; then
  DOWNLOADS="${HOME}/Downloads"
  mkdir -p "$DOWNLOADS"

  case "$OS" in
    Darwin)
      VIEWER_URL="$(asset "\.dmg$")"
      if [ -z "$VIEWER_URL" ]; then
        echo "No macOS app bundle in release v${VERSION} — skipping."
        echo "  See https://github.com/$REPO/releases"
        exit 0
      fi
      VIEWER_FILE="$(basename "$VIEWER_URL")"
      VIEWER_DEST="$DOWNLOADS/$VIEWER_FILE"

      echo "Downloading Napkin Studio OS..."
      curl -fsSL --progress-bar "$VIEWER_URL" -o "$VIEWER_DEST"

      echo ""
      echo "✓ Napkin Studio OS downloaded to $VIEWER_DEST"
      echo ""
      echo "To install:"
      echo "  1. Open $VIEWER_DEST"
      echo "  2. Drag Napkin Studio OS to your Applications folder"
      echo "  3. On first launch, clear the Gatekeeper warning:"
      echo "     xattr -d com.apple.quarantine \"/Applications/Napkin Studio OS.app\""
      echo "     open \"/Applications/Napkin Studio OS.app\""
      echo ""
      echo "To open any .clan file in the viewer:"
      echo "  open -a \"Napkin Studio OS\" your-file.clan"

      # Offer to open the DMG immediately
      echo ""
      printf "Open the DMG now? [y/N] "
      read -r OPEN_DMG </dev/tty
      if [[ "$OPEN_DMG" =~ ^[Yy]$ ]]; then
        open "$VIEWER_DEST"
      fi
      ;;

    Linux)
      VIEWER_URL="$(asset "\.AppImage$")"
      if [ -z "$VIEWER_URL" ]; then
        echo "No Linux AppImage in release v${VERSION} — skipping."
        echo "  See https://github.com/$REPO/releases"
        exit 0
      fi
      VIEWER_FILE="$(basename "$VIEWER_URL")"
      VIEWER_DEST="$DOWNLOADS/$VIEWER_FILE"

      echo "Downloading Napkin Studio OS..."
      curl -fsSL --progress-bar "$VIEWER_URL" -o "$VIEWER_DEST"
      chmod +x "$VIEWER_DEST"

      echo ""
      echo "✓ Napkin Studio OS downloaded to $VIEWER_DEST"
      echo ""
      echo "To launch:"
      echo "  $VIEWER_DEST"
      echo ""
      echo "To make it easier to run, create an alias:"
      echo "  echo \"alias napkin-app='$VIEWER_DEST'\" >> ~/.bashrc && source ~/.bashrc"
      echo "  (not 'napkin' — install-napkin.sh installs a 'napkin' command that"
      echo "   starts the agent and opens the app; an alias would shadow it.)"
      echo ""
      echo "To open any .clan file:"
      echo "  $VIEWER_DEST your-file.clan"
      ;;
  esac
else
  echo ""
  echo "Skipping viewer. You can install it later from:"
  echo "  https://github.com/$REPO/releases"
fi

echo ""
echo "Get started: clan --help"
