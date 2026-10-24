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


def test_downtime_start_packets_have_provenance(api):
    packets = go_to_downtime(api)
    assert len(packets) == 12 and audited("downtime.start") and audited("ai.call")
    for p in packets:
        lines = [i for sec in ("situation", "background", "assessment", "recommendation") for i in getattr(p.sbar, sec)]
        assert lines and all(i.source_path for i in lines)
    assert api.get("/bca/snapshot").status_code == 404  # simulator is a separate service
    assert len(api.get("/packets").json()) == 12 and audited("packet.list")


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
    v = api.post(f"/backload/{rec.record_id}/verify", json={"verified_by": "RN Test"})
    assert v.status_code == 200 and v.json()["status"] == "verified" and audited("backload.verify")
    e = api.post(f"/backload/{rec.record_id}/entered")
    assert e.status_code == 200 and CapturedRecord.model_validate(e.json()).status.value == "entered"
    assert audited("backload.entered")


def test_recovery_restore_discrepancies(api):
    go_to_downtime(api)
    capture(api, "vitals_flowsheet")
    capture(api, "downtime_mar")
    capture(api, "verbal_order", patient_id="pt-12")
    r = api.post("/recovery/restore")
    assert r.status_code == 200, r.text
    found = [Discrepancy.model_validate(d) for d in r.json()]
    rule_ids = {d.rule_id for d in found}
    assert {"R-REC-01", "R-REC-02", "R-VO-01", "DIFF-ORDER", "DIFF-ADT"} <= rule_ids
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
