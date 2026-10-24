"""DRY_RUN smoke test for every API endpoint: status, contract shape, and an audit event written."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import clock, sim_client
from app.store import get_store, reset_store
from contracts import (Allergy, AuditEvent, CapturedRecord, Discrepancy, DowntimePacket, PatientSnapshot, PolicyAnswer,
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
    clock.reset()
    reset_store()
    from app.main import app
    with TestClient(app) as c:
        yield c
    clock.reset()
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


def test_first_pull_of_fresh_process_is_audited_in_unit_time(api):
    """Fresh store, never-synced unit clock, simulator reachable: the FIRST request is POST /snapshot/pull.
    The pull syncs the unit clock before it audits, so the row is in the simulator era (02:45), not wall-clock."""
    from simulator.state import CLOCK_START
    assert clock._synced is None and not get_store().list_audit()
    assert api.post("/snapshot/pull").status_code == 200
    pull = next(e for e in get_store().list_audit() if e.action == "snapshot.pull")
    assert CLOCK_START <= pull.ts < CLOCK_START + timedelta(minutes=15)


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


def test_capture_audits_x_role(api):
    go_to_downtime(api)
    files = {"file": ("form.png", PNG, "image/png")}
    r = api.post("/capture", files=files, data={"form_type": "vitals_flowsheet"}, headers={"X-Role": "pharmacy"})
    assert r.status_code == 201
    assert events("capture.create")[0].actor_role == "pharmacy"


def test_capture_client_id_is_idempotent(api):
    """Offline queue replay: the same client id twice -> one record, one capture.create audit, same body."""
    go_to_downtime(api)
    form = {"form_type": "vitals_flowsheet", "client_id": "c-0f8e2b9a"}
    first = api.post("/capture", files={"file": ("form.png", PNG, "image/png")}, data=form, headers={"X-Role": "rn"})
    again = api.post("/capture", files={"file": ("form.png", PNG, "image/png")}, data=form, headers={"X-Role": "rn"})
    assert first.status_code == 201 and again.status_code == 200, again.text
    assert again.json() == first.json()
    store = get_store()
    assert [r.record_id for r in store.list_records()] == [first.json()["record_id"]]
    created = events("capture.create")
    assert len(created) == 1 and created[0].details["client_id"] == "c-0f8e2b9a"
    assert len(events("capture.crops")) <= 1
    # A later save (verify/edit path) keeps the id; another id or no id still creates a new record.
    store.save_record(store.get_record(first.json()["record_id"]))
    assert store.record_by_client_id("c-0f8e2b9a").record_id == first.json()["record_id"]
    other = api.post("/capture", files={"file": ("form.png", PNG, "image/png")},
                     data={**form, "client_id": "c-other"}, headers={"X-Role": "rn"})
    plain = api.post("/capture", files={"file": ("form.png", PNG, "image/png")}, data={"form_type": "vitals_flowsheet"})
    assert other.status_code == 201 and plain.status_code == 201
    assert len(store.list_records()) == 3 and len(events("capture.create")) == 3


def test_store_upgrades_a_pre_s7_captured_table(tmp_path):
    import sqlite3

    from app.store.sqlite import Store
    path = str(tmp_path / "old.sqlite")
    sqlite3.connect(path).executescript(
        "CREATE TABLE captured (record_id TEXT PRIMARY KEY, seq INTEGER, body TEXT);").close()
    Store(path)
    Store(path)  # idempotent on reopen
    assert {"client_id", "text_source"} <= {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(captured)")}


def test_store_adds_text_source_to_an_s7_store_and_keeps_old_rows(tmp_path):
    """A store from S7 (client_id, no text_source) gains the column; its old rows read as unknown (None)."""
    import sqlite3

    from app.store.sqlite import Store
    path = str(tmp_path / "s7.sqlite")
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE captured (record_id TEXT PRIMARY KEY, seq INTEGER, body TEXT, client_id TEXT);"
                     "INSERT INTO captured VALUES ('rec-old', 1, '{}', 'c-1');")
    db.commit()
    db.close()
    store = Store(path)
    assert "text_source" in {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(captured)")}
    assert store.text_source("rec-old") is None


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
    simc = TestClient(sim.app)
    simc.post("/clock/advance", json={"minutes": 15})
    go_to_downtime(api)  # 03:00
    capture(api, "vitals_flowsheet")  # SpO2 89 at 02:55, no documented response
    capture(api, "downtime_mar")  # bed 12 heparin: ordered, but inside its frequency window
    capture(api, "downtime_mar", patient_id="pt-02")  # heparin with no pre-downtime order for pt-02
    simc.post("/clock/advance", json={"minutes": 45})  # restore at 03:45: the 02:55 response window closed at 03:25
    # Taken after the window: a pt-12 order inside 02:55-03:25 would count as the documented response (R-REC-02).
    capture(api, "verbal_order", patient_id="pt-12")
    r = api.post("/recovery/restore")
    assert r.status_code == 200, r.text
    found = [Discrepancy.model_validate(d) for d in r.json()]
    rule_ids = {d.rule_id for d in found}
    assert {"R-REC-01", "R-REC-02", "R-REC-03", "R-VO-01", "DIFF-ORDER", "DIFF-ADT", "DIFF-ALLERGY"} <= rule_ids
    assert {d.patient_id for d in found if d.rule_id == "R-REC-01"} == {"pt-02"}
    assert "R-REC-04" not in rule_ids  # the bed 12 MAR's 5000 units matches the heparin order
    allergy = next(ch for ch in sim.state.restore_changes if ch["kind"] == "allergy_added")
    (diff,) = [d for d in found if d.rule_id == "DIFF-ALLERGY"]
    assert diff.patient_id == allergy["patient_id"] and diff.severity.value == "warning"
    assert allergy["substance"] in diff.description and diff.record_id is None
    assert audited("recovery.restore")
    assert len(api.get("/discrepancies").json()) == len(found) and audited("discrepancy.list")


VO_BED7 = "Read back: heparin 5000 units SC every 12 hours, Dr. Okafor, read back confirmed"  # DEMO.md step 4

# DEMO.md step 6: seed --fresh, +15, declare, vitals (Bed 12) + MAR (Bed 2) + verbal order (Bed 7), +45, restore.
# Exactly these, nothing else (rule_id, patient_id, severity).
PLANTED = sorted([
    ("R-REC-01", "pt-02", "critical"),    # heparin on paper, Bed 2 has no heparin order
    ("R-REC-02", "pt-12", "warning"),     # 02:55 SpO2 89, no documented response by 03:25
    ("R-VO-01", "pt-07", "warning"),      # verbal order not co-signed
    ("DIFF-ADT", "pt-05", "info"),        # Bed 5 -> ICU 4
    ("DIFF-ORDER", "pt-19", "warning"),   # pantoprazole backloaded by pharmacy
    ("DIFF-ALLERGY", "pt-16", "warning"),  # cephalexin documented in the restored EHR
])


def scripted_flow(c: TestClient) -> list[Discrepancy]:
    from simulator.state import DEFAULT_SEED
    simc = TestClient(sim.app)
    simc.post("/reset", json={"seed": DEFAULT_SEED})
    simc.post("/clock/advance", json={"minutes": 15})
    go_to_downtime(c)
    capture(c, "vitals_flowsheet")
    capture(c, "downtime_mar", patient_id="pt-02")
    r = c.post("/capture", data={"form_type": "verbal_order", "readback_text": VO_BED7, "patient_id": "pt-07"},
               headers={"X-Role": "rn"})
    assert r.status_code == 201, r.text
    simc.post("/clock/advance", json={"minutes": 45})
    r = c.post("/recovery/restore")
    assert r.status_code == 200, r.text
    return [Discrepancy.model_validate(d) for d in r.json()]


def test_scripted_flow_yields_exactly_the_planted_set(api):
    found = scripted_flow(api)
    assert sorted((d.rule_id, d.patient_id, d.severity.value) for d in found) == PLANTED
    assert len({d.discrepancy_id for d in found}) == len(PLANTED)


def test_rec04_dose_mismatch_through_restore(api):
    """A reviewer-corrected MAR dose (2500 units) against Bed 12's heparin 5000 units order."""
    simc = TestClient(sim.app)
    simc.post("/clock/advance", json={"minutes": 15})
    go_to_downtime(api)
    rec = capture(api, "downtime_mar")
    api.patch(f"/backload/{rec.record_id}/fields/dose", json={"value": "2500 units"}, headers={"X-Role": "rn"})
    found = [Discrepancy.model_validate(d) for d in api.post("/recovery/restore").json()]
    (hit,) = [d for d in found if d.rule_id == "R-REC-04"]
    assert (hit.patient_id, hit.record_id, hit.severity.value) == ("pt-12", rec.record_id, "critical")
    assert "2500 units" in hit.description and "5000 units" in hit.description


def test_diff_restored_allergy_added_and_removed():
    from app.api.backload import diff_restored
    from simulator.state import load_patients
    pre = {p.patient_id: p for p in load_patients()}
    p = pre["pt-09"]  # latex
    new = Allergy(substance="Cephalexin", reaction="rash", severity="moderate")
    added = p.model_copy(update={"allergies": [*p.allergies, new]})
    removed = p.model_copy(update={"allergies": []})
    same_case = p.model_copy(update={"allergies": [a.model_copy(update={"substance": "LATEX"}) for a in p.allergies]})
    hits = {k: [d for d in diff_restored(pre, {**pre, "pt-09": v}) if d.rule_id == "DIFF-ALLERGY"]
            for k, v in (("added", added), ("removed", removed), ("same", same_case))}
    assert [d.patient_id for d in hits["added"]] == ["pt-09"] and "Cephalexin" in hits["added"][0].description
    assert len(hits["removed"]) == 1 and "missing from the restored EHR" in hits["removed"][0].description
    assert hits["same"] == []  # case-only spelling difference is not a changed allergy list
    assert diff_restored(pre, pre) == []


def test_restore_inside_response_window_omits_rec02(api):
    """Restored at 03:10, the 02:55 vitals still have until 03:25 for a documented response."""
    simc = TestClient(sim.app)
    simc.post("/clock/advance", json={"minutes": 15})
    go_to_downtime(api)
    capture(api, "vitals_flowsheet")
    simc.post("/clock/advance", json={"minutes": 10})
    r = api.post("/recovery/restore")
    assert r.status_code == 200, r.text
    assert "R-REC-02" not in {d["rule_id"] for d in r.json()}


def test_digest(api):
    go_to_downtime(api)
    capture(api, "vitals_flowsheet")
    r = api.get("/digest")
    assert r.status_code == 200
    d = ShiftDigest.model_validate(r.json())
    assert len(d.per_patient) == 12 and d.unit_summary and audited("digest.generate")


def _digest_lines(d: ShiftDigest) -> list:
    return [i for p in d.per_patient for s in ("situation", "background", "assessment", "recommendation")
            for i in getattr(p.sbar, s)]


def test_digest_verified_only_display_names_and_provenance(api):
    """Scripted flow, then verify + enter the Bed 12 vitals only: the MAR and verbal order stay needs_review."""
    import re
    found = scripted_flow(api)
    recs = {r["form_type"]: r for g in api.get("/backload").json().values() for r in g}
    vitals, mar, vo = (recs[k]["record_id"] for k in ("vitals_flowsheet", "downtime_mar", "verbal_order"))
    assert api.post(f"/backload/{vitals}/verify", json={"verified_by": "RN Test"}).status_code == 200
    assert api.post(f"/backload/{vitals}/entered").status_code == 200
    d = ShiftDigest.model_validate(api.get("/digest").json())
    assert len(d.per_patient) == 12
    lines = _digest_lines(d)
    text = " ".join([d.unit_summary, *d.open_items, *(i.text for i in lines)])
    # display names, never raw ids
    assert not re.search(r"\bpt-\d+|\brec-[0-9a-f]+|\bspo2\b|resp_rate|vitals_flowsheet|downtime_mar", text)
    assert "Bed 12" in text and "SpO2 89 (out of range)" in text
    # open items lead with the patient; the R-REC-02 description carries display labels from emission
    bed12 = next(p for p in get_store().latest_snapshot() if p.patient_id == "pt-12")
    assert any(i.startswith(f"Bed 12 · {bed12.name} — R-REC-02 (warning): Out-of-range vitals (Resp rate 27, SpO2 89)")
               for i in d.open_items), d.open_items
    # provenance: every line cites resolvable refs; verified record and audit events are cited by id / seq
    refs = {i.source_path for i in lines}
    seqs = {str(s) for s, _ in get_store().audit_log()}
    for i in lines:
        for ref in i.source_path.split(","):
            kind, _, rest = ref.partition(".")
            assert kind in ("snapshot", "captured", "discrepancy", "audit"), ref
            assert kind != "audit" or rest in seqs
    assert f"captured.{vitals}" in refs and any(r.startswith("audit.") for r in refs)
    # verified-only: the needs_review MAR / verbal order contribute no content and are never cited
    assert f"captured.{mar}" not in refs and f"captured.{vo}" not in refs
    assert "heparin" not in text.lower() and "Okafor" not in text
    held = {f"discrepancy.{x.discrepancy_id}" for x in found if x.record_id in (mar, vo)}
    assert held and not held & refs
    bed2 = next(p for p in d.per_patient if p.patient_id == "pt-02")
    assert any("awaiting verification" in i.text for i in bed2.sbar.recommendation)
    assert next(e for e in events("digest.generate")).details["guardrail"]["status"] == "passed"


@pytest.mark.parametrize("raw, shown", [
    ("SAFETY: Bed 12 SpO2 89, no response. (discrepancy.d-7045e5ab3c76)", "SAFETY: Bed 12 SpO2 89, no response."),
    ("VERIFY: MAR for pt-02 awaits verification (audit.24)", "VERIFY: MAR for Bed 2 awaits verification"),
    ("Bed 12 vitals [source: captured.rec-077b971694aa, snapshot.pt-12.admitting_dx].", "Bed 12 vitals."),
    ("see audit.7 and the audit log.", "see and the audit log."),
])
def test_display_strips_provenance_refs_from_prose(raw, shown):
    """Live Sonnet (S6) appended refs to open items; refs belong in source_path, never in prose."""
    from app.ai.digest import display
    assert display(raw, {"pt-02": "Bed 2", "pt-12": "Bed 12"}) == shown


def test_digest_drops_lines_without_resolvable_provenance(api, monkeypatch):
    import json as _json
    from app.ai import client as ai
    go_to_downtime(api)
    real = ai.call

    def fake(job, prompt, user, **kw):
        if job != "digest":
            return real(job, prompt, user, **kw)
        return real(job, prompt, user, **{**kw, "mock": lambda: _json.dumps({
            "unit_summary": "pt-12 spo2 fine", "open_items": ["check pt-12"],
            "per_patient": [{"patient_id": "pt-12", "sbar": {
                "situation": [{"text": "pt-12 roster", "source_path": "snapshot.pt-12.admitting_dx"},
                              {"text": "invented", "source_path": "snapshot.pt-12.made_up"},
                              {"text": "no source", "source_path": ""}]}}]})})

    monkeypatch.setattr(ai, "call", fake)
    d = ShiftDigest.model_validate(api.get("/digest").json())
    bed12 = next(p for p in d.per_patient if p.patient_id == "pt-12")
    assert [(i.text, i.source_path) for i in bed12.sbar.situation] == [("Bed 12 roster", "snapshot.pt-12.admitting_dx")]
    assert d.unit_summary == "Bed 12 SpO2 fine" and d.open_items == ["check Bed 12"]
    assert len(d.per_patient) == 12  # patients the model left out get the deterministic SBAR


def _pt(pid: str, bed: str, name: str) -> PatientSnapshot:
    return PatientSnapshot.model_validate({
        "patient_id": pid, "mrn": f"SYN-{pid}", "name": name, "dob": "1960-01-01", "age": 66, "sex": "female",
        "bed": bed, "admitting_dx": "test", "code_status": "Full code", "allergies": [], "medications": [],
        "fall_risk": "low", "snapshot_time": "2026-10-24T02:47:00+00:00"})


def test_canned_open_items_lead_with_patient_sorted_by_severity():
    """Deterministic open items: "Bed · Name — RULE (severity): …", critical -> warning -> info, stable within a
    severity; a discrepancy whose patient is not on the roster reads "Unit — "."""
    from app.ai.digest import _canned, facts
    pts = [_pt("pt-03", "Bed 3", "Ana Ruiz"), _pt("pt-16", "Bed 16", "Lee Park")]

    def d(n, rule, pid, sev):
        return Discrepancy(discrepancy_id=f"d-{n}", rule_id=rule, patient_id=pid, description=f"item {n}",
                           severity=sev)

    discs = [d(1, "DIFF-ADT", "pt-03", "info"), d(2, "R-REC-02", "pt-16", "warning"),
             d(3, "R-REC-01", "pt-03", "critical"), d(4, "DIFF-ORDER", "pt-16", "warning"),
             d(5, "DIFF-ALLERGY", "pt-99", "info")]
    assert _canned(pts, facts(pts, [], discs, []), 0)["open_items"] == [
        "Bed 3 · Ana Ruiz — R-REC-01 (critical): item 3",
        "Bed 16 · Lee Park — R-REC-02 (warning): item 2",
        "Bed 16 · Lee Park — DIFF-ORDER (warning): item 4",
        "Bed 3 · Ana Ruiz — DIFF-ADT (info): item 1",
        "Unit — DIFF-ALLERGY (info): item 5",
    ]


def test_incident_correction_row_renders_blank_not_none():
    from app.ai.incident import timeline
    ts = datetime(2026, 10, 24, 3, 0)
    edits = [(5, AuditEvent(event_id="e5", ts=ts, actor_role="rn", action="backload.edit", target_type="captured_record",
                            target_id="rec-x", details={"field": "initials", "before": None, "after": "JK"})),
             (6, AuditEvent(event_id="e6", ts=ts, actor_role="rn", action="backload.edit", target_type="captured_record",
                            target_id="rec-x", details={"field": "spo2", "before": "89", "after": ""}))]
    assert [r["detail"] for r in timeline(edits, [], [], [])] == ["Paper record: Initials blank → JK",
                                                                  "Paper record: SpO2 89 → blank"]


def test_incident_timeline_filtered_in_seq_order_with_summary(api):
    scripted_flow(api)
    recs = {r["form_type"]: r for g in api.get("/backload").json().values() for r in g}
    vitals = recs["vitals_flowsheet"]["record_id"]
    api.post(f"/backload/{vitals}/verify", json={"verified_by": "RN Test"})
    api.post(f"/backload/{vitals}/entered")
    api.get("/audit"), api.get("/packets"), api.get(f"/blobs/{recs['vitals_flowsheet']['image_ref']}")  # view noise
    r = api.get("/incident")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"generated_at", "outage_started_at", "restored_at", "model_id", "summary", "guardrail",
                         "cost", "counts", "timeline"}
    rows = body["timeline"]
    assert all(set(x) == {"seq", "ts", "role", "action", "event", "patient", "detail"} for x in rows)
    actions = {x["action"] for x in rows}
    assert actions == {"downtime.start", "capture.create", "backload.verify", "backload.entered", "recovery.restore"}
    assert not any(a.endswith((".view", ".list")) or a in ("ai.call", "rules.evaluate", "snapshot.pull")
                   for a in actions)
    assert [x["seq"] for x in rows] == sorted(x["seq"] for x in rows)  # store sequence order
    events_in_order = [x["event"] for x in rows]
    assert events_in_order[:2] == ["Downtime declared", "Downtime packets generated"]
    assert events_in_order.count("Paper form captured") == 3
    restore_at = events_in_order.index("Systems restored")
    assert events_in_order.index("Verified") > restore_at and events_in_order[-1] == "Marked entered in the EHR"
    discs = [x for x in rows if x["event"].startswith("Discrepancy ")]
    assert sorted(x["event"].split()[1] for x in discs) == sorted(rid for rid, _, _ in PLANTED)
    assert discs[0]["event"] == "Discrepancy R-REC-01" and discs[0]["patient"] == "Bed 2"  # critical first
    assert body["counts"] == {"captures": 3, "verified": 1, "entered": 1, "corrections": 0, "discrepancies": 6,
                              "critical": 1}
    assert len(body["summary"]) == 2 and all(body["summary"])  # DRY_RUN canned two paragraphs
    assert "03:00" in body["summary"][0] and "03:45" in body["summary"][0]
    assert body["guardrail"]["status"] == "passed" and body["model_id"].startswith("dry-run:us.anthropic")
    text = " ".join([*body["summary"], *(f"{x['patient'] or ''} {x['detail']}" for x in rows)])
    assert not re.search(r"\bpt-\d+|\brec-[0-9a-f]+|\bspo2\b|vitals_flowsheet", text)
    assert audited("incident.view")
    again = api.get("/incident").json()
    assert again["timeline"] == rows  # its own incident.view and ai.call rows never enter the timeline


def test_restore_status_audit_and_digest_on_unit_clock(api):
    """DEMO.md: +15 min, declare (03:00), +45 min, restore (03:45). Every stamp is unit time, never wall clock."""
    simc = TestClient(sim.app)
    simc.post("/clock/advance", json={"minutes": 15})
    go_to_downtime(api)
    simc.post("/clock/advance", json={"minutes": 45})
    assert api.get("/status").json()["restored_at"] is None
    assert api.post("/recovery/restore").status_code == 200
    s = api.get("/status").json()
    restored = datetime.fromisoformat(s["restored_at"])
    assert restored == sim.state.now == datetime.fromisoformat(s["now"])
    close = timedelta(seconds=5)
    assert abs(events("downtime.start")[-1].ts - (restored - timedelta(minutes=45))) < close
    assert abs(events("recovery.restore")[-1].ts - restored) < close
    d = ShiftDigest.model_validate(api.get("/digest").json())
    assert d.shift_window.end == restored and abs(d.generated_at - restored) < close
    start = restored - timedelta(minutes=45)
    assert all(start - close < datetime.fromisoformat(c["ts"]) < restored + close
               for c in api.get("/costs").json()["calls"])


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
