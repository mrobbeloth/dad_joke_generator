# S3 outputs (task 16.2). Bucket names and ARNs are exposed for downstream
# tasks: 16.4 (Lambda IAM role policies for audio + training-corpus reads),
# 16.5 (CloudFront origin for spa-assets), and 16.8 (smoke tests).

output "spa_assets_bucket_name" {
  description = "Name of the spa-assets S3 bucket (CloudFront origin)."
  value       = aws_s3_bucket.spa_assets.id
}

output "spa_assets_bucket_arn" {
  description = "ARN of the spa-assets S3 bucket."
  value       = aws_s3_bucket.spa_assets.arn
}

output "audio_bucket_name" {
  description = "Name of the audio S3 bucket (Polly MP3 outputs, 30-day lifecycle)."
  value       = aws_s3_bucket.audio.id
}

output "audio_bucket_arn" {
  description = "ARN of the audio S3 bucket."
  value       = aws_s3_bucket.audio.arn
}

output "training_corpus_bucket_name" {
  description = "Name of the training-corpus S3 bucket (Lambda-only read)."
  value       = aws_s3_bucket.training_corpus.id
}

output "training_corpus_bucket_arn" {
  description = "ARN of the training-corpus S3 bucket."
  value       = aws_s3_bucket.training_corpus.arn
}
