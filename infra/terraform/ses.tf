# SES domain identity verification + DKIM + custom MAIL FROM
# (PLAN.md MS08).
#
# # What this provides
#
# - A verified SESv2 email identity for `dad-joke-generator.com` so the
#   account can send email from any address `*@dad-joke-generator.com`.
# - Easy DKIM (SES-managed signing) with the three CNAME records SES
#   requires. DKIM is enabled by default for new SESv2 identities; we
#   only need to publish the public-key records into Route 53.
# - A custom MAIL FROM subdomain (`mail.dad-joke-generator.com`) so
#   bounces, complaints, and SPF reports come back through our domain
#   rather than `amazonses.com`. Matches modern reputation best practice.
#
# # What this does NOT do
#
# - Move the SES account out of sandbox mode. New SESv2 accounts can
#   only send to verified addresses by default. Lifting that limit
#   requires opening an AWS support case ("Request production access")
#   which is a manual operator step. SNS email subscribers (used for
#   the cost / ops alerts in this stack) do NOT need SES at all -- SNS
#   has its own delivery infrastructure -- so MS09 and MS10 work
#   regardless of SES sandbox status.
# - Configure outbound feedback (bounce/complaint SNS topics). Phase 1
#   does not yet send custom-branded email; when we do, we will add
#   `aws_sesv2_configuration_set_event_destination` and a feedback SNS
#   topic.
#
# References:
#   - design.md A8 (alert delivery)
#   - PLAN.md MS08
#   - https://docs.aws.amazon.com/ses/latest/dg/send-email-authentication-dkim-easy.html
#   - https://docs.aws.amazon.com/ses/latest/dg/mail-from.html

# ---------------------------------------------------------------------------
# Domain identity (R-id N/A; sender authentication infrastructure).
#
# `aws_sesv2_email_identity` declares the domain as a sender identity. SES
# returns three DKIM tokens (the public-key fingerprints SES will use to
# sign outgoing messages); we publish those as CNAMEs in Route 53 below.
# Once the CNAMEs propagate (~minutes), SES marks the identity verified.
# ---------------------------------------------------------------------------
resource "aws_sesv2_email_identity" "primary" {
  email_identity = var.custom_domain

  # Easy DKIM with 2048-bit keys. RSA-2048 is the SES default and is
  # accepted by every major mail provider; specifying it explicitly
  # protects against a future SES default change.
  dkim_signing_attributes {
    next_signing_key_length = "RSA_2048_BIT"
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-ses-identity"
    Environment = var.environment
  }
}

# ---------------------------------------------------------------------------
# DKIM CNAMEs.
#
# SES returns three DKIM tokens (32-char hex strings). For each, the
# public verification record lives at
#
#   <token>._domainkey.<domain>  CNAME  <token>.dkim.amazonses.com.
#
# Publishing all three lets SES rotate signing keys without breaking
# downstream verification.
#
# `for_each` keys on static index strings ("0", "1", "2") rather than the
# token values themselves: SES doesn't expose the tokens until the
# identity is created, and Terraform refuses to plan a `for_each` whose
# keys are unknown at plan time. Indexing by stable position lets the
# whole stack come up in a single apply. SES always returns exactly
# three tokens, so the index range is static.
# ---------------------------------------------------------------------------
resource "aws_route53_record" "ses_dkim" {
  for_each = toset(["0", "1", "2"])

  zone_id = data.aws_route53_zone.primary.zone_id
  name = format(
    "%s._domainkey.%s",
    aws_sesv2_email_identity.primary.dkim_signing_attributes[0].tokens[tonumber(each.key)],
    var.custom_domain
  )
  type = "CNAME"
  ttl  = 300
  records = [
    format(
      "%s.dkim.amazonses.com",
      aws_sesv2_email_identity.primary.dkim_signing_attributes[0].tokens[tonumber(each.key)]
    )
  ]
  allow_overwrite = true
}

# ---------------------------------------------------------------------------
# Custom MAIL FROM domain.
#
# By default SES uses a `mailfrom.amazonses.com` Return-Path. With a
# custom MAIL FROM, bounce reports come back through our own subdomain,
# which improves sender reputation (RFC 7489 / DMARC alignment) and
# keeps amazonses.com out of bounce DSN headers.
#
# behavior_on_mx_failure = USE_DEFAULT_VALUE means if the MX record we
# publish below is missing or fails resolution, SES silently falls back
# to its default sender domain rather than rejecting the message. For a
# learning project this is the safer default; production senders that
# care about strict alignment may want REJECT_MESSAGE.
# ---------------------------------------------------------------------------
resource "aws_sesv2_email_identity_mail_from_attributes" "primary" {
  email_identity         = aws_sesv2_email_identity.primary.email_identity
  behavior_on_mx_failure = "USE_DEFAULT_VALUE"
  mail_from_domain       = "mail.${var.custom_domain}"
}

# ---------------------------------------------------------------------------
# MAIL FROM MX record.
#
# SES requires an MX record pointing at one of its regional feedback
# endpoints so DSN bounces have somewhere to go. Per AWS docs, the
# regional value for us-east-1 is `feedback-smtp.us-east-1.amazonses.com`
# at priority 10.
#
# References:
#   - https://docs.aws.amazon.com/ses/latest/dg/mail-from.html
# ---------------------------------------------------------------------------
resource "aws_route53_record" "ses_mail_from_mx" {
  zone_id = data.aws_route53_zone.primary.zone_id
  name    = "mail.${var.custom_domain}"
  type    = "MX"
  ttl     = 300
  records = ["10 feedback-smtp.${var.aws_region}.amazonses.com"]

  allow_overwrite = true
}

# ---------------------------------------------------------------------------
# MAIL FROM SPF (TXT).
#
# Authorizes amazonses.com to send mail on behalf of the MAIL FROM
# subdomain. `~all` (soft-fail) rather than `-all` so a misconfigured
# downstream relay degrades to "received but flagged" rather than
# outright rejection.
# ---------------------------------------------------------------------------
resource "aws_route53_record" "ses_mail_from_spf" {
  zone_id = data.aws_route53_zone.primary.zone_id
  name    = "mail.${var.custom_domain}"
  type    = "TXT"
  ttl     = 300
  records = ["v=spf1 include:amazonses.com ~all"]

  allow_overwrite = true
}
