terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state in the bootstrap-provisioned S3 bucket + DynamoDB lock
  # table. The bucket and table were created by
  # infra/terraform-bootstrap/state.tf; the names and region here MUST
  # match its outputs:
  #
  #   bucket         = bootstrap output `tfstate_bucket_name`
  #   key            = "main/terraform.tfstate"
  #   dynamodb_table = bootstrap output `tflock_table_name`
  #
  # `encrypt = true` defends in depth on top of the bucket's default
  # SSE-S3 setting (state objects are also encrypted server-side).
  #
  # First operator step in this directory is:
  #
  #   $env:AWS_PROFILE = "dadjokes-admin"
  #   terraform init -migrate-state    # moves any local state into S3
  #
  # Subsequent applies pick the same backend up automatically.
  backend "s3" {
    bucket         = "dadjokes-tfstate-455110962976-us-east-1"
    key            = "main/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "dadjokes-tflock"
    encrypt        = true
  }
}
