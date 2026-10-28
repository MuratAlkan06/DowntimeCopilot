"""The eval harness must refuse to fabricate: no fixtures, DRY_RUN, or a projected overspend -> nonzero, nothing
written. The manifest fixes the inputs before the run. Per-reader scoring (S8): Textract and Bedrock reads are
scored separately against ground truth."""
from __future__ import annotations

import hashlib
import io
import json
import random
import re
import shutil
from collections import Counter
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from contracts import ReaderOutput
from evals import report, run

ROOT = Path(__file__).resolve().parent.parent
COMMITTED_PHOTOS = ROOT / "evals" / "fixtures"  # run.PHOTO_FIXTURES before any monkeypatch: committed truth only


def test_empty_fixtures_exit_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "0")
    out = tmp_path / "RESULTS.md"
    assert run.main(["--fixtures", str(tmp_path), "--runs", str(tmp_path / "runs"), "--out", str(out)]) != 0
    assert not out.exists() and not (tmp_path / "runs").exists()
    assert "EVALS NOT RUN" in capsys.readouterr().err


def test_dry_run_refuses_even_with_fixtures(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "1")
    out = tmp_path / "RESULTS.md"
    assert run.main(["--runs", str(tmp_path / "runs"), "--out", str(out)]) != 0
    assert not out.exists() and not (tmp_path / "runs").exists()
    assert "DRY_RUN=1" in capsys.readouterr().err


def test_textract_off_and_unset_store_refuse(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "PHOTO_FIXTURES", _stub_pairs(tmp_path / "photos"))  # the declared set, stub images
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("TEXTRACT_ENABLED", "0")
    with pytest.raises(run.Refused, match="TEXTRACT_ENABLED"):
        run.preflight(run.FIXTURES)
    monkeypatch.setenv("TEXTRACT_ENABLED", "1")
    monkeypatch.delenv("STORE_PATH", raising=False)
    with pytest.raises(run.Refused, match="STORE_PATH"):
        run.preflight(run.FIXTURES)


def test_preflight_passes_the_real_manifest_when_every_declared_image_is_bound_to_its_truth(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "PHOTO_FIXTURES", _stub_pairs(tmp_path / "photos"))
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("TEXTRACT_ENABLED", "1")
    monkeypatch.setenv("STORE_PATH", str(tmp_path / "evals.sqlite"))
    assert run.preflight(run.FIXTURES) == json.loads(run.MANIFEST.read_text())


# ------------------------------------------------------------------ manifest fixes the inputs

def _demo_questions() -> list[tuple[str, str]]:
    rows = re.findall(r"^\| \d \| (.+?) \| (\d+(?:\.\d+)?) ", (ROOT / "DEMO.md").read_text(), re.M)
    return [(q, s) for q, s in rows]


def test_manifest_fixes_inputs():
    m = run.load_manifest()
    assert m["seed_patients"] == 12
    assert [(q["question"], q["expected_section"]) for q in m["questions"]] == _demo_questions()
    assert len(m["questions"]) == 5
    assert m["form_fixtures"] == ["vitals_flowsheet-01", "downtime_mar-01", "admission_note-01"]
    for stem in m["form_fixtures"]:
        truth = json.loads((run.FIXTURES / f"{stem}.json").read_text())
        assert truth["synthetic"] is True and truth["fields"]
    assert m["speech_fixture"] == {"stem": "verbal_order-01", "patient_id": "pt-07"}
    assert m["clock_advance_minutes"] == {"before_downtime": 15, "before_costs": 45}
    b = m["budget"]
    assert b["limit_usd"] == 4.0 and sum(b["step_estimates_usd"].values()) < b["limit_usd"]
    assert b["step_estimates_usd"]["photographed_forms"] == 2.70


def test_manifest_with_missing_fixture_refuses(tmp_path):
    (tmp_path / "vitals_flowsheet-01.json").write_text("{}")  # ground truth without its image
    with pytest.raises(run.Refused, match="vitals_flowsheet-01"):
        run.load_manifest(fixtures=tmp_path)


# ------------------------------------------------------------------ guardrail refusal questions (#80), pre-registered

REFUSAL_SUBCATEGORY_COUNTS = {
    ("should_refuse", "direct_dosing"): 5, ("should_refuse", "treatment_advice"): 5,
    ("should_refuse", "rephrased_indirect"): 5, ("should_refuse", "prompt_injection"): 5,
    ("should_answer", "procedure"): 10, ("should_answer", "mentions_medication"): 5,
}


def test_manifest_pre_registers_the_refusal_question_set():
    m = run.load_manifest()
    rq = m["refusal_questions"]
    assert Counter((q["category"], q["subcategory"]) for q in rq) == REFUSAL_SUBCATEGORY_COUNTS
    assert len({q["id"] for q in rq}) == len({q["question"] for q in rq}) == 35
    assert all(q["id"].startswith({"should_refuse": "sr-", "should_answer": "sa-"}[q["category"]]) for q in rq)
    assert all(("expected_section" in q) == (q["category"] == "should_answer") for q in rq)
    assert not {q["question"] for q in rq} & {q["question"] for q in m["questions"]}
    assert run.scrub(json.dumps(rq)) == json.dumps(rq)  # the bundle scrubber leaves every entry as written


def test_every_expected_section_is_a_real_policy_section():
    """The sections the app grounds on (app.ai.policy parses data/policy/downtime_policy.md); imported here, never by
    load_manifest, which stays env- and app-free."""
    from app.ai import policy

    m = run.load_manifest()
    expected = [q["expected_section"] for q in [*m["questions"], *m["refusal_questions"]] if "expected_section" in q]
    assert len(expected) == 5 + 15
    assert [e for e in expected if e not in policy.sections()] == []


def _refusal_manifest(tmp_path, edit) -> Path:
    """The real manifest with `edit(refusal_questions, manifest)` applied."""
    m = json.loads(run.MANIFEST.read_text())
    edit(m["refusal_questions"], m)
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(m))
    return p


@pytest.mark.parametrize("edit, match", [
    (lambda rq, m: m.update(refusal_questions={"sr-dose-01": "?"}), "list of objects"),
    (lambda rq, m: rq[0].update(note="x"), "unknown field.*note"),
    (lambda rq, m: rq[0].update(category="maybe"), "category must be one of"),
    (lambda rq, m: rq[0].update(subcategory="procedure"), "subcategory of should_refuse"),
    (lambda rq, m: rq[0].update(id="sa-dose-01"), "id must be sr-"),
    (lambda rq, m: rq[1].update(id=rq[0]["id"]), "id repeated"),
    (lambda rq, m: rq[0].update(question="  "), "non-empty"),
    (lambda rq, m: rq[0].pop("question"), "non-empty"),
    (lambda rq, m: rq[0].update(question=m["questions"][0]["question"]), "repeats a manifest `questions` text"),
    (lambda rq, m: rq[1].update(question=rq[0]["question"]), "question repeated"),
    (lambda rq, m: rq[0].update(question="Is 555123456789 the pharmacy line?"), "12-digit"),
    (lambda rq, m: rq[-1].pop("expected_section"), "needs expected_section"),
    (lambda rq, m: rq[0].update(expected_section="5"), "has no expected_section"),
    (lambda rq, m: rq.pop(0), "19 should_refuse entries"),
    (lambda rq, m: rq.append(rq[-1] | {"id": "sa-med-06", "question": "One more?"}), "16 should_answer entries"),
], ids=["not-a-list", "unknown-field", "category", "subcategory", "id-prefix", "id-repeat", "blank-question",
        "no-question", "demo-overlap", "question-repeat", "12-digit-run", "answer-without-section",
        "refuse-with-section", "too-few", "too-many"])
def test_refusal_questions_failing_a_static_check_refuse_in_load_manifest(tmp_path, edit, match):
    with pytest.raises(run.Refused, match=match):
        run.load_manifest(_refusal_manifest(tmp_path, edit))


def test_a_manifest_without_refusal_questions_loads_as_before(tmp_path):
    m = run.load_manifest(_refusal_manifest(tmp_path, lambda rq, m: m.pop("refusal_questions")))
    assert "refusal_questions" not in m and len(m["questions"]) == 5


# ------------------------------------------------------------------ budget guard

def test_budget_guard_trips_on_mocked_cost_overrun():
    spend = {"usd": 0.0}
    ran = []

    def packets():
        ran.append("packets")
        spend["usd"] = 1.9  # mocked overrun: the first step cost far more than its estimate

    guard = run.BudgetGuard(2.0, {"packets": 0.4, "policy": 0.2})
    with pytest.raises(run.BudgetExceeded, match="projected spend"):
        run.execute([("packets", packets), ("policy", lambda: ran.append("policy"))], guard, lambda: spend["usd"])
    assert ran == ["packets"]  # the next step never ran


def test_budget_guard_refuses_before_the_first_step():
    with pytest.raises(run.BudgetExceeded):
        run.execute([("a", lambda: None)], run.BudgetGuard(1.0, {"a": 1.5}), lambda: 0.0)


def test_budget_guard_passes_within_budget():
    guard = run.BudgetGuard(2.0, {"a": 0.5, "b": 0.5})
    run.execute([("a", lambda: None), ("b", lambda: None)], guard, lambda: 0.3)
    assert guard.max_projected == pytest.approx(1.3)


def test_budget_projection_with_the_photographed_estimate_stays_under_the_limit():
    b = run.load_manifest()["budget"]
    est = b["step_estimates_usd"]
    projected = run.BudgetGuard(b["limit_usd"], est).check(0.0)  # before the first step: nothing spent, all ahead
    assert projected == pytest.approx(3.58) and projected <= b["limit_usd"] == 4.0
    spend, guard = {"usd": 0.0}, run.BudgetGuard(b["limit_usd"], est)
    run.execute([(name, lambda usd=usd: spend.update(usd=spend["usd"] + usd)) for name, usd in est.items()],
                guard, lambda: spend["usd"])  # every step costs exactly its estimate: the projection stays flat
    assert guard.max_projected == pytest.approx(projected) and guard.remaining == {}


def test_budget_overrun_exits_nonzero_without_writing(tmp_path, monkeypatch, capsys):
    def boom(fixtures, runs):
        raise run.BudgetExceeded("projected spend $2.10 > budget $2.00")
    monkeypatch.setattr(run, "live_run", boom)
    out = tmp_path / "RESULTS.md"
    assert run.main(["--out", str(out)]) == 4
    assert not out.exists() and "budget" in capsys.readouterr().err


# ------------------------------------------------------------------ cost consistency, scrub

def _costs(**over) -> dict:
    calls = [{"job": "packet", "model_id": "m", "tokens_in": 10, "tokens_out": 5, "latency_ms": 100, "usd": 0.1,
              "dry_run": False, "ts": "t"},
             {"job": "policy", "model_id": "m", "tokens_in": 20, "tokens_out": 5, "latency_ms": 300, "usd": 0.2,
              "dry_run": False, "ts": "t"}]
    return {"calls": calls, "total_usd": 0.3, "tokens_in": 30, "tokens_out": 10, "outage_hours": 0.75,
            "usd_per_outage_hour": 0.4, "dry_run": False} | over


def test_check_costs_sums_and_refusals():
    assert run.check_costs(_costs())["packet"]["calls"] == 1
    with pytest.raises(run.Refused, match="total_usd"):
        run.check_costs(_costs(total_usd=0.31))
    with pytest.raises(run.Refused, match="outage"):
        run.check_costs(_costs(usd_per_outage_hour=None))
    with pytest.raises(run.Refused, match="dry-run"):
        run.check_costs(_costs(dry_run=True))


def test_scrub_removes_guard_patterns():
    raw = 'arn:aws:events:us-east-1:123456789012:event-bus/default "AKIA' + "A" * 16 + '" acct 210987654321 x'
    clean = run.scrub(raw)
    assert run.guard_hits(raw) and not run.guard_hits(clean)
    assert "123456789012" not in clean and "AKIA" not in clean


# ------------------------------------------------------------------ the whole flow (DRY_RUN, preflight bypassed)

def test_flow_writes_a_scrubbed_bundle(tmp_path, monkeypatch):
    """Harness wiring only: canned mocks, in-process simulator, the real manifest with stub images for its declared
    photographed set (the real ones are git-ignored). The live refusals are tested above."""
    monkeypatch.setattr(run, "PHOTO_FIXTURES", _stub_pairs(tmp_path / "photos"))
    bundle, steps = _flow(tmp_path, monkeypatch, run.MANIFEST)
    meta = json.loads((bundle / "run.json").read_text())
    assert meta["manifest"] == json.loads(run.MANIFEST.read_text())
    # every step has an estimate and every estimate a step: an estimate whose step never runs is never dropped
    assert sorted(steps) == sorted(meta["manifest"]["budget"]["step_estimates_usd"])
    declared = meta["manifest"]["photographed_fixtures"]
    assert [c["fixture"] for c in json.loads((bundle / "outputs" / "photographed.json").read_text())] == declared
    assert sorted(meta["photographed_truth"]) == sorted(declared) and meta["photographed_fixtures"] == len(declared)
    assert len(json.loads((bundle / "outputs" / "packets.json").read_text())) == 12
    assert len(json.loads((bundle / "outputs" / "packet_passes.json").read_text())) == 12
    assert len(json.loads((bundle / "outputs" / "policy.json").read_text())) == 5
    assert [c["fixture"] for c in json.loads((bundle / "outputs" / "captures.json").read_text())] == \
        meta["manifest"]["form_fixtures"]
    assert json.loads((bundle / "outputs" / "speech.json").read_text())["transcribe"]["outcome"] == "mock"
    costs = json.loads((bundle / "costs.json").read_text())
    assert costs["outage_hours"] == pytest.approx(0.75) and costs["usd_per_outage_hour"] == 0.0  # $0 in mock
    assert {"service": "Textract", "probe": "ALLOWED"} in meta["services"]
    assert meta["unmetered"]["textract_analyze_document"] == 3 + len(declared)  # synthetic forms + photographed
    for f in bundle.rglob("*.json"):
        assert not run.guard_hits(f.read_text()), f
    assert len(run.digest(bundle)) == 64


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


# ------------------------------------------------------------------ photographed-style fixtures (#67)

def _photo_manifest(tmp_path, stems) -> Path:
    m = json.loads(run.MANIFEST.read_text())
    m["photographed_fixtures"] = stems
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(m))
    return p


def _photo_pair(photos: Path, stem: str, *, with_image: bool = True, with_truth: bool = True, **over) -> None:
    """A schema-1 pair built from the synthetic vitals fixture; `over` replaces truth keys (None drops one)."""
    photos.mkdir(exist_ok=True)
    src = run.FIXTURES / "vitals_flowsheet-01"
    if with_image:
        shutil.copy(src.with_suffix(".jpg"), photos / f"{stem}.jpg")
    if with_truth:
        t = json.loads(src.with_suffix(".json").read_text())
        doc = {"schema": 1, "form_type": t["form_type"], "fields": t["fields"], "image": f"{stem}.jpg",
               "seed": 7, "writer": "f0042_17", "distortion": "L1", "tags": ["nominal"],
               "source": "NIST Special Database 19, 2nd edition"}
        if with_image:
            doc["image_sha256"] = hashlib.sha256((photos / f"{stem}.jpg").read_bytes()).hexdigest()
        doc = {k: v for k, v in (doc | over).items() if v is not None}
        (photos / f"{stem}.json").write_text(json.dumps(doc))


def _stub_image(stem: str) -> bytes:
    """A tiny decodable JPEG whose pixels are seeded by the stem: generated per test, never committed."""
    from PIL import Image

    buf = io.BytesIO()
    Image.frombytes("RGB", (8, 8), random.Random(f"stub:{stem}").randbytes(8 * 8 * 3)).save(buf, "JPEG")
    return buf.getvalue()


def _stub_pairs(photos: Path) -> Path:
    """The real manifest's declared photographed set as pairs the pre-spend check accepts: each stem's committed truth
    JSON beside a stub image, image_sha256 recomputed over the stub (the real images are git-ignored, absent in CI)."""
    photos.mkdir(exist_ok=True)
    for s in json.loads(run.MANIFEST.read_text())["photographed_fixtures"]:
        img = _stub_image(s)
        (photos / f"{s}.jpg").write_bytes(img)
        truth = json.loads((COMMITTED_PHOTOS / f"{s}.json").read_text())
        (photos / f"{s}.json").write_text(json.dumps(truth | {"image_sha256": hashlib.sha256(img).hexdigest()}))
    return photos


def _checked(manifest: Path, photos: Path, fixtures: Path = run.FIXTURES) -> dict:
    """Both photographed halves in preflight's order: static (load_manifest), then pre-spend (images)."""
    m = run.load_manifest(manifest, fixtures=fixtures, photos=photos)
    run.check_photographed_images(m["photographed_fixtures"], photos)
    return m


def test_manifest_declares_exactly_the_committed_photographed_truth():
    declared = run.load_manifest()["photographed_fixtures"]
    assert declared == sorted(p.stem for p in COMMITTED_PHOTOS.glob("hw-*.json")) and len(declared) == 36
    kinds = Counter(json.loads((COMMITTED_PHOTOS / f"{s}.json").read_text())["form_type"] for s in declared)
    assert kinds == {"admission_note": 12, "downtime_mar": 12, "vitals_flowsheet": 12}


def test_real_manifest_loads_with_no_photographed_image_present(tmp_path):
    """The CI reality: the committed truth JSONs and no image (the images are git-ignored). load_manifest is the
    static half only, so the real manifest loads; the images are preflight's (check_photographed_images)."""
    truth_only = tmp_path / "photos"
    truth_only.mkdir()
    for p in COMMITTED_PHOTOS.glob("*.json"):
        shutil.copy(p, truth_only / p.name)
    on_disk = list(truth_only.iterdir())
    assert on_disk and not [p for p in on_disk if p.suffix.lower() in run.IMAGE_EXT]
    m = run.load_manifest(photos=truth_only)
    assert m == json.loads(run.MANIFEST.read_text()) and len(m["photographed_fixtures"]) == 36
    with pytest.raises(run.Refused, match="missing.*hw-admission_note-001"):
        run.check_photographed_images(m["photographed_fixtures"], truth_only)


def test_declared_photographed_pairs_pass(tmp_path):
    photos = tmp_path / "photos"
    _photo_pair(photos, "sd19-a")
    _photo_pair(photos, "sd19-b", image_sha256=None, schema=None, seed=None, writer=None, distortion=None,
                tags=None, source=None, image=None)  # form_type + fields only: metadata may be absent
    assert _checked(_photo_manifest(tmp_path, ["sd19-a", "sd19-b"]), photos)["photographed_fixtures"] == [
        "sd19-a", "sd19-b"]
    assert _checked(_photo_manifest(tmp_path, []), tmp_path / "none")["photographed_fixtures"] == []


@pytest.mark.parametrize("pairs, declared, match", [
    ({"sd19-a": {"with_truth": False}}, ["sd19-a"], "truth missing.*sd19-a"),
    ({"sd19-a": {"with_image": False, "schema": 2}}, ["sd19-a"], "schema 2"),
    ({"sd19-a": {"with_image": False, "fields": None}}, ["sd19-a"], "needs form_type"),
    ({"sd19-a": {"with_image": False, "form_type": None}}, ["sd19-a"], "needs form_type"),
    ({"sd19-a": {"with_image": False}}, ["sd19-a", "sd19-a"], "distinct"),
    ({"hw-vitals_flowsheet-001": {"with_image": False}, "hw-vitals_flowsheet-999": {"with_image": False}},
     ["hw-vitals_flowsheet-001"], "truth in .* not declared.*hw-vitals_flowsheet-999"),
], ids=["no-truth", "schema", "no-fields", "no-form-type", "repeat", "stray-hw-truth"])
def test_photographed_truth_not_matching_the_manifest_refuses_in_load_manifest(tmp_path, pairs, declared, match):
    """Static half: refused from the committed truth alone, no image needed (most cases have none on disk)."""
    photos = tmp_path / "photos"
    for stem, kw in pairs.items():
        _photo_pair(photos, stem, **kw)
    with pytest.raises(run.Refused, match=match):
        run.load_manifest(_photo_manifest(tmp_path, declared), photos=photos)


@pytest.mark.parametrize("pairs, declared, match", [
    ({"sd19-a": {"with_image": False}}, ["sd19-a"], "missing.*sd19-a"),
    ({"sd19-a": {}, "sd19-b": {}}, ["sd19-a"], "not declared.*sd19-b"),
    ({"sd19-a": {}}, [], "not declared.*sd19-a"),
    ({"sd19-a": {"image_sha256": "0" * 64}}, ["sd19-a"], "sd19-a: image sha256 does not match"),
], ids=["no-image", "stray", "stray-with-empty-list", "sha-mismatch"])
def test_photographed_images_not_matching_the_manifest_refuse_in_the_pre_spend_check(tmp_path, pairs, declared,
                                                                                     match):
    """Pre-spend half: load_manifest reads no image, so it passes; check_photographed_images refuses."""
    photos = tmp_path / "photos"
    for stem, kw in pairs.items():
        _photo_pair(photos, stem, **kw)
    m = run.load_manifest(_photo_manifest(tmp_path, declared), photos=photos)
    with pytest.raises(run.Refused, match=match):
        run.check_photographed_images(m["photographed_fixtures"], photos)


def test_two_images_for_one_photographed_stem_refuse(tmp_path):
    photos = tmp_path / "photos"
    _photo_pair(photos, "sd19-a")
    shutil.copy(photos / "sd19-a.jpg", photos / "sd19-a.png")
    with pytest.raises(run.Refused, match="more than one image.*sd19-a"):
        _checked(_photo_manifest(tmp_path, ["sd19-a"]), photos)


@pytest.mark.parametrize("damage, match", [
    (lambda img: img.unlink(), "missing.*sd19-a"),
    (lambda img: img.write_bytes(img.read_bytes() + b"\0"), "sd19-a: image sha256 does not match"),
], ids=["image-missing", "sha-mismatch"])
def test_preflight_refuses_a_missing_or_altered_image_before_any_step(tmp_path, monkeypatch, capsys, damage, match):
    """The live path (main -> live_run -> preflight) runs both halves: an image refusal exits 3 before the simulator
    is contacted or any step executes, and nothing is written."""
    from app import sim_client

    photos = tmp_path / "photos"
    _photo_pair(photos, "sd19-a")
    damage(photos / "sd19-a.jpg")
    manifest, load, touched = _photo_manifest(tmp_path, ["sd19-a"]), run.load_manifest, []
    monkeypatch.setattr(run, "load_manifest", lambda **kw: load(manifest, **kw))
    monkeypatch.setattr(run, "PHOTO_FIXTURES", photos)
    monkeypatch.setattr(run, "execute", lambda *a: touched.append("execute"))
    monkeypatch.setattr(sim_client, "make_client", lambda: touched.append("simulator"))
    monkeypatch.setenv("DRY_RUN", "0")
    out = tmp_path / "RESULTS.md"
    assert run.main(["--runs", str(tmp_path / "runs"), "--out", str(out)]) == 3
    assert touched == [] and not out.exists() and not (tmp_path / "runs").exists()
    assert re.search(match, capsys.readouterr().err)


def test_stray_photographed_pair_refuses_in_preflight_before_anything_is_written(tmp_path, monkeypatch, capsys):
    photos = _stub_pairs(tmp_path / "photos")  # the declared set passes both halves
    _photo_pair(photos, "sd19-a")  # on disk beside it, not declared: only the pre-spend pair equality sees it
    monkeypatch.setattr(run, "PHOTO_FIXTURES", photos)
    monkeypatch.setenv("DRY_RUN", "0")
    out = tmp_path / "RESULTS.md"
    assert run.main(["--runs", str(tmp_path / "runs"), "--out", str(out)]) == 3
    assert not out.exists() and not (tmp_path / "runs").exists()
    assert "not declared" in capsys.readouterr().err


def _flow(tmp_path, monkeypatch, manifest: Path) -> tuple[Path, list[str]]:
    """test_flow_writes_a_scrubbed_bundle's wiring with a chosen manifest; also records the step order."""
    from app import clock, sim_client
    from app.store import reset_store
    from simulator import main as sim

    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("STORE_PATH", str(tmp_path / "evals.sqlite"))
    monkeypatch.setattr(sim_client, "make_client", lambda: TestClient(sim.app))
    # the live preflight's input checks (both photographed halves), without its DRY_RUN / env refusals
    monkeypatch.setattr(run, "preflight", lambda fixtures: _checked(manifest, run.PHOTO_FIXTURES, fixtures))
    monkeypatch.setattr(run, "LIVE_TRANSCRIBE", frozenset({"mock"}))
    monkeypatch.setattr(run, "check_costs", lambda costs: None)
    steps: list[str] = []
    execute = run.execute

    def spy(named, guard, spent):
        steps.extend(n for n, _ in named)
        return execute(named, guard, spent)
    monkeypatch.setattr(run, "execute", spy)
    clock.reset()
    reset_store()
    try:
        return run.live_run(run.FIXTURES, tmp_path / "runs"), steps
    finally:
        clock.reset()
        reset_store()


def test_flow_without_photographed_fixtures_keeps_the_bundle_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "PHOTO_FIXTURES", tmp_path / "photos")
    bundle, steps = _flow(tmp_path, monkeypatch, _photo_manifest(tmp_path, []))
    assert steps == ["packets", "policy", "forms", "speech", "photographed_forms"]
    meta = json.loads((bundle / "run.json").read_text())
    assert "photographed_truth" not in meta and meta["photographed_fixtures"] == 0
    assert sorted(p.name for p in (bundle / "outputs").iterdir()) == [
        "captures.json", "packet_passes.json", "packets.json", "policy.json", "snapshot.json", "speech.json"]
    assert report.photographed(bundle) is None


def test_flow_with_photographed_fixtures_runs_them_last_and_bundles_fields_only(tmp_path, monkeypatch):
    photos = tmp_path / "photos"
    _photo_pair(photos, "sd19-vitals_flowsheet-01")
    monkeypatch.setattr(run, "PHOTO_FIXTURES", photos)
    bundle, steps = _flow(tmp_path, monkeypatch, _photo_manifest(tmp_path, ["sd19-vitals_flowsheet-01"]))
    assert steps[-1] == "photographed_forms"
    meta = json.loads((bundle / "run.json").read_text())
    truth = json.loads((photos / "sd19-vitals_flowsheet-01.json").read_text())
    assert meta["photographed_truth"] == {
        "sd19-vitals_flowsheet-01": {"form_type": truth["form_type"], "fields": truth["fields"]}}
    assert meta["photographed_fixtures"] == 1
    assert meta["unmetered"]["textract_analyze_document"] == 4  # 3 synthetic forms + the photographed one
    caps = json.loads((bundle / "outputs" / "photographed.json").read_text())
    assert [(c["fixture"], c["record"]["form_type"]) for c in caps] == [
        ("sd19-vitals_flowsheet-01", "vitals_flowsheet")]
    for f in bundle.rglob("*.json"):  # metadata stays in the committed truth file; the bundle stays scrubbed
        text = f.read_text()
        assert "image_sha256" not in text and truth["image_sha256"] not in text and truth["writer"] not in text, f
        assert not run.guard_hits(text), f
    ph = report.photographed(bundle)
    assert [r["fixture"] for r in ph["rows"]] == ["sd19-vitals_flowsheet-01"] and ph["per"]["combined"][1] == 10
