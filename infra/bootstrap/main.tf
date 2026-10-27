data "aws_caller_identity" "current" {}

locals {
  # Built from the caller's account, never hard-coded (tools/guard.sh sweeps infra/ for 12-digit IDs).
  deploy_user_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:user/${var.deploy_user_name}"
}

resource "random_id" "suffix" {
  byte_length = 4
}

# ---------------------------------------------------------------- remote state bucket (main stack backend)

resource "aws_s3_bucket" "state" {
  bucket = "downtime-copilot-tfstate-${random_id.suffix.hex}"
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "abort-incomplete-multipart-after-7-days"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "state_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # State writes only from the admin role (plus a root carve-out against lockout). Blocks plan poisoning by
  # any other principal in the account (security review L3).
  statement {
    sid       = "DenyStateWritesExceptAdmin"
    effect    = "Deny"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.state.arn}/*"]

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    condition {
      test     = "StringNotEquals"
      variable = "aws:PrincipalArn"
      values = [
        aws_iam_role.admin.arn,
        "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root",
      ]
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = data.aws_iam_policy_document.state_bucket.json
}

# ---------------------------------------------------------------- admin role (profile downtime-admin)

data "aws_iam_policy_document" "trust_deploy_user" {
  statement {
    sid     = "DeployUserAssumes"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [local.deploy_user_arn]
    }

    dynamic "condition" {
      for_each = var.require_admin_mfa ? [1] : []
      content {
        test     = "Bool"
        variable = "aws:MultiFactorAuthPresent"
        values   = ["true"]
      }
    }
  }
}

resource "aws_iam_role" "admin" {
  name                 = "downtime-copilot-admin"
  max_session_duration = 3600
  description          = "Terraform operator role for the downtime-copilot main stack (infra/). Assumed by the deploy user."
  assume_role_policy   = data.aws_iam_policy_document.trust_deploy_user.json
}

resource "aws_iam_role_policy_attachment" "admin" {
  role       = aws_iam_role.admin.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}
