<!-- GENERATED - DO NOT EDIT. tools/model_compare.py writes this file from report.json. -->
# Model comparison: Nova 2 Lite, Claude Haiku 4.5, Claude Sonnet 4.6

Status: **complete**

- Run id: `20261028T081528Z-beca1f9` (live); started / finished (UTC): 2026-10-28T08:15:28+00:00 / 2026-10-28T08:23:55+00:00
- Git SHA at run time: `beca1f99e83816f8d55ba4f919dd1c7d28459e15`
- Fixed eval workload per model: 39 form reads (36 photographed + 3 synthetic), 12 packets, 5 policy answers (`evals/manifest.json`)
- Forms re-merged with the fixed Textract reads of `evals/baseline/20261028T060034Z-609f90e/` (digest `137d625b84c377968f726d05183782112c845922ad8a9c97412113de6a2e6150`, 39 reads); no Textract call in this run
- in-process TestClient, no lifespan (no scheduler, no sync worker); fresh empty scratch store per attempt; packets at PACKET_CONCURRENCY=1 (production 4); guardrail mode `bedrock`; packet checker `us.amazon.nova-2-lite-v1:0` in every pass
- Snapshots byte-identical across the 3 published pass(es): yes

## Comparison

| Model | Job | Accuracy | Model call p50 / p95 ms (n) | Wall-clock p50 / p95 ms (n) | USD per job |
|---|---|---|---|---|---|
| Nova 2 Lite | Form reading, photographed (36) | combined 91.7% (242/264); model alone 89.0% (235/264) | 2068 / 2841 (n=36) | 2071.5 / 2843 (n=36) | $0.001531 |
| Nova 2 Lite | Form reading, synthetic (3) | combined 95.5% (21/22); model alone 100.0% (22/22) | 2498 / 32907 (n=3) | 2501 / 32910 (n=3) | $0.001556 |
| Nova 2 Lite | SBAR packet (12) | final 100.0% (108/108); first pass 100.0% (108/108) | 2062.5 / 2419 (n=12) | 2602 / 3000 (n=12) | $0.001520 |
| Nova 2 Lite | Policy answer (5) | expected section 100.0% (5/5); grounded 100.0% (5/5) | 1120 / 1222 (n=5) | 1500 / 1609 (n=5) | $0.001129 |
| Claude Haiku 4.5 | Form reading, photographed (36) | combined 92.4% (244/264); model alone 92.0% (243/264) | 2339 / 2914 (n=36) | 2343 / 2919 (n=36) | $0.004708 |
| Claude Haiku 4.5 | Form reading, synthetic (3) | combined 95.5% (21/22); model alone 100.0% (22/22) | 2331 / 2790 (n=3) | 2337 / 2793 (n=3) | $0.004711 |
| Claude Haiku 4.5 | SBAR packet (12) | final 100.0% (130/130); first pass 100.0% (130/130) | 2366.5 / 2868 (n=12) | 2917.5 / 3465 (n=12) | $0.004529 |
| Claude Haiku 4.5 | Policy answer (5) | expected section 100.0% (5/5); grounded 100.0% (5/5) | 1686 / 2326 (n=5) | 2073 / 2796 (n=5) | $0.003835 |
| Claude Sonnet 4.6 | Form reading, photographed (36) | combined 91.7% (242/264); model alone 79.5% (210/264) | 3096.5 / 4112 (n=36) | 3102 / 4114 (n=36) | $0.012924 |
| Claude Sonnet 4.6 | Form reading, synthetic (3) | combined 95.5% (21/22); model alone 100.0% (22/22) | 2955 / 3882 (n=3) | 2959 / 3888 (n=3) | $0.013075 |
| Claude Sonnet 4.6 | SBAR packet (12) | final 100.0% (193/193); first pass 100.0% (193/193) | 4970 / 6206 (n=12) | 5585.5 / 7216 (n=12) | $0.016771 |
| Claude Sonnet 4.6 | Policy answer (5) | expected section 100.0% (5/5); grounded 100.0% (5/5) | 2734 / 3186 (n=5) | 3169 / 3558 (n=5) | $0.013520 |

Accuracy: forms = field accuracy against the committed truth (combined = the model read merged with the fixed Textract read, as /capture does); packets = SBAR lines whose source_path resolves in the snapshot; policy = grounded answers citing the expected section. USD per job: metered model calls of that job only (the packet checker and ApplyGuardrail are reported apart).

## Forms: photographed set (primary)

| Model | Model alone | Textract (fixed) | Combined | False flags on clean | Flags that caught a wrong value | Silent errors | Unparseable replies |
|---|---|---|---|---|---|---|---|
| Nova 2 Lite | 89.0% (235/264) | 88.6% (234/264) | 91.7% (242/264) | 10.3% (25/243) | 13 | 9 | 0 |
| Claude Haiku 4.5 | 92.0% (243/264) | 88.6% (234/264) | 92.4% (244/264) | 9.5% (23/243) | 14 | 6 | 0 |
| Claude Sonnet 4.6 | 79.5% (210/264) | 88.6% (234/264) | 91.7% (242/264) | 6.2% (15/243) | 10 | 12 | 8 |

## Forms: synthetic set

| Model | Model alone | Textract (fixed) | Combined | False flags on clean | Flags that caught a wrong value | Silent errors | Unparseable replies |
|---|---|---|---|---|---|---|---|
| Nova 2 Lite | 100.0% (22/22) | 90.9% (20/22) | 95.5% (21/22) | 0.0% (0/19) | 1 | 0 | 0 |
| Claude Haiku 4.5 | 100.0% (22/22) | 90.9% (20/22) | 95.5% (21/22) | 0.0% (0/19) | 1 | 0 | 0 |
| Claude Sonnet 4.6 | 100.0% (22/22) | 90.9% (20/22) | 95.5% (21/22) | 0.0% (0/19) | 1 | 0 | 0 |

An unparseable form reply is scored as a read with no fields (the model alone misses every field the truth fills; combined falls back to the Textract read) and counted above.

## Packets

| Model | First pass | Final | Omitted | Regenerated | Unparseable | Checker calls | Checker model | Checker USD |
|---|---|---|---|---|---|---|---|---|
| Nova 2 Lite | 100.0% (108/108) | 100.0% (108/108) | 0 | 0 | 0 | 12 | us.amazon.nova-2-lite-v1:0 | $0.003567 |
| Claude Haiku 4.5 | 100.0% (130/130) | 100.0% (130/130) | 0 | 0 | 0 | 12 | us.amazon.nova-2-lite-v1:0 | $0.003644 |
| Claude Sonnet 4.6 | 100.0% (193/193) | 100.0% (193/193) | 0 | 0 | 0 | 12 | us.amazon.nova-2-lite-v1:0 | $0.004661 |

## Policy

| Model | Grounded | Expected section | Guardrail interventions |
|---|---|---|---|
| Nova 2 Lite | 100.0% (5/5) | 100.0% (5/5) | 0 |
| Claude Haiku 4.5 | 100.0% (5/5) | 100.0% (5/5) | 0 |
| Claude Sonnet 4.6 | 100.0% (5/5) | 100.0% (5/5) | 0 |

## Latency instruments

- Model call: `latency_ms` of each Converse call as logged in `/costs` (one call; a transient same-model retry, if any, is inside it).
- Wall-clock: per packet, `packets.generate_packet` (model call, faithfulness check, the one regeneration if any, checker call); per policy answer, `POST /policy/ask` in-process (model call, ApplyGuardrail, grounding check); per form, `forms.read_form_with_boxes` (model call and parse).
- Packets ran one at a time (PACKET_CONCURRENCY=1; production runs 4 in flight), so these are uncontended per-packet latencies, not the production `/downtime/start` time.
- p50 = median; p95 = nearest rank, ceil(0.95 n): with n <= 20 the p95 is the maximum.

## Spend

| Model | Attempt | Status | Calls | Metered USD | ApplyGuardrail checks | Transient retries | Reasons |
|---|---|---|---|---|---|---|---|
| Nova 2 Lite | 1 | ok | 68 | $0.087233 | 5 | 0 | - |
| Claude Haiku 4.5 | 1 | ok | 68 | $0.260790 | 5 | 0 | - |
| Claude Sonnet 4.6 | 1 | ok | 68 | $0.778003 | 5 | 0 | - |

Metered $1.126026 + ApplyGuardrail allowance $0.150000 (15 checks, unmetered by the app) + probes $0.000114 = **$1.276140**, of which $0.000000 in discarded or stopped attempts. Budget limit $5.00, max projected $2.9516.

## Pricing provenance

app/ai/client.py PRICING (us. geographic profiles), verified 2026-10-24; USD per 1M tokens.

| Model id | Input | Output |
|---|---|---|
| `us.amazon.nova-2-lite-v1:0` | 0.33 | 2.75 |
| `us.anthropic.claude-haiku-4-5-20251001-v1:0` | 1.10 | 5.50 |
| `us.anthropic.claude-sonnet-4-6` | 3.30 | 16.50 |

## All-Sonnet cost counterfactual (pre-registered)

"all-Sonnet" = form_read + packet + policy on Sonnet; the Nova checker and ApplyGuardrail are identical in both arms and excluded from the difference; stated on the fixed eval workload, not production.

On the fixed eval workload (39 form reads + 12 packets + 5 policy answers): routed $0.328628 (form_read on `us.amazon.nova-2-lite-v1:0` $0.059780, packet on `us.anthropic.claude-sonnet-4-6` $0.201248, policy on `us.anthropic.claude-sonnet-4-6` $0.067600); all-Sonnet $0.773342; difference $0.444714.

## Pre-registered route-change rule (frozen in #76 before any live run)

A route changes only via its own slice, and only if: forms (primary dataset: the photographed set, combined-with-fixed-Textract accuracy): candidate strictly better by >=3 fields, no worse on silent errors, p50 model latency <=1.5x incumbent; or equal-within-3-fields AND >=40% cheaper per job with the same guards. Packets (n=12) and policy (n=5) are UNDER-POWERED: this run may recommend but never auto-trigger a packet/policy route change; that requires the owner's recorded sign-off.

| Route | Incumbent | Candidate | Combined fields vs incumbent | Silent errors (incumbent / candidate) | p50 model call ratio | USD per job ratio | Verdict |
|---|---|---|---|---|---|---|---|
| form_read | Nova 2 Lite | Claude Haiku 4.5 | +2 | 9 / 6 | 1.131 | 3.075 | |
| form_read | Nova 2 Lite | Claude Sonnet 4.6 | +0 | 9 / 12 | 1.497 | 8.442 | |
| packet | Claude Sonnet 4.6 | any | under-powered (n=12): may recommend, never auto-trigger; a change needs the owner's recorded sign-off | | | | |
| policy | Claude Sonnet 4.6 | any | under-powered (n=5): may recommend, never auto-trigger; a change needs the owner's recorded sign-off | | | | |

Verdict slots are empty in this harness version (S1 of #76); the live run (S2) records each verdict from these numbers under the rule exactly as frozen.
