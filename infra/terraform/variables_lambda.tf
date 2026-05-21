# Lambda + API Gateway specific variables (task 16.4).
#
# Owned by 16.4. Do not add S3, DynamoDB, SSM, or CloudWatch-alarm variables
# here; those live in the files owned by 16.1 / 16.2 / 16.3 / 16.7.
#
# Shared variables (project_name, environment, aws_region) come from
# variables.tf (owned by 16.1).

variable "lambda_timeout_seconds" {
  description = <<-EOT
    Hard timeout for the Joke_API Lambda invocation, in seconds.
    Default 30 matches the SPA-side request budget in R7.5 (the per-stage
    sub-budgets are enforced inside the handler, not at the platform level).
    Range 1..900 inclusive (the AWS Lambda maximum is 15 minutes).
  EOT
  type        = number
  default     = 30

  validation {
    condition     = var.lambda_timeout_seconds >= 1 && var.lambda_timeout_seconds <= 900
    error_message = "lambda_timeout_seconds must be between 1 and 900 inclusive."
  }
}

variable "lambda_memory_mb" {
  description = <<-EOT
    Memory size for the Joke_API Lambda, in MB. Lambda also scales vCPU
    proportionally to memory. 512 MB is enough for the boto3 clients
    (Bedrock, Polly, Comprehend, DynamoDB, S3, SSM) plus a small
    in-process few-shot corpus, while keeping cold starts on arm64 below
    the R7.5 budget. Range 128..10240 inclusive (AWS Lambda limits).
  EOT
  type        = number
  default     = 512

  validation {
    condition     = var.lambda_memory_mb >= 128 && var.lambda_memory_mb <= 10240
    error_message = "lambda_memory_mb must be between 128 and 10240 inclusive."
  }
}

variable "lambda_log_retention_days" {
  description = <<-EOT
    Retention, in days, for the Lambda's CloudWatch log group. Default 30.
    CloudWatch only accepts a fixed set of retention values; the validation
    below mirrors the AWS API's allowed list. Use 0 is NOT permitted here
    (CloudWatch's "never expire" sentinel is not an integer the AWS API
    exposes through this attribute).
  EOT
  type        = number
  default     = 30

  validation {
    condition = contains(
      [1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827, 2192, 2557, 2922, 3288, 3653],
      var.lambda_log_retention_days
    )
    error_message = "lambda_log_retention_days must be one of CloudWatch's allowed values: 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827, 2192, 2557, 2922, 3288, 3653."
  }
}

variable "lambda_package_path" {
  description = <<-EOT
    Path to a pre-built Lambda deployment zip on the local filesystem.
    When null (the default), the module synthesizes a tiny stub zip via
    data.archive_file so that `terraform validate` and even
    `terraform apply` succeed for local infrastructure work without a
    real deployment artifact. The deployment pipeline (task 17.1) is
    responsible for building the real zip and supplying its path here.
  EOT
  type        = string
  default     = null
}

variable "cost_alerts_topic_arn" {
  description = <<-EOT
    Optional ARN of an SNS topic that should receive cost-related alerts
    published by the Lambda (R16.3). Defaults to null; when null the
    sns:Publish statement is omitted from the execution role's inline
    policy so the role remains minimum-privilege. Task 16.7 owns the
    alarm topics; the operator wires that output back through this
    variable.
  EOT
  type        = string
  default     = null
}

variable "ops_alerts_topic_arn" {
  description = <<-EOT
    Optional ARN of an SNS topic that should receive operational alerts
    (errors, throttles, p95 latency breaches) published by the Lambda.
    Defaults to null; when null the sns:Publish statement is omitted
    from the execution role's inline policy. Task 16.7 owns the alarm
    topics; the operator wires that output back through this variable.
  EOT
  type        = string
  default     = null
}
