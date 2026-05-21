# Joke_API Lambda function + log group + API Gateway invoke permission
# (task 16.4 / R12.2).
#
# Design ref: design.md "Joke_API (Lambda handler)". The Lambda runs the
# request_validator -> rate_limiter -> moderation -> Bedrock -> Polly ->
# DynamoDB persist pipeline behind a single HTTP API.

# ---------------------------------------------------------------------------
# Stub deployment package.
#
# Real builds (task 17.1) provide var.lambda_package_path pointing at a
# pre-built zip. For local infrastructure work we synthesize a tiny zip
# with a no-op handler so `terraform validate` and `terraform apply`
# stay usable end-to-end without coupling them to the build pipeline.
# The stub handler shape matches design.md's "joke_api.handler" module
# (task 10.1) so swapping in the real artifact is a no-op for IaC.
# ---------------------------------------------------------------------------
data "archive_file" "lambda_stub" {
  count       = var.lambda_package_path == null ? 1 : 0
  type        = "zip"
  output_path = "${path.module}/.terraform/joke_api_stub.zip"

  source {
    content  = "def lambda_handler(event, context):\n    return {\"statusCode\": 200, \"body\": \"ok\"}\n"
    filename = "joke_api/handler.py"
  }
}

# ---------------------------------------------------------------------------
# CloudWatch log group.
#
# Created explicitly (rather than letting Lambda auto-create on first
# invoke) so retention is managed and so task 16.7 can wire alarms /
# metric filters against a known group name. The function_name is
# "${var.project_name}-${var.environment}", matching what task 16.7
# expects as a default for var.lambda_function_name.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project_name}-${var.environment}"
  retention_in_days = var.lambda_log_retention_days
}

# ---------------------------------------------------------------------------
# Lambda function.
#
# - Runtime python3.12 matches pyproject.toml's requires-python.
# - Handler "joke_api.handler.lambda_handler" assumes the design.md
#   src/joke_api/handler.py module (task 10.1) exposes a function named
#   lambda_handler. This is the conventional AWS Lambda entrypoint name;
#   if task 10.1 chooses a different name it should update this string.
# - arm64 architecture costs ~20% less than x86_64 on Lambda for the
#   same memory size, supporting the design's low-operating-cost goal.
# - PassThrough X-Ray tracing in Phase 1; flip to Active when tracing is
#   actually wired (Phase 2 hardening).
# - source_code_hash forces redeploy whenever the zip contents change.
# ---------------------------------------------------------------------------
resource "aws_lambda_function" "joke_api" {
  function_name = "${var.project_name}-${var.environment}"
  description   = "Joke_API Lambda: routes /v1/jokes, /v1/jokes/{id}, /v1/config, /v1/health (R12.2)."

  role = aws_iam_role.lambda_execution.arn

  filename         = var.lambda_package_path != null ? var.lambda_package_path : data.archive_file.lambda_stub[0].output_path
  source_code_hash = var.lambda_package_path != null ? filebase64sha256(var.lambda_package_path) : data.archive_file.lambda_stub[0].output_base64sha256

  handler       = "joke_api.handler.lambda_handler"
  runtime       = "python3.12"
  architectures = ["arm64"]

  timeout     = var.lambda_timeout_seconds
  memory_size = var.lambda_memory_mb

  tracing_config {
    # Phase 1: respect the upstream sampling decision but do not force
    # tracing on (which would otherwise add per-invoke X-Ray cost).
    mode = "PassThrough"
  }

  environment {
    variables = {
      DADJOKES_TABLE                  = aws_dynamodb_table.dadjokes.name
      DADJOKES_AUDIO_BUCKET           = aws_s3_bucket.audio.id
      DADJOKES_TRAINING_CORPUS_BUCKET = aws_s3_bucket.training_corpus.id
      DADJOKES_SSM_PREFIX             = var.parameter_prefix
      DADJOKES_AWS_REGION             = var.aws_region
    }
  }

  # Ensure the log group exists (and has the right retention) before the
  # Lambda is created. Without this, the first invocation would create
  # the group with the AWS default "Never expire" retention.
  depends_on = [
    aws_cloudwatch_log_group.lambda,
    aws_iam_role_policy.lambda_least_privilege,
    aws_iam_role_policy_attachment.lambda_basic_execution,
  ]
}

# ---------------------------------------------------------------------------
# API Gateway -> Lambda invoke permission.
#
# Granted at the API level using the v2 API's execution_arn and the
# wildcard "*/*" suffix so any route + stage in the API may invoke this
# function. Narrower per-route permissions are not necessary for a
# single-Lambda HTTP API.
# ---------------------------------------------------------------------------
resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowAPIGatewayV2Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.joke_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.joke_api.execution_arn}/*/*"
}
