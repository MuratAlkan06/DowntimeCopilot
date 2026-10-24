"""S14 supervisor command center: pure aggregator (due boundary, administration records, injected clock, no
model), N-unit structure from a fixture, seed-edit invariance of the planted sets, and GET /command-center."""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import command_center as cc
from app.store import get_store
from contracts import (Agreement, BackloadGroup, CapturedField, CapturedRecord, Discrepancy, DiscrepancyStatus,
                       FormType, Medication, PatientSnapshot, RecordStatus, Severity)
from simulator import main as sim
from simulator.state import load_patients
from tests.test_api import PLANTED, api, audited, scripted_flow  # noqa: F401 - shared DRY_RUN app fixture

NOW = datetime(2026, 10, 24, 3, 45, tzinfo=timezone.utc)
PANELS = ("overdue_high_alert", "open_discrepancies", "backload_counts", "critical_labs_awaiting")


def at(t: datetime):
    return lambda: t


def patient(pid: str, meds: list[Medication], labs: list[str] = ()) -> PatientSnapshot:
    return PatientSnapshot(patient_id=pid, mrn=f"SYN-{pid}", name=f"Test {pid}", dob=date(1950, 1, 1), age=76,
                           sex="female", bed=f"Bed {pid[-2:]}", admitting_dx="test", code_status="Full",
                           medications=meds, pending_labs=list(labs), fall_risk="low",
                           snapshot_time=NOW - timedelta(hours=1))


def heparin(due: datetime, high_alert: bool = True) -> Medication:
    return Medication(name="heparin", dose="5000 units", route="SC", frequency="q8h", next_due=due,
                      high_alert=high_alert, last_given=due - timedelta(hours=8))


def mar(pid: str | None, med: str, time_given: str, status: RecordStatus = RecordStatus.needs_review,
        rid: str = "rec-1") -> CapturedRecord:
    f = [CapturedField(name=n, value=v, confidence=1.0, agreement=Agreement.single)
         for n, v in (("medication", med), ("dose", "5000 units"), ("time_given", time_given))]
    return CapturedRecord(record_id=rid, patient_id=pid, form_type=FormType.downtime_mar, image_ref="blob:x",
                          captured_at=NOW, captured_by_role="rn", fields=f, status=status,
                          backload_group=BackloadGroup.nursing_med_admin)


def disc(n: int, pid: str, sev: str, status: DiscrepancyStatus = DiscrepancyStatus.open) -> Discrepancy:
    return Discrepancy(discrepancy_id=f"d-{n}", rule_id=f"R-{n}", patient_id=pid, description=f"item {n}",
                       severity=Severity(sev), status=status)


def unit(snapshot=(), records=(), discrepancies=(), uid: str = "u1") -> cc.UnitInputs:
    return cc.UnitInputs(unit_id=uid, name=f"Unit {uid}", snapshot=list(snapshot), records=list(records),
                         discrepancies=list(discrepancies))


def overdue(u: cc.UnitInputs, now: datetime) -> list[dict]:
    return cc.build([u], clock=at(now))["units"][0]["overdue_high_alert"]["items"]


# ------------------------------------------------------------------ aggregator (pure)

def test_due_boundary_equal_is_not_overdue_one_second_later_is():
    due = NOW
    u = unit([patient("pt-90", [heparin(due)])])
    assert overdue(u, due) == []
    assert overdue(u, due - timedelta(seconds=1)) == []
    (item,) = overdue(u, due + timedelta(seconds=1))
    assert (item["patient_id"], item["medication"], item["next_due"]) == ("pt-90", "heparin", due.isoformat())
    assert item["overdue_minutes"] == 0
    assert overdue(u, due + timedelta(minutes=15))[0]["overdue_minutes"] == 15


def test_only_high_alert_with_a_due_time_counts():
    meds = [heparin(NOW - timedelta(minutes=30), high_alert=False),
            Medication(name="insulin regular infusion", dose="per protocol", route="IV", frequency="continuous",
                       high_alert=True)]
    assert overdue(unit([patient("pt-90", meds)]), NOW) == []


def test_administration_record_after_last_charted_dose_clears_overdue():
    due = NOW - timedelta(minutes=15)  # 03:30, last charted 19:30 the day before
    p = patient("pt-90", [heparin(due)])
    assert overdue(unit([p], [mar("pt-90", "Heparin", "03:32")]), NOW) == []
    still = {  # each of these is NOT an administration of this dose
        "rejected": mar("pt-90", "heparin", "03:32", status=RecordStatus.rejected),
        "other patient": mar("pt-91", "heparin", "03:32"),
        "unknown patient": mar(None, "heparin", "03:32"),
        "other medication": mar("pt-90", "enoxaparin", "03:32"),
        "the charted dose": mar("pt-90", "heparin", (due - timedelta(hours=8)).isoformat()),
    }
    for why, rec in still.items():
        assert len(overdue(unit([p], [rec]), NOW)) == 1, why


def test_clock_is_injected_read_once_and_the_view_is_deterministic():
    u = unit([patient("pt-90", [heparin(NOW - timedelta(minutes=1))], ["INR 05:00 (critical)"])])
    assert cc.build([u], clock=at(NOW)) == cc.build([u], clock=at(NOW))
    reads = []
    view = cc.build([u, unit(uid="u2")], clock=lambda: reads.append(1) or NOW)
    assert len(reads) == 1 and view["generated_at"] == NOW.isoformat()
    assert {v["as_of"] for v in view["units"]} == {NOW.isoformat()}
    with pytest.raises(TypeError):
        cc.build([u])  # no default clock: the aggregator never reads time by itself


def test_aggregator_never_calls_a_model_or_reads_time():
    src = Path(cc.__file__).read_text()
    assert not re.search(r"\b(boto3|botocore|bedrock|anthropic|app\.ai|from app import ai|sim_client|httpx|"
                         r"app\.store|app\.clock|unit_now|datetime\.now|time\.time)\b", src, re.IGNORECASE)
    out = subprocess.run([sys.executable, "-c", "import sys, app.command_center; print(sorted(m for m in sys.modules "
                          "if m.split('.')[0] in ('boto3', 'botocore', 'httpx') or m.startswith('app.ai') "
                          "or m in ('app.store', 'app.sim_client', 'app.api')))"],
                         capture_output=True, text=True, check=True, cwd=Path(cc.__file__).parent.parent)
    assert out.stdout.strip() == "[]"


def test_critical_lab_marker():
    assert cc.critical_lab("INR 05:00 (critical)") == "INR 05:00"
    assert cc.critical_lab("H&H 04:00 (Critical) ") == "H&H 04:00"
    assert cc.critical_lab("INR 05:00") is None and cc.critical_lab("critical care consult") is None


def test_discrepancies_open_only_by_severity_most_severe_first_and_backload_counts():
    discs = [disc(1, "pt-91", "info"), disc(2, "pt-90", "warning"), disc(3, "pt-91", "critical"),
             disc(4, "pt-90", "critical", DiscrepancyStatus.resolved), disc(5, "pt-90", "critical")]
    recs = [mar("pt-90", "heparin", "03:00", rid="rec-a"),
            mar("pt-90", "heparin", "03:00", status=RecordStatus.verified, rid="rec-b")]
    v = cc.build([unit([patient("pt-90", [])], recs, discs)], clock=at(NOW))["units"][0]
    d = v["open_discrepancies"]
    assert d["count"] == 4 and d["by_severity"] == {"critical": 2, "warning": 1, "info": 1}
    assert [i["discrepancy_id"] for i in d["items"]] == ["d-5", "d-3", "d-2", "d-1"]
    assert d["items"][0]["bed"] == "Bed 90" and d["items"][1]["bed"] is None  # pt-91 not in this snapshot
    b = v["backload_counts"]
    assert b["count"] == 2 and b["needs_review"] == 1 and set(b["by_group"]) == {g.value for g in BackloadGroup}
    assert b["by_group"]["nursing_med_admin"] == 2 and sum(b["by_group"].values()) == 2


@pytest.fixture()
def two_units() -> list[cc.UnitInputs]:
    """A fixture, not seeded data: two units with different patients, records and discrepancies."""
    a = unit([patient("pt-90", [heparin(NOW - timedelta(minutes=20))], ["K 04:00 (critical)"])],
             [mar("pt-90", "morphine", "03:10")], [disc(1, "pt-90", "critical")], uid="med-surg-3")
    b = unit([patient("pt-80", [heparin(NOW + timedelta(minutes=20))], ["CBC 05:00"]),
              patient("pt-81", [heparin(NOW - timedelta(minutes=5))], ["lactate 04:00 (critical)", "INR (critical)"])],
             [], [disc(2, "pt-81", "warning"), disc(3, "pt-80", "info")], uid="icu-2")
    return [a, b]


def test_n_units_render_with_the_same_structure(two_units):
    view = cc.build(two_units, clock=at(NOW))
    assert [u["unit_id"] for u in view["units"]] == ["med-surg-3", "icu-2"]  # input order kept
    for u in view["units"]:
        assert set(u) == {"unit_id", "name", "as_of", *PANELS} and all("count" in u[p] for p in PANELS)
    a, b = view["units"]
    assert [i["patient_id"] for i in a["overdue_high_alert"]["items"]] == ["pt-90"]
    assert [i["patient_id"] for i in b["overdue_high_alert"]["items"]] == ["pt-81"]  # pt-80 not yet due
    assert (a["open_discrepancies"]["by_severity"], b["open_discrepancies"]["by_severity"]) == (
        {"critical": 1, "warning": 0, "info": 0}, {"critical": 0, "warning": 1, "info": 1})
    assert (a["backload_counts"]["count"], b["backload_counts"]["count"]) == (1, 0)
    assert [i["lab"] for i in b["critical_labs_awaiting"]["items"]] == ["lactate 04:00", "INR"]
    assert a["critical_labs_awaiting"]["count"] == 1


# ------------------------------------------------------------------ seed edit: planted sets unchanged

FIRE_SETS = {"R-ALG-01": {"pt-03", "pt-05"}, "R-DUP-01": {"pt-07"}, "R-DUE-01": {"pt-08", "pt-11", "pt-12"},
             "R-STALE-01": {"pt-14"}}
CRITICAL_LABS = [("pt-09", "INR 05:00"), ("pt-21", "H&H 04:00")]


def test_seeded_critical_labs_sit_outside_every_planted_set():
    labs = [(p.patient_id, lab) for p in load_patients() for e in p.pending_labs if (lab := cc.critical_lab(e))]
    assert labs == CRITICAL_LABS
    planted = set().union(*FIRE_SETS.values()) | {pid for _, pid, _ in PLANTED}
    assert not planted & {pid for pid, _ in labs}


def test_planted_sets_keep_their_exact_ids_after_the_seed_edit(api):
    TestClient(sim.app).post("/clock/advance", json={"minutes": 15})  # 03:00, as DEMO.md
    api.post("/snapshot/pull")
    fired: dict[str, set[str]] = {}
    for pid, hits in api.get("/rules/current").json()["patients"].items():
        for h in hits:
            fired.setdefault(h["rule_id"], set()).add(pid)
    assert fired == FIRE_SETS
    found = scripted_flow(api)
    assert sorted((d.rule_id, d.patient_id, d.severity.value) for d in found) == PLANTED


# ------------------------------------------------------------------ GET /command-center

def test_command_center_before_any_snapshot_is_one_empty_unit(api):
    r = api.get("/command-center")
    assert r.status_code == 200 and audited("command_center.view")
    (u,) = r.json()["units"]
    assert (u["unit_id"], u["name"]) == ("med-surg-3", "Med-Surg 3")
    assert all(u[p]["count"] == 0 for p in PANELS)


def test_command_center_after_the_scripted_flow(api):
    scripted_flow(api)  # seed, +15, declare, three captures, +45, restore at 03:45
    body = api.get("/command-center", headers={"X-Role": "charge_nurse"}).json()
    assert body["generated_at"] == sim.state.now.isoformat() == "2026-10-24T03:45:00+00:00"  # unit time
    (u,) = body["units"]
    overdue_meds = {(i["patient_id"], i["medication"]) for i in u["overdue_high_alert"]["items"]}
    assert {("pt-11", "insulin regular infusion"), ("pt-12", "heparin")} <= overdue_meds
    assert all(i["next_due"] < body["generated_at"] for i in u["overdue_high_alert"]["items"])
    d = u["open_discrepancies"]
    assert d["by_severity"] == {"critical": 1, "warning": 4, "info": 1}
    assert sorted((i["rule_id"], i["patient_id"], i["severity"]) for i in d["items"]) == PLANTED
    groups = {g: len(v) for g, v in api.get("/backload").json().items()}
    assert u["backload_counts"]["by_group"] == groups and u["backload_counts"]["count"] == 3
    assert [(i["patient_id"], i["lab"]) for i in u["critical_labs_awaiting"]["items"]] == CRITICAL_LABS
    ev = next(e for e in get_store().list_audit() if e.action == "command_center.view")
    assert ev.target_id == "med-surg-3" and ev.details["open_discrepancies"] == 6
