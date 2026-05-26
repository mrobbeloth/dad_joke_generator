# CloudFront Function for the apex-canonical, www→apex 301 redirect.
#
# Pattern: dad-joke-generator.com is the canonical hostname; CloudFront
# 301-redirects www.dad-joke-generator.com → dad-joke-generator.com on
# every request, preserving path + query.
#
# Why a CloudFront Function (and not Lambda@Edge):
#   - Functions run at the viewer-request stage with single-digit-ms
#     overhead and no cold-start.
#   - Cost is roughly free under typical learning-project traffic
#     ($0.10 per 1M invocations after the always-free 2M/month tier).
#   - The redirect is pure string manipulation — no AWS-API calls
#     required — which is exactly what Functions are designed for.
#   - Lambda@Edge would work too but adds Lambda cold-start latency,
#     us-east-1-only deployment friction, and higher per-request cost.
#
# The function is associated with both the SPA cache behavior
# (default_cache_behavior) and the API cache behavior
# (ordered_cache_behavior /v1/*) so a request to
# www.dad-joke-generator.com/v1/jokes redirects to
# dad-joke-generator.com/v1/jokes rather than 404'ing or hitting the API
# under the wrong host.
#
# References:
#   - cloudfront.tf (distribution + cache behaviors)
#   - variables_cdn.tf (var.custom_domain, var.custom_domain_sans)
#   - PLAN.md MS05/MS06

# Locals: the apex hostname is the value the redirect points TO. SANs
# (everything else on the cert) get redirected to it. If the SAN list
# ever grows past www, the function condition automatically picks up
# the new entries via the `redirect_from_hosts` set.
locals {
  redirect_to_host = var.custom_domain

  redirect_from_hosts = [
    for s in var.custom_domain_sans :
    s if s != var.custom_domain
  ]
}

# Render the host-list literal once via jsonencode so the function code
# below reads as plain JS without HCL-side string concatenation.
locals {
  redirect_from_hosts_json = jsonencode(local.redirect_from_hosts)
}

resource "aws_cloudfront_function" "host_redirect" {
  name    = "${var.project_name}-${var.environment}-host-redirect"
  runtime = "cloudfront-js-2.0"
  comment = "301-redirect non-canonical hosts (e.g. www.${var.custom_domain}) to the canonical apex (${local.redirect_to_host}). Preserves path + query."
  publish = true

  # CloudFront-JS 2.0 is closer to modern ES; const/arrow/template strings
  # all supported. The script must export a single `handler(event)`
  # function and complete in <1ms — for a redirect that's trivial.
  code = <<-EOT
    function handler(event) {
      var request = event.request;
      var host = request.headers.host && request.headers.host.value;
      if (!host) {
        return request;
      }

      var redirectFromHosts = ${local.redirect_from_hosts_json};
      var canonicalHost = "${local.redirect_to_host}";

      // Case-insensitive match on the Host header. Only redirect when
      // the host is in the explicit non-canonical list; every other
      // host (including the canonical apex itself) passes through
      // untouched.
      var hostLower = host.toLowerCase();
      var shouldRedirect = false;
      for (var i = 0; i < redirectFromHosts.length; i++) {
        if (redirectFromHosts[i].toLowerCase() === hostLower) {
          shouldRedirect = true;
          break;
        }
      }
      if (!shouldRedirect) {
        return request;
      }

      // Preserve the request path and query string verbatim.
      var queryString = "";
      if (request.querystring) {
        var parts = [];
        for (var key in request.querystring) {
          var entry = request.querystring[key];
          if (entry.multiValue) {
            for (var j = 0; j < entry.multiValue.length; j++) {
              parts.push(
                encodeURIComponent(key) + "=" +
                encodeURIComponent(entry.multiValue[j].value)
              );
            }
          } else {
            parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(entry.value));
          }
        }
        if (parts.length > 0) {
          queryString = "?" + parts.join("&");
        }
      }

      var location = "https://" + canonicalHost + request.uri + queryString;

      return {
        statusCode: 301,
        statusDescription: "Moved Permanently",
        headers: {
          location: { value: location },
          "cache-control": { value: "public, max-age=86400" },
        },
      };
    }
  EOT
}

# The function-association block is added to cloudfront.tf's distribution
# resource via a separate resource attribute change there. Terraform 1.5+
# disallows nested function_association blocks via overrides, so the
# distribution's HCL itself owns the wiring. See cloudfront.tf for the
# event_type = "viewer-request" association.
