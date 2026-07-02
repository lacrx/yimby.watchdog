output "bucket_name" {
  value = aws_s3_bucket.watchdog_data.id
}

output "bucket_arn" {
  value = aws_s3_bucket.watchdog_data.arn
}

output "ecr_repository_url" {
  value = aws_ecr_repository.watchdog.repository_url
}

output "lambda_function_name" {
  value = aws_lambda_function.watchdog_pipeline.function_name
}

output "lambda_function_arn" {
  value = aws_lambda_function.watchdog_pipeline.arn
}
