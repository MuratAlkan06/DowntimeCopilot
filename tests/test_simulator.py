"""Simulator smoke: snapshots validate, outage blocks the feed, restore differs deterministically."""
from __future__ import annotations

from fastapi.testclient import TestClient

from contracts import PatientSnapshot
from simulator import main as sim
from simulator.state import DEFAULT_SEED, DEMO_CRITICAL, SimState


def client() -> TestClient:
    c = TestClient(sim.app)
    c.post("/reset", json={"seed": 7})
    return c


def test_snapshot_validates_and_has_demo_patient():
    c = client()
    r = c.get("/bca/snapshot")
    assert r.status_code == 200
    patients = [PatientSnapshot.model_validate(p) for p in r.json()]
    assert len(patients) == 12
    bed12 = next(p for p in patients if p.bed == "Bed 12")
    assert any(a.substance == "penicillin" for a in bed12.allergies)
    insulin = next(m for m in bed12.medications if "insulin" in m.name)
    assert insulin.high_alert and insulin.next_due.strftime("%H:%M") == "04:00"


def test_clock_advance_drifts_schedule():
    c = client()
    before = c.get("/bca/snapshot").json()
    c.post("/clock/advance", json={"minutes": 120})
    after = c.get("/bca/snapshot").json()
    assert before != after
    assert after[0]["snapshot_time"] != before[0]["snapshot_time"]


def test_outage_and_restore_are_deterministic():
    results = []
    for _ in range(2):
        c = client()
        pre = {p["patient_id"]: p for p in c.get("/bca/snapshot").json()}
        assert c.post("/outage/start").json()["outage"] is True
        assert c.get("/bca/snapshot").status_code == 503
        changes = c.post("/outage/restore").json()["changes"]
        post = {p["patient_id"]: PatientSnapshot.model_validate(p) for p in c.get("/bca/snapshot").json()}
        kinds = {ch["kind"] for ch in changes}
        assert kinds == {"pharmacy_backloaded_order", "transfer", "allergy_added"}
        transfer = next(ch for ch in changes if ch["kind"] == "transfer")
        assert transfer["patient_id"] != "pt-12"
        assert post[transfer["patient_id"]].bed != pre[transfer["patient_id"]]["bed"]
        allergy = next(ch for ch in changes if ch["kind"] == "allergy_added")
        assert allergy["patient_id"] not in DEMO_CRITICAL | {ch["patient_id"] for ch in changes if ch is not allergy}
        before = {a["substance"] for a in pre[allergy["patient_id"]]["allergies"]}
        assert {a.substance for a in post[allergy["patient_id"]].allergies} - before == {allergy["substance"]}
        results.append(changes)
    assert results[0] == results[1]


def test_default_seed_restore_picks_are_stable():
    """DEMO.md step 6 names these beds; the allergy delta is drawn after the order and transfer picks."""
    changes = {ch["kind"]: ch for ch in SimState(DEFAULT_SEED).restore()}
    assert changes["pharmacy_backloaded_order"]["patient_id"] == "pt-19"
    assert (changes["transfer"]["patient_id"], changes["transfer"]["to_bed"]) == ("pt-05", "ICU 4")
    assert changes["allergy_added"]["patient_id"] not in DEMO_CRITICAL | {"pt-19", "pt-05"}
