<!-- GENERATED - DO NOT EDIT. `make results` regenerates this file from the bundle named below. -->
# RESULTS

GENERATED - DO NOT EDIT. Every number below is computed from one recorded live run (the bundle named here); commentary comes verbatim from `evals/NOTES.md`.

- Run id: `20261024T000000Z-synthetic`
- Run started / finished (UTC, recorded at run time): 2026-10-24T00:00:00+00:00 / 2026-10-24T00:05:00+00:00
- Git SHA at run time: `0000000000000000000000000000000000000000`
- Artifact bundle: `tests/fixtures/eval_bundle_photographed/`
- Artifact digest (sha256 over the bundle, `evals.run.digest`): `d73818e27d798fff85618e39fd36f402cdd07eca9b2ebe8a3177297c22f93f78`
- Inputs fixed before the run (`evals/manifest.json`): 12 seeded patients, 2 DEMO.md policy questions, form fixtures: 1, photographed fixtures: 2 (forms built from public handwriting samples), speech fixtures: 1, simulated clock +15 min then +45 min

## Headline

| Metric | Value |
|---|---|
| Packet faithfulness, final lines with a resolving source_path | 92.9% (13/14) |
| Packet faithfulness, first-pass model lines | 92.9% (13/14) |
| Packet omissions (unverifiable lines withheld) | 1 |
| Policy grounding rate | 50.0% (1/2) |
| Policy expected-section correctness | 50.0% (1/2) |
| Form field accuracy, Nova 2 Lite vision (synthetic fixtures) | 90.0% (9/10) |
| Form field accuracy, Textract (synthetic fixtures) | 100.0% (10/10) |
| Form field accuracy, combined (agreement) (synthetic fixtures) | 90.0% (9/10) |
| False-flag rate on clean fields (synthetic fixtures) | 0.0% (0/8) |
| Form field accuracy, Nova 2 Lite vision (forms built from public handwriting samples) | 76.5% (13/17) |
| Form field accuracy, Textract (forms built from public handwriting samples) | 90.0% (9/10) |
| Form field accuracy, combined (agreement) (forms built from public handwriting samples) | 82.4% (14/17) |
| False-flag rate on clean fields (forms built from public handwriting samples) | 6.2% (1/16) |
| Speech transcription word accuracy (1 synthetic fixture) | 94.1% (16/17) |
| Model spend, this run (`/costs`, Bedrock only) | $0.0764 |
| Cost per simulated outage-hour | $0.1018 (0.75 simulated h) |

## Paths that ran

- `form_read`: us.amazon.nova-2-lite-v1:0 (route us.amazon.nova-2-lite-v1:0)
- `packet`: us.anthropic.claude-sonnet-4-6 (route us.anthropic.claude-sonnet-4-6)
- `policy`: us.anthropic.claude-sonnet-4-6 (route us.anthropic.claude-sonnet-4-6)
- Textract second read: on; guardrail: checker; transcription: transcribed; region us-east-1

## Services (from docs/decisions.md at run time)

| Service | Probe |
|---|---|
| Claude on Bedrock | ALLOWED |
| Bedrock Guardrails | ALLOWED |
| Knowledge Bases | ALLOWED (list only) |
| Textract | ALLOWED |
| Transcribe | ALLOWED |
| DynamoDB | ALLOWED |
| Lambda + EventBridge | AVAILABLE, DEFERRED |
| S3 | ALLOWED |

## 1. Packet faithfulness

Packets: 1. Final: 92.9% (13/14) of shown lines resolve to a snapshot path, 1 omitted. First pass: 92.9% (13/14). Regenerated once: 1; unparseable model output: 0.

## 2. Policy grounding

| # | Question | Expected | Cited | Grounded | Guardrail | Expected section |
|---|---|---|---|---|---|---|
| 1 | How do I get a stat lab with the order system down? | 4.2 | 4.2 | yes | passed | yes |
| 2 | How do I take a verbal order and do the read-back during downtime? | 6 | 6 | no | passed | no |

Grounding rate 50.0% (1/2); expected-section correctness 50.0% (1/2).

Notes (evals/NOTES.md, `grounding`):

Question 2 is ungrounded in this synthetic bundle on purpose.

## 3. Form extraction (synthetic fixtures)

| Fixture | Nova 2 Lite | Textract | Combined |
|---|---|---|---|
| vitals_flowsheet-01 | 90.0% (9/10) | 100.0% (10/10) | 90.0% (9/10) |
| **All (synthetic fixtures)** | 90.0% (9/10) | 100.0% (10/10) | 90.0% (9/10) |

Forms built from public handwriting samples: 2 fixtures, scored separately in section 8.

## 4. Flags on clean fields (synthetic fixtures)

Clean field: legible on paper (ground truth not blank) and inside the unit's normal range. False flag: a clean field flagged for review although the combined value was right. False-flag rate 0.0% (0/8). Flags that caught a wrong value: 1. Wrong values with no flag (silent errors): 0.

## 5. Speech transcription (1 synthetic fixture)

Word accuracy 94.1% (16/17) against the fixture transcript (lowercase, punctuation ignored; accuracy = 1 - (substitutions + deletions + insertions) / reference words). Verbal-order structuring from the live transcript: 85.7% (6/7) fields.

Notes (evals/NOTES.md, `speech`):

One word dropped on purpose.

## 6. Latency and cost per call type (`/costs`)

| Call type | Calls | Median latency (ms) | Tokens in / out | USD |
|---|---|---|---|---|
| form_read | 1 | 3000 | 2000 / 400 | 0.001600 |
| packet | 2 | 10000 | 8200 / 1850 | 0.052275 |
| policy | 1 | 6000 | 6000 / 300 | 0.022500 |
| **Total** | 4 | 7500 | 16200 / 2550 | 0.076375 |

Per-call-type sums equal the `/costs` totals (checked at run time and again here). Not metered by the app, so not in these sums: Textract AnalyzeDocument 1 page(s), Transcribe 1 job(s), ApplyGuardrail 0 check(s); see AWS billing. Budget guard: limit $2.00, max projected $0.8800.

## 7. Cost per simulated outage-hour

$0.1018 per simulated outage-hour: $0.076375 of model calls over 0.75 simulated hours (outage declared after +15 min, `/costs` read after +45 min).

## 8. Form extraction (forms built from public handwriting samples)

| Fixture | Nova 2 Lite | Textract | Combined |
|---|---|---|---|
| sd19-vitals_flowsheet-01 | 70.0% (7/10) | 90.0% (9/10) | 80.0% (8/10) |
| sd19-downtime_mar-01 | 85.7% (6/7) | n/a (reader did not run) | 85.7% (6/7) |
| **All (forms built from public handwriting samples)** | 76.5% (13/17) | 90.0% (9/10) | 82.4% (14/17) |

Each fixture is one of the three form templates filled in with one writer's characters from NIST Special Database 19 (public, labeled handwriting samples), then given a photo-style distortion; scored exactly like sections 3 and 4. False-flag rate 6.2% (1/16). Flags that caught a wrong value: 1. Wrong values with no flag (silent errors): 2.

## FAILURES

Every miss in this run.

Packet lines (first pass rejected, or omitted / unresolved in the final packet):

- pt-12 first [assessment] source_path `labs[0].glucose`: Glucose trending up
- pt-12 final (omitted) [assessment] source_path `faithfulness-check`: omitted: unverifiable line

Policy questions without a grounded answer from the expected section:

- "How do I take a verbal order and do the read-back during downtime?": expected 6, got 6 (grounded no, guardrail passed)

Form fields (synthetic fixtures; field, expected, got):

- vitals_flowsheet-01 / nova / `resp_rate`: expected "22", got "27"
- vitals_flowsheet-01 / combined / `resp_rate`: expected "22", got "27"

Form fields (forms built from public handwriting samples; field, expected, got):

- sd19-vitals_flowsheet-01 / nova / `bp_diastolic`: expected "74", got "71"
- sd19-vitals_flowsheet-01 / nova / `heart_rate`: expected "88", got "86"
- sd19-vitals_flowsheet-01 / nova / `initials`: expected "KM", got "KN"
- sd19-vitals_flowsheet-01 / textract / `bp_diastolic`: expected "74", got "71"
- sd19-vitals_flowsheet-01 / combined / `bp_diastolic`: expected "74", got "71"
- sd19-vitals_flowsheet-01 / combined / `initials`: expected "KM", got "KN"
- sd19-downtime_mar-01 / nova / `time_given`: expected "01:15", got "01:16"
- sd19-downtime_mar-01 / combined / `time_given`: expected "01:15", got "01:16"

Speech words (operation, expected, got):

- delete: expected "confirmed", got null

Verbal-order structured fields (field, expected, got):

- `route`: expected "SC", got "subcutaneous"
