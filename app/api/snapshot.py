"""Before: pull the BCA feed, store the snapshot locally, report status."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app import sim_client
from app.api import actor_role
from app.store import get_store
from contracts import PatientSnapshot

router = APIRouter(tags=["snapshot"])


def pull_and_store(role: str = "system") -> list[PatientSnapshot]:
    store = get_store()
    try:
        patients = sim_client.pull_snapshot()
    except sim_client.SimUnavailable as exc:
        store.audit(role, "snapshot.pull_failed", "snapshot", None, {"error": str(exc)})
        raise
    pull_id = store.save_snapshot(patients, "pre")
    store.audit(role, "snapshot.pull", "snapshot", str(pull_id), {"patients": len(patients)})
    return patients


@router.post("/snapshot/pull", response_model=list[PatientSnapshot])
def snapshot_pull(role: str = Depends(actor_role)) -> list[PatientSnapshot]:
    try:
        return pull_and_store(role)
    except sim_client.SimUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/snapshot", response_model=list[PatientSnapshot])
def snapshot_latest(role: str = Depends(actor_role)) -> list[PatientSnapshot]:
    store = get_store()
    store.audit(role, "snapshot.view", "snapshot")
    return store.latest_snapshot()


@router.get("/status")
def status(role: str = Depends(actor_role)) -> dict:
    store = get_store()
    snap = store.latest_snapshot()
    now = sim_client.now()
    snap_time = max((p.snapshot_time for p in snap), default=None)
    store.audit(role, "status.view", "status")
    return {
        "phase": store.get_kv("phase", "before"),
        "now": now.isoformat(),
        "snapshot_time": snap_time.isoformat() if snap_time else None,
        "snapshot_age_minutes": int((now - snap_time).total_seconds() // 60) if snap_time else None,
        "patients": len(snap),
        "outage_started_at": store.get_kv("outage_started_at"),
    }
