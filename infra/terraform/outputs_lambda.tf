# Outputs for Lambda + API Gateway (task 16.4).
#
# Consumers:
#   - Task 16.5 (CloudFront) consumes api_gateway_invoke_url to wire the
#     /v1/* origin behavior on the distribution.
#   - Task 16.7 (CloudWatch alarms) consumes lambda_function_name and
#     lambda_log_group_name as the values for its var.lambda_function_name
#     and var.lambda_log_group_name inputs.
#   - Task 17.1 (deploy pipeline) consumes lambda_function_name as the
#     target of `aws lambda update-function-code`.

output "lambda_function_name" {
  description = "Name of the Joke_API Lambda function. Pass into 16.7's var.lambda_function_name."
  value       = aws_lambda_function.joke_api.function_name
}

output "lambda_function_arn" {
  description = "ARN of the Joke_API Lambda function."
  value       = aws_lambda_function.joke_api.arn
}

output "lambda_role_arn" {
  description = "ARN of the Lambda execution role (for cross-account or auditing references)."
  value       = aws_iam_role.lambda_execution.arn
}

output "lambda_log_group_name" {
  description = "Name of the Lambda's CloudWatch log group. Pass into 16.7's var.lambda_log_group_name."
  value       = aws_cloudwatch_log_group.lambda.name
}

output "api_gateway_id" {
  description = "ID of the HTTP API."
  value       = aws_apigatewayv2_api.joke_api.id
}

output "api_gateway_endpoint" {
  description = "Default HTTP API endpoint (https://<api-id>.execute-api.<region>.amazonaws.com)."
  value       = aws_apigatewayv2_api.joke_api.api_endpoint
}

output "api_gateway_execution_arn" {
  description = "Execution ARN prefix for the HTTP API; used as the source_arn for Lambda permissions."
  value       = aws_apigatewayv2_api.joke_api.execution_arn
}

output "api_gateway_invoke_url" {
  description = "Fully qualified invoke URL for the $default stage. Consumed by 16.5 to wire the CloudFront origin."
  value       = aws_apigatewayv2_stage.default.invoke_url
}
