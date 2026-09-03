#!/usr/bin/env bash
# Napkin Studio OS — one-command install (macOS / Linux).
#
#   curl -fsSL https://raw.githubusercontent.com/batunii/napkin-os/main/install-napkin.sh | bash
#
# Installs all three pieces, which is the point: the app alone is a viewer with
# no agent, and the agent alone is ungrounded.
#
#   1. the `clan` CLI            -> /usr/local/bin/clan
#   2. the desktop app           -> /Applications (macOS) | ~/Applications (Linux)
#   3. the agent + its knowledge -> ~/.napkin/agent
#   4. a `napkin` launcher       -> /usr/local/bin/napkin   (start | stop | status | …)
#
# Asset names are resolved from the GitHub release by PATTERN, never hardcoded:
# the bundles were published as "CLAN Viewer" and the app has since been renamed
# "Napkin Studio OS", so a hardcoded filename 404s on one side of that rename.
#
# Env:
#   CLAN_REPO=owner/repo   install from a fork          (default batunii/napkin-os)
#   CLAN_VERSION=1.2.3     pin a version               (default: latest release)
#   NAPKIN_HOME=~/.napkin  where the agent lives
#   NAPKIN_NO_APP=1        skip the desktop app
set -euo pipefail

REPO="${CLAN_REPO:-batunii/napkin-os}"
NAPKIN_HOME="${NAPKIN_HOME:-$HOME/.napkin}"
# Where `clan` goes. /usr/local/bin is the nicest default *when it is writable*,
# but on a stock macOS it is root-owned — and this script's headline invocation is
# `curl ... | bash`, where the script itself occupies stdin. sudo then has no
# channel to prompt on and fails outright ("no tty present"), which under
# `set -e` killed the install at step 1 before anything landed. So: honour an
# explicit CLAN_BIN_DIR, else take the first directory we can actually write.
pick_bin_dir() {
  if [ -n "${CLAN_BIN_DIR:-}" ]; then printf '%s\n' "$CLAN_BIN_DIR"; return; fi
  for d in /usr/local/bin "$HOME/.local/bin" "$HOME/bin"; do
    [ -d "$d" ] && [ -w "$d" ] && { printf '%s\n' "$d"; return; }
  done
  # Nothing writable exists yet — create the conventional user bindir.
  printf '%s\n' "$HOME/.local/bin"
}
BIN_DIR="$(pick_bin_dir)"

OS="$(uname -s)"
ARCH="$(uname -m)"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
say()  { printf '  %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*"; }
die()  { printf '\nerror: %s\n' "$*" >&2; exit 1; }

case "$OS" in
  Darwin) case "$ARCH" in
            arm64)  TARGET="aarch64-apple-darwin" ;;
            x86_64) TARGET="x86_64-apple-darwin" ;;
            *) die "unsupported macOS architecture: $ARCH" ;;
          esac ;;
  Linux)  case "$ARCH" in
            x86_64) TARGET="x86_64-unknown-linux-gnu" ;;
            *) die "unsupported Linux architecture: $ARCH" ;;
          esac ;;
  *) die "unsupported OS: $OS (Windows: download the .msi from https://github.com/$REPO/releases)" ;;
esac

bold "Napkin Studio OS installer"
say "repo    $REPO"
say "system  $OS / $ARCH"

# ── resolve the release ───────────────────────────────────────────────────────
VERSION="${CLAN_VERSION:-}"
API_BASE="https://api.github.com/repos/${REPO}/releases"
if [ -n "$VERSION" ]; then
  API_JSON="$(curl -fsSL "${API_BASE}/tags/v${VERSION}" || true)"
else
  API_JSON="$(curl -fsSL "${API_BASE}/latest" || true)"
  VERSION="$(printf '%s' "$API_JSON" \
    | sed -nE 's/.*"tag_name"[[:space:]]*:[[:space:]]*"v?([^"]+)".*/\1/p' | head -n1)"
fi
[ -n "${API_JSON:-}" ] || die "could not reach the GitHub API for $REPO"
[ -n "$VERSION" ] || die "no release found for $REPO. Set CLAN_VERSION to pin one."
say "version $VERSION"

# Every download URL in the release, one per line — matched by pattern below.
# grep/sed rather than python3: on macOS python3 can be a Command Line Tools
# stub, and the installer must not depend on the thing it is about to check.
ASSETS="$(printf '%s' "$API_JSON" \
  | grep -oE '"browser_download_url"[[:space:]]*:[[:space:]]*"[^"]+"' \
  | sed -E 's/.*"(https[^"]+)"/\1/')"
asset() { printf '%s\n' "$ASSETS" | grep -E "$1" | head -n1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fetch() { # url dest
  curl -fsSL --progress-bar "$1" -o "$2" || die "download failed: $1"
}

install_to_bindir() { # src name
  mkdir -p "$BIN_DIR" 2>/dev/null || true
  if [ -w "$BIN_DIR" ]; then install -m 755 "$1" "$BIN_DIR/$2"
  else
    # Only reachable when CLAN_BIN_DIR names a directory we cannot write.
    # Read the password from the terminal explicitly: stdin is the piped script.
    say "sudo needed to write $BIN_DIR"
    if [ -r /dev/tty ]; then
      sudo -p "password for %u (to write $BIN_DIR): " \
        install -m 755 "$1" "$BIN_DIR/$2" < /dev/tty \
        || die "could not write $BIN_DIR.
  Re-run pointing at a directory you own:
    CLAN_BIN_DIR=\"\$HOME/.local/bin\" ... | bash"
    else
      die "$BIN_DIR needs root, and there is no terminal to ask for a password on
  (stdin is the piped script). Either:
    - install somewhere you own:  CLAN_BIN_DIR=\"\$HOME/.local/bin\" ... | bash
    - or download first, then run:  curl -fsSL -O <url> && bash install-napkin.sh"
    fi
  fi
  [ "$OS" = "Darwin" ] && xattr -d com.apple.quarantine "$BIN_DIR/$2" 2>/dev/null || true
}

# ── 1. the CLI ───────────────────────────────────────────────────────────────
bold "1/5  clan CLI"
CLI_URL="$(asset "clan-v?${VERSION}-${TARGET}\.tar\.gz$")"
[ -n "$CLI_URL" ] || CLI_URL="$(asset "clan-.*${TARGET}\.tar\.gz$")"
[ -n "$CLI_URL" ] || die "no CLI asset for $TARGET in release v$VERSION"
fetch "$CLI_URL" "$TMP/cli.tar.gz"
tar -xzf "$TMP/cli.tar.gz" -C "$TMP"
CLI_BIN="$(find "$TMP" -type f -name clan -perm -u+x | head -n1)"
[ -n "$CLI_BIN" ] || die "no clan binary inside $(basename "$CLI_URL")"
install_to_bindir "$CLI_BIN" clan
say "installed $BIN_DIR/clan ($("$BIN_DIR/clan" --version 2>/dev/null || echo '?'))"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on your PATH. Add it:"
     warn "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc && exec zsh" ;;
esac
# An older clan earlier in PATH silently wins over the one we just installed.
SHADOW="$(command -v clan 2>/dev/null || true)"
if [ -n "$SHADOW" ] && [ "$SHADOW" != "$BIN_DIR/clan" ]; then
  warn "another clan is ahead of it on PATH: $SHADOW ($("$SHADOW" --version 2>/dev/null || echo '?'))"
fi

# ── 2. the desktop app ───────────────────────────────────────────────────────
APP_PATH=""
if [ "${NAPKIN_NO_APP:-}" = "1" ]; then
  bold "2/5  desktop app — skipped (NAPKIN_NO_APP=1)"
elif [ "$OS" = "Darwin" ]; then
  bold "2/5  desktop app"
  # Prefer the .app tarball over the DMG: no hdiutil mount/detach dance, and
  # nothing left attached if the script is interrupted.
  APP_URL="$(asset "\.app\.tar\.gz$")"
  if [ -n "$APP_URL" ]; then
    fetch "$APP_URL" "$TMP/app.tar.gz"
    mkdir -p "$TMP/appx" && tar -xzf "$TMP/app.tar.gz" -C "$TMP/appx"
    SRC_APP="$(find "$TMP/appx" -maxdepth 2 -name '*.app' | head -n1)"
  else
    APP_URL="$(asset "\.dmg$")" || true
    [ -n "$APP_URL" ] || die "no .app.tar.gz or .dmg in release v$VERSION"
    fetch "$APP_URL" "$TMP/app.dmg"
    MNT="$(mktemp -d)"
    hdiutil attach -nobrowse -quiet -mountpoint "$MNT" "$TMP/app.dmg" \
      || die "could not mount $(basename "$APP_URL")"
    SRC_APP="$(find "$MNT" -maxdepth 2 -name '*.app' | head -n1)"
    [ -n "$SRC_APP" ] && cp -R "$SRC_APP" "$TMP/" && SRC_APP="$TMP/$(basename "$SRC_APP")"
    hdiutil detach -quiet "$MNT" || true
    rmdir "$MNT" 2>/dev/null || true
  fi
  [ -n "${SRC_APP:-}" ] || die "no .app found in the downloaded bundle"
  APP_NAME="$(basename "$SRC_APP")"
  APP_PATH="/Applications/$APP_NAME"
  rm -rf "$APP_PATH" 2>/dev/null || sudo rm -rf "$APP_PATH" 2>/dev/null || true
  cp -R "$SRC_APP" /Applications/ 2>/dev/null || sudo cp -R "$SRC_APP" /Applications/ \
    || die "could not copy $APP_NAME to /Applications"
  # The whole reason a hand-held install beats a DMG drag: clear the quarantine
  # flag here, so the recipient never meets Gatekeeper's "unidentified
  # developer" dialog. The bundles are not signed or notarized.
  xattr -dr com.apple.quarantine "$APP_PATH" 2>/dev/null \
    || sudo xattr -dr com.apple.quarantine "$APP_PATH" 2>/dev/null || true
  say "installed $APP_PATH"
else
  bold "2/5  desktop app"
  APP_URL="$(asset "\.AppImage$")" || true
  if [ -n "$APP_URL" ]; then
    mkdir -p "$HOME/Applications"
    APP_PATH="$HOME/Applications/$(basename "$APP_URL")"
    fetch "$APP_URL" "$APP_PATH"; chmod +x "$APP_PATH"
    say "installed $APP_PATH"
  else
    warn "no AppImage in this release — skipping the app"
  fi
fi

# ── 3. the agent + its knowledge ─────────────────────────────────────────────
bold "3/5  agent + knowledge"
AGENT_DIR="$NAPKIN_HOME/agent"
mkdir -p "$AGENT_DIR"
AGENT_URL="$(asset "napkin-agent.*\.zip$")" || true
if [ -n "$AGENT_URL" ]; then
  fetch "$AGENT_URL" "$TMP/agent.zip"
  rm -rf "$TMP/agentx" && mkdir -p "$TMP/agentx"
  unzip -q "$TMP/agent.zip" -d "$TMP/agentx"
  # No maxdepth: the zip wraps everything in a napkin-agent-<version>/ directory,
  # so server.py sits at agentx/napkin-agent-X/mock-agent/server.py — depth 3.
  # Guard the result too: an empty find fed `cd "$(dirname "")/.."` resolves to
  # the parent of the CWD rather than failing, which turns a wrong layout into a
  # confusing error somewhere else.
  found="$(find "$TMP/agentx" -name server.py -path '*mock-agent*' | head -n1)"
  [ -n "$found" ] || die "$(basename "$AGENT_URL") has no mock-agent/server.py"
  SRC="$(cd "$(dirname "$found")/.." && pwd)"
  say "from release asset $(basename "$AGENT_URL")"
else
  # No packaged agent in this release. Fall back to source: the tag first, then
  # a branch — a tag cut before the agent landed simply does not contain it
  # (v1.1.8, for one, has no mock-agent/ at all), so the tag alone is not enough.
  say "no agent asset in the release — falling back to source"
  SRC=""
  for ref in "refs/tags/v${VERSION}" "refs/heads/${CLAN_REF:-main}"; do
    rm -rf "$TMP/srcx"; mkdir -p "$TMP/srcx"
    curl -fsSL "https://codeload.github.com/${REPO}/tar.gz/${ref}" -o "$TMP/src.tar.gz" 2>/dev/null \
      || { say "  $ref — not available"; continue; }
    tar -xzf "$TMP/src.tar.gz" -C "$TMP/srcx" 2>/dev/null || continue
    cand="$(find "$TMP/srcx" -maxdepth 1 -mindepth 1 -type d | head -n1)"
    if [ -n "$cand" ] && [ -f "$cand/mock-agent/server.py" ]; then
      SRC="$cand"; say "  $ref — has the agent"; break
    fi
    say "  $ref — no mock-agent/ (predates it)"
  done
  [ -n "$SRC" ] || die "this release has no agent bundle, and neither v${VERSION} nor
  branch '${CLAN_REF:-main}' of ${REPO} contains mock-agent/server.py.

  Fix one of these:
    - cut a release from a branch that has the agent (it will then ship
      napkin-agent-<version>.zip and this fallback is not needed), or
    - point at the right branch:  CLAN_REF=<branch> ... | bash"
fi

# Copy only what the agent needs, preserving the layout it resolves relative to
# __file__ (so no env vars are needed to find the knowledge).
copied=0
mkdir -p "$AGENT_DIR/mock-agent"
[ -f "$SRC/mock-agent/server.py" ] || die "server.py missing from the download"
cp "$SRC/mock-agent/server.py" "$AGENT_DIR/mock-agent/"
for d in "$SRC"/engine/packs_dist/*/digest.md; do
  [ -f "$d" ] || continue
  p="$(basename "$(dirname "$d")")"
  mkdir -p "$AGENT_DIR/engine/packs_dist/$p"; cp "$d" "$AGENT_DIR/engine/packs_dist/$p/"
  copied=$((copied + 1))
done
mkdir -p "$AGENT_DIR/engine/golden-brief" "$AGENT_DIR/engine/reference/betterbriefs"
for f in "$SRC"/engine/golden-brief/*.md "$SRC"/engine/golden-brief/*.json; do
  [ -f "$f" ] && cp "$f" "$AGENT_DIR/engine/golden-brief/"
done
for f in "$SRC"/engine/reference/betterbriefs/*; do
  [ -f "$f" ] && cp "$f" "$AGENT_DIR/engine/reference/betterbriefs/"
done
say "installed $AGENT_DIR  ($copied knowledge digest(s))"
[ "$copied" -gt 0 ] || warn "no digests found — drafts would be ungrounded"

printf 'repo=%s\nversion=%s\napp=%s\nagent=%s\n' \
  "$REPO" "$VERSION" "\"${APP_PATH:-}\"" "\"$AGENT_DIR\"" > "$NAPKIN_HOME/config"

# ── 4. the app library ───────────────────────────────────────────────────────
# Napkin Studio lists whatever it finds in <app-data>/apps/<app_id>/app.clan.
# Nothing ever installed Brief Maker there, so a fresh machine opened to an
# empty library and the flagship app simply did not exist. It looked fine on any
# machine that had built the template by hand at some point, because that
# directory outlives installing and uninstalling the app.
bold "4/5  app library"
case "$OS" in
  Darwin) APPS_DIR="${NAPKIN_APPS_DIR:-$HOME/Library/Application Support/ie.napkin.studio/apps}" ;;
  *)      APPS_DIR="${NAPKIN_APPS_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/ie.napkin.studio/apps}" ;;
esac
BM_DIR="$APPS_DIR/ie.napkin.brief-maker"

install_brief_maker() {
  # A packaged template in the release is the cheap path — no toolchain needed.
  BM_URL="$(asset "brief-maker.*\.clan$")" || true
  if [ -n "$BM_URL" ]; then
    mkdir -p "$BM_DIR"
    fetch "$BM_URL" "$BM_DIR/app.clan"
    say "installed Brief Maker from $(basename "$BM_URL")"
    return 0
  fi
  # Otherwise build it from source, which needs cargo.
  if command -v cargo >/dev/null 2>&1; then
    say "no template asset in the release — building from source (takes a minute)"
    BM_SRC=""
    for ref in "refs/tags/v${VERSION}" "refs/heads/${CLAN_REF:-main}"; do
      rm -rf "$TMP/bmsrc"; mkdir -p "$TMP/bmsrc"
      curl -fsSL "https://codeload.github.com/${REPO}/tar.gz/${ref}" -o "$TMP/bm.tar.gz" 2>/dev/null || continue
      tar -xzf "$TMP/bm.tar.gz" -C "$TMP/bmsrc" 2>/dev/null || continue
      cand="$(find "$TMP/bmsrc" -maxdepth 1 -mindepth 1 -type d | head -n1)"
      [ -n "$cand" ] && [ -d "$cand/app/templates/brief-maker" ] && { BM_SRC="$cand"; break; }
    done
    if [ -n "$BM_SRC" ] && ( cd "$BM_SRC" && cargo run -q -p clan-sdk --example make_brief_maker -- "$TMP/bm.app.clan" >/dev/null 2>&1 ); then
      mkdir -p "$BM_DIR"; cp "$TMP/bm.app.clan" "$BM_DIR/app.clan"
      say "built and installed Brief Maker"
      return 0
    fi
    warn "could not build the template from source"
  fi
  return 1
}

if [ -f "$BM_DIR/app.clan" ] && [ "${NAPKIN_FORCE_APPS:-}" != "1" ]; then
  say "Brief Maker already in the library — left alone (NAPKIN_FORCE_APPS=1 to replace)"
elif install_brief_maker; then
  say "library $APPS_DIR"
else
  warn "Brief Maker is NOT installed — the app will open with an empty library."
  warn "From a checkout of this repo:"
  warn "  cargo run -p clan-sdk --example make_brief_maker -- bm.clan"
  warn "  mkdir -p '$BM_DIR' && cp bm.clan '$BM_DIR/app.clan'"
fi

# ── 4. the launcher ──────────────────────────────────────────────────────────
bold "5/5  napkin launcher"
cat > "$TMP/napkin" <<'LAUNCHER'
#!/usr/bin/env bash
# Start, stop and inspect the Napkin agent. Installed by install-napkin.sh.
set -uo pipefail

NAPKIN_HOME="${NAPKIN_HOME:-$HOME/.napkin}"
AGENT_DIR="$NAPKIN_HOME/agent"
LOG="$NAPKIN_HOME/agent.log"
PIDF="$NAPKIN_HOME/agent.pid"
PORT="${NAPKIN_MOCK_PORT:-8787}"
MODE="${NAPKIN_RETRIEVE:-agentic}"
PLIST="$HOME/Library/LaunchAgents/ie.napkin.agent.plist"

[ -f "$NAPKIN_HOME/config" ] && . "$NAPKIN_HOME/config" 2>/dev/null || true

die() { printf 'napkin: %s\n' "$1" >&2; exit 1; }

listening() {  # something already bound to $PORT?
  python3 - "$PORT" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket(); s.settimeout(0.4)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

preflight() {
  command -v python3 >/dev/null 2>&1 || die "python3 not found. macOS: xcode-select --install"
  python3 -c 'import sys' >/dev/null 2>&1 \
    || die "python3 is a stub, not a working interpreter. Run: xcode-select --install"
  command -v claude >/dev/null 2>&1 || die "Claude Code not found.
  Install it from https://claude.ai/code, then run 'claude' once to log in.
  Every draft uses YOUR Claude login — there is no API key involved."
  [ -f "$AGENT_DIR/mock-agent/server.py" ] || die "agent not installed at $AGENT_DIR"
}

cmd_start() {
  preflight
  if listening; then echo "napkin: already running on :$PORT"; return 0; fi
  mkdir -p "$NAPKIN_HOME"
  # `exec` inside a single backgrounded subshell, so the recorded PID *is* the
  # interpreter. Written as `cd && nohup py &` the `&` bound the whole AND-list:
  # bash forked a subshell, ran python as its child, and $! captured the
  # subshell — one lower than the real process. `napkin stop` then killed the
  # wrapper, reported "stopped", removed the pidfile, and left the agent serving.
  ( cd "$AGENT_DIR" && exec env NAPKIN_RETRIEVE="$MODE" NAPKIN_MOCK_PORT="$PORT" \
      python3 -u mock-agent/server.py >"$LOG" 2>&1 ) &
  echo $! > "$PIDF"
  for _ in $(seq 1 40); do
    listening && { echo "napkin: agent up on :$PORT (mode=$MODE)  log: $LOG"; return 0; }
    sleep 0.25
  done
  echo "napkin: agent did not come up — last lines of $LOG:" >&2
  tail -n 15 "$LOG" >&2; return 1
}

cmd_stop() {
  killed=""
  if [ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2>/dev/null; then killed=1; fi
  rm -f "$PIDF"
  # Never trust the kill alone: a stale or wrong pidfile made `stop` claim
  # success while the agent kept listening. The port is the ground truth.
  for _ in 1 2 3 4 5 6 7 8; do listening || break; sleep 0.25; done
  if listening; then
    pkill -f 'mock-agent/server.py' 2>/dev/null && killed=1
    for _ in 1 2 3 4 5 6 7 8; do listening || break; sleep 0.25; done
  fi
  if listening; then
    echo "napkin: something is STILL listening on :$PORT — inspect it:" >&2
    echo "  lsof -nP -iTCP:$PORT -sTCP:LISTEN" >&2
    return 1
  fi
  [ -n "$killed" ] && echo "napkin: stopped" || echo "napkin: not running"
}

cmd_status() {
  if listening; then
    echo "agent   running on :$PORT"
    command -v curl >/dev/null && curl -s -m 2 "http://127.0.0.1:$PORT/stats" && echo
  else
    echo "agent   not running   (napkin start)"
  fi
  echo "agent dir $AGENT_DIR"
  echo "digests   $(ls -1 "$AGENT_DIR"/engine/packs_dist/*/digest.md 2>/dev/null | wc -l | tr -d ' ')"
  echo "app       ${app:-not recorded}"
}

cmd_open() {
  cmd_start || return 1
  if [ -n "${app:-}" ] && [ -e "${app:-}" ]; then open "$app" 2>/dev/null && return 0; fi
  # Fall back to a glob: the app has been published under more than one name.
  for a in /Applications/*Napkin*.app /Applications/*CLAN*.app; do
    [ -e "$a" ] && { open "$a"; return 0; }
  done
  [ -n "${app:-}" ] && [ -x "${app:-}" ] && { "$app" & return 0; }   # Linux AppImage
  echo "napkin: app not found — install it or open it yourself" >&2; return 1
}

cmd_autostart() {
  [ "$(uname -s)" = "Darwin" ] || die "autostart is macOS-only"
  case "${1:-}" in
    on)
      mkdir -p "$(dirname "$PLIST")"
      cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ie.napkin.agent</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>-lc</string>
         <string>cd '$AGENT_DIR' &amp;&amp; NAPKIN_RETRIEVE=$MODE NAPKIN_MOCK_PORT=$PORT exec python3 -u mock-agent/server.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PL
      launchctl unload "$PLIST" 2>/dev/null || true
      launchctl load "$PLIST" && echo "napkin: autostart ON (starts at login)"
      ;;
    off)
      launchctl unload "$PLIST" 2>/dev/null || true
      rm -f "$PLIST"; echo "napkin: autostart OFF" ;;
    *) echo "usage: napkin autostart on|off" >&2; return 2 ;;
  esac
}

case "${1:-open}" in
  start)     cmd_start ;;
  stop)      cmd_stop ;;
  restart)   cmd_stop; cmd_start ;;
  status)    cmd_status ;;
  logs)      tail -f "$LOG" ;;
  open|app)  cmd_open ;;
  autostart) shift; cmd_autostart "${1:-}" ;;
  -h|--help|help)
    cat <<USAGE
napkin — run the Napkin agent

  napkin            start the agent (if needed) and open the app
  napkin start      start the agent in the background
  napkin stop       stop it
  napkin restart    stop then start
  napkin status     is it running, what knowledge does it have, session cost
  napkin logs       follow the agent log
  napkin autostart on|off     run the agent at login (macOS)

Env: NAPKIN_MOCK_PORT (8787)  NAPKIN_RETRIEVE (agentic|digest)
USAGE
    ;;
  *) echo "napkin: unknown command '${1}' (try: napkin help)" >&2; exit 2 ;;
esac
LAUNCHER
install_to_bindir "$TMP/napkin" napkin
say "installed $BIN_DIR/napkin"

# ── what's still missing ─────────────────────────────────────────────────────
echo
bold "Installed."
MISSING=0
if ! command -v claude >/dev/null 2>&1; then
  MISSING=1
  warn "Claude Code is NOT installed — the agent needs it for every draft."
  say "    Install: https://claude.ai/code   then run 'claude' once to log in."
fi
if ! python3 -c 'import sys' >/dev/null 2>&1; then
  MISSING=1
  warn "python3 is not usable (macOS ships a Command Line Tools stub)."
  say "    Fix: xcode-select --install"
fi
echo
if [ "$MISSING" = "1" ]; then
  bold "Finish the two items above, then run:  napkin"
else
  bold "Run:  napkin"
fi
say "That starts the agent and opens the app. 'napkin status' to check, 'napkin help' for more."
echo
say "Drafts run on your own Claude account (~\$0.30-0.40 each, ~90s)."
