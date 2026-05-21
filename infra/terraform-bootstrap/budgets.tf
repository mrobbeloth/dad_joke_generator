# Account-total monthly budget (MS03) — currently DISABLED.
#
# # Why this is disabled
#
# This account (455110962976) is a member of the Ohio State University
# AWS Organization (master 683792142612, master email
# cio-aws-master-acct@osu.edu). The account is shared between several
# OSU projects (stoplight-classroom, retro-web-gateway, solterra-threejs,
# enterprise CIO-* roles, CrowdStrike security tooling, dad_joke_generator)
# and racks up ~$200/month in non-dadjokes spend. A whole-account budget
# at the design's $30 threshold therefore fires the moment it is
# created, which is noise, not signal.
#
# The correct fix is a budget scoped to `Project=dadjokes`-tagged
# resources, but that requires the `Project` user-defined cost-allocation
# tag to be activated at the org level, and only the management account
# can do that:
#
#   aws ce update-cost-allocation-tags-status \
#     --cost-allocation-tags-status TagKey=Project,Status=Active
#
# OSU IT has been emailed (2026-05-21). Once they confirm the tag is
# active:
#
#   1. Set `budget_enabled = true` (variable defaults to false).
#   2. Verify a Cost-Explorer probe with a `Project=dadjokes` tag
#      filter returns non-zero on tagged resources, e.g.:
#        aws ce get-cost-and-usage \
#          --time-period Start=YYYY-MM-DD,End=YYYY-MM-DD \
#          --granularity MONTHLY --metrics UnblendedCost \
#          --filter '{"Tags":{"Key":"Project","Values":["dadjokes"]}}'
#   3. terraform apply
#   4. Tick MS03 in docs/PLAN.md with the activation date.
#
# # Why not delete the resource block entirely
#
# Keeping the HCL behind a `count` flag means re-enabling is a single
# variable change rather than a re-implementation. The resource block
# itself is still syntax- and provider-schema-validated by every CI
# `terraform validate` run, so it cannot rot.
#
# References:
#   - design.md A8 (account-total cost guardrail)
#   - PLAN.md MS03

resource "aws_budgets_budget" "account_total" {
  # Disabled until OSU IT activates the `Project` cost-allocation tag at
  # the org level. See the file-level docstring above. Set
  # `budget_enabled = true` to re-enable.
  count = var.budget_enabled ? 1 : 0

  name         = "${var.project_name}-account-total-monthly"
  budget_type  = "COST"
  limit_amount = format("%.2f", var.budget_monthly_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Scope the budget to dadjokes-tagged resources only. Without this
  # filter the budget covers the whole shared account and produces
  # constant false alarms from non-dadjokes workloads. The filter
  # requires the `Project` cost-allocation tag to be activated at the
  # org level (see the file-level docstring).
  cost_filter {
    name   = "TagKeyValue"
    values = ["user:Project$${var.project_name}"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
