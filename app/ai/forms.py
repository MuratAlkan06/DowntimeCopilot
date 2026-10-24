"""Bedrock vision form reading (Bedrock job 2) into a ReaderOutput. Runs beside Textract."""
from __future__ import annotations

import json
import re

from app.ai import client as ai
from app.capture.transcribe import MOCK_TRANSCRIPT
from contracts import FormType, ReaderOutput

# Canned dry-run reads. Vitals: vs the Textract mock, 8 fields agree, resp_rate disagrees,
# pain_score is unreadable (README demo step 3).
MOCK_READS: dict[FormType, dict[str, tuple[str | None, float]]] = {
    FormType.vitals_flowsheet: {
        "bed": ("12", 0.97), "time": ("03:30", 0.93), "temp_c": ("37.8", 0.91), "heart_rate": ("104", 0.9),
        "resp_rate": ("27", 0.62), "bp_systolic": ("148", 0.9), "bp_diastolic": ("86", 0.88),
        "spo2": ("89", 0.92), "pain_score": (None, 0.2), "initials": ("PN", 0.95),
    },
    FormType.downtime_mar: {
        "bed": ("12", 0.96), "medication": ("ondansetron", 0.9), "dose": ("4 mg", 0.88), "route": ("IV", 0.95),
        "time_given": ("03:10", 0.9), "initials": ("PN", 0.94), "second_verifier": (None, 0.3),
    },
    FormType.admission_note: {
        "bed": ("12", 0.96), "allergies": ("penicillin (hives)", 0.86), "height_cm": ("178", 0.9),
        "weight_kg": ("92.4", 0.9), "history": ("type 2 diabetes, hypertension", 0.8),
    },
    FormType.verbal_order: {
        "medication": ("ondansetron", 0.9), "dose": ("4 mg", 0.9), "route": ("IV", 0.92),
        "frequency": ("q6h PRN nausea", 0.85), "ordering_provider": ("Dr. Quill (fictional)", 0.8),
        "read_back": ("yes", 0.9), "provider_cosign": ("no", 0.95),
    },
}


def mock_output(form_type: FormType) -> ReaderOutput:
    data = MOCK_READS[form_type]
    return ReaderOutput(fields={k: v for k, (v, _) in data.items()}, confidence={k: c for k, (_, c) in data.items()})


_DOSE = re.compile(r"(\d+(?:\.\d+)?)\s*(milligrams?|mg|micrograms?|mcg|grams?|g|units?|ml|milliliters?|meq)\b", re.I)
_ROUTE = re.compile(r"\b(IV|IM|PO|SC|SQ|SL|subcut\w*|sublingual|intravenous\w*|by mouth)\b", re.I)
_FREQ = re.compile(r"\b(q\s?\d+\s?h\w*(?: PRN[^,.;]*)?|every \d+ hours?(?: as needed[^,.;]*)?|as needed[^,.;]*|PRN[^,.;]*"
                   r"|(?:once|now|stat|daily|BID|TID|QID|nightly)\b)", re.I)
_PROVIDER = re.compile(r"\b(?:Dr\.?|Doctor)\s+([A-Z][\w'-]+)")
_UNITS = {"milligram": "mg", "microgram": "mcg", "gram": "g", "milliliter": "ml", "unit": "units"}


def mock_verbal_order(transcript: str) -> ReaderOutput:
    """Dry-run structuring of a read-back: the canned audio transcript keeps the fixture; any other
    (typed) text is pattern-parsed so the record reflects it. Unparsed fields stay None -> flagged."""
    if transcript.strip() == MOCK_TRANSCRIPT:
        return mock_output(FormType.verbal_order)
    seg = transcript.rsplit(":", 1)[-1]
    found = {k: rx.search(seg) for k, rx in (("dose", _DOSE), ("route", _ROUTE), ("frequency", _FREQ),
                                             ("ordering_provider", _PROVIDER))}
    cut = min([m.start() for m in found.values() if m] + [len(seg)])
    med = re.sub(r"^(please\s+)?(give|start|administer|order)\s+", "", seg[:cut].strip(" ,.;"), flags=re.I) or None
    dose = found["dose"]
    unit = dose and dose.group(2).lower().rstrip("s")
    values = {
        "medication": med,
        "dose": dose and f"{dose.group(1)} {_UNITS.get(unit, unit)}",
        "route": found["route"] and found["route"].group(1),
        "frequency": found["frequency"] and found["frequency"].group(1).strip(),
        "ordering_provider": found["ordering_provider"] and f"Dr. {found['ordering_provider'].group(1)}",
        "read_back": "yes" if re.search(r"read[- ]?back|confirmed", transcript, re.I) else None,
        "provider_cosign": "no",  # a fresh verbal order is never co-signed yet
    }
    return ReaderOutput(fields=values, confidence={k: (0.85 if v else 0.2) for k, v in values.items()})


def _to_reader(text: str) -> ReaderOutput:
    raw = ai.extract_json(text)
    fields = {str(k): (None if v is None else str(v)) for k, v in (raw.get("fields") or {}).items()}
    conf = {str(k): max(0.0, min(1.0, float(v))) for k, v in (raw.get("confidence") or {}).items()}
    return ReaderOutput(fields=fields, confidence={k: conf.get(k, 0.5) for k in fields})


def read_form(image: bytes, image_format: str, form_type: FormType, actor_role: str = "rn") -> ReaderOutput:
    user = json.dumps({"form_type": form_type.value, "expected_fields": list(MOCK_READS[form_type])})
    res = ai.call("form_read", "form_read", user, image=image, image_format=image_format, actor_role=actor_role,
                  mock=lambda: mock_output(form_type).model_dump_json())
    return _to_reader(res.text)


def structure_transcript(transcript: str, actor_role: str = "rn") -> ReaderOutput:
    """Verbal-order read-back transcript -> structured order fields (cheap structured task)."""
    user = json.dumps({"form_type": "verbal_order", "transcript": transcript,
                       "expected_fields": list(MOCK_READS[FormType.verbal_order])})
    res = ai.call("structure", "form_read", user, actor_role=actor_role,
                  mock=lambda: mock_verbal_order(transcript).model_dump_json())
    return _to_reader(res.text)
