"""S13 readiness: pure scorer (weights, math, injected clock, no model), summary checker, drill isolation."""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import clock, drill, readiness, sim_client
from app.store import get_store, reset_store
from simulator import main as sim

NOW = datetime(2026, 10, 24, 3, 0, tzinfo=timezone.utc)
ALL_ROLES = readiness.ACK_ROLES


def inputs(fresh: int = 4, stale: int = 0, covered: int | None = None, acked: tuple[str, ...] = ALL_ROLES,
           templates: int = 3) -> readiness.Inputs:
    times = {f"pt-{i}": NOW - timedelta(minutes=5) for i in range(fresh)}
    times |= {f"pt-s{i}": NOW - timedelta(minutes=40) for i in range(stale)}
    pids = sorted(times)
    covered = len(pids) if covered is None else covered
    return readiness.Inputs(snapshot_times=times, packets_ok={pid: i < covered for i, pid in enumerate(pids)},
                            roles_required=ALL_ROLES, roles_acknowledged=frozenset(acked),
                            templates={f: i < templates for i, f in enumerate(readiness.TEMPLATE_FORMS)})


def at(t: datetime):
    return lambda: t


# ------------------------------------------------------------------ scorer

def test_weights_are_documented_and_sum_to_100():
    assert sum(readiness.WEIGHTS.values()) == 100
    assert set(readiness.WEIGHTS) == {"snapshot_freshness", "packet_coverage", "checklist_ack", "forms_templates"}
    doc = readiness.__doc__
    for name, weight in readiness.WEIGHTS.items():
        assert re.search(rf"{name}\s+{weight}\b", doc), f"{name} weight {weight} not in the module docstring"


def test_perfect_inputs_score_100_and_every_component_reports_score_weight_detail():
    r = readiness.score(inputs(), clock=at(NOW))
    assert r["score"] == 100
    for name, comp in r["components"].items():
        assert set(comp) == {"score", "weight", "detail"}
        assert comp["score"] == 100 and comp["weight"] == readiness.WEIGHTS[name]


def test_component_math_and_single_rounding():
    # 11 of 12 fresh (pt-14 style), 12 of 12 packets, 0 of 4 roles, 3 of 3 templates: the seeded shape.
    r = readiness.score(inputs(fresh=11, stale=1, acked=()), clock=at(NOW))
    c = r["components"]
    assert (c["snapshot_freshness"]["score"], c["packet_coverage"]["score"],
            c["checklist_ack"]["score"], c["forms_templates"]["score"]) == (92, 100, 0, 100)
    assert r["score"] == round(35 * 11 / 12 + 30 + 0 + 20) == 82
    assert c["snapshot_freshness"]["detail"] == "11 of 12 patient snapshots are at most 30 min old"
    assert c["checklist_ack"]["detail"] == "0 of 4 roles have opened their checklist"
    # Partial everything: 2 of 3 fresh, 1 of 3 packets, 2 of 4 roles, 1 of 3 templates.
    r = readiness.score(inputs(fresh=2, stale=1, covered=1, acked=("rn", "pharmacy"), templates=1), clock=at(NOW))
    assert [x["score"] for x in r["components"].values()] == [67, 33, 50, 33]
    assert r["score"] == 48  # 70/3 + 10 + 15/2 + 20/3 = 47.5 exactly: half up, no float or banker's rounding


def test_component_scores_round_half_up():
    eighth = readiness.Inputs(snapshot_times={f"pt-{i}": NOW for i in range(8)}, packets_ok={"pt-0": True})
    assert readiness.score(eighth, clock=at(NOW))["components"]["packet_coverage"]["score"] == 13  # 12.5


def test_empty_denominators_score_zero():
    r = readiness.score(readiness.Inputs(), clock=at(NOW))
    assert r["score"] == 0 and all(c["score"] == 0 for c in r["components"].values())


def test_stale_boundary_matches_r_stale_01():
    exactly_30 = readiness.Inputs(snapshot_times={"pt-1": NOW - timedelta(minutes=30)})
    over_30 = readiness.Inputs(snapshot_times={"pt-1": NOW - timedelta(minutes=30, seconds=1)})
    assert readiness.score(exactly_30, clock=at(NOW))["components"]["snapshot_freshness"]["score"] == 100
    assert readiness.score(over_30, clock=at(NOW))["components"]["snapshot_freshness"]["score"] == 0


def test_clock_is_injected_and_the_score_is_deterministic():
    x = inputs()
    assert readiness.score(x, clock=at(NOW)) == readiness.score(x, clock=at(NOW))
    later = readiness.score(x, clock=at(NOW + timedelta(minutes=26)))  # every snapshot now 31 min old
    assert later["components"]["snapshot_freshness"]["score"] == 0 and later["score"] == 65
    with pytest.raises(TypeError):
        readiness.score(x)  # no default clock: the scorer never reads time by itself


def test_scorer_never_calls_a_model_or_reads_time():
    src = Path(readiness.__file__).read_text()
    assert not re.search(r"\b(boto3|botocore|bedrock|anthropic|app\.ai|from app import ai|sim_client|httpx|"
                         r"app\.store|app\.clock|unit_now|datetime\.now|time\.time)\b", src, re.IGNORECASE)
    out = subprocess.run([sys.executable, "-c", "import sys, app.readiness; print(sorted(m for m in sys.modules "
                          "if m.split('.')[0] in ('boto3', 'botocore', 'httpx') or m.startswith('app.ai') "
                          "or m in ('app.store', 'app.sim_client', 'app.drill')))"],
                         capture_output=True, text=True, check=True, cwd=Path(readiness.__file__).parent.parent)
    assert out.stdout.strip() == "[]"


# ------------------------------------------------------------------ summary checker

def test_canned_summary_passes_its_own_check():
    for x in (inputs(), inputs(fresh=11, stale=1, acked=()), readiness.Inputs()):
        r = readiness.score(x, clock=at(NOW))
        text = readiness.canned_summary(r)
        assert readiness.check_summary(text, r) == [] and len(readiness.sentences(text)) == 2


def test_checker_rejects_wrong_sentence_count_and_divergent_numbers():
    r = readiness.score(inputs(fresh=11, stale=1, acked=()), clock=at(NOW))  # 82; 92 / 100 / 0 / 100
    ok = "The drill scored 82 out of 100. Checklist acknowledgment is lowest at 0, with 0 of 4 roles."
    assert readiness.check_summary(ok, r) == []
    assert readiness.check_summary("The drill scored 82 out of 100.", r) == ["expected 2 sentences, got 1"]
    assert "expected 2 sentences, got 3" in readiness.check_summary(ok + " Fix it now.", r)
    bad = readiness.check_summary("The drill scored 83 out of 100. Freshness is 91.7 percent.", r)
    assert bad == ["numbers not in the score: 83, 91.7"]


# ------------------------------------------------------------------ drill (API, DRY_RUN, isolation)

@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("STORE_PATH", str(tmp_path / "live.sqlite"))
    monkeypatch.setenv("SNAPSHOT_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("SYNC_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("SYNC_EXPORT_DIR", str(tmp_path / "logs"))
    calls: list[tuple[str, str]] = []

    class RecordingSim(TestClient):
        def request(self, method, url, *a, **kw):
            calls.append((method.upper(), str(url)))
            return super().request(method, url, *a, **kw)

    monkeypatch.setattr(sim_client, "make_client", lambda: RecordingSim(sim.app))
    monkeypatch.setattr(drill, "_last", None)
    sim.state.reset(7)
    clock.reset()
    reset_store()
    from app.main import app
    with TestClient(app) as c:
        c.sim_calls = calls
        yield c
    clock.reset()
    reset_store()


def live_state() -> tuple[str, list, int]:
    store = get_store()
    return (hashlib.sha256(Path(store.path).read_bytes()).hexdigest(), store.outbox(10_000), len(store.audit_log()))


def prime_live(c: TestClient) -> None:
    """A live store with real rows: a pull, a declared downtime, and one role acknowledgment."""
    assert c.post("/snapshot/pull").status_code == 200
    assert c.get("/checklists", headers={"X-Role": "rn"}).status_code == 200
    assert c.get("/checklists?role=pharmacy", headers={"X-Role": "charge_nurse"}).status_code == 200  # not own role


def test_readiness_is_empty_before_the_first_drill(api):
    assert api.get("/readiness").json() == {"score": None, "components": {}, "last_drill_at": None,
                                            "summary": None, "drill": None}


def test_drill_scores_seeded_data_in_dry_run(api):
    prime_live(api)
    r = api.post("/readiness/drill")
    assert r.status_code == 200, r.text
    body = r.json()
    c = body["components"]
    assert c["snapshot_freshness"]["detail"] == "11 of 12 patient snapshots are at most 30 min old"  # pt-14 lags
    assert c["packet_coverage"]["score"] == 100 and c["forms_templates"]["score"] == 100
    assert c["checklist_ack"]["detail"] == "1 of 4 roles have opened their checklist"  # rn only
    assert body["score"] == round(35 * 11 / 12 + 30 + 15 / 4 + 20) == 86
    assert body["last_drill_at"] == sim.state.now.isoformat()
    assert readiness.check_summary(body["summary"], body) == [] and len(readiness.sentences(body["summary"])) == 2
    d = body["drill"]
    assert [s["name"] for s in d["steps"]] == ["snapshot", "packets", "restore"] and all(s["ok"] for s in d["steps"])
    assert d["summary_source"] == "dry_run" and d["summary_model_id"].startswith("dry-run:") and d["summary_usd"] == 0
    assert api.get("/readiness").json() == body


def test_drill_writes_nothing_live_and_mirrors_nothing(api, monkeypatch):
    prime_live(api)
    from app.sync import base, worker
    puts = []
    monkeypatch.setattr(base, "get_mirror", lambda: puts.append("get_mirror"))
    monkeypatch.setattr(worker, "drain", lambda *a, **kw: puts.append("drain"))
    sim_before = (sim.state.now, sim.state.outage, sim.state.restored)
    before = live_state()
    api.sim_calls.clear()
    assert api.post("/readiness/drill").status_code == 200
    assert live_state() == before  # live file byte-stable, outbox rows identical, no audit row
    assert puts == []  # the sync worker and the mirrors were never reached
    assert (sim.state.now, sim.state.outage, sim.state.restored) == sim_before
    assert api.sim_calls and {m for m, _ in api.sim_calls} == {"GET"}  # simulator read only, never /outage/*
    assert get_store().get_kv("phase", "before") == "before"


def test_drill_runs_live_mode_packets_as_dry_run(api, monkeypatch):
    """With DRY_RUN off in the process, drill packets stay canned; only the summary goes to the model."""
    monkeypatch.setenv("DRY_RUN", "0")
    jobs = []

    def fake_converse(model_id, system, user_text, image, image_format, max_tokens):
        jobs.append(model_id)
        return {"output": {"message": {"content": [{"text": "Readiness scored 999 of 100. All good."}]}},
                "usage": {"inputTokens": 10, "outputTokens": 10}}

    from app.ai import client as ai
    monkeypatch.setattr(ai, "_converse", fake_converse)
    before = live_state()
    body = api.post("/readiness/drill").json()
    assert jobs == [ai.SONNET_4_6]  # one summary call; zero packet calls
    assert body["drill"]["summary_source"] == "canned"  # 999 diverges from the score: replaced
    assert body["drill"]["summary_check"] == ["numbers not in the score: 999"]
    assert readiness.check_summary(body["summary"], body) == []
    assert live_state() == before  # the summary's cost row went to the ephemeral store


def test_failed_mid_drill_leaves_live_state_and_last_result_untouched(api, monkeypatch):
    prime_live(api)
    first = api.post("/readiness/drill").json()
    before = live_state()
    real, n = drill.generate_packet, []

    def flaky(*a, **kw):
        n.append(1)
        if len(n) == 3:
            raise RuntimeError("boom")
        return real(*a, **kw)

    monkeypatch.setattr(drill, "generate_packet", flaky)
    r = api.post("/readiness/drill")
    assert r.status_code == 500 and "live state untouched" in r.json()["detail"]
    assert live_state() == before
    assert api.get("/readiness").json() == first
    assert not drill._lock.locked()


def test_drill_refuses_during_a_simulator_outage(api):
    sim.state.start_outage()
    before = live_state() if Path(get_store().path).exists() else None
    r = api.post("/readiness/drill")
    assert r.status_code == 409 and "outage" in r.json()["detail"]
    assert (live_state() if before else None) == before
