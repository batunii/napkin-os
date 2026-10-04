# The knowledge layers' database: Aurora PostgreSQL Serverless v2, able to
# pause at 0 ACU when nothing is connected.
#
# One database, both layers. The category layer and the brand layer share it on
# purpose: a fact, the decision justifying it and its source links commit in
# one transaction (peripherals.md §4.6). Brand isolation is row-level security
# on (org, brand), set up by the schema migrations, not here.
#
# Pausing only happens with no user connections for `seconds_until_auto_pause`.
# A connection pool that holds idle connections, or a health check that opens
# one, keeps it awake and billing — see infra/README.md.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

locals {
  family = "aurora-postgresql${split(".", var.engine_version)[0]}"
}

resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids
}

resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "Aurora: Postgres from data clients only"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "from_clients" {
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = var.client_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "Postgres from napkin data clients"
}

resource "aws_rds_cluster_parameter_group" "this" {
  name   = "${var.name}-${local.family}"
  family = local.family

  # TLS on every connection; the layers service connects with sslmode=verify-full.
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  # Queries slower than a second land in the log — research writes are small,
  # so anything that slow is worth reading.
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
}

resource "aws_rds_cluster" "this" {
  cluster_identifier = var.name
  engine             = "aurora-postgresql"
  engine_mode        = "provisioned"
  engine_version     = var.engine_version
  database_name      = var.database_name

  # The master user is for migrations and break-glass only. Its password is
  # generated and rotated by RDS in Secrets Manager; nobody types it. The
  # layers service gets its own non-owner role (RLS applies to it) from the
  # first migration.
  master_username               = "napkin_admin"
  manage_master_user_password   = true
  master_user_secret_kms_key_id = var.kms_key_arn

  iam_database_authentication_enabled = true

  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [aws_security_group.db.id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.this.name

  storage_encrypted = true
  kms_key_id        = var.kms_key_arn

  backup_retention_period      = var.backup_retention_days
  preferred_backup_window      = "02:00-03:00"
  preferred_maintenance_window = "sun:03:30-sun:04:30"
  copy_tags_to_snapshot        = true
  deletion_protection          = var.deletion_protection
  skip_final_snapshot          = false
  final_snapshot_identifier    = "${var.name}-final"

  enabled_cloudwatch_logs_exports = ["postgresql"]

  serverlessv2_scaling_configuration {
    min_capacity             = var.min_acu
    max_capacity             = var.max_acu
    seconds_until_auto_pause = var.min_acu == 0 ? var.seconds_until_auto_pause : null
  }
}

resource "aws_rds_cluster_instance" "writer" {
  identifier                 = "${var.name}-1"
  cluster_identifier         = aws_rds_cluster.this.id
  instance_class             = "db.serverless"
  engine                     = aws_rds_cluster.this.engine
  engine_version             = aws_rds_cluster.this.engine_version
  publicly_accessible        = false
  auto_minor_version_upgrade = true
}
