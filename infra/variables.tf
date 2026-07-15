variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-west-2"
}

variable "project" {
  description = "Project tag for all resources"
  type        = string
  default     = "civic-watchdog"
}

variable "bucket_name" {
  description = "S3 bucket for pipeline data"
  type        = string
  default     = "yimby-watchdog-data"
}

variable "schedule_nightly" {
  description = "Cron expression for nightly pipeline run (UTC)"
  type        = string
  default     = "cron(0 8 * * ? *)" # 1 AM Pacific (UTC-7)
}

variable "schedule_afternoon" {
  description = "Cron expression for afternoon pipeline run (UTC)"
  type        = string
  default     = "cron(0 1 ? * TUE-FRI *)" # 6 PM Pacific on weekdays
}

variable "lambda_memory" {
  description = "Lambda memory in MB"
  type        = number
  default     = 512
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds"
  type        = number
  default     = 900
}

variable "ssm_prefix" {
  description = "SSM Parameter Store prefix for jurisdiction config"
  type        = string
  default     = "/civic-monitor/"
}
