# Runtime role (profile downtime-runtime): what the running app may call, derived from its call inventory
# (app/ai/client.py, app/ai/guardrail.py, app/capture/textract.py, app/capture/transcribe.py, app/sync/s3.py,
# app/sync/dynamodb.py). Deliberately no Create*, no Put*Configuration, no iam:*: the app's create-if-missing
# paths get AccessDenied and latch their local fallbacks, which is the designed behavior when infra is missing.
locals {
  # Bare model IDs behind the `us.` inference profiles the app routes to (README "Model routing").
  routed_models = [
    "amazon.nova-2-lite-v1:0",
    "anthropic.claude-sonnet-4-6",
    "anthropic.claude-haiku-4-5-20251001-v1:0",
  ]
  bucket_arn = aws_s3_bucket.audio.arn
}

resource "aws_iam_role" "runtime" {
  name               = "downtime-copilot-runtime"
  description        = "Least-privilege runtime role for the downtime-copilot app. Assumed by the deploy user."
  assume_role_policy = data.aws_iam_policy_document.trust_deploy_user.json
}

data "aws_iam_policy_document" "trust_deploy_user" {
  statement {
    sid     = "DeployUserAssumes"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [local.deploy_user_arn]
    }
  }
}

data "aws_iam_policy_document" "runtime" {
  # S1a. The three `us.` inference profiles the router uses (app/ai/client.py ROUTES).
  statement {
    sid       = "InvokeRoutedProfiles"
    actions   = ["bedrock:InvokeModel"]
    resources = [for m in local.routed_models : "arn:aws:bedrock:${var.region}:${local.account_id}:inference-profile/us.${m}"]
  }

  # S1b. A `us.` profile fans out to the model in US Regions. The condition pins model invocation to those
  # profiles, so the role cannot call the models directly or outside the profile's routing (security review L1).
  statement {
    sid       = "InvokeRoutedModelsViaProfile"
    actions   = ["bedrock:InvokeModel"]
    resources = [for m in local.routed_models : "arn:aws:bedrock:us-*::foundation-model/${m}"]

    condition {
      test     = "StringEquals"
      variable = "bedrock:InferenceProfileArn"
      values   = [for m in local.routed_models : "arn:aws:bedrock:${var.region}:${local.account_id}:inference-profile/us.${m}"]
    }
  }

  # S2. Policy answers (app/ai/guardrail.py).
  statement {
    sid     = "ApplyPublishedGuardrail"
    actions = ["bedrock:ApplyGuardrail"]
    # The standard-tier topic evaluates via the us.guardrail.v1:0 profile, so ApplyGuardrail also needs
    # the profile ARNs in its destination regions (Bedrock user guide, guardrail-profiles-permissions).
    resources = concat(
      [aws_bedrock_guardrail.main.guardrail_arn],
      [for r in ["us-east-1", "us-east-2", "us-west-2"] : "arn:aws:bedrock:${r}:${local.account_id}:guardrail-profile/us.guardrail.v1:0"],
    )
  }

  # S3. AnalyzeDocument has no resource-level permissions (AWS service authorization reference).
  statement {
    sid       = "TextractAnalyzeDocument"
    actions   = ["textract:AnalyzeDocument"]
    resources = ["*"]
  }

  # S4a. StartTranscriptionJob has no resource type in the AWS service authorization reference, so it cannot be
  # scoped to a job ARN; "*" is the only resource it accepts.
  statement {
    sid       = "TranscribeStartJob"
    actions   = ["transcribe:StartTranscriptionJob"]
    resources = ["*"]
  }

  # S4b. Get and Delete do take the transcriptionjob resource; the app names every job dtc-<uuid4>.
  statement {
    sid       = "TranscribeOwnJobs"
    actions   = ["transcribe:GetTranscriptionJob", "transcribe:DeleteTranscriptionJob"]
    resources = ["arn:aws:transcribe:${var.region}:${local.account_id}:transcription-job/dtc-*"]
  }

  # S5. HeadBucket (s3:ListBucket) and the lifecycle read in ensure_bucket / ensure_lifecycle.
  statement {
    sid       = "AppBucketHeadAndLifecycleRead"
    actions   = ["s3:ListBucket", "s3:GetLifecycleConfiguration"]
    resources = [local.bucket_arn]
  }

  # S6. Verbal-order audio: put, Transcribe reads it as the caller, cleanup deletes it.
  statement {
    sid       = "AudioObjects"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
    resources = ["${local.bucket_arn}/audio/*"]
  }

  # S7. Snapshot mirror and audit exports (HeadObject is authorized as s3:GetObject).
  statement {
    sid       = "MirrorObjects"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${local.bucket_arn}/snapshots/*", "${local.bucket_arn}/exports/*"]
  }

  # S8. Sync mirror: DescribeTable (ACTIVE check) and PutItem only.
  statement {
    sid       = "SyncTableDescribeAndPut"
    actions   = ["dynamodb:DescribeTable", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.sync.arn]
  }
}

resource "aws_iam_role_policy" "runtime" {
  name   = "downtime-copilot-runtime"
  role   = aws_iam_role.runtime.id
  policy = data.aws_iam_policy_document.runtime.json
}
