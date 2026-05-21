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

   **Do not attempt to hand-write this policy.** A deploy role's
   permissions are CRUD operations against the Terraform-managed
   resources (`CreateTable`, `PutBucketPolicy`, `CreateRole`,
   `CreateAlarm`, etc.), not the runtime invocation actions the Lambda
   role uses. The bullet list above is approximate; the real action set
   depends on Terraform provider versions, optional features the module
   enables, and bootstrapping IAM API calls (e.g. `iam:TagRole`) the
   provider issues that are not easy to predict from the HCL.

   See **"Narrowing the deploy role after first apply (MS12 runbook)"**
   below for the IAM Access Analyzer workflow that generates the
   policy from real CloudTrail events of a successful deploy.
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


## Narrowing the deploy role after first apply (MS12 runbook)

The `dadjokes-github-deploy` role currently has `AdministratorAccess`
attached. This is intentional bootstrap-time scaffolding: a deploy role
needs CRUD permissions against every resource the main module
provisions, which spans roughly fifteen AWS services. Hand-writing that
policy without a known-good baseline is error-prone and will produce
either a policy that's too narrow (the next deploy fails halfway with
authorization errors) or too broad (defeats the point of narrowing).

The right pattern is: **deploy once with admin, capture the actual API
calls, narrow to exactly what was used.** AWS provides
`iam:GenerateServiceLastAccessedDetails` and IAM Access Analyzer's
"generate policy from CloudTrail" feature for exactly this workflow.

### Prerequisites

- The main module under `infra/terraform/` has been applied at least
  once via the GitHub Actions deploy job, end-to-end with no failures.
- CloudTrail is logging in `us-east-1` (it is by default for management
  events; confirm in the CloudTrail console).
- You have a CloudTrail event-history time window that covers the entire
  duration of that successful deploy (default retention is 90 days).

### Generating the policy

1. **Open IAM Access Analyzer in the console**
   `IAM → Access Analyzer → Generate policy → Generate policy`.

2. **Select the principal** — pick `dadjokes-github-deploy` as the role
   to analyze.

3. **Select the time window** — span from before the deploy started to
   after it finished. A 24-hour window covering one successful CI run is
   safest; Access Analyzer ignores activity outside the role's actual
   usage.

4. **Click Generate** — the analyzer scans CloudTrail for actions taken
   under the role's session. Generation runs for several minutes
   (proportional to event volume).

5. **Review the generated policy** — the result is a JSON document
   listing every `Action` and `Resource` actually used. Cross-reference
   against the main module's resource ARNs to confirm there are no
   surprises (e.g. wildcard `*` resources should generally be replaced
   with the explicit ARN that came from the module's outputs).

6. **Copy the JSON** and save it to
   `infra/terraform-bootstrap/policies/github_deploy.json`.

### Wiring the narrowed policy into Terraform

The bootstrap module currently attaches `AdministratorAccess` via
`aws_iam_role_policy_attachment.github_deploy_admin`. Replace it with an
inline policy resource:

```hcl
resource "aws_iam_role_policy" "github_deploy" {
  name   = "${var.project_name}-github-deploy-least-privilege"
  role   = aws_iam_role.github_deploy.id
  policy = file("${path.module}/policies/github_deploy.json")
}
```

Then remove (`terraform state rm`) the
`aws_iam_role_policy_attachment.github_deploy_admin` resource and
`terraform apply`. The next CI deploy will run with the narrowed policy.

### Validating the narrowed policy

The first CI deploy after narrowing is the validation: if the policy is
too restrictive, Terraform will report `AccessDeniedException` for some
specific action and the deploy will fail. Capture the missing action,
add it to the JSON, and re-apply. Two or three iterations typically
suffice to converge.

If iteration is too slow, AWS Service Last Accessed (the
`aws iam generate-service-last-accessed-details` /
`get-service-last-accessed-details` API pair) provides per-service
"last used at" timestamps which can also flag unused permissions.

### When to re-run

Re-run the Access Analyzer policy generation whenever:
- A new AWS service is added to the main module (e.g. switching from
  Polly to a different TTS).
- A provider version bump introduces new IAM actions (rare; documented
  in the AWS provider changelog under "Required IAM permissions").
- An apply fails with `AccessDenied` and the missing action turns out to
  be one the deploy actually needs.

### Why not just keep AdministratorAccess

For a Phase 1 MVP on a single-developer learning account, the practical
risk of a leaked admin role token is bounded by GitHub OIDC's
`sub`-claim restriction (the role can only be assumed from the
`mrobbeloth/dad_joke_generator` repo's `main` branch or a PR in that
repo). An attacker needs to push code to the repo to use the role,
and you'd notice.

For a production deployment serving real users, the answer is "narrow
the role" — the same OIDC restriction is in place, but the blast radius
of any escape route (e.g. an action's `iam:PassRole` to a privileged
role) is unbounded with admin and capped at the policy with narrowing.
The narrow-down is therefore part of the production-readiness
checklist, not the MVP-checkpoint checklist.

