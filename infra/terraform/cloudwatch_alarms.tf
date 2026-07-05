# CloudWatch alarms, SNS topics, and metric filters (task 16.7).
#
# Traceability:
#
#   R16.2 -- ops alarms on the dadjokes/* CloudWatch metrics emitted by
#            joke_api.observability.emit_metric.
#   R16.3 -- cost guardrail. Originally an account-wide billing alarm here;
#            moved 2026-05-29 to the tag-scoped AWS Budget in
#            infra/terraform-bootstrap/budgets.tf (billing metrics cannot be
#            filtered by cost-allocation tag on the shared OSU account).
#   R16.4 -- separate cost SNS topic carrying the [COST-ALERT] subject line.
#   R16.6 -- separate ops SNS topic carrying the [OPS-ALERT] subject line.
#
# Channel separation (Property 33) is enforced here: every ops alarm wires
# only to aws_sns_topic.ops_alerts. The cost SNS topic is retained for the
# Lambda-side dispatcher (joke_api.observability.dispatch_cost_alert), even
# though no CloudWatch alarm publishes to it anymore. The email subject
# prefixes are produced by that dispatcher; IaC only owns the topic wiring.

locals {
  # Resolve Lambda wiring lazily so this module validates standalone even
  # before sibling task 16.4 lands lambda.tf. coalesce() skips nulls so a
  # caller can still override either name explicitly.
  lambda_function_name  = coalesce(var.lambda_function_name, "${var.project_name}-${var.environment}")
  lambda_log_group_name = coalesce(var.lambda_log_group_name, "/aws/lambda/${local.lambda_function_name}")
}

# ---------------------------------------------------------------------------
# SNS topics (R16.4 / R16.6 channel separation)
# ---------------------------------------------------------------------------

# Cost-alert channel (R16.4). Carries [COST-ALERT] emails produced by
# joke_api.observability.dispatch_cost_alert. Property 31's "send only on
# OK->ALARM" gate is implemented at the dispatcher layer, not by suppressing
# the OK transition here, so we publish both transitions to this topic.
resource "aws_sns_topic" "cost_alerts" {
  name = "${var.project_name}-${var.environment}-cost-alerts"
}

# Ops-alert channel (R16.6). Carries [OPS-ALERT] emails produced by
# joke_api.observability.dispatch_ops_alert. Subject must NOT contain "cost"
# (Property 33); that constraint is enforced in the dispatcher.
resource "aws_sns_topic" "ops_alerts" {
  name = "${var.project_name}-${var.environment}-ops-alerts"
}

# Optional email subscriptions. AWS requires the recipient to confirm the
# subscription out-of-band by clicking the link in the confirmation email
# (matches docs/PLAN.md MS09 / MS10). Leaving the variable empty skips
# subscription creation so the topic can still be wired by publishers.
resource "aws_sns_topic_subscription" "cost_alerts_email" {
  count     = var.cost_alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.cost_alerts.arn
  protocol  = "email"
  endpoint  = var.cost_alert_email
}

resource "aws_sns_topic_subscription" "ops_alerts_email" {
  count     = var.ops_alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.ops_alerts.arn
  protocol  = "email"
  endpoint  = var.ops_alert_email
}

# ---------------------------------------------------------------------------
# Cost guardrail (R16.3) — moved to AWS Budgets.
# ---------------------------------------------------------------------------
#
# The Phase 1 design specified a CloudWatch alarm on AWS/Billing
# EstimatedCharges for the cost guardrail. That approach is only correct
# on a DEDICATED AWS account: AWS/Billing EstimatedCharges supports only
# the Currency, ServiceName, and LinkedAccount dimensions — never
# user-defined cost-allocation tags. On this SHARED OSU member account
# (455110962976, org o-w9mnpf422e) the metric sums every project's spend
# (stoplight-classroom, retro-web-gateway, CIO enterprise tooling, etc.),
# so an account-wide alarm at any dadjokes-appropriate threshold fires
# constantly on non-dadjokes cost — noise, not signal.
#
# The dadjokes-only cost guardrail therefore lives in AWS Budgets, which
# CAN filter by the `Proj=dadjokes` cost-allocation tag (active at the
# org payer-account level per OSU IT, 2026-05-27). See
# infra/terraform-bootstrap/budgets.tf: aws_budgets_budget.account_total
# scoped to user:Proj$dadjokes. The budget captures ALL dadjokes-tagged
# spend (Lambda + S3 + CloudFront + DynamoDB + idle), not just the
# per-request Lambda cost estimates a custom CloudWatch metric could
# offer.
#
# The cost_alerts SNS topic and its email subscription (MS09) are
# retained: the Lambda-side joke_api.observability.dispatch_cost_alert
# path still publishes to it, and Property 31/33's email-shape contract
# is unaffected. Only the account-wide billing ALARM was removed.
#
# References:
#   - PLAN.md MS03 (the AWS Budget)
#   - infra/terraform-bootstrap/budgets.tf (the replacement guardrail)
#   - design.md R16.3 (original CloudWatch-alarm intent; superseded on
#     shared accounts)

# ---------------------------------------------------------------------------
# Ops alarms (R16.2 / R16.6)
# ---------------------------------------------------------------------------
#
# All four counters live in the "dadjokes" namespace (matches
# joke_api.observability.CLOUDWATCH_NAMESPACE). period = 300 keeps these on
# the standard-resolution metric tier (no extra cost). statistic = "Sum"
# matches the per-event emit_metric calls -- the runtime publishes
# value=1.0 per event, so Sum over 5 minutes is the count of events.
#
# Note: jokes_per_hour is intentionally NOT alarmed here. It is a healthy-
# state indicator; the meaningful operational signal would be a *drop* (no
# jokes for an hour), which requires an anomaly detector or low-watermark
# alarm and is out of scope for the 16.7 deliverable.

# moderation_rejections_per_hour (R16.6, Property 33).
resource "aws_cloudwatch_metric_alarm" "moderation_rejection_spike" {
  alarm_name          = "${var.project_name}-${var.environment}-moderation-rejection-spike"
  alarm_description   = "moderation_rejections_per_hour Sum >= ${var.moderation_rejection_alarm_threshold} in 5-minute window (R16.6)."
  namespace           = "dadjokes"
  metric_name         = "moderation_rejections_per_hour"
  statistic           = "Sum"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  period              = 300
  threshold           = var.moderation_rejection_alarm_threshold
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops_alerts.arn]
}

# rate_limit_rejections_per_hour (R16.6, Property 33).
resource "aws_cloudwatch_metric_alarm" "rate_limit_rejection_spike" {
  alarm_name          = "${var.project_name}-${var.environment}-rate-limit-rejection-spike"
  alarm_description   = "rate_limit_rejections_per_hour Sum >= ${var.rate_limit_rejection_alarm_threshold} in 5-minute window (R16.6)."
  namespace           = "dadjokes"
  metric_name         = "rate_limit_rejections_per_hour"
  statistic           = "Sum"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  period              = 300
  threshold           = var.rate_limit_rejection_alarm_threshold
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops_alerts.arn]
}

# observability_failure (R16.6, R16.8). Non-zero means the structured-log
# or metric-emit transport is itself failing -- the situation Property 35
# soft-fails at runtime so the request still succeeds. Threshold default 1
# catches the first occurrence in the window.
resource "aws_cloudwatch_metric_alarm" "observability_failure" {
  alarm_name          = "${var.project_name}-${var.environment}-observability-failure"
  alarm_description   = "observability_failure Sum >= ${var.observability_failure_alarm_threshold} in 5-minute window (R16.6, R16.8)."
  namespace           = "dadjokes"
  metric_name         = "observability_failure"
  statistic           = "Sum"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  period              = 300
  threshold           = var.observability_failure_alarm_threshold
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops_alerts.arn]
}

# ---------------------------------------------------------------------------
# Bedrock / Polly error metric filter + alarm (R16.6)
# ---------------------------------------------------------------------------
#
# Bedrock and Polly transport failures surface as structured log lines with
# decision="error" (per joke_api.observability.LogRecord.to_json_dict and
# design.md Data Models > Structured Log Record). A CloudWatch Logs metric
# filter increments lambda_decision_error_count every time such a line is
# written; the alarm below fires when the 5-minute Sum crosses the
# configured threshold.
#
# JSON metric filter pattern: `{ $.decision = "error" }` matches any line
# parsed as JSON whose top-level "decision" field equals the string "error".
# This is robust to additional fields the LogRecord may add over time
# because the pattern only constrains one key.

resource "aws_cloudwatch_log_metric_filter" "lambda_decision_error" {
  name           = "${var.project_name}-${var.environment}-decision-error"
  log_group_name = local.lambda_log_group_name
  pattern        = "{ $.decision = \"error\" }"

  metric_transformation {
    name          = "lambda_decision_error_count"
    namespace     = "dadjokes"
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "lambda_decision_error" {
  alarm_name          = "${var.project_name}-${var.environment}-lambda-decision-error"
  alarm_description   = "lambda_decision_error_count Sum >= ${var.decision_error_alarm_threshold} in 5-minute window (R16.6)."
  namespace           = "dadjokes"
  metric_name         = aws_cloudwatch_log_metric_filter.lambda_decision_error.metric_transformation[0].name
  statistic           = "Sum"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  period              = 300
  threshold           = var.decision_error_alarm_threshold
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops_alerts.arn]
}

