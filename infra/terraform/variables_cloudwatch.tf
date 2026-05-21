# Variables for CloudWatch alarms, SNS topics, and metric filters (task 16.7).
#
# Owned by 16.7. Do not add Lambda or API Gateway variables here; those belong
# to 16.4's variables_lambda.tf.
#
# Each threshold variable below feeds an aws_cloudwatch_metric_alarm in
# cloudwatch_alarms.tf. Defaults match the numeric thresholds called out in
# design.md Property 31 / Property 33 and the SSM parameter
# /dadjokes/cost_alarm_threshold_usd (R16.3) so the IaC layer and the runtime
# email-shape layer (joke_api.observability) stay aligned.

# --- Cost alarm (R16.3) ------------------------------------------------------
#
# var.cost_alarm_threshold_usd is intentionally NOT declared here. It is
# already declared in variables_ssm.tf (task 16.3) so the SSM parameter and
# the CloudWatch alarm consume the same value and cannot drift. The alarm
# resource in cloudwatch_alarms.tf references it directly.

variable "cost_alert_email" {
  description = <<-EOT
    Email address subscribed to the cost SNS topic (R16.4). Empty string skips
    subscription creation so the topic itself can still be wired by Lambda
    publishers ahead of an operator confirming the address out-of-band. AWS
    sends a confirmation email on first apply; the subscription stays in
    "PendingConfirmation" until clicked (matches docs/PLAN.md MS09).
  EOT
  type        = string
  default     = ""
}

# --- Ops alarms (R16.2 / R16.6) ---------------------------------------------

variable "ops_alert_email" {
  description = <<-EOT
    Email address subscribed to the ops SNS topic (R16.6). Empty string skips
    subscription creation. AWS sends a confirmation email on first apply; the
    subscription stays in "PendingConfirmation" until clicked (matches
    docs/PLAN.md MS10).
  EOT
  type        = string
  default     = ""
}

variable "moderation_rejection_alarm_threshold" {
  description = <<-EOT
    Threshold for the moderation_rejections_per_hour ops alarm (R16.6). Triggers
    when the per-5-minute Sum of the dadjokes/moderation_rejections_per_hour
    metric meets or exceeds this value. Default 50 matches Property 33's
    "moderation rejections > 50 in any 5-minute window".
  EOT
  type        = number
  default     = 50

  validation {
    condition     = var.moderation_rejection_alarm_threshold >= 1 && var.moderation_rejection_alarm_threshold <= 10000
    error_message = "moderation_rejection_alarm_threshold must be between 1 and 10000 inclusive."
  }
}

variable "rate_limit_rejection_alarm_threshold" {
  description = <<-EOT
    Threshold for the rate_limit_rejections_per_hour ops alarm (R16.6). Triggers
    when the per-5-minute Sum of the dadjokes/rate_limit_rejections_per_hour
    metric meets or exceeds this value. Default 100 matches Property 33's
    "rate-limit rejections > 100 in any 5-minute window".
  EOT
  type        = number
  default     = 100

  validation {
    condition     = var.rate_limit_rejection_alarm_threshold >= 1 && var.rate_limit_rejection_alarm_threshold <= 10000
    error_message = "rate_limit_rejection_alarm_threshold must be between 1 and 10000 inclusive."
  }
}

variable "observability_failure_alarm_threshold" {
  description = <<-EOT
    Threshold for the observability_failure ops alarm (R16.6, R16.8). Any
    non-zero count of this metric means the structured-log or metric-emit
    transport is failing; default 1 catches the first occurrence in the
    5-minute evaluation window.
  EOT
  type        = number
  default     = 1

  validation {
    condition     = var.observability_failure_alarm_threshold >= 1 && var.observability_failure_alarm_threshold <= 1000
    error_message = "observability_failure_alarm_threshold must be between 1 and 1000 inclusive."
  }
}

variable "decision_error_alarm_threshold" {
  description = <<-EOT
    Threshold for the lambda_decision_error_count ops alarm derived from the
    Lambda log group via aws_cloudwatch_log_metric_filter. Triggers when the
    per-5-minute Sum of structured log lines whose `decision` field equals
    "error" meets or exceeds this value. Bedrock and Polly transport failures
    surface here per design.md "Cross-Cutting Concerns > Telemetry on failure"
    (R16.6). Default 5 is intentionally tighter than Property 33's "> 10"
    runtime gate so operators see infra-side spikes earlier; the runtime ops
    dispatcher continues to gate on its own threshold independently.
  EOT
  type        = number
  default     = 5

  validation {
    condition     = var.decision_error_alarm_threshold >= 1 && var.decision_error_alarm_threshold <= 1000
    error_message = "decision_error_alarm_threshold must be between 1 and 1000 inclusive."
  }
}

# --- Lambda wiring ----------------------------------------------------------
#
# These two variables let 16.7 validate standalone without depending on the
# resources 16.4 provisions (lambda.tf). Both default to null and are resolved
# via locals in cloudwatch_alarms.tf using the same naming convention 16.4
# follows ("${var.project_name}-${var.environment}").

variable "lambda_function_name" {
  description = <<-EOT
    Name of the Lambda function whose log group feeds the decision-error
    metric filter. Leave null to derive "$${var.project_name}-$${var.environment}",
    which matches the naming convention used by 16.4's lambda.tf.
  EOT
  type        = string
  default     = null
}

variable "lambda_log_group_name" {
  description = <<-EOT
    CloudWatch Logs log group to attach the decision-error metric filter to.
    Leave null to derive "/aws/lambda/<lambda_function_name>", which is the
    default log group AWS creates for any Lambda function.
  EOT
  type        = string
  default     = null
}

