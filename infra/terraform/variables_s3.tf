# S3-specific variables (task 16.2). Keep this file scoped to inputs that only
# the S3 buckets need; shared variables (project_name, environment, region)
# live in variables.tf and are owned by 16.1.

variable "audio_retention_days" {
  description = "Number of days after which objects in the audio bucket are expired by the lifecycle rule (R2.4). Default 30."
  type        = number
  default     = 30

  validation {
    condition     = var.audio_retention_days >= 1 && var.audio_retention_days <= 365
    error_message = "audio_retention_days must be between 1 and 365 (inclusive)."
  }
}

variable "cloudfront_distribution_arn" {
  description = "ARN of the CloudFront distribution that should be allowed to read from spa-assets. Empty string disables the bucket policy and is the expected state in Phase 1 until task 16.5 wires up CloudFront."
  type        = string
  default     = ""
}
