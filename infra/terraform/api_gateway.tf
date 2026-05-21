# API Gateway HTTP API for the Joke_API Lambda (task 16.4 / R12.2).
#
# Design ref: design.md "Joke_API (Lambda handler)" routes table.
# HTTP API (v2) is chosen over REST API (v1) for lower per-request cost
# and built-in JWT/IAM auth options that the v2 product supports if we
# ever need them. Phase 1 is anonymous, so no authorizer is attached.

# ---------------------------------------------------------------------------
# HTTP API.
#
# CORS is intentionally permissive in Phase 1 (any origin, GET/POST,
# content-type) because the SPA is anonymous and the API is read/write
# but not authenticated. Task 16.5 wires a custom domain via CloudFront;
# at that point the operator can narrow allow_origins to that domain if
# desired.
# ---------------------------------------------------------------------------
resource "aws_apigatewayv2_api" "joke_api" {
  name          = "${var.project_name}-${var.environment}"
  description   = "HTTP API fronting the Joke_API Lambda (R12.2)."
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST"]
    allow_headers = ["content-type"]
    # max_age default (0) is fine; keep preflight responses uncached so
    # CORS policy changes take effect immediately.
  }
}

# ---------------------------------------------------------------------------
# Lambda integration (AWS_PROXY, payload format v2).
#
# Payload format 2.0 is the modern HTTP API event shape; the design's
# joke_api.handler is built against it (event.requestContext.http.method,
# event.rawPath, etc.).
# ---------------------------------------------------------------------------
resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.joke_api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.joke_api.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"

  # Default 30s integration timeout matches the Lambda's default
  # function timeout. Keeping them equal avoids the API-Gateway side
  # cutting off requests before the handler can return its sanitized
  # error envelope (R7.5, R7.6).
  timeout_milliseconds = 30000
}

# ---------------------------------------------------------------------------
# Routes (one per design table entry).
#
# Route keys use the HTTP API "METHOD /path" format. Path parameters are
# expressed as {name}. All four routes target the same Lambda integration.
# ---------------------------------------------------------------------------

# POST /v1/jokes -- generate a new joke (R1, R3, R4, R5).
resource "aws_apigatewayv2_route" "post_jokes" {
  api_id    = aws_apigatewayv2_api.joke_api.id
  route_key = "POST /v1/jokes"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

# GET /v1/jokes/{id} -- audit-style retrieval (R18.2).
resource "aws_apigatewayv2_route" "get_joke_by_id" {
  api_id    = aws_apigatewayv2_api.joke_api.id
  route_key = "GET /v1/jokes/{id}"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

# GET /v1/config -- public config blob (R8.1, R8.4, R5.7).
resource "aws_apigatewayv2_route" "get_config" {
  api_id    = aws_apigatewayv2_api.joke_api.id
  route_key = "GET /v1/config"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

# GET /v1/health -- Production_Gate self-health probe (R12.2).
resource "aws_apigatewayv2_route" "get_health" {
  api_id    = aws_apigatewayv2_api.joke_api.id
  route_key = "GET /v1/health"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

# ---------------------------------------------------------------------------
# Stage + access logging.
#
# $default is the implicit stage HTTP APIs use when no stage segment is
# present in the URL, which is what we want fronting a CloudFront
# distribution. auto_deploy = true means the routes above go live as
# soon as terraform applies; no manual deployment step is needed.
#
# Access logs land in a separate log group from the Lambda's own logs
# so retention and metric filters can be tuned independently.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "api_gateway_access" {
  name              = "/aws/apigateway/${var.project_name}-${var.environment}-access"
  retention_in_days = var.lambda_log_retention_days
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.joke_api.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway_access.arn

    # Compact JSON line per request. Captures enough to triage 4xx/5xx
    # without inflating log volume; correlates to Lambda logs via the
    # request id.
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      protocol       = "$context.protocol"
      responseLength = "$context.responseLength"
      integrationErr = "$context.integrationErrorMessage"
    })
  }

  # No default_route_settings throttle here; Phase 1 relies on the
  # per-IP daily limit (R5.7) enforced inside the Lambda. If we ever
  # need a global ceiling, this is the right place to add it.
}
