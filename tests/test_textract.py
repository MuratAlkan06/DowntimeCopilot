"""Textract second read (S8): AnalyzeDocument FORMS + QUERIES, mapping, cap, AccessDenied fallback.

Every test uses a botocore Stubber on a real textract client (request params validated against the
installed service model, no network, no credentials). CI never calls AWS.
"""
from __future__ import annotations

import logging

import boto3
import pytest
from botocore.stub import Stubber

from app.capture import crops, textract
from app.capture.agreement import merge
from app.capture.layouts import layout
from contracts import Agreement, FormType

VITALS = list(layout(FormType.vitals_flowsheet))


@pytest.fixture()
def live(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("TEXTRACT_ENABLED", "1")
    monkeypatch.setattr(textract, "_denied", False)
    client = boto3.client("textract", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    sent: list[dict] = []
    client.meta.events.register("provide-client-params.textract.AnalyzeDocument",
                                lambda params, **_: sent.append(dict(params)))
    monkeypatch.setattr(textract, "_client", lambda: client)
    with Stubber(client) as stub:
        yield stub, sent


def bb(left, top, w=0.1, h=0.04):
    return {"BoundingBox": {"Left": left, "Top": top, "Width": w, "Height": h}}


def query(qid, alias, answer_ids):
    q = {"BlockType": "QUERY", "Id": qid, "Query": {"Text": textract.query_text(alias), "Alias": alias}}
    if answer_ids:
        q["Relationships"] = [{"Type": "ANSWER", "Ids": answer_ids}]
    return q


def result(rid, text, conf, geom=None):
    r = {"BlockType": "QUERY_RESULT", "Id": rid, "Text": text, "Confidence": conf}
    if geom:
        r["Geometry"] = geom
    return r


def word(wid, text):
    return {"BlockType": "WORD", "Id": wid, "Text": text, "Confidence": 99.0}


def kv(kid, vid, key_words, value_words, conf, geom=None):
    """KEY block + VALUE block + their WORD children."""
    kw = [word(f"{kid}w{i}", t) for i, t in enumerate(key_words)]
    vw = [word(f"{vid}w{i}", t) for i, t in enumerate(value_words)]
    key = {"BlockType": "KEY_VALUE_SET", "Id": kid, "EntityTypes": ["KEY"], "Confidence": 90.0,
           "Relationships": [{"Type": "VALUE", "Ids": [vid]}, {"Type": "CHILD", "Ids": [w["Id"] for w in kw]}]}
    val = {"BlockType": "KEY_VALUE_SET", "Id": vid, "EntityTypes": ["VALUE"], "Confidence": conf,
           "Relationships": [{"Type": "CHILD", "Ids": [w["Id"] for w in vw]}] if vw else []}
    if geom:
        val["Geometry"] = geom
    return [key, val, *kw, *vw]


def expected_params(form_type: FormType) -> dict:
    return {"Document": {"Bytes": b"img"}, "FeatureTypes": ["FORMS", "QUERIES"],
            "QueriesConfig": textract.queries_config(list(layout(form_type)))}


# ------------------------------------------------------------------ request shape

def test_call_is_forms_and_queries_without_human_loop(live):
    stub, sent = live
    stub.add_response("analyze_document", {"Blocks": []}, expected_params(FormType.downtime_mar))
    textract.read(b"img", FormType.downtime_mar)
    stub.assert_no_pending_responses()
    (kwargs,) = sent
    assert "HumanLoopConfig" not in kwargs  # never a human review loop on a patient form
    assert set(kwargs) == {"Document", "FeatureTypes", "QueriesConfig"}
    qs = kwargs["QueriesConfig"]["Queries"]
    assert {q["Alias"] for q in qs} == set(layout(FormType.downtime_mar))  # Alias = field name, one per field
    assert all(q["Text"].isascii() and q["Text"].endswith("?") and q["Pages"] == ["1"] for q in qs)


def test_queries_cover_every_template_and_are_valid_query_input():
    for ft in (FormType.vitals_flowsheet, FormType.downtime_mar, FormType.admission_note):
        names = list(layout(ft))
        qs = textract.queries_config(names)["Queries"]
        assert [q["Alias"] for q in qs] == textract.select_fields(names) and set(names) == {q["Alias"] for q in qs}
        assert all(1 <= len(q["Text"]) <= 200 and q["Text"].isascii() for q in qs)


def test_query_cap_keeps_clinically_critical_fields():
    admin = [f"admin_field_{i}" for i in range(8)]
    names = admin[:4] + ["initials", "second_verifier", "bed", "history", "height_cm", "weight_kg", "allergies",
                         "temp_c", "bp_diastolic", "bp_systolic", "heart_rate", "resp_rate", "spo2", "pain_score",
                         "route", "dose", "medication"] + admin[4:]
    assert len(names) > textract.MAX_QUERIES_PER_PAGE
    chosen = textract.select_fields(names)
    assert len(chosen) == textract.MAX_QUERIES_PER_PAGE == 15
    assert len(textract.queries_config(names)["Queries"]) == 15
    for critical in ("medication", "dose", "route", "spo2", "resp_rate", "heart_rate", "bp_systolic",
                     "bp_diastolic", "temp_c", "allergies", "bed"):
        assert critical in chosen, critical
    assert not set(admin) & set(chosen)  # unknown admin fields go first
    assert "initials" not in chosen and "second_verifier" not in chosen
    assert chosen[:3] == ["medication", "dose", "route"]


# ------------------------------------------------------------------ mapping

@pytest.mark.parametrize("raw,norm", [(0, 0.0), (100, 1.0), (57.25, 0.573), (-3, 0.0), (140, 1.0), (None, 0.0)])
def test_confidence_normalized_0_100_to_0_1(raw, norm):
    assert textract._conf(raw) == norm


def test_query_results_preferred_then_key_value_fallback(live):
    stub, _ = live
    hr = layout(FormType.vitals_flowsheet)["heart_rate"]
    blocks = [
        query("q1", "heart_rate", ["r1", "r2"]),
        result("r1", "104", 96.0, bb(hr.left + 0.02, hr.top + 0.03)),
        result("r2", "184", 41.0),                       # lower-confidence alternative answer
        query("q2", "spo2", ["r3"]), result("r3", "89", 100.0),
        query("q3", "pain_score", []),                   # asked, no answer
        query("q4", "unknown_alias", ["r4"]), result("r4", "x", 99.0),  # not a template field
        *kv("k1", "v1", ["Heart", "rate", "(/min)"], ["140"], 99.0),     # query already answered it
        *kv("k2", "v2", ["Temperature", "(°C)"], ["37.8"], 88.5, bb(0.07, 0.3)),
        *kv("k3", "v3", ["BP", "diastolic", "(mmHg)"], ["86"], 0.0),
        *kv("k4", "v4", ["BP", "systolic", "(mmHg)"], ["148"], 75.0),
        *kv("k5", "v5", ["RN", "initials"], ["PN"], 93.0),
        *kv("k6", "v6", ["Resp.", "rate", "(/min)"], [], 30.0),          # empty value
        *kv("k7", "v7", ["Ward", "phone"], ["5551234"], 99.0),           # matches no field
    ]
    stub.add_response("analyze_document", {"Blocks": blocks}, expected_params(FormType.vitals_flowsheet))
    out, boxes = textract.read_with_boxes(b"img", FormType.vitals_flowsheet)
    f, c = out.fields, out.confidence
    assert set(f) == set(VITALS)  # every template field, nothing else
    assert (f["heart_rate"], c["heart_rate"]) == ("104", 0.96)  # QUERY_RESULT beats the key-value "140"
    assert (f["spo2"], c["spo2"]) == ("89", 1.0)
    assert (f["temp_c"], c["temp_c"]) == ("37.8", 0.885)  # fuzzy "Temperature (°C)" -> temp_c
    assert (f["bp_diastolic"], c["bp_diastolic"]) == ("86", 0.0)
    assert f["bp_systolic"] == "148" and f["initials"] == "PN"
    assert f["resp_rate"] is None and f["pain_score"] is None and c["pain_score"] == 0.0
    assert f["bed"] is None and f["time"] is None  # not read by either path
    assert boxes["heart_rate"].left == pytest.approx(hr.left + 0.02) and boxes["temp_c"].top == 0.3
    assert set(boxes) == {"heart_rate", "temp_c"}


def test_fuzzy_labels_of_every_printed_form():
    printed = {  # tools/form_fixtures.py labels
        FormType.downtime_mar: {"Bed": "bed", "Time given (24 h)": "time_given", "Medication": "medication",
                                "Dose + unit": "dose", "Route": "route", "Given by (initials)": "initials",
                                "Second verifier (high-alert meds)": "second_verifier"},
        FormType.admission_note: {"Bed": "bed", "Height (cm)": "height_cm", "Weight (kg)": "weight_kg",
                                  "Allergies (substance + reaction)": "allergies", "Past medical history": "history"},
    }
    for ft, labels in printed.items():
        blocks = []
        for i, (label, name) in enumerate(labels.items()):
            blocks += kv(f"k{i}", f"v{i}", label.split(), [f"val-{name}"], 80.0)
        out, _ = textract.map_blocks(blocks, list(layout(ft)))
        assert out.fields == {name: f"val-{name}" for name in labels.values()}, ft


def test_textract_box_is_crop_source_when_on_the_printed_field():
    printed = layout(FormType.vitals_flowsheet)
    hr, t = printed["heart_rate"], printed["temp_c"]
    tx = {"heart_rate": textract.Crop(left=hr.left + 0.01, top=hr.top + 0.02, width=0.1, height=0.04),
          "temp_c": textract.Crop(left=0.05, top=0.85, width=0.1, height=0.05)}  # off its printed box
    model = {"temp_c": textract.Crop(left=t.left + 0.02, top=t.top + 0.02, width=0.1, height=0.04)}
    boxes, source = crops.choose_boxes(FormType.vitals_flowsheet, model, tx)
    assert source["heart_rate"] == "textract" and boxes["heart_rate"] == tx["heart_rate"]
    assert source["temp_c"] == "model" and source["spo2"] == "layout"


def test_two_live_reads_disagree_on_a_field(live):
    stub, _ = live
    stub.add_response("analyze_document", {"Blocks": [query("q", "resp_rate", ["r"]), result("r", "22", 91.0)]},
                      expected_params(FormType.vitals_flowsheet))
    t = textract.read(b"img", FormType.vitals_flowsheet)
    b = textract.ReaderOutput(fields={"resp_rate": "27"}, confidence={"resp_rate": 0.9})
    (f,) = [x for x in merge(t, b) if x.name == "resp_rate"]
    assert f.agreement == Agreement.disagree


# ------------------------------------------------------------------ AccessDenied -> single read

def test_access_denied_degrades_to_single_read_once(live, caplog):
    stub, sent = live
    stub.add_client_error("analyze_document", service_error_code="AccessDeniedException",
                          service_message="not authorized", http_status_code=400)
    with caplog.at_level(logging.WARNING, logger="app.capture.textract"):
        assert textract.read_with_boxes(b"img", FormType.vitals_flowsheet) == (None, {})
        assert textract.read(b"img", FormType.downtime_mar) is None  # no second call, no retry
    assert len(sent) == 1
    stub.assert_no_pending_responses()
    assert sum("denied" in r.getMessage() for r in caplog.records) == 1
    b = textract.ReaderOutput(fields={"bed": "12"}, confidence={"bed": 0.9})
    assert [f.agreement for f in merge(None, b)] == [Agreement.single]


def test_other_client_errors_are_not_swallowed(live):
    from botocore.exceptions import ClientError

    stub, _ = live
    stub.add_client_error("analyze_document", service_error_code="InvalidParameterException", http_status_code=400)
    with pytest.raises(ClientError):
        textract.read(b"img", FormType.vitals_flowsheet)
    assert textract._denied is False


def test_dry_run_mock_unchanged(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("TEXTRACT_ENABLED", "1")
    monkeypatch.setattr(textract, "_client", lambda: pytest.fail("DRY_RUN must not build a client"))
    out, boxes = textract.read_with_boxes(b"img", FormType.vitals_flowsheet)
    assert out.fields["resp_rate"] == "22" and out.confidence["resp_rate"] == 0.58 and boxes == {}
    assert textract.read(b"img", FormType.verbal_order) is None
