# Variables for SSM Parameter Store entries (task 16.3).
#
# Owned by 16.3. Do not add S3 or DynamoDB variables here.
# Each value below feeds an aws_ssm_parameter resource in ssm.tf.
#
# Defaults are chosen so that `terraform apply` immediately matches the
# runtime expectations encoded in docs/COST_REPORT.md Section 5
# (amazon.nova-lite-v1:0 + Joanna Standard voice) and the daily-limit
# range mandated by R5.7.

variable "parameter_prefix" {
  description = <<-EOT
    Prefix applied to every SSM parameter name created by this module.
    The full name is "$${var.parameter_prefix}/<key>". Default matches the
    design.md SSM table ("/dadjokes"). Override per environment if you want
    "/dadjokes-dev/..." style isolation.
  EOT
  type        = string
  default     = "/dadjokes"

  validation {
    condition     = startswith(var.parameter_prefix, "/")
    error_message = "parameter_prefix must start with a forward slash (e.g. \"/dadjokes\")."
  }
}

variable "daily_limit" {
  description = "Per-IP daily joke generation limit. Range 5..10 inclusive (R5.7)."
  type        = number
  default     = 5

  validation {
    condition     = var.daily_limit >= 5 && var.daily_limit <= 10
    error_message = "daily_limit must be between 5 and 10 inclusive (R5.7)."
  }
}

variable "bedrock_model_id" {
  description = "Bedrock on-demand model id used by Joke_Generator. Default per docs/COST_REPORT.md Section 5 (R1.6, R9.4)."
  type        = string
  default     = "amazon.nova-lite-v1:0"

  validation {
    condition     = length(trimspace(var.bedrock_model_id)) > 0
    error_message = "bedrock_model_id must be a non-empty string."
  }
}

variable "polly_voice_id" {
  description = "Polly Standard-engine voice id used by Voice_Synthesizer. Default per docs/COST_REPORT.md Section 5 (R2.8, R9.4)."
  type        = string
  default     = "Joanna"

  validation {
    condition     = length(trimspace(var.polly_voice_id)) > 0
    error_message = "polly_voice_id must be a non-empty string."
  }
}

variable "ad_module_enabled" {
  description = "Master enable flag for the optional advertising banner (R8.1). Stored as the string \"true\"/\"false\" in SSM."
  type        = bool
  default     = false
}

variable "ad_network_id" {
  description = "Identifier of the single ad network to load when ad_module_enabled is true (R8.4). Empty string is allowed and means \"no network configured yet\"."
  type        = string
  default     = ""
}

variable "ip_hash_salt" {
  description = <<-EOT
    Salt used by joke_api.ip_hashing for HMAC-ing client IPs before storage (R16.7).
    Operationally this value MUST be at least 32 random bytes. Terraform stores
    a placeholder here and the SSM resource is configured with
    `lifecycle { ignore_changes = [value] }` so the real salt can be written
    out-of-band by the operator after the first apply (see ssm.tf for the
    aws-cli command). The runtime length check is enforced by
    joke_api.config.load, not by this variable, because the placeholder is
    intentionally shorter than 32 chars.
  EOT
  type        = string
  default     = "REPLACE_VIA_AWS_CLI"
  sensitive   = true
}

variable "cost_alarm_threshold_usd" {
  description = "Monthly AWS cost alarm threshold in USD. Range 1.00..10000 inclusive (R16.3)."
  type        = number
  default     = 10.0

  validation {
    condition     = var.cost_alarm_threshold_usd >= 1.0 && var.cost_alarm_threshold_usd <= 10000.0
    error_message = "cost_alarm_threshold_usd must be between 1.00 and 10000 inclusive (R16.3)."
  }
}
