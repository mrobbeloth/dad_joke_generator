# S3 buckets for the dadjokes Web_App (task 16.2).
#
# Three buckets per design.md "Data Models > S3 Buckets":
#   - spa-assets:      SPA static files; CloudFront origin via OAC (R6, R17 not applicable).
#   - audio:           Polly MP3 outputs; 30-day lifecycle expiration (R2.4).
#   - training-corpus: Few-shot prompt corpus; Lambda-only read (R17.2, R17.3).
#
# Bucket names must be globally unique. We append a 4-byte random hex suffix so
# re-creates after destroy and parallel deployments to multiple environments do
# not collide on the same name.

resource "random_id" "suffix" {
  byte_length = 4
}

# ---------------------------------------------------------------------------
# Bucket 1: spa-assets
# Static SPA files served through CloudFront with an Origin Access Control.
# Public access is fully blocked; CloudFront reads via the bucket policy below.
# ---------------------------------------------------------------------------

# R6/R7: spa-assets is the CloudFront origin. Public access is blocked; reads
# happen only via the CloudFront OAC principal granted in the bucket policy.
resource "aws_s3_bucket" "spa_assets" {
  bucket = "${var.project_name}-${var.environment}-spa-assets-${random_id.suffix.hex}"
}

# R17.2-style hardening: Block Public Access on all four flags. CloudFront does
# not require any of these to be relaxed because it authenticates as a service
# principal via the OAC, not as an anonymous public reader.
resource "aws_s3_bucket_public_access_block" "spa_assets" {
  bucket                  = aws_s3_bucket.spa_assets.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning enabled so CloudFront cache invalidation can roll forward and back
# between SPA bundle versions without losing prior objects.
resource "aws_s3_bucket_versioning" "spa_assets" {
  bucket = aws_s3_bucket.spa_assets.id
  versioning_configuration {
    status = "Enabled"
  }
}

# AES256 (SSE-S3) encryption. KMS is overkill for static SPA assets and adds
# per-request cost; design.md A6 favors managed keys for this bucket.
resource "aws_s3_bucket_server_side_encryption_configuration" "spa_assets" {
  bucket = aws_s3_bucket.spa_assets.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Stub policy document allowing s3:GetObject only when invoked by the
# CloudFront distribution identified by var.cloudfront_distribution_arn.
# The distribution itself is created in task 16.5, so the ARN is empty in
# Phase 1; we only attach a bucket policy when a non-empty ARN is supplied.
data "aws_iam_policy_document" "spa_assets_oac" {
  statement {
    sid     = "AllowCloudFrontOACRead"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.spa_assets.arn}/*",
    ]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [var.cloudfront_distribution_arn]
    }
  }
}

# Trade-off: the CloudFront distribution lives in task 16.5 and is not created
# here, so until that task lands the OAC ARN is unknown. Skipping the policy
# entirely (count = 0) keeps the bucket reachable only via direct IAM grants
# and avoids a dangling policy that points at a non-existent distribution.
resource "aws_s3_bucket_policy" "spa_assets" {
  count  = var.cloudfront_distribution_arn == "" ? 0 : 1
  bucket = aws_s3_bucket.spa_assets.id
  policy = data.aws_iam_policy_document.spa_assets_oac.json

  depends_on = [aws_s3_bucket_public_access_block.spa_assets]
}

# ---------------------------------------------------------------------------
# Bucket 2: audio
# Polly MP3 outputs. Short-lived per R2.4 (lifecycle expiration in 30 days).
# Lambda reads/writes via its IAM role; no bucket policy is needed.
# ---------------------------------------------------------------------------

# R2.4: audio bucket holds short-lived MP3 outputs. Visitors fetch via 15-min
# presigned GET URLs, never via direct public access.
resource "aws_s3_bucket" "audio" {
  bucket = "${var.project_name}-${var.environment}-audio-${random_id.suffix.hex}"
}

# R17.2: BPA on all four flags. Audio is delivered exclusively through
# presigned URLs, so no anonymous public access is ever required.
resource "aws_s3_bucket_public_access_block" "audio" {
  bucket                  = aws_s3_bucket.audio.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning intentionally disabled. Audio objects are ephemeral and the
# lifecycle rule below deletes them after var.audio_retention_days; keeping
# noncurrent versions would defeat the purpose and inflate storage cost.
resource "aws_s3_bucket_versioning" "audio" {
  bucket = aws_s3_bucket.audio.id
  versioning_configuration {
    status = "Disabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audio" {
  bucket = aws_s3_bucket.audio.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# R2.4 / R17.2: expire current audio objects after the configured retention
# window (default 30 days). Single rule with no filter scopes the rule to the
# entire bucket, which matches the design intent that every audio object is
# short-lived.
resource "aws_s3_bucket_lifecycle_configuration" "audio" {
  bucket = aws_s3_bucket.audio.id

  rule {
    id     = "expire-audio-after-retention"
    status = "Enabled"

    # Empty filter applies the rule to all objects in the bucket. Required by
    # the AWS API for v2 lifecycle configurations.
    filter {}

    expiration {
      days = var.audio_retention_days
    }
  }
}

# ---------------------------------------------------------------------------
# Bucket 3: training-corpus
# Author-curated few-shot examples. Read access is granted to the Lambda
# execution role only (R17.2). No bucket policy is attached here; the role
# itself receives s3:GetObject on this bucket in task 16.4.
# ---------------------------------------------------------------------------

# R17.1/R17.2: training-corpus holds the Joke_Generator's few-shot prompt pool.
# Contents must never reach clients (R17.3), so the bucket stays fully private.
resource "aws_s3_bucket" "training_corpus" {
  bucket = "${var.project_name}-${var.environment}-training-corpus-${random_id.suffix.hex}"
}

# R17.2: BPA on all four flags. Only the Lambda execution role (created in
# task 16.4) is permitted to read; no public access path exists.
resource "aws_s3_bucket_public_access_block" "training_corpus" {
  bucket                  = aws_s3_bucket.training_corpus.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning enabled so corpus edits (which are author-curated) are reversible
# without out-of-band backups.
resource "aws_s3_bucket_versioning" "training_corpus" {
  bucket = aws_s3_bucket.training_corpus.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "training_corpus" {
  bucket = aws_s3_bucket.training_corpus.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
