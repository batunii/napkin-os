# The whole staging stack planned against mocked providers: catches AWS-side
# argument rules (names, descriptions, enums) without an account.
#   cd infra/envs/staging && terraform init -backend=false && terraform test

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["eu-west-1a", "eu-west-1b", "eu-west-1c"] }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111111111111" }
  }
  mock_data "aws_subnet" {
    defaults = { availability_zone = "eu-west-1a" }
  }
  mock_resource "aws_rds_cluster" {
    defaults = {
      master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:eu-west-1:111111111111:secret:master", kms_key_id = "k", secret_status = "active" }]
      port               = 5432
      endpoint           = "napkin-staging.cluster-x.eu-west-1.rds.amazonaws.com"
    }
  }
}
mock_provider "random" {}

variables {
  budget_emails = ["alerts@example.com", "hello@example.com"]
  agencies = {
    acme   = { slot = 1 }
    globex = { slot = 2 }
  }
}

run "plans_with_two_agencies" {
  command = plan

  assert {
    condition     = keys(module.databases.secret_arns) == ["napkin_agency_acme", "napkin_agency_globex", "napkin_category"]
    error_message = "S3: one category database plus one per agency"
  }
  assert {
    condition     = length(module.rag.store_secret_arns) == 4
    error_message = "house read-write + house read-only + one key per agency"
  }
  assert {
    condition     = contains(jsondecode(aws_iam_policy.model_invoke.policy).Statement[0].Action, "bedrock:InvokeModel")
    error_message = "the admin host (seeding) may call Claude on Bedrock"
  }
  assert {
    condition     = aws_budgets_budget.monthly["card"].cost_types[0].include_credit && !aws_budgets_budget.monthly["burn"].cost_types[0].include_credit
    error_message = "card = after credits, burn = before credits"
  }
}
