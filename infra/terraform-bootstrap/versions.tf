# Provider and Terraform version constraints for the bootstrap module.
#
# The bootstrap module's own state lives in a local file (it cannot use
# the S3 backend it is creating). After bootstrap is applied you can
# optionally migrate this module's state into the same bucket; see
# README.md.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
}
