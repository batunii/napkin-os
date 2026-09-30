# The databases foundation-spec S3 asks for, inside the one Aurora cluster:
#
#   napkin_category          the category layer, shared by every agency, behind the layers API
#   napkin_agency_<slug>     one per agency: its brand layer (RLS on brand_id inside it),
#                            its RAG overrides, its decisions
#
# Separate databases buy what row-level security cannot: restoring one agency
# without touching the others (pg_dump / pg_restore of its database), and a
# deletion you can put in a contract (DROP DATABASE).
#
# Terraform cannot reach inside a private cluster, so it creates each
# database's app credentials and the list; infra/scripts/provision-databases.sh,
# run on the admin host, creates the databases and roles from that list.

terraform {
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

locals {
  databases = merge(
    { napkin_category = { kind = "category", agency = null } },
    { for a in var.agencies : "napkin_agency_${replace(a, "-", "_")}" => { kind = "agency", agency = a } },
  )
}

resource "random_password" "app" {
  for_each = local.databases
  length   = 40
  special  = false
}

resource "aws_secretsmanager_secret" "app" {
  for_each                = local.databases
  name                    = "${var.name}/db/${each.key}"
  kms_key_id              = var.kms_key_arn
  recovery_window_in_days = 7
  tags                    = { Database = each.key }
}

# The connection the layers service uses for this database. Its user owns
# nothing, so the row-level security the migrations force applies to it.
resource "aws_secretsmanager_secret_version" "app" {
  for_each  = local.databases
  secret_id = aws_secretsmanager_secret.app[each.key].id
  secret_string = jsonencode({
    engine   = "postgres"
    host     = var.host
    port     = var.port
    dbname   = each.key
    username = "${each.key}_app"
    password = random_password.app[each.key].result
    sslmode  = "verify-full"
  })
}

resource "aws_ssm_parameter" "databases" {
  name = "/${var.name}/db/databases"
  type = "String"
  value = jsonencode({
    host              = var.host
    port              = var.port
    master_secret_arn = var.master_secret_arn
    databases = { for k, d in local.databases : k => {
      kind       = d.kind
      agency     = d.agency
      secret_arn = aws_secretsmanager_secret.app[k].arn
    } }
  })
}
