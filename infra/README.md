# Napkin on AWS — stage 1: the data plane

Terraform for the stores Napkin's research and RAG land in, before any service
that uses them. It follows foundation-spec **S3** (a database per agency, the
category layer shared) and **S8** (a vector store per agency, so isolation is a
deployment boundary, not a filter).

| Store | Holds | Contract |
|---|---|---|
| **Aurora PostgreSQL** (Serverless v2), database `napkin_category` | The **category** layer: facts shared by every agency, their sources and decisions, the category tree | `napkin.layers/1`: `docs/contracts/peripherals.md` §4 |
| Same cluster, database `napkin_agency_<slug>` per agency | That agency's **brand** layer (RLS on `brand_id` inside it), roster, decisions, and its RAG overrides | same |
| **RAG host**: Qdrant store `house` | The licensed corpus (Cannes, D&AD, IPA, Effie, playbooks, templates). **Shared, read-only**: retrieval holds only a read-only key | `napkin.retrieval/1`: §3 |
| RAG host: Qdrant store `<agency>` per agency | That agency's **learnings** only: its own material and its locked briefs | same |
| **S3** | `corpus` (ingestion input), `blobs` (document assets by hash), `backups` | — |

A search reads **house + the caller's agency store** and merges by rank, so
every agency sees the house corpus without anyone storing a copy. A house
update reaches everyone at once.

Nothing here is public. The stores sit in private subnets, and you reach them
through SSM Session Manager on the admin host: no SSH keys, no open ports.

```
infra/
  bootstrap/            the Terraform state bucket (once per account)
  modules/
    network/            VPC, 2 AZs, one NAT, S3 endpoint, private zone napkin.internal
    aurora/             cluster + writer, pauses at 0 ACU, TLS forced, RDS-managed master secret
    tenant-databases/   napkin_category + napkin_agency_<slug>: app credentials and the list
    rag-host/           one EC2 host, one Qdrant container per store, each on its own
                        EBS volume, key and port; reconcile.sh keeps the host in line
    storage/            the three buckets: KMS, versioned, TLS-only, no public access
    admin-host/         SSM-only host with psql and uv, for migrations and ingestion
  scripts/
    provision-databases.sh   creates the databases and roles inside Aurora
  envs/staging/         wires the modules together
```

## Running it

Prerequisites: Terraform ≥ 1.10, the AWS CLI v2, the
[Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html),
and credentials for the target account with admin rights (`aws sts get-caller-identity`).

```sh
# 1. State bucket, once per account
cd infra/bootstrap
terraform init && terraform apply          # prints state_bucket

# 2. The stack
cd ../envs/staging
cp backend.hcl.example backend.hcl         # put the bucket name in
cp terraform.tfvars.example terraform.tfvars
terraform init -backend-config=backend.hcl
terraform plan -out=stage1.tfplan          # read it
terraform apply stage1.tfplan              # ~15 min, Aurora is most of it

# 3. The databases inside Aurora
$(terraform output -raw provision_databases)
```

Before `apply`, check that the region offers the engine version:
`aws rds describe-db-engine-versions --engine aurora-postgresql --query 'DBEngineVersions[].EngineVersion'`.
Pausing at 0 ACU needs Aurora PostgreSQL 16.3 or later (or 15.7, 14.12, 13.15).

### Claude on Amazon Bedrock

Model calls go to Claude in Amazon Bedrock (`NAPKIN_MODEL_API=bedrock`, the
`bedrock-runtime` endpoint by default; `docs/contracts/peripherals.md` §1.3a),
signed with the caller's IAM role. The stack creates `napkin-<env>-model-invoke`
(`bedrock:InvokeModel` on the Claude `eu.`/`global.` inference profiles, plus
the Mantle action for the optional endpoint) and attaches it to the admin
host, where seeding runs; stage 2 services attach the same policy
(`terraform output model_invoke_policy_arn`).

Once per account and per model, outside Terraform (the console's model catalog
has no Enable button any more):

1. **Agreement** ($0, pay per token), accepted by the account:
   ```sh
   tok=$(aws bedrock list-foundation-model-agreement-offers --model-id anthropic.claude-opus-5-5 \
         --query 'offers[0].offerToken' --output text)
   aws bedrock create-foundation-model-agreement --model-id anthropic.claude-opus-5-5 --offer-token "$tok"
   ```
   Check: `aws bedrock get-foundation-model-availability --model-id <id>` shows
   `agreementAvailability.status: AVAILABLE`.
2. **Quota above 0.** A new account can start a model at 0 tokens per minute
   (the call then fails `403 … not available for this account` even with the
   agreement). Service Quotas → Amazon Bedrock → "Global cross-region model
   inference tokens per minute for Anthropic Claude <model>" → request the
   default. Free.
3. **Prove it** with one call (a few hundred tokens):
   `cd server && NAPKIN_MODEL_API=bedrock NAPKIN_MODEL_REGION=eu-west-1 NAPKIN_MODEL=<profile id> uv run python scripts/model_smoke.py`

Bedrock usage is billed per token on the AWS bill, outside the table below;
the budget counts it, since the budget is account-wide. Whether promotional
credits cover it shows the next day in Cost Explorer (record type Credit
against Usage for Amazon Bedrock).

Tests run with no AWS account, against mocked providers:

```sh
cd infra/modules/rag-host && terraform init && terraform test    # ports, devices, input rules
cd infra/envs/staging && terraform init -backend=false && terraform test   # whole stack plans
```

## Agencies

Agencies are one map in `terraform.tfvars`, keyed by org slug:

```hcl
agencies = {
  acme   = { slot = 1 }
  globex = { slot = 2, volume_gb = 10 }
}
```

`slot` (1–20) fixes the agency's port (6340 + slot) and its disk's device, so
adding or removing one agency never moves another. **Never reuse a slot while
that agency's store exists.**

Each agency gets:

| | |
|---|---|
| RAG store | its own Qdrant container (memory-capped), 5 GB encrypted disk, API key, port, name `qdrant-<slug>.napkin.internal` |
| KMS key | its own (`own_kms_key`, default true): deleting it makes every copy of its vectors unreadable |
| Database | `napkin_agency_<slug>`, with `<db>_owner` (owns it) and `<db>_app` (the layers service's user, owns nothing, can connect to this database and no other) |

**Onboarding.**
1. Add the agency to the map, then `terraform apply`. This creates its disk,
   keys and database credentials. The host starts its container through SSM
   and is never rebuilt: the other stores keep running.
2. Run `$(terraform output -raw provision_databases)` to create its database.

**Restoring one agency** (e.g. its RAG to Tuesday):
1. Create a volume from that store's snapshot (tag `Store=<slug>`).
2. `docker rm -f qdrant-<slug>` on the RAG host, then unmount
   `/var/lib/qdrant/<slug>`.
3. Swap the volumes: `terraform import` the new one, or attach it by hand at
   the same device.
4. Run `napkin-rag-reconcile`.

Its database restores with `pg_dump` / `pg_restore` of `napkin_agency_<slug>`.
Neither step touches any other agency.

**Offboarding**, in this order:
1. Set `state = "retired"` and apply. Reconcile stops the container and
   unmounts the disk.
2. Remove the entry and apply. The volume is deleted, leaving a final snapshot
   under the agency's key.
3. Schedule the agency's KMS key for deletion (AWS waits 7–30 days). After
   that, every snapshot is unreadable too.
4. `DROP DATABASE napkin_agency_<slug>` from the admin host. Terraform only
   deletes the database's credentials, never the data.

Skipping step 1 detaches a mounted disk.

## Reaching the stores

```sh
terraform output -raw tunnel_postgres      # run the printed command; leave it open
aws secretsmanager get-secret-value --secret-id "$(terraform output -raw aurora_master_secret_arn)" \
  --query SecretString --output text | jq -r .password
psql "host=localhost port=5432 dbname=napkin user=napkin_admin sslmode=require"

terraform output -raw tunnel_rag_house     # then http://localhost:6333/dashboard
terraform output rag_store_urls            # every store's internal URL
```

Or open a shell on the admin host with
`aws ssm start-session --target "$(terraform output -raw admin_instance_id)"`.
From there `db.napkin.internal` and `qdrant-<store>.napkin.internal` resolve
directly.

## Things that bite

- **Aurora only pauses with no connections.** A pool that keeps idle
  connections open, a health check that queries the database, or a `psql`
  left open keeps it awake and billing. The layers service must close idle
  connections (a pool idle timeout well under `aurora_seconds_until_auto_pause`)
  and answer `/health` without a query. After a pause, the first connection
  waits up to ~15 s, so connect timeouts must be longer than that.
  `NAPKIN_LAYERS_TIMEOUT` is already 30 s.
- **One RAG host is one failure domain.** If it fails, every store is down until
  EC2 auto-recovery restarts it (minutes). Briefs still draft, without
  precedent passages. Retrieval must answer `502`, never an empty `200`.
- **Memory decides how many agencies fit.** The house container is capped at
  1.5 GB and each agency at 512 MB. Idle Qdrant memory has not been measured:
  measure before going past a handful of agencies on a t4g.medium. When an
  agency outgrows the host, move its disk to a host of its own. That needs no
  re-embedding.
- **Bumping `qdrant_version` restarts each container on the new image.** The
  host and the data stay. A reconcile-script change does not reach a running
  host on its own (user data is ignored after first boot, so the host is never
  rebuilt by accident). Replace the host deliberately with
  `terraform apply -replace=module.rag.aws_instance.rag`. The disks re-attach
  with their data.
- **Qdrant is plain HTTP inside the VPC,** protected by per-store keys and the
  security group. Add TLS before anything outside the VPC talks to it.
- **Destroy is guarded.** Aurora has deletion protection and a final snapshot,
  and every store's volume takes a final snapshot. Set
  `deletion_protection = false` first if you mean to tear staging down.
- **Backups.** Aurora keeps 7 days point-in-time. Every store's disk is
  snapshotted daily and kept 14 days.

## Rough monthly cost (eu-west-1, on-demand; check the AWS pricing calculator)

| | ~USD/mo |
|---|---|
| NAT gateway (the biggest fixed item) | 35 + data |
| RAG host t4g.medium + root + house disk | 30 |
| Each agency: 5 GB disk + snapshots + its KMS key | ~2 |
| Admin t4g.small (stop it when unused) | 13 |
| Aurora, a few agencies in working hours (~200 h × ~1 ACU), paused otherwise. Databases per agency cost nothing extra | 25–35 |
| Aurora storage, KMS, Secrets Manager, Route 53, S3 | 5–10 |
| **Total, 3 agencies** | **~115–130** |

Set `budget_emails` to get two budgets, each emailing when the forecast passes
80% or the actual passes 100%: **burn** (`monthly_budget_usd`, $150, the gross
cost the credits are paying for) and **card** (`monthly_card_budget_usd`, $5,
what is left after credits, i.e. real money).

## What comes next

1. **Layers schema** (migrations, run as `<db>_owner`):
   - port `mock-backend/layers_port.py`'s SQLite schema to Postgres with typed
     value columns;
   - in agency databases, force RLS on `brand_id`;
   - in `napkin_category`, keep the category facts, their sources and the tree
     seeded from `docs/contracts/peripherals/taxonomy.json`;
   - add the idempotency table.
2. **The layers service** on that schema, passing
   `mock-backend/contract/layers_contract.py` unchanged. It picks the agency
   database from the authenticated org. Then the middleware switches over with
   `NAPKIN_LAYERS_URL`.
3. **RAG** (with Sai, on `engine/rag` from the `ragAdded` branch):
   - reconcile `rag_io` v1.3 with `napkin.retrieval/1`;
   - route each request to its agency store by tenant, read house + agency and
     merge by RRF;
   - apply the agency's exclusion list;
   - add a write path that indexes a locked brief into its agency store. Only
     human-verified work becomes a learning;
   - move the house corpus off Qdrant Cloud with
     `rag.py migrate --from local --to qdrant`, which copies vectors and never
     re-embeds.
4. **Stage 2 compute:** web, middleware, layers and retrieval as services that
   join the `data_clients` security group.
