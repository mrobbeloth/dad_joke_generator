# Inputs for the bootstrap module. All have sensible defaults so a
# straight `terraform apply` works without a tfvars file. Override via
# CLI -var or a *.tfvars file (gitignored) only when needed.

variable "aws_region" {
  description = "AWS region for the bootstrap stack. Must match the workload region (design.md A6)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Resource-name prefix and Project tag value."
  type        = string
  default     = "dadjokes"
}

variable "aws_account_id" {
  description = "AWS account id for this deployment. Embedded in resource names so multi-account installs do not collide."
  type        = string
  default     = "455110962976"
}

variable "tfstate_bucket_name" {
  description = "Globally unique S3 bucket name for the main module's remote state."
  type        = string
  default     = "dadjokes-tfstate-455110962976-us-east-1"
}

variable "tflock_table_name" {
  description = "DynamoDB table name for state locking."
  type        = string
  default     = "dadjokes-tflock"
}

variable "github_owner" {
  description = "GitHub user or organisation owning the deployment repository."
  type        = string
  default     = "mrobbeloth"
}

variable "github_repo" {
  description = "GitHub repository name."
  type        = string
  default     = "dad_joke_generator"
}

variable "github_oidc_audience" {
  description = "OIDC audience for the GitHub provider. AWS requires sts.amazonaws.com."
  type        = string
  default     = "sts.amazonaws.com"
}

variable "github_deploy_role_name" {
  description = "Name of the IAM role GitHub Actions assumes for deploys."
  type        = string
  default     = "dadjokes-github-deploy"
}

variable "github_deploy_session_name_prefix" {
  description = "Session-name prefix that GitHub Actions must use when assuming the deploy role."
  type        = string
  default     = "dadjokes-ci"
}


# Budget configuration (MS03). The budget itself lives in budgets.tf;
# only the email and threshold values are surfaced as variables so an
# operator can adjust them without editing HCL. The budget is disabled
# by default — see budgets.tf for the OSU-IT cost-allocation-tag
# dependency that is blocking re-enablement.
variable "budget_enabled" {
  description = "Whether the dadjokes-scoped AWS Budget is provisioned. Enabled 2026-05-29: the `Proj` cost-allocation tag is active at the org level and this budget is now the primary cost guardrail (replaces the removed account-wide CloudWatch billing alarm). See infra/terraform-bootstrap/budgets.tf."
  type        = bool
  default     = true
}

variable "budget_alert_email" {
  description = "Email address that receives AWS Budgets notifications. Same address is reused for MS08-MS10 SES verification and SNS subscriptions."
  type        = string
  default     = "robbeloth.1@osu.edu"

  validation {
    condition     = can(regex("^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}$", var.budget_alert_email))
    error_message = "budget_alert_email must look like a valid email address."
  }
}

variable "budget_monthly_limit_usd" {
  description = "Monthly dadjokes-scoped budget limit in USD (filtered to Proj=dadjokes). Set to 20 per operator request 2026-05-29. This is the project's primary cost guardrail now that the account-wide CloudWatch billing alarm has been removed (it could not be tag-scoped on the shared account)."
  type        = number
  default     = 20

  validation {
    condition     = var.budget_monthly_limit_usd > 0 && var.budget_monthly_limit_usd <= 1000
    error_message = "budget_monthly_limit_usd must be between 1 and 1000 USD for the Phase 1 MVP."
  }
}
