# SSM Parameter Store entries for the dadjokes service (task 16.3).
#
# Each parameter below corresponds to one row of the
# "Configuration (SSM Parameter Store)" table in design.md and traces to
# specific requirements in requirements.md:
#
#   /dadjokes/daily_limit               -> R5.7
#   /dadjokes/bedrock_model_id          -> R1.6, R9.4
#   /dadjokes/polly_voice_id            -> R2.8, R9.4
#   /dadjokes/ad_module_enabled         -> R8.1
#   /dadjokes/ad_network_id             -> R8.4
#   /dadjokes/ip_hash_salt              -> R16.7  (SecureString)
#   /dadjokes/cost_alarm_threshold_usd  -> R16.3
#
# Names are parameterized through var.parameter_prefix so that multi-env
# deployments can use e.g. "/dadjokes-dev/..." without code changes.
#
# Resources are declared explicitly (one aws_ssm_parameter per entry)
# instead of being generated from a `for_each` map. Explicit declaration
# is more reviewable for governance and produces clearer plan diffs when
# a single parameter is touched.

# --- daily_limit (R5.7) ------------------------------------------------------
resource "aws_ssm_parameter" "daily_limit" {
  name        = "${var.parameter_prefix}/daily_limit"
  description = "Per-IP daily joke generation limit (R5.7). Range 5..10."
  type        = "String"
  value       = tostring(var.daily_limit)
  tier        = "Standard"
}

# --- bedrock_model_id (R1.6, R9.4) -------------------------------------------
resource "aws_ssm_parameter" "bedrock_model_id" {
  name        = "${var.parameter_prefix}/bedrock_model_id"
  description = "Bedrock on-demand model id used by Joke_Generator (R1.6, R9.4)."
  type        = "String"
  value       = var.bedrock_model_id
  tier        = "Standard"
}

# --- polly_voice_id (R2.8, R9.4) ---------------------------------------------
resource "aws_ssm_parameter" "polly_voice_id" {
  name        = "${var.parameter_prefix}/polly_voice_id"
  description = "Polly Standard-engine voice id used by Voice_Synthesizer (R2.8, R9.4)."
  type        = "String"
  value       = var.polly_voice_id
  tier        = "Standard"
}

# --- ad_module_enabled (R8.1) ------------------------------------------------
# Stored as a string ("true" / "false") because SSM has no native boolean type.
resource "aws_ssm_parameter" "ad_module_enabled" {
  name        = "${var.parameter_prefix}/ad_module_enabled"
  description = "Master enable flag for the optional advertising banner (R8.1)."
  type        = "String"
  value       = var.ad_module_enabled ? "true" : "false"
  tier        = "Standard"
}

# --- ad_network_id (R8.4) ----------------------------------------------------
# Empty string is permitted: the Ad_Module treats empty as "no network
# configured" and renders no slot. AWS SSM rejects literal empty strings, so
# fall back to a single space which the runtime trims to "" (R8.4).
resource "aws_ssm_parameter" "ad_network_id" {
  name        = "${var.parameter_prefix}/ad_network_id"
  description = "Identifier of the single ad network to load when ad_module_enabled is true (R8.4)."
  type        = "String"
  value       = length(var.ad_network_id) == 0 ? " " : var.ad_network_id
  tier        = "Standard"
}

# --- ip_hash_salt (R16.7) ----------------------------------------------------
# SecureString. Terraform writes a placeholder on first apply and ignores all
# subsequent value changes so the real 32+ byte random salt can be set
# out-of-band without ever landing in the Terraform state plaintext.
#
# Operator runbook (matches docs/PLAN.md task 15.1, R15.1):
#
#   aws ssm put-parameter \
#     --name /dadjokes/ip_hash_salt \
#     --type SecureString \
#     --value "$(openssl rand -base64 48)" \
#     --overwrite
#
# The runtime length check (>= 32 bytes) is enforced in
# joke_api.config.load, which is the single source of truth for that rule.
resource "aws_ssm_parameter" "ip_hash_salt" {
  name        = "${var.parameter_prefix}/ip_hash_salt"
  description = "HMAC salt for client IP hashing (R16.7). Real value is set out-of-band via aws ssm put-parameter."
  type        = "SecureString"
  value       = var.ip_hash_salt
  tier        = "Standard"

  lifecycle {
    ignore_changes = [value]
  }
}

# --- cost_alarm_threshold_usd (R16.3) ----------------------------------------
resource "aws_ssm_parameter" "cost_alarm_threshold_usd" {
  name        = "${var.parameter_prefix}/cost_alarm_threshold_usd"
  description = "Monthly AWS cost alarm threshold in USD (R16.3). Range 1.00..10000."
  type        = "String"
  value       = format("%.2f", var.cost_alarm_threshold_usd)
  tier        = "Standard"
}
