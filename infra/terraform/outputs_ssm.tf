# Outputs for SSM parameters (task 16.3).
#
# IMPORTANT: Only parameter NAMES are exported, never VALUES. The
# /dadjokes/ip_hash_salt parameter is a SecureString and its value must
# never appear in Terraform output, state diffs, or CI logs.
#
# Downstream IAM policy generation (task 16.4) consumes
# `ssm_parameter_names` to build a least-privilege ssm:GetParameter
# policy scoped to exactly these names.

output "ssm_parameter_names" {
  description = "Map of logical key to fully qualified SSM parameter name. Consumed by 16.4 to build least-privilege IAM policies."
  value = {
    daily_limit              = aws_ssm_parameter.daily_limit.name
    bedrock_model_id         = aws_ssm_parameter.bedrock_model_id.name
    polly_voice_id           = aws_ssm_parameter.polly_voice_id.name
    ad_module_enabled        = aws_ssm_parameter.ad_module_enabled.name
    ad_network_id            = aws_ssm_parameter.ad_network_id.name
    ip_hash_salt             = aws_ssm_parameter.ip_hash_salt.name
    cost_alarm_threshold_usd = aws_ssm_parameter.cost_alarm_threshold_usd.name
  }
}

output "daily_limit_parameter_name" {
  description = "Fully qualified SSM name for the per-IP daily joke generation limit (R5.7)."
  value       = aws_ssm_parameter.daily_limit.name
}

output "bedrock_model_id_parameter_name" {
  description = "Fully qualified SSM name for the Bedrock model id used by Joke_Generator (R1.6, R9.4)."
  value       = aws_ssm_parameter.bedrock_model_id.name
}

output "polly_voice_id_parameter_name" {
  description = "Fully qualified SSM name for the Polly voice id used by Voice_Synthesizer (R2.8, R9.4)."
  value       = aws_ssm_parameter.polly_voice_id.name
}

output "ad_module_enabled_parameter_name" {
  description = "Fully qualified SSM name for the advertising-banner master enable flag (R8.1)."
  value       = aws_ssm_parameter.ad_module_enabled.name
}

output "ad_network_id_parameter_name" {
  description = "Fully qualified SSM name for the ad network identifier (R8.4)."
  value       = aws_ssm_parameter.ad_network_id.name
}

output "ip_hash_salt_parameter_name" {
  description = "Fully qualified SSM name for the IP hash salt SecureString (R16.7). Value is intentionally NOT exported."
  value       = aws_ssm_parameter.ip_hash_salt.name
}

output "cost_alarm_threshold_usd_parameter_name" {
  description = "Fully qualified SSM name for the monthly AWS cost alarm threshold in USD (R16.3)."
  value       = aws_ssm_parameter.cost_alarm_threshold_usd.name
}
