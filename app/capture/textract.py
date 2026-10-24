"""Textract adapter: second, independent read of a photographed form.

Real path (S8): AnalyzeDocument with FeatureTypes FORMS + QUERIES. Call shape verified against the
installed botocore service model (1.43.x): AnalyzeDocument(Document{Bytes|S3Object}, FeatureTypes,
QueriesConfig{Queries[{Text, Alias, Pages}]}, HumanLoopConfig, AdaptersConfig) -> Blocks[]. Query
Text/Alias must match the model's ASCII QueryInput pattern (1..200 chars).

- One query per template field (layout map schema), Alias = field name, at most MAX_QUERIES_PER_PAGE.
  Over the cap, PRIORITY decides which fields are asked; the rest still map from FORMS key-values.
- Mapping: QUERY -> ANSWER -> QUERY_RESULT by Alias first; else a KEY_VALUE_SET whose key text
  fuzzy-matches the field. Confidence 0..100 -> 0..1. Answer geometry (normalized BoundingBox) is
  returned as the Textract-side crop box.
- HumanLoopConfig is never sent (no human review loop sees a patient form).
- AccessDenied: logged once, Textract skipped for the rest of the process; the record gets no
  Textract read, so agreement is 'single'. No retry.
"""
from __future__ import annotations

import difflib
import logging
import re

from app import config
from app.capture.layouts import layout
from contracts import Crop, FormType, ReaderOutput

log = logging.getLogger(__name__)

# Canned dry-run read; differs from the Bedrock mock only on resp_rate (README demo step 3).
MOCK: dict[FormType, dict[str, tuple[str | None, float]]] = {
    FormType.vitals_flowsheet: {
        "bed": ("12", 0.99), "time": ("02:55", 0.95), "temp_c": ("37.8", 0.94), "heart_rate": ("104", 0.93),
        "resp_rate": ("22", 0.58), "bp_systolic": ("148", 0.92), "bp_diastolic": ("86", 0.9),
        "spo2": ("89", 0.95), "pain_score": (None, 0.12), "initials": ("PN", 0.9),
    },
    FormType.downtime_mar: {
        "bed": ("12", 0.98), "medication": ("heparin", 0.88), "dose": ("5000 units", 0.9), "route": ("SC", 0.96),
        "time_given": ("02:50", 0.92), "initials": ("PN", 0.9), "second_verifier": (None, 0.2),
    },
    FormType.admission_note: {
        "bed": ("12", 0.97), "allergies": ("penicillin (hives)", 0.8), "height_cm": ("178", 0.93),
        "weight_kg": ("92.4", 0.91), "history": ("type 2 diabetes, hypertension", 0.75),
    },
}

MAX_QUERIES_PER_PAGE = 15  # AnalyzeDocument (synchronous) queries-per-page quota

# Natural-language query per field (ASCII only: the QueryInput pattern rejects e.g. the degree sign).
QUERY_TEXT: dict[str, str] = {
    "bed": "What is the bed number?",
    "time": "What time were the vital signs taken?",
    "initials": "What are the nurse's initials?",
    "temp_c": "What is the temperature in degrees Celsius?",
    "heart_rate": "What is the heart rate per minute?",
    "resp_rate": "What is the respiratory rate per minute?",
    "bp_systolic": "What is the systolic blood pressure?",
    "bp_diastolic": "What is the diastolic blood pressure?",
    "spo2": "What is the SpO2 percentage?",
    "pain_score": "What is the pain score?",
    "medication": "What medication was given?",
    "dose": "What dose and unit were given?",
    "route": "What was the route of administration?",
    "time_given": "What time was the medication given?",
    "second_verifier": "Who is the second verifier?",
    "allergies": "What allergies and reactions are listed?",
    "height_cm": "What is the height in cm?",
    "weight_kg": "What is the weight in kg?",
    "history": "What is the past medical history?",
}

# Query priority when a template has more fields than the cap (docs/decisions.md S8): medication
# administration, then vitals, then allergies, then patient identity and timing, then anthropometrics
# and history, then admin sign-off fields. Fields not listed come last, in template order.
PRIORITY: tuple[str, ...] = (
    "medication", "dose", "route",
    "spo2", "resp_rate", "heart_rate", "bp_systolic", "bp_diastolic", "temp_c", "pain_score",
    "allergies",
    "bed", "time_given", "time",
    "weight_kg", "height_cm", "history",
    "second_verifier", "initials",
)

KV_MATCH_MIN = 0.8  # fuzzy key-label score needed to map a FORMS key-value to a field
LABEL_ALIASES: dict[str, tuple[str, ...]] = {"temp_c": ("temperature",), "resp_rate": ("respiratory rate",)}

_denied = False  # set on the first AccessDenied; the process then stays on the single read


def _ascii(text: str) -> str:
    return re.sub(r"[^\x20-\x7e]", "", text).strip()[:200] or "value"


def query_text(name: str) -> str:
    return QUERY_TEXT.get(name) or _ascii(f"What is the {config.FIELD_LABELS.get(name, name.replace('_', ' '))}?")


def select_fields(names: list[str], cap: int = MAX_QUERIES_PER_PAGE) -> list[str]:
    """The fields that get a query: all of them up to `cap`, else the `cap` highest-priority ones."""
    rank = {n: i for i, n in enumerate(PRIORITY)}
    ordered = sorted(names, key=lambda n: (rank.get(n, len(PRIORITY)), names.index(n)))
    return ordered[:cap]


def queries_config(names: list[str]) -> dict:
    return {"Queries": [{"Text": query_text(n), "Alias": n, "Pages": ["1"]} for n in select_fields(names)]}


def _conf(value) -> float:
    """Textract 0..100 -> 0..1, clamped."""
    try:
        return round(min(max(float(value) / 100.0, 0.0), 1.0), 3)
    except (TypeError, ValueError):
        return 0.0


def _box(block: dict) -> Crop | None:
    bb = (block.get("Geometry") or {}).get("BoundingBox")
    if not bb:
        return None
    try:
        left, top = min(max(float(bb["Left"]), 0.0), 1.0), min(max(float(bb["Top"]), 0.0), 1.0)
        width, height = min(max(float(bb["Width"]), 0.0), 1.0 - left), min(max(float(bb["Height"]), 0.0), 1.0 - top)
    except (KeyError, TypeError, ValueError):
        return None
    return Crop(left=left, top=top, width=width, height=height) if width > 0 and height > 0 else None


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _variants(text: str) -> set[str]:
    """Normalized text with and without parenthetical parts ("Height (cm)" -> "height cm", "height")."""
    return {v for v in (_norm(text), _norm(re.sub(r"\([^)]*\)", " ", text))) if v}


def _label_score(key_text: str, name: str) -> float:
    keys = _variants(key_text)
    cands = _variants(name.replace("_", " ")) | _variants(config.FIELD_LABELS.get(name, ""))
    cands |= {_norm(a) for a in LABEL_ALIASES.get(name, ())}
    best = 0.0
    for k in keys:
        for c in cands:
            best = max(best, difflib.SequenceMatcher(None, k, c).ratio())
            if set(c.split()) <= set(k.split()):
                best = max(best, 0.85)  # every word of the field label appears in the printed key
    return best


def _children_text(block: dict, by_id: dict[str, dict]) -> str:
    words = []
    for rel in block.get("Relationships") or []:
        if rel.get("Type") == "CHILD":
            words += [by_id[i].get("Text", "") for i in rel.get("Ids", []) if by_id.get(i, {}).get("BlockType") == "WORD"]
    return " ".join(w for w in words if w).strip()


def map_blocks(blocks: list[dict], names: list[str]) -> tuple[ReaderOutput, dict[str, Crop]]:
    """AnalyzeDocument Blocks -> (ReaderOutput over the template fields `names`, field -> answer box)."""
    by_id = {b["Id"]: b for b in blocks if "Id" in b}
    fields: dict[str, str | None] = {}
    conf: dict[str, float] = {}
    boxes: dict[str, Crop] = {}

    for q in blocks:  # 1. QUERY -> ANSWER -> QUERY_RESULT, by Alias; best-confidence answer wins
        alias = (q.get("Query") or {}).get("Alias")
        if q.get("BlockType") != "QUERY" or alias not in names:
            continue
        answers = [by_id[i] for rel in q.get("Relationships") or [] if rel.get("Type") == "ANSWER"
                   for i in rel.get("Ids", []) if by_id.get(i, {}).get("BlockType") == "QUERY_RESULT"]
        answers = [a for a in answers if (a.get("Text") or "").strip()]
        if answers:
            top = max(answers, key=lambda a: a.get("Confidence", 0.0))
            fields[alias], conf[alias] = top["Text"].strip(), _conf(top.get("Confidence"))
            if (box := _box(top)) is not None:
                boxes[alias] = box

    pairs = []  # 2. FORMS key-values for fields no query answered: best fuzzy label match, one-to-one
    for k in blocks:
        if k.get("BlockType") != "KEY_VALUE_SET" or "KEY" not in (k.get("EntityTypes") or []):
            continue
        key_text = _children_text(k, by_id)
        value = next((by_id[i] for rel in k.get("Relationships") or [] if rel.get("Type") == "VALUE"
                      for i in rel.get("Ids", []) if i in by_id), None)
        if not key_text or value is None:
            continue
        for name in names:
            if name not in fields and (score := _label_score(key_text, name)) >= KV_MATCH_MIN:
                pairs.append((score, k["Id"], name, value))
    used: set[str] = set()
    for _score, key_id, name, value in sorted(pairs, key=lambda p: -p[0]):
        if key_id in used or name in fields:
            continue
        used.add(key_id)
        text = _children_text(value, by_id)
        fields[name], conf[name] = text or None, _conf(value.get("Confidence"))
        if text and (box := _box(value)) is not None:
            boxes[name] = box

    for name in names:  # 3. asked but not read: no value (agreement.merge never guesses)
        if name not in fields:
            fields[name], conf[name] = None, 0.0
    return ReaderOutput(fields=fields, confidence=conf), boxes


def _client():
    import boto3
    return boto3.client("textract", region_name=config.aws_region())


def _is_access_denied(exc: Exception) -> bool:
    code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")
    return code in ("AccessDeniedException", "AccessDenied")


def read_with_boxes(image: bytes, form_type: FormType) -> tuple[ReaderOutput | None, dict[str, Crop]]:
    """(read, field -> normalized answer box). (None, {}) when Textract is off, denied, or the form type
    has no image; agreement is then 'single'."""
    global _denied
    if form_type == FormType.verbal_order or not config.textract_enabled() or _denied:
        return None, {}
    if config.dry_run():
        data = MOCK[form_type]
        return ReaderOutput(fields={k: v for k, (v, _) in data.items()}, confidence={k: c for k, (_, c) in data.items()}), {}
    from botocore.exceptions import ClientError

    names = list(layout(form_type)) or list(MOCK[form_type])
    try:
        resp = _client().analyze_document(Document={"Bytes": image}, FeatureTypes=["FORMS", "QUERIES"],
                                          QueriesConfig=queries_config(names))
    except ClientError as exc:
        if not _is_access_denied(exc):
            raise
        _denied = True
        log.warning("Textract AnalyzeDocument denied (%s); second read off for this process, single read only",
                    exc.response["Error"]["Code"])
        return None, {}
    return map_blocks(resp.get("Blocks", []), names)


def read(image: bytes, form_type: FormType) -> ReaderOutput | None:
    """None when Textract is disabled, denied, or the form type has no image (agreement becomes 'single')."""
    return read_with_boxes(image, form_type)[0]
