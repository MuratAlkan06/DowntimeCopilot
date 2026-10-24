"""The eval harness must refuse to fabricate: no fixtures or DRY_RUN -> nonzero, no RESULTS.md.
Per-reader scoring (S8): Textract and Bedrock reads are scored separately against ground truth."""
from __future__ import annotations

from contracts import ReaderOutput
from evals import run


def test_empty_fixtures_exit_nonzero(tmp_path, capsys):
    out = tmp_path / "RESULTS.md"
    assert run.main(["--fixtures", str(tmp_path), "--out", str(out)]) != 0
    assert not out.exists()
    assert "EVALS NOT RUN" in capsys.readouterr().err


def test_dry_run_refuses_even_with_fixtures(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    (tmp_path / "f.png").write_bytes(b"x")
    (tmp_path / "f.json").write_text('{"form_type": "vitals_flowsheet", "fields": {"bed": "12"}}')
    out = tmp_path / "RESULTS.md"
    assert run.main(["--fixtures", str(tmp_path), "--out", str(out)]) != 0
    assert not out.exists()


# ------------------------------------------------------------------ per-reader scoring hook (S8)

TRUTH = {"bed": "12", "resp_rate": "22", "spo2": "89", "pain_score": None}


def _reader(fields: dict) -> ReaderOutput:
    return ReaderOutput(fields=fields, confidence={k: 0.9 for k in fields})


def test_per_reader_scores_differ_when_reads_differ():
    t = _reader({"bed": "12", "resp_rate": "22", "spo2": "89", "pain_score": None})
    b = _reader({"bed": "12", "resp_rate": "27", "spo2": "98", "pain_score": None})
    s = run.score_reads(t, b, TRUTH)
    assert s["textract"] == (4, 4) and s["bedrock"] == (2, 4)
    assert s["agreement"] == {"agree": 1, "disagree": 2, "single": 1}  # pain_score: both None -> single
    assert s["combined"][1] == 4


def test_per_reader_scores_equal_when_reads_agree():
    same = {"bed": "12", "resp_rate": "22", "spo2": "89", "pain_score": None}
    s = run.score_reads(_reader(same), _reader(dict(same)), TRUTH)
    assert s["textract"] == s["bedrock"] == s["combined"] == (4, 4)
    assert s["agreement"]["disagree"] == 0


def test_missing_textract_read_is_not_scored_as_zero():
    s = run.score_reads(None, _reader({"bed": "12", "resp_rate": "22"}), TRUTH)
    assert s["textract"] is None and s["bedrock"] == (3, 4)  # pain_score None == truth None
    assert s["agreement"] == {"agree": 0, "disagree": 0, "single": 2}
