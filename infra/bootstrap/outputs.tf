output "state_bucket" {
  description = "Bucket for the main stack's backend: copy into infra/backend.hcl (from backend.hcl.example)."
  value       = aws_s3_bucket.state.bucket
}

output "admin_role_arn" {
  description = "role_arn for the downtime-admin profile in ~/.aws/config (source_profile = the deploy user)."
  value       = aws_iam_role.admin.arn
}
