#!/usr/bin/env bash
# Build a self-contained agent bundle you can send to someone.
#
#   ./package-handoff.sh                 # -> dist/napkin-agent-<version>.zip
#
# The desktop app ships the viewer only: the agent server and the knowledge it
# grounds on are NOT in the .app bundle (bundle.externalBin is empty, and
# bundle.resources carries just LICENSE/NOTICE). Sending only the installer
# therefore produces a viewer with no agent, and sending only server.py produces
# an agent with no knowledge — it starts, and is silently ungrounded.
#
# So this bundles the three things that have to travel together, in the repo
# layout the server expects (it resolves the digests and corpus relative to
# __file__, so no env vars are needed on the far side).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(sed -nE 's/^version = "([^"]+)".*/\1/p' "$ROOT/Cargo.toml" | head -n1)"
VERSION="${VERSION:-0.0.0}"
OUT="$ROOT/dist"
STAGE="$(mktemp -d)"
BUNDLE="napkin-agent-${VERSION}"
trap 'rm -rf "$STAGE"' EXIT

say() { printf '  %s\n' "$*"; }

echo "Packaging napkin agent handoff v${VERSION}"

D="$STAGE/$BUNDLE"
mkdir -p "$D/mock-agent" "$D/engine/packs_dist" \
         "$D/engine/golden-brief" "$D/engine/reference/betterbriefs"

# ── 1. the server ─────────────────────────────────────────────────────────────
cp "$ROOT/mock-agent/server.py" "$D/mock-agent/"
say "server:    mock-agent/server.py"

# ── 2. the knowledge: pack digests (award-corpus patterns, paraphrased) ───────
# Only digest.md — .digest_state.json is regeneration bookkeeping.
digests=0
for d in "$ROOT"/engine/packs_dist/*/digest.md; do
  [ -f "$d" ] || continue
  pack="$(basename "$(dirname "$d")")"
  mkdir -p "$D/engine/packs_dist/$pack"
  cp "$d" "$D/engine/packs_dist/$pack/"
  digests=$((digests + 1))
done
say "digests:   $digests pack(s)"
[ "$digests" -gt 0 ] || echo "  WARNING: no digests found — the bundle will be ungrounded." >&2

# ── 3. the corpus for agentic retrieval (docs we own outright) ───────────────
# Extensions match server.py's _CORPUS_EXT; the .html design references in
# golden-brief/ are deliberately left out (never staged, just weight).
corpus=0
for f in "$ROOT"/engine/golden-brief/*.md "$ROOT"/engine/golden-brief/*.json; do
  [ -f "$f" ] && { cp "$f" "$D/engine/golden-brief/"; corpus=$((corpus + 1)); }
done
for f in "$ROOT"/engine/reference/betterbriefs/*; do
  [ -f "$f" ] && { cp "$f" "$D/engine/reference/betterbriefs/"; corpus=$((corpus + 1)); }
done
say "corpus:    $corpus doc(s)"

# ── 4. licences (MPL-2.0 — ship them with the code) ──────────────────────────
cp "$ROOT/LICENSE" "$ROOT/NOTICE" "$D/" 2>/dev/null || true

# ── 5. the launcher ──────────────────────────────────────────────────────────
cat > "$D/start-agent.command" <<'LAUNCHER'
#!/usr/bin/env bash
# Double-click this on macOS. On Linux: bash start-agent.command
# Preflight first — every failure below is one a recipient would otherwise hit
# as an unexplained blank panel in the app.
cd "$(dirname "$0")" || exit 1

PORT="${NAPKIN_MOCK_PORT:-8787}"
MODE="${NAPKIN_RETRIEVE:-agentic}"

echo "Napkin agent — starting up"
echo

fail() { echo; echo "✗ $1"; echo; echo "Press return to close."; read -r _; exit 1; }

# 1. a working python3. On macOS /usr/bin/python3 can be a Command Line Tools
#    stub that prompts a download instead of running.
command -v python3 >/dev/null 2>&1 || fail "python3 not found.
  macOS: run  xcode-select --install  then try again."
python3 -c 'import sys' >/dev/null 2>&1 || fail "python3 is present but not working.
  macOS: run  xcode-select --install  (it is a Command Line Tools stub)."
echo "  ✓ python3 $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"

# 2. the Claude Code CLI, logged in. The agent calls it for every draft, using
#    THIS machine's login — there is no key in the bundle.
command -v claude >/dev/null 2>&1 || fail "Claude Code not found.
  Install it from https://claude.ai/code, run  claude  once to log in, then try again."
echo "  ✓ claude $(claude --version 2>/dev/null | head -n1)"

# 3. port free — otherwise the bind fails with a raw Python traceback. Tested
#    with python3 rather than lsof: lsof is always on macOS but not on every
#    Linux, and a missing tool must not silently skip the check.
if ! python3 - "$PORT" <<'PORTCHECK'
import socket, sys
s = socket.socket(); s.settimeout(0.4)
# connect_ex == 0 means something is already listening there.
sys.exit(1 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 0)
PORTCHECK
then
  fail "Port $PORT is already in use — an agent may already be running.
  Close it, or start this one elsewhere:  NAPKIN_MOCK_PORT=8788 bash start-agent.command
  (If you change the port, point the app at it too.)"
fi
echo "  ✓ port $PORT free"

# 4. knowledge present — the whole point of the bundle.
d=$(ls -1 engine/packs_dist/*/digest.md 2>/dev/null | wc -l | tr -d ' ')
[ "$d" -gt 0 ] || echo "  ! no digests found — drafts will be ungrounded"
[ "$d" -gt 0 ] && echo "  ✓ $d knowledge digest(s)"

echo
echo "Leave this window open while you use the app. Ctrl-C to stop."
echo "Grounding mode: $MODE  (NAPKIN_RETRIEVE=digest for a leaner, cheaper run)"
echo
NAPKIN_RETRIEVE="$MODE" NAPKIN_MOCK_PORT="$PORT" exec python3 -u mock-agent/server.py
LAUNCHER
chmod +x "$D/start-agent.command"
say "launcher:  start-agent.command"

# ── 6. instructions for whoever receives it ──────────────────────────────────
cat > "$D/README.txt" <<'READIT'
Napkin agent — how to run this
==============================

The desktop app is the viewer. This folder is the agent that writes the briefs.
You need both, plus your own Claude Code login.

WHAT YOU NEED FIRST
  1. Claude Code, installed and logged in.  https://claude.ai/code
     Run `claude` once in a terminal and sign in. Every draft uses YOUR login —
     there is no API key in this folder, and nothing here bills anyone else.
  2. The Napkin Studio OS app.
  3. python3. macOS: if you get a Command Line Tools prompt, accept it, or run
     xcode-select --install

START THE AGENT
  Double-click  start-agent.command

  macOS will likely refuse the first time ("cannot be opened because it is from
  an unidentified developer") because this folder came off the internet. Either:
    - right-click start-agent.command -> Open -> Open, or
    - run this once in Terminal, in this folder:   xattr -cr .

  It checks its dependencies, then prints "Napkin mock agent on http://localhost:8787".
  Leave the window open while you work. Ctrl-C stops it.

THEN OPEN THE APP
  The app looks for the agent on localhost:8787 by default — no configuration.
  Open Brief Maker, paste or attach a brief, press Generate.

  A draft takes roughly 90 seconds and costs about $0.30-0.40 against your own
  Claude account. Regenerating a single field takes about 10 seconds.

IF THE APP SAYS THERE IS NO AGENT
  The start-agent window is closed, still starting, or reported an error. Look
  at that window first — it says what is wrong.

WHAT GROUNDS THE BRIEFS
  engine/packs_dist/    paraphrased pattern notes distilled from award
                        effectiveness corpora (IPA / Cannes / D&AD) and
                        planning playbooks. Every draft reads these.
  engine/golden-brief/, engine/reference/betterbriefs/
                        brief-craft methodology and a scoring rubric. In the
                        default "agentic" mode the agent searches these per
                        brief and cites them by file and line.

  These are craft and structure rules, not campaign case studies. The award
  corpora themselves are licensed and are not in this bundle.

Licensed under MPL-2.0 — see LICENSE and NOTICE.
READIT
say "readme:    README.txt"

# ── 7. zip it ────────────────────────────────────────────────────────────────
mkdir -p "$OUT"
ZIP="$OUT/${BUNDLE}.zip"
rm -f "$ZIP"
(cd "$STAGE" && zip -qr "$ZIP" "$BUNDLE")

echo
echo "✓ $ZIP  ($(du -h "$ZIP" | cut -f1))"
echo
echo "Send that zip. The recipient needs Claude Code logged in, python3, and the app."
echo "Remind them: macOS quarantines downloads — right-click start-agent.command -> Open,"
echo "or 'xattr -cr .' in the unzipped folder."
