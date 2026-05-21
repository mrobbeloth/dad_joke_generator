# dadjokes Terraform module

Flat root module for the Phase 1 / MVP Dad Joke Generator infrastructure.
The surface is intentionally small (one DynamoDB table, one S3 bucket, a
handful of SSM parameters), so resources live directly under
`infra/terraform/` rather than in nested modules. We can extract modules
later if the surface grows.

## What this module provisions

- DynamoDB single-table store `dadjokes-<environment>` with on-demand
  billing, TTL on `expires_at`, point-in-time recovery, server-side
  encryption, and deletion protection. (Owned by task 16.1.)
- S3 audio bucket and lifecycle / public-access block. (Owned by task 16.2,
  delivered in `s3.tf`, `variables_s3.tf`, `outputs_s3.tf`.)
- SSM Parameter Store entries for runtime configuration. (Owned by task
  16.3, delivered in `ssm.tf`, `variables_ssm.tf`, `outputs_ssm.tf`.)

The DynamoDB table is the only resource defined as of task 16.1. The S3 and
SSM resources land in their own files in subsequent tasks.

## File-ownership convention

To make parallel work safe, each task in the 16.x batch owns a disjoint
set of files:

| Task | Owns                                                                    |
|------|-------------------------------------------------------------------------|
| 16.1 | `versions.tf`, `provider.tf`, `variables.tf`, `outputs.tf`, `dynamodb.tf`, `README.md`, `.gitignore` |
| 16.2 | `s3.tf`, `variables_s3.tf`, `outputs_s3.tf`                             |
| 16.3 | `ssm.tf`, `variables_ssm.tf`, `outputs_ssm.tf`                          |

Shared scaffolding (provider, versions, default tags, region, project
name) lives in the 16.1 files. Task 16.2 and 16.3 add their resources
without editing the scaffolding files.

## Where to set AWS credentials

This module uses the standard AWS provider credential chain. Pick whichever
is convenient locally:

- Environment variables: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  `AWS_SESSION_TOKEN` (if using STS).
- Shared config / credentials files under `~/.aws/`.
- AWS SSO via `aws sso login` and `AWS_PROFILE`.
- An IAM role attached to the host (EC2 instance profile, GitHub Actions
  OIDC role, etc.).

The module does not embed credentials.

## How to run

From this directory:

```sh
terraform fmt -recursive          # format files in place
terraform fmt -check -recursive   # CI-style format check (no changes)
terraform init -backend=false     # download providers; skip remote state
terraform validate                # syntactic + provider-schema validation
terraform plan                    # show what would change (needs creds)
terraform apply                   # apply changes (needs creds)
```

`terraform validate` is offline once providers are downloaded, so it can
run in CI without AWS credentials. `plan` and `apply` need real
credentials and a real AWS account.

## Variables

| Name           | Default     | Purpose                                                    |
|----------------|-------------|------------------------------------------------------------|
| `aws_region`   | `us-east-1` | AWS region for all resources (design.md A6).               |
| `project_name` | `dadjokes`  | Resource-name prefix and `Project` default tag.            |
| `environment`  | `dev`       | Environment suffix in resource names.                      |

Override via `-var`, `-var-file=foo.tfvars`, or `TF_VAR_*` environment
variables. Per `.gitignore`, `*.tfvars` files are not committed.
