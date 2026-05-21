# Variables for CloudFront + ACM + Route 53 (task 16.5).
#
# Owned by 16.5. Do not add S3, DynamoDB, SSM, Lambda, API Gateway, or
# CloudWatch-alarm variables here; those live in the variables_*.tf files
# owned by 16.1 / 16.2 / 16.3 / 16.4 / 16.7.
#
# Shared variables (project_name, environment, aws_region) come from
# variables.tf (owned by 16.1).
#
# The hosted zone itself is NOT created here — it is an out-of-band
# operator-setup step (docs/PLAN.md MS05). Terraform looks the existing
# hosted zone up by name via a data source.

# ---------------------------------------------------------------------------
# var.custom_domain (R6.1, R6.2)
#
# The single primary hostname the visitor will reach the Web_App at. The
# ACM certificate's domain_name and the CloudFront distribution's first
# alias both bind to this value; Route 53 publishes apex A/AAAA aliases
# pointing at the distribution. No default — the operator must set this
# explicitly per environment so no accidental "example.com" lands in
# production.
# ---------------------------------------------------------------------------
variable "custom_domain" {
  description = <<-EOT
    Primary fully-qualified domain name (FQDN) the Web_App is served at
    (R6.1, R6.2). Example: "dadjokes.example.com". Wildcard apex domains
    are not accepted here; use var.custom_domain_sans for additional names
    that should be covered by the same ACM certificate and CloudFront
    distribution.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", var.custom_domain))
    error_message = "custom_domain must be a lowercase FQDN with at least two labels (e.g. \"dadjokes.example.com\"). Wildcards are not allowed."
  }
}

# ---------------------------------------------------------------------------
# var.custom_domain_sans
#
# Optional Subject Alternative Names added to the ACM certificate and
# CloudFront aliases. Empty list (the default) means the cert and the
# distribution cover only var.custom_domain. Common reasons to populate:
# binding both the apex and "www." subdomain, or covering a staging alias.
#
# The same FQDN regex used on var.custom_domain is applied to every entry,
# so wildcard SANs are not accepted in Phase 1 — keeping the validation
# strict avoids accidentally issuing wildcard certs without explicit
# review. Relaxing this in a future phase is a one-line change.
# ---------------------------------------------------------------------------
variable "custom_domain_sans" {
  description = <<-EOT
    Subject Alternative Names added to the ACM certificate and CloudFront
    aliases (R6.2). Empty list means just var.custom_domain. Each entry
    must satisfy the same FQDN regex as var.custom_domain.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for d in var.custom_domain_sans :
      can(regex("^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", d))
    ])
    error_message = "every entry in custom_domain_sans must be a lowercase FQDN with at least two labels. Wildcards are not allowed."
  }
}

# ---------------------------------------------------------------------------
# var.route53_zone_name
#
# Name of an existing Route 53 public hosted zone that already authoritatively
# answers for var.custom_domain (and any var.custom_domain_sans). Terraform
# looks it up via data.aws_route53_zone.primary in route53.tf; it does NOT
# create the zone. Hosted-zone creation is an out-of-band operator step
# (docs/PLAN.md MS05) because the registrar's NS records need to be updated
# manually after the zone is provisioned, and that handoff is hard to model
# inside a single terraform apply.
# ---------------------------------------------------------------------------
variable "route53_zone_name" {
  description = <<-EOT
    Name of the existing Route 53 hosted zone that authoritatively answers
    for var.custom_domain (e.g. "example.com" when custom_domain is
    "dadjokes.example.com"). The hosted zone itself is created out-of-band
    per docs/PLAN.md MS05; this module only references it.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", var.route53_zone_name))
    error_message = "route53_zone_name must be a lowercase FQDN with at least two labels (e.g. \"example.com\")."
  }
}

# ---------------------------------------------------------------------------
# var.cloudfront_price_class
#
# Optional CloudFront price class override. Default PriceClass_100 (US,
# Canada, Europe) is the cheapest tier and is sufficient for an A6 primary
# region of us-east-1; visitors outside those regions will still resolve
# but get slightly higher RTT. Operators wanting full global edge coverage
# can set this to PriceClass_All; PriceClass_200 adds Asia/Middle East/Africa.
# ---------------------------------------------------------------------------
variable "cloudfront_price_class" {
  description = "CloudFront price class. PriceClass_100 (default) is the cheapest tier; PriceClass_200 and PriceClass_All add more edge locations at higher cost."
  type        = string
  default     = "PriceClass_100"

  validation {
    condition     = contains(["PriceClass_100", "PriceClass_200", "PriceClass_All"], var.cloudfront_price_class)
    error_message = "cloudfront_price_class must be one of PriceClass_100, PriceClass_200, or PriceClass_All."
  }
}
