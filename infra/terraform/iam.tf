# IAM execution role + least-privilege inline policy for the Joke_API
# Lambda (task 16.4 / R12.2).
#
# Design ref: design.md "Joke_API (Lambda handler)" + the per-component
# AWS service usage. The inline policy is generated from a
# data "aws_iam_policy_document" so resource ARNs and conditions stay
# explicit and reviewable; locals below collapse the SSM parameter ARNs
# and the optional SNS-topic list to keep the policy document readable.

# AWS-managed KMS key alias used to encrypt SecureString SSM parameters
# (only /dadjokes/ip_hash_salt is SecureString today). Looked up via data
# source so the policy can grant kms:Decrypt on its real ARN rather than
# using a wildcard.
data "aws_kms_key" "ssm" {
  key_id = "alias/aws/ssm"
}

# ---------------------------------------------------------------------------
# Trust policy: Lambda service can assume this role.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    sid     = "LambdaServiceAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_execution" {
  name               = "${var.project_name}-${var.environment}-lambda-exec"
  description        = "Execution role for the Joke_API Lambda (R12.2). Least-privilege inline policy + AWSLambdaBasicExecutionRole."
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# CloudWatch Logs basic write access. The managed policy grants
# logs:CreateLogStream and logs:PutLogEvents on /aws/lambda/* which is
# what aws_lambda_function expects out of the box.
resource "aws_iam_role_policy_attachment" "lambda_basic_execution" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ---------------------------------------------------------------------------
# Locals: collect SSM parameter ARNs and the optional SNS topic list.
# Keeping these in locals (rather than inline) makes the policy document
# below easier to read and to diff in code review.
# ---------------------------------------------------------------------------
locals {
  # Every SSM parameter the Lambda is allowed to read. Exhaustive list
  # matches the seven entries in ssm.tf (16.3). Adding a new parameter
  # there requires extending this list.
  ssm_parameter_arns = [
    aws_ssm_parameter.daily_limit.arn,
    aws_ssm_parameter.bedrock_model_id.arn,
    aws_ssm_parameter.polly_voice_id.arn,
    aws_ssm_parameter.ad_module_enabled.arn,
    aws_ssm_parameter.ad_network_id.arn,
    aws_ssm_parameter.ip_hash_salt.arn,
    aws_ssm_parameter.cost_alarm_threshold_usd.arn,
  ]

  # SNS topic ARNs the Lambda may publish to. Operator wires 16.7 outputs
  # through var.cost_alerts_topic_arn / var.ops_alerts_topic_arn. Null
  # entries are filtered out so the resulting list is empty when neither
  # topic is supplied; in that case the sns:Publish statement is skipped
  # entirely (see the `dynamic "statement"` block below).
  sns_topic_arns = compact([
    var.cost_alerts_topic_arn,
    var.ops_alerts_topic_arn,
  ])
}

# ---------------------------------------------------------------------------
# Least-privilege inline policy (R12.2).
#
# Each statement's resources are scoped to the smallest ARN(s) the
# associated AWS service supports. Where a service does not support
# resource-level ARNs (Polly, Comprehend, CloudWatch metrics) we use "*"
# and add a Condition to narrow the grant where possible.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_least_privilege" {
  # DynamoDB: point reads/writes only. No Scan/Query because the access
  # patterns in rate_limiter.py and joke_store.py are pk/sk lookups.
  statement {
    sid    = "DynamoDBPointAccess"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
    ]
    resources = [aws_dynamodb_table.dadjokes.arn]
  }

  # S3 audio bucket: write Polly outputs and read them back to construct
  # presigned URLs. Object-level only; no list permission is needed.
  statement {
    sid    = "S3AudioBucketObjectAccess"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
    ]
    resources = ["${aws_s3_bucket.audio.arn}/*"]
  }

  # S3 training-corpus bucket: read-only (R17.2). List is required so the
  # few-shot loader can enumerate available examples; Get is the actual
  # read. Two separate ARNs because s3:ListBucket is a bucket-level
  # action while s3:GetObject is object-level.
  statement {
    sid    = "S3TrainingCorpusReadList"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
    ]
    resources = [aws_s3_bucket.training_corpus.arn]
  }
  statement {
    sid    = "S3TrainingCorpusReadObject"
    effect = "Allow"
    actions = [
      "s3:GetObject",
    ]
    resources = ["${aws_s3_bucket.training_corpus.arn}/*"]
  }

  # SSM Parameter Store: scoped to the exact seven parameters the
  # config loader reads. GetParameters is bulk-read; GetParameter is the
  # single-key form. No Put / Delete / Describe permissions.
  statement {
    sid    = "SSMReadConfigParameters"
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
    ]
    resources = local.ssm_parameter_arns
  }

  # KMS Decrypt for the SSM SecureString (/dadjokes/ip_hash_salt). The
  # AWS-managed alias/aws/ssm key is the default encryption key for
  # SecureString parameters.
  statement {
    sid       = "KMSDecryptSSMSecureString"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [data.aws_kms_key.ssm.arn]

    # Belt-and-braces: only allow Decrypt when the request originates
    # from SSM itself. Prevents misuse if the role is ever assumed for
    # an unexpected purpose.
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.aws_region}.amazonaws.com"]
    }
  }

  # Bedrock: InvokeModel only, scoped to foundation models in the
  # current region. The specific model id is configurable at runtime via
  # SSM (R1.6, R9.4), so wildcarding the model name within the regional
  # foundation-model namespace keeps the policy stable when the operator
  # switches models without redeploying.
  statement {
    sid       = "BedrockInvokeFoundationModels"
    effect    = "Allow"
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:aws:bedrock:${var.aws_region}::foundation-model/*"]
  }

  # Polly: SynthesizeSpeech does not support resource-level permissions,
  # so the resource must be "*". The action itself is narrow enough.
  statement {
    sid       = "PollySynthesizeSpeech"
    effect    = "Allow"
    actions   = ["polly:SynthesizeSpeech"]
    resources = ["*"]
  }

  # Comprehend: DetectToxicContent is the only call the Input/Output
  # moderators make. Like Polly, Comprehend does not support
  # resource-level ARNs.
  statement {
    sid       = "ComprehendDetectToxicContent"
    effect    = "Allow"
    actions   = ["comprehend:DetectToxicContent"]
    resources = ["*"]
  }

  # CloudWatch metrics: PutMetricData has no resource-level ARN support,
  # but it does support a cloudwatch:namespace condition. Restricting to
  # the "dadjokes" namespace prevents accidental cross-tenant metric
  # publication.
  statement {
    sid       = "CloudWatchPutDadjokesMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["dadjokes"]
    }
  }

  # SNS Publish: emitted only when at least one alert topic ARN is
  # supplied (16.7 wires this through). dynamic "statement" prevents
  # generating an empty Resource list when both ARNs are null, which
  # would otherwise trip the AWS IAM policy validator.
  dynamic "statement" {
    for_each = length(local.sns_topic_arns) > 0 ? [1] : []
    content {
      sid       = "SNSPublishAlerts"
      effect    = "Allow"
      actions   = ["sns:Publish"]
      resources = local.sns_topic_arns
    }
  }
}

resource "aws_iam_role_policy" "lambda_least_privilege" {
  name   = "${var.project_name}-${var.environment}-lambda-least-privilege"
  role   = aws_iam_role.lambda_execution.id
  policy = data.aws_iam_policy_document.lambda_least_privilege.json
}
