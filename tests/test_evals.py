"""The eval harness must refuse to fabricate: no fixtures or DRY_RUN -> nonzero, no RESULTS.md."""
from __future__ import annotations

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
