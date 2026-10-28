"""RESULTS.md generation is a pure function of the artifact bundle (+ NOTES.md): byte-identical regeneration,
k/n beside every percentage, 'n/a' on a zero denominator, synthetic labels, and refusals on mock or inconsistent
bundles. The fixture bundle under tests/fixtures/eval_bundle is synthetic (canned reads, invented costs)."""
from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path

import pytest

from evals import report, run

FIX = Path(__file__).resolve().parent / "fixtures"
BUNDLE, NOTES, EXPECTED = FIX / "eval_bundle", FIX / "eval_notes.md", FIX / "eval_bundle.RESULTS.md"


def test_regeneration_is_byte_identical_to_the_recorded_output():
    assert report.render(BUNDLE, NOTES).encode() == EXPECTED.read_bytes()


def test_cli_writes_the_same_bytes(tmp_path):
    out = tmp_path / "RESULTS.md"
    assert report.main([str(BUNDLE), "--notes", str(NOTES), "--out", str(out)]) == 0
    assert out.read_bytes() == EXPECTED.read_bytes()


def test_header_run_metadata_comes_from_the_bundle_not_now():
    text = report.render(BUNDLE, NOTES)
    assert text.startswith("<!-- GENERATED - DO NOT EDIT.") and "GENERATED - DO NOT EDIT." in text.splitlines()[3]
    assert "`20261024T000000Z-synthetic`" in text and f"`{'0' * 40}`" in text
    assert "2026-10-24T00:00:00+00:00 / 2026-10-24T00:05:00+00:00" in text
    assert f"`{run.digest(BUNDLE)}`" in text


def test_pct_k_over_n_and_zero_denominator():
    assert report.pct(5, 6) == "83.3% (5/6)"
    assert report.pct(0, 4) == "0.0% (0/4)"
    assert report.pct(0, 0, "n/a (0 clean fields)") == "n/a (0 clean fields)"


def test_every_percentage_carries_k_over_n():
    text = report.render(BUNDLE, NOTES)
    for m in re.finditer(r"\d+\.\d%", text):
        assert re.match(r" \(\d+/\d+\)", text[m.end():]), text[m.start():m.end() + 20]


def test_extraction_numbers_are_labeled_synthetic_and_photographed_pending():
    text = report.render(BUNDLE, NOTES)
    for line in text.splitlines():
        if "Form field accuracy" in line or "False-flag rate on clean" in line:
            assert "synthetic fixtures" in line
    assert "## 3. Form extraction (synthetic fixtures)" in text
    assert "Photographed forms: 0 fixtures — pending" in text


def _copy(tmp_path) -> Path:
    dest = tmp_path / "bundle"
    shutil.copytree(BUNDLE, dest)
    return dest


def _edit(bundle: Path, name: str, fn) -> None:
    p = bundle / name
    p.write_text(json.dumps(fn(json.loads(p.read_text())), indent=2, sort_keys=True) + "\n")


def test_zero_clean_fields_is_not_a_number(tmp_path):
    b = _copy(tmp_path)

    def blank(meta):
        meta["truth"]["vitals_flowsheet-01"]["fields"] = {k: None for k in meta["truth"]["vitals_flowsheet-01"]["fields"]}
        return meta
    _edit(b, "run.json", blank)
    text = report.render(b, NOTES)
    assert "False-flag rate on clean fields (synthetic fixtures) | n/a (0 clean fields) |" in text


def test_failures_list_every_miss():
    text = report.render(BUNDLE, NOTES).split("## FAILURES", 1)[1]
    assert "- vitals_flowsheet-01 / nova / `resp_rate`: expected \"22\", got \"27\"" in text
    assert "source_path `labs[0].glucose`" in text
    assert "- delete: expected \"confirmed\", got null" in text
    assert "- `route`: expected \"SC\", got \"subcutaneous\"" in text
    assert "expected 6, got 6 (grounded no" in text


def test_notes_are_included_verbatim_and_unknown_keys_refuse(tmp_path):
    text = report.render(BUNDLE, NOTES)
    assert "Notes (evals/NOTES.md, `speech`):\n\nOne word dropped on purpose." in text
    bad = tmp_path / "NOTES.md"
    bad.write_text("## accuracy_overall\n\nnot a metric\n")
    with pytest.raises(run.Refused, match="accuracy_overall"):
        report.render(BUNDLE, bad)


def test_refuses_a_bundle_with_dry_run_calls(tmp_path, capsys):
    b = _copy(tmp_path)

    def mock(costs):
        costs["calls"][0]["dry_run"] = True
        return costs
    _edit(b, "costs.json", mock)
    out = tmp_path / "RESULTS.md"
    assert report.main([str(b), "--notes", str(NOTES), "--out", str(out)]) != 0
    assert not out.exists() and "dry-run" in capsys.readouterr().err


def test_refuses_null_outage_cost_and_inconsistent_sums(tmp_path):
    b = _copy(tmp_path)
    _edit(b, "costs.json", lambda c: c | {"usd_per_outage_hour": None})
    with pytest.raises(run.Refused, match="outage"):
        report.render(b, NOTES)
    _edit(b, "costs.json", lambda c: c | {"usd_per_outage_hour": 0.1, "total_usd": 9.0})
    with pytest.raises(run.Refused, match="total_usd"):
        report.render(b, NOTES)


def test_word_errors_minimal_edit_script():
    assert report.words("Read-back confirmed.") == ["read", "back", "confirmed"]
    assert report.word_errors(["heparin", "sc", "now"], ["heparin", "now"]) == [("delete", "sc", None)]
    assert report.word_errors(["heparin", "now"], ["heparin", "iv", "now"]) == [("insert", None, "iv")]
    assert report.word_errors(["heparin", "sc"], ["heparin", "iv"]) == [("substitute", "sc", "iv")]
    assert len(report.word_errors(report.words("5000 units SC now"), report.words("5000 unit now stat"))) == 3
    assert report.word_errors(["a"], ["a"]) == []


def test_apply_guardrail_count_is_policy_answers_in_bedrock_mode_only():
    assert run.guardrail_apply("bedrock", 5) == 5  # packets use the metered checker even in bedrock mode
    assert run.guardrail_apply("checker", 5) == 0


def test_committed_results_regenerate_byte_identical_from_the_baseline():
    root = Path(__file__).resolve().parent.parent
    bundle = report.latest_baseline()
    assert report.render(bundle, root / "evals" / "NOTES.md").encode() == (root / "RESULTS.md").read_bytes()
    assert f"`{run.digest(bundle)}`" in (root / "RESULTS.md").read_text()
    for f in report.BASELINE.rglob("*"):  # every committed bundle, history included, stays scrubbed
        assert not f.is_file() or not run.guard_hits(f.read_text()), f


def test_latest_baseline_is_the_newest_run_id_and_refuses_none(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "BASELINE", tmp_path)
    with pytest.raises(run.Refused):
        report.latest_baseline()
    for rid in ("20261024T120050Z-90d316a", "20261024T102817Z-a020e81"):
        (tmp_path / rid).mkdir()
    (tmp_path / "20991231T000000Z-notadir").write_text("")  # files are ignored
    assert report.latest_baseline() == tmp_path / "20261024T120050Z-90d316a"


# ------------------------------------------------------------------ forms built from public handwriting samples (#67)
# eval_bundle_photographed = eval_bundle + outputs/photographed.json + run.json photographed_truth (canned reads: a
# false flag, a caught flag, two silent errors, a blank truth field, and a fixture whose Textract read did not run).

PHOTO_BUNDLE, PHOTO_EXPECTED = FIX / "eval_bundle_photographed", FIX / "eval_bundle_photographed.RESULTS.md"
PUB = "forms built from public handwriting samples"
PRE_67_BASELINES = ("20261024T102817Z-a020e81", "20261024T120050Z-90d316a", "20261027T170936Z-b1f74cb")

# extraction() output recorded at 9c8ccd5, before _score_captures was extracted from it.
GOLDEN_TEST_BUNDLE = {
    "rows": [{"fixture": "vitals_flowsheet-01", "nova": (9, 10), "textract": (10, 10), "combined": (9, 10)}],
    "per": {"nova": [9, 10], "textract": [10, 10], "combined": [9, 10]},
    "clean": 8, "false_flags": 0, "caught": 1, "silent": 0,
    "misses": [{"fixture": "vitals_flowsheet-01", "reader": "nova", "field": "resp_rate", "expected": "22",
                "got": "27"},
               {"fixture": "vitals_flowsheet-01", "reader": "combined", "field": "resp_rate", "expected": "22",
                "got": "27"}],
}
GOLDEN_BASELINE_B1F74CB = {
    "rows": [{"fixture": "vitals_flowsheet-01", "nova": (10, 10), "textract": (9, 10), "combined": (9, 10)},
             {"fixture": "downtime_mar-01", "nova": (7, 7), "textract": (6, 7), "combined": (7, 7)},
             {"fixture": "admission_note-01", "nova": (5, 5), "textract": (5, 5), "combined": (5, 5)}],
    "per": {"nova": [22, 22], "textract": [20, 22], "combined": [21, 22]},
    "clean": 19, "false_flags": 0, "caught": 1, "silent": 0,
    "misses": [{"fixture": "vitals_flowsheet-01", "reader": "textract", "field": "pain_score", "expected": None,
                "got": "NW"},
               {"fixture": "vitals_flowsheet-01", "reader": "combined", "field": "pain_score", "expected": None,
                "got": "NW"},
               {"fixture": "downtime_mar-01", "reader": "textract", "field": "second_verifier", "expected": None,
                "got": "HIGH-ALERT MEDS"}],
}


def test_score_captures_is_the_pre_refactor_extraction():
    meta = json.loads((BUNDLE / "run.json").read_text())
    assert report.extraction(BUNDLE, meta) == GOLDEN_TEST_BUNDLE
    base = report.BASELINE / "20261027T170936Z-b1f74cb"
    bmeta = json.loads((base / "run.json").read_text())
    assert report.extraction(base, bmeta) == GOLDEN_BASELINE_B1F74CB
    caps = json.loads((base / "outputs" / "captures.json").read_text())
    before = copy.deepcopy((caps, bmeta))
    assert report._score_captures(caps, bmeta["truth"], bmeta["vital_ranges"]) == GOLDEN_BASELINE_B1F74CB
    assert (caps, bmeta) == before  # pure: the inputs are not touched


def test_pre_67_bundles_have_no_photographed_set_and_keep_the_pending_lines():
    for b in (BUNDLE, *(report.BASELINE / rid for rid in PRE_67_BASELINES)):
        assert report.photographed(b) is None, b
        text = report.render(b, NOTES)
        assert text.count("Photographed forms: 0 fixtures — pending") == 2 and "## 8." not in text, b
        assert PUB not in text, b


def test_photographed_bundle_renders_byte_identical_to_the_recorded_output(tmp_path):
    assert report.render(PHOTO_BUNDLE, NOTES).encode() == PHOTO_EXPECTED.read_bytes()
    out = tmp_path / "RESULTS.md"
    assert report.main([str(PHOTO_BUNDLE), "--notes", str(NOTES), "--out", str(out)]) == 0
    assert out.read_bytes() == PHOTO_EXPECTED.read_bytes()


def test_photographed_set_is_scored_with_the_synthetic_metrics():
    ph = report.photographed(PHOTO_BUNDLE)
    assert ph["per"] == {"nova": [13, 17], "textract": [9, 10], "combined": [14, 17]}
    assert [(r["fixture"], r["nova"], r["textract"], r["combined"]) for r in ph["rows"]] == [
        ("sd19-vitals_flowsheet-01", (7, 10), (9, 10), (8, 10)), ("sd19-downtime_mar-01", (6, 7), None, (6, 7))]
    assert (ph["clean"], ph["false_flags"], ph["caught"], ph["silent"]) == (16, 1, 1, 2)
    # the synthetic set in the same bundle is scored exactly as before
    assert report.extraction(PHOTO_BUNDLE, json.loads((PHOTO_BUNDLE / "run.json").read_text())) == GOLDEN_TEST_BUNDLE


def test_photographed_labels_say_built_from_public_handwriting_samples():
    text = report.render(PHOTO_BUNDLE, NOTES)
    rows = [line for line in text.splitlines() if "Form field accuracy" in line or "False-flag rate on clean" in line]
    assert len(rows) == 8 and sum(PUB in line for line in rows) == 4
    assert all("synthetic fixtures" in line or "built from public handwriting samples" in line for line in rows)
    assert "handwritten forms" not in text.lower() and "pending" not in text
    assert text.index("## 7. Cost") < text.index(f"## 8. Form extraction ({PUB})") < text.index("## FAILURES")
    assert "Forms built from public handwriting samples: 2 fixtures, scored separately in section 8." in text
    sec8 = text.split("## 8.", 1)[1].split("## FAILURES", 1)[0]
    assert "NIST Special Database 19" in sec8
    assert f"| **All ({PUB})** | 76.5% (13/17) | 90.0% (9/10) | 82.4% (14/17) |" in sec8
    assert "| sd19-downtime_mar-01 | 85.7% (6/7) | n/a (reader did not run) | 85.7% (6/7) |" in sec8
    assert ("False-flag rate 6.2% (1/16). Flags that caught a wrong value: 1. Wrong values with no flag (silent "
            "errors): 2.") in sec8
    for m in re.finditer(r"\d+\.\d%", text):
        assert re.match(r" \(\d+/\d+\)", text[m.end():]), text[m.start():m.end() + 20]


def test_photographed_misses_are_listed_in_failures():
    text = report.render(PHOTO_BUNDLE, NOTES).split("## FAILURES", 1)[1]
    block = text.split(f"Form fields ({PUB}; field, expected, got):\n\n", 1)[1].split("\n\n", 1)[0]
    ph = report.photographed(PHOTO_BUNDLE)
    assert block.splitlines() == [f"- {m['fixture']} / {m['reader']} / `{m['field']}`: expected "
                                  f"{json.dumps(m['expected'])}, got {json.dumps(m['got'])}" for m in ph["misses"]]
    assert len(ph["misses"]) == 8
    assert '- sd19-vitals_flowsheet-01 / combined / `bp_diastolic`: expected "74", got "71"' in block  # silent
    assert '- sd19-downtime_mar-01 / nova / `time_given`: expected "01:15", got "01:16"' in block
    assert '- vitals_flowsheet-01 / nova / `resp_rate`: expected "22", got "27"' in text  # synthetic misses stay


def test_photographed_outputs_without_truth_refuse(tmp_path):
    b = tmp_path / "bundle"
    shutil.copytree(PHOTO_BUNDLE, b)
    _edit(b, "run.json", lambda meta: {k: v for k, v in meta.items() if k != "photographed_truth"})
    with pytest.raises(run.Refused, match="photographed_truth"):
        report.render(b, NOTES)


def test_photographed_is_a_metric_and_its_notes_section_is_optional(tmp_path):
    assert "photographed" in report.METRICS
    notes = tmp_path / "NOTES.md"
    notes.write_text("## photographed\n\nComposited glyphs, not paper.\n")
    sec8 = report.render(PHOTO_BUNDLE, notes).split("## 8.", 1)[1].split("## FAILURES", 1)[0]
    assert "Notes (evals/NOTES.md, `photographed`):\n\nComposited glyphs, not paper." in sec8
    assert "`photographed`" not in report.render(BUNDLE, notes)  # no section 8 in an old bundle, so no note
    assert "Notes (evals/NOTES.md, `photographed`)" not in PHOTO_EXPECTED.read_text()  # NOTES without the section
