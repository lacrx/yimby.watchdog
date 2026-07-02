resource "aws_s3_bucket" "watchdog_data" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_versioning" "watchdog_data" {
  bucket = aws_s3_bucket.watchdog_data.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "watchdog_data" {
  bucket = aws_s3_bucket.watchdog_data.id

  rule {
    id     = "raw-to-ia"
    status = "Enabled"
    filter {
      prefix = "raw/"
    }
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
  }

  rule {
    id     = "archive-to-glacier"
    status = "Enabled"
    filter {
      prefix = "archive/"
    }
    transition {
      days          = 90
      storage_class = "GLACIER"
    }
  }

  rule {
    id     = "cleanup-noncurrent"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

resource "aws_s3_bucket_public_access_block" "watchdog_data" {
  bucket = aws_s3_bucket.watchdog_data.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
