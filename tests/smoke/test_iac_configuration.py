"""Smoke tests for the Terraform IaC module under ``infra/terraform/``.

These tests are pure static analysis: they read the ``.tf`` source files
as text and assert that specific resource declarations and attributes
are present. They run without AWS credentials, in CI, on Windows or
Linux, and do not require ``terraform plan``.

Two structural-sanity tests at the bottom (``terraform fmt -check`` and
``terraform validate``) shell out to the Terraform CLI when it is on
PATH and skip cleanly when it is not.

Validates Requirements:
- 6.2  -- ACM certificate covers Custom_Domain via DNS validation, with
          the SAN list bound to var.custom_domain_sans.
- 17.2 -- S3 buckets ``audio`` and ``training-corpus`` have all four
          Block Public Access flags enabled.

Additional structural checks not strictly required by 16.8 but useful
as smoke coverage:
- 5.6 / 18.4 (DynamoDB TTL + retention guards)
- 5.7, 8.1, 8.4, 16.3, 16.7 (SSM parameter set and SecureString lifecycle)
- 6.1, 6.3, 6.5 (CloudFront aliases, redirect-to-https, viewer cert)
- 12.2 (Lambda IAM least-privilege Sid coverage and DynamoDB scope)
- 16.4 / 16.6 (CloudWatch alarms wired to the right SNS topic per
              channel separation, plus the lambda decision-error metric
              filter pattern).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

# Repo root resolved relative to this test file so the test runs the
# same way regardless of pytest's invocation directory.
REPO_ROOT = Path(__file__).resolve().parents[2]
TF_DIR = REPO_ROOT / "infra" / "terraform"


def _load_tf(filename: str) -> str:
    """Return the contents of ``infra/terraform/<filename>`` as a string."""
    path = TF_DIR / filename
    if not path.is_file():
        pytest.fail(f"expected Terraform file is missing: {path}")
    return path.read_text(encoding="utf-8")


def _resource_block(tf: str, resource_type: str, local_name: str) -> str:
    """Return the body of a top-level ``resource "<type>" "<name>"`` block.

    Relies on Terraform's standard formatting (``terraform fmt``): the
    closing brace of a top-level resource lives at column 0. The match
    is non-greedy and anchored at the start of a line, so nested
    ``lifecycle { ... }``, ``ttl { ... }``, etc. are captured intact.
    """
    pattern = (
        rf'resource\s+"{re.escape(resource_type)}"\s+'
        rf'"{re.escape(local_name)}"\s*\{{(.*?)^\}}'
    )
    match = re.search(pattern, tf, flags=re.DOTALL | re.MULTILINE)
    if match is None:
        pytest.fail(
            f'resource "{resource_type}" "{local_name}" not found in '
            f"the supplied Terraform source"
        )
    return match.group(1)


# ---------------------------------------------------------------------------
# Group 1: SSM parameters (R5.7, R8.1, R8.4, R16.3, R16.7)
# ---------------------------------------------------------------------------


class TestSSMParameters:
    """Each design-table SSM parameter is declared in ssm.tf."""

    EXPECTED_PARAMETERS = (
        "daily_limit",
        "bedrock_model_id",
        "polly_voice_id",
        "ad_module_enabled",
        "ad_network_id",
        "ip_hash_salt",
        "cost_alarm_threshold_usd",
    )

    @pytest.mark.parametrize("param_name", EXPECTED_PARAMETERS)
    def test_ssm_parameter_declared_with_prefixed_name(self, param_name: str) -> None:
        """Validates Requirements 5.7, 8.1, 8.4, 16.3, 16.7.

        Each expected SSM parameter is declared and its ``name``
        attribute is anchored at ``${var.parameter_prefix}/<key>`` so
        multi-environment deployments can re-use the same module.
        """
        tf = _load_tf("ssm.tf")
        # The resource block exists.
        body = _resource_block(tf, "aws_ssm_parameter", param_name)
        # And its name attribute references the var.parameter_prefix.
        name_pattern = (
            rf'name\s*=\s*"\$\{{var\.parameter_prefix\}}/{re.escape(param_name)}"'
        )
        assert re.search(name_pattern, body), (
            f'aws_ssm_parameter "{param_name}" does not declare '
            f'name = "${{var.parameter_prefix}}/{param_name}"'
        )

    def test_ip_hash_salt_is_securestring_with_value_ignore_changes(self) -> None:
        """Validates Requirements 16.7.

        The ip_hash_salt parameter must be a SecureString and must
        ignore subsequent value changes so the operator can rotate the
        real salt out-of-band via ``aws ssm put-parameter`` without
        terraform reverting it.
        """
        tf = _load_tf("ssm.tf")
        body = _resource_block(tf, "aws_ssm_parameter", "ip_hash_salt")

        assert re.search(r'type\s*=\s*"SecureString"', body), (
            "ip_hash_salt must be declared with type = \"SecureString\""
        )
        # lifecycle { ignore_changes = [value] } -- whitespace tolerant.
        lifecycle_pattern = r"lifecycle\s*\{\s*ignore_changes\s*=\s*\[\s*value\s*\]\s*\}"
        assert re.search(lifecycle_pattern, body, flags=re.DOTALL), (
            "ip_hash_salt must declare lifecycle { ignore_changes = [value] }"
        )


# ---------------------------------------------------------------------------
# Group 2: ACM cert SAN configuration (R6.2)
# ---------------------------------------------------------------------------


class TestAcmCertificate:
    """ACM cert SANs match Custom_Domain and DNS validation completes."""

    def test_certificate_resource_uses_var_custom_domain(self) -> None:
        """Validates Requirements 6.2.

        ``aws_acm_certificate.app`` is bound to ``var.custom_domain``
        with ``var.custom_domain_sans`` as its SAN list and DNS-01
        validation.
        """
        tf = _load_tf("acm.tf")
        body = _resource_block(tf, "aws_acm_certificate", "app")

        assert re.search(r"domain_name\s*=\s*var\.custom_domain", body), (
            "aws_acm_certificate.app must set domain_name = var.custom_domain"
        )
        assert re.search(
            r"subject_alternative_names\s*=\s*var\.custom_domain_sans", body
        ), (
            "aws_acm_certificate.app must set "
            "subject_alternative_names = var.custom_domain_sans"
        )
        assert re.search(r'validation_method\s*=\s*"DNS"', body), (
            'aws_acm_certificate.app must set validation_method = "DNS"'
        )

    def test_certificate_uses_create_before_destroy_lifecycle(self) -> None:
        """Validates Requirements 6.2.

        Re-issued certs must swap into CloudFront's viewer cert before
        the old one is destroyed, otherwise viewers see a TLS gap.
        """
        tf = _load_tf("acm.tf")
        body = _resource_block(tf, "aws_acm_certificate", "app")

        lifecycle_pattern = (
            r"lifecycle\s*\{\s*create_before_destroy\s*=\s*true\s*\}"
        )
        assert re.search(lifecycle_pattern, body, flags=re.DOTALL), (
            "aws_acm_certificate.app must declare "
            "lifecycle { create_before_destroy = true }"
        )

    def test_certificate_validation_resource_exists(self) -> None:
        """Validates Requirements 6.2.

        CloudFront waits on ``aws_acm_certificate_validation.app`` so
        the cert is ISSUED before being attached.
        """
        tf = _load_tf("acm.tf")
        # Resource is declared.
        _ = _resource_block(tf, "aws_acm_certificate_validation", "app")


# ---------------------------------------------------------------------------
# Group 3: DynamoDB TTL configured (R5.6, R18.4)
# ---------------------------------------------------------------------------


class TestDynamoDBTable:
    """The dadjokes table has TTL, PITR, on-demand billing and a destroy guard."""

    def test_table_ttl_attribute_is_expires_at_and_enabled(self) -> None:
        """Validates Requirements 5.6, 18.4.

        TTL drives both rate-limit reset (R5.6) and the 90-day joke
        retention (R18.4), keyed off the ``expires_at`` attribute.
        """
        tf = _load_tf("dynamodb.tf")
        body = _resource_block(tf, "aws_dynamodb_table", "dadjokes")

        ttl_pattern = (
            r"ttl\s*\{[^}]*?attribute_name\s*=\s*\"expires_at\""
            r"[^}]*?enabled\s*=\s*true[^}]*?\}"
        )
        assert re.search(ttl_pattern, body, flags=re.DOTALL), (
            'aws_dynamodb_table.dadjokes must declare '
            'ttl { attribute_name = "expires_at", enabled = true }'
        )

    def test_table_uses_pay_per_request_billing(self) -> None:
        """Validates Requirements 5.6, 18.4 (cost posture).

        On-demand billing keeps idle cost at zero for a small
        operational table.
        """
        tf = _load_tf("dynamodb.tf")
        body = _resource_block(tf, "aws_dynamodb_table", "dadjokes")
        assert re.search(r'billing_mode\s*=\s*"PAY_PER_REQUEST"', body), (
            'aws_dynamodb_table.dadjokes must use billing_mode = "PAY_PER_REQUEST"'
        )

    def test_table_has_point_in_time_recovery_enabled(self) -> None:
        """Validates Requirements 18.4.

        PITR enables 35 days of restore granularity for live counter
        and joke data.
        """
        tf = _load_tf("dynamodb.tf")
        body = _resource_block(tf, "aws_dynamodb_table", "dadjokes")
        pitr_pattern = (
            r"point_in_time_recovery\s*\{\s*enabled\s*=\s*true\s*\}"
        )
        assert re.search(pitr_pattern, body, flags=re.DOTALL), (
            "aws_dynamodb_table.dadjokes must declare "
            "point_in_time_recovery { enabled = true }"
        )

    def test_table_deletion_protection_enabled(self) -> None:
        """Validates Requirements 18.4.

        Accidental destroy would wipe rate-limit counters; require an
        explicit override before terraform can delete the table.
        """
        tf = _load_tf("dynamodb.tf")
        body = _resource_block(tf, "aws_dynamodb_table", "dadjokes")
        assert re.search(r"deletion_protection_enabled\s*=\s*true", body), (
            "aws_dynamodb_table.dadjokes must set "
            "deletion_protection_enabled = true"
        )


# ---------------------------------------------------------------------------
# Group 4: S3 BPA on `audio` and `training-corpus` (R2.4, R17.2)
# ---------------------------------------------------------------------------


class TestS3PublicAccessBlocks:
    """All four BPA flags are true on every project bucket."""

    BPA_FLAGS = (
        "block_public_acls",
        "block_public_policy",
        "ignore_public_acls",
        "restrict_public_buckets",
    )

    @pytest.mark.parametrize(
        "bucket_local_name",
        # Spec calls out audio + training_corpus specifically; spa_assets
        # is included for completeness because it uses the same hardening
        # posture.
        ["audio", "training_corpus", "spa_assets"],
    )
    def test_bucket_has_public_access_block_with_all_flags_true(
        self, bucket_local_name: str
    ) -> None:
        """Validates Requirements 17.2.

        Each project bucket has an aws_s3_bucket_public_access_block
        resource with all four flags set to true.
        """
        tf = _load_tf("s3.tf")
        body = _resource_block(
            tf, "aws_s3_bucket_public_access_block", bucket_local_name
        )

        for flag in self.BPA_FLAGS:
            assert re.search(rf"{flag}\s*=\s*true", body), (
                f'aws_s3_bucket_public_access_block.{bucket_local_name} '
                f"must set {flag} = true"
            )

    def test_audio_bucket_lifecycle_uses_var_audio_retention_days(self) -> None:
        """Validates Requirements 2.4, 17.2.

        Audio objects expire after the configured retention window so
        Polly outputs do not accumulate indefinitely.
        """
        tf = _load_tf("s3.tf")
        body = _resource_block(tf, "aws_s3_bucket_lifecycle_configuration", "audio")

        expiration_pattern = (
            r"expiration\s*\{\s*days\s*=\s*var\.audio_retention_days\s*\}"
        )
        assert re.search(expiration_pattern, body, flags=re.DOTALL), (
            "aws_s3_bucket_lifecycle_configuration.audio must declare "
            "expiration { days = var.audio_retention_days }"
        )


# ---------------------------------------------------------------------------
# Group 5: CloudFront distribution wiring (R6.1, R6.3, R6.5)
# ---------------------------------------------------------------------------


class TestCloudFrontDistribution:
    """The CloudFront distribution has the right aliases, redirects and TLS."""

    def test_distribution_aliases_include_custom_domain_and_sans(self) -> None:
        """Validates Requirements 6.1, 6.5.

        SNI matching enforces R6.5; the alias list must include the
        primary custom domain plus every SAN.
        """
        tf = _load_tf("cloudfront.tf")
        body = _resource_block(tf, "aws_cloudfront_distribution", "app")
        aliases_pattern = (
            r"aliases\s*=\s*concat\(\s*\[\s*var\.custom_domain\s*\]\s*,"
            r"\s*var\.custom_domain_sans\s*\)"
        )
        assert re.search(aliases_pattern, body, flags=re.DOTALL), (
            "aws_cloudfront_distribution.app must declare "
            "aliases = concat([var.custom_domain], var.custom_domain_sans)"
        )

    def test_default_cache_behavior_redirects_to_https(self) -> None:
        """Validates Requirements 6.3.

        SPA traffic must be 301-redirected from HTTP to HTTPS by
        CloudFront's default behavior.
        """
        tf = _load_tf("cloudfront.tf")
        body = _resource_block(tf, "aws_cloudfront_distribution", "app")
        # Match the default_cache_behavior block and its viewer policy.
        default_block = re.search(
            r"default_cache_behavior\s*\{(.*?)^\s{2}\}",
            body,
            flags=re.DOTALL | re.MULTILINE,
        )
        assert default_block is not None, (
            "aws_cloudfront_distribution.app must declare a "
            "default_cache_behavior block"
        )
        assert re.search(
            r'viewer_protocol_policy\s*=\s*"redirect-to-https"',
            default_block.group(1),
        ), (
            "default_cache_behavior must set "
            'viewer_protocol_policy = "redirect-to-https"'
        )

    def test_v1_path_pattern_ordered_cache_behavior_redirects_to_https(self) -> None:
        """Validates Requirements 6.3.

        API traffic on /v1/* must also redirect HTTP -> HTTPS.
        """
        tf = _load_tf("cloudfront.tf")
        body = _resource_block(tf, "aws_cloudfront_distribution", "app")
        ordered_block = re.search(
            r"ordered_cache_behavior\s*\{(.*?)^\s{2}\}",
            body,
            flags=re.DOTALL | re.MULTILINE,
        )
        assert ordered_block is not None, (
            "aws_cloudfront_distribution.app must declare an "
            "ordered_cache_behavior for /v1/*"
        )
        ordered_body = ordered_block.group(1)
        assert re.search(r'path_pattern\s*=\s*"/v1/\*"', ordered_body), (
            "ordered_cache_behavior must set path_pattern = \"/v1/*\""
        )
        assert re.search(
            r'viewer_protocol_policy\s*=\s*"redirect-to-https"', ordered_body
        ), (
            "ordered_cache_behavior must set "
            'viewer_protocol_policy = "redirect-to-https"'
        )

    def test_viewer_certificate_uses_sni_only_with_modern_tls(self) -> None:
        """Validates Requirements 6.5.

        SNI-only is the modern free option; TLSv1.2_2021 disables
        legacy ciphers.
        """
        tf = _load_tf("cloudfront.tf")
        body = _resource_block(tf, "aws_cloudfront_distribution", "app")
        viewer_block = re.search(
            r"viewer_certificate\s*\{(.*?)^\s{2}\}",
            body,
            flags=re.DOTALL | re.MULTILINE,
        )
        assert viewer_block is not None, (
            "aws_cloudfront_distribution.app must declare a "
            "viewer_certificate block"
        )
        viewer_body = viewer_block.group(1)
        assert re.search(r'ssl_support_method\s*=\s*"sni-only"', viewer_body), (
            'viewer_certificate must set ssl_support_method = "sni-only"'
        )
        assert re.search(
            r'minimum_protocol_version\s*=\s*"TLSv1\.2_2021"', viewer_body
        ), (
            "viewer_certificate must set "
            'minimum_protocol_version = "TLSv1.2_2021"'
        )


# ---------------------------------------------------------------------------
# Group 6: Lambda IAM least-privilege (R12.2, structural)
# ---------------------------------------------------------------------------


class TestLambdaIAMLeastPrivilege:
    """The lambda_execution role has a least-privilege inline policy."""

    REQUIRED_SIDS = (
        "DynamoDBPointAccess",
        "S3AudioBucketObjectAccess",
        "S3TrainingCorpusReadList",
        "S3TrainingCorpusReadObject",
        "SSMReadConfigParameters",
        "KMSDecryptSSMSecureString",
        "BedrockInvokeFoundationModels",
        "PollySynthesizeSpeech",
        "ComprehendDetectToxicContent",
        "CloudWatchPutDadjokesMetrics",
    )

    def test_lambda_execution_role_is_declared_with_assume_role_policy(self) -> None:
        """Validates Requirements 12.2.

        The Lambda role's trust policy is generated from
        ``data.aws_iam_policy_document.lambda_assume_role``.
        """
        tf = _load_tf("iam.tf")
        body = _resource_block(tf, "aws_iam_role", "lambda_execution")
        assert re.search(
            r"assume_role_policy\s*=\s*"
            r"data\.aws_iam_policy_document\.lambda_assume_role\.json",
            body,
        ), (
            "aws_iam_role.lambda_execution must reference "
            "data.aws_iam_policy_document.lambda_assume_role.json"
        )

    @pytest.mark.parametrize("sid", REQUIRED_SIDS)
    def test_least_privilege_policy_includes_required_sid(self, sid: str) -> None:
        """Validates Requirements 12.2.

        Every documented Sid in the inline policy must be present so
        the policy stays in lock-step with design.md's per-component
        AWS service usage table.
        """
        tf = _load_tf("iam.tf")
        assert re.search(rf'sid\s*=\s*"{re.escape(sid)}"', tf), (
            f'lambda_least_privilege policy is missing sid = "{sid}"'
        )

    def test_sns_publish_alerts_is_a_dynamic_statement(self) -> None:
        """Validates Requirements 12.2.

        The SNS Publish grant is opt-in: when neither cost nor ops
        topic ARN is supplied the statement must be omitted entirely.
        ``dynamic "statement"`` is the structural marker for that.
        """
        tf = _load_tf("iam.tf")
        # Find the dynamic block and assert it carries the
        # SNSPublishAlerts sid inside its content body.
        dynamic_pattern = (
            r'dynamic\s+"statement"\s*\{[^{]*?'
            r"content\s*\{[^}]*?"
            r'sid\s*=\s*"SNSPublishAlerts"'
        )
        assert re.search(dynamic_pattern, tf, flags=re.DOTALL), (
            "SNSPublishAlerts must be declared inside a "
            'dynamic "statement" block to support the optional-topic case'
        )

    def test_dynamodb_grant_is_point_only(self) -> None:
        """Validates Requirements 12.2.

        Lambda's DynamoDB grant covers only point read/write actions
        (GetItem, PutItem, UpdateItem). Scan and Query are not allowed
        because both runtime callers (rate_limiter, joke_store) work in
        pk/sk-only mode.
        """
        tf = _load_tf("iam.tf")
        for required in ("dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"):
            assert required in tf, (
                f"least-privilege policy must allow {required}"
            )
        for forbidden in ("dynamodb:Scan", "dynamodb:Query"):
            assert forbidden not in tf, (
                f"least-privilege policy must NOT allow {forbidden}"
            )


# ---------------------------------------------------------------------------
# Group 7: CloudWatch alarms wired to correct SNS topics (R16.4, R16.6)
# ---------------------------------------------------------------------------


class TestCloudWatchAlarmsWiring:
    """Cost alarms publish only to cost_alerts; ops alarms only to ops_alerts."""

    def test_separate_sns_topics_declared(self) -> None:
        """Validates Requirements 16.4, 16.6.

        Channel separation (Property 33) requires two distinct SNS
        topics: one for cost, one for ops.
        """
        tf = _load_tf("cloudwatch_alarms.tf")
        # Both resources are declared.
        _ = _resource_block(tf, "aws_sns_topic", "cost_alerts")
        _ = _resource_block(tf, "aws_sns_topic", "ops_alerts")

    def test_cost_alarm_publishes_only_to_cost_topic(self) -> None:
        """Validates Requirements 16.3, 16.4.

        The cost-threshold alarm wires to the cost_alerts topic and not
        to the ops_alerts topic.
        """
        tf = _load_tf("cloudwatch_alarms.tf")
        body = _resource_block(tf, "aws_cloudwatch_metric_alarm", "cost_threshold")

        assert re.search(
            r"alarm_actions\s*=\s*\[\s*aws_sns_topic\.cost_alerts\.arn\s*\]",
            body,
        ), (
            "cost_threshold alarm must wire alarm_actions to "
            "[aws_sns_topic.cost_alerts.arn]"
        )
        assert "aws_sns_topic.ops_alerts.arn" not in body, (
            "cost_threshold alarm must not reference the ops_alerts topic"
        )

    def test_ops_alarm_publishes_only_to_ops_topic(self) -> None:
        """Validates Requirements 16.6.

        The moderation rejection-spike alarm wires to ops_alerts and
        not to cost_alerts (channel separation per Property 33).
        """
        tf = _load_tf("cloudwatch_alarms.tf")
        body = _resource_block(
            tf, "aws_cloudwatch_metric_alarm", "moderation_rejection_spike"
        )

        assert re.search(
            r"alarm_actions\s*=\s*\[\s*aws_sns_topic\.ops_alerts\.arn\s*\]",
            body,
        ), (
            "moderation_rejection_spike alarm must wire alarm_actions to "
            "[aws_sns_topic.ops_alerts.arn]"
        )
        assert "aws_sns_topic.cost_alerts.arn" not in body, (
            "moderation_rejection_spike alarm must not reference the "
            "cost_alerts topic"
        )

    def test_lambda_decision_error_metric_filter_pattern(self) -> None:
        """Validates Requirements 16.6, 16.8.

        The metric filter that powers the Bedrock/Polly error alarm
        uses the JSON pattern ``{ $.decision = "error" }`` so any
        structured log line with ``decision = "error"`` increments the
        counter.
        """
        tf = _load_tf("cloudwatch_alarms.tf")
        body = _resource_block(
            tf, "aws_cloudwatch_log_metric_filter", "lambda_decision_error"
        )
        # Whitespace inside the JSON pattern is part of the wire format,
        # but we keep the regex tolerant on tokens just outside it.
        pattern_re = r'pattern\s*=\s*"\{ \$\.decision = \\"error\\" \}"'
        assert re.search(pattern_re, body), (
            'aws_cloudwatch_log_metric_filter.lambda_decision_error must '
            'set pattern = "{ $.decision = \\"error\\" }"'
        )


# ---------------------------------------------------------------------------
# Group 8: Module-level structural sanity (terraform fmt + validate)
# ---------------------------------------------------------------------------


class TestTerraformModuleSanity:
    """``terraform fmt -check -recursive`` and ``terraform validate`` succeed."""

    @staticmethod
    def _terraform_or_skip() -> str:
        path = shutil.which("terraform")
        if path is None:
            pytest.skip("terraform not on PATH")
        return path

    def test_terraform_fmt_check_recursive_passes(self) -> None:
        """Validates Requirements 6.2, 17.2 (module hygiene).

        Run ``terraform fmt -check -recursive`` from a subprocess and
        assert exit code 0.
        """
        terraform = self._terraform_or_skip()
        completed = subprocess.run(
            [terraform, "fmt", "-check", "-recursive"],
            cwd=str(TF_DIR),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, (
            "terraform fmt -check -recursive returned non-zero. "
            f"stdout=\n{completed.stdout}\nstderr=\n{completed.stderr}"
        )

    def test_terraform_validate_succeeds(self) -> None:
        """Validates Requirements 6.2, 17.2 (module hygiene).

        Run ``terraform validate`` from a subprocess and assert exit
        code 0 with the success marker on stdout.
        """
        terraform = self._terraform_or_skip()
        completed = subprocess.run(
            [terraform, "validate"],
            cwd=str(TF_DIR),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert completed.returncode == 0, (
            "terraform validate returned non-zero. "
            f"stdout=\n{completed.stdout}\nstderr=\n{completed.stderr}"
        )
        assert "Success" in completed.stdout, (
            "terraform validate stdout did not contain 'Success'. "
            f"stdout=\n{completed.stdout}\nstderr=\n{completed.stderr}"
        )
