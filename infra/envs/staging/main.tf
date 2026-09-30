# Napkin's data plane, stage 1: the stores the knowledge layers and the RAG
# need, before any service that uses them.
#
#   network   VPC, private subnets, one NAT, S3 endpoint, napkin.internal
#   kms       one customer key for everything at rest
#   aurora    the knowledge layers' cluster — Postgres, pauses when idle
#   rag       the RAG host: a shared read-only house store + one learnings
#             store per agency, each its own Qdrant, disk and key (S8)
#   databases napkin_category + napkin_agency_<slug> in the cluster (S3)
#   storage   S3: corpus (ingestion input), blobs, backups
#   admin     an SSM-only host to run migrations, ingestion and psql
#
# Services (web, middleware, layers, retrieval) come in stage 2 and join the
# `data_clients` security group to reach Aurora and Qdrant.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  # Partial configuration: `terraform init -backend-config=backend.hcl`.
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "napkin", Env = var.env, ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "me" {}

locals {
  name = "napkin-${var.env}"
}

module "network" {
  source = "../../modules/network"
  name   = local.name
  region = var.region
  cidr   = var.vpc_cidr
}

resource "aws_kms_key" "data" {
  description             = "${local.name}: data at rest (Aurora, Qdrant volume, S3, secrets)"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "data" {
  name          = "alias/${local.name}-data"
  target_key_id = aws_kms_key.data.key_id
}

# Membership in this group is what grants network access to Aurora and Qdrant.
# It has no rules of its own; the stores' groups reference it.
resource "aws_security_group" "data_clients" {
  name        = "${local.name}-data-clients"
  description = "Members may reach Aurora (5432) and Qdrant (6333/6334)"
  vpc_id      = module.network.vpc_id
}

module "storage" {
  source      = "../../modules/storage"
  name        = local.name
  account_id  = data.aws_caller_identity.me.account_id
  kms_key_arn = aws_kms_key.data.arn
}

module "aurora" {
  source                   = "../../modules/aurora"
  name                     = local.name
  vpc_id                   = module.network.vpc_id
  subnet_ids               = module.network.private_subnet_ids
  client_security_group_id = aws_security_group.data_clients.id
  kms_key_arn              = aws_kms_key.data.arn
  engine_version           = var.aurora_engine_version
  min_acu                  = var.aurora_min_acu
  max_acu                  = var.aurora_max_acu
  seconds_until_auto_pause = var.aurora_seconds_until_auto_pause
  deletion_protection      = var.deletion_protection
}

resource "aws_route53_record" "db" {
  zone_id = module.network.internal_zone_id
  name    = "db.${module.network.internal_domain}"
  type    = "CNAME"
  ttl     = 60
  records = [module.aurora.endpoint]
}

module "rag" {
  source                   = "../../modules/rag-host"
  name                     = local.name
  region                   = var.region
  vpc_id                   = module.network.vpc_id
  subnet_id                = module.network.private_subnet_ids[0]
  client_security_group_id = aws_security_group.data_clients.id
  kms_key_arn              = aws_kms_key.data.arn
  internal_zone_id         = module.network.internal_zone_id
  internal_domain          = module.network.internal_domain
  instance_type            = var.rag_instance_type
  qdrant_version           = var.qdrant_version
  house_volume_gb          = var.house_volume_gb
  agencies                 = var.agencies
}

# S3: the shared category database and one database per agency, in the cluster.
# Retired agencies keep their database until it is dropped by hand
# (offboarding, infra/README.md).
module "databases" {
  source            = "../../modules/tenant-databases"
  name              = local.name
  agencies          = keys(var.agencies)
  host              = module.aurora.endpoint
  port              = module.aurora.port
  master_secret_arn = module.aurora.master_secret_arn
  kms_key_arn       = aws_kms_key.data.arn
}

# provision-databases.sh as an SSM document, so it runs on the admin host with
# one `aws ssm send-command` and nobody copies scripts around.
resource "aws_ssm_document" "provision_databases" {
  name            = "${local.name}-provision-databases"
  document_type   = "Command"
  document_format = "JSON"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Create napkin_category and the per-agency databases and roles"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "provision"
      inputs = {
        runCommand = concat(
          ["set -- ${local.name}"],
          split("\n", file("${path.module}/../../scripts/provision-databases.sh")),
        )
      }
    }]
  })
}

module "admin" {
  source                   = "../../modules/admin-host"
  name                     = local.name
  vpc_id                   = module.network.vpc_id
  vpc_cidr                 = module.network.vpc_cidr
  subnet_id                = module.network.private_subnet_ids[0]
  extra_security_group_ids = [aws_security_group.data_clients.id]
  secret_arns = concat(
    [module.aurora.master_secret_arn],
    module.rag.store_secret_arns,
    values(module.databases.secret_arns),
  )
  parameter_arns    = [module.databases.list_parameter_arn]
  kms_key_arn       = aws_kms_key.data.arn
  corpus_bucket_arn = module.storage.bucket_arns["corpus"]
}

# A monthly ceiling that emails before the bill surprises anyone. Account-wide:
# a tag filter would read $0 until the Env tag is activated for cost
# allocation in Billing, which is exactly when an alert is needed most.
resource "aws_budgets_budget" "monthly" {
  count        = var.budget_email == "" ? 0 : 1
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }
}
