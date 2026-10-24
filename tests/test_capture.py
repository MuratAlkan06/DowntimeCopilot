"""Capture pipeline: vision read, never-guess unreadable fields, crops (model box -> layout map), range flags.

Every test runs offline. The live path (DRY_RUN=0) is exercised with the Bedrock call stubbed at
app.ai.client._converse, so CI never reaches Nova.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import sim_client
from app.ai import client as ai
from app.capture import crops, pipeline
from app.capture.agreement import merge
from app.capture.layouts import layout
from app.rules import out_of_range
from app.store import get_store, reset_store
from contracts import CapturedRecord, Crop, FieldFlag, FormType, ReaderOutput
from simulator import main as sim
from simulator.state import load_patients

FIXTURES = Path(__file__).resolve().parent.parent / "data" / "forms" / "fixtures"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("STORE_PATH", str(tmp_path / "test.sqlite"))
    monkeypatch.setenv("SNAPSHOT_INTERVAL_SECONDS", "0")
    monkeypatch.setattr(sim_client, "make_client", lambda: TestClient(sim.app))
    sim.state.reset(7)
    reset_store()
    yield monkeypatch
    reset_store()


def fixture(form_type: FormType) -> tuple[bytes, dict]:
    truth = json.loads((FIXTURES / f"{form_type.value}-01.json").read_text())
    return (FIXTURES / truth["image"]).read_bytes(), truth["fields"]


def by_name(rec: CapturedRecord) -> dict:
    return {f.name: f for f in rec.fields}


def stub_nova(monkeypatch, reply: dict, calls: list | None = None):
    """Live path with the Bedrock call replaced (no network, no credentials)."""
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("TEXTRACT_ENABLED", "0")

    def fake(model_id, system, user_text, image, image_format, max_tokens):
        if calls is not None:
            calls.append({"model_id": model_id, "system": system, "image": image, "format": image_format})
        return {"output": {"message": {"content": [{"text": "```json\n" + json.dumps(reply) + "\n```"}]}},
                "usage": {"inputTokens": 1900, "outputTokens": 240}}

    monkeypatch.setattr(ai, "_converse", fake)


# ------------------------------------------------------------------ unreadable is never guessed

def test_dry_run_unreadable_has_no_value_and_layout_crops(env):
    data, _truth = fixture(FormType.vitals_flowsheet)
    rec = pipeline.capture(data, "image/jpeg", FormType.vitals_flowsheet, None, "rn")
    f = by_name(rec)
    assert f["pain_score"].flag == FieldFlag.unreadable and f["pain_score"].value is None
    assert all(x.value is None for x in rec.fields if x.flag == FieldFlag.unreadable)
    printed = layout(FormType.vitals_flowsheet)
    assert {x.name: x.crop for x in rec.fields} == printed  # DRY_RUN parity: crops from the layout map
    store = get_store()
    assert store.get_blob(rec.image_ref) == (data, "image/jpeg")  # original kept
    for name in printed:
        blob = store.get_blob(crops.crop_ref(rec.record_id, name))
        assert blob is not None and blob[1] == "image/jpeg" and blob[0][:2] == b"\xff\xd8"
    ev = next(e for e in store.list_audit() if e.action == "capture.crops")
    assert set(ev.details["box_source"].values()) == {"layout"}


def test_live_unreadable_never_guessed(env):
    calls: list = []
    stub_nova(env, {
        "fields": {"bed": "12", "time": "03:30", "temp_c": "37.8", "heart_rate": "104", "resp_rate": "22",
                   "bp_systolic": "148", "bp_diastolic": "86", "spo2": "illegible", "pain_score": "4",
                   "initials": "PN"},
        "confidence": {"bed": 0.98, "time": 0.95, "temp_c": 0.93, "heart_rate": 0.94, "resp_rate": 0.9,
                       "bp_systolic": 0.92, "bp_diastolic": 0.9, "spo2": 0.7, "pain_score": 0.9, "initials": 0.9},
        "unreadable": ["pain_score"],  # the model said unreadable but still wrote a value: it is dropped
    }, calls)
    data, _ = fixture(FormType.vitals_flowsheet)
    rec = pipeline.capture(data, "image/jpeg", FormType.vitals_flowsheet, None, "rn")
    f = by_name(rec)
    assert len(calls) == 1 and calls[0]["model_id"] == ai.ROUTES["form_read"] == ai.NOVA_2_LITE
    assert calls[0]["image"] == data and "Form reading (v2)" in calls[0]["system"]
    for name in ("pain_score", "spo2"):
        assert f[name].value is None and f[name].flag == FieldFlag.unreadable, name
        assert rec.reads.bedrock.fields[name] is None and rec.reads.bedrock.confidence[name] <= 0.2
    assert f["heart_rate"].value == "104" and f["heart_rate"].agreement.value == "single"
    assert rec.reads.textract is None
    cost = get_store().list_costs()[-1]
    assert cost["job"] == "form_read" and cost["dry_run"] is False and cost["usd"] > 0


def test_live_low_confidence_value_is_not_used(env):
    stub_nova(env, {"fields": {"bed": "12", "pain_score": "7"}, "confidence": {"bed": 0.95, "pain_score": 0.15}})
    data, _ = fixture(FormType.vitals_flowsheet)
    f = by_name(pipeline.capture(data, "image/jpeg", FormType.vitals_flowsheet, None, "rn"))
    assert f["pain_score"].value is None and f["pain_score"].flag == FieldFlag.unreadable


def test_merge_never_carries_a_value_on_unreadable():
    low = ReaderOutput(fields={"pain_score": "6"}, confidence={"pain_score": 0.1})
    other = ReaderOutput(fields={"pain_score": "5"}, confidence={"pain_score": 0.3})
    (f,) = merge(low, other)
    assert f.flag == FieldFlag.unreadable and f.value is None


# ------------------------------------------------------------------ crop box source

def test_crop_source_model_box_then_layout_fallback():
    printed = layout(FormType.vitals_flowsheet)
    hr = printed["heart_rate"]
    on_field = Crop(left=hr.left + 0.02, top=hr.top + 0.03, width=0.08, height=0.04)
    elsewhere = Crop(left=0.05, top=0.85, width=0.1, height=0.05)  # far from the printed temp_c box
    boxes, source = crops.choose_boxes(FormType.vitals_flowsheet, {"heart_rate": on_field, "temp_c": elsewhere})
    assert boxes["heart_rate"] == on_field and source["heart_rate"] == "model"
    assert boxes["temp_c"] == printed["temp_c"] and source["temp_c"] == "layout"
    assert boxes["spo2"] == printed["spo2"] and source["spo2"] == "layout"  # no model box at all
    assert set(boxes) == set(printed)


def test_live_model_boxes_parsed_and_used(env):
    hr = layout(FormType.vitals_flowsheet)["heart_rate"]
    stub_nova(env, {"fields": {"heart_rate": "104", "temp_c": "37.8"}, "confidence": {"heart_rate": 0.9, "temp_c": 0.9},
                    "boxes": {"heart_rate": [round((hr.left + 0.02) * 1000), round((hr.top + 0.03) * 1000), 80, 40],
                              "temp_c": {"x": 1.5, "y": 0.2, "w": 0.1, "h": 0.1}}})  # 0..1000 grid; invalid box
    data, _ = fixture(FormType.vitals_flowsheet)
    rec = pipeline.capture(data, "image/jpeg", FormType.vitals_flowsheet, None, "rn")
    f = by_name(rec)
    assert f["heart_rate"].crop == Crop(left=round(hr.left + 0.02, 3), top=round(hr.top + 0.03, 3), width=0.08,
                                        height=0.04)
    assert f["temp_c"].crop == layout(FormType.vitals_flowsheet)["temp_c"]
    ev = next(e for e in get_store().list_audit() if e.action == "capture.crops")
    assert ev.details["box_source"]["heart_rate"] == "model" and ev.details["box_source"]["temp_c"] == "layout"


def test_undecodable_upload_still_captures_without_crop_blobs(env):
    rec = pipeline.capture(b"\x89PNG\r\n\x1a\nnot-an-image", "image/png", FormType.downtime_mar, None, "rn")
    assert all(f.crop is not None for f in rec.fields)  # boxes still set from the layout map
    assert get_store().get_blob(crops.crop_ref(rec.record_id, "medication")) is None


# ------------------------------------------------------------------ out-of-range thresholds

@pytest.mark.parametrize("name,at_low,at_high,below,above", [
    ("spo2", "92", "100", "91.9", "100.1"),
    ("heart_rate", "50", "110", "49", "111"),
    ("resp_rate", "10", "24", "9", "25"),
    ("temp_c", "36.0", "38.0", "35.9", "38.1"),
    ("bp_systolic", "90", "160", "89", "161"),
    ("bp_diastolic", "50", "100", "49", "101"),
])
def test_threshold_boundaries(name, at_low, at_high, below, above):
    assert not out_of_range(name, at_low) and not out_of_range(name, at_high)  # exactly at threshold
    assert out_of_range(name, below) and out_of_range(name, above)              # beyond


def test_thresholds_env_overridable_and_flag_field(monkeypatch):
    def flag(value: str):
        (f,) = merge(None, ReaderOutput(fields={"spo2": value}, confidence={"spo2": 0.95}))
        return f.flag
    assert flag("92") is None and flag("91") == FieldFlag.out_of_range
    monkeypatch.setenv("VITAL_RANGE_SPO2", "90,100")
    assert flag("90") is None and flag("89.9") == FieldFlag.out_of_range
    assert not out_of_range("pain_score", "10")


@pytest.mark.parametrize("value", ["abc", "1O4", "9 8", "98.6.1", "nan", "inf", "1e2", "unreadable"])
def test_unparseable_numeric_vital_is_flagged(value):
    assert out_of_range("heart_rate", value)
    (f,) = merge(None, ReaderOutput(fields={"heart_rate": value}, confidence={"heart_rate": 0.95}))
    assert f.flag == FieldFlag.out_of_range


@pytest.mark.parametrize("value", ["22", " 22 ", "22.", "22.0", "+22"])
def test_numeric_vital_in_range_is_not_flagged(value):
    assert not out_of_range("resp_rate", value)


def test_empty_vital_keeps_unreadable_semantics_and_free_text_fields_untouched():
    assert not out_of_range("spo2", None) and not out_of_range("spo2", "") and not out_of_range("spo2", "  ")
    assert not out_of_range("response", "abc") and not out_of_range("pain_score", "abc")


def test_dry_run_fixture_records_are_contract_valid(env):
    get_store().save_snapshot(load_patients())  # bed "12" on the form resolves to pt-12
    for ft in (FormType.vitals_flowsheet, FormType.downtime_mar, FormType.admission_note):
        data, _ = fixture(ft)
        rec = pipeline.capture(data, "image/jpeg", ft, None, "rn")
        again = CapturedRecord.model_validate_json(rec.model_dump_json())
        assert again == rec and rec.patient_id == "pt-12" and rec.image_ref
        assert {f.name for f in rec.fields} == set(layout(ft))
        assert all(f.flag != FieldFlag.unreadable or f.value is None for f in rec.fields)
