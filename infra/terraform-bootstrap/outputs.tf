# Outputs consumed by:
#   - the main module's S3 backend block (bucket, lock table, region)
#   - the GitHub Actions secrets / variables wiring (deploy role ARN)
#   - PLAN.md MS15 ("GitHub Actions repository secrets configured")
#
# After apply, capture all outputs with:
#
#   terraform output -json > bootstrap_outputs.json
#
# bootstrap_outputs.json is gitignored.

output "tfstate_bucket_name" {
  description = "S3 bucket name for the main module's remote state."
  value       = aws_s3_bucket.tfstate.bucket
}

output "tfstate_bucket_arn" {
  description = "ARN of the remote-state bucket."
  value       = aws_s3_bucket.tfstate.arn
}

output "tflock_table_name" {
  description = "DynamoDB table name used for state locking."
  value       = aws_dynamodb_table.tflock.name
}

output "tflock_table_arn" {
  description = "ARN of the state-lock table."
  value       = aws_dynamodb_table.tflock.arn
}

output "github_oidc_provider_arn" {
  description = "OIDC provider ARN for GitHub Actions (shared with other modules in this account)."
  value       = data.aws_iam_openid_connect_provider.github.arn
}

output "github_deploy_role_arn" {
  description = "ARN to set as the AWS_ROLE_ARN GitHub Actions secret. Wires MS15."
  value       = aws_iam_role.github_deploy.arn
}

output "github_deploy_role_name" {
  description = "IAM role name (matches the role-session-name-prefix expected by the workflow)."
  value       = aws_iam_role.github_deploy.name
}

output "aws_region" {
  description = "Region where everything was provisioned. Set as vars.AWS_REGION in GitHub."
  value       = var.aws_region
}

output "main_module_backend_block" {
  description = "Drop-in HCL backend block for infra/terraform/versions.tf (or a new backend.tf)."
  value       = <<-EOT
    terraform {
      backend "s3" {
        bucket         = "${aws_s3_bucket.tfstate.bucket}"
        key            = "main/terraform.tfstate"
        region         = "${var.aws_region}"
        dynamodb_table = "${aws_dynamodb_table.tflock.name}"
        encrypt        = true
      }
    }
  EOT
}
