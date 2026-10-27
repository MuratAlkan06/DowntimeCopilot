data "aws_caller_identity" "current" {}

locals {
  # Built from the caller's account, never hard-coded (tools/guard.sh sweeps infra/ for 12-digit IDs).
  account_id      = data.aws_caller_identity.current.account_id
  deploy_user_arn = "arn:aws:iam::${local.account_id}:user/${var.deploy_user_name}"
}

# One suffix for the table and the bucket, like the names tools/sync_table.py and tools/audio_bucket.py made.
resource "random_id" "suffix" {
  byte_length = 4
}
