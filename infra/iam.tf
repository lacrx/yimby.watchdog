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
  name                 = "civic-deployer"
  description          = "Unified deploy role for civic monitoring projects"
  assume_role_policy   = data.aws_iam_policy_document.deployer_assume.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "deployer_permissions" {
  # S3 — data buckets (both projects)
  statement {
    sid     = "S3Buckets"
    actions = ["s3:*"]
    resources = [
      "arn:aws:s3:::yimby-watchdog-*",
      "arn:aws:s3:::yimby-watchdog-*/*",
      "arn:aws:s3:::stoside-*",
      "arn:aws:s3:::stoside-*/*",
    ]
  }

  # ECR — push and manage images
  statement {
    sid     = "ECR"
    actions = ["ecr:*"]
    resources = [
      "arn:aws:ecr:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:repository/civic-watchdog*",
    ]
  }

  statement {
    sid       = "ECRAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # Lambda — deploy and manage (both regions)
  statement {
    sid     = "LambdaWest"
    actions = ["lambda:*"]
    resources = [
      "arn:aws:lambda:us-west-2:${data.aws_caller_identity.current.account_id}:function:civic-watchdog-*",
    ]
  }

  statement {
    sid     = "LambdaEast"
    actions = ["lambda:*"]
    resources = [
      "arn:aws:lambda:us-east-1:${data.aws_caller_identity.current.account_id}:function:civic-data-*",
      "arn:aws:lambda:us-east-1:${data.aws_caller_identity.current.account_id}:layer:civic-data-*",
      "arn:aws:lambda:us-east-1:${data.aws_caller_identity.current.account_id}:layer:civic-data-*:*",
    ]
  }

  # IAM — manage project roles + self
  statement {
    sid = "IAM"
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
      "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/civic-*",
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
    sid     = "Scheduler"
    actions = ["scheduler:*"]
    resources = [
      "arn:aws:scheduler:us-west-2:${data.aws_caller_identity.current.account_id}:schedule/default/civic-watchdog-*",
    ]
  }

  # CloudWatch Logs
  statement {
    sid     = "Logs"
    actions = ["logs:*"]
    resources = [
      "arn:aws:logs:us-west-2:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/civic-watchdog-*",
      "arn:aws:logs:us-east-1:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/civic-data-*",
    ]
  }

  # API Gateway (us-east-1)
  statement {
    sid = "APIGateway"
    actions = [
      "apigateway:GET",
      "apigateway:POST",
      "apigateway:PUT",
      "apigateway:PATCH",
      "apigateway:DELETE",
    ]
    resources = ["arn:aws:apigateway:us-east-1::/apis/*"]
  }

  # CloudFront
  statement {
    sid = "CloudFront"
    actions = [
      "cloudfront:GetDistribution",
      "cloudfront:GetDistributionConfig",
      "cloudfront:UpdateDistribution",
      "cloudfront:ListDistributions",
      "cloudfront:GetOriginAccessControl",
      "cloudfront:CreateOriginAccessControl",
      "cloudfront:UpdateOriginAccessControl",
      "cloudfront:DeleteOriginAccessControl",
      "cloudfront:ListOriginAccessControls",
      "cloudfront:CreateInvalidation",
      "cloudfront:TagResource",
      "cloudfront:UntagResource",
      "cloudfront:ListTagsForResource",
    ]
    resources = ["*"]
  }

  # Terraform state
  statement {
    sid     = "TerraformState"
    actions = ["s3:*"]
    resources = [
      "arn:aws:s3:::stoside-terraform-state",
      "arn:aws:s3:::stoside-terraform-state/*",
    ]
  }
}

resource "aws_iam_role_policy" "watchdog_deployer_permissions" {
  name   = "civic-deploy"
  role   = aws_iam_role.watchdog_deployer.id
  policy = data.aws_iam_policy_document.deployer_permissions.json
}
