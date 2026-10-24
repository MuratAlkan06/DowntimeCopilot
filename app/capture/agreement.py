"""Two-reader agreement (README "Bedrock does four jobs" 2): per field agree / disagree / single,
with flags unreadable / disagreement / out_of_range."""
from __future__ import annotations

from app.rules import out_of_range
from contracts import Agreement, CapturedField, Crop, FieldFlag, ReaderOutput

UNREADABLE_BELOW = 0.4


def _same(a: str, b: str) -> bool:
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


def merge(textract: ReaderOutput | None, bedrock: ReaderOutput | None) -> list[CapturedField]:
    names: list[str] = []
    for r in (bedrock, textract):
        for n in (r.fields if r else {}):
            if n not in names:
                names.append(n)
    out = []
    for i, name in enumerate(names):
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
        elif usable:
            value, conf = max(usable, key=lambda vc: vc[1])
            agreement = Agreement.single
        else:
            value, conf = None, max((c for _, c in reads), default=0.0)
            agreement, flag = Agreement.single, FieldFlag.unreadable  # never claim agreement on nothing
        if flag is None and out_of_range(name, value):
            flag = FieldFlag.out_of_range
        # TODO(role 4): real crops from Textract BoundingBox; placeholder row boxes for the crop viewer.
        crop = Crop(left=0.08, top=min(0.92, 0.08 + i * 0.08), width=0.84, height=0.07)
        out.append(CapturedField(name=name, value=value, confidence=round(conf, 3), agreement=agreement,
                                 crop=crop, flag=flag))
    return out
