# Terraform bootstrap module

One-time bootstrap for the Dad Joke Generator deployment pipeline. This
module exists to break the chicken-and-egg problem of remote Terraform
state: the main module under `infra/terraform/` will store its state in
the S3 bucket created here, and the GitHub Actions deploy job will assume
the IAM role created here.

## What it provisions

| Resource | Purpose |
|---|---|
| `aws_s3_bucket.tfstate` | Remote state bucket for the main module (versioning, SSE, BPA) |
| `aws_dynamodb_table.tflock` | State-lock table (`LockID` PK, on-demand billing) |
| `aws_iam_openid_connect_provider.github` | GitHub Actions OIDC provider |
| `aws_iam_role.github_deploy` | Role GitHub Actions assumes for deploys |
| `aws_iam_role_policy.github_deploy_admin` | Inline admin policy (narrowed later, see TODO below) |

## Bootstrap state

This module's own state lives in a **local file** (`terraform.tfstate` in
this directory) because it cannot store state in a bucket it has not yet
created. The local state file is gitignored. After bootstrap is applied,
this module's state should be moved to the new S3 bucket as well; see
"Move bootstrap state into the new bucket" below.

## How to apply

From this directory, with AWS credentials set to `dadjokes-admin`:

```sh
$env:AWS_PROFILE = "dadjokes-admin"            # PowerShell
# or
$env:AWS_PROFILE="dadjokes-admin"; $env:AWS_REGION="us-east-1"

terraform init
terraform plan   -out=bootstrap.plan
terraform apply  bootstrap.plan
```

The plan output will show ~10 resources to create. Review carefully
before applying. The bucket name is global; if the bucket name in
`variables.tf` collides with one in another account, the plan will
succeed but the apply will fail with `BucketAlreadyExists` and you will
need to pick a different name.

After apply, capture the outputs (`terraform output -json > bootstrap_outputs.json`)
so the values are available when wiring up GitHub Actions secrets and
the main module's backend config.

## TODO after bootstrap

1. **Wire GitHub Actions secrets** (MS15) using the outputs:
   - `secrets.AWS_ROLE_ARN` ← `github_deploy_role_arn`
   - `vars.AWS_REGION`      ← `us-east-1`
   - `vars.SPA_ASSETS_BUCKET` and `vars.CLOUDFRONT_DISTRIBUTION_ID` are
     produced later by the main module.
2. **Add a backend block** to the main module under
   `infra/terraform/versions.tf` (or a new `backend.tf`) pointing at
   the bucket + lock table created here:
   ```hcl
   terraform {
     backend "s3" {
       bucket         = "dadjokes-tfstate-455110962976-us-east-1"
       key            = "main/terraform.tfstate"
       region         = "us-east-1"
       dynamodb_table = "dadjokes-tflock"
       encrypt        = true
     }
   }
   ```
   Then `cd ../terraform && terraform init -migrate-state` to move the
   main module's state into the bucket.
3. **Narrow the deploy role policy** from `AdministratorAccess` to a
   least-privilege policy listing only the actions the main module
   needs (Bedrock invoke-model, Polly synthesize-speech, Comprehend
   detect-toxic-content, DynamoDB on the `dadjokes` tables, S3 on the
   audio + spa-assets buckets, SSM read on `/dadjokes/*`, CloudWatch
   logs/metrics/alarms, IAM only on the Lambda execution role). This
   replaces `iam:AdministratorAccess` with the actual permission set.
4. **Rotate the local IAM user keys** (`DadJokes-Admin`). Once OIDC is
   working end-to-end, the human user keys are only needed for
   emergency state-recovery; you can delete the access key and
   re-generate one only when needed.

## Move bootstrap state into the new bucket (optional but recommended)

After the first apply succeeds, you can move this module's local state
into the same S3 bucket so all Terraform state is centrally managed:

```sh
# Add a backend block to versions.tf in this directory:
terraform {
  backend "s3" {
    bucket         = "dadjokes-tfstate-455110962976-us-east-1"
    key            = "bootstrap/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "dadjokes-tflock"
    encrypt        = true
  }
}
# Then:
terraform init -migrate-state
```

## Tearing it all down

The bootstrap stack should never be torn down without first emptying
the state bucket and removing the main module. Order of operations:

1. `terraform destroy` against the main module (with backend still
   configured — destroys all workload resources).
2. `terraform init -reconfigure -backend=false` against the main module
   to detach from the remote backend.
3. Empty the state bucket (`aws s3 rm --recursive` or via console).
4. `terraform destroy` against this bootstrap module.

If you delete the bootstrap stack first, the main module loses its
state file and locks and will fail to plan or destroy.
