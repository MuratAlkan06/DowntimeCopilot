<!-- GENERATED - DO NOT EDIT. `make results` regenerates this file from the bundle named below. -->
# RESULTS

GENERATED - DO NOT EDIT. Every number below is computed from one recorded live run (the bundle named here); commentary comes verbatim from `evals/NOTES.md`.

- Run id: `20261028T060034Z-609f90e`
- Run started / finished (UTC, recorded at run time): 2026-10-28T06:00:34+00:00 / 2026-10-28T06:05:49+00:00
- Git SHA at run time: `609f90ec7524722d1560a41e9fcdde71f4d02924`
- Artifact bundle: `evals/baseline/20261028T060034Z-609f90e/`
- Artifact digest (sha256 over the bundle, `evals.run.digest`): `137d625b84c377968f726d05183782112c845922ad8a9c97412113de6a2e6150`
- Inputs fixed before the run (`evals/manifest.json`): 12 seeded patients, 5 DEMO.md policy questions, form fixtures: 3, speech fixtures: 1, simulated clock +15 min then +45 min

## Headline

| Metric | Value |
|---|---|
| Packet faithfulness, final lines with a resolving source_path | 100.0% (192/192) |
| Packet faithfulness, first-pass model lines | 100.0% (192/192) |
| Packet omissions (unverifiable lines withheld) | 0 |
| Policy grounding rate | 100.0% (5/5) |
| Policy expected-section correctness | 100.0% (5/5) |
| Form field accuracy, Nova 2 Lite vision (synthetic fixtures) | 100.0% (22/22) |
| Form field accuracy, Textract (synthetic fixtures) | 90.9% (20/22) |
| Form field accuracy, combined (agreement) (synthetic fixtures) | 95.5% (21/22) |
| False-flag rate on clean fields (synthetic fixtures) | 0.0% (0/19) |
| Form field accuracy, Nova 2 Lite vision (forms built from public handwriting samples) | 89.8% (237/264) |
| Form field accuracy, Textract (forms built from public handwriting samples) | 88.6% (234/264) |
| Form field accuracy, combined (agreement) (forms built from public handwriting samples) | 92.4% (244/264) |
| False-flag rate on clean fields (forms built from public handwriting samples) | 10.7% (26/243) |
| Speech transcription word accuracy (1 synthetic fixture) | 100.0% (17/17) |
| Model spend, this run (`/costs`, Bedrock only) | $0.3278 |
| Cost per simulated outage-hour | $0.4370 (0.75 simulated h) |

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
| Knowledge Bases | ALLOWED (list only); creation UNVERIFIED |
| Textract | ALLOWED |
| Transcribe | ALLOWED |
| DynamoDB | ALLOWED |
| Lambda + EventBridge | AVAILABLE, DEFERRED |
| S3 | ALLOWED |

## 1. Packet faithfulness

Packets: 12. Final: 100.0% (192/192) of shown lines resolve to a snapshot path, 0 omitted. First pass: 100.0% (192/192). Regenerated once: 0; unparseable model output: 0.

Notes (evals/NOTES.md, `faithfulness`):

One run of 12 packets. Every first-pass line resolved, so the regenerate-once path and the omission path did not
run in this bundle; their behaviour is covered by unit tests only, not by this measurement. The line count differs
from the previous baseline (`20261024T120050Z-90d316a`, kept under `evals/baseline/` as history) on identical
inputs: per-packet totals move by a line or two between runs, which is run-to-run model variance, not a seed or
harness change.

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

Forms built from public handwriting samples: 36 fixtures, scored separately in section 8.

Notes (evals/NOTES.md, `extraction`):

These are synthetic printed fixtures rendered by `tools/form_fixtures.py`, not handwriting, so they overstate
real-world accuracy. All Textract misses are blank boxes where Textract returned text: `pain_score` on the vitals
flowsheet and `second_verifier` on the MAR. Nova 2 Lite left both blank. The combined vitals miss is the
`pain_score` disagreement: the merged record keeps Textract's value with confidence 0 and flags it for review, so
it shows up under "flags that caught a wrong value", not as a silent error. Scoring is exact match after lowercasing
and collapsing whitespace, with no synonym mapping. Values in real people's handprint are measured in section 8
(forms built from public handwriting samples); see its notes for what that set is and is not.

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
read-back never mentions a co-sign. S9 saw the same behaviour live, and this run repeats the previous baseline's
four misses field for field. Verbal-order structuring is still the weakest measured step.

## 6. Latency and cost per call type (`/costs`)

| Call type | Calls | Median latency (ms) | Tokens in / out | USD |
|---|---|---|---|---|
| checker | 12 | 593.5 | 10071 / 292 | 0.004127 |
| form_read | 39 | 2707 | 22581 / 19072 | 0.059902 |
| packet | 12 | 5239.5 | 11499 / 9556 | 0.195619 |
| policy | 5 | 2852 | 10835 / 1926 | 0.067534 |
| structure | 1 | 1106 | 373 / 164 | 0.000574 |
| **Total** | 69 | 2707 | 55359 / 31010 | 0.327756 |

Per-call-type sums equal the `/costs` totals (checked at run time and again here). Not metered by the app, so not in these sums: Textract AnalyzeDocument 39 page(s), Transcribe 1 job(s), ApplyGuardrail 5 check(s); see AWS billing. Budget guard: limit $4.00, max projected $3.5800.

Notes (evals/NOTES.md, `latency_cost`):

`checker` is the packet guardrail. Packets send grounding without a query, so `BedrockGuardrail` routes them to
the Nova 2 Lite checker, which is metered here. ApplyGuardrail runs only for policy answers (one per answer). USD
comes from the app's `PRICING` table at the `us.` geographic-profile rates, which list 10% above the `global.`
rates — the same rates as the previous baselines (`90d316a`, `b1f74cb`). This bundle spends more than they do
because `form_read` also reads every fixture of section 8, one Nova 2 Lite call each; the other call types match
them up to token-count variance.
Latency comes from `/costs` and covers the model call only (no HTTP, no Textract or Transcribe time); medians move
run to run.

## 7. Cost per simulated outage-hour

$0.4370 per simulated outage-hour: $0.327756 of model calls over 0.75 simulated hours (outage declared after +15 min, `/costs` read after +45 min).

Notes (evals/NOTES.md, `outage_cost`):

Simulated hours run from the outage declaration (clock 03:00) to the `/costs` read (clock 03:45). Only Bedrock
model calls are counted. Textract, Transcribe and ApplyGuardrail are billed by AWS outside the app; section 6
lists how many of each ran.

## 8. Form extraction (forms built from public handwriting samples)

| Fixture | Nova 2 Lite | Textract | Combined |
|---|---|---|---|
| hw-admission_note-001 | 100.0% (5/5) | 60.0% (3/5) | 80.0% (4/5) |
| hw-admission_note-002 | 100.0% (5/5) | 100.0% (5/5) | 100.0% (5/5) |
| hw-admission_note-003 | 100.0% (5/5) | 100.0% (5/5) | 100.0% (5/5) |
| hw-admission_note-004 | 80.0% (4/5) | 100.0% (5/5) | 100.0% (5/5) |
| hw-admission_note-005 | 100.0% (5/5) | 80.0% (4/5) | 100.0% (5/5) |
| hw-admission_note-006 | 100.0% (5/5) | 80.0% (4/5) | 100.0% (5/5) |
| hw-admission_note-007 | 100.0% (5/5) | 80.0% (4/5) | 80.0% (4/5) |
| hw-admission_note-008 | 80.0% (4/5) | 80.0% (4/5) | 80.0% (4/5) |
| hw-admission_note-009 | 80.0% (4/5) | 100.0% (5/5) | 100.0% (5/5) |
| hw-admission_note-010 | 60.0% (3/5) | 60.0% (3/5) | 60.0% (3/5) |
| hw-admission_note-011 | 100.0% (5/5) | 80.0% (4/5) | 80.0% (4/5) |
| hw-admission_note-012 | 100.0% (5/5) | 100.0% (5/5) | 100.0% (5/5) |
| hw-downtime_mar-001 | 85.7% (6/7) | 85.7% (6/7) | 100.0% (7/7) |
| hw-downtime_mar-002 | 85.7% (6/7) | 100.0% (7/7) | 100.0% (7/7) |
| hw-downtime_mar-003 | 85.7% (6/7) | 85.7% (6/7) | 100.0% (7/7) |
| hw-downtime_mar-004 | 100.0% (7/7) | 100.0% (7/7) | 100.0% (7/7) |
| hw-downtime_mar-005 | 71.4% (5/7) | 100.0% (7/7) | 100.0% (7/7) |
| hw-downtime_mar-006 | 100.0% (7/7) | 71.4% (5/7) | 85.7% (6/7) |
| hw-downtime_mar-007 | 85.7% (6/7) | 71.4% (5/7) | 85.7% (6/7) |
| hw-downtime_mar-008 | 85.7% (6/7) | 71.4% (5/7) | 71.4% (5/7) |
| hw-downtime_mar-009 | 85.7% (6/7) | 71.4% (5/7) | 71.4% (5/7) |
| hw-downtime_mar-010 | 100.0% (7/7) | 100.0% (7/7) | 100.0% (7/7) |
| hw-downtime_mar-011 | 85.7% (6/7) | 57.1% (4/7) | 71.4% (5/7) |
| hw-downtime_mar-012 | 71.4% (5/7) | 71.4% (5/7) | 71.4% (5/7) |
| hw-vitals_flowsheet-001 | 90.0% (9/10) | 100.0% (10/10) | 100.0% (10/10) |
| hw-vitals_flowsheet-002 | 100.0% (10/10) | 100.0% (10/10) | 100.0% (10/10) |
| hw-vitals_flowsheet-003 | 70.0% (7/10) | 80.0% (8/10) | 90.0% (9/10) |
| hw-vitals_flowsheet-004 | 100.0% (10/10) | 100.0% (10/10) | 100.0% (10/10) |
| hw-vitals_flowsheet-005 | 90.0% (9/10) | 90.0% (9/10) | 100.0% (10/10) |
| hw-vitals_flowsheet-006 | 90.0% (9/10) | 100.0% (10/10) | 100.0% (10/10) |
| hw-vitals_flowsheet-007 | 90.0% (9/10) | 80.0% (8/10) | 80.0% (8/10) |
| hw-vitals_flowsheet-008 | 80.0% (8/10) | 100.0% (10/10) | 100.0% (10/10) |
| hw-vitals_flowsheet-009 | 80.0% (8/10) | 90.0% (9/10) | 90.0% (9/10) |
| hw-vitals_flowsheet-010 | 100.0% (10/10) | 100.0% (10/10) | 100.0% (10/10) |
| hw-vitals_flowsheet-011 | 100.0% (10/10) | 100.0% (10/10) | 100.0% (10/10) |
| hw-vitals_flowsheet-012 | 100.0% (10/10) | 100.0% (10/10) | 100.0% (10/10) |
| **All (forms built from public handwriting samples)** | 89.8% (237/264) | 88.6% (234/264) | 92.4% (244/264) |

Each fixture is one of the three form templates filled in with one writer's characters from NIST Special Database 19 (public, labeled handwriting samples), then given a photo-style distortion; scored exactly like sections 3 and 4. False-flag rate 10.7% (26/243). Flags that caught a wrong value: 11. Wrong values with no flag (silent errors): 9.

Notes (evals/NOTES.md, `photographed`):

What the set is: the three synthetic form templates, filled in with values the generator chose
(`tools/hw_fixtures.py`) and written in characters from NIST Special Database 19. SD19 is handprint: letters and
digits printed by hand on NIST's sample forms and cut out one character at a time. So this set measures careful
block handprint, not cursive, joined or rushed bedside writing, and it is not a set of forms a person filled in by
hand. Every fixture uses one real writer's characters throughout, and no two fixtures share a writer.

Words are composed letter by letter: each value is pasted one character image at a time, with letter and word
gaps set by the generator, not by the writer's own hand. Scoring is the same exact match as sections 3 and 4
(lowercase, whitespace collapsed, no synonyms), and it was declared before the run that spacing-only reader errors
count as misses: "ast hma" for "asthma" is a miss. The same rule makes a time read with a dot or without its colon
("07.03" for "07:03", "1740" for "17:40") a miss. The most frequent combined misses are the route "IO" read as
"10" by both readers, and blank boxes where Textract returned text (such as the printed label "(0-10)") that the
combined record kept.

`hw-admission_note-006` carries a small ink speck that came with one of its source character images: authentic
ink from NIST's scan, not something the generator drew. It can read as a decimal point. The owner reviewed the
fixture and accepted it as is. In this run neither reader returned a stray decimal point on that form.

The photo look is software only. Each image is the composited page put through a perspective tilt, a lighting
gradient, a slight rotation, sensor noise, blur and JPEG compression, at one of three strengths (L0, L1, L2; L0
is exactly the synthetic set's effect, with no perspective or lighting change). No camera took these pictures,
and no image-generation model was used anywhere in making them. The images stay out of git and are rebuilt
byte-for-byte from the seed; licensing and privacy are in `docs/handwriting-datasets.md`.

Handwriting samples: NIST Special Database 19, Handprinted Forms and Characters, 2nd edition, National Institute
of Standards and Technology, https://doi.org/10.18434/T4H01C. NIST is the source of the data. Changed by
DowntimeCopilot in 2026: individual character images were cropped, scaled, recolored and composited onto
synthetic form templates, then distorted to resemble phone photos (`tools/hw_fixtures.py`). NIST does not endorse
this project.

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

Form fields (forms built from public handwriting samples; field, expected, got):

- hw-admission_note-001 / textract / `allergies`: expected "codeine nausea", got "code ine nausen"
- hw-admission_note-001 / textract / `history`: expected "asthma", got "Asthmas"
- hw-admission_note-001 / combined / `history`: expected "asthma", got "Asthmas"
- hw-admission_note-004 / nova / `allergies`: expected "shellfish", got "sheffish"
- hw-admission_note-005 / textract / `history`: expected "asthma", got "ast hma"
- hw-admission_note-006 / textract / `bed`: expected "9", got "is"
- hw-admission_note-007 / textract / `history`: expected null, got "ALLERGIES (SUBSTANCE + REACTION)"
- hw-admission_note-007 / combined / `history`: expected null, got "ALLERGIES (SUBSTANCE + REACTION)"
- hw-admission_note-008 / nova / `weight_kg`: expected "89.9", got "84.9"
- hw-admission_note-008 / textract / `weight_kg`: expected "89.9", got "84.9"
- hw-admission_note-008 / combined / `weight_kg`: expected "89.9", got "84.9"
- hw-admission_note-009 / nova / `allergies`: expected "penicillin hives", got "penicilllin hives"
- hw-admission_note-010 / nova / `height_cm`: expected "159", got "169"
- hw-admission_note-010 / nova / `weight_kg`: expected "50.8", got "60.8"
- hw-admission_note-010 / textract / `height_cm`: expected "159", got "169"
- hw-admission_note-010 / textract / `weight_kg`: expected "50.8", got "60.8"
- hw-admission_note-010 / combined / `height_cm`: expected "159", got "169"
- hw-admission_note-010 / combined / `weight_kg`: expected "50.8", got "60.8"
- hw-admission_note-011 / textract / `height_cm`: expected null, got "20"
- hw-admission_note-011 / combined / `height_cm`: expected null, got "20"
- hw-downtime_mar-001 / nova / `second_verifier`: expected "UL", got "4L"
- hw-downtime_mar-001 / textract / `time_given`: expected "17:40", got "1740"
- hw-downtime_mar-002 / nova / `time_given`: expected "16:26", got "1626"
- hw-downtime_mar-003 / nova / `bed`: expected "4", got "7"
- hw-downtime_mar-003 / textract / `route`: expected "IV", got "N"
- hw-downtime_mar-005 / nova / `dose`: expected "25 mg", got "a5 mg"
- hw-downtime_mar-005 / nova / `route`: expected "SL", got "BL"
- hw-downtime_mar-006 / textract / `initials`: expected "QK", got "QV"
- hw-downtime_mar-006 / textract / `route`: expected null, got "WA"
- hw-downtime_mar-006 / combined / `route`: expected null, got "WA"
- hw-downtime_mar-007 / nova / `route`: expected "IO", got "10"
- hw-downtime_mar-007 / textract / `route`: expected "IO", got "10"
- hw-downtime_mar-007 / textract / `second_verifier`: expected "OS", got "HIGH-ALERT MEDS"
- hw-downtime_mar-007 / combined / `route`: expected "IO", got "10"
- hw-downtime_mar-008 / nova / `route`: expected "IO", got "10"
- hw-downtime_mar-008 / textract / `dose`: expected "0.1 mg", got "01 mg"
- hw-downtime_mar-008 / textract / `route`: expected "IO", got "10"
- hw-downtime_mar-008 / combined / `dose`: expected "0.1 mg", got "01 mg"
- hw-downtime_mar-008 / combined / `route`: expected "IO", got "10"
- hw-downtime_mar-009 / nova / `route`: expected "IO", got "10"
- hw-downtime_mar-009 / textract / `dose`: expected "0.1 mg", got "01 mg"
- hw-downtime_mar-009 / textract / `route`: expected "IO", got "10"
- hw-downtime_mar-009 / combined / `dose`: expected "0.1 mg", got "01 mg"
- hw-downtime_mar-009 / combined / `route`: expected "IO", got "10"
- hw-downtime_mar-011 / nova / `route`: expected "IO", got "10"
- hw-downtime_mar-011 / textract / `medication`: expected "insulin", got "Insulen"
- hw-downtime_mar-011 / textract / `route`: expected "IO", got "10"
- hw-downtime_mar-011 / textract / `time_given`: expected null, got "24  H"
- hw-downtime_mar-011 / combined / `medication`: expected "insulin", got "Insulen"
- hw-downtime_mar-011 / combined / `route`: expected "IO", got "10"
- hw-downtime_mar-012 / nova / `route`: expected "IO", got "10"
- hw-downtime_mar-012 / nova / `time_given`: expected "07:55", got "07.55"
- hw-downtime_mar-012 / textract / `medication`: expected null, got "1.5 mg"
- hw-downtime_mar-012 / textract / `route`: expected "IO", got "10"
- hw-downtime_mar-012 / combined / `medication`: expected null, got "1.5 mg"
- hw-downtime_mar-012 / combined / `route`: expected "IO", got "10"
- hw-vitals_flowsheet-001 / nova / `time`: expected "07:03", got "07.03"
- hw-vitals_flowsheet-003 / nova / `resp_rate`: expected "12", got "102"
- hw-vitals_flowsheet-003 / nova / `spo2`: expected "97", got "94"
- hw-vitals_flowsheet-003 / nova / `time`: expected "19:08", got "19.08"
- hw-vitals_flowsheet-003 / textract / `bed`: expected "5", got "10"
- hw-vitals_flowsheet-003 / textract / `heart_rate`: expected "110", got "10"
- hw-vitals_flowsheet-003 / combined / `heart_rate`: expected "110", got "10"
- hw-vitals_flowsheet-005 / nova / `initials`: expected "NY", got "NV"
- hw-vitals_flowsheet-005 / textract / `resp_rate`: expected "13", got "3"
- hw-vitals_flowsheet-006 / nova / `time`: expected "17:24", got "17.24"
- hw-vitals_flowsheet-007 / nova / `time`: expected "18:08", got "18:18"
- hw-vitals_flowsheet-007 / textract / `pain_score`: expected null, got "(0-10)"
- hw-vitals_flowsheet-007 / textract / `time`: expected "18:08", got "18:118"
- hw-vitals_flowsheet-007 / combined / `pain_score`: expected null, got "(0-10)"
- hw-vitals_flowsheet-007 / combined / `time`: expected "18:08", got "18:18"
- hw-vitals_flowsheet-008 / nova / `bed`: expected "19", got "17"
- hw-vitals_flowsheet-008 / nova / `initials`: expected "BSB", got "B5B"
- hw-vitals_flowsheet-009 / nova / `bed`: expected "11", got "II"
- hw-vitals_flowsheet-009 / nova / `initials`: expected "SZ", got "S2"
- hw-vitals_flowsheet-009 / textract / `initials`: expected "SZ", got "S2"
- hw-vitals_flowsheet-009 / combined / `initials`: expected "SZ", got "S2"

Speech words (operation, expected, got):

- none

Verbal-order structured fields (field, expected, got):

- `ordering_provider`: expected "Dr. Quill", got "Doctor Quill"
- `provider_cosign`: expected "no", got null
- `read_back`: expected "yes", got "confirmed"
- `route`: expected "SC", got "subcutaneous"
