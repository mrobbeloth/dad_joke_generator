# Outputs for CloudFront + ACM + Route 53 (task 16.5).
#
# Consumers:
#   - Operator wires `cloudfront_distribution_arn` back into the second
#     `terraform apply` (see the two-stage apply note at the top of
#     cloudfront.tf) so the spa-assets bucket policy in s3.tf attaches.
#   - Task 16.8 (smoke tests) consumes cloudfront_distribution_domain_name
#     and the ACM cert ARN to verify the distribution is reachable and the
#     cert covers the expected SANs (R6.2).
#   - Operators consume route53_zone_id to confirm the existing hosted zone
#     was discovered correctly.

output "cloudfront_distribution_id" {
  description = "ID of the CloudFront distribution. Used for cache invalidations during SPA deploys."
  value       = aws_cloudfront_distribution.app.id
}

output "cloudfront_distribution_arn" {
  description = "ARN of the CloudFront distribution. Pass into the spa-assets bucket policy via -var=cloudfront_distribution_arn=... on a second apply (see cloudfront.tf)."
  value       = aws_cloudfront_distribution.app.arn
}

output "cloudfront_distribution_domain_name" {
  description = "CloudFront-managed domain (e.g. d1234abcd.cloudfront.net). Route 53 alias records target this; visitors reach the site via var.custom_domain instead."
  value       = aws_cloudfront_distribution.app.domain_name
}

output "acm_certificate_arn" {
  description = "ARN of the validated ACM certificate attached to the distribution (R6.2)."
  value       = aws_acm_certificate_validation.app.certificate_arn
}

output "acm_certificate_validation_record_fqdns" {
  description = "Fully-qualified domain names of the DNS validation records published into the hosted zone (R6.4). Useful for smoke tests that assert the records exist."
  value       = [for r in aws_route53_record.cert_validation : r.fqdn]
}

output "route53_zone_id" {
  description = "Zone ID of the existing Route 53 public hosted zone discovered via data.aws_route53_zone.primary."
  value       = data.aws_route53_zone.primary.zone_id
}
