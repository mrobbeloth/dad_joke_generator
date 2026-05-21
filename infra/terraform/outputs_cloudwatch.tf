# Outputs for CloudWatch alarms, SNS topics, and metric filters (task 16.7).
#
# Owned by 16.7. Downstream consumers:
#   - 16.4 (lambda IAM): may grant the Lambda role sns:Publish on the
#     cost/ops topic ARNs so joke_api.observability.dispatch_*_alert can
#     publish without the topic ARN being hard-coded in the Lambda env.
#   - Operators / smoke tests: alarm names are exported so external tooling
#     can describe-alarms without re-deriving the naming convention.

output "cost_alerts_topic_arn" {
  description = "ARN of the SNS topic carrying [COST-ALERT] emails (R16.4)."
  value       = aws_sns_topic.cost_alerts.arn
}

output "ops_alerts_topic_arn" {
  description = "ARN of the SNS topic carrying [OPS-ALERT] emails (R16.6)."
  value       = aws_sns_topic.ops_alerts.arn
}

output "cost_alarm_name" {
  description = "Name of the AWS/Billing EstimatedCharges cost alarm (R16.3)."
  value       = aws_cloudwatch_metric_alarm.cost_threshold.alarm_name
}

output "moderation_rejection_alarm_name" {
  description = "Name of the moderation_rejections_per_hour ops alarm (R16.6)."
  value       = aws_cloudwatch_metric_alarm.moderation_rejection_spike.alarm_name
}

output "rate_limit_rejection_alarm_name" {
  description = "Name of the rate_limit_rejections_per_hour ops alarm (R16.6)."
  value       = aws_cloudwatch_metric_alarm.rate_limit_rejection_spike.alarm_name
}

output "observability_failure_alarm_name" {
  description = "Name of the observability_failure ops alarm (R16.6, R16.8)."
  value       = aws_cloudwatch_metric_alarm.observability_failure.alarm_name
}

output "decision_error_alarm_name" {
  description = "Name of the Bedrock/Polly decision-error ops alarm derived from the Lambda log metric filter (R16.6)."
  value       = aws_cloudwatch_metric_alarm.lambda_decision_error.alarm_name
}

output "decision_error_metric_filter_name" {
  description = "Name of the CloudWatch Logs metric filter that counts structured log lines with decision=\"error\" (R16.6)."
  value       = aws_cloudwatch_log_metric_filter.lambda_decision_error.name
}

