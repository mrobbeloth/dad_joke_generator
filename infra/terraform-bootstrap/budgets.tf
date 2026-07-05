# dadjokes-scoped monthly budget (MS03) — ENABLED, $20 limit.
#
# This is the project's PRIMARY cost guardrail. The account-wide
# CloudWatch billing alarm that originally satisfied R16.3 was removed
# on 2026-05-29 (see infra/terraform/cloudwatch_alarms.tf) because
# AWS/Billing EstimatedCharges cannot be scoped to a cost-allocation
# tag and therefore fired constantly on non-dadjokes spend on this
# shared OSU account. This budget filters to `Proj=dadjokes` and is the
# correct dadjokes-only cost signal.
#
# # Background
#
# This account (455110962976) is a member of the Ohio State University
# AWS Organization (master 683792142612, master email
# cio-aws-master-acct@osu.edu). The account is shared between several
# OSU projects (stoplight-classroom, retro-web-gateway, solterra-threejs,
# enterprise CIO-* roles, CrowdStrike security tooling, dad_joke_generator)
# and racks up ~$200/month in non-dadjokes spend. A whole-account budget
# at the design's $30 threshold therefore fires the moment it is
# created, which is noise, not signal. The correct fix is a budget
# scoped to dadjokes-tagged resources only.
#
# # The Proj cost-allocation tag
#
# OSU OTDI Cloud Platform (Lok Yu, yu.31@osu.edu, 2026-05-27) confirmed
# the existing user-defined cost-allocation tag at the org level is
# `Proj` (not `Project`). The provider's default_tags blocks in both
# infra/terraform-bootstrap/provider.tf and infra/terraform/provider.tf
# now emit BOTH `Project=dadjokes` (human-readable, console-visible)
# AND `Proj=dadjokes` (the cost-allocation key). Cost Explorer and
# Budgets filter on the latter; the cost_filter below uses
# `user:Proj$dadjokes`.
#
# # Why this is disabled right now (separate from the original deferral)
#
# Cost-allocation tag data backfills with a delay of up to 24 hours
# after a resource is first tagged. We just re-tagged every resource
# in this stack on 2026-05-29 (terraform apply that propagated the
# `Proj` default tag to every taggable resource). Until backfill
# completes, the budget would report $0 even with the filter set
# correctly. The plan is:
#
#   1. terraform apply with budget_enabled=false (re-tags resources;
#      this is the apply you are reviewing now).
#   2. Wait ~24 hours.
#   3. Verify with a Cost-Explorer probe:
#        aws ce get-cost-and-usage \
#          --time-period Start=YYYY-MM-DD,End=YYYY-MM-DD \
#          --granularity MONTHLY --metrics UnblendedCost \
#          --filter '{"Tags":{"Key":"Proj","Values":["dadjokes"]}}'
#      If the result is non-zero, the budget filter will work.
#   4. Set budget_enabled = true and terraform apply again.
#   5. Tick MS03 in docs/PLAN.md with the activation date.
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
  # Disabled until cost-allocation tag data backfills on the newly-applied
  # `Proj` tag (see the file-level docstring above). Set
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
  # requires the `Proj` cost-allocation tag to be activated at the
  # org level (active per OSU IT confirmation 2026-05-27); see
  # the file-level docstring above.
  # AWS Budgets TagKeyValue syntax is "user:<TagKey>$<TagValue>", which
  # needs a LITERAL dollar sign between key and value. Terraform's `$$`
  # escaping is error-prone here (an earlier `$${var.project_name}`
  # produced the literal string "user:Proj${var.project_name}" and
  # silently filtered to $0). `format()` sidesteps escaping entirely:
  # the `$` in the format string is a plain character and `%s` is the
  # interpolated project name, yielding "user:Proj$dadjokes".
  cost_filter {
    name   = "TagKeyValue"
    values = [format("user:Proj$%s", var.project_name)]
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
