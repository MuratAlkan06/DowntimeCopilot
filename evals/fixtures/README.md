# Eval fixtures

Forms built from public handwriting samples, for the form-reading evals. Each fixture is a **pair** with the
same stem:

```text
evals/fixtures/hw-vitals_flowsheet-001.jpg    # the form image (git-ignored, regenerated from the seed)
evals/fixtures/hw-vitals_flowsheet-001.json   # ground truth for that image (committed)
```

## Provenance

- **Values first.** `tools/hw_fixtures.py` chooses every field's value before the image exists (a fixed,
  parenthesis-free value policy; vitals and their out-of-range variants come from `app.config.vital_ranges()`),
  and writes the truth JSON before any glyph is picked. Truth is exact by construction, not a transcription.
- **Real ink, public source.** Every mark on the form is a character image from NIST Special Database 19 (a
  public U.S. government dataset), one anonymous writer per fixture, composited onto the synthetic form
  templates and then distorted to look like a phone photo (L0 is the synthetic set's photo effect; L1 and L2
  add perspective and uneven lighting). Licensing, acknowledgment and privacy:
  [`docs/handwriting-datasets.md`](../../docs/handwriting-datasets.md).
- **Human QA.** A person checks every generated image against its truth before the truth JSONs are committed.
  A fixture a careful reader cannot read as its truth is regenerated or dropped, never relabeled.
- Call them "forms built from public handwriting samples", never "handwritten forms".

## Ground-truth JSON (schema 1)

```json
{
  "schema": 1,
  "form_type": "vitals_flowsheet",
  "fields": {"bed": "12", "time": "02:55", "initials": "PN", "temp_c": "37.8", "heart_rate": "104",
             "resp_rate": "22", "bp_systolic": "148", "bp_diastolic": "86", "spo2": "89", "pain_score": null},
  "image": "hw-vitals_flowsheet-001.jpg",
  "seed": 20261027,
  "writer": "f0000_14",
  "distortion": "L0",
  "tags": ["nominal", "blank"],
  "source": "composited from public handwriting samples (NIST Special Database 19)",
  "image_sha256": "<sha256 of the image bytes>"
}
```

- `form_type` is one of `vitals_flowsheet`, `downtime_mar`, `admission_note` (contracts `FormType`).
- `fields` lists exactly the reader's fields for that form type (`app/ai/forms.py` `MOCK_READS`). `null` means
  the box was left blank on paper.
- `writer` is SD19's anonymous writer code; `tags` are `nominal`, `ambiguous` (confusion-pair digits and
  letters, i/l/s-rich words) or `out_of_range`, plus `blank` when any box is empty.
- `image_sha256` binds the image to its truth; the eval run checks it before any model spend.

## Generating

```bash
python -m tools.hw_glyphs fetch --write-lock   # once, network: download SD19, verify NIST's MD5 logs, pin sha256
python -m tools.hw_glyphs index                # hash-join by_class x by_write into .cache/handwriting/
python -m tools.hw_fixtures --seed N --out evals/fixtures --glyph-root .cache/handwriting
```

The same seed and the same verified cache reproduce byte-identical images; the seed is in every truth JSON.

## Rules

- Images stay local: `*.jpg`, `*.jpeg`, `*.png` in this folder are git-ignored (each one reproduces a real
  person's letterforms). Ground-truth JSON is committed.
- Only synthetic patient data on the forms.
- Run with live credentials: `source .env && DRY_RUN=0 make evals`. The harness refuses to run in `DRY_RUN=1`
  and exits nonzero with no fixtures; it never writes numbers it did not measure.
