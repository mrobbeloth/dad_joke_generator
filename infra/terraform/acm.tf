# ACM certificate for the Custom_Domain (task 16.5 / R6.2, R6.4).
#
# CloudFront only accepts viewer certificates issued in us-east-1. The default
# AWS provider in provider.tf already targets us-east-1 (var.aws_region default
# from variables.tf, design.md A6), so we use the default provider here without
# a `provider = aws.us_east_1` alias. If a future change moves the project's
# primary region away from us-east-1, this file must add a us-east-1-aliased
# provider and pin these resources to it.
#
# DNS validation (R6.4): the ACM cert is issued via the DNS-01 method, with
# Terraform writing the CNAME validation records into the existing Route 53
# hosted zone (see route53.tf). Downstream resources (CloudFront, in
# cloudfront.tf) reference aws_acm_certificate_validation.app, NOT the raw
# certificate, so terraform waits for ACM to confirm validation before
# attaching the cert to the distribution.

# ---------------------------------------------------------------------------
# Certificate request.
#
# domain_name covers the primary hostname; subject_alternative_names covers
# any additional aliases (e.g. apex + www, or a staging hostname). Both bind
# the same cert to multiple FQDNs without buying a wildcard.
#
# create_before_destroy lets an in-place renewal swap the new cert into the
# distribution before the old one is destroyed; otherwise CloudFront would
# briefly lose its viewer cert during a re-issue.
# ---------------------------------------------------------------------------
resource "aws_acm_certificate" "app" {
  domain_name               = var.custom_domain
  subject_alternative_names = var.custom_domain_sans
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-cert"
    Environment = var.environment
  }
}

# ---------------------------------------------------------------------------
# Validation completion.
#
# This resource blocks until ACM has observed all DNS validation records
# (created in route53.tf) and marked the certificate ISSUED. CloudFront's
# viewer_certificate references aws_acm_certificate_validation.app.certificate_arn
# rather than aws_acm_certificate.app.arn so terraform serializes correctly:
#
#   1. aws_acm_certificate.app is created (state PENDING_VALIDATION).
#   2. aws_route53_record.cert_validation publishes the CNAMEs.
#   3. aws_acm_certificate_validation.app waits for ACM to verify them.
#   4. aws_cloudfront_distribution.app attaches the now-ISSUED cert.
# ---------------------------------------------------------------------------
resource "aws_acm_certificate_validation" "app" {
  certificate_arn = aws_acm_certificate.app.arn
  validation_record_fqdns = [
    for r in aws_route53_record.cert_validation : r.fqdn
  ]
}
