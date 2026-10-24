# Decisions

## Permission-aware fallbacks (from the probe, 2026-10-23, us-east-1)

Source: `docs/probe-results.md`. Rows follow the README "Permission-aware fallbacks" table. `RESULTS.md` records which path actually ran in the demo.

| Service | Probe | Chosen path | Notes |
|---|---|---|---|
| Claude on Bedrock | ALLOWED | If allowed: Sonnet 4.6 for packets, policy answers, digest; Haiku 4.5 fallback | Text and image OK on `us.` and `global.` profiles for Sonnet 4.6; Haiku 4.5 OK on `us.` |
| Bedrock Guardrails | ALLOWED | If allowed: one guardrail, `ApplyGuardrail` on all output | Create + apply OK (`GUARDRAIL_INTERVENED` on dosing text). Probe guardrail is DRAFT; Role 3 creates the real one with a published version |
| Knowledge Bases | ALLOWED (list only) | Start with full policy in context (P0); KB retrieval stays P2 | `ListKnowledgeBases` and `iam:ListRoles` OK; KB creation and service-role pass not verified |
| Textract | ALLOWED | If allowed: second independent read of each form | `DetectDocumentText` OK |
| Transcribe | ALLOWED (list only) | If allowed: read-back audio to transcript; typed read-back kept as fallback | Only `ListTranscriptionJobs` probed; `StartTranscriptionJob` not verified |
| DynamoDB | ALLOWED | If allowed: action-log mirror + sync status, SQLite stays the local source of truth | Create table + put item OK (`sparthill-probe`) |
| Lambda + EventBridge | UNKNOWN | In-app scheduler (P0/P1); cloud puller stays P2 | Not probed. IAM list is allowed, so Lambda may be possible; verify before starting the P2 item |
| S3 | ALLOWED | If allowed: snapshots, images, audio, audit trail with SSE-S3 | Create bucket + SSE-S3 put OK. SSE-KMS not probed |

Model routing note: Nova 2 Lite answered "Black." to the red test image, but every Claude model and Nova Lite 1.0 answered "Red". Nova 2 Lite stays the default form reader per the README, but the eval harness must compare Nova 2 Lite with Haiku 4.5 on form reading before the 1:00 AM checkpoint.

## Deferred (CI minimalism ruling)

- TODO: dependabot config
- TODO: README CI badge
- TODO: ruff lint job
