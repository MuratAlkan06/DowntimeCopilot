"""Policy answers: grounding refusal with nearest sections (DRY_RUN, model reply mocked)."""
from __future__ import annotations

import json

import pytest

from app.ai import policy
from app.store import reset_store

STAT_Q = "How do I get a stat lab with the order system down?"


@pytest.fixture(autouse=True)
def dry_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("STORE_PATH", str(tmp_path / "test.sqlite"))
    reset_store()
    yield
    reset_store()


def mock_reply(monkeypatch, reply: dict | str) -> None:
    text = reply if isinstance(reply, str) else json.dumps(reply)
    monkeypatch.setattr(policy, "_mock_answer", lambda question: text)


def assert_refusal(a, question: str) -> None:
    assert a.grounded is False
    assert a.answer.startswith(policy.REFUSAL)
    near = policy.nearest(question)
    assert [c.section_id for c in a.citations] == [s.section_id for s in near]
    secs = policy.sections()
    for c in a.citations:  # best-effort quotes are verbatim section text
        assert policy._norm(c.quote) in policy._norm(secs[c.section_id].text)
        assert c.title == secs[c.section_id].title and f"{c.section_id} {c.title}" in a.answer


def test_unresolvable_citations_refuse_with_nearest_sections(monkeypatch):
    mock_reply(monkeypatch, {"answer": "Use the pneumatic tube and page the lab.", "citations": [
        {"section_id": "9.4", "title": "Stat specimens", "quote": "use the pneumatic tube"},
        {"section_id": "4.2.9", "title": "Stat labs", "quote": "page the lab"},
    ]})
    a = policy.ask(STAT_Q)
    assert_refusal(a, STAT_Q)
    assert "pneumatic tube and page" not in a.answer  # the ungrounded text is never shown
    assert 2 <= len(a.citations) <= 3 and a.citations[0].section_id == "4.2"
    assert a.guardrail.status == "passed"  # DRY_RUN guardrail still reports a status


def test_real_section_with_fabricated_quote_refuses(monkeypatch):
    mock_reply(monkeypatch, {"answer": "x", "citations": [
        {"section_id": "4.2", "title": "Stat labs", "quote": "stat labs may be sent by pneumatic tube"}]})
    assert_refusal(policy.ask(STAT_Q), STAT_Q)


@pytest.mark.parametrize("reply", [
    {"answer": "The policy does not cover this.", "citations": []},  # model cites nothing
    "not json at all",                                                # unparseable reply
])
def test_no_citations_refuse(monkeypatch, reply):
    q = "Who needs to double-check insulin before I give it?"
    mock_reply(monkeypatch, reply)
    a = policy.ask(q)
    assert_refusal(a, q)
    assert a.citations[0].section_id == "5"


def test_nearest_is_deterministic_and_bounded():
    q = "Who re-enters pharmacy orders when the system comes back?"
    first = [s.section_id for s in policy.nearest(q)]
    assert first == [s.section_id for s in policy.nearest(q)] and 1 <= len(first) <= 3
    assert "7" in first or "2" in first
    assert all(policy.sections()[sid].text.strip() for sid in first)  # header-only parents never listed


def test_no_overlap_refusal_points_to_binder(monkeypatch):
    mock_reply(monkeypatch, {"answer": "", "citations": []})
    a = policy.ask("zzz qqq")
    assert a.grounded is False and a.citations == []
    assert a.answer == f"{policy.REFUSAL} Check the paper downtime binder."


def test_grounded_reply_is_unchanged():
    a = policy.ask(STAT_Q)  # default mock cites 4.2 verbatim
    assert a.grounded and a.citations[0].section_id == "4.2"
    assert not a.answer.startswith(policy.REFUSAL)
