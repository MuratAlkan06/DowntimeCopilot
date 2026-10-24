"""Supervisor command center (S14, docs/decisions.md): a pure, deterministic per-unit roll-up of what the
supervisor has to act on during a downtime. No model calls, no network, no store and no clock of its own: the
endpoint (app/api/command_center.py) gathers each unit's inputs and injects the unit clock. Same inputs and the
same clock give the same view, and the shape is a list of units, so N units render the same way as one.

Panels, per unit (each carries a `count`):
  overdue_high_alert      high-alert medications in the last good snapshot whose next_due is strictly before
                          clock() (due == now is not yet overdue; one second later it is) and that have no
                          administration record: no non-rejected downtime MAR capture for that patient and
                          medication timed after the dose the EHR last charted (last_given).
  open_discrepancies      discrepancies still open, counted by severity (critical, warning, info); items are
                          listed most severe first.
  backload_counts         captured records per backload group (the grouping GET /backload serves), the total,
                          and how many still need review.
  critical_labs_awaiting  pending labs the snapshot flags critical. The frozen PatientSnapshot contract has no
                          flag field, so the flag is a trailing "(critical)" marker on the pending_labs entry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from app.rules.drug_classes import same_medication
from app.rules.engine import fields, record_time
from contracts import (BackloadGroup, CapturedRecord, Discrepancy, DiscrepancyStatus, FormType, Medication,
                       PatientSnapshot, RecordStatus, Severity)

SEVERITIES: tuple[Severity, ...] = (Severity.critical, Severity.warning, Severity.info)
_RANK = {s: i for i, s in enumerate(SEVERITIES)}
_CRITICAL_MARK = re.compile(r"\s*\(critical\)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class UnitInputs:
    unit_id: str
    name: str
    snapshot: list[PatientSnapshot] = field(default_factory=list)  # last good (pre-downtime) snapshot
    records: list[CapturedRecord] = field(default_factory=list)  # captured paper records
    discrepancies: list[Discrepancy] = field(default_factory=list)


def critical_lab(entry: str) -> str | None:
    """The lab without its marker when the pending_labs entry is flagged critical, else None."""
    m = _CRITICAL_MARK.search(entry)
    return entry[:m.start()].strip() if m else None


def administered(med: Medication, patient_id: str, records: list[CapturedRecord]) -> bool:
    """A non-rejected downtime MAR capture for this patient and medication, timed after the last charted dose."""
    for r in records:
        if r.form_type != FormType.downtime_mar or r.patient_id != patient_id or r.status == RecordStatus.rejected:
            continue
        name = fields(r).get("medication")
        if name and same_medication(name, med.name) and (med.last_given is None or record_time(r) > med.last_given):
            return True
    return False


def _overdue(u: UnitInputs, now: datetime) -> dict:
    items = [{"patient_id": p.patient_id, "bed": p.bed, "medication": m.name, "dose": m.dose, "route": m.route,
              "next_due": m.next_due.isoformat(), "overdue_minutes": int((now - m.next_due).total_seconds() // 60)}
             for p in u.snapshot for m in p.medications
             if m.high_alert and m.next_due is not None and m.next_due < now
             and not administered(m, p.patient_id, u.records)]
    items.sort(key=lambda i: (i["next_due"], i["patient_id"], i["medication"]))
    return {"count": len(items), "items": items}


def _discrepancies(u: UnitInputs, beds: dict[str, str]) -> dict:
    open_ = sorted((d for d in u.discrepancies if d.status == DiscrepancyStatus.open),
                   key=lambda d: (_RANK[d.severity], d.patient_id, d.rule_id, d.discrepancy_id))
    return {"count": len(open_),
            "by_severity": {s.value: sum(1 for d in open_ if d.severity == s) for s in SEVERITIES},
            "items": [{"discrepancy_id": d.discrepancy_id, "rule_id": d.rule_id, "patient_id": d.patient_id,
                       "bed": beds.get(d.patient_id), "severity": d.severity.value, "description": d.description,
                       "record_id": d.record_id} for d in open_]}


def _backload(u: UnitInputs) -> dict:
    return {"count": len(u.records),
            "by_group": {g.value: sum(1 for r in u.records if r.backload_group == g) for g in BackloadGroup},
            "needs_review": sum(1 for r in u.records if r.status == RecordStatus.needs_review)}


def _critical_labs(u: UnitInputs) -> dict:
    items = [{"patient_id": p.patient_id, "bed": p.bed, "lab": lab, "entry": entry}
             for p in sorted(u.snapshot, key=lambda p: p.patient_id) for entry in p.pending_labs
             if (lab := critical_lab(entry)) is not None]
    return {"count": len(items), "items": items}


def unit_view(u: UnitInputs, now: datetime) -> dict:
    beds = {p.patient_id: p.bed for p in u.snapshot}
    return {"unit_id": u.unit_id, "name": u.name, "as_of": now.isoformat(),
            "overdue_high_alert": _overdue(u, now),
            "open_discrepancies": _discrepancies(u, beds),
            "backload_counts": _backload(u),
            "critical_labs_awaiting": _critical_labs(u)}


def build(units: list[UnitInputs], *, clock: Callable[[], datetime]) -> dict:
    """{units: [{unit_id, name, as_of, <four panels>}], generated_at}. `clock` is the unit clock (required: the
    aggregator never reads time by itself); it is read once, so every unit is judged at the same instant."""
    now = clock()
    return {"units": [unit_view(u, now) for u in units], "generated_at": now.isoformat()}
