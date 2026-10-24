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

## Step-zero reconciliation (2026-10-23, slice S1)

- **Guardrail re-probe.** `ConflictException` on create means the `sparthill-probe` name already exists. It is a name collision, not a deny. The current DRAFT guardrail id is `jeb5uct6gv35`; the previously recorded `0wnxjjzefh0n` is no longer listed (`docs/probe-results.md` corrected). `GUARDRAIL_ID` is unset, so `config.guardrail_mode()` uses the checker fallback (Nova 2 Lite + `prompts/checker.md`) until Role 3 publishes a guardrail version and sets `GUARDRAIL_ID`.
- **Model routing reproduced 19:16 PDT.** Same profiles as the README "Model routing" table.
- **Scheduler on the simulated clock (this slice).** The in-app puller ticks every `SNAPSHOT_INTERVAL_SECONDS` real seconds (default 5; 0 disables). Each tick reads the simulator's `GET /clock` and pulls only when the phase is `before`, the EHR is up, and at least 15 simulated minutes have passed since the newest pre-outage snapshot (`app.api.snapshot.should_pull`). It never pulls in `downtime` or `recovery`, so the pre-outage baseline that `/recovery/restore` and `/digest` read stays intact. Manual `POST /snapshot/pull` is unchanged. `python -m simulator.seed --fresh` empties the app store for a clean demo.
- **Packets.** `prompts/packet.md` is now v2. `source_path` may list several comma-separated snapshot paths, and every one must resolve. Rules-engine alerts are never a source. A rejected line is logged and the packet is regenerated once. Lines that still fail are dropped and shown as an `omitted: unverifiable line` item (`source_path: faithfulness-check`). `generated_at` uses the simulated clock. `/downtime/start` runs up to `PACKET_CONCURRENCY` (default 4) generations at once.
- **S4 / S11 physical-photo deferral (owner decision).** The photographed-paper-form steps are deferred to their slices.
- **Remaining wall-clock stamps deferred.** Packets and the web UI (packet header, print footer, snapshot-age banner, outage start) now use the simulated clock. The digest `generated_at`, store timestamps (`pulls.pulled_at`, audit `ts`, cost `ts`) and recovery stamps still use the wall clock. Each is fixed in its own slice.

## Deferred (CI minimalism ruling)

- TODO: dependabot config
- TODO: README CI badge
- TODO: ruff lint job
- TODO: one clock: sqlite store stamps real wall-clock while the simulator runs a sim clock (UTC); the UI shows sim/unit time only (packets + UI done in S1; digest/store/recovery remain, see above)

## S2 acceptance time (2026-10-23, orchestrator per working-rules autonomy)
The build contract's S2 acceptance specified rule fire-counts "at 03:30 simulated time" with R-DUE-01 = {pt-08, pt-11, pt-12}. At 03:30 the seeded doses have rolled past: pt-08 hydromorphone (03:15) and pt-11 insulin (03:00) are overdue-and-rolled, the simulator advances pt-12's heparin to its next dose, and R-DUE-01 would be {pt-09, pt-12}. The contract's expected sets hold at 02:45-03:00, and DEMO.md advances the clock to exactly 03:00 before packet generation. Decision: acceptance and tests are pinned at 03:00; seed data and the demo script are unchanged. The 03:30 figure in the contract is treated as an internal inconsistency, not a requirement.
