# The three buckets the data plane needs:
#   corpus  — the licensed craft corpora (Cannes, D&AD, IPA, Effie…) as source
#             files, the input to RAG ingestion. licensed-internal: never public,
#             never served to a browser.
#   blobs   — document assets by sha256 (the Postgres + S3 decision). Empty
#             until the layers and web host write to it.
#   backups — Qdrant snapshot exports and database dumps taken by hand.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

locals {
  buckets = {
    corpus  = { noncurrent_days = 365 }
    blobs   = { noncurrent_days = 30 }
    backups = { noncurrent_days = 30 }
  }
}

resource "aws_s3_bucket" "this" {
  for_each = local.buckets
  bucket   = "${var.name}-${each.key}-${var.account_id}"
}

resource "aws_s3_bucket_ownership_controls" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each                = aws_s3_bucket.this
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = local.buckets[each.key].noncurrent_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_policy" "tls_only" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [each.value.arn, "${each.value.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.this]
}
