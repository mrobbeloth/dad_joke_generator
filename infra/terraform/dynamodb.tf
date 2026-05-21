# DynamoDB single-table store for the dadjokes service.
#
# Design ref: design.md "Data Models > DynamoDB: dadjokes (single table)".
# Two access patterns share the table:
#   - R5.4, R5.6: Rate_Limiter rows partitioned by pk = "RL#" + ip_hash,
#     sort key sk = "DAY#" + utc_date_string. Atomic UpdateItem ADD increments
#     the daily count. TTL via expires_at = next UTC midnight + 60 s gives
#     same-day eventual cleanup; the read path also treats prior-day records
#     as zero so the logical reset is immediate at the boundary.
#   - R18.4: Joke_Store rows with pk = "JOKE#" + uuid, sk = "META", and
#     expires_at = created_at + 90 days (epoch seconds). DynamoDB TTL deletes
#     each record within ~24 h of expiration, satisfying the 24-hour purge SLA.
#
# No GSIs are provisioned for Phase 1 access patterns; both rate_limiter.py
# and joke_store.py read by pk/sk only. Revisit if /v1/jokes/{id} latency or
# audit-replay queries grow beyond what a single GetItem can serve.
resource "aws_dynamodb_table" "dadjokes" {
  name = "${var.project_name}-${var.environment}"

  # On-demand billing keeps idle cost at zero (design.md "Architecture &
  # Technology Choices"). No read/write capacity is set for PAY_PER_REQUEST.
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "pk"
  range_key = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  # R5.6, R18.4: TTL drives both rate-limit reset and 90-day joke retention.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  # Cheap insurance for a small operational table that holds rate-limit and
  # joke records; PITR enables 35 days of restore granularity.
  point_in_time_recovery {
    enabled = true
  }

  # AWS-managed KMS key encrypts at rest. Explicit for readability even though
  # SSE is on by default for new tables.
  server_side_encryption {
    enabled = true
  }

  # Accidental destroy of this table would wipe live rate-limit counters and
  # joke history; require an explicit override before terraform can delete it.
  deletion_protection_enabled = true
}
