#!/usr/bin/env sh
# The removability check (contract 5, §0.1): the middleware must not know the
# mock backend exists. Every port is one base-URL setting; moving a port from
# the mock to the real service is changing that value, and there is no mock
# branch, flag, port number or `if` in server/napkin to delete.
#
#   sh mock-backend/tests/removability.sh        # from anywhere; exit 0 = clean
set -eu
root="$(cd "$(dirname "$0")/../.." && pwd)"
if [ ! -d "$root/server/napkin" ]; then
  echo "removability: no server/napkin - nothing to check"
  exit 0
fi
if grep -rniE 'mock|8797|8791|8792|claude -p' "$root/server/napkin"; then
  echo "removability: FAIL - server/napkin references the mock (lines above)" >&2
  exit 1
fi
echo "removability: clean"
