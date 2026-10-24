"""End to end in DRY_RUN: the README demo script, step by step, through the HTTP API only."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.store import get_store
from contracts import Agreement, CapturedRecord, Discrepancy, DowntimePacket, PolicyAnswer, ShiftDigest
from simulator import main as sim
from tests.test_api import PNG, api, audited  # noqa: F401 - fixture

RN, CHARGE, RX = {"X-Role": "rn"}, {"X-Role": "charge_nurse"}, {"X-Role": "pharmacy"}


def test_demo_script_end_to_end(api):  # noqa: F811
    c = api
    clock = TestClient(sim.app)
    assert c.get("/status").json()["phase"] == "before"
    clock.post("/clock/advance", json={"minutes": 15})  # downtime at 03:00
    assert len(c.post("/snapshot/pull").json()) == 12

    # 1. Go to downtime: a packet per patient; Bed 12 shows R-DUE-01 and every line cites its source.
    r = c.post("/downtime/start", headers=CHARGE)
    assert r.status_code == 200
    packets = {p["patient_id"]: DowntimePacket.model_validate(p) for p in r.json()}
    assert len(packets) == 12 and "R-DUE-01" in {a.rule_id for a in packets["pt-12"].alerts}
    assert all(i.source_path for i in packets["pt-12"].sbar.situation)
    status = c.get("/status").json()
    assert status["phase"] == "downtime" and status["outage_started_at"] and status["restored_at"] is None

    # The RN's checklist comes from the policy.
    assert c.get("/checklists", headers=RN).json()["items"]

    # 2. Policy question: cited, grounded answer.
    a = PolicyAnswer.model_validate(
        c.post("/policy/ask", json={"question": "How do I get a stat lab with the order system down?"}, headers=RN).json())
    assert a.grounded and a.citations

    # 3. Photograph a vitals flowsheet: two readers, one disagreement, one unreadable; recorded as the RN's capture.
    r = c.post("/capture", files={"file": ("form.png", PNG, "image/png")}, data={"form_type": "vitals_flowsheet"},
               headers=RN)
    assert r.status_code == 201
    vitals = CapturedRecord.model_validate(r.json())
    flags = {f.name: f.flag.value for f in vitals.fields if f.flag}
    assert sum(f.agreement == Agreement.agree for f in vitals.fields) == 8
    assert flags["resp_rate"] == "disagreement" and flags["pain_score"] == "unreadable"
    assert vitals.captured_by_role == "rn" and vitals.patient_id == "pt-12"

    mar = CapturedRecord.model_validate(
        c.post("/capture", files={"file": ("mar.png", PNG, "image/png")}, data={"form_type": "downtime_mar"},
               headers=RN).json())
    mar_02 = CapturedRecord.model_validate(  # heparin given to Bed 2, which has no heparin order
        c.post("/capture", files={"file": ("mar.png", PNG, "image/png")},
               data={"form_type": "downtime_mar", "patient_id": "pt-02"}, headers=RN).json())
    clock.post("/clock/advance", json={"minutes": 45})  # 03:45: the 30-min vitals response window has closed

    # 4. Verbal order read-back for Bed 12 (high-alert heparin) -> pharmacy backload, needs co-sign. Taken after
    # the window: inside it, a pt-12 order would count as the documented response to the vitals (R-REC-02).
    r = c.post("/capture", data={"form_type": "verbal_order", "patient_id": "pt-12",
                                 "readback_text": "heparin 5000 units SC q12h, Dr. Quill, read back and confirmed"},
               headers=RN)
    assert r.status_code == 201
    vo = CapturedRecord.model_validate(r.json())
    assert vo.backload_group.value == "pharmacy_orders"

    assert len(get_store().list_records()) == 4

    # 6. Systems restored: restored-EHR diff + recovery rules, restore time on the unit clock.
    r = c.post("/recovery/restore", headers=CHARGE)
    assert r.status_code == 200, r.text
    found = [Discrepancy.model_validate(d) for d in r.json()]
    assert {"R-REC-01", "R-REC-02", "R-REC-03", "R-VO-01", "DIFF-ORDER", "DIFF-ADT"} <= {d.rule_id for d in found}
    assert any(d.rule_id == "R-VO-01" and d.record_id == vo.record_id for d in found)
    status = c.get("/status").json()
    assert status["restored_at"] and status["restored_at"] >= status["outage_started_at"]

    queue = c.get("/backload", headers=RN).json()
    assert [x["record_id"] for x in queue["nursing_vitals"]] == [vitals.record_id]
    assert {x["record_id"] for x in queue["nursing_med_admin"]} == {mar.record_id, mar_02.record_id}

    # Verify one record and mark it entered.
    assert c.post(f"/backload/{vitals.record_id}/verify", json={}, headers=RN).json()["status"] == "verified"
    assert c.post(f"/backload/{vitals.record_id}/entered", headers=RN).json()["status"] == "entered"

    # High-alert heparin order: RN first, then a different person (pharmacy) completes it.
    assert c.post(f"/backload/{vo.record_id}/verify", json={}, headers=RN).json()["status"] == "needs_review"
    assert c.post(f"/backload/{vo.record_id}/verify", json={}, headers=RN).status_code == 409
    done = c.post(f"/backload/{vo.record_id}/verify", json={}, headers=RX).json()
    assert done["status"] == "verified" and done["verified_by"] == "rn + pharmacy"
    assert c.post(f"/backload/{vo.record_id}/entered", headers=RX).json()["status"] == "entered"

    # Charge nurse works the discrepancies (policy 7.5).
    d = next(x for x in found if x.rule_id == "R-VO-01")
    r = c.post(f"/discrepancies/{d.discrepancy_id}/status", json={"status": "acknowledged"}, headers=CHARGE)
    assert r.status_code == 200 and r.json()["status"] == "acknowledged"
    assert c.post(f"/discrepancies/{d.discrepancy_id}/status", json={"status": "resolved"},
                  headers=CHARGE).json()["status"] == "resolved"
    assert c.post("/discrepancies/nope/status", json={"status": "resolved"}).status_code == 404
    assert c.post(f"/discrepancies/{d.discrepancy_id}/status", json={"status": "bogus"}).status_code == 422
    still_open = [x for x in c.get("/discrepancies").json() if x["status"] == "open"]
    assert len(still_open) == len(found) - 1

    # 7. Shift digest, then audit sync; every action is audited under the role that took it.
    digest = ShiftDigest.model_validate(c.get("/digest", headers=CHARGE).json())
    assert len(digest.per_patient) == 12 and digest.unit_summary
    assert c.post("/sync", headers=CHARGE).json()["pushed"] > 0
    events = get_store().list_audit(5000)
    assert all(e.synced for e in events if e.action != "sync.push")
    roles = {(e.action, e.actor_role) for e in events}
    assert {("capture.create", "rn"), ("backload.verify", "pharmacy"), ("recovery.restore", "charge_nurse"),
            ("discrepancy.acknowledged", "charge_nurse"), ("discrepancy.resolved", "charge_nurse")} <= roles
    assert audited("digest.generate") and c.get("/costs").json()["dry_run"] is True
