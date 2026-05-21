# Outputs owned by 16.1 (DynamoDB scaffolding).
#
# S3 outputs belong in outputs_s3.tf (owned by 16.2).
# SSM outputs belong in outputs_ssm.tf (owned by 16.3).

output "dynamodb_table_name" {
  description = "Name of the dadjokes single-table DynamoDB store."
  value       = aws_dynamodb_table.dadjokes.name
}

output "dynamodb_table_arn" {
  description = "ARN of the dadjokes DynamoDB table."
  value       = aws_dynamodb_table.dadjokes.arn
}
