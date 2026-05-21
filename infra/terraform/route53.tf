# Route 53 records for the Custom_Domain (task 16.5 / R6.1, R6.4).
#
# This file does NOT create the hosted zone itself — that is an out-of-band
# operator step (docs/PLAN.md MS05). It expects the hosted zone identified
# by var.route53_zone_name to already exist and to be the authoritative
# nameserver for var.custom_domain.
#
# Two record sets live here:
#   1. ACM DNS validation CNAMEs (per-name, per-cert), keyed via for_each
#      over aws_acm_certificate.app.domain_validation_options.
#   2. Apex + SAN A/AAAA alias records pointing at the CloudFront
#      distribution.

# ---------------------------------------------------------------------------
# Hosted zone lookup.
#
# private_zone = false ensures we match the public hosted zone even if a
# private zone with the same name exists in the account (which would
# otherwise make the lookup ambiguous and fail).
# ---------------------------------------------------------------------------
data "aws_route53_zone" "primary" {
  name         = var.route53_zone_name
  private_zone = false
}

# ---------------------------------------------------------------------------
# ACM DNS validation records (R6.4).
#
# domain_validation_options yields one entry per name on the certificate
# (the primary domain plus every SAN). Keying for_each on resource_record_name
# de-duplicates entries when the same validation record applies to multiple
# names (ACM sometimes reuses one CNAME across a SAN set).
#
# allow_overwrite = true lets a re-apply replace a stale validation record
# without manual cleanup (e.g. after the cert is rotated).
# ---------------------------------------------------------------------------
resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.app.domain_validation_options :
    dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id         = data.aws_route53_zone.primary.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

# ---------------------------------------------------------------------------
# Apex / SAN alias records pointing at CloudFront (R6.1).
#
# CloudFront publishes a Z2FDTNDATAQYW2 hosted-zone id (a fixed AWS-owned
# value) which is what aws_cloudfront_distribution.app.hosted_zone_id
# returns. Using an alias record (rather than CNAME) lets the apex of the
# Custom_Domain be answered with A records, which CNAME at the apex cannot
# do per RFC 1034.
#
# evaluate_target_health = false because CloudFront does not expose a
# Route 53 health check target and the distribution is globally fronted.
#
# Each Custom_Domain hostname (the primary plus every SAN) gets both an A
# and an AAAA alias because the distribution has is_ipv6_enabled = true
# in cloudfront.tf — IPv6 visitors must resolve to a AAAA record or they
# fall back to A and lose the IPv6 path.
# ---------------------------------------------------------------------------

locals {
  cdn_aliases = toset(concat([var.custom_domain], var.custom_domain_sans))
}

resource "aws_route53_record" "alias_a" {
  for_each = local.cdn_aliases

  zone_id = data.aws_route53_zone.primary.zone_id
  name    = each.value
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.app.domain_name
    zone_id                = aws_cloudfront_distribution.app.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "alias_aaaa" {
  for_each = local.cdn_aliases

  zone_id = data.aws_route53_zone.primary.zone_id
  name    = each.value
  type    = "AAAA"

  alias {
    name                   = aws_cloudfront_distribution.app.domain_name
    zone_id                = aws_cloudfront_distribution.app.hosted_zone_id
    evaluate_target_health = false
  }
}
