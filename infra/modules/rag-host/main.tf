# The RAG host: one EC2 machine running one Qdrant per store (foundation-spec
# S8: isolation between agencies is a deployment boundary, not a filter).
#
#   house        the licensed corpus, shared and read-only: retrieval holds a
#                read-only key; only ingestion (admin host) holds the write key
#   <agency>     that agency's learnings only — its own material and its locked
#                briefs — on its own disk, with its own key and port
#
# A search reads house + the caller's agency store and merges by rank. Nothing
# is copied per agency, so a house update reaches everyone at once.
#
# Why EC2 + EBS and not Fargate: Qdrant needs block storage and does not work
# on NFS, so EFS (Fargate's only persistent volume) is out.
#
# The store list lives in an SSM parameter the host reconciles against, so
# adding an agency or bumping Qdrant restarts containers, never the host.

terraform {
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

data "aws_subnet" "this" {
  id = var.subnet_id
}

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

locals {
  stores = merge(
    {
      house = {
        kind        = "house"
        slot        = 0
        volume_gb   = var.house_volume_gb
        memory_mb   = var.house_memory_mb
        own_kms_key = false
        state       = "active"
      }
    },
    { for k, a in var.agencies : k => merge(a, { kind = "agency" }) },
  )

  # slot 0 = house on 6333; agency slot n on 6340 + n, device /dev/sd[f + n].
  port   = { for k, s in local.stores : k => s.slot == 0 ? 6333 : 6340 + s.slot }
  device = { for k, s in local.stores : k => "/dev/sd${substr("fghijklmnopqrstuvwxyz", s.slot, 1)}" }

  agency_keys = { for k, a in var.agencies : k => a if a.own_kms_key }
  volume_key  = { for k, s in local.stores : k => contains(keys(local.agency_keys), k) ? aws_kms_key.agency[k].arn : var.kms_key_arn }

  param_name = "/${var.name}/rag/stores"
}

# ── per-agency encryption keys ───────────────────────────────────────────────
# Offboarding schedules the agency's key for deletion: after the waiting
# period every copy of its vectors — volume and snapshots — is unreadable.

resource "aws_kms_key" "agency" {
  for_each                = local.agency_keys
  description             = "${var.name}: RAG learnings of agency ${each.key}"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = { Agency = each.key }
}

resource "aws_kms_alias" "agency" {
  for_each      = local.agency_keys
  name          = "alias/${var.name}-rag-${each.key}"
  target_key_id = aws_kms_key.agency[each.key].key_id
}

# ── API keys ─────────────────────────────────────────────────────────────────
# Each store has its own key: agency A's key opens nothing but A's store. The
# house store also has a read-only key, the only one retrieval is given.

resource "random_password" "api_key" {
  for_each = local.stores
  length   = 48
  special  = false
}

resource "aws_secretsmanager_secret" "api_key" {
  for_each                = local.stores
  name                    = "${var.name}/rag/${each.key}/api-key"
  kms_key_id              = var.kms_key_arn
  recovery_window_in_days = 7
  tags                    = { Store = each.key }
}

resource "aws_secretsmanager_secret_version" "api_key" {
  for_each      = local.stores
  secret_id     = aws_secretsmanager_secret.api_key[each.key].id
  secret_string = random_password.api_key[each.key].result
}

resource "random_password" "house_read_only" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "house_read_only" {
  name                    = "${var.name}/rag/house/read-only-key"
  kms_key_id              = var.kms_key_arn
  recovery_window_in_days = 7
  tags                    = { Store = "house" }
}

resource "aws_secretsmanager_secret_version" "house_read_only" {
  secret_id     = aws_secretsmanager_secret.house_read_only.id
  secret_string = random_password.house_read_only.result
}

# ── network ──────────────────────────────────────────────────────────────────

resource "aws_security_group" "rag" {
  name        = "${var.name}-rag"
  description = "RAG host: Qdrant REST ports from data clients only"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "house" {
  security_group_id            = aws_security_group.rag.id
  referenced_security_group_id = var.client_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 6333
  to_port                      = 6333
  description                  = "house store"
}

resource "aws_vpc_security_group_ingress_rule" "agencies" {
  security_group_id            = aws_security_group.rag.id
  referenced_security_group_id = var.client_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 6341
  to_port                      = 6360
  description                  = "agency stores, 6340 + slot"
}

resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.rag.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "http" {
  security_group_id = aws_security_group.rag.id
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "0.0.0.0/0"
  description       = "dnf mirrors"
}

# ── the store list the host reconciles against ───────────────────────────────

resource "aws_ssm_parameter" "stores" {
  name = local.param_name
  type = "String"
  value = jsonencode({
    qdrant_version = var.qdrant_version
    stores = { for k, s in local.stores : k => merge(
      {
        kind               = s.kind
        state              = s.state
        port               = local.port[k]
        memory_mb          = s.memory_mb
        volume_id          = replace(aws_ebs_volume.store[k].id, "-", "")
        api_key_secret_arn = aws_secretsmanager_secret.api_key[k].arn
      },
      k == "house" ? { read_only_key_secret_arn = aws_secretsmanager_secret.house_read_only.arn } : {},
    ) }
  })
}

# ── the host ─────────────────────────────────────────────────────────────────

resource "aws_iam_role" "rag" {
  name = "${var.name}-rag"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Shell access and the reconcile runs are SSM only: no SSH key, no port 22.
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.rag.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "rag" {
  name = "rag"
  role = aws_iam_role.rag.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadStoreList"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = aws_ssm_parameter.stores.arn
      },
      {
        Sid    = "ReadStoreKeys"
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        Resource = concat(
          [for s in aws_secretsmanager_secret.api_key : s.arn],
          [aws_secretsmanager_secret.house_read_only.arn],
        )
      },
      {
        Sid      = "DecryptSecrets"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = var.kms_key_arn
      }
    ]
  })
}

resource "aws_iam_instance_profile" "rag" {
  name = "${var.name}-rag"
  role = aws_iam_role.rag.name
}

resource "aws_instance" "rag" {
  ami                    = data.aws_ssm_parameter.al2023_arm64.value
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = [aws_security_group.rag.id]
  iam_instance_profile   = aws_iam_instance_profile.rag.name

  metadata_options {
    http_tokens = "required"
  }

  # Restarts the host on new hardware if AWS's checks see it fail.
  maintenance_options {
    auto_recovery = "default"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 20
    encrypted   = true
    kms_key_id  = var.kms_key_arn
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region        = var.region
    param_name    = local.param_name
    reconcile_b64 = base64encode(file("${path.module}/reconcile.sh"))
  })

  tags = { Name = "${var.name}-rag" }

  lifecycle {
    # Neither a newer AMI nor a reconcile-script change may silently rebuild
    # the host every store runs on. Replace it deliberately (README).
    ignore_changes = [ami, user_data]
  }
}

# ── one disk per store ───────────────────────────────────────────────────────

resource "aws_ebs_volume" "store" {
  for_each          = local.stores
  availability_zone = data.aws_subnet.this.availability_zone
  size              = each.value.volume_gb
  type              = "gp3"
  encrypted         = true
  kms_key_id        = local.volume_key[each.key]
  # Deleting a store still leaves a last snapshot, under the store's own key.
  final_snapshot = true
  tags = {
    Name     = "${var.name}-rag-${each.key}"
    Store    = each.key
    Snapshot = "${var.name}-rag"
  }
}

# Detaching never stops the host (that would take every store down). A store
# is retired first, which unmounts it, so its detach is clean.
resource "aws_volume_attachment" "store" {
  for_each    = local.stores
  device_name = local.device[each.key]
  volume_id   = aws_ebs_volume.store[each.key].id
  instance_id = aws_instance.rag.id
}

# Run the reconcile whenever the store list changes (and once the disks are on).
resource "aws_ssm_association" "reconcile" {
  name             = "AWS-RunShellScript"
  association_name = "${var.name}-rag-reconcile"
  targets {
    key    = "InstanceIds"
    values = [aws_instance.rag.id]
  }
  parameters = {
    commands = "/usr/local/bin/napkin-rag-reconcile # stores ${sha1(aws_ssm_parameter.stores.value)}"
  }
  depends_on = [aws_volume_attachment.store]
}

resource "aws_route53_record" "store" {
  for_each = local.stores
  zone_id  = var.internal_zone_id
  name     = "qdrant-${each.key}.${var.internal_domain}"
  type     = "A"
  ttl      = 60
  records  = [aws_instance.rag.private_ip]
}

# ── backups ──────────────────────────────────────────────────────────────────
# Daily snapshots of every store's disk, each on its own volume and key, so one
# agency restores alone. Crash-consistent is enough: Qdrant recovers from its WAL.

resource "aws_iam_role" "dlm" {
  name = "${var.name}-rag-dlm"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "dlm.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "dlm" {
  role       = aws_iam_role.dlm.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

resource "aws_dlm_lifecycle_policy" "stores" {
  description        = "${var.name} RAG stores daily"
  execution_role_arn = aws_iam_role.dlm.arn
  state              = "ENABLED"

  policy_details {
    resource_types = ["VOLUME"]
    target_tags    = { Snapshot = "${var.name}-rag" }

    schedule {
      name = "daily"
      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["03:00"]
      }
      retain_rule {
        count = var.snapshot_retention_days
      }
      copy_tags = true
    }
  }
}
