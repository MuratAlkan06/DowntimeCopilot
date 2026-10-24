"""Shift digest for the oncoming team."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends

from app import sim_client
from app.ai.digest import build_digest
from app.api import actor_role
from app.store import get_store
from contracts import ShiftDigest

router = APIRouter(tags=["digest"])


@router.get("/digest", response_model=ShiftDigest)
def digest(role: str = Depends(actor_role)) -> ShiftDigest:
    store = get_store()
    patients = store.latest_snapshot("restored") or store.latest_snapshot("pre")
    end = sim_client.now()
    started = store.get_kv("outage_started_at")
    start = datetime.fromisoformat(started) if started else min((p.snapshot_time for p in patients), default=end)
    result = build_digest(patients, store.list_records(), store.list_discrepancies(), start, max(start, end),
                          actor_role=role)
    store.audit(role, "digest.generate", "digest", None, {"model_id": result.model_id, "patients": len(result.per_patient)})
    return result
