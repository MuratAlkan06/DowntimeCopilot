"""Two-reader agreement (README "Bedrock does four jobs" 2): per field agree / disagree / single,
with flags unreadable / disagreement / out_of_range.

How a field is represented in a ReaderOutput (docs/decisions.md, 2026-10-24 ruling):
- reader did not run (TEXTRACT_ENABLED=0, AccessDenied skip, verbal order): the ReaderOutput is None.
  A key the reader did not report at all is treated the same way for that field (no evidence either way).
- reader ran and found the field unreadable/empty: the key is present with value None, or with a value
  below UNREADABLE_BELOW confidence (textract.map_blocks fills every asked field; forms._parse keeps nulls).
Ran-and-unreadable vs a confident value is a disagreement, never a single read: the value stays pre-filled
but carries the disagreement flag, and `reads` keeps both sides for the reviewer."""
from __future__ import annotations

from app.rules import out_of_range
from contracts import Agreement, CapturedField, Crop, FieldFlag, ReaderOutput

UNREADABLE_BELOW = 0.4


def _same(a: str, b: str) -> bool:
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


def merge(textract: ReaderOutput | None, bedrock: ReaderOutput | None,
          boxes: dict[str, Crop] | None = None) -> list[CapturedField]:
    """`boxes`: field -> crop box on the source image (app/capture/crops.py chooses model box or layout map)."""
    names: list[str] = []
    for r in (bedrock, textract):
        for n in (r.fields if r else {}):
            if n not in names:
                names.append(n)
    out = []
    for name in names:
        # Only readers that ran AND reported this field (key present, even if None) take part.
        reads = [(r.fields.get(name), r.confidence.get(name, 0.0)) for r in (textract, bedrock)
                 if r is not None and name in r.fields]
        usable = [(v, c) for v, c in reads if v is not None and c >= UNREADABLE_BELOW]
        flag: FieldFlag | None = None
        if len(reads) == 2 and len(usable) == 2:
            (v1, c1), (v2, c2) = usable
            if _same(v1, v2):
                value, conf, agreement = v1, max(c1, c2), Agreement.agree
            else:
                value, conf = (v1, c1) if c1 >= c2 else (v2, c2)
                conf, agreement, flag = min(c1, c2), Agreement.disagree, FieldFlag.disagreement
        elif len(reads) == 2 and len(usable) == 1:
            # One reader ran and could not read the field, the other read a value (live: scribbled-out pain
            # score, Nova null vs Textract "NW" @ 1.00). Never a silent single read: flag it for a human.
            (value, c_val), = usable
            (other, c_other), = [r for r in reads if r != usable[0]]  # differs in value (None) or confidence
            if other is not None and _same(other, value):
                conf, agreement = c_val, Agreement.single  # a low-confidence read of the same value is no dissent
            else:
                conf, agreement, flag = min(c_val, c_other), Agreement.disagree, FieldFlag.disagreement
        elif usable:
            value, conf = max(usable, key=lambda vc: vc[1])
            agreement = Agreement.single
        else:
            value, conf = None, max((c for _, c in reads), default=0.0)
            agreement, flag = Agreement.single, FieldFlag.unreadable  # never claim agreement on nothing
        if flag is None and out_of_range(name, value):
            flag = FieldFlag.out_of_range
        if flag == FieldFlag.unreadable:
            value = None  # never guessed: an unreadable field carries no value on any path
        out.append(CapturedField(name=name, value=value, confidence=round(conf, 3), agreement=agreement,
                                 crop=(boxes or {}).get(name), flag=flag))
    return out
