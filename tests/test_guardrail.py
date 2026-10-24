"""Guardrail adapter: mode selection by env, ApplyGuardrail request shape, checker fallback (no AWS in CI)."""
from __future__ import annotations

import pytest

from app.ai import client as ai
from app.ai import guardrail, policy
from app.store import reset_store
from contracts import Guardrail

ENV = ("DRY_RUN", "GUARDRAIL_ID", "GUARDRAIL_VERSION", "GUARDRAIL_MODE")


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STORE_PATH", str(tmp_path / "test.sqlite"))
    reset_store()
    yield
    reset_store()


@pytest.mark.parametrize("env, expected", [
    ({"DRY_RUN": "1", "GUARDRAIL_ID": "gr123", "GUARDRAIL_VERSION": "1"}, guardrail.DryRunGuardrail),
    ({"DRY_RUN": "0", "GUARDRAIL_ID": "gr123", "GUARDRAIL_VERSION": "1"}, guardrail.BedrockGuardrail),
    ({"DRY_RUN": "0", "GUARDRAIL_ID": "gr123"}, guardrail.CheckerGuardrail),           # unpublished: no version
    ({"DRY_RUN": "0", "GUARDRAIL_VERSION": "1"}, guardrail.CheckerGuardrail),
    ({"DRY_RUN": "0"}, guardrail.CheckerGuardrail),
    ({"DRY_RUN": "0", "GUARDRAIL_ID": "gr123", "GUARDRAIL_VERSION": "1", "GUARDRAIL_MODE": "checker"},
     guardrail.CheckerGuardrail),
])
def test_mode_selection(monkeypatch, env, expected):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    g = guardrail.get_guardrail()
    assert type(g) is expected
    if expected is guardrail.BedrockGuardrail:
        assert (g.guardrail_id, g.version) == ("gr123", "1")


class FakeRuntime:
    def __init__(self, resp: dict) -> None:
        self.resp, self.calls = resp, []

    def apply_guardrail(self, **kw):
        self.calls.append(kw)
        return self.resp


@pytest.fixture()
def runtime(monkeypatch):
    fake = FakeRuntime({"action": "NONE", "assessments": []})
    monkeypatch.setattr(ai, "_runtime", lambda: fake)
    return fake


def test_bedrock_request_shape(runtime):
    g = guardrail.BedrockGuardrail("gr123", "1")
    assert g.check("answer", grounding="policy text", query="q?").status == "passed"
    assert g.check("bare text").status == "passed"
    with_q, bare = runtime.calls
    assert with_q["guardrailIdentifier"] == "gr123" and with_q["guardrailVersion"] == "1"
    assert with_q["source"] == "OUTPUT"
    assert [c["text"]["qualifiers"] for c in with_q["content"]] == [["grounding_source"], ["query"], ["guard_content"]]
    assert [c["text"]["qualifiers"] for c in bare["content"]] == [["guard_content"]]


def test_bedrock_record_grounded_without_query_uses_checker(runtime, monkeypatch):
    seen = []
    monkeypatch.setattr(guardrail.CheckerGuardrail, "check",
                        lambda self, output, grounding=None, query=None: seen.append(grounding) or
                        Guardrail(status="passed", reasons=[]))
    g = guardrail.BedrockGuardrail("gr123", "1").check("Insulin lispro 4 units due 04:00.", grounding='{"meds": 1}')
    assert g.status == "passed" and seen == ['{"meds": 1}'] and runtime.calls == []  # packets/digest


def test_bedrock_intervened_reasons(runtime):
    runtime.resp = {"action": "GUARDRAIL_INTERVENED",
                    "assessments": [{"topicPolicy": {"topics": []}, "contextualGroundingPolicy": {"filters": []}}]}
    g = guardrail.BedrockGuardrail("gr123", "1").check("Give 8 units now.", grounding="p", query="q")
    assert g.status == "intervened" and g.reasons == ["topicPolicy", "contextualGroundingPolicy"]


def test_checker_fallback_still_runs(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")  # ai.call returns the checker's canned pass verdict, zero network
    assert guardrail.CheckerGuardrail().check("x", grounding="y", query="z") == Guardrail(status="passed", reasons=[])


def test_intervention_replaces_policy_answer(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")

    class Blocks:
        def check(self, output, *, grounding=None, query=None):
            return Guardrail(status="intervened", reasons=["topicPolicy"])

    monkeypatch.setattr(policy, "get_guardrail", lambda: Blocks())
    q = "How do I get a stat lab with the order system down?"  # grounded reply, but the guardrail blocks it
    a = policy.ask(q)
    assert a.grounded is False and a.answer.startswith(policy.BLOCKED)
    assert a.guardrail.status == "intervened" and a.guardrail.reasons == ["topicPolicy"]
    assert [c.section_id for c in a.citations] == [s.section_id for s in policy.nearest(q)]
