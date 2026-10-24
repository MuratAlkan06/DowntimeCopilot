"""RESULTS.md generation is a pure function of the artifact bundle (+ NOTES.md): byte-identical regeneration,
k/n beside every percentage, 'n/a' on a zero denominator, synthetic labels, and refusals on mock or inconsistent
bundles. The fixture bundle under tests/fixtures/eval_bundle is synthetic (canned reads, invented costs)."""
from __future__ import annotations

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
