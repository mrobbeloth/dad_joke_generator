# AWS provider for the dadjokes infra. Region is sourced from var.aws_region
# (default us-east-1 per design.md A6). Default tags propagate to every taggable
# resource so individual resources do not have to repeat them.
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "Terraform"
    }
  }
}
