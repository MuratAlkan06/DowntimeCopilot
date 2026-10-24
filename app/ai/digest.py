"""Shift digest (Bedrock job 4): SBAR handoff for the oncoming shift from snapshots, captured
records, and open discrepancies. Source paths point at snapshot fields or record IDs."""
from __future__ import annotations

import json
from datetime import datetime

from app.ai import client as ai
from app.ai.guardrail import get_guardrail
from app.store import utcnow
from contracts import (CapturedRecord, Cost, Discrepancy, PatientDigest, PatientSnapshot, Sbar, ShiftDigest,
                       ShiftWindow)


def _canned(patients: list[PatientSnapshot], records: list[CapturedRecord], discs: list[Discrepancy]) -> dict:
    open_d = [d for d in discs if d.status == "open"]
    review = [r for r in records if r.status == "needs_review"]
    per = []
    for p in patients:
        recs = [r for r in records if r.patient_id == p.patient_id]
        ds = [d for d in open_d if d.patient_id == p.patient_id]
        per.append({"patient_id": p.patient_id, "sbar": {
            "situation": [{"text": f"{p.bed}: {p.name}, {p.admitting_dx}", "source_path": f"snapshot.{p.patient_id}.admitting_dx"}],
            "background": [{"text": f"{r.form_type.value} captured {r.captured_at:%H:%M}, status {r.status.value}",
                            "source_path": f"captured.{r.record_id}"} for r in recs],
            "assessment": [{"text": f"{d.rule_id}: {d.description}", "source_path": f"discrepancy.{d.discrepancy_id}"} for d in ds],
            "recommendation": [{"text": f"Verify and backload {r.form_type.value}", "source_path": f"captured.{r.record_id}"}
                               for r in recs if r.status != "entered"],
        }})
    summary = (f"{len(patients)} patients; {len(records)} paper records captured "
               f"({sum(r.status == 'entered' for r in records)} entered, {len(review)} awaiting review); "
               f"{len(open_d)} open discrepancies.")
    items = [f"{d.rule_id} {d.patient_id}: {d.description}" for d in open_d] + \
            [f"Review {r.form_type.value} {r.record_id}" for r in review]
    return {"unit_summary": summary, "per_patient": per, "open_items": items}


def build_digest(patients: list[PatientSnapshot], records: list[CapturedRecord], discs: list[Discrepancy],
                 start: datetime, end: datetime, actor_role: str = "system") -> ShiftDigest:
    canned = _canned(patients, records, discs)
    user = json.dumps({"patients": [json.loads(p.model_dump_json()) for p in patients],
                       "records": [json.loads(r.model_dump_json()) for r in records],
                       "discrepancies": [json.loads(d.model_dump_json()) for d in discs]})
    res = ai.call("digest", "digest", user, mock=lambda: json.dumps(canned), actor_role=actor_role)
    try:
        raw = ai.extract_json(res.text)
        per = [PatientDigest(patient_id=str(x["patient_id"]), sbar=Sbar.model_validate(x["sbar"]))
               for x in raw.get("per_patient", [])]
        summary, items = str(raw.get("unit_summary", "")), [str(i) for i in raw.get("open_items", [])]
    except (ValueError, KeyError, TypeError):
        per = [PatientDigest.model_validate(x) for x in canned["per_patient"]]
        summary, items = canned["unit_summary"] + " (deterministic digest: model output unparseable)", canned["open_items"]
    get_guardrail().check(summary, grounding=user)  # TODO: surface digest guardrail status (no field in contract)
    return ShiftDigest(shift_window=ShiftWindow(start=start, end=end), generated_at=utcnow(), model_id=res.model_id,
                       unit_summary=summary, per_patient=per, open_items=items,
                       cost=Cost(tokens_in=res.tokens_in, tokens_out=res.tokens_out, usd=res.usd))
