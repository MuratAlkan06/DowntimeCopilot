"""Deterministic rules (README "Rules"). No model calls. Each rule has an ID and a one-line description.

Snapshot rules take (PatientSnapshot, now) and return Alerts for the packet.
Recovery rules take a RecoveryContext and return Discrepancies.

Captured-record field names the recovery rules read (shared with app/capture):
  downtime_mar:     medication, dose, route, time_given, frequency (optional), initials, second_verifier
  vitals_flowsheet: time, temp_c, heart_rate, resp_rate, bp_systolic, bp_diastolic, spo2, pain_score,
                    response (documented response to an abnormal value)
  verbal_order:     medication, dose, route, frequency, ordering_provider, read_back, provider_cosign
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from app import config
from app.rules.drug_classes import allergy_matches, drug_class, frequency_hours, same_medication
from contracts import Alert, CapturedRecord, Discrepancy, FormType, Medication, PatientSnapshot, Severity

DUE_WINDOW = timedelta(minutes=60)
STALE_AFTER = timedelta(minutes=30)
RESPONSE_WINDOW = timedelta(minutes=30)

# Unit parameters for the Vitals Flowsheet (policy 3.4). Inclusive normal ranges; the defaults live in
# app/config.py and are env-overridable (config.vital_ranges() is read at call time).
VITAL_RANGES: dict[str, tuple[float, float]] = config.VITAL_RANGE_DEFAULTS

AFFIRMATIVE = {"yes", "y", "true", "done", "confirmed", "read back and confirmed", "signed"}


@dataclass(frozen=True)
class Rule:
    rule_id: str
    phase: str
    description: str
    fn: Callable


RULES: dict[str, Rule] = {}


def rule(rule_id: str, phase: str, description: str):
    def wrap(fn):
        RULES[rule_id] = Rule(rule_id, phase, description, fn)
        return fn
    return wrap


@dataclass
class RecoveryContext:
    snapshots: dict[str, PatientSnapshot]
    records: list[CapturedRecord] = field(default_factory=list)


# ------------------------------------------------------------------ helpers

_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)")  # digits with at most one decimal point


def out_of_range(name: str, value: str | None) -> bool:
    """True when a configured numeric vital is beyond its inclusive range, or is not a number at all
    ("abc", "1O4"): an unparseable vital is never shown as in range. None/empty is left to the
    unreadable path."""
    ranges = config.vital_ranges()
    if name not in ranges or value is None:
        return False
    s = str(value).strip()
    if not s:
        return False
    if not _NUMBER.fullmatch(s):
        return True
    v = float(s)
    lo, hi = ranges[name]
    return not (lo <= v <= hi)


def parse_time(value: str | None, ref: datetime) -> datetime | None:
    """ISO datetime, or HH:MM on the reference date (same tz)."""
    if not value:
        return None
    v = value.strip()
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=ref.tzinfo)
    except ValueError:
        pass
    try:
        hh, mm = v.replace(".", ":").split(":")[:2]
        return ref.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except (ValueError, TypeError):
        return None


def fields(record: CapturedRecord) -> dict[str, str | None]:
    return {f.name: f.value for f in record.fields}


def record_time(record: CapturedRecord) -> datetime:
    f = fields(record)
    return parse_time(f.get("time_given") or f.get("time"), record.captured_at) or record.captured_at


def frequency_window(frequency: str | None) -> timedelta | None:
    hours = frequency_hours(frequency or "")
    return timedelta(hours=hours) if hours else None


def discrepancy(rule_id: str, patient_id: str, record_id: str | None, description: str,
                severity: Severity) -> Discrepancy:
    digest = hashlib.sha1(f"{rule_id}|{patient_id}|{record_id}|{description}".encode()).hexdigest()[:12]
    return Discrepancy(discrepancy_id=f"d-{digest}", rule_id=rule_id, patient_id=patient_id,
                       record_id=record_id, description=description, severity=severity)


# ------------------------------------------------------------------ snapshot rules

@rule("R-ALG-01", "any", "Active medication matches a documented allergy")
def r_alg_01(p: PatientSnapshot, now: datetime) -> list[Alert]:
    return [
        Alert(rule_id="R-ALG-01",
              text=f"{m.name} is active but {p.name} has a documented {a.substance} allergy ({a.reaction}, {a.severity})")
        for a in p.allergies for m in p.medications if allergy_matches(a.substance, m.name)
    ]


@rule("R-DUP-01", "any", "Two active medications in the same therapeutic class")
def r_dup_01(p: PatientSnapshot, now: datetime) -> list[Alert]:
    by_class: dict[str, list[str]] = {}
    for m in p.medications:
        cls = drug_class(m.name)
        if cls:
            by_class.setdefault(cls, []).append(m.name)
    return [Alert(rule_id="R-DUP-01", text=f"Duplicate {cls.replace('_', ' ')} therapy: {', '.join(names)}")
            for cls, names in by_class.items() if len(names) > 1]


# Alert text carries ABSOLUTE unit times only ("due 03:30", "taken 02:05"). Alerts are stored in packets
# and served from the rules endpoint long after evaluation, so a relative phrase ("in 43 min") would
# freeze and go wrong. The UI computes the relative part at render time from the unit clock (/status).

def due_high_alert(p: PatientSnapshot, now: datetime) -> list[Medication]:
    """High-alert medications whose next dose falls in [now, now + 60 min]."""
    return [m for m in p.medications
            if m.high_alert and m.next_due is not None and now <= m.next_due <= now + DUE_WINDOW]


@rule("R-DUE-01", "downtime", "High-alert medication due within 60 minutes")
def r_due_01(p: PatientSnapshot, now: datetime) -> list[Alert]:
    return [Alert(rule_id="R-DUE-01",
                  text=f"High-alert {m.name} {m.dose} {m.route} due {m.next_due:%H:%M}; "
                       "independent double-check required")
            for m in due_high_alert(p, now)]


@rule("R-STALE-01", "downtime", "Snapshot older than 30 minutes")
def r_stale_01(p: PatientSnapshot, now: datetime) -> list[Alert]:
    if now - p.snapshot_time > STALE_AFTER:
        return [Alert(rule_id="R-STALE-01",
                      text=f"Snapshot taken {p.snapshot_time:%H:%M} is older than 30 min; "
                           "reconcile with the patient and paper chart")]
    return []


# ------------------------------------------------------------------ recovery rules

def _records(ctx: RecoveryContext, form_type: FormType) -> list[CapturedRecord]:
    return [r for r in ctx.records if r.form_type == form_type and r.patient_id and r.status != "rejected"]


@rule("R-REC-01", "recovery", "Medication administered on paper with no pre-downtime order")
def r_rec_01(ctx: RecoveryContext) -> list[Discrepancy]:
    out = []
    for r in _records(ctx, FormType.downtime_mar):
        med = fields(r).get("medication")
        snap = ctx.snapshots.get(r.patient_id)
        if not med or snap is None:
            continue
        if not any(same_medication(med, m.name) for m in snap.medications):
            out.append(discrepancy("R-REC-01", r.patient_id, r.record_id,
                                   f"{med} given on paper with no pre-downtime order", Severity.critical))
    return out


@rule("R-REC-02", "recovery", "Vitals out of range with no documented response within 30 minutes")
def r_rec_02(ctx: RecoveryContext) -> list[Discrepancy]:
    out = []
    for r in _records(ctx, FormType.vitals_flowsheet):
        f = fields(r)
        abnormal = [f"{k}={v}" for k, v in f.items() if out_of_range(k, v)]
        if not abnormal or (f.get("response") or "").strip():
            continue
        t = record_time(r)
        responded = any(
            o.patient_id == r.patient_id and o.record_id != r.record_id
            and o.form_type in (FormType.downtime_mar, FormType.verbal_order)
            and t <= record_time(o) <= t + RESPONSE_WINDOW
            for o in ctx.records
        )
        if not responded:
            out.append(discrepancy("R-REC-02", r.patient_id, r.record_id,
                                   f"Out-of-range vitals ({', '.join(abnormal)}) at {t:%H:%M} with no documented "
                                   "response within 30 min", Severity.warning))
    return out


@rule("R-REC-03", "recovery", "Duplicate administration inside the frequency window")
def r_rec_03(ctx: RecoveryContext) -> list[Discrepancy]:
    out = []
    mars = _records(ctx, FormType.downtime_mar)
    for r in mars:
        f = fields(r)
        med, t = f.get("medication"), record_time(r)
        if not med:
            continue
        snap = ctx.snapshots.get(r.patient_id)
        order = next((m for m in (snap.medications if snap else []) if same_medication(med, m.name)), None)
        window = frequency_window(f.get("frequency") or (order.frequency if order else None))
        if window is None:
            continue
        priors = [record_time(o) for o in mars
                  if o.patient_id == r.patient_id and o.record_id != r.record_id
                  and same_medication(med, fields(o).get("medication") or "") and record_time(o) < t]
        if order and order.last_given:
            priors.append(order.last_given)
        hit = [p for p in priors if timedelta(0) <= t - p < window]
        if hit:
            out.append(discrepancy("R-REC-03", r.patient_id, r.record_id,
                                   f"{med} given at {t:%H:%M}, previous dose {max(hit):%H:%M} is inside the "
                                   f"{int(window.total_seconds() // 3600)}h window", Severity.critical))
    return out


@rule("R-VO-01", "recovery", "Verbal order without read-back or provider co-sign")
def r_vo_01(ctx: RecoveryContext) -> list[Discrepancy]:
    out = []
    for r in _records(ctx, FormType.verbal_order):
        f = fields(r)
        missing = [label for key, label in (("read_back", "read-back"), ("provider_cosign", "provider co-sign"))
                   if (f.get(key) or "").strip().lower() not in AFFIRMATIVE]
        if missing:
            out.append(discrepancy("R-VO-01", r.patient_id, r.record_id,
                                   f"Verbal order {f.get('medication') or ''} missing {' and '.join(missing)}".replace("  ", " "),
                                   Severity.warning))
    return out


# ------------------------------------------------------------------ runners

def snapshot_alerts(p: PatientSnapshot, now: datetime, phase: str = "downtime") -> list[Alert]:
    phases = {"any", phase}
    return [a for r in RULES.values() if r.phase in phases and r.phase != "recovery" for a in r.fn(p, now)]


def rule_hits(patients: list[PatientSnapshot], now: datetime) -> dict:
    """Every snapshot rule (R-ALG-01, R-DUP-01, R-DUE-01, R-STALE-01) for every patient, as served by the
    non-contract GET /rules/current. Each hit is the contract Alert ({rule_id, text}) plus structured
    times the UI needs to render relative phrases live: `due_at` (R-DUE-01) and `snapshot_time`
    (R-STALE-01). Kept out of contracts/models.py on purpose: the Alert contract stays {rule_id, text}."""
    out: dict[str, list[dict]] = {}
    for p in patients:
        due = iter(due_high_alert(p, now))  # same order r_due_01 emits its alerts in
        hits = []
        for a in snapshot_alerts(p, now):
            hit = a.model_dump(mode="json")
            if a.rule_id == "R-DUE-01":
                hit["due_at"] = next(due).next_due.isoformat()
            elif a.rule_id == "R-STALE-01":
                hit["snapshot_time"] = p.snapshot_time.isoformat()
            hits.append(hit)
        out[p.patient_id] = hits
    return {"evaluated_at": now.isoformat(), "patients": out}


def recovery_discrepancies(ctx: RecoveryContext) -> list[Discrepancy]:
    return [d for r in RULES.values() if r.phase == "recovery" for d in r.fn(ctx)]
