#!/usr/bin/env bash
# Make studio accounts in the Cognito user pool: `user@agency`, a display
# name, and a temporary password printed once (the first sign-in asks for a
# new one). Accounts that already exist are left as they are.
#
#   AWS_PROFILE=napkin ./create-users.sh <user-pool-id> accounts.tsv
#
# accounts.tsv: one account per line, `user@agency<TAB>Display name`.
# Lines starting with # are skipped.
set -euo pipefail
pool="${1:?user pool id (terraform output cognito_user_pool_id)}"
file="${2:?accounts file: user@agency<TAB>Display name}"

while IFS=$'\t' read -r user name; do
  [[ -z "${user// }" || "$user" == \#* ]] && continue
  if ! [[ "$user" =~ ^[a-z0-9._-]+@[a-z0-9][a-z0-9-]*$ ]]; then
    echo "skip: $user is not user@agency (lower case, no dot after the agency)" >&2
    continue
  fi
  if aws cognito-idp admin-get-user --user-pool-id "$pool" --username "$user" >/dev/null 2>&1; then
    echo "exists: $user"
    continue
  fi
  # 16 characters with a letter and a number: the pool's rule, and enough
  temp="Napkin-$(openssl rand -hex 6)7"
  aws cognito-idp admin-create-user \
    --user-pool-id "$pool" \
    --username "$user" \
    --temporary-password "$temp" \
    --message-action SUPPRESS \
    --user-attributes Name=name,Value="${name:-$user}" >/dev/null
  printf '%s\t%s\t(temporary; changed at first sign-in)\n' "$user" "$temp"
done < "$file"
