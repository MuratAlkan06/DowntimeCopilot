"""Each rule: one fixture that fires it and one that does not (README "Rules")."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.rules import RULES, RecoveryContext, snapshot_alerts
from contracts import Agreement, CapturedField, CapturedRecord, PatientSnapshot

NOW = datetime(2026, 10, 24, 2, 47, tzinfo=timezone.utc)
PATIENTS = Path(__file__).resolve().parent.parent / "data" / "patients"


def base(**overrides) -> PatientSnapshot:
    doc = {
        "patient_id": "pt-t", "mrn": "SYN-T", "name": "Test Person", "dob": "1960-01-01", "age": 66,
        "sex": "female", "bed": "Bed 99", "admitting_dx": "test", "code_status": "Full code",
        "allergies": [], "medications": [], "fall_risk": "low", "snapshot_time": NOW.isoformat(),
    }
    doc.update(overrides)
    return PatientSnapshot.model_validate(doc)


def med(name, freq="q6h", next_due=None, high_alert=False, last_given=None):
    return {"name": name, "dose": "1 unit", "route": "IV", "frequency": freq,
            "next_due": next_due.isoformat() if next_due else None, "high_alert": high_alert,
            "last_given": last_given.isoformat() if last_given else None}


def rec(form_type, fields, patient_id="pt-t", rid="r1", at=NOW + timedelta(hours=1), group="nursing_med_admin"):
    return CapturedRecord(
        record_id=rid, patient_id=patient_id, form_type=form_type, image_ref="blob:x", captured_at=at,
        captured_by_role="rn", backload_group=group,
        fields=[CapturedField(name=k, value=v, confidence=0.9, agreement=Agreement.agree) for k, v in fields.items()],
    )


def run(rule_id, *args):
    return RULES[rule_id].fn(*args)


# ------------------------------------------------------------ R-ALG-01
def test_alg_fires_on_penicillin_class():
    p = base(allergies=[{"substance": "penicillin", "reaction": "hives", "severity": "moderate"}],
             medications=[med("ampicillin-sulbactam")])
    assert [a.rule_id for a in run("R-ALG-01", p, NOW)] == ["R-ALG-01"]


def test_alg_quiet_without_conflict():
    p = base(allergies=[{"substance": "codeine", "reaction": "nausea", "severity": "mild"}],
             medications=[med("hydromorphone")])
    assert run("R-ALG-01", p, NOW) == []


# ------------------------------------------------------------ R-DUP-01
def test_dup_fires_on_two_anticoagulants():
    p = base(medications=[med("enoxaparin"), med("apixaban")])
    assert len(run("R-DUP-01", p, NOW)) == 1


def test_dup_quiet_on_different_classes():
    p = base(medications=[med("heparin"), med("insulin lispro")])
    assert run("R-DUP-01", p, NOW) == []


# ------------------------------------------------------------ R-DUE-01
def test_due_fires_for_high_alert_within_60():
    p = base(medications=[med("heparin", next_due=NOW + timedelta(minutes=43), high_alert=True)])
    assert len(run("R-DUE-01", p, NOW)) == 1


def test_due_quiet_outside_window_or_not_high_alert():
    p = base(medications=[med("insulin lispro", next_due=NOW + timedelta(minutes=73), high_alert=True),
                          med("vancomycin", next_due=NOW + timedelta(minutes=10), high_alert=False)])
    assert run("R-DUE-01", p, NOW) == []


# ------------------------------------------------------------ R-STALE-01
def test_stale_fires_after_30_minutes():
    p = base(snapshot_time=(NOW - timedelta(minutes=31)).isoformat())
    assert len(run("R-STALE-01", p, NOW)) == 1


def test_stale_quiet_when_fresh():
    p = base(snapshot_time=(NOW - timedelta(minutes=2)).isoformat())
    assert run("R-STALE-01", p, NOW) == []


# ------------------------------------------------------------ R-REC-01
def test_rec01_fires_on_unordered_paper_med():
    ctx = RecoveryContext({"pt-t": base(medications=[med("vancomycin")])},
                          [rec("downtime_mar", {"medication": "ondansetron", "time_given": "03:10"})])
    assert [d.rule_id for d in run("R-REC-01", ctx)] == ["R-REC-01"]


def test_rec01_quiet_on_ordered_med():
    ctx = RecoveryContext({"pt-t": base(medications=[med("vancomycin")])},
                          [rec("downtime_mar", {"medication": "Vancomycin", "time_given": "06:00"})])
    assert run("R-REC-01", ctx) == []


# ------------------------------------------------------------ R-REC-02
def test_rec02_fires_on_unanswered_low_spo2():
    ctx = RecoveryContext({"pt-t": base()},
                          [rec("vitals_flowsheet", {"time": "03:30", "spo2": "88", "heart_rate": "92"},
                               group="nursing_vitals")])
    assert len(run("R-REC-02", ctx)) == 1


def test_rec02_quiet_when_response_documented():
    ctx = RecoveryContext({"pt-t": base()},
                          [rec("vitals_flowsheet", {"time": "03:30", "spo2": "88", "response": "2L NC, recheck 94"},
                               group="nursing_vitals")])
    assert run("R-REC-02", ctx) == []


# ------------------------------------------------------------ R-REC-03
def test_rec03_fires_on_double_dose_inside_window():
    p = base(medications=[med("vancomycin", freq="q12h", last_given=NOW - timedelta(hours=1))])
    ctx = RecoveryContext({"pt-t": p}, [rec("downtime_mar", {"medication": "vancomycin", "time_given": "04:00"})])
    assert len(run("R-REC-03", ctx)) == 1


def test_rec03_quiet_outside_window():
    p = base(medications=[med("vancomycin", freq="q12h", last_given=NOW - timedelta(hours=13))])
    ctx = RecoveryContext({"pt-t": p}, [rec("downtime_mar", {"medication": "vancomycin", "time_given": "04:00"})])
    assert run("R-REC-03", ctx) == []


# ------------------------------------------------------------ R-VO-01
def test_vo01_fires_without_cosign():
    ctx = RecoveryContext({"pt-t": base()},
                          [rec("verbal_order", {"medication": "ondansetron", "read_back": "yes", "provider_cosign": "no"},
                               group="pharmacy_orders")])
    assert len(run("R-VO-01", ctx)) == 1


def test_vo01_quiet_with_readback_and_cosign():
    ctx = RecoveryContext({"pt-t": base()},
                          [rec("verbal_order", {"medication": "ondansetron", "read_back": "yes",
                                                "provider_cosign": "signed"}, group="pharmacy_orders")])
    assert run("R-VO-01", ctx) == []


# ------------------------------------------------------------ registry + seeded data
def test_registry_is_exactly_the_readme_table():
    assert set(RULES) == {"R-ALG-01", "R-DUP-01", "R-DUE-01", "R-STALE-01",
                          "R-REC-01", "R-REC-02", "R-REC-03", "R-VO-01"}
    assert all(r.description for r in RULES.values())


@pytest.mark.parametrize("pid,rule_id", [("pt-03", "R-ALG-01"), ("pt-05", "R-ALG-01"), ("pt-07", "R-DUP-01"),
                                         ("pt-12", "R-DUE-01")])
def test_seeded_patients_fire_expected_rules(pid, rule_id):
    p = PatientSnapshot.model_validate(json.loads((PATIENTS / f"{pid}.json").read_text()))
    assert rule_id in {a.rule_id for a in snapshot_alerts(p, NOW)}
