"""tools/guardrail_calibrate.py (#80 S3): call construction, score parsing, the frozen threshold formula and the
guardrail topic limits. Offline: fake bedrock-runtime client, no AWS, no network."""
from __future__ import annotations

import json
import re
import shutil
import socket
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app.ai import client as ai
from app.ai import guardrail
from evals.run import Refused
from tools import guardrail_calibrate as gc

ROOT = Path(__file__).resolve().parent.parent
GID = "gr-fake-id-0123"
ENV = ("DRY_RUN", "GUARDRAIL_ID", "GUARDRAIL_VERSION")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    for k in ENV:
        monkeypatch.delenv(k, raising=False)


def resp(action="NONE", grounding=0.9, relevance=0.95, topic=None, units=1, extra=None) -> dict:
    filters = [{"type": t, "threshold": th, "score": s, "action": "BLOCKED" if s < th else "NONE", "detected": s < th}
               for t, th, s in (("GROUNDING", 0.75, grounding), ("RELEVANCE", 0.5, relevance)) if s is not None]
    a = {"contextualGroundingPolicy": {"filters": filters}}
    if topic:
        a["topicPolicy"] = {"topics": [{"name": topic, "type": "DENY", "action": "BLOCKED", "detected": True}]}
    return {"action": action, "assessments": [a | (extra or {})], "outputs": [],
            "usage": {"topicPolicyUnits": units, "contextualGroundingPolicyUnits": units, "wordPolicyUnits": 0},
            "ResponseMetadata": {"RequestId": "r-1", "HTTPHeaders": {}}}


class FakeClient:
    def __init__(self, responses: dict[str, dict] | None = None, default: dict | None = None) -> None:
        self.responses, self.default, self.calls = responses or {}, default or resp(), []

    def apply_guardrail(self, **kw):
        self.calls.append(kw)
        for key, r in self.responses.items():  # keyed by the query (the refusal question)
            if kw["content"][1]["text"]["text"] == key:
                return r
        return self.default


def entry(i="sa-proc-01", category="should_answer", question="q?", text="answer", baseline="answered") -> dict:
    return {"id": i, "category": category, "subcategory": "procedure", "question": question,
            "pre_guardrail": text, "baseline": baseline}


# ------------------------------------------------------------------ call construction

def test_request_is_the_apps_own_call_plus_full_scope():
    client = FakeClient()
    policy, question, answer = "P" * 150_000, "Q" * 1_500, "A" * 5_000
    request, response = gc.check_one(entry(question=question, text=answer), policy, client, GID, "1")
    assert response is client.default and len(client.calls) == 1
    sent = client.calls[0]
    assert sent["guardrailIdentifier"] == GID and sent["guardrailVersion"] == "1" and sent["source"] == "OUTPUT"
    assert sent["outputScope"] == "FULL" and "outputScope" not in request  # added on the wire only
    blocks = [c["text"] for c in sent["content"]]
    assert [b["qualifiers"] for b in blocks] == [["grounding_source"], ["query"], ["guard_content"]]
    assert blocks[0]["text"] == policy[:100_000] and blocks[1]["text"] == question[:1_000]  # the app's caps
    assert blocks[2]["text"] == answer  # the answer is never capped

    class AppRuntime:  # what the app itself sends for the same answer
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def apply_guardrail(self, **kw):
            self.calls.append(kw)
            return {"action": "NONE"}

    app_rt = AppRuntime()
    orig = ai._runtime
    ai._runtime = lambda: app_rt
    try:
        guardrail.BedrockGuardrail(GID, "1").check(answer, grounding=policy, query=question)
    finally:
        ai._runtime = orig
    assert app_rt.calls == [request]
    assert ai._runtime is orig  # the replay restores the shared client


def test_recording_restores_the_runtime_on_error():
    class Boom:
        def apply_guardrail(self, **_kw):
            raise RuntimeError("denied")

    orig = ai._runtime
    with pytest.raises(RuntimeError):
        gc.check_one(entry(), "policy", Boom(), GID, "1")
    assert ai._runtime is orig


# ------------------------------------------------------------------ parsing

def test_summarize_full_response():
    s = gc.summarize(resp("GUARDRAIL_INTERVENED", grounding=0.12, relevance=0.88, topic="dosing-treatment-advice",
                          units=2))
    assert s["action"] == "GUARDRAIL_INTERVENED"
    assert s["topics"] == [{"name": "dosing-treatment-advice", "action": "BLOCKED", "detected": True}]
    assert s["filters"] == [
        {"type": "GROUNDING", "score": 0.12, "threshold": 0.75, "action": "BLOCKED", "detected": True},
        {"type": "RELEVANCE", "score": 0.88, "threshold": 0.5, "action": "NONE", "detected": False}]
    assert s["usage"] == {"topicPolicyUnits": 2, "contextualGroundingPolicyUnits": 2}
    assert s["usd"] == pytest.approx(2 * 0.15 / 1000 + 2 * 0.10 / 1000)  # PRICE_PER_1K
    row = entry() | s
    assert gc.score(row, "GROUNDING") == 0.12 and gc.score(row, "RELEVANCE") == 0.88
    assert gc.blocked_by(row, "GROUNDING") and not gc.blocked_by(row, "RELEVANCE")
    assert [t["name"] for t in gc.topic_hits(row)] == ["dosing-treatment-advice"]


@pytest.mark.parametrize("raw", [
    {},
    {"action": None, "assessments": None, "usage": None},
    {"assessments": [{}]},
    {"assessments": [{"topicPolicy": {}, "contextualGroundingPolicy": {"filters": [{"type": "GROUNDING"}]}}]},
])
def test_summarize_tolerates_missing_parts(raw):
    s = gc.summarize(raw)
    assert s["action"] == "NONE" and s["topics"] == [] and s["usd"] == 0
    row = entry() | s
    assert gc.score(row, "GROUNDING") is None and gc.score(row, "RELEVANCE") is None
    assert all(f["action"] == "NONE" for f in s["filters"])


def test_undetected_topic_is_not_a_hit():
    row = entry() | gc.summarize({"assessments": [{"topicPolicy": {"topics": [
        {"name": "dosing-treatment-advice", "type": "DENY", "action": "NONE", "detected": False}]}}]})
    assert gc.topic_hits(row) == []


# ------------------------------------------------------------------ the frozen formula

@pytest.mark.parametrize("scores, expected", [
    ({"a": 0.99, "b": 0.86}, 0.75),  # capped at the current 0.75
    ({"a": 0.9, "b": 0.85}, 0.75),  # 0.80 -> cap
    ({"a": 0.9, "b": 0.62}, 0.55),  # 0.57 floors to 0.55
    ({"a": 0.9, "b": 0.65}, 0.60),  # exact: 0.65 - 0.05 is 0.60, not 0.55 (no float drift)
    ({"a": 0.9, "b": 0.35, "c": 0.7}, 0.65),  # 0.30 < 0.40: recompute without b -> 0.65
    ({"a": 0.9, "b": 0.45}, 0.40),  # 0.40 is not below the floor: no recompute
])
def test_threshold_formula(scores, expected):
    assert gc.calibrate(scores).threshold == pytest.approx(expected)


def test_threshold_recompute_and_stop():
    c = gc.calibrate({"a": 0.9, "b": 0.30, "c": 0.62})
    assert (c.first, c.excluded, c.second, c.threshold) == (pytest.approx(0.25), "b", pytest.approx(0.55),
                                                            pytest.approx(0.55))
    stop = gc.calibrate({"a": 0.9, "b": 0.30, "c": 0.41})  # 0.36 still below 0.40
    assert stop.threshold is None and stop.excluded == "b" and stop.second == pytest.approx(0.35)
    tie = gc.calibrate({"a": 0.2, "b": 0.2, "c": 0.9})  # only the single lowest is excluded
    assert tie.threshold is None and tie.excluded == "a"
    assert gc.calibrate({"a": 0.1}).threshold is None
    with pytest.raises(ValueError):
        gc.calibrate({})


def _rows(sa_grounding: dict[str, float], sr_grounding: dict[str, float], rel_block: str | None = None) -> list[dict]:
    rows = []
    for i, g in sa_grounding.items():
        baseline = "blocked" if i.startswith("sa-med") else "answered"
        rows.append(entry(i, baseline=baseline) | gc.summarize(resp(grounding=g, relevance=0.2 if i == rel_block
                                                                    else 0.9)))
    for i, g in sr_grounding.items():
        rows.append(entry(i, "should_refuse", baseline="blocked") |
                    gc.summarize(resp("GUARDRAIL_INTERVENED", grounding=g, topic="t" if i != "sr-2" else None)))
    return rows


def test_plan_grounding_relevance_and_topic_dependency():
    rows = _rows({"sa-proc-01": 0.92, "sa-med-01": 0.66}, {"sr-1": 0.10, "sr-2": 0.70, "sr-3": 0.58})
    p = gc.plan(rows)
    assert p["T"] == pytest.approx(0.60) and p["relevance"] is None and p["R"] == pytest.approx(0.5)
    assert [r["grounding_at_T"] for r in rows] == ["pass", "pass", "block", "pass", "block"]
    assert p["topic_dependent"] == ["sr-2"]  # grounding no longer blocks it, whatever the topic did
    text = "\n".join(gc.render_plan(rows, p))
    assert "TOPIC-DEPENDENT at T (1)" in text and "sr-2" in text and "NO hit" in text


def test_plan_recalibrates_relevance_only_on_a_baseline_blocked_entry():
    assert gc.plan(_rows({"sa-proc-01": 0.9, "sa-med-01": 0.9}, {}, rel_block="sa-proc-01"))["relevance"] is None
    p = gc.plan(_rows({"sa-proc-01": 0.9, "sa-med-01": 0.9}, {}, rel_block="sa-med-01"))
    assert p["relevance_blocked"] == ["sa-med-01"]
    assert (p["relevance"].excluded, p["R"]) == ("sa-med-01", pytest.approx(0.75))  # 0.15 -> recompute -> cap
    rows = _rows({"sa-proc-01": 0.9, "sa-med-01": 0.9, "sa-med-02": 0.9}, {}, rel_block="sa-med-01")
    rows[2]["filters"][1]["score"] = 0.3  # a second low relevance score: still below 0.40 -> STOP
    p = gc.plan(rows)
    assert p["relevance"].threshold is None and p["R"] is None


def test_plan_without_every_score_gives_no_threshold():
    rows = _rows({"sa-proc-01": 0.9}, {})
    rows.append(entry("sa-proc-02") | gc.summarize({}))
    p = gc.plan(rows)
    assert p["T"] is None and p["missing"] == ["sa-proc-02"]


# ------------------------------------------------------------------ inputs and CLI refusals

def test_committed_bundle_matches_the_manifest():
    entries = gc.load_entries()
    assert len(entries) == 35
    assert sum(e["category"] == "should_answer" for e in entries) == 15
    blocked_sa = [e["id"] for e in entries if e["category"] == "should_answer" and e["baseline"] == "blocked"]
    assert blocked_sa == ["sa-proc-08", "sa-med-01", "sa-med-02", "sa-med-03", "sa-med-04"]


def test_tampered_bundle_is_refused(tmp_path):
    bundle = tmp_path / "b"
    shutil.copytree(gc.BASELINE, bundle)
    assert len(gc.load_entries(bundle)) == 35  # the digest covers content, not location
    path = bundle / "outputs" / "refusals.json"
    data = json.loads(path.read_text())
    data[0]["response"]["question"] = "something else"
    path.write_text(json.dumps(data))
    with pytest.raises(Refused, match="digest"):
        gc.load_entries(bundle)
    with pytest.raises(Refused, match="differs from the manifest"):
        gc.load_entries(bundle, None)
    data[0]["response"]["question"] = gc.load_entries()[0]["question"]
    data[0]["pre_guardrail"] = " "
    path.write_text(json.dumps(data))
    with pytest.raises(Refused, match="no pre_guardrail"):
        gc.load_entries(bundle, None)


@pytest.mark.parametrize("env, arg, msg", [
    ({"GUARDRAIL_ID": GID, "GUARDRAIL_VERSION": "1"}, None, "DRY_RUN=0"),
    ({"DRY_RUN": "1", "GUARDRAIL_ID": GID, "GUARDRAIL_VERSION": "1"}, None, "DRY_RUN=0"),
    ({"DRY_RUN": "0", "GUARDRAIL_VERSION": "1"}, None, "GUARDRAIL_ID"),
    ({"DRY_RUN": "0", "GUARDRAIL_ID": GID}, None, "version"),
    ({"DRY_RUN": "0", "GUARDRAIL_ID": GID, "GUARDRAIL_VERSION": "DRAFT"}, None, "--version DRAFT"),
    ({"DRY_RUN": "0", "GUARDRAIL_ID": GID, "GUARDRAIL_VERSION": "1"}, "v2", "neither"),
])
def test_target_refusals(monkeypatch, env, arg, msg):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(Refused, match=re.escape(msg)):
        gc.target(arg)


def test_target_version_sources(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("GUARDRAIL_ID", GID)
    monkeypatch.setenv("GUARDRAIL_VERSION", "1")
    assert gc.target(None) == (GID, "1")
    assert gc.target("2") == (GID, "2")
    assert gc.target("draft") == (GID, "DRAFT")  # by name only


def test_raw_out_must_be_scratch(tmp_path):
    assert gc.check_raw_out(None) is None
    assert gc.check_raw_out(tmp_path / "new") == (tmp_path / "new").resolve()
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("x")
    with pytest.raises(Refused, match="new or empty"):
        gc.check_raw_out(tmp_path / "full")
    with pytest.raises(Refused, match="not git-ignored"):
        gc.check_raw_out(ROOT / "docs" / "calibration")
    assert gc.check_raw_out(ROOT / "evals" / "runs" / "calibration-test") is not None  # git-ignored scratch


def test_main_end_to_end_offline(monkeypatch, tmp_path, capsys):
    entries = gc.load_entries()
    by_q = {e["question"]: e for e in entries}
    arn = "arn:aws:bedrock:us-east-1:123456789012:guardrail/" + GID
    responses = {}
    for q, e in by_q.items():
        g = 0.3 if e["category"] == "should_refuse" else 0.71
        extra = {"appliedGuardrailDetails": {"guardrailId": GID, "guardrailArn": arn, "guardrailVersion": "1"}}
        responses[q] = resp("GUARDRAIL_INTERVENED" if g < 0.75 else "NONE", grounding=g, extra=extra)
    assert entries[20]["id"] == "sa-proc-01"
    responses[entries[20]["question"]] = resp(grounding=0.62)  # the lowest S_sa
    fake = FakeClient(responses)
    monkeypatch.setattr(ai, "_runtime", lambda: fake)
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("GUARDRAIL_ID", GID)
    monkeypatch.setenv("GUARDRAIL_VERSION", "1")
    raw = tmp_path / "raw"
    assert gc.main(["--raw-out", str(raw)]) == 0
    out = capsys.readouterr().out
    assert len(fake.calls) == 35 and all(c["guardrailVersion"] == "1" for c in fake.calls)
    assert GID not in out and "123456789012" not in out and "arn:aws" not in out
    assert "min 0.62 (sa-proc-01)" in out and "=> GROUNDING threshold 0.55" in out
    assert "stays at 0.50" in out and "TOPIC-DEPENDENT at T (0): grounding would no longer block: none" in out
    assert "total: 35 calls" in out
    written = (raw / "responses.json").read_text() + (raw / "rows.json").read_text()
    assert GID not in written and "123456789012" not in written and "ResponseMetadata" not in written
    assert len(json.loads((raw / "rows.json").read_text())) == 35


def test_main_stops_at_the_first_error(monkeypatch, capsys):
    class Denied:
        calls = 0

        def apply_guardrail(self, **_kw):
            Denied.calls += 1
            raise ClientError({"Error": {"Code": "ExpiredTokenException", "Message": "x"}}, "ApplyGuardrail")

    monkeypatch.setattr(ai, "_runtime", lambda: Denied())
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("GUARDRAIL_ID", GID)
    monkeypatch.setenv("GUARDRAIL_VERSION", "1")
    assert gc.main([]) == 2
    out = capsys.readouterr().out
    assert Denied.calls == 1 and "sr-dose-01: error ExpiredTokenException" in out and "not retried" in out
    assert GID not in out


# ------------------------------------------------------------------ infra/guardrail.tf limits

def _topics(tf: str) -> list[dict]:
    out = []
    for block in re.findall(r"topics_config\s*\{(.*?)\n\s{4}\}", tf, re.S):
        definition = re.search(r'definition\s*=\s*"((?:[^"\\]|\\.)*)"', block).group(1)
        examples = re.findall(r'"((?:[^"\\]|\\.)*)"', re.search(r"examples\s*=\s*\[(.*?)\]", block, re.S).group(1))
        out.append({"definition": definition, "examples": examples})
    return out


def test_guardrail_topic_within_bedrock_limits():
    """Bedrock limits (GuardrailTopicConfig): definition up to 200 characters, up to 5 examples of up to 100."""
    tf = (ROOT / "infra" / "guardrail.tf").read_text()
    topics = _topics(tf)
    assert topics, "no topics_config block parsed"
    for t in topics:
        assert 1 <= len(t["definition"]) <= 200
        assert 1 <= len(t["examples"]) <= 5
        assert all(1 <= len(e) <= 100 for e in t["examples"])
    thresholds = [float(x) for x in re.findall(r"threshold\s*=\s*([0-9.]+)", tf)]
    assert len(thresholds) == 2 and all(0 <= x <= 0.99 for x in thresholds)
    assert re.search(r'name\s*=\s*"downtime-copilot-v1"', tf)  # renaming would replace the guardrail
