# CloudFront distribution fronting the SPA + Joke_API (task 16.5 / R6.1,
# R6.3, R6.5).
#
# Two-stage apply note
# --------------------
# The spa-assets bucket policy (s3.tf, owned by 16.2) attaches only when
# `var.cloudfront_distribution_arn` is non-empty. The distribution defined
# below is the source of that ARN, so the apply order is:
#
#   1. terraform apply  (no -var for cloudfront_distribution_arn)
#        Creates the distribution; bucket policy resource is count = 0.
#   2. terraform apply -var="cloudfront_distribution_arn=$(terraform output -raw cloudfront_distribution_arn)"
#        Re-runs and attaches the OAC bucket policy to spa-assets.
#
# The alternative (declaring the bucket policy here) would step on 16.2's
# file ownership boundary, so we accept the two-stage apply pattern.
#
# Architecture summary
# --------------------
# - SPA origin: s3 bucket spa-assets, accessed via Origin Access Control
#   (OAC, the modern replacement for OAI). Default cache behavior. SPA
#   client-side routing fallback via custom 403/404 -> /index.html.
# - API origin: API Gateway HTTP API ($default stage). /v1/* path pattern
#   forwards to it with caching disabled and viewer headers passed through
#   (minus Host, which would break API Gateway's host-based routing).
# - Viewer cert: ACM cert from acm.tf, SNI-only, TLS 1.2_2021 minimum.
#   CloudFront terminates TLS only when SNI matches an alias (R6.5).
# - HTTP visitors are 301-redirected to HTTPS, preserving path + query
#   (R6.3) — CloudFront's redirect-to-https behavior does this natively.

# ---------------------------------------------------------------------------
# Origin Access Control (OAC) for the spa-assets bucket (R6.1, R6.5).
#
# Replaces the legacy Origin Access Identity (OAI). OAC signs every origin
# request with SigV4 so the S3 bucket policy in s3.tf can scope GetObject
# to AWS:SourceArn = this distribution. signing_behavior = always means
# CloudFront signs even when the viewer request doesn't carry the headers
# that S3 requires; without "always" some S3 reads would fail.
# ---------------------------------------------------------------------------
resource "aws_cloudfront_origin_access_control" "spa_assets" {
  name                              = "${var.project_name}-${var.environment}-spa-oac"
  description                       = "OAC granting CloudFront SigV4-signed read access to spa-assets (R6.1, R6.5)."
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# ---------------------------------------------------------------------------
# AWS-managed cache + origin-request policies.
#
# Pulling these from data sources rather than declaring our own keeps the
# config terse and tracks AWS upstream improvements (e.g. when CloudFront
# adds new compression types to Managed-CachingOptimized).
#
#   Managed-CachingOptimized       — default SPA caching (gzip/brotli, TTL).
#   Managed-CachingDisabled        — API responses are dynamic; do not cache.
#   Managed-AllViewerExceptHostHeader — forward viewer headers/query/cookies
#                                       to the API origin EXCEPT Host, which
#                                       would override API Gateway's
#                                       expected execute-api hostname.
# ---------------------------------------------------------------------------
data "aws_cloudfront_cache_policy" "managed_caching_optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_cache_policy" "managed_caching_disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "managed_all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

# ---------------------------------------------------------------------------
# Response headers policy.
#
# Attaches standard browser security headers to every response served by
# the distribution. The Requirement-6 acceptance criteria do not pin
# specific values, but operators expect HSTS + X-Content-Type-Options +
# X-Frame-Options + Referrer-Policy on a TLS-fronted SPA. Values match
# common hardening defaults; tighten override = true so origin headers
# never leak through.
#
# HSTS: 1 year (31536000 s), include subdomains, preload-eligible.
# X-Content-Type-Options: nosniff (override = true).
# X-Frame-Options: DENY (no embedding) — the Web_App is not designed to be
#                  iframed; clickjacking protection is essentially free.
# Referrer-Policy: strict-origin-when-cross-origin — same-origin gets the
#                  full URL, cross-origin gets only the origin, downgrade
#                  to no referrer if HTTP. Sensible default for an
#                  anonymous SPA.
# ---------------------------------------------------------------------------
resource "aws_cloudfront_response_headers_policy" "security_headers" {
  name    = "${var.project_name}-${var.environment}-security-headers"
  comment = "Standard browser security headers attached to every response (R6 hardening)."

  security_headers_config {
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      preload                    = true
      override                   = true
    }

    content_type_options {
      override = true
    }

    frame_options {
      frame_option = "DENY"
      override     = true
    }

    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }
  }
}

# ---------------------------------------------------------------------------
# Distribution (R6.1, R6.3, R6.5).
#
# - aliases bind every name on the cert; CloudFront enforces SNI matching
#   here, satisfying R6.5 (non-matching SNI -> TLS terminated, no Web_App
#   content delivered).
# - viewer_certificate references aws_acm_certificate_validation.app
#   rather than the raw certificate so terraform waits for ACM ISSUED
#   before this resource creates.
# - default cache behavior serves the SPA from S3 with redirect-to-https
#   (R6.3). CloudFront's redirect preserves path and query string by
#   default — no extra config required.
# - ordered_cache_behavior for /v1/* sends API traffic to API Gateway with
#   caching disabled and the viewer's headers/query/cookies forwarded.
# - custom_error_response remaps S3 403/404 to /index.html so the SPA
#   client-side router can handle deep links without a real S3 object.
# - logging_config is intentionally omitted in Phase 1; CloudFront access
#   logs are a Phase 2 cost-vs-value decision.
# ---------------------------------------------------------------------------
resource "aws_cloudfront_distribution" "app" {
  enabled             = true
  is_ipv6_enabled     = true
  http_version        = "http2and3"
  comment             = "${var.project_name}-${var.environment} CDN"
  price_class         = var.cloudfront_price_class
  default_root_object = "index.html"
  aliases             = concat([var.custom_domain], var.custom_domain_sans)

  # ----- SPA origin: S3 spa-assets via OAC ---------------------------------
  origin {
    origin_id                = "spa-assets"
    domain_name              = aws_s3_bucket.spa_assets.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.spa_assets.id
  }

  # ----- API origin: API Gateway $default stage ----------------------------
  # api_endpoint is "https://<api-id>.execute-api.<region>.amazonaws.com".
  # CloudFront's origin domain_name accepts a hostname only — we strip the
  # scheme via replace(). origin_protocol_policy = https-only forces
  # CloudFront -> API Gateway TLS even though the viewer side is also TLS,
  # so an attacker who could reach CloudFront's egress cannot downgrade to
  # plaintext.
  origin {
    origin_id   = "joke-api"
    domain_name = replace(aws_apigatewayv2_api.joke_api.api_endpoint, "https://", "")

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  # ----- Default cache behavior (SPA) --------------------------------------
  default_cache_behavior {
    target_origin_id       = "spa-assets"
    viewer_protocol_policy = "redirect-to-https" # R6.3
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    cache_policy_id            = data.aws_cloudfront_cache_policy.managed_caching_optimized.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security_headers.id
  }

  # ----- /v1/* -> API Gateway ----------------------------------------------
  # The HTTP API exposes routes under /v1/* (see api_gateway.tf). CloudFront
  # forwards these to the API origin without caching and with viewer
  # headers/query/cookies passed through (minus Host).
  ordered_cache_behavior {
    path_pattern           = "/v1/*"
    target_origin_id       = "joke-api"
    viewer_protocol_policy = "redirect-to-https" # R6.3 applies to API too.
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    cache_policy_id            = data.aws_cloudfront_cache_policy.managed_caching_disabled.id
    origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.managed_all_viewer_except_host.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security_headers.id
  }

  # ----- SPA client-side routing fallback ----------------------------------
  # An unauthenticated S3 GET for a missing object returns 403 (with BPA on)
  # rather than 404, so we remap both to a 200 for /index.html. The SPA's
  # router then resolves the deep link client-side. ttl 0 prevents
  # CloudFront from caching the rewrite past a successful deploy.
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  # ----- Viewer certificate (R6.2, R6.5) -----------------------------------
  # SNI-only is the modern free option (vs. dedicated IP, which costs
  # ~$600/mo). Minimum protocol TLSv1.2_2021 disables the older 1.0/1.1
  # ciphers; current AWS-recommended security policy.
  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.app.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  # ----- Geo restriction ---------------------------------------------------
  # Phase 1: open globally. Future phases may add an allowlist for cost
  # control if abusive traffic appears.
  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-cdn"
    Environment = var.environment
  }
}
