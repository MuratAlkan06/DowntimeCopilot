# Read by tools/env_sync.py (`make infra-env`) into the local .env. Keep the names in step with it.
output "sync_table" {
  description = "SYNC_TABLE"
  value       = aws_dynamodb_table.sync.name
}

output "audio_bucket" {
  description = "AUDIO_BUCKET"
  value       = aws_s3_bucket.audio.bucket
}

output "guardrail_id" {
  description = "GUARDRAIL_ID"
  value       = aws_bedrock_guardrail.main.guardrail_id
}

output "guardrail_version" {
  description = "GUARDRAIL_VERSION (the published version, never DRAFT)"
  value       = aws_bedrock_guardrail_version.main.version
}

output "runtime_role_arn" {
  description = "role_arn for the downtime-runtime profile in ~/.aws/config (source_profile = the deploy user)."
  value       = aws_iam_role.runtime.arn
}

output "region" {
  description = "AWS_DEFAULT_REGION"
  value       = var.region
}
