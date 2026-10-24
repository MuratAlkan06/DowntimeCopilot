# Eval fixtures

Each fixture is a **pair** with the same stem:

```text
evals/fixtures/vitals-01.jpg     # photo of a hand-filled paper form (jpg / jpeg / png)
evals/fixtures/vitals-01.json    # ground truth for that photo
```

Ground-truth JSON:

```json
{
  "form_type": "vitals_flowsheet",
  "fields": {"bed": "12", "time": "02:55", "temp_c": "37.8", "heart_rate": "104", "resp_rate": "22",
             "bp_systolic": "148", "bp_diastolic": "86", "spo2": "89", "pain_score": null, "initials": "PN"}
}
```

- `form_type` is one of `vitals_flowsheet`, `downtime_mar`, `admission_note` (contracts `FormType`).
- `fields` lists exactly what the writer put on paper, transcribed by a teammate who did **not** see the readers' output. Use `null` for a box left blank or deliberately made illegible.
- Field names match the reader prompts (`app/ai/forms.py` `MOCK_READS` lists them per form type).

Rules:

- Photos stay local: `*.jpg`, `*.jpeg`, `*.png` in this folder are git-ignored (they may contain real handwriting). Ground-truth JSON can be committed.
- Only synthetic patient data on the forms.
- Run with live credentials: `source .env && DRY_RUN=0 make evals`. The harness refuses to run in `DRY_RUN=1` and exits nonzero with no fixtures; it never writes numbers it did not measure.
