# The one app bucket (AUDIO_BUCKET, docs/decisions.md S9 + S10): audio/, snapshots/, exports/. Hardened the way
# app/sync/s3.py harden_bucket does it. Unversioned on purpose (the app expects it). The lifecycle rules match
# app/sync/s3.py LIFECYCLE_RULES field for field; ensure_lifecycle checks them by ID, so with all three present
# it never calls PutBucketLifecycleConfiguration (which the runtime role is not allowed).
locals {
  lifecycle_rules = {
    "expire-after-1-day"            = { prefix = "audio/", days = 1 }
    "expire-snapshots-after-7-days" = { prefix = "snapshots/", days = 7 }
    "expire-exports-after-7-days"   = { prefix = "exports/", days = 7 }
  }
}

resource "aws_s3_bucket" "audio" {
  bucket = "downtime-copilot-audio-${random_id.suffix.hex}"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audio" {
  bucket = aws_s3_bucket.audio.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "audio" {
  bucket = aws_s3_bucket.audio.id

  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "audio" {
  bucket = aws_s3_bucket.audio.id

  dynamic "rule" {
    for_each = local.lifecycle_rules

    content {
      id     = rule.key
      status = "Enabled"

      filter {
        prefix = rule.value.prefix
      }

      expiration {
        days = rule.value.days
      }

      abort_incomplete_multipart_upload {
        days_after_initiation = rule.value.days
      }
    }
  }
}
