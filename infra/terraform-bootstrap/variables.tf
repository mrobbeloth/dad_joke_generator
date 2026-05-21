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
