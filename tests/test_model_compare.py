"""tools/model_compare.py (#76): the model comparison harness, offline.

No fixture image is needed (the photographed images are git-ignored, absent in CI): the declared photographed set is
stubbed with tiny generated JPEGs bound to their committed truth. No network: sockets are refused for the whole
module, the simulator runs in-process, and the "live" paths run with the Bedrock calls stubbed at
app.ai.client._converse / _runtime.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import random
import re
import shutil
import socket
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi.testclient import TestClient

from app import clock, config, sim_client
from app.ai import client as ai
from app.ai import forms, packets, policy
from app.store import get_store, reset_store
from contracts import FormType, PatientSnapshot, ReaderOutput
from evals import report as evals_report
from evals import run
from simulator import main as sim
from tools import model_compare as mc

ROOT = Path(__file__).resolve().parent.parent
COMMITTED_PHOTOS = ROOT / "evals" / "fixtures"
ALL = ("nova", "haiku", "sonnet")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr("urllib.request.urlopen", refuse)


@pytest.fixture(autouse=True)
def no_override():
    ai.set_route_override(None)
    yield
    ai.set_route_override(None)


def _stub_pairs(photos: Path) -> Path:
    """The manifest's declared photographed set: committed truth beside a stub JPEG, image_sha256 recomputed."""
    from PIL import Image

    photos.mkdir(exist_ok=True)
    for s in json.loads(run.MANIFEST.read_text())["photographed_fixtures"]:
        buf = io.BytesIO()
        Image.frombytes("RGB", (8, 8), random.Random(f"stub:{s}").randbytes(8 * 8 * 3)).save(buf, "JPEG")
        (photos / f"{s}.jpg").write_bytes(buf.getvalue())
        truth = json.loads((COMMITTED_PHOTOS / f"{s}.json").read_text())
        sha = hashlib.sha256(buf.getvalue()).hexdigest()
        (photos / f"{s}.json").write_text(json.dumps(truth | {"image_sha256": sha}))
    return photos


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """In-process simulator, stub photographed images, a clean env. Yields the run directory to pass as --out."""
    monkeypatch.setattr(run, "PHOTO_FIXTURES", _stub_pairs(tmp_path / "photos"))
    monkeypatch.setattr(sim_client, "make_client", lambda: TestClient(sim.app))
    for k in ("GUARDRAIL_ID", "GUARDRAIL_VERSION", "GUARDRAIL_MODE", "SEED", *(f"VITAL_RANGE_{n.upper()}"
              for n in config.VITAL_RANGE_DEFAULTS)):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("PACKET_CONCURRENCY", "4")
    monkeypatch.setenv("STORE_PATH", str(tmp_path / "outside.sqlite"))
    sim.state.reset()
    clock.reset()
    reset_store()
    yield tmp_path / "run"
    clock.reset()
    reset_store()


def _report(out: Path) -> dict:
    return json.loads((out / "report.json").read_text())


def _calls(out: Path, rel: str) -> list[dict]:
    return json.loads((out / rel / "costs.json").read_text())["calls"]


# ------------------------------------------------------------------ the whole flow, DRY_RUN

def test_dry_run_compares_three_models_with_per_job_routing(harness, monkeypatch):
    seen_concurrency: list[int] = []
    orig = config.packet_concurrency
    monkeypatch.setattr(config, "packet_concurrency", lambda: seen_concurrency.append(orig()) or seen_concurrency[-1])
    assert mc.main(["--out", str(harness)]) == 0
    r = _report(harness)
    assert (r["mode"], r["status"], r["problems"], r["order"]) == ("dry-run", "complete", [], list(ALL))
    assert seen_concurrency == [1, 1, 1]  # one /downtime/start per pass, at PACKET_CONCURRENCY=1
    for k in ALL:
        p = r["passes"][k]
        assert p["status"] == "ok" and p["published_attempt"] == f"passes/{k}/attempt-1"
        calls = _calls(harness, p["published_attempt"])
        want = f"dry-run:{mc.MODELS[k]}"
        for job, n in (("form_read", 39), ("packet", 12), ("policy", 5)):
            assert [c["model_id"] for c in calls if c["job"] == job] == [want] * n  # packets: through the pool
        assert {c["job"] for c in calls} == {"form_read", "packet", "policy"}  # DRY_RUN guardrail: no checker
        x = r["metrics"][k]
        assert (x["forms"]["photographed"]["n"], x["forms"]["synthetic"]["n"]) == (36, 3)
        assert x["packets"]["n"] == 12 and x["policy"]["n"] == 5
        assert x["forms"]["photographed"]["per"]["textract"] == [234, 264]  # the pinned baseline's fixed reads
        assert x["packets"]["latency_ms"]["wall_clock"]["n"] == 12 and x["policy"]["latency_ms"]["model_call"]["n"] == 5
    lc = r["lifecycle"]
    assert lc["snapshots_identical"] and lc["snapshots_compared"] == 3 and lc["packet_concurrency"] == 1
    snaps = {(harness / f"passes/{k}/attempt-1/outputs/snapshot.json").read_bytes() for k in ALL}
    assert len(snaps) == 1 and len(json.loads(snaps.pop())) == 12
    assert r["baseline"] == {"bundle": "evals/baseline/20261028T060034Z-609f90e", "digest": mc.BASELINE_DIGEST,
                             "textract_reads": 39, "vital_ranges": r["baseline"]["vital_ranges"]}
    assert r["rule"]["verdicts"] == {"form_read": None, "packet": None, "policy": None}
    assert all(x["verdict"] is None for x in r["rule"]["inputs"]) and len(r["rule"]["inputs"]) == 2
    assert r["counterfactual"]["available"] and r["counterfactual"]["routed"]["form_read"]["model"] == ai.NOVA_2_LITE
    assert r["budget"]["spent"]["total_usd"] == 0.0 and r["budget"]["max_projected_usd"] < 5.0
    md = (harness / "report.md").read_text()
    assert md == mc.render_md(r)  # report.md is a pure function of report.json
    assert "**DRY RUN: canned mocks" in md and "| Model | Job | Accuracy |" in md
    for f in harness.rglob("*"):
        if f.is_file():
            assert not run.guard_hits(f.read_text()), f
    assert ai._route_override is None and ai._converse.__module__ == "app.ai.client"
    assert packets.generate_packet.__module__ == "app.ai.packets"
    assert orig() == 4 and os.environ["DRY_RUN"] == "1"  # the env is restored after the run
    assert os.environ["STORE_PATH"].endswith("outside.sqlite")


def test_models_run_cheapest_first_and_the_counterfactual_needs_its_passes(harness):
    assert mc.main(["--out", str(harness), "--models", "sonnet,nova"]) == 0
    r = _report(harness)
    assert r["order"] == ["nova", "sonnet"] and r["counterfactual"]["available"]
    out2 = harness.parent / "run2"
    assert mc.main(["--out", str(out2), "--models", "haiku"]) == 0
    assert _report(out2)["counterfactual"] == {"available": False,
                                               "reason": f"no published pass for {ai.NOVA_2_LITE}, {ai.SONNET_4_6}"}


# ------------------------------------------------------------------ discard, rerun, fail, unparseable

def test_contaminated_pass_is_discarded_and_rerun_once_on_a_fresh_store(harness, monkeypatch):
    read, stray = forms.read_form_with_boxes, []

    def one_stray_row(*a, **kw):
        if ai._route_override == {"form_read": ai.HAIKU_4_5} and not stray:
            stray.append(1)  # a row from another model lands in the haiku pass (what a fallback would do)
            get_store().record_cost("form_read", f"dry-run:{ai.SONNET_4_6}", 1, 1, 1, 0.0, True)
        return read(*a, **kw)

    monkeypatch.setattr(forms, "read_form_with_boxes", one_stray_row)
    assert mc.main(["--out", str(harness)]) == 0
    r = _report(harness)
    tries = r["passes"]["haiku"]["attempts"]
    assert [a["status"] for a in tries] == ["discarded", "ok"] and r["status"] == "complete"
    assert any("contaminated: form_read rows from dry-run:" + ai.SONNET_4_6 in x for x in tries[0]["reasons"])
    assert r["passes"]["haiku"]["published_attempt"] == "passes/haiku/attempt-2"
    assert len(_calls(harness, "passes/haiku/attempt-1")) == 57 and len(_calls(harness, "passes/haiku/attempt-2")) == 56
    md = (harness / "report.md").read_text()
    assert "| Claude Haiku 4.5 | 1 | discarded |" in md and "| Claude Haiku 4.5 | 2 | ok |" in md


def test_pass_failing_twice_is_marked_failed_and_never_scored(harness, monkeypatch, capsys):
    read = forms.read_form_with_boxes

    def down_for_haiku(*a, **kw):
        if ai._route_override == {"form_read": ai.HAIKU_4_5}:
            raise RuntimeError("bedrock down")  # before any cost row: a call failure, not a reply
        return read(*a, **kw)

    monkeypatch.setattr(forms, "read_form_with_boxes", down_for_haiku)
    assert mc.main(["--out", str(harness)]) == 3
    r = _report(harness)
    assert r["status"] == "incomplete" and r["problems"] == ["haiku: pass failed"]
    assert [a["status"] for a in r["passes"]["haiku"]["attempts"]] == ["discarded", "discarded"]
    assert r["passes"]["haiku"]["attempts"][0]["reasons"] == ["forms: RuntimeError: bedrock down"]
    assert "haiku" not in r["metrics"] and set(r["metrics"]) == {"nova", "sonnet"}
    assert r["counterfactual"]["available"]  # routed (nova forms, sonnet packet/policy) and all-Sonnet both ran
    assert "| Claude Haiku 4.5 | all | pass failed: no metrics (see Spend) |" in (harness / "report.md").read_text()
    assert "status incomplete" in capsys.readouterr().out


def test_unparseable_form_reply_is_scored_not_discarded(harness, monkeypatch):
    parse = forms._parse

    def no_json_from_sonnet(text):
        if ai._route_override == {"form_read": ai.SONNET_4_6}:
            raise ValueError("no JSON object in model output")
        return parse(text)

    monkeypatch.setattr(forms, "_parse", no_json_from_sonnet)
    assert mc.main(["--out", str(harness)]) == 0
    r = _report(harness)
    s, n = r["metrics"]["sonnet"]["forms"], r["metrics"]["nova"]["forms"]
    assert (s["photographed"]["unparseable"], s["synthetic"]["unparseable"]) == (36, 3)
    assert n["photographed"]["unparseable"] == 0
    assert s["photographed"]["per"]["combined"] != n["photographed"]["per"]["combined"]
    rec = json.loads((harness / "passes/sonnet/attempt-1/outputs/forms.json").read_text())[0]
    assert rec["record"]["reads"]["bedrock"] == {"fields": {}, "confidence": {}} and "ValueError" in rec["unparseable"]


def test_snapshots_that_differ_across_passes_invalidate_the_report(harness, monkeypatch):
    reset, n = sim.state.reset, []

    def drift_on_the_last_pass(seed=None):
        reset(seed)
        n.append(1)
        if len(n) == 4:  # preflight, nova, haiku, sonnet
            sim.state.advance(5)

    monkeypatch.setattr(sim.state, "reset", drift_on_the_last_pass)
    assert mc.main(["--out", str(harness)]) == 3
    r = _report(harness)
    assert r["status"] == "invalid" and not r["lifecycle"]["snapshots_identical"]
    assert r["problems"][0].startswith("snapshots differ")


def test_identical_compares_bytes():
    assert mc.identical({}) and mc.identical({"nova": b"[1]", "haiku": b"[1]", "sonnet": b"[1]"})
    assert not mc.identical({"nova": b"[1]", "haiku": b"[1] "})


# ------------------------------------------------------------------ contamination and checker constancy (pure)

def _rows(model: str, *, live: bool = True, forms_n: int = 39, packet_n: int = 12, policy_n: int = 5,
          checker_n: int = 12, checker_model: str = ai.NOVA_2_LITE) -> list[dict]:
    tag = (lambda m: m) if live else (lambda m: f"dry-run:{m}")
    rows = []
    for job, n, m in (("form_read", forms_n, model), ("packet", packet_n, model), ("policy", policy_n, model),
                      ("checker", checker_n if live else 0, checker_model)):
        rows += [{"job": job, "model_id": tag(m), "dry_run": not live} for _ in range(n)]
    return rows


COUNTS = {"forms": 39, "packets": 12, "questions": 5}


def test_clean_rows_pass():
    assert mc.pass_problems(_rows(ai.HAIKU_4_5), ai.HAIKU_4_5, True, COUNTS) == []
    assert mc.pass_problems(_rows(ai.HAIKU_4_5, live=False), ai.HAIKU_4_5, False, COUNTS) == []
    assert mc.pass_problems(_rows(ai.NOVA_2_LITE, packet_n=24), ai.NOVA_2_LITE, True, COUNTS) == []  # 12 regenerated


@pytest.mark.parametrize("mutate, live, match", [
    (lambda rows: rows[39].update(model_id=ai.FALLBACK_MODEL), True, "contaminated: packet rows from " + ai.HAIKU_4_5),
    (lambda rows: rows[-1].update(model_id=ai.HAIKU_4_5), True, "checker not constant"),
    (lambda rows: rows[-1].update(model_id=f"dry-run:{ai.NOVA_2_LITE}", dry_run=True), True, "dry-run leak"),
    (lambda rows: rows[0].update(model_id=ai.SONNET_4_6, dry_run=False), False, "live leak"),
    (lambda rows: rows.pop(0), True, "38 form_read row"),
    (lambda rows: rows.pop(-1), True, "checker ran 11 time"),
    (lambda rows: rows.append({"job": "structure", "model_id": ai.SONNET_4_6, "dry_run": False}), True,
     "unexpected job rows: structure"),
    (lambda rows: rows.extend([rows[39]] * 13), True, "25 packet row"),
], ids=["fallback-row", "checker-fallback", "dry-run-in-live", "live-in-dry-run", "row-count", "checker-count",
        "unexpected-job", "too-many-packets"])
def test_rows_that_cannot_be_published(mutate, live, match):
    rows = _rows(ai.SONNET_4_6, live=live)
    mutate(rows)
    assert any(match in p for p in mc.pass_problems(rows, ai.SONNET_4_6, live, COUNTS)), \
        mc.pass_problems(rows, ai.SONNET_4_6, live, COUNTS)


# ------------------------------------------------------------------ scoring reuse (pure, committed files only)

def test_baseline_pin_carries_a_textract_read_for_all_39_fixtures():
    manifest = run.load_manifest()
    b = mc.load_baseline(manifest)
    assert run.digest(mc.BASELINE) == mc.BASELINE_DIGEST == b["digest"]
    assert sorted(b["textract"]) == sorted([*manifest["form_fixtures"], *manifest["photographed_fixtures"]])
    assert len(b["textract"]) == 39 and all(b["textract"][s]["fields"] for s in b["textract"])


def test_scoring_reuses_the_evals_scorers_and_reproduces_the_published_baseline(tmp_path):
    """The baseline's own Nova reads, re-merged with its Textract reads by form_record and scored by score_attempt,
    give exactly the evals numbers of that bundle: forms (_score_captures), packets (faithfulness), policy
    (grounding)."""
    manifest, bundle = run.load_manifest(), mc.BASELINE
    meta, base = json.loads((bundle / "run.json").read_text()), mc.load_baseline(run.load_manifest())
    adir = tmp_path / "attempt"
    (adir / "outputs").mkdir(parents=True)
    for name in ("snapshot", "packets", "packet_passes", "policy"):
        shutil.copy(bundle / "outputs" / f"{name}.json", adir / "outputs" / f"{name}.json")
    shutil.copy(bundle / "costs.json", adir / "costs.json")
    recs, timings = [], []
    for set_name, file in (("synthetic", "captures.json"), ("photographed", "photographed.json")):
        for c in json.loads((bundle / "outputs" / file).read_text()):
            read = ReaderOutput.model_validate(c["record"]["reads"]["bedrock"])
            rec = mc.form_record(c["fixture"], set_name, c["record"]["form_type"], read, base["textract"][c["fixture"]])
            assert sorted((f["name"], f["value"], f["agreement"], str(f["flag"])) for f in rec["record"]["fields"]) == \
                sorted((f["name"], f["value"], f["agreement"], str(f["flag"])) for f in c["record"]["fields"])  # merge
            recs.append(rec)
            timings.append({"fixture": c["fixture"], "set": set_name, "wall_ms": 1, "model_ms": [1], "usd": 0.0})
    (adir / "outputs" / "forms.json").write_text(json.dumps(recs))
    (adir / "timings.json").write_text(json.dumps({"packets": [], "policy": [], "forms": timings}))
    got = mc.score_attempt(adir, manifest, base)

    for set_name, want in (("synthetic", evals_report.extraction(bundle, meta)),
                           ("photographed", evals_report.photographed(bundle))):
        g = got["forms"][set_name]
        assert g["per"] == {"model": want["per"]["nova"], "textract": want["per"]["textract"],
                            "combined": want["per"]["combined"]}
        assert (g["clean"], g["false_flags"], g["caught"], g["silent"], g["misses"]) == \
            (want["clean"], want["false_flags"], want["caught"], want["silent"], len(want["misses"]))
    ph = got["forms"]["photographed"]  # the published photographed baseline (README, #67)
    assert (ph["per"]["model"], ph["per"]["textract"], ph["per"]["combined"]) == ([237, 264], [234, 264], [244, 264])
    assert (ph["false_flags"], ph["clean"], ph["caught"], ph["silent"]) == (26, 243, 11, 9)
    fa, gr = evals_report.faithfulness(bundle), evals_report.grounding(bundle, manifest)
    assert (got["packets"]["first"], got["packets"]["final"]) == (list(fa["first"]), list(fa["final"]))
    assert (got["packets"]["omitted"], got["packets"]["regenerated"], got["packets"]["unparseable"]) == \
        (fa["omitted"], fa["regenerated"], fa["unparseable"])
    assert (got["policy"]["grounded"], got["policy"]["correct"], got["policy"]["n"]) == \
        (gr["grounded"], gr["correct"], gr["n"])
    calls = json.loads((bundle / "costs.json").read_text())["calls"]
    assert got["packets"]["usd"] == round(sum(c["usd"] for c in calls if c["job"] == "packet"), 6)
    assert got["packets"]["checker"]["calls"] == 12 and got["packets"]["checker"]["models"] == [ai.NOVA_2_LITE]


def test_baseline_that_does_not_match_its_pin_refuses(tmp_path, monkeypatch):
    manifest = run.load_manifest()
    with pytest.raises(run.Refused, match="digest"):
        mc.load_baseline(manifest, pinned="0" * 64)
    copy = tmp_path / mc.BASELINE.name
    shutil.copytree(mc.BASELINE, copy)
    caps = json.loads((copy / "outputs" / "photographed.json").read_text())
    caps[5]["record"]["reads"]["textract"] = None
    (copy / "outputs" / "photographed.json").write_text(json.dumps(caps))
    with pytest.raises(run.Refused, match=f"no Textract read for {caps[5]['fixture']}"):
        mc.load_baseline(manifest, bundle=copy, pinned=run.digest(copy))
    monkeypatch.setenv("VITAL_RANGE_SPO2", "90,100")
    with pytest.raises(run.Refused, match="vital ranges"):
        mc.load_baseline(manifest)


def test_committed_truth_that_drifted_from_the_baseline_refuses(tmp_path, monkeypatch):
    photos = _stub_pairs(tmp_path / "photos")
    stem = "hw-vitals_flowsheet-003"
    t = json.loads((photos / f"{stem}.json").read_text())
    t["fields"]["spo2"] = "99" if t["fields"].get("spo2") != "99" else "98"
    (photos / f"{stem}.json").write_text(json.dumps(t))
    monkeypatch.setattr(run, "PHOTO_FIXTURES", photos)
    with pytest.raises(run.Refused, match=f"truth for {stem} differs"):
        mc.load_baseline(run.load_manifest(photos=photos))


# ------------------------------------------------------------------ budget

def test_step_estimates_project_the_three_passes_under_the_default_cap():
    manifest = run.load_manifest()
    est = mc.step_estimates(list(ALL), manifest, "bedrock", 0.01)
    assert sorted(est) == sorted(f"{k}:{s}" for k in ALL for s in ("packets", "policy", "forms"))
    assert est["sonnet:forms"] == round(39 * ai.estimate_usd(ai.SONNET_4_6, 2_500, 1_000), 6)
    assert est["nova:policy"] == round(5 * ai.estimate_usd(ai.NOVA_2_LITE, 3_000, 800) + 5 * 0.01, 6)
    assert sum(est.values()) < 5.0
    assert est["nova:forms"] < est["haiku:forms"] < est["sonnet:forms"]  # cheapest first is cheapest


def test_budget_projection_refuses_before_any_call_and_writes_nothing(harness, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(ai, "call", lambda *a, **k: called.append(a))
    assert mc.main(["--out", str(harness), "--budget", "0.10"]) == 4
    assert not harness.exists() and called == []
    assert "projected spend" in capsys.readouterr().err


def test_budget_stop_mid_run_writes_the_spend_and_runs_nothing_more(harness, monkeypatch):
    _live(monkeypatch, usage=(1_000_000, 1_000_000))  # every call costs dollars: the first step blows the cap
    assert mc.main(["--out", str(harness), "--live"]) == 4
    r = _report(harness)
    assert r["status"] == "incomplete" and r["problems"][-1].startswith("budget: projected spend")
    assert [r["passes"][k]["status"] for k in ALL] == ["stopped", "not run", "not run"]
    nova = r["passes"]["nova"]["attempts"][0]
    assert nova["status"] == "stopped" and nova["metered_usd"] > 5.0
    assert r["budget"]["spent"]["abandoned_usd"] >= nova["metered_usd"] and r["metrics"] == {}


# ------------------------------------------------------------------ the live paths, Bedrock stubbed

class _Runtime:
    def __init__(self):
        self.applied = 0

    def apply_guardrail(self, **kw):
        self.applied += 1
        return {"action": "NONE", "assessments": []}


def _live(monkeypatch, *, usage=(100, 50), fail=None) -> list[tuple[str, str]]:
    """DRY_RUN=0 with a published guardrail; _converse answers each job with its contract-shaped canned reply.
    `fail(model_id, kind)` -> an exception to raise, or None. Returns (model, kind) per call."""
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("GUARDRAIL_ID", "gr-test")
    monkeypatch.setenv("GUARDRAIL_VERSION", "3")
    seen: list[tuple[str, str]] = []
    runtime = _Runtime()

    def converse(model_id, system, user_text, image, image_format, max_tokens):
        if max_tokens == 1:
            kind, text = "probe", "OK"
        else:
            u = json.loads(user_text)
            if "snapshot" in u:
                kind = "packet"
                text = json.dumps({"sbar": packets.template_sbar(PatientSnapshot.model_validate(u["snapshot"]))})
            elif "grounding_source" in u:
                kind, text = "checker", '{"verdict": "pass", "reasons": []}'
            elif "question" in u:
                kind, text = "policy", policy._mock_answer(u["question"])
            else:
                kind, text = "form_read", forms.mock_output(FormType(u["form_type"])).model_dump_json()
        seen.append((model_id, kind))
        if fail is not None and (exc := fail(model_id, kind)) is not None:
            raise exc
        tin, tout = (1, 1) if kind == "probe" else usage
        return {"output": {"message": {"content": [{"text": text}]}},
                "usage": {"inputTokens": tin, "outputTokens": tout}}

    monkeypatch.setattr(ai, "_converse", converse)
    monkeypatch.setattr(ai, "_runtime", lambda: runtime)
    return seen


def test_live_flow_offline_checks_every_row_and_the_checker(harness, monkeypatch):
    seen = _live(monkeypatch)
    assert mc.main(["--out", str(harness), "--live"]) == 0
    r = _report(harness)
    assert (r["mode"], r["status"], r["lifecycle"]["guardrail_mode"]) == ("live", "complete", "bedrock")
    assert [m for m, kind in seen if kind == "probe"] == [mc.MODELS[k] for k in ALL]  # 1-token probe per model
    for k in ALL:
        calls = _calls(harness, f"passes/{k}/attempt-1")
        assert {c["model_id"] for c in calls if c["job"] != "checker"} == {mc.MODELS[k]}
        assert [c["model_id"] for c in calls if c["job"] == "checker"] == [ai.NOVA_2_LITE] * 12
        assert not any(c["dry_run"] for c in calls)
        a = r["passes"][k]["attempts"][0]
        assert a["guardrail_apply"] == 5 and a["guardrail"] == {"mode": "bedrock", "version": "3"}
        assert r["metrics"][k]["packets"]["checker"]["models"] == [ai.NOVA_2_LITE]
    s = r["budget"]["spent"]
    assert s["metered_usd"] > 0 and s["probes_usd"] > 0 and s["guardrail_allowance_usd"] == 0.15
    assert s["total_usd"] == pytest.approx(s["metered_usd"] + 0.15 + s["probes_usd"], abs=1e-5)
    assert s["abandoned_usd"] == 0
    c = r["counterfactual"]
    assert c["available"] and c["difference_usd"] == round(c["all_sonnet_usd"] - c["routed_usd"], 6) > 0
    assert "gr-test" not in (harness / "report.json").read_text()  # the guardrail id is compared, never written
    assert "DRY RUN" not in (harness / "report.md").read_text()


def test_live_checker_fallback_discards_the_pass(harness, monkeypatch):
    """A checker call that fails on Nova falls back to Haiku (the checker is never overridden): the Haiku checker
    row breaks checker constancy, so every pass is discarded, rerun once, then marked failed; spend still counts."""
    _live(monkeypatch, fail=lambda m, kind: RuntimeError("nova down") if (m, kind) == (ai.NOVA_2_LITE, "checker")
          else None)
    assert mc.main(["--out", str(harness), "--live"]) == 3
    r = _report(harness)
    assert [r["passes"][k]["status"] for k in ALL] == ["failed"] * 3 and r["metrics"] == {}
    for k in ALL:
        for a in r["passes"][k]["attempts"]:
            assert a["status"] == "discarded" and any("checker not constant" in x for x in a["reasons"])
    s = r["budget"]["spent"]
    assert s["abandoned_usd"] == pytest.approx(s["metered_usd"] + s["guardrail_allowance_usd"], abs=1e-5)
    assert s["abandoned_usd"] > 0 and s["guardrail_apply"] == 3 * 2 * 5  # every attempt reached its policy step


def test_live_overridden_job_error_propagates_and_discards_without_a_haiku_row(harness, monkeypatch):
    _live(monkeypatch, fail=lambda m, kind: RuntimeError("sonnet down") if (m, kind) == (ai.SONNET_4_6, "policy")
          else None)
    assert mc.main(["--out", str(harness), "--live"]) == 3
    r = _report(harness)
    assert r["passes"]["sonnet"]["status"] == "failed" and r["passes"]["nova"]["status"] == "ok"
    for a in r["passes"]["sonnet"]["attempts"]:
        assert a["reasons"] == ["policy: RuntimeError: sonnet down"]
        calls = _calls(harness, a["dir"])
        assert not [c for c in calls if c["job"] == "policy"]  # no fallback row, no row for the failed call
        assert ai.HAIKU_4_5 not in {c["model_id"] for c in calls}


def test_transient_error_gets_one_same_model_retry(harness, monkeypatch):
    throttled = []

    def once(model, kind):
        if (model, kind) == (ai.HAIKU_4_5, "form_read") and not throttled:
            throttled.append(1)
            return ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse")
        return None

    seen = _live(monkeypatch, fail=once)
    assert mc.main(["--out", str(harness), "--live"]) == 0
    r = _report(harness)
    a = r["passes"]["haiku"]["attempts"]
    assert len(a) == 1 and a[0]["status"] == "ok" and a[0]["retries"] == [f"{ai.HAIKU_4_5}: ThrottlingException"]
    assert seen.count((ai.HAIKU_4_5, "form_read")) == 40  # 39 reads, the first one twice


def _boom(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Converse")


@pytest.mark.parametrize("errors, retried, raises", [
    ([_boom("ServiceUnavailableException")], 1, False),
    ([EndpointConnectionError(endpoint_url="https://bedrock-runtime")], 1, False),
    ([_boom("ThrottlingException"), _boom("ThrottlingException")], 1, True),
    ([_boom("ValidationException")], 0, True),
    ([_boom("AccessDeniedException")], 0, True),
    ([RuntimeError("bug")], 0, True),
], ids=["503", "connection", "twice", "validation", "access-denied", "not-botocore"])
def test_retry_once_classifies_transient_errors(monkeypatch, errors, retried, raises):
    seen = []

    def fake(model_id, *a, **kw):
        seen.append(model_id)
        if errors:
            raise errors.pop(0)
        return "ok"

    monkeypatch.setattr(ai, "_converse", fake)
    log: list[str] = []
    with mc.retry_once(log):
        if raises:
            with pytest.raises(Exception):
                ai._converse(ai.NOVA_2_LITE, "s", "u", None, "jpeg", 10)
        else:
            assert ai._converse(ai.NOVA_2_LITE, "s", "u", None, "jpeg", 10) == "ok"
    assert len(log) == retried and set(seen) == {ai.NOVA_2_LITE} and len(seen) == 1 + retried
    assert ai._converse is fake


# ------------------------------------------------------------------ refusals before any call

def test_live_refusals_before_any_call(harness, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(ai, "_converse", lambda *a, **k: called.append(a))
    assert mc.main(["--out", str(harness), "--live"]) == 3  # DRY_RUN=1 from the fixture
    assert "unset DRY_RUN" in capsys.readouterr().err
    monkeypatch.setenv("DRY_RUN", "0")
    assert mc.main(["--out", str(harness), "--live"]) == 3
    assert "published guardrail" in capsys.readouterr().err
    assert called == [] and not harness.exists()


def test_failed_probe_refuses_before_any_pass(harness, monkeypatch, capsys):
    _live(monkeypatch, fail=lambda m, kind: _boom("AccessDeniedException") if (m, kind) == (ai.HAIKU_4_5, "probe")
          else None)
    assert mc.main(["--out", str(harness), "--live"]) == 3
    assert "1-token probe of " + ai.HAIKU_4_5 in capsys.readouterr().err and not harness.exists()


@pytest.mark.parametrize("damage, match", [
    (lambda photos: (photos / "hw-downtime_mar-004.jpg").unlink(), "missing.*hw-downtime_mar-004"),
    (lambda photos: (photos / "hw-downtime_mar-004.jpg").write_bytes(b"x"), "hw-downtime_mar-004: image sha256"),
], ids=["image-missing", "sha-mismatch"])
def test_fixture_images_are_checked_in_both_modes(harness, monkeypatch, capsys, damage, match):
    damage(run.PHOTO_FIXTURES)
    assert mc.main(["--out", str(harness)]) == 3
    assert re.search(match, capsys.readouterr().err) and not harness.exists()


def test_cli_refusals(harness, capsys):
    assert mc.main(["--out", str(harness), "--models", "nova,opus"]) == 3
    assert "--models must name" in capsys.readouterr().err
    harness.mkdir()
    (harness / "report.json").write_text("{}")
    assert mc.main(["--out", str(harness)]) == 3
    assert "not empty" in capsys.readouterr().err


def test_simulator_unreachable_refuses(harness, monkeypatch, capsys):
    def down():
        raise ConnectionError("refused")

    monkeypatch.setattr(sim_client, "make_client", down)
    assert mc.main(["--out", str(harness)]) == 3
    assert "simulator unreachable" in capsys.readouterr().err and not harness.exists()


def test_production_concurrency_disclosure_matches_the_app_default(monkeypatch):
    monkeypatch.delenv("PACKET_CONCURRENCY", raising=False)
    assert config.packet_concurrency() == mc.PRODUCTION_PACKET_CONCURRENCY
    assert mc.CHECKER_MODEL == ai.ROUTES["checker"] and list(mc.MODELS.values()) == sorted(
        mc.MODELS.values(), key=lambda m: ai.PRICING[m])  # cheapest first
