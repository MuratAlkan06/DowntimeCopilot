"""Packet faithfulness: multi-path source_path, alerts never a source, regenerate-once then omit."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.ai import client as ai
from app.ai import packets
from simulator.state import load_patients

PT12 = next(p for p in load_patients() if p.patient_id == "pt-12")
SNAP = json.loads(PT12.model_dump_json())
NOW = datetime(2026, 10, 24, 2, 47, tzinfo=timezone.utc)


def test_multi_path_accepted():
    assert packets.resolve(SNAP, "medications[0].next_due, medications[0].high_alert")
    assert packets.resolve(SNAP, "allergies[0],code_status")
    kept, rejected = packets.faithful(
        {"background": [{"text": "insulin due 04:00 HIGH-ALERT",
                         "source_path": "medications[0].next_due,medications[0].high_alert"}]}, SNAP)
    assert not rejected
    assert kept["background"][0].source_path == "medications[0].next_due, medications[0].high_alert"


@pytest.mark.parametrize("path", [
    "medications[0].next_due, medications[99].dose",   # one bad index
    "medications[0].next_due, nonexistent_field",      # one bad field
    "code_status,",                                     # trailing empty path
    "",
])
def test_one_bad_path_rejects_whole_line(path):
    assert not packets.resolve(SNAP, path)
    kept, rejected = packets.faithful({"situation": [{"text": "x", "source_path": path}]}, SNAP)
    assert kept["situation"] == [] and rejected == [{"section": "situation", "text": "x", "source_path": path}]


def test_alerts_are_never_a_source():
    doc = {**SNAP, "alerts": [{"rule_id": "R-DUE-01", "text": "insulin due"}]}
    assert not packets.resolve(doc, "alerts[0]")
    assert not packets.resolve(doc, "medications[0].next_due, alerts[0]")
    assert packets.resolve(doc, "medications[0].next_due")


def _reply(sbar: dict) -> ai.AIResult:
    return ai.AIResult(json.dumps({"sbar": sbar}), "test-model", 100, 50, 10, 0.001, False)


GOOD = {"text": "Code status: Full code", "source_path": "code_status"}
BAD = {"text": "R-DUE-01 insulin due", "source_path": "alerts[0]"}
FIXED = {"text": "Insulin lispro due 04:00", "source_path": "medications[0].next_due, medications[0].high_alert"}


@pytest.fixture()
def scripted(monkeypatch):
    """Replace the Bedrock call with scripted replies; record the user payload of each call."""
    monkeypatch.setenv("DRY_RUN", "1")  # guardrail adapter: dry-run pass
    calls: list[dict] = []

    def install(*replies: dict):
        def fake_call(job, prompt, user_text, **kw):
            calls.append(json.loads(user_text))
            return _reply(replies[len(calls) - 1])
        monkeypatch.setattr(packets.ai, "call", fake_call)
        return calls
    return install


def test_clean_packet_is_generated_once(scripted):
    calls = scripted({"situation": [GOOD]})
    pk = packets.generate_packet(PT12, [], NOW)
    assert len(calls) == 1 and pk.sbar.situation[0].text == GOOD["text"]
    assert not any("faithfulness" in r for r in pk.guardrail.reasons)
    assert pk.generated_at == NOW  # sim clock, never wall clock


def test_regeneration_fixes_rejected_line(scripted):
    calls = scripted({"situation": [GOOD, BAD]}, {"situation": [GOOD, FIXED]})
    pk = packets.generate_packet(PT12, [], NOW)
    assert len(calls) == 2 and calls[1]["rejected"][0]["source_path"] == "alerts[0]"
    assert [i.text for i in pk.sbar.situation] == [GOOD["text"], FIXED["text"]]
    assert not any(packets.is_omission(i) for i in pk.sbar.situation)
    assert "faithfulness: rejected 1 line(s) without a resolvable source_path" in pk.guardrail.reasons
    assert "faithfulness: regenerated once; omitted 0 unverifiable line(s)" in pk.guardrail.reasons
    assert pk.cost.tokens_in == 200 and pk.cost.usd == pytest.approx(0.002)


def test_still_unverifiable_after_one_regeneration_is_omitted(scripted):
    calls = scripted({"situation": [GOOD, BAD], "recommendation": [BAD]},
                     {"situation": [GOOD, BAD], "recommendation": [BAD]})
    pk = packets.generate_packet(PT12, [], NOW)
    assert len(calls) == 2  # regenerated exactly once
    assert pk.sbar.situation[0].text == GOOD["text"]
    for section in (pk.sbar.situation[1:], pk.sbar.recommendation):
        assert [(i.text, i.source_path) for i in section] == [(packets.OMISSION_TEXT, packets.OMISSION_SOURCE)]
    assert "faithfulness: regenerated once; omitted 2 unverifiable line(s)" in pk.guardrail.reasons
    assert not packets.resolve(SNAP, packets.OMISSION_SOURCE)  # the omission item never reads as a fact
