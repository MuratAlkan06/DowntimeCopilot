"""Contract round-trips, frozen enum values, and schema-export drift."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from contracts import (
    TOP_LEVEL_MODELS,
    Agreement,
    AuditEvent,
    BackloadGroup,
    CapturedRecord,
    Discrepancy,
    DiscrepancyStatus,
    DowntimePacket,
    FieldFlag,
    FormType,
    PatientSnapshot,
    PolicyAnswer,
    RecordStatus,
    Severity,
    ShiftDigest,
)
from contracts.export_schemas import SCHEMA_DIR, render

T0 = datetime(2026, 10, 24, 2, 47, tzinfo=timezone.utc)
COST = {"tokens_in": 1200, "tokens_out": 340, "usd": 0.0071}
GUARDRAIL = {"status": "passed", "reasons": []}
SBAR = {
    "situation": [{"text": "Bed 12, insulin due 04:00 (high-alert)", "source_path": "medications[0].next_due"}],
    "background": [{"text": "Penicillin allergy: hives", "source_path": "allergies[0]"}],
    "assessment": [{"text": "Fall risk high", "source_path": "fall_risk"}],
    "recommendation": [{"text": "Double-check insulin at 04:00", "source_path": "medications[0].high_alert"}],
}


def snapshot() -> PatientSnapshot:
    return PatientSnapshot(
        patient_id="p-012",
        mrn="MRN0000012",
        name="Synthetic Patient Twelve",
        dob=date(1958, 3, 14),
        age=68,
        sex="F",
        bed="12",
        admitting_dx="Community-acquired pneumonia",
        code_status="Full code",
        isolation="droplet",
        allergies=[{"substance": "penicillin", "reaction": "hives", "severity": "moderate"}],
        medications=[{
            "name": "insulin glargine", "dose": "20 units", "route": "SC", "frequency": "daily",
            "next_due": T0 + timedelta(hours=1, minutes=13), "high_alert": True, "last_given": T0 - timedelta(hours=23),
        }],
        lines_drains=["PIV L forearm 20g"],
        pending_labs=["BMP 05:00"],
        fall_risk="high",
        weight_kg=71.4,
        snapshot_time=T0 - timedelta(minutes=12),
    )


def packet() -> DowntimePacket:
    return DowntimePacket(
        patient_id="p-012", generated_at=T0, snapshot_time=T0 - timedelta(minutes=12),
        model_id="us.anthropic.claude-sonnet-4-6", sbar=SBAR,
        alerts=[{"rule_id": "R-DUE-01", "text": "High-alert insulin due within 60 minutes"}],
        guardrail=GUARDRAIL, cost=COST,
    )


def captured() -> CapturedRecord:
    return CapturedRecord(
        record_id="r-001", patient_id="p-012", form_type="vitals_flowsheet",
        image_ref="captures/r-001.jpg", captured_at=T0 + timedelta(hours=1), captured_by_role="rn",
        reads={
            "textract": {"fields": {"hr": "88", "temp": None}, "confidence": {"hr": 0.93, "temp": 0.12}},
            "bedrock": {"fields": {"hr": "88", "temp": "38.9"}, "confidence": {"hr": 0.97, "temp": 0.61}},
        },
        fields=[
            {"name": "hr", "value": "88", "confidence": 0.95, "agreement": "agree",
             "crop": {"left": 0.1, "top": 0.2, "width": 0.1, "height": 0.05}},
            {"name": "temp", "value": "38.9", "confidence": 0.61, "agreement": "disagree", "flag": "disagreement"},
        ],
        backload_group="nursing_vitals",
    )


def policy_answer() -> PolicyAnswer:
    return PolicyAnswer(
        question="How do I get a stat lab with the order system down?",
        answer="Use the downtime lab requisition and call the lab (section 4.2).",
        citations=[{"section_id": "4.2", "title": "Laboratory orders", "quote": "Use form DT-LAB-01"}],
        model_id="us.anthropic.claude-sonnet-4-6", grounded=True, guardrail=GUARDRAIL, cost=COST,
    )


def discrepancy() -> Discrepancy:
    return Discrepancy(
        discrepancy_id="d-001", rule_id="R-REC-01", patient_id="p-012", record_id="r-002",
        description="Medication administered on paper with no pre-downtime order", severity="critical",
    )


def digest() -> ShiftDigest:
    return ShiftDigest(
        shift_window={"start": T0 - timedelta(hours=8), "end": T0 + timedelta(hours=4)},
        generated_at=T0 + timedelta(hours=4), model_id="us.anthropic.claude-sonnet-4-6",
        unit_summary="EHR down 02:47-13:47; 14 records captured.",
        per_patient=[{"patient_id": "p-012", "sbar": SBAR}], open_items=["Provider co-sign for VO r-003"], cost=COST,
    )


def audit() -> AuditEvent:
    return AuditEvent(
        event_id="e-001", ts=T0, actor_role="charge_nurse", action="go_to_downtime",
        target_type="unit", target_id="med-surg-4", details={"packets": 12},
    )


BUILDERS = [snapshot, packet, captured, policy_answer, discrepancy, digest, audit]


@pytest.mark.parametrize("build", BUILDERS, ids=lambda b: b.__name__)
def test_round_trip(build):
    original = build()
    restored = type(original).model_validate_json(original.model_dump_json())
    assert restored == original


def test_every_top_level_model_has_a_round_trip():
    assert {type(b()) for b in BUILDERS} == set(TOP_LEVEL_MODELS)


def test_frozen_enum_values():
    assert {e.value for e in FormType} == {"vitals_flowsheet", "downtime_mar", "admission_note", "verbal_order"}
    assert {e.value for e in Agreement} == {"agree", "disagree", "single"}
    assert {e.value for e in FieldFlag} == {"unreadable", "disagreement", "out_of_range"}
    assert {e.value for e in RecordStatus} == {"needs_review", "verified", "entered", "rejected"}
    assert {e.value for e in BackloadGroup} == {
        "nursing_med_admin", "nursing_vitals", "nursing_io", "pharmacy_orders", "unit_registration",
    }
    assert {e.value for e in Severity} == {"info", "warning", "critical"}
    assert {e.value for e in DiscrepancyStatus} == {"open", "acknowledged", "resolved"}


def test_flag_is_nullable_and_source_is_fixed():
    rec = captured()
    assert rec.fields[0].flag is None
    assert snapshot().source == "bca-feed-sim"
    with pytest.raises(ValidationError):
        PatientSnapshot.model_validate({**snapshot().model_dump(), "source": "real-ehr"})


def test_rejects_unknown_enum_and_extra_keys():
    data = captured().model_dump()
    with pytest.raises(ValidationError):
        CapturedRecord.model_validate({**data, "form_type": "lab_slip"})
    with pytest.raises(ValidationError):
        CapturedRecord.model_validate({**data, "unexpected": 1})


def test_captured_record_needs_image_or_audio():
    data = captured().model_dump()
    with pytest.raises(ValidationError):
        CapturedRecord.model_validate({**data, "image_ref": None, "audio_ref": None})
    vo = CapturedRecord.model_validate({
        **data, "image_ref": None, "audio_ref": "audio/vo-1.webm", "form_type": "verbal_order",
        "backload_group": "pharmacy_orders", "reads": {}, "fields": [],
    })
    assert vo.reads.textract is None


@pytest.mark.parametrize("model", TOP_LEVEL_MODELS, ids=lambda m: m.__name__)
def test_exported_schema_is_current(model):
    path = SCHEMA_DIR / f"{model.__name__}.json"
    assert path.exists(), "run `make schemas`"
    assert path.read_text(encoding="utf-8") == render(model), "schema drift: run `make schemas` and commit"
    assert json.loads(path.read_text(encoding="utf-8"))["title"] == model.__name__
