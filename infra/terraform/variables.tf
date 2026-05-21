# Shared variables for the dadjokes Terraform root module.
# Sibling files (s3.tf, ssm.tf) are encouraged to add their own
# variables_*.tf to keep ownership boundaries clear (see README.md).

variable "aws_region" {
  description = "AWS region for all resources in this module. Defaults to us-east-1 per design.md A6."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Logical project name. Used as a prefix for resource names and as the value of the Project default tag."
  type        = string
  default     = "dadjokes"
}

variable "environment" {
  description = "Deployment environment (e.g., dev, staging, prod). Used in resource names and the Environment default tag."
  type        = string
  default     = "dev"
}
