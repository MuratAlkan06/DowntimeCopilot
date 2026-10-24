"""Audit log (append only), cost per outage-hour, and cloud sync (docs/decisions.md S10)."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends

from app import sim_client
from app.api import actor_role
from app.store import get_store
from app.sync import worker as sync_worker
from contracts import AuditEvent

router = APIRouter(tags=["audit"])


@router.get("/audit", response_model=list[AuditEvent])
def audit(limit: int = 200, role: str = Depends(actor_role)) -> list[AuditEvent]:
    store = get_store()
    store.audit(role, "audit.view", "audit")
    return store.list_audit(limit)


@router.get("/costs")
def costs(role: str = Depends(actor_role)) -> dict:
    store = get_store()
    calls = store.list_costs()
    started = store.get_kv("outage_started_at")
    hours = None
    if started:
        hours = max((sim_client.now() - datetime.fromisoformat(started)).total_seconds() / 3600, 0.0)
    total = round(sum(c["usd"] for c in calls), 6)
    models: dict[str, dict] = {}
    for c in calls:
        m = models.setdefault(c["model_id"], {"calls": 0, "tokens_in": 0, "tokens_out": 0, "usd": 0.0})
        m["calls"] += 1
        m["tokens_in"] += c["tokens_in"]
        m["tokens_out"] += c["tokens_out"]
        m["usd"] = round(m["usd"] + c["usd"], 6)
    store.audit(role, "costs.view", "costs")
    return {
        "calls": calls,
        "models": models,
        "total_usd": total,
        "tokens_in": sum(c["tokens_in"] for c in calls),
        "tokens_out": sum(c["tokens_out"] for c in calls),
        "outage_hours": round(hours, 3) if hours is not None else None,
        "usd_per_outage_hour": round(total / hours, 6) if hours else None,
        "dry_run": any(c["dry_run"] for c in calls),
    }


@router.post("/sync")
def sync(role: str = Depends(actor_role)) -> dict:
    """Drain the outbox now (the background worker does the same). Never raises on a sync failure."""
    pushed, error = sync_worker.drain_all()
    st = sync_worker.status()
    get_store().audit(role, "sync.push", "audit", st["mode"], {"pushed": pushed, "error": error})
    return {"mirror": st["mode"], "pushed": pushed, "pending": st["pending"], "error": error}


@router.get("/sync/status")
def sync_status() -> dict:
    """Non-contract. {mode, mirrored, pending, last_success_ts, last_error}; same keys in DRY_RUN. Not audited:
    the web chip polls it, and an audit row per poll would itself be a pending item."""
    return sync_worker.status()
