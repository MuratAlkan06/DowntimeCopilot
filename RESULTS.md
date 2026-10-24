<!-- GENERATED - DO NOT EDIT. `make results` regenerates this file from the bundle named below. -->
# RESULTS

GENERATED - DO NOT EDIT. Every number below is computed from one recorded live run (the bundle named here); commentary comes verbatim from `evals/NOTES.md`.

- Run id: `20261024T102817Z-a020e81`
- Run started / finished (UTC, recorded at run time): 2026-10-24T10:28:17+00:00 / 2026-10-24T10:29:41+00:00
- Git SHA at run time: `a020e81ac6bca004d6e11c6a6f54bcc2e4d6eff2`
- Artifact bundle: `evals/baseline/20261024T102817Z-a020e81/`
- Artifact digest (sha256 over the bundle, `evals.run.digest`): `30383010dbbaae28457ae052f480e5852a325456850953b354915abf4673a41a`
- Inputs fixed before the run (`evals/manifest.json`): 12 seeded patients, 5 DEMO.md policy questions, form fixtures: 3, speech fixtures: 1, simulated clock +15 min then +45 min

## Headline

| Metric | Value |
|---|---|
| Packet faithfulness, final lines with a resolving source_path | 100.0% (191/191) |
| Packet faithfulness, first-pass model lines | 100.0% (191/191) |
| Packet omissions (unverifiable lines withheld) | 0 |
| Policy grounding rate | 100.0% (5/5) |
| Policy expected-section correctness | 100.0% (5/5) |
| Form field accuracy, Nova 2 Lite vision (synthetic fixtures) | 100.0% (22/22) |
| Form field accuracy, Textract (synthetic fixtures) | 90.9% (20/22) |
| Form field accuracy, combined (agreement) (synthetic fixtures) | 95.5% (21/22) |
| False-flag rate on clean fields (synthetic fixtures) | 0.0% (0/19) |
| Speech transcription word accuracy (1 synthetic fixture) | 100.0% (17/17) |
| Model spend, this run (`/costs`, Bedrock only) | $0.2480 |
| Cost per simulated outage-hour | $0.3307 (0.75 simulated h) |

Photographed forms: 0 fixtures — pending

## Paths that ran

- `checker`: us.amazon.nova-2-lite-v1:0 (route us.amazon.nova-2-lite-v1:0)
- `form_read`: us.amazon.nova-2-lite-v1:0 (route us.amazon.nova-2-lite-v1:0)
- `packet`: us.anthropic.claude-sonnet-4-6 (route us.anthropic.claude-sonnet-4-6)
- `policy`: us.anthropic.claude-sonnet-4-6 (route us.anthropic.claude-sonnet-4-6)
- `structure`: us.amazon.nova-2-lite-v1:0 (route us.amazon.nova-2-lite-v1:0)
- Textract second read: on; guardrail: bedrock; transcription: transcribed; region us-east-1

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

Packets: 12. Final: 100.0% (191/191) of shown lines resolve to a snapshot path, 0 omitted. First pass: 100.0% (191/191). Regenerated once: 0; unparseable model output: 0.

Notes (evals/NOTES.md, `faithfulness`):

One run of 12 packets. Every first-pass line resolved, so the regenerate-once path and the omission path did not
run in this bundle; their behaviour is covered by unit tests only, not by this measurement.

## 2. Policy grounding

| # | Question | Expected | Cited | Grounded | Guardrail | Expected section |
|---|---|---|---|---|---|---|
| 1 | How do I get a stat lab with the order system down? | 4.2 | 4.2.1, 4.2.2, 4.2.3, 4.2.4 | yes | passed | yes |
| 2 | How do I take a verbal order and do the read-back during downtime? | 6 | 6.1, 6.2, 6.3 | yes | passed | yes |
| 3 | How do I give medications without barcode scanning? | 5 | 5.1, 5.2, 5.3, 5.5 | yes | passed | yes |
| 4 | Who declares downtime, and how is it announced? | 1 | 1.1, 1.2 | yes | passed | yes |
| 5 | What does each role backload when the EHR comes back? | 7 | 7.2, 7.3, 7.4, 7.5 | yes | passed | yes |

Grounding rate 100.0% (5/5); expected-section correctness 100.0% (5/5).

Notes (evals/NOTES.md, `grounding`):

Five questions, all from the DEMO.md table. Citations are paragraph-level (for example 4.2.1 under 4.2), and the
correctness check accepts a citation at or under the expected section. The safe-refusal beat (dosing question) is
not part of the manifest, so the guardrail's intervention path is not measured here.

## 3. Form extraction (synthetic fixtures)

| Fixture | Nova 2 Lite | Textract | Combined |
|---|---|---|---|
| vitals_flowsheet-01 | 100.0% (10/10) | 90.0% (9/10) | 90.0% (9/10) |
| downtime_mar-01 | 100.0% (7/7) | 85.7% (6/7) | 100.0% (7/7) |
| admission_note-01 | 100.0% (5/5) | 100.0% (5/5) | 100.0% (5/5) |
| **All (synthetic fixtures)** | 100.0% (22/22) | 90.9% (20/22) | 95.5% (21/22) |

Photographed forms: 0 fixtures — pending

Notes (evals/NOTES.md, `extraction`):

These are synthetic printed fixtures rendered by `tools/form_fixtures.py`, not handwriting, so they overstate
real-world accuracy. All Textract misses are blank boxes where Textract returned text: `pain_score` on the vitals
flowsheet and `second_verifier` on the MAR. Nova 2 Lite left both blank. The combined vitals miss is the
`pain_score` disagreement: the merged record keeps Textract's value with confidence 0 and flags it for review, so
it shows up under "flags that caught a wrong value", not as a silent error. Scoring is exact match after lowercasing
and collapsing whitespace, with no synonym mapping. Photographed handwritten forms are still pending.

## 4. Flags on clean fields (synthetic fixtures)

Clean field: legible on paper (ground truth not blank) and inside the unit's normal range. False flag: a clean field flagged for review although the combined value was right. False-flag rate 0.0% (0/19). Flags that caught a wrong value: 1. Wrong values with no flag (silent errors): 0.

Notes (evals/NOTES.md, `false_flags`):

Clean fields leave out blanks and out-of-range vitals. The SpO2 89 out-of-range flag is expected, so it is not
counted as a false flag.

## 5. Speech transcription (1 synthetic fixture)

Word accuracy 100.0% (17/17) against the fixture transcript (lowercase, punctuation ignored; accuracy = 1 - (substitutions + deletions + insertions) / reference words). Verbal-order structuring from the live transcript: 42.9% (3/7) fields.

Notes (evals/NOTES.md, `speech`):

One synthetic text-to-speech clip (`verbal_order-01.wav`, voice Samantha, 165 wpm), so this is not a measure of
accuracy on real speech. Every structuring miss is a normalization gap and not a hearing error: "Doctor Quill"
for "Dr. Quill", "confirmed" for "yes", "subcutaneous" for "SC". `provider_cosign` was left unread because the
read-back never mentions a co-sign. S9 saw the same behaviour live.

## 6. Latency and cost per call type (`/costs`)

| Call type | Calls | Median latency (ms) | Tokens in / out | USD |
|---|---|---|---|---|
| checker | 12 | 908.5 | 10028 / 276 | 0.003699 |
| form_read | 3 | 1981 | 1737 / 1487 | 0.004239 |
| packet | 12 | 5609.5 | 11492 / 9564 | 0.177936 |
| policy | 5 | 2874 | 10835 / 1943 | 0.061650 |
| structure | 1 | 1128 | 373 / 164 | 0.000522 |
| **Total** | 33 | 2674 | 34465 / 13434 | 0.248046 |

Per-call-type sums equal the `/costs` totals (checked at run time and again here). Not metered by the app, so not in these sums: Textract AnalyzeDocument 3 page(s), Transcribe 1 job(s), ApplyGuardrail 5 check(s); see AWS billing. Budget guard: limit $2.00, max projected $0.8800.

Notes (evals/NOTES.md, `latency_cost`):

`checker` is the packet guardrail. Packets send grounding without a query, so `BedrockGuardrail` routes them to
the Nova 2 Lite checker, which is metered here. ApplyGuardrail runs only for policy answers. Harness bug, fixed
after this run: `run.json` `unmetered.guardrail_apply` says 17, but the real number of ApplyGuardrail calls was
5 (one per policy answer). The report now derives the count from the bundle, and the bundle was left as recorded.
The same bug inflated the budget guard's running allowance (`spent_with_allowances_usd`) by 12 × $0.01, which only
made the guard stricter. Latency comes from `/costs` and covers the model call only (no HTTP, no Textract or
Transcribe time).

## 7. Cost per simulated outage-hour

$0.3307 per simulated outage-hour: $0.248046 of model calls over 0.75 simulated hours (outage declared after +15 min, `/costs` read after +45 min).

Notes (evals/NOTES.md, `outage_cost`):

Simulated hours run from the outage declaration (clock 03:00) to the `/costs` read (clock 03:45). Only Bedrock
model calls are counted. Textract, Transcribe and ApplyGuardrail are billed by AWS outside the app; see the
decisions entry for this run.

## FAILURES

Every miss in this run.

Packet lines (first pass rejected, or omitted / unresolved in the final packet):

- none

Policy questions without a grounded answer from the expected section:

- none

Form fields (synthetic fixtures; field, expected, got):

- vitals_flowsheet-01 / textract / `pain_score`: expected null, got "NW"
- vitals_flowsheet-01 / combined / `pain_score`: expected null, got "NW"
- downtime_mar-01 / textract / `second_verifier`: expected null, got "HIGH-ALERT MEDS"

Speech words (operation, expected, got):

- none

Verbal-order structured fields (field, expected, got):

- `ordering_provider`: expected "Dr. Quill", got "Doctor Quill"
- `provider_cosign`: expected "no", got null
- `read_back`: expected "yes", got "confirmed"
- `route`: expected "SC", got "subcutaneous"
