# Remote state bucket + DynamoDB lock table for the main module's
# `terraform { backend "s3" { ... } }` configuration.
#
# Hardening:
#   - Versioning enabled (recover from accidental destroy)
#   - Server-side encryption (SSE-S3 / AES256)
#   - Public access fully blocked
#   - DynamoDB lock table with on-demand billing (~$0/month at idle)

resource "aws_s3_bucket" "tfstate" {
  bucket = var.tfstate_bucket_name

  # We never want a `terraform destroy` against this module to silently
  # delete the state bucket while it still has objects in it. Lift this
  # only when intentionally tearing down the whole stack (see README).
  force_destroy = false
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_dynamodb_table" "tflock" {
  name         = var.tflock_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  deletion_protection_enabled = true
}
