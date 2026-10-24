"""Recovery: backload queue grouped like Epic, verify / entered, restored-EHR diff."""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from pydantic import BaseModel

from app import sim_client
from app.api import actor_role
from app.rules import RecoveryContext, out_of_range, recovery_discrepancies
from app.rules.drug_classes import is_high_alert, same_medication
from app.rules.engine import discrepancy
from app.store import get_store
from contracts import (BackloadGroup, CapturedRecord, Discrepancy, FieldFlag, FormType, PatientSnapshot, RecordStatus,
                       Severity)

router = APIRouter(tags=["backload"])


class Verify(BaseModel):
    verified_by: str | None = None


class FieldEdit(BaseModel):
    value: str | None = None


HIGH_ALERT_FORMS = (FormType.downtime_mar, FormType.verbal_order)


def high_alert_medication(record: CapturedRecord) -> str | None:
    """The record's medication value when it is high-alert (two-person verify applies), else None."""
    if record.form_type not in HIGH_ALERT_FORMS:
        return None
    med = next((f.value for f in record.fields if f.name == "medication"), None)
    return med if is_high_alert(med) else None


def _pending_key(record_id: str) -> str:
    return f"verify1:{record_id}"


def _pending(record_id: str) -> dict | None:
    raw = get_store().get_kv(_pending_key(record_id))
    return json.loads(raw) if raw else None


def _same_actor(a: dict, b: dict) -> bool:
    """Two named verifiers are compared by name (two RNs may double-check); otherwise by role."""
    if a.get("verified_by") and b.get("verified_by"):
        return a["verified_by"].strip().lower() == b["verified_by"].strip().lower()
    return a["role"] == b["role"]


def _who(v: dict) -> str:
    return v.get("verified_by") or v["role"]


class Verification(BaseModel):
    record_id: str
    high_alert_medication: str | None
    required: int
    verifications: list[dict]


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


@router.get("/backload/{record_id}/verification", response_model=Verification)
def verification(record_id: str, role: str = Depends(actor_role)) -> Verification:
    """How many verifications this record needs and which are recorded (high-alert meds need two)."""
    record = _record(record_id)
    med = high_alert_medication(record)
    done: list[dict] = []
    if record.status in (RecordStatus.verified, RecordStatus.entered):
        done = [{"verified_by": record.verified_by, "at": record.verified_at.isoformat() if record.verified_at else None}]
    elif (first := _pending(record_id)) is not None:
        done = [first]
    get_store().audit(role, "backload.verification_view", "captured_record", record_id)
    return Verification(record_id=record_id, high_alert_medication=med, required=2 if med else 1, verifications=done)


@router.post("/backload/{record_id}/verify", response_model=CapturedRecord)
def verify(record_id: str, response: Response, body: Verify | None = Body(default=None),
           role: str = Depends(actor_role)) -> CapturedRecord:
    """Human verification. A record with a high-alert medication field needs two verify calls by different
    people (README safety section): the first is held pending, a same-actor second call is rejected (409)."""
    record = _record(record_id)
    if record.status != RecordStatus.needs_review:
        raise HTTPException(409, f"record is already {record.status.value}")
    store = get_store()
    name = (body.verified_by or "").strip() if body else ""
    me = {"role": role, "verified_by": name or None, "at": sim_client.now().isoformat()}
    med = high_alert_medication(record)
    first = _pending(record_id) if med else None
    if med and first is None:
        store.set_kv(_pending_key(record_id), json.dumps(me))
        store.audit(role, "backload.verify", "captured_record", record_id,
                    {"verified_by": _who(me), "role": role, "step": 1, "required": 2, "high_alert_medication": med})
        response.headers["X-Verifications"] = "1/2"
        return record
    if med and _same_actor(first, me):
        store.audit(role, "backload.verify_rejected", "captured_record", record_id,
                    {"reason": "same verifier", "first": first, "attempt": me, "high_alert_medication": med})
        raise HTTPException(409, f"high-alert medication ({med}): the second verification must be by a different "
                                 f"person than {_who(first)}; switch role or verifier")
    verified_by = f"{_who(first)} + {_who(me)}" if med else _who(me)
    updated = record.model_copy(update={"status": RecordStatus.verified, "verified_by": verified_by,
                                        "verified_at": datetime.fromisoformat(me["at"])})
    store.save_record(updated)
    store.set_kv(_pending_key(record_id), None)
    store.audit(role, "backload.verify", "captured_record", record_id,
                {"verified_by": _who(me), "role": role, "step": 2 if med else 1, "required": 2 if med else 1,
                 **({"high_alert_medication": med, "first_verifier": first} if med else {})})
    response.headers["X-Verifications"] = "2/2" if med else "1/1"
    return updated


@router.patch("/backload/{record_id}/fields/{name}", response_model=CapturedRecord)
def edit_field(record_id: str, name: str, body: FieldEdit, role: str = Depends(actor_role)) -> CapturedRecord:
    """Correct a read value before verification. The audit event keeps before and after; the original
    reader outputs stay in `reads`. Any pending first verification is reset (the content changed)."""
    record = _record(record_id)
    if record.status != RecordStatus.needs_review:
        raise HTTPException(409, "fields can only be edited before the record is verified")
    old = next((f for f in record.fields if f.name == name), None)
    if old is None:
        raise HTTPException(404, f"record {record_id} has no field {name}")
    after = (body.value or "").strip() or None
    if after == old.value:
        return record
    flag = FieldFlag.unreadable if after is None else (FieldFlag.out_of_range if out_of_range(name, after) else None)
    new = old.model_copy(update={"value": after, "flag": flag, "confidence": 1.0 if after is not None else 0.0})
    updated = record.model_copy(update={"fields": [new if f.name == name else f for f in record.fields]})
    store = get_store()
    store.save_record(updated)
    reset = _pending(record_id) is not None
    if reset:
        store.set_kv(_pending_key(record_id), None)
    store.audit(role, "backload.edit", "captured_record", record_id,
                {"field": name, "before": old.value, "after": after,
                 "before_flag": old.flag.value if old.flag else None, "after_flag": flag.value if flag else None,
                 "verification_reset": reset})
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
