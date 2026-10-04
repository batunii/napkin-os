# Who can sign in to the studio: a Cognito user pool.
#
# Accounts are named `user@agency` (the agency is the workspace) and made by
# an admin only (scripts/create-users.sh): no one signs up. An admin-made
# account has a temporary password; Cognito asks for a new one at the first
# sign-in (NEW_PASSWORD_REQUIRED), which napkin-web's sign-in screen answers.
# napkin-web signs in with USER_PASSWORD_AUTH through a public app client (no
# client secret) and keeps its own signed session cookie afterwards.

terraform {
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

resource "aws_cognito_user_pool" "studio" {
  name = "${var.name}-studio"

  # usernames are `user@agency`, matched case-insensitively; no email login
  username_configuration {
    case_sensitive = false
  }

  admin_create_user_config {
    allow_admin_create_user_only = true
    # the temporary password an admin hands over stays good for a week
  }

  password_policy {
    minimum_length                   = 10
    require_lowercase                = false
    require_uppercase                = false
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  # the person's display name (`Laurance`), shown in the studio
  schema {
    name                = "name"
    attribute_data_type = "String"
    mutable             = true
    required            = false
    string_attribute_constraints {
      min_length = 0
      max_length = 128
    }
  }

  account_recovery_setting {
    # no email or phone on these accounts: an admin resets a forgotten password
    recovery_mechanism {
      name     = "admin_only"
      priority = 1
    }
  }

  deletion_protection = var.deletion_protection ? "ACTIVE" : "INACTIVE"
}

resource "aws_cognito_user_pool_client" "web" {
  name         = "${var.name}-web"
  user_pool_id = aws_cognito_user_pool.studio.id

  generate_secret               = false
  explicit_auth_flows           = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  read_attributes               = ["name"]
  write_attributes              = ["name"]
  id_token_validity             = 1
  access_token_validity         = 1
  refresh_token_validity        = 30
  token_validity_units {
    id_token      = "hours"
    access_token  = "hours"
    refresh_token = "days"
  }
}

# napkin-web's session cookies are signed with this; every web task gets the same one.
resource "random_password" "session" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "session" {
  name        = "${var.name}/web/session-secret"
  description = "Signs napkin-web's session cookies (NAPKIN_SESSION_SECRET)"
  kms_key_id  = var.kms_key_arn
}

resource "aws_secretsmanager_secret_version" "session" {
  secret_id     = aws_secretsmanager_secret.session.id
  secret_string = random_password.session.result
}
