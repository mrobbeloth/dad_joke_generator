# AWS provider for the bootstrap module. Region pinned to us-east-1
# (design.md A6). Default tags identify every resource as
# bootstrap-owned so they are easy to find / clean up.

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project_name
      Component = "bootstrap"
      ManagedBy = "terraform"
      Module    = "infra/terraform-bootstrap"
    }
  }
}
