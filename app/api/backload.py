"""Recovery: backload queue grouped like Epic, verify / entered, restored-EHR diff."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from app import sim_client
from app.api import actor_role
from app.rules import RecoveryContext, recovery_discrepancies
from app.rules.drug_classes import same_medication
from app.rules.engine import discrepancy
from app.store import get_store
from contracts import BackloadGroup, CapturedRecord, Discrepancy, PatientSnapshot, RecordStatus, Severity

router = APIRouter(tags=["backload"])


class Verify(BaseModel):
    verified_by: str | None = None


@router.get("/backload", response_model=dict[BackloadGroup, list[CapturedRecord]])
def backload(role: str = Depends(actor_role)) -> dict[BackloadGroup, list[CapturedRecord]]:
    store = get_store()
    groups: dict[BackloadGroup, list[CapturedRecord]] = {g: [] for g in BackloadGroup}
    for r in store.list_records():
        groups[r.backload_group].append(r)
    store.audit(role, "backload.view", "backload", None, {g.value: len(v) for g, v in groups.items()})
    return groups


def _record(record_id: str) -> CapturedRecord:
    record = get_store().get_record(record_id)
    if record is None:
        raise HTTPException(404, f"unknown record {record_id}")
    return record


@router.post("/backload/{record_id}/verify", response_model=CapturedRecord)
def verify(record_id: str, body: Verify | None = Body(default=None), role: str = Depends(actor_role)) -> CapturedRecord:
    # TODO(role 4): high-alert medication fields need two distinct verifiers (README safety section).
    record = _record(record_id)
    updated = record.model_copy(update={"status": RecordStatus.verified,
                                        "verified_by": (body.verified_by if body and body.verified_by else role),
                                        "verified_at": sim_client.now()})
    store = get_store()
    store.save_record(updated)
    store.audit(role, "backload.verify", "captured_record", record_id, {"verified_by": updated.verified_by})
    return updated


@router.post("/backload/{record_id}/entered", response_model=CapturedRecord)
def entered(record_id: str, role: str = Depends(actor_role)) -> CapturedRecord:
    record = _record(record_id)
    if record.status != RecordStatus.verified:
        raise HTTPException(409, "a human must verify the record before it is marked entered")
    updated = record.model_copy(update={"status": RecordStatus.entered, "entered_at": sim_client.now()})
    store = get_store()
    store.save_record(updated)
    store.audit(role, "backload.entered", "captured_record", record_id)
    return updated


def diff_restored(pre: dict[str, PatientSnapshot], restored: dict[str, PatientSnapshot]) -> list[Discrepancy]:
    """Restored EHR vs last good snapshot. IDs DIFF-* are diff findings, not README rules."""
    out = []
    for pid, before in pre.items():
        after = restored.get(pid)
        if after is None:
            out.append(discrepancy("DIFF-ADT", pid, None, f"{before.bed} {before.name} missing from restored EHR "
                                   "(discharge or transfer?); confirm against the census sheet", Severity.warning))
            continue
        if after.bed != before.bed:
            out.append(discrepancy("DIFF-ADT", pid, None, f"Transferred {before.bed} -> {after.bed} during downtime; "
                                   "confirm against the census sheet", Severity.info))
        for m in after.medications:
            if not any(same_medication(m.name, b.name) for b in before.medications):
                out.append(discrepancy("DIFF-ORDER", pid, None, f"Order {m.name} {m.dose} {m.route} {m.frequency} "
                                       "is in the restored EHR but not the pre-downtime snapshot; check paper orders",
                                       Severity.warning))
    return out


@router.post("/recovery/restore", response_model=list[Discrepancy])
def recovery_restore(role: str = Depends(actor_role)) -> list[Discrepancy]:
    store = get_store()
    pre = {p.patient_id: p for p in store.latest_snapshot("pre")}
    if not pre:
        raise HTTPException(409, "no pre-downtime snapshot stored")
    try:
        restored_list, changes = sim_client.restore()
    except sim_client.SimUnavailable as exc:
        store.audit(role, "recovery.restore_failed", "unit", None, {"error": str(exc)})
        raise HTTPException(503, str(exc)) from exc
    store.save_snapshot(restored_list, "restored")
    restored = {p.patient_id: p for p in restored_list}
    found = diff_restored(pre, restored) + recovery_discrepancies(RecoveryContext(pre, store.list_records()))
    store.save_discrepancies(found)
    store.set_kv("phase", "recovery")
    store.set_kv("restored_at", sim_client.now().isoformat())
    store.audit(role, "recovery.restore", "unit", None,
                {"discrepancies": len(found), "simulator_changes": changes})
    return store.list_discrepancies()


@router.get("/discrepancies", response_model=list[Discrepancy])
def discrepancies(role: str = Depends(actor_role)) -> list[Discrepancy]:
    store = get_store()
    store.audit(role, "discrepancy.list", "discrepancy")
    return store.list_discrepancies()
