# A tiny host you reach through SSM Session Manager, never SSH, to run psql
# against Aurora, curl Qdrant, run migrations and the corpus ingestion. It is
# also the endpoint for port forwarding from your laptop (see infra/README.md).
# No inbound rule at all: SSM connects outbound.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_security_group" "admin" {
  name        = "${var.name}-admin"
  description = "Admin host: no inbound; out to the data stores and HTTPS"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.admin.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "http" {
  security_group_id = aws_security_group.admin.id
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "0.0.0.0/0"
  description       = "dnf mirrors"
}

resource "aws_vpc_security_group_egress_rule" "postgres" {
  security_group_id = aws_security_group.admin.id
  ip_protocol       = "tcp"
  from_port         = 5432
  to_port           = 5432
  cidr_ipv4         = var.vpc_cidr
}

resource "aws_vpc_security_group_egress_rule" "qdrant" {
  security_group_id = aws_security_group.admin.id
  ip_protocol       = "tcp"
  from_port         = 6333
  to_port           = 6360
  description       = "RAG stores: house 6333, agencies 6341-6360"
  cidr_ipv4         = var.vpc_cidr
}

resource "aws_iam_role" "admin" {
  name = "${var.name}-admin"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.admin.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "extra" {
  for_each   = var.policy_arns
  role       = aws_iam_role.admin.name
  policy_arn = each.value
}

resource "aws_iam_role_policy" "admin" {
  name = "admin"
  role = aws_iam_role.admin.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadDataSecrets"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = var.secret_arns
      },
      {
        # The database list provision-databases.sh reads.
        Sid      = "ReadStackParameters"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = var.parameter_arns
      },
      {
        Sid      = "UseDataKey"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.kms_key_arn
      },
      {
        # Ingestion reads the corpus from here.
        Sid      = "ReadCorpus"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [var.corpus_bucket_arn, "${var.corpus_bucket_arn}/*"]
      }
    ]
  })
}

resource "aws_iam_instance_profile" "admin" {
  name = "${var.name}-admin"
  role = aws_iam_role.admin.name
}

resource "aws_instance" "admin" {
  ami                    = data.aws_ssm_parameter.al2023_arm64.value
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = concat([aws_security_group.admin.id], var.extra_security_group_ids)
  iam_instance_profile   = aws_iam_instance_profile.admin.name

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 20
    encrypted   = true
    kms_key_id  = var.kms_key_arn
  }

  user_data = <<-EOF
    #!/bin/bash
    dnf install -y postgresql16 jq git
    # uv brings the exact Python the server and ingestion pin (3.12).
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
  EOF

  tags = { Name = "${var.name}-admin" }

  lifecycle {
    ignore_changes = [ami]
  }
}
