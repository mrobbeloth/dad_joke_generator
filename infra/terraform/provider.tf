# AWS provider for the dadjokes infra. Region is sourced from var.aws_region
# (default us-east-1 per design.md A6). Default tags propagate to every taggable
# resource so individual resources do not have to repeat them.
#
# Tag strategy:
#   - `Project` (capital P, full word) is the human-readable identifier
#     visible in the AWS console.
#   - `Proj` (capital P, abbreviated) is the OSU-IT cost-allocation tag
#     active at the AWS Organization payer-account level (confirmed by
#     Lok Yu, OTDI Cloud Platform, 2026-05-27). Cost Explorer and
#     Budgets filter on this key.
#   - Both carry the same value so they are equivalent for filtering.
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project_name
      Proj      = var.project_name
      ManagedBy = "Terraform"
    }
  }
}
