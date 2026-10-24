"""Incident report (non-contract): GET /incident -> deterministic audit timeline + two-paragraph summary."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app import clock
from app.ai import incident
from app.api import actor_role
from app.store import get_store

router = APIRouter(tags=["incident"])


@router.get("/incident")
def incident_report(role: str = Depends(actor_role)) -> dict:
    """Shape (stable, outside the frozen contracts): {generated_at, outage_started_at, restored_at, model_id,
    summary[2], guardrail{status, reasons}, cost{tokens_in, tokens_out, usd}, counts{...},
    timeline[{seq, ts, role, action, event, patient, detail}]}."""
    store = get_store()
    patients = store.latest_snapshot("restored") or store.latest_snapshot("pre")
    rows = incident.timeline(store.audit_log(), store.list_records(), store.list_discrepancies(), patients)
    summary, model_id, verdict, cost = incident.summarize(rows, {p.patient_id: p.bed for p in patients},
                                                          actor_role=role)
    store.audit(role, "incident.view", "incident", None,
                {"rows": len(rows), "model_id": model_id, "guardrail": verdict.status})
    return {"generated_at": clock.unit_now().isoformat(), "outage_started_at": store.get_kv("outage_started_at"),
            "restored_at": store.get_kv("restored_at"), "model_id": model_id, "summary": summary,
            "guardrail": verdict.model_dump(), "cost": cost.model_dump(), "counts": incident.counts(rows),
            "timeline": rows}
