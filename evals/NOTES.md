# Eval notes

Commentary for RESULTS.md, keyed by metric (`## <metric>`). `make results` copies each section verbatim under its
metric. Keys: faithfulness, grounding, extraction, false_flags, speech, latency_cost, outage_cost, photographed,
refusals.
Numbers are never typed here; they come from the bundle.

## faithfulness

One run of 12 packets. Every first-pass line resolved, so the regenerate-once path and the omission path did not
run in this bundle; their behaviour is covered by unit tests only, not by this measurement. The line count differs
from the previous baseline (`20261024T120050Z-90d316a`, kept under `evals/baseline/` as history) on identical
inputs: per-packet totals move by a line or two between runs, which is run-to-run model variance, not a seed or
harness change.

## grounding

Five questions, all from the DEMO.md table. Citations are paragraph-level (for example 4.2.1 under 4.2), and the
correctness check accepts a citation at or under the expected section. The safe-refusal beat (dosing question) is
not part of the manifest, so the guardrail's intervention path is not measured here.

## extraction

These are synthetic printed fixtures rendered by `tools/form_fixtures.py`, not handwriting, so they overstate
real-world accuracy. All Textract misses are blank boxes where Textract returned text: `pain_score` on the vitals
flowsheet and `second_verifier` on the MAR. Nova 2 Lite left both blank. The combined vitals miss is the
`pain_score` disagreement: the merged record keeps Textract's value with confidence 0 and flags it for review, so
it shows up under "flags that caught a wrong value", not as a silent error. Scoring is exact match after lowercasing
and collapsing whitespace, with no synonym mapping. Values in real people's handprint are measured in section 8
(forms built from public handwriting samples); see its notes for what that set is and is not.

## false_flags

Clean fields leave out blanks and out-of-range vitals. The SpO2 89 out-of-range flag is expected, so it is not
counted as a false flag.

## speech

One synthetic text-to-speech clip (`verbal_order-01.wav`, voice Samantha, 165 wpm), so this is not a measure of
accuracy on real speech. Every structuring miss is a normalization gap and not a hearing error: "Doctor Quill"
for "Dr. Quill", "confirmed" for "yes", "subcutaneous" for "SC". `provider_cosign` was left unread because the
read-back never mentions a co-sign. S9 saw the same behaviour live, and this run repeats the previous baseline's
four misses field for field. Verbal-order structuring is still the weakest measured step.

## latency_cost

`checker` is the packet guardrail. Packets send grounding without a query, so `BedrockGuardrail` routes them to
the Nova 2 Lite checker, which is metered here. ApplyGuardrail runs only for policy answers (one per answer). USD
comes from the app's `PRICING` table at the `us.` geographic-profile rates, which list 10% above the `global.`
rates — the same rates as the previous baselines (`90d316a`, `b1f74cb`). This bundle spends more than they do
because `form_read` also reads every fixture of section 8, one Nova 2 Lite call each; the other call types match
them up to token-count variance.
Latency comes from `/costs` and covers the model call only (no HTTP, no Textract or Transcribe time); medians move
run to run.

## outage_cost

Simulated hours run from the outage declaration (clock 03:00) to the `/costs` read (clock 03:45). Only Bedrock
model calls are counted. Textract, Transcribe and ApplyGuardrail are billed by AWS outside the app; section 6
lists how many of each ran.

## photographed

What the set is: the three synthetic form templates, filled in with values the generator chose
(`tools/hw_fixtures.py`) and written in characters from NIST Special Database 19. SD19 is handprint: letters and
digits printed by hand on NIST's sample forms and cut out one character at a time. So this set measures careful
block handprint, not cursive, joined or rushed bedside writing, and it is not a set of forms a person filled in by
hand. Every fixture uses one real writer's characters throughout, and no two fixtures share a writer.

The mix departs from the plan frozen in #67 in one place. The plan called for 6 nominal, 3 ambiguous-heavy and 3
out-of-range fixtures per form, but only the vitals flowsheet has normal ranges, so the set holds 3 out-of-range
fixtures in total, all vitals flowsheets. The admission note and the MAR wrote their out-of-range slots as
ambiguous-heavy fixtures with one extra blank box each. The tag counts were disclosed in #70, and the owner's QA
pass accepted all 36. All three out-of-range values (SpO2 81 and 78, temperature 40.6) were read correctly by both
readers and flagged out_of_range.

Words are composed letter by letter: each value is pasted one character image at a time, with letter and word
gaps set by the generator, not by the writer's own hand. Scoring is the same exact match as sections 3 and 4
(lowercase, whitespace collapsed, no synonyms), and it was declared before the run that spacing-only reader errors
count as misses: "ast hma" for "asthma" is a miss. The same rule makes a time read with a dot or without its colon
("07.03" for "07:03", "1740" for "17:40") a miss. The most frequent combined misses are the route "IO" read as
"10" by both readers, and blank boxes where Textract returned text (such as the printed label "(0-10)") that the
combined record kept. The clinically sharper misses are three numbers that both readers got wrong the same way, so
the combined record kept them with no flag: weight 89.9 read as 84.9 (`hw-admission_note-008`), and height 159 read
as 169 and weight 50.8 read as 60.8 (`hw-admission_note-010`). When both readers agree on a wrong number, the
agreement check has nothing to flag, and weight and height have no normal range to trip.

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

## refusals

First live run pending. The question set was fixed in `evals/manifest.json` (`refusal_questions`) before any run;
each miss of that run will be listed here with its question id, expected outcome, actual outcome and reason.
