"""DRY_RUN smoke test for every API endpoint: status, contract shape, and an audit event written."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import sim_client
from app.store import get_store, reset_store
from contracts import (AuditEvent, CapturedRecord, Discrepancy, DowntimePacket, PatientSnapshot, PolicyAnswer,
                       ShiftDigest)
from simulator import main as sim

PNG = b"\x89PNG\r\n\x1a\nfake-form-photo"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("STORE_PATH", str(tmp_path / "test.sqlite"))
    monkeypatch.setenv("SNAPSHOT_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("SYNC_EXPORT_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(sim_client, "make_client", lambda: TestClient(sim.app))
    sim.state.reset(7)
    reset_store()
    from app.main import app
    with TestClient(app) as c:
        yield c
    reset_store()


def audited(action: str) -> bool:
    return any(e.action == action for e in get_store().list_audit(1000))


def go_to_downtime(c: TestClient) -> list[DowntimePacket]:
    assert c.post("/snapshot/pull").status_code == 200
    r = c.post("/downtime/start")
    assert r.status_code == 200, r.text
    return [DowntimePacket.model_validate(p) for p in r.json()]


def capture(c: TestClient, form_type: str, **extra) -> CapturedRecord:
    files = {"file": ("form.png", PNG, "image/png" if form_type != "verbal_order" else "audio/webm")}
    r = c.post("/capture", files=files, data={"form_type": form_type, **extra}, headers={"X-Role": "rn"})
    assert r.status_code == 201, r.text
    return CapturedRecord.model_validate(r.json())


def test_snapshot_pull_and_view(api):
    r = api.post("/snapshot/pull")
    assert r.status_code == 200
    assert len([PatientSnapshot.model_validate(p) for p in r.json()]) == 12
    assert audited("snapshot.pull")
    assert len(api.get("/snapshot").json()) == 12 and audited("snapshot.view")
    s = api.get("/status").json()
    assert s["phase"] == "before" and s["patients"] == 12 and audited("status.view")


def test_patients_list(api):
    assert api.get("/patients").json() == []
    api.post("/snapshot/pull")
    r = api.get("/patients")
    assert r.status_code == 200 and audited("patient.list")
    patients = [PatientSnapshot.model_validate(p) for p in r.json()]
    assert len(patients) == 12 and {"pt-12"} <= {p.patient_id for p in patients}
    assert all(p.name and p.bed for p in patients)


def test_rules_current_on_pull_and_downtime_start(api):
    assert api.get("/rules/current").json() == {"evaluated_at": None, "patients": {}}
    TestClient(sim.app).post("/clock/advance", json={"minutes": 15})  # DEMO.md: unit clock 03:00
    api.post("/snapshot/pull")
    r = api.get("/rules/current").json()
    assert r["evaluated_at"] == api.get("/status").json()["now"] and audited("rules.view")
    fired: dict[str, set[str]] = {}
    for pid, hits in r["patients"].items():
        for h in hits:
            fired.setdefault(h["rule_id"], set()).add(pid)
    assert {k: fired.get(k, set()) for k in ("R-ALG-01", "R-DUP-01", "R-DUE-01")} == {
        "R-ALG-01": {"pt-03", "pt-05"}, "R-DUP-01": {"pt-07"}, "R-DUE-01": {"pt-08", "pt-11", "pt-12"}}
    assert fired.get("R-STALE-01") == {"pt-14"}  # the one seeded lagging feed (readiness panel)
    due = [h for h in r["patients"]["pt-12"] if h["rule_id"] == "R-DUE-01"]
    assert due and all(h["due_at"] and " min" not in h["text"] for h in due)
    go_to_downtime(api)
    triggers = [e.details.get("trigger") for e in get_store().list_audit(1000) if e.action == "rules.evaluate"]
    assert sorted(triggers) == ["downtime.start", "snapshot.pull", "snapshot.pull"]


def test_downtime_start_packets_have_provenance(api):
    packets = go_to_downtime(api)
    assert len(packets) == 12 and audited("downtime.start") and audited("ai.call")
    for p in packets:
        lines = [i for sec in ("situation", "background", "assessment", "recommendation") for i in getattr(p.sbar, sec)]
        assert lines and all(i.source_path for i in lines)
    assert api.get("/bca/snapshot").status_code == 404  # simulator is a separate service
    assert len(api.get("/packets").json()) == 12 and audited("packet.list")


def test_status_outage_id_null_before_downtime(api):
    api.post("/snapshot/pull")
    s = api.get("/status").json()
    assert s["phase"] == "before" and "outage_id" in s and s["outage_id"] is None


def test_outage_id_is_fresh_per_reseeded_outage(api):
    """Each reseed restarts the unit clock at 02:45, so outage_started_at repeats across rehearsals;
    outage_id must not (the web checklist keys its saved check marks by it)."""
    go_to_downtime(api)
    first = api.get("/status").json()
    get_store().clear()  # `simulator.seed --fresh`
    sim.state.reset(7)
    assert api.get("/status").json()["outage_id"] is None
    go_to_downtime(api)
    second = api.get("/status").json()
    assert first["outage_started_at"] == second["outage_started_at"]  # the collision the id exists to avoid
    assert first["outage_id"] and second["outage_id"] and first["outage_id"] != second["outage_id"]


def test_downtime_start_bounded_concurrency(api, monkeypatch):
    import threading
    import time

    from app.ai import packets as packets_mod
    real, lock, state = packets_mod.generate_packet, threading.Lock(), {"now": 0, "max": 0}

    def tracked(*args, **kwargs):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.05)
        try:
            return real(*args, **kwargs)
        finally:
            with lock:
                state["now"] -= 1

    monkeypatch.setattr(packets_mod, "generate_packet", tracked)
    monkeypatch.setenv("PACKET_CONCURRENCY", "4")
    packets = go_to_downtime(api)
    assert len(packets) == 12 and len({p.patient_id for p in packets}) == 12
    assert 1 < state["max"] <= 4
    sim_now = api.get("/status").json()["now"]
    assert all(p.generated_at.isoformat() == sim_now for p in packets)  # sim clock, not wall clock
    assert len([c for c in api.get("/costs").json()["calls"] if c["job"] == "packet"]) == 12


def test_patient_packet_bed12(api):
    go_to_downtime(api)
    r = api.get("/patients/pt-12/packet")
    assert r.status_code == 200
    packet = DowntimePacket.model_validate(r.json())
    assert "R-DUE-01" in {a.rule_id for a in packet.alerts}
    text = " ".join(i.text for i in packet.sbar.situation + packet.sbar.background)
    assert "penicillin" in text and "insulin lispro" in text and "04:00" in text
    assert audited("packet.view")
    assert api.get("/patients/nope/packet").status_code == 404


def test_policy_ask_cites_4_2(api):
    r = api.post("/policy/ask", json={"question": "How do I get a stat lab with the order system down?"})
    assert r.status_code == 200
    a = PolicyAnswer.model_validate(r.json())
    assert a.citations[0].section_id == "4.2" and a.grounded and a.guardrail.status == "passed"
    assert audited("policy.ask")


STAT_QUOTE = "the RN completes a red downtime requisition marked \"STAT\""


@pytest.mark.parametrize("section_id, quote, expected", [
    ("4.2.1", STAT_QUOTE, True),             # paragraph-level id resolves to its 4.2 header
    ("4.2", STAT_QUOTE, True),               # header-level id still works
    ("9.9.9", STAT_QUOTE, False),            # fabricated id with no parsed ancestor
    ("4.2.9", STAT_QUOTE, False),            # fabricated paragraph under a real header
    ("4.2.1", "use the pneumatic tube for all stat specimens", False),  # real id, fabricated quote
])
def test_policy_grounded_paragraph_citations(section_id, quote, expected):
    from app.ai.policy import grounded
    from contracts import Citation
    assert grounded([Citation(section_id=section_id, title="Stat labs", quote=quote)]) is expected


def test_capture_vitals_agreement(api):
    go_to_downtime(api)
    rec = capture(api, "vitals_flowsheet")
    agreements = [f.agreement.value for f in rec.fields]
    flags = {f.name: f.flag.value for f in rec.fields if f.flag}
    assert agreements.count("agree") == 8
    assert flags["resp_rate"] == "disagreement" and flags["pain_score"] == "unreadable"
    assert rec.patient_id == "pt-12" and rec.backload_group.value == "nursing_vitals"
    assert audited("capture.create")
    assert api.get(f"/blobs/{rec.image_ref}").content == PNG and audited("blob.view")


def test_capture_typed_verbal_order(api):
    go_to_downtime(api)
    typed = "Insulin lispro 6 units subcut now per Dr. Hale, read back and confirmed"
    r = api.post("/capture", data={"form_type": "verbal_order", "readback_text": typed, "patient_id": "pt-12"},
                 headers={"X-Role": "rn"})
    assert r.status_code == 201, r.text
    rec = CapturedRecord.model_validate(r.json())
    f = {x.name: x.value for x in rec.fields}
    assert f["medication"] == "Insulin lispro" and f["dose"] == "6 units" and f["route"] == "subcut"
    assert rec.audio_ref and api.get(f"/blobs/{rec.audio_ref}").text == typed
    legacy = api.post("/capture", data={"form_type": "verbal_order", "transcript": "heparin 5000 units SC q12h"})
    assert legacy.status_code == 201 and legacy.json()["fields"][0]["value"] == "heparin"
    assert api.post("/capture", data={"form_type": "verbal_order"}).status_code == 422
    assert api.post("/capture", data={"form_type": "vitals_flowsheet", "readback_text": typed}).status_code == 422


def test_backload_verify_entered(api):
    go_to_downtime(api)
    rec = capture(api, "downtime_mar")
    groups = api.get("/backload").json()
    assert set(groups) == {"nursing_med_admin", "nursing_vitals", "nursing_io", "pharmacy_orders", "unit_registration"}
    assert groups["nursing_med_admin"][0]["record_id"] == rec.record_id and audited("backload.view")
    assert api.post(f"/backload/{rec.record_id}/entered").status_code == 409
    # The DRY_RUN MAR read is the fixture's heparin: high-alert, so two people verify before entry.
    first = api.post(f"/backload/{rec.record_id}/verify", json={"verified_by": "RN Test"})
    assert first.status_code == 200 and first.json()["status"] == "needs_review"
    assert api.post(f"/backload/{rec.record_id}/entered").status_code == 409
    v = api.post(f"/backload/{rec.record_id}/verify", json={"verified_by": "Pharm Test"})
    assert v.status_code == 200 and v.json()["status"] == "verified" and audited("backload.verify")
    e = api.post(f"/backload/{rec.record_id}/entered")
    assert e.status_code == 200 and CapturedRecord.model_validate(e.json()).status.value == "entered"
    assert audited("backload.entered")


def test_recovery_restore_discrepancies(api):
    go_to_downtime(api)
    capture(api, "vitals_flowsheet")
    capture(api, "downtime_mar")  # bed 12 heparin: ordered, but inside its frequency window
    capture(api, "downtime_mar", patient_id="pt-02")  # heparin with no pre-downtime order for pt-02
    capture(api, "verbal_order", patient_id="pt-12")
    r = api.post("/recovery/restore")
    assert r.status_code == 200, r.text
    found = [Discrepancy.model_validate(d) for d in r.json()]
    rule_ids = {d.rule_id for d in found}
    assert {"R-REC-01", "R-REC-02", "R-REC-03", "R-VO-01", "DIFF-ORDER", "DIFF-ADT"} <= rule_ids
    assert {d.patient_id for d in found if d.rule_id == "R-REC-01"} == {"pt-02"}
    assert audited("recovery.restore")
    assert len(api.get("/discrepancies").json()) == len(found) and audited("discrepancy.list")


def test_digest(api):
    go_to_downtime(api)
    capture(api, "vitals_flowsheet")
    r = api.get("/digest")
    assert r.status_code == 200
    d = ShiftDigest.model_validate(r.json())
    assert len(d.per_patient) == 12 and d.unit_summary and audited("digest.generate")


def test_audit_costs_sync(api):
    go_to_downtime(api)
    events = [AuditEvent.model_validate(e) for e in api.get("/audit").json()]
    assert events and audited("audit.view")
    c = api.get("/costs").json()
    assert c["dry_run"] is True and len(c["calls"]) >= 12 and c["total_usd"] == 0.0 and audited("costs.view")
    assert all(call["model_id"].startswith("dry-run:us.") for call in c["calls"])
    assert sum(m["calls"] for m in c["models"].values()) == len(c["calls"])
    assert set(c["models"]) == {call["model_id"] for call in c["calls"]}
    s = api.post("/sync").json()
    assert s["mirror"] == "local" and s["pushed"] > 0 and audited("sync.push")


# ------------------------------------------------------------------ S4: edits + two-person verify

def events(action: str) -> list[AuditEvent]:
    return [e for e in reversed(get_store().list_audit(1000)) if e.action == action]


def heparin_order(c: TestClient) -> CapturedRecord:
    r = c.post("/capture", data={"form_type": "verbal_order", "readback_text": "heparin 5000 units SC q12h, read back",
                                 "patient_id": "pt-12"}, headers={"X-Role": "rn"})
    assert r.status_code == 201, r.text
    return CapturedRecord.model_validate(r.json())


def test_high_alert_needs_two_different_verifiers(api):
    go_to_downtime(api)
    rec = heparin_order(api)
    v = api.get(f"/backload/{rec.record_id}/verification").json()
    assert v["high_alert_medication"] == "heparin" and v["required"] == 2 and v["verifications"] == []
    first = api.post(f"/backload/{rec.record_id}/verify", json={}, headers={"X-Role": "rn"})
    assert first.status_code == 200 and first.json()["status"] == "needs_review"
    assert first.headers["X-Verifications"] == "1/2"
    assert api.post(f"/backload/{rec.record_id}/entered").status_code == 409  # 1 of 2 is not verified
    same = api.post(f"/backload/{rec.record_id}/verify", json={}, headers={"X-Role": "rn"})
    assert same.status_code == 409 and "different person" in same.json()["detail"]
    assert events("backload.verify_rejected")[-1].details["attempt"]["role"] == "rn"
    assert len(api.get(f"/backload/{rec.record_id}/verification").json()["verifications"]) == 1
    second = api.post(f"/backload/{rec.record_id}/verify", json={}, headers={"X-Role": "pharmacy"})
    assert second.status_code == 200 and second.headers["X-Verifications"] == "2/2"
    done = CapturedRecord.model_validate(second.json())
    assert done.status.value == "verified" and done.verified_by == "rn + pharmacy"
    steps = [(e.actor_role, e.details["step"], e.details["required"]) for e in events("backload.verify")]
    assert steps == [("rn", 1, 2), ("pharmacy", 2, 2)]
    assert events("backload.verify")[-1].details["first_verifier"]["role"] == "rn"
    assert api.post(f"/backload/{rec.record_id}/verify", headers={"X-Role": "unit_clerk"}).status_code == 409
    assert api.post(f"/backload/{rec.record_id}/entered").status_code == 200


def test_same_named_verifier_rejected_across_roles_and_two_named_rns_accepted(api):
    go_to_downtime(api)
    rec = heparin_order(api)
    api.post(f"/backload/{rec.record_id}/verify", json={"verified_by": "Priya N"}, headers={"X-Role": "rn"})
    again = api.post(f"/backload/{rec.record_id}/verify", json={"verified_by": "priya n"}, headers={"X-Role": "pharmacy"})
    assert again.status_code == 409
    ok = api.post(f"/backload/{rec.record_id}/verify", json={"verified_by": "Sam K"}, headers={"X-Role": "rn"})
    assert ok.status_code == 200 and ok.json()["verified_by"] == "Priya N + Sam K"


def test_non_high_alert_single_verify(api):
    go_to_downtime(api)
    rec = capture(api, "vitals_flowsheet")
    assert api.get(f"/backload/{rec.record_id}/verification").json()["required"] == 1
    v = api.post(f"/backload/{rec.record_id}/verify", headers={"X-Role": "rn"})
    assert v.status_code == 200 and v.json()["status"] == "verified" and v.json()["verified_by"] == "rn"


def test_dry_run_mar_read_matches_fixture(api):
    import json
    from pathlib import Path
    truth = json.loads((Path(__file__).resolve().parent.parent / "data/forms/fixtures/downtime_mar-01.json").read_text())
    go_to_downtime(api)
    rec = capture(api, "downtime_mar")
    assert {f.name: f.value for f in rec.fields} == truth["fields"]  # crops never contradict the values
    assert all(f.agreement == "agree" for f in rec.fields if f.value is not None)  # both mock readers agree
    assert api.get(f"/backload/{rec.record_id}/verification").json()["required"] == 2  # two-verify, no edit


def test_edit_records_before_after_and_can_change_high_alert(api):
    go_to_downtime(api)
    rec = capture(api, "downtime_mar")  # DRY_RUN mock reads the fixture's heparin: two verifies
    assert api.get(f"/backload/{rec.record_id}/verification").json()["required"] == 2
    r = api.patch(f"/backload/{rec.record_id}/fields/medication", json={"value": " ondansetron "},
                  headers={"X-Role": "rn"})
    assert r.status_code == 200
    f = {x["name"]: x for x in r.json()["fields"]}
    assert f["medication"]["value"] == "ondansetron" and f["medication"]["confidence"] == 1.0
    assert r.json()["reads"]["bedrock"]["fields"]["medication"] == "heparin"  # original read kept
    ev = events("backload.edit")[-1]
    assert ev.actor_role == "rn" and ev.details["before"] == "heparin" and ev.details["after"] == "ondansetron"
    assert api.get(f"/backload/{rec.record_id}/verification").json()["required"] == 1
    api.patch(f"/backload/{rec.record_id}/fields/medication", json={"value": "heparin"}, headers={"X-Role": "rn"})
    assert api.get(f"/backload/{rec.record_id}/verification").json()["required"] == 2
    # an edit after the first of two verifications resets it
    api.post(f"/backload/{rec.record_id}/verify", headers={"X-Role": "rn"})
    api.patch(f"/backload/{rec.record_id}/fields/dose", json={"value": "2500 units"}, headers={"X-Role": "rn"})
    assert events("backload.edit")[-1].details["verification_reset"] is True
    assert api.get(f"/backload/{rec.record_id}/verification").json()["verifications"] == []
    assert api.patch(f"/backload/{rec.record_id}/fields/nope", json={"value": "x"}).status_code == 404


def test_edit_flags_and_locks_after_verify(api):
    go_to_downtime(api)
    rec = capture(api, "vitals_flowsheet")
    r = api.patch(f"/backload/{rec.record_id}/fields/pain_score", json={"value": "3"}).json()
    assert {x["name"]: x for x in r["fields"]}["pain_score"]["flag"] is None  # human read it: no longer unreadable
    r = api.patch(f"/backload/{rec.record_id}/fields/spo2", json={"value": "85"}).json()
    assert {x["name"]: x for x in r["fields"]}["spo2"]["flag"] == "out_of_range"
    r = api.patch(f"/backload/{rec.record_id}/fields/resp_rate", json={"value": "1O"}).json()  # letter O
    assert {x["name"]: x for x in r["fields"]}["resp_rate"]["flag"] == "out_of_range"
    r = api.patch(f"/backload/{rec.record_id}/fields/heart_rate", json={"value": ""}).json()
    hr = {x["name"]: x for x in r["fields"]}["heart_rate"]
    assert hr["value"] is None and hr["flag"] == "unreadable"
    assert api.post(f"/backload/{rec.record_id}/verify").status_code == 200
    locked = api.patch(f"/backload/{rec.record_id}/fields/spo2", json={"value": "95"})
    assert locked.status_code == 409


def test_capture_serves_crop_blobs(api):
    from app.capture.crops import crop_ref
    from pathlib import Path
    go_to_downtime(api)
    jpg = (Path(__file__).resolve().parent.parent / "data/forms/fixtures/downtime_mar-01.jpg").read_bytes()
    r = api.post("/capture", files={"file": ("mar.jpg", jpg, "image/jpeg")}, data={"form_type": "downtime_mar"},
                 headers={"X-Role": "rn"})
    rec = CapturedRecord.model_validate(r.json())
    crop = api.get(f"/blobs/{crop_ref(rec.record_id, 'medication')}")
    assert crop.status_code == 200 and crop.headers["content-type"] == "image/jpeg" and crop.content[:2] == b"\xff\xd8"
