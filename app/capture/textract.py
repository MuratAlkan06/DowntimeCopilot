"""Textract adapter: second, independent read of a photographed form.

Real call shape verified against the installed botocore service model (textract
DetectDocumentText: Document{Bytes|S3Object} -> Blocks[] with BlockType LINE/WORD, Text, Confidence
0..100, Geometry.BoundingBox). docs/decisions.md: Textract ALLOWED (DetectDocumentText OK).
"""
from __future__ import annotations

import re

from app import config
from contracts import FormType, ReaderOutput

# Canned dry-run read; differs from the Bedrock mock only on resp_rate (README demo step 3).
MOCK: dict[FormType, dict[str, tuple[str | None, float]]] = {
    FormType.vitals_flowsheet: {
        "bed": ("12", 0.99), "time": ("03:30", 0.95), "temp_c": ("37.8", 0.94), "heart_rate": ("104", 0.93),
        "resp_rate": ("22", 0.58), "bp_systolic": ("148", 0.92), "bp_diastolic": ("86", 0.9),
        "spo2": ("89", 0.95), "pain_score": (None, 0.12), "initials": ("PN", 0.9),
    },
    FormType.downtime_mar: {
        "bed": ("12", 0.98), "medication": ("heparin", 0.88), "dose": ("5000 units", 0.9), "route": ("SC", 0.96),
        "time_given": ("03:10", 0.92), "initials": ("PN", 0.9), "second_verifier": (None, 0.2),
    },
    FormType.admission_note: {
        "bed": ("12", 0.97), "allergies": ("penicillin (hives)", 0.8), "height_cm": ("178", 0.93),
        "weight_kg": ("92.4", 0.91), "history": ("type 2 diabetes, hypertension", 0.75),
    },
}

_LABEL = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _/()%-]{0,40}?)\s*[:=]\s*(.*)$")


def _field_name(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def read(image: bytes, form_type: FormType) -> ReaderOutput | None:
    """None when Textract is disabled or the form type has no image (agreement becomes 'single')."""
    if form_type == FormType.verbal_order or not config.textract_enabled():
        return None
    if config.dry_run():
        data = MOCK[form_type]
        return ReaderOutput(fields={k: v for k, (v, _) in data.items()}, confidence={k: c for k, (_, c) in data.items()})
    import boto3
    resp = boto3.client("textract", region_name=config.aws_region()).detect_document_text(Document={"Bytes": image})
    # TODO(role 4): map lines to the printed form's boxes by BoundingBox (or AnalyzeDocument FORMS /
    # QUERIES) once data/forms/*.pdf layouts are final. Naive "Label: value" parsing until then.
    fields: dict[str, str | None] = {}
    conf: dict[str, float] = {}
    for b in resp.get("Blocks", []):
        if b.get("BlockType") != "LINE":
            continue
        m = _LABEL.match(b.get("Text", ""))
        if m:
            name = _field_name(m.group(1))
            fields[name] = m.group(2).strip() or None
            conf[name] = round(float(b.get("Confidence", 0.0)) / 100.0, 3)
    return ReaderOutput(fields=fields, confidence=conf)
