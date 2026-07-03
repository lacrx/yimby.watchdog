data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_execution" {
  name               = "${var.project}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda_permissions" {
  # S3 access — watchdog bucket only
  statement {
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
      "s3:DeleteObject",
    ]
    resources = [
      aws_s3_bucket.watchdog_data.arn,
      "${aws_s3_bucket.watchdog_data.arn}/*",
    ]
  }

  # SSM Parameter Store — read jurisdiction config
  statement {
    actions   = ["ssm:GetParametersByPath"]
    resources = ["arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter${var.ssm_prefix}*"]
  }

  # CloudWatch Logs
  statement {
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:*"]
  }

  # Lambda self-invoke (fan-out pattern)
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.watchdog_pipeline.arn]
  }

  # Explicit deny on stoside resources
  statement {
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      "arn:aws:s3:::stoside-*",
      "arn:aws:s3:::stoside-*/*",
    ]
  }
}

resource "aws_iam_role_policy" "lambda_permissions" {
  name   = "${var.project}-lambda-permissions"
  role   = aws_iam_role.lambda_execution.id
  policy = data.aws_iam_policy_document.lambda_permissions.json
}


# ── Deployer role (human/CI — build, push, terraform apply) ───────────

data "aws_iam_policy_document" "deployer_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:user/lacrx"]
    }
  }
}

resource "aws_iam_role" "watchdog_deployer" {
  name                 = "watchdog-deployer"
  description          = "Deploy role for yimby.watchdog project"
  assume_role_policy   = data.aws_iam_policy_document.deployer_assume.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "deployer_permissions" {
  # S3 — data bucket
  statement {
    sid     = "WatchdogS3"
    actions = ["s3:*"]
    resources = [
      "arn:aws:s3:::yimby-watchdog-*",
      "arn:aws:s3:::yimby-watchdog-*/*",
    ]
  }

  # ECR — push and manage images
  statement {
    sid     = "WatchdogECR"
    actions = ["ecr:*"]
    resources = [
      "arn:aws:ecr:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:repository/yimby-watchdog*",
    ]
  }

  statement {
    sid       = "ECRAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # Lambda — deploy and manage
  statement {
    sid     = "WatchdogLambda"
    actions = ["lambda:*"]
    resources = [
      "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:yimby-watchdog-*",
    ]
  }

  # IAM — manage project roles + self
  statement {
    sid = "WatchdogIAM"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:GetRole",
      "iam:PassRole",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:AttachRolePolicy",
      "iam:DetachRolePolicy",
      "iam:ListInstanceProfilesForRole",
      "iam:TagRole",
      "iam:ListRoleTags",
    ]
    resources = [
      "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/yimby-watchdog-*",
      "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/watchdog-deployer",
    ]
  }

  # SSM — jurisdiction config
  statement {
    sid = "WatchdogSSM"
    actions = [
      "ssm:PutParameter",
      "ssm:GetParametersByPath",
    ]
    resources = [
      "arn:aws:ssm:${data.aws_region.current.name}:*:parameter${var.ssm_prefix}*",
    ]
  }

  # EventBridge Scheduler
  statement {
    sid     = "WatchdogScheduler"
    actions = ["scheduler:*"]
    resources = [
      "arn:aws:scheduler:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:schedule/default/yimby-watchdog-*",
    ]
  }

  # CloudWatch Logs
  statement {
    sid     = "WatchdogLogs"
    actions = ["logs:*"]
    resources = [
      "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/yimby-watchdog-*",
    ]
  }

  # Explicit deny on stoside resources
  statement {
    sid    = "DenyStoside"
    effect = "Deny"
    actions = [
      "s3:*",
      "lambda:*",
      "ecr:*",
      "iam:*",
      "scheduler:*",
    ]
    resources = [
      "arn:aws:s3:::stoside-*",
      "arn:aws:s3:::stoside-*/*",
      "arn:aws:lambda:*:${data.aws_caller_identity.current.account_id}:function:stoside-*",
      "arn:aws:ecr:*:${data.aws_caller_identity.current.account_id}:repository/stoside-*",
      "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/stoside-*",
      "arn:aws:scheduler:*:${data.aws_caller_identity.current.account_id}:schedule/default/stoside-*",
    ]
  }
}

resource "aws_iam_role_policy" "watchdog_deployer_permissions" {
  name   = "watchdog-deploy"
  role   = aws_iam_role.watchdog_deployer.id
  policy = data.aws_iam_policy_document.deployer_permissions.json
}
