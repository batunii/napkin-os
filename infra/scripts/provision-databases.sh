#!/bin/bash
# Create the databases and roles Terraform listed in /<stack>/db/databases
# (foundation-spec S3): napkin_category plus one napkin_agency_<slug> per agency.
# Runs on the admin host, after every apply that adds an agency. Terraform
# publishes it as an SSM document, so from your laptop:
#
#   aws ssm send-command --document-name napkin-staging-provision-databases \
#     --targets Key=InstanceIds,Values=<admin_instance_id>
#
# or on the admin host itself: bash provision-databases.sh napkin-staging
#
# For each database:
#   <db>_owner  NOLOGIN, owns the database and, later, its tables (migrations
#               run as it via SET ROLE)
#   <db>_app    LOGIN, the layers service's user; owns nothing, so forced RLS
#               applies to it; may connect to its own database and no other
#
# Idempotent: re-running changes nothing but the app passwords, reset to
# the values in Secrets Manager. It never drops anything.
set -euo pipefail

stack=${1:?usage: provision-databases.sh <stack name, e.g. napkin-staging>}
token=$(curl -sf -X PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
region=$(curl -sf -H "X-aws-ec2-metadata-token: $token" \
  http://169.254.169.254/latest/meta-data/placement/region)

secret() {
  aws secretsmanager get-secret-value --region "$region" --secret-id "$1" \
    --query SecretString --output text
}

cfg=$(aws ssm get-parameter --region "$region" --name "/$stack/db/databases" \
  --query Parameter.Value --output text)
master=$(secret "$(jq -r .master_secret_arn <<<"$cfg")")

export PGHOST=$(jq -r .host <<<"$cfg")
export PGPORT=$(jq -r .port <<<"$cfg")
export PGUSER=$(jq -r .username <<<"$master")
export PGPASSWORD=$(jq -r .password <<<"$master")
export PGDATABASE=napkin
export PGSSLMODE=require

# Nobody but the admin connects to the cluster's default database.
psql -v ON_ERROR_STOP=1 -q -c 'REVOKE ALL ON DATABASE napkin FROM PUBLIC;'

for db in $(jq -r '.databases | keys[]' <<<"$cfg"); do
  pw=$(secret "$(jq -r --arg d "$db" '.databases[$d].secret_arn' <<<"$cfg")" | jq -r .password)
  psql -v ON_ERROR_STOP=1 -q \
    -v db="$db" -v owner="${db}_owner" -v app="${db}_app" -v pw="$pw" <<'SQL'
SELECT format('CREATE ROLE %I NOLOGIN', :'owner')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'owner') \gexec
SELECT format('CREATE ROLE %I LOGIN', :'app')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app') \gexec
ALTER ROLE :"app" PASSWORD :'pw';
GRANT :"owner" TO CURRENT_USER;
SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'owner')
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db') \gexec
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"db" TO :"app";
SQL
  echo "$db: ready"
done
