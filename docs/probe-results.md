# Bedrock / AWS permission probe results

- Date: 2026-10-23 17:56 PDT
- Re-run: 2026-10-23 19:16 PDT (step-zero reconciliation): model routing reproduced unchanged; guardrail re-probed, see the correction below
- Region: us-east-1
- Command: `make probe` (`tools/bedrock_probe.py --extras`, local-only, distributed by DM)
- Account: AWS Workshop Studio team account
- Redaction: 12-digit account IDs (including ARN account segments and the probe bucket name) replaced with `[REDACTED]`. Enforced by `tools/guard.sh`.

Resources the probe created and left in place for reuse: guardrail `sparthill-probe` (DRAFT), S3 bucket `sparthill-probe-[REDACTED]-us-east-1`, DynamoDB table `sparthill-probe`.

Correction (re-run 19:16 PDT): the current DRAFT guardrail id is `jeb5uct6gv35`. The id in the raw output below (`0wnxjjzefh0n`) is no longer listed. On the re-run, create returned `ConflictException`: the `sparthill-probe` name already exists. That is a name collision, not a permission deny. The raw output is left as recorded.

Not probed: Lambda, EventBridge, Transcribe `StartTranscriptionJob`, Knowledge Base creation.

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
