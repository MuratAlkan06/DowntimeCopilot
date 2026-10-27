# Eval notes

Commentary for RESULTS.md, keyed by metric (`## <metric>`). `make results` copies each section verbatim under its
metric. Keys: faithfulness, grounding, extraction, false_flags, speech, latency_cost, outage_cost. Numbers are
never typed here; they come from the bundle.

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
and collapsing whitespace, with no synonym mapping. Photographed handwritten forms are still pending.

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
rates — the same rates as the previous baseline (`90d316a`), so this bundle's spend matches it almost exactly;
the residual difference is token-count variance.
Latency comes from `/costs` and covers the model call only (no HTTP, no Textract or Transcribe time); medians move
run to run.

## outage_cost

Simulated hours run from the outage declaration (clock 03:00) to the `/costs` read (clock 03:45). Only Bedrock
model calls are counted. Textract, Transcribe and ApplyGuardrail are billed by AWS outside the app; see the
decisions entry for this run.
