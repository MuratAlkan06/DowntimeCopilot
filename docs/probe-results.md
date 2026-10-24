# Bedrock / AWS permission probe results

- Date: 2026-10-23 17:56 PDT
- Re-run: 2026-10-23 19:16 PDT (step-zero reconciliation): model routing reproduced unchanged; guardrail re-probed, see the correction below
- Region: us-east-1
- Command: `make probe` (`tools/bedrock_probe.py --extras`, local-only, distributed by DM)
- Account: AWS Workshop Studio team account
- Redaction: 12-digit account IDs (including ARN account segments and the probe bucket name) replaced with `[REDACTED]`. Enforced by `tools/guard.sh`.

Resources the probe created and left in place for reuse: guardrail `sparthill-probe` (DRAFT), S3 bucket `sparthill-probe-[REDACTED]-us-east-1`, DynamoDB table `sparthill-probe`.

Correction (re-run 19:16 PDT): the current DRAFT guardrail id is `jeb5uct6gv35`. The id in the raw output below (`0wnxjjzefh0n`) is no longer listed. On the re-run, create returned `ConflictException`: the `sparthill-probe` name already exists. That is a name collision, not a permission deny. The raw output is left as recorded.

Not probed: Lambda, EventBridge, Transcribe `StartTranscriptionJob` (since verified live in S9, see below), Knowledge Base creation.

Probe delta (S8, 2026-10-24 ~01:00 PDT, us-east-1): before S8, `DetectDocumentText` was the only Textract call probed (`TEXTRACT  OK` below). `AnalyzeDocument` with `FeatureTypes` `FORMS` + `QUERIES` was first called in the S8 live pass and was **GRANTED**: 3 of 3 calls succeeded, with no AccessDenied. This was not a `make probe` re-run; the evidence is in `docs/decisions.md` (S8).

Probe delta (S9, 2026-10-24 ~01:30 PDT, us-east-1): before S9, Transcribe was probed list-only (`TRANSCRIBE OK (list allowed)` below). `StartTranscriptionJob` (en-US, wav from an SSE-S3 bucket owned by this account), `GetTranscriptionJob` and `DeleteTranscriptionJob` were first called in the S9 live pass and were **ALLOWED**: 2 of 2 jobs COMPLETED, with no AccessDenied. S3 `CreateBucket`, `PutBucketEncryption`, `PutPublicAccessBlock`, `PutBucketLifecycleConfiguration`, `PutObject` and `DeleteObject` on the audio bucket were also allowed. This was not a `make probe` re-run; the evidence is in `docs/decisions.md` (S9).

Probe delta (S12, 2026-10-24T10:42Z, us-east-1): Knowledge Base creation is still not probed. The S12 ladder began with `aws iam simulate-principal-policy` on the caller's role for `bedrock:CreateKnowledgeBase`, `aoss:CreateCollection`, `iam:CreateRole` and `iam:PassRole`. The simulator itself was **DENIED**: `An error occurred (AccessDenied) when calling the SimulatePrincipalPolicy operation: User: arn:aws:sts::[REDACTED]:assumed-role/WSParticipantRole/Participant is not authorized to perform: iam:SimulatePrincipalPolicy on resource: arn:aws:iam::[REDACTED]:role/WSParticipantRole because no identity-based policy allows the iam:SimulatePrincipalPolicy action`. The ladder stops at its first denial, so `aoss:ListCollections`, `iam:GetRole` and `CreateKnowledgeBase` were never called, and no resource was created. The four KB actions are unverified, not denied. This was not a `make probe` re-run; the evidence is in `docs/decisions.md` (S12).

## Raw output (redacted)

```text
region: us-east-1
identity OK: arn:aws:sts::[REDACTED]:assumed-role/WSParticipantRole/Participant

TEXT  OK    Nova 2 Lite (us.)            us.amazon.nova-2-lite-v1:0  -> 'OK.'
TEXT  OK    Nova 2 Lite (global.)        global.amazon.nova-2-lite-v1:0  -> 'OK.'
TEXT  OK    Claude Sonnet 4.6 (us.)      us.anthropic.claude-sonnet-4-6  -> 'OK'
TEXT  OK    Claude Sonnet 4.6 (global.)  global.anthropic.claude-sonnet-4-6  -> 'OK'
TEXT  OK    Claude Haiku 4.5 (us.)       us.anthropic.claude-haiku-4-5-20251001-v1:0  -> 'OK'
TEXT  OK    Nova Lite 1.0 (us.)          us.amazon.nova-lite-v1:0  -> 'OK.'
TEXT  OK    Nova Pro 1.0 (us.)           us.amazon.nova-pro-v1:0  -> 'OK.'

IMAGE OK    Nova 2 Lite (us.)            -> 'Black.'
IMAGE OK    Nova 2 Lite (global.)        -> 'Black.'
IMAGE OK    Claude Sonnet 4.6 (us.)      -> '**Red**'
IMAGE OK    Claude Sonnet 4.6 (global.)  -> '**Red**'
IMAGE OK    Claude Haiku 4.5 (us.)       -> 'Red'
IMAGE OK    Nova Lite 1.0 (us.)          -> 'Red'
IMAGE OK    Nova Pro 1.0 (us.)           -> 'The image consists of a'

GUARDRAIL create OK   id=0wnxjjzefh0n version=DRAFT
GUARDRAIL apply OK    action=GUARDRAIL_INTERVENED

--- extras: other services on the diagram ---
S3        OK    bucket=sparthill-probe-[REDACTED]-us-east-1 (SSE-S3 put worked)
DYNAMODB  OK    table=sparthill-probe
TEXTRACT  OK
TRANSCRIBE OK   (list allowed)
KNOWLEDGE BASES OK (list allowed; creation still needs a service role)
IAM       OK    (unusual for a workshop account)

Done. Anything marked OK is usable as-is; DENY means plan around it.
```
