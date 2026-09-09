#!/bin/bash
# Two processes, one container: the agent on loopback, the host in front of it.
# Either one exiting takes the container down — a half-running Napkin that
# answers pages but cannot draft is worse than one the platform restarts.
set -uo pipefail

if [ -z "${ANTHROPIC_API_KEY:-}${ANTHROPIC_AUTH_TOKEN:-}" ]; then
  echo "ANTHROPIC_API_KEY is not set. This image runs the Messages API backend" >&2
  echo "and has no Claude Code CLI to fall back to." >&2
  exit 1
fi

mkdir -p "${NAPKIN_WEB_DATA:-/data}"

python3 /srv/napkin/mock-agent/server.py &
agent=$!
napkin-web &
web=$!

shutdown() {
  kill -TERM "$agent" "$web" 2>/dev/null
  wait "$agent" "$web" 2>/dev/null
  exit 0
}
trap shutdown TERM INT

# Whichever falls over first ends the wait; take the other with it.
wait -n "$agent" "$web"
code=$?
echo "entrypoint: a process exited ($code) — stopping the container" >&2
kill -TERM "$agent" "$web" 2>/dev/null
exit "${code:-1}"
