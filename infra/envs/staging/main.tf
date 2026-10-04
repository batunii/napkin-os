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
#   identity  the Cognito user pool people sign in with (user@agency)
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

# Who can sign in: accounts `user@agency`, made by an admin (scripts/create-users.sh).
module "identity" {
  source      = "../../modules/identity"
  name        = local.name
  kms_key_arn = aws_kms_key.data.arn
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

# Claude through Amazon Bedrock (peripherals.md §1.3a): the middleware and the
# seeding job sign their calls with their role. One policy, attached to the
# admin host now (it runs seeding) and to the stage 2 services later.
#
#   runtime (default)  bedrock:InvokeModel on the Claude inference profiles in
#                      this region (eu. and global.) and on the Claude models
#                      they route to, in any region (global. can land anywhere;
#                      its models have no region in their ARN)
#   mantle (option)    bedrock-mantle:CreateInference; no per-model resource type
#                      in IAM yet, so the request region is the limit
#
# Model access itself is a Marketplace agreement per model, accepted once per
# account (infra/README.md).
resource "aws_iam_policy" "model_invoke" {
  name        = "${local.name}-model-invoke"
  description = "Call Claude on Amazon Bedrock from ${var.region}"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ClaudeInferenceProfiles"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:GetInferenceProfile"]
        Resource = [
          "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.me.account_id}:inference-profile/eu.anthropic.claude-*",
          "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.me.account_id}:inference-profile/global.anthropic.claude-*",
        ]
      },
      {
        Sid      = "ClaudeModelsBehindTheProfiles"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = ["arn:aws:bedrock:*::foundation-model/anthropic.claude-*", "arn:aws:bedrock:::foundation-model/anthropic.claude-*"]
      },
      {
        Sid       = "ClaudeOnMantle"
        Effect    = "Allow"
        Action    = "bedrock-mantle:CreateInference"
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = var.region } }
      },
    ]
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
  policy_arns       = { model_invoke = aws_iam_policy.model_invoke.arn }
}

# Two account-wide budgets (see variables.tf). Account-wide: a tag filter would
# read $0 until the Env tag is activated for cost allocation in Billing, which
# is exactly when an alert is needed most.
locals {
  budgets = length(var.budget_emails) == 0 ? {} : {
    burn = { limit = var.monthly_budget_usd, include_credit = false }
    card = { limit = var.monthly_card_budget_usd, include_credit = true }
  }
}

resource "aws_budgets_budget" "monthly" {
  for_each     = local.budgets
  name         = "${local.name}-monthly-${each.key}"
  budget_type  = "COST"
  limit_amount = tostring(each.value.limit)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # include_credit = true: credits count against the cost, so this reads what
  # the card pays. false: the gross cost, what the credits are paying for.
  cost_types {
    include_credit = each.value.include_credit
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = var.budget_emails
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = var.budget_emails
  }
}
