# GitHub Actions OIDC provider + deploy role.
#
# Trust model:
#   - The OIDC provider is shared infrastructure in this AWS account
#     (created by another module, tagged Project=stoplight-classroom).
#     AWS allows only one provider per issuer URL per account, so we
#     reference the existing one via a data source rather than try to
#     manage it here. This avoids fighting with the other module on
#     every apply.
#   - The deploy role's trust policy further restricts which workflow
#     contexts can assume it: only `pull_request` and the `main`
#     branch in the configured repo. Forks and arbitrary refs are
#     rejected by the StringLike condition on `sub`.
#
# Inline policy (scoped down post-bootstrap):
#   - For now, AdministratorAccess is granted via a managed-policy
#     attachment so the first end-to-end deploy can succeed without
#     enumerating every IAM action used by the main module. The README
#     TODO #3 describes the narrow least-privilege policy that should
#     replace it before opening the deployment to public traffic.

# Look up the existing OIDC provider rather than create a new one.
# AWS rejects a second provider for the same issuer URL with HTTP 409
# EntityAlreadyExists. The lookup is by URL; AWS returns the canonical
# ARN regardless of who created the provider.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "github_deploy_assume" {
  statement {
    sid     = "AllowGitHubActionsAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = [var.github_oidc_audience]
    }

    # Restrict to this repo and to the `main` branch + pull_request
    # contexts. Anything else (forks, tags, other branches) is rejected.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_owner}/${var.github_repo}:ref:refs/heads/main",
        "repo:${var.github_owner}/${var.github_repo}:pull_request",
      ]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name               = var.github_deploy_role_name
  description        = "Role assumed by GitHub Actions to deploy the Dad Joke Generator main module."
  assume_role_policy = data.aws_iam_policy_document.github_deploy_assume.json

  # 1 hour is enough for any single deploy. Sessions auto-renew within
  # a workflow if needed; lower limits reduce blast radius if a token
  # leaks.
  max_session_duration = 3600
}

# Bootstrap-only AdministratorAccess attachment. README TODO #3
# describes the narrowed-down inline policy that should replace this
# before the first production deployment.
resource "aws_iam_role_policy_attachment" "github_deploy_admin" {
  role       = aws_iam_role.github_deploy.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}
