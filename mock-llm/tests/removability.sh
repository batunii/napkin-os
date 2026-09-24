#!/usr/bin/env sh
# The removability test (build plan W1P-I6): the middleware must not know the
# mock exists. It constructs its client from ANTHROPIC_BASE_URL and
# ANTHROPIC_API_KEY and has no branch, so removing the mock is unsetting one
# variable and there is no code to delete.
#
#   sh mock-llm/tests/removability.sh        # from the repo root; exit 0 = clean
set -eu
root="$(cd "$(dirname "$0")/../.." && pwd)"
if [ ! -d "$root/server" ]; then
  echo "removability: no server/ yet - nothing to check"
  exit 0
fi
if grep -rniE 'mock|8788|8791|claude -p|localhost' "$root/server/"; then
  echo "removability: FAIL - server/ references the mock (lines above)" >&2
  exit 1
fi
echo "removability: clean"
