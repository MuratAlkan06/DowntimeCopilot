# Downtime Copilot: pitch

## One line

When the EHR goes dark, Downtime Copilot briefs the unit on every patient, keeps the paper process safe, and brings everything written on paper back into structured records when systems return.

## Priya's night

Priya Nair is the night supervisor on a 24-bed med-surg unit. At 2:47 AM security isolates the EHR after a ransomware alert. She has six nurses, four fresh admissions and forty-plus medications due at 4 AM. Barcode scanning is gone, so every dose is a safety risk. The day shift arrives at 7 with no handoff. When the system comes back, everything written on paper has to be re-entered by hand by tired staff.

## The problem

EHR downtime is routine: ransomware isolation, vendor outages, bad updates, planned upgrades. Hospitals already have a checkpoint for it. Epic's Business Continuity Access (and Cerner's 724Access) keeps a read-only copy of recent data and prints raw reports, so data gets out. Nothing brings it back in. After recovery, staff backload every medication administration, vital sign, order and allergy from paper by hand, at the end of a shift.

## What we built

One offline-first app for three phases and the trip back:

- **Before.** A snapshot of the unit every 15 minutes, a deterministic readiness score, and a drill that rehearses an outage without touching live data.
- **Declare downtime.** A one-page SBAR packet per patient, built from the last good snapshot. Every line cites the snapshot field it came from. Deterministic safety rules (allergy conflicts, duplicate classes, high-alert doses due, stale data) run with no model. Role checklists come from the hospital's own policy.
- **During.** Policy answers that cite the policy section. A bilingual notice for patients and families. Paper forms photographed and read by two independent readers. Verbal-order read-backs: Transcribe transcribes, Bedrock structures, and a rule flags provider co-sign. All of it works offline: captures queue on the device and upload on reconnect.
- **The return trip.** **Systems restored** turns the queue into a backload worklist, grouped the way Epic groups it. A human verifies each record (two people for high-alert medications). The app diffs the restored EHR against the snapshot and the paper, raises discrepancies, and writes the shift digest from verified facts only. A command center shows the charge nurse the whole unit on one screen.

Rules decide; Bedrock explains. Humans verify everything before it counts.

## Bedrock's four jobs

1. **Packets.** Sonnet 4.6 writes the SBAR packet. A faithfulness check rejects any line whose source path does not resolve in the snapshot.
2. **Form reading.** Nova 2 Lite reads each photographed form beside Amazon Textract. Agreement logic decides what the reviewer sees, and an unreadable field is flagged, never guessed.
3. **Policy answers.** Sonnet 4.6 answers from the hospital's downtime policy and cites numbered sections. They pass the published Bedrock Guardrail (denied topic: dosing advice not in the record; contextual grounding).
4. **Shift digest.** Sonnet 4.6 writes the handoff from verified records and the audit log. Every line cites its source.

## Measured results

Each table comes from one recorded live run of fixed inputs in us-east-1; every run's bundle is kept under `evals/baseline/`.

Synthetic-fixture baseline, run `20261024T120050Z-90d316a`. All inputs are synthetic: 12 seeded patients, 5 policy questions, 3 printed form fixtures, 1 text-to-speech clip.

| What | Result |
|---|---|
| Packet lines whose source resolves to a snapshot field | 100.0% (198/198), 12 packets, 0 withheld |
| Policy answers grounded and citing the expected section | 100.0% (5/5) |
| Form fields, Nova 2 Lite / Textract / combined (synthetic printed forms) | 100.0% (22/22) / 90.9% (20/22) / 95.5% (21/22) |
| Wrong combined values left unflagged | 0. The one combined miss was flagged for review |
| False flags on clean fields | 0.0% (0/19) |
| Speech word accuracy (1 synthetic clip) | 100.0% (17/17) |
| Verbal-order fields structured correctly from that transcript | 42.9% (3/7) |
| Bedrock spend for the whole run | $0.2780, or $0.3706 per simulated outage-hour |

Verbal-order structuring is the weak spot, and we report it as measured. Three of the four misses are normalization gaps ("Doctor Quill" for "Dr. Quill", "confirmed" for "yes", "subcutaneous" for "SC"). The fourth is a co-sign the read-back never mentions. The previous baseline had the same four misses. Textract, Transcribe and Guardrail checks are billed outside the app's meter, about $0.20 more for this run.

Forms built from public handwriting samples, run `20261028T060034Z-609f90e` (the run [`RESULTS.md`](RESULTS.md) shows): 36 forms, glyphs from NIST Special Database 19 composited onto the three templates, one writer per fixture, with a software photo-style distortion. They are not forms a person filled in by hand.

| What | Result |
|---|---|
| Form fields, Nova 2 Lite / Textract / combined (forms built from public handwriting samples) | 89.8% (237/264) / 88.6% (234/264) / 92.4% (244/264) |
| Wrong combined values left unflagged | 9 of 20. The other 11 were flagged for review |
| False flags on clean fields | 10.7% (26/243) |

The same run repeats every synthetic-set number in the first table except packet lines, 100.0% (192/192), and spend, $0.3278 ($0.4370 per simulated outage-hour), which also pays for the 36 extra form reads.

## Limitations

- Synthetic data only: a simulated EHR, BCA feed and outage, for a single unit.
- Form accuracy comes from printed synthetic fixtures, which overstate real-world handwriting accuracy, and from forms built from public handwriting samples: NIST Special Database 19 glyphs composited onto the templates, not forms a person filled in by hand. On those the two readers combined get 92.4% (244/264) of fields right; the samples are careful block handprint rather than rushed bedside writing, and the photo effect is software, not a camera.
- Speech is one synthetic clip, not real clinical speech.
- The Spanish patient notice has passed the Guardrail check but is pending native-speaker review, and it says so on the page.
- No real authentication: roles are self-asserted. The local store is not encrypted at rest. Knowledge Bases retrieval and the cloud-side Lambda puller were not built in this account.

## Path to production

- **Authentication and access:** SSO with real roles, and on-device encryption at rest for the store and the queue.
- **Real EHR integration:** read from the hospital's BCA or FHIR feed, and write the backload through the EHR's own interfaces, still behind human verification.
- **Clinical review:** policy content, rules and thresholds reviewed by nursing, pharmacy and clinical-safety owners. Accuracy measured on real photographed forms and real read-backs.
- **Native translation:** a native speaker reviews the Spanish notice, and more languages go through the same review.
