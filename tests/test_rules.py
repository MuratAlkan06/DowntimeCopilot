"""Each rule: one fixture that fires it and one that does not (README "Rules")."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.rules import RULES, RecoveryContext, engine, rule_hits, snapshot_alerts
from contracts import Agreement, CapturedField, CapturedRecord, PatientSnapshot
from simulator.state import DEFAULT_SEED, SimState

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


def judged(hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return NOW.replace(hour=h, minute=m)


def vitals_0255(**extra):
    """The retimed vitals fixture: 02:55 on paper, captured 03:00, SpO2 89 out of range."""
    return rec("vitals_flowsheet", {"time": "02:55", "spo2": "89", "heart_rate": "104", **extra},
               at=judged("03:00"), group="nursing_vitals")


def test_rec02_fires_once_window_closed_at_judgment_time():
    ctx = RecoveryContext({"pt-t": base()}, [vitals_0255()], now=judged("03:45"))
    hits = run("R-REC-02", ctx)
    assert [d.rule_id for d in hits] == ["R-REC-02"] and "at 02:55" in hits[0].description


def test_rec02_fires_exactly_at_window_close():
    assert len(run("R-REC-02", RecoveryContext({"pt-t": base()}, [vitals_0255()], now=judged("03:25")))) == 1


def test_rec02_quiet_while_window_still_open():
    ctx = RecoveryContext({"pt-t": base()}, [vitals_0255()], now=judged("03:10"))
    assert run("R-REC-02", ctx) == []


def test_rec02_quiet_when_response_documented_after_window():
    ctx = RecoveryContext({"pt-t": base()}, [vitals_0255(response="2L NC, recheck 94")], now=judged("03:45"))
    assert run("R-REC-02", ctx) == []


def test_rec02_quiet_when_order_given_inside_window():
    mar = rec("downtime_mar", {"medication": "albuterol", "time_given": "03:05"}, rid="r2", at=judged("03:10"))
    ctx = RecoveryContext({"pt-t": base()}, [vitals_0255(), mar], now=judged("03:45"))
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


# ------------------------------------------------------------ R-REC-04
def mar_dose(dose, medication="apixaban"):
    return rec("downtime_mar", {"medication": medication, "dose": dose, "time_given": "03:10"})


def ordered(dose, name="apixaban"):
    return {"pt-t": base(medications=[{**med(name), "dose": dose}])}


def test_rec04_fires_on_dose_mismatch():
    hits = run("R-REC-04", RecoveryContext(ordered("2.5 mg"), [mar_dose("5 mg")]))
    assert [(d.rule_id, d.severity.value, d.record_id) for d in hits] == [("R-REC-04", "critical", "r1")]
    assert "5 mg" in hits[0].description and "2.5 mg" in hits[0].description


def test_rec04_quiet_on_matching_dose():
    assert run("R-REC-04", RecoveryContext(ordered("2.5 mg"), [mar_dose("2.5 mg")])) == []


@pytest.mark.parametrize("paper, order", [
    ("5 milligrams", "5 mg"), ("5mg", "5 mg"), ("5 MG", "5 mg"), ("0.005 g", "5 mg"), ("1 g", "1000 mg"),
    ("5,000 units", "5000 units"), ("5000 unit", "5000 units"), ("8 mg/hr", "8 mg/h"),
])
def test_rec04_same_dose_different_unit_string_is_quiet(paper, order):
    """Boundary (decided S6): unit spellings are canonicalized and mass units compared in mg."""
    assert run("R-REC-04", RecoveryContext(ordered(order), [mar_dose(paper)])) == []


@pytest.mark.parametrize("paper, order", [("5 units", "5 mg"), ("8 mg/h", "8 mg"), ("5 mcg", "5 mg")])
def test_rec04_same_number_different_dimension_fires(paper, order):
    assert len(run("R-REC-04", RecoveryContext(ordered(order), [mar_dose(paper)]))) == 1


@pytest.mark.parametrize("paper_med, paper, order_med, order", [
    ("apixaban", None, "apixaban", "2.5 mg"),                     # unreadable dose: flagged for review
    ("insulin lispro", "6 units", "insulin lispro", "sliding scale"),  # no fixed ordered dose
    ("ondansetron", "5 mg", "apixaban", "2.5 mg"),                # no order for this med: R-REC-01's case
])
def test_rec04_quiet_when_not_comparable(paper_med, paper, order_med, order):
    ctx = RecoveryContext(ordered(order, order_med), [mar_dose(paper, paper_med)])
    assert run("R-REC-04", ctx) == []


def test_parse_dose():
    assert engine.parse_dose("5000 units") == (5000.0, "units")
    assert engine.parse_dose("50 mcg/h") == (0.05, "mg/h")
    assert engine.parse_dose("per DKA protocol") is None and engine.parse_dose("800-160 mg") is None
    assert engine.parse_dose("5 tablets") is None and engine.parse_dose(None) is None


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
                          "R-REC-01", "R-REC-02", "R-REC-03", "R-REC-04", "R-VO-01"}
    assert all(r.description for r in RULES.values())
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    table = dict(re.findall(r"^\| (R-[A-Z]+-\d+) \| \w+ \| (.+?) \|$", readme, re.MULTILINE))
    assert table == {rid: r.description for rid, r in RULES.items()}


@pytest.mark.parametrize("pid,rule_id", [("pt-03", "R-ALG-01"), ("pt-05", "R-ALG-01"), ("pt-07", "R-DUP-01"),
                                         ("pt-12", "R-DUE-01")])
def test_seeded_patients_fire_expected_rules(pid, rule_id):
    p = PatientSnapshot.model_validate(json.loads((PATIENTS / f"{pid}.json").read_text()))
    assert rule_id in {a.rule_id for a in snapshot_alerts(p, NOW)}


# ------------------------------------------------------------ fire counts on the simulated feed
SEEDED_FIRES = {"R-ALG-01": {"pt-03", "pt-05"}, "R-DUP-01": {"pt-07"}, "R-DUE-01": {"pt-08", "pt-11", "pt-12"}}


def fired(hits: dict) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for pid, items in hits["patients"].items():
        for h in items:
            out.setdefault(h["rule_id"], set()).add(pid)
    return out


@pytest.mark.parametrize("minutes", [0, 15])  # seed clock 02:45, and 03:00 after the DEMO.md "advance 15"
def test_seeded_fire_counts_exact(minutes):
    state = SimState(DEFAULT_SEED)
    state.advance(minutes)
    got = fired(rule_hits(state.snapshot(), state.now))
    assert {rid: got.get(rid, set()) for rid in SEEDED_FIRES} == SEEDED_FIRES


def due_and_stale_patient() -> PatientSnapshot:
    return base(medications=[med("heparin", next_due=NOW + timedelta(minutes=43), high_alert=True)],
                snapshot_time=(NOW - timedelta(minutes=42)).isoformat())


def test_alert_text_is_absolute_time_only():
    texts = [a.text for a in snapshot_alerts(due_and_stale_patient(), NOW)]
    assert any("due 03:30" in t for t in texts) and any("taken 02:05" in t for t in texts)
    assert not any(re.search(r"\bin \d+ min|\d+ min old", t) for t in texts)


def test_rule_hits_stable_text_with_structured_times():
    p = due_and_stale_patient()
    early, late = (({h["rule_id"]: h for h in rule_hits([p], t)["patients"]["pt-t"]})
                   for t in (NOW, NOW + timedelta(minutes=20)))
    assert early["R-DUE-01"]["text"] == late["R-DUE-01"]["text"]  # nothing relative frozen into the text
    assert early["R-DUE-01"]["due_at"] == late["R-DUE-01"]["due_at"] == "2026-10-24T03:30:00+00:00"
    assert early["R-STALE-01"]["snapshot_time"] == "2026-10-24T02:05:00+00:00"
    assert set(early["R-DUE-01"]) == {"rule_id", "text", "due_at"}


def test_rules_engine_never_calls_a_model():
    src = "\n".join(f.read_text() for f in sorted(Path(engine.__file__).parent.glob("*.py")))
    assert not re.search(r"\b(boto3|bedrock|anthropic|app\.ai|from app import ai)\b", src, re.IGNORECASE)


@pytest.mark.parametrize("minutes", [0, 15])
def test_seeded_stale_example_is_exactly_one(minutes):
    """Readiness: one lagging feed in the seed (pt-14, 40 min behind the unit clock), nobody else stale."""
    state = SimState(DEFAULT_SEED)
    state.advance(minutes)
    hits = rule_hits(state.snapshot(), state.now)
    assert fired(hits).get("R-STALE-01") == {"pt-14"}
    (stale,) = [h for h in hits["patients"]["pt-14"] if h["rule_id"] == "R-STALE-01"]
    assert state.now - datetime.fromisoformat(stale["snapshot_time"]) == timedelta(minutes=40)


def test_high_alert_classifier_matches_seeded_flags():
    """Two-person verify keys off is_high_alert(); it must agree with every seeded medication's flag."""
    import json
    from pathlib import Path

    from app.rules.drug_classes import is_high_alert
    for path in sorted((Path(__file__).resolve().parent.parent / "data" / "patients").glob("*.json")):
        for m in json.loads(path.read_text())["medications"]:
            assert is_high_alert(m["name"]) is m["high_alert"], (path.name, m["name"])
    assert is_high_alert("Heparin 5000 units") and is_high_alert("KCl 20 mEq") and not is_high_alert(None)
