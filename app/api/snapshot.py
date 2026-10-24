"""Before: pull the BCA feed, store the snapshot locally, report status."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from app import sim_client
from app.api import actor_role
from app.store import get_store
from contracts import PatientSnapshot

router = APIRouter(tags=["snapshot"])

# README "Before": a snapshot every 15 minutes of UNIT (simulated) time, not wall-clock time.
SNAPSHOT_EVERY = timedelta(minutes=15)


def should_pull(phase: str, sim_now: datetime, newest_pre: datetime | None, *, ehr_up: bool = True,
                every: timedelta = SNAPSHOT_EVERY) -> bool:
    """Scheduler decision (pure). Only in the "before" phase with the EHR up, and only once `every`
    simulated minutes have passed since the newest pre-outage snapshot. Never pulling in any other
    phase is what keeps the pre-outage baseline that recovery and the digest diff against."""
    if phase != "before" or not ehr_up:
        return False
    return newest_pre is None or sim_now - newest_pre >= every


def scheduled_pull(role: str = "scheduler") -> bool:
    """One scheduler tick: read the simulator clock, pull only if should_pull(). True if it pulled."""
    store = get_store()
    status = sim_client.clock()
    newest = max((p.snapshot_time for p in store.latest_snapshot("pre")), default=None)
    if not should_pull(store.get_kv("phase", "before") or "before", datetime.fromisoformat(status["now"]), newest,
                       ehr_up=not (status.get("outage") or status.get("restored"))):
        return False
    pull_and_store(role)
    return True


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


@router.get("/patients", response_model=list[PatientSnapshot])
def patients_list(role: str = Depends(actor_role)) -> list[PatientSnapshot]:
    store = get_store()
    store.audit(role, "patient.list", "patient")
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
