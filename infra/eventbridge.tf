resource "aws_scheduler_schedule" "nightly" {
  name       = "${var.project}-nightly"
  group_name = "default"

  schedule_expression          = var.schedule_nightly
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.watchdog_pipeline.arn
    role_arn = aws_iam_role.eventbridge_invoke.arn
    input    = jsonencode({ phase = "full" })
  }
}

resource "aws_scheduler_schedule" "afternoon" {
  name       = "${var.project}-afternoon"
  group_name = "default"

  schedule_expression          = var.schedule_afternoon
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.watchdog_pipeline.arn
    role_arn = aws_iam_role.eventbridge_invoke.arn
    input    = jsonencode({ phase = "full" })
  }
}

# IAM role for EventBridge to invoke Lambda
data "aws_iam_policy_document" "eventbridge_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eventbridge_invoke" {
  name               = "${var.project}-eventbridge"
  assume_role_policy = data.aws_iam_policy_document.eventbridge_assume.json
}

data "aws_iam_policy_document" "eventbridge_invoke_lambda" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.watchdog_pipeline.arn]
  }
}

resource "aws_iam_role_policy" "eventbridge_invoke_lambda" {
  name   = "${var.project}-eventbridge-invoke"
  role   = aws_iam_role.eventbridge_invoke.id
  policy = data.aws_iam_policy_document.eventbridge_invoke_lambda.json
}
