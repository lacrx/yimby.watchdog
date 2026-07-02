resource "aws_lambda_function" "watchdog_pipeline" {
  function_name = "${var.project}-pipeline"
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.watchdog.repository_url}:latest"
  role          = aws_iam_role.lambda_execution.arn
  timeout       = var.lambda_timeout
  memory_size   = var.lambda_memory

  environment {
    variables = {
      WATCHDOG_S3_BUCKET = aws_s3_bucket.watchdog_data.id
      WATCHDOG_DATA_DIR  = "/tmp/data"
    }
  }
}

# Log group auto-created by Lambda on first invocation.
# Retention managed via console or CLI if needed.
