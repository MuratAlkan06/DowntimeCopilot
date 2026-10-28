"""Route override (#76, app.ai.client.set_route_override): strict per-job routing for the model comparison harness.

An overridden job runs on the requested model on every thread (packets run on the /downtime/start worker pool) and
never falls back to Haiku; a job that is not overridden routes and falls back exactly as before. Offline: DRY_RUN, or
the Bedrock call stubbed at app.ai.client._converse.
"""
from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import clock, sim_client
from app.ai import client as ai
from app.ai import packets
from app.store import get_store, reset_store
from simulator import main as sim


@pytest.fixture(autouse=True)
def no_override():
    ai.set_route_override(None)
    yield
    ai.set_route_override(None)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_PATH", str(tmp_path / "test.sqlite"))
    reset_store()
    yield get_store()
    reset_store()


@pytest.fixture()
def api(store, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("SNAPSHOT_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("SYNC_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("PACKET_CONCURRENCY", "4")  # the production pool size: packets run on worker threads
    monkeypatch.setattr(sim_client, "make_client", lambda: TestClient(sim.app))
    sim.state.reset(7)
    clock.reset()
    from app.main import app
    yield TestClient(app, headers={"X-Role": "charge_nurse"})  # no lifespan: no scheduler, no sync worker
    clock.reset()


def _reply(text: str = '{"ok": true}') -> dict:
    return {"output": {"message": {"content": [{"text": text}]}}, "usage": {"inputTokens": 10, "outputTokens": 5}}


def _stub(monkeypatch, fail: set[str]) -> list[str]:
    """Live path with _converse stubbed: models in `fail` raise, others answer. Returns the models called."""
    monkeypatch.setenv("DRY_RUN", "0")
    called: list[str] = []

    def fake(model_id, system, user_text, image, image_format, max_tokens):
        called.append(model_id)
        if model_id in fail:
            raise RuntimeError(f"bedrock down for {model_id}")
        return _reply()

    monkeypatch.setattr(ai, "_converse", fake)
    return called


# ------------------------------------------------------------------ the thread pool sees the override

def test_downtime_start_under_override_routes_every_packet_through_the_worker_pool(api, monkeypatch):
    threads: list[str] = []
    generate = packets.generate_packet

    def on_thread(*a, **kw):
        threads.append(threading.current_thread().name)
        return generate(*a, **kw)

    monkeypatch.setattr(packets, "generate_packet", on_thread)
    assert api.post("/snapshot/pull").status_code == 200
    with ai.route_override({"packet": ai.HAIKU_4_5}):
        r = api.post("/downtime/start", json={"simulate_outage": True})
    assert r.status_code == 200, r.text
    assert ai._route_override is None  # restored after the block
    assert len(threads) == 12 and all(t.startswith("packet") for t in threads)  # ThreadPoolExecutor workers
    assert threading.main_thread().name not in threads
    rows = [c for c in get_store().list_costs() if c["job"] == "packet"]
    assert len(rows) == 12 and {c["model_id"] for c in rows} == {f"dry-run:{ai.HAIKU_4_5}"}
    assert {p["model_id"] for p in r.json()} == {f"dry-run:{ai.HAIKU_4_5}"}
    assert {c["job"] for c in get_store().list_costs()} == {"packet"}  # DRY_RUN guardrail: no checker call


def test_downtime_start_without_override_keeps_the_routes_table(api):
    assert api.post("/snapshot/pull").status_code == 200
    assert api.post("/downtime/start").status_code == 200
    assert {c["model_id"] for c in get_store().list_costs()} == {f"dry-run:{ai.ROUTES['packet']}"}


# ------------------------------------------------------------------ validation

@pytest.mark.parametrize("mapping, match", [
    ({"packets": ai.NOVA_2_LITE}, "unknown job"),
    ({"packet": "us.anthropic.claude-opus-9"}, "unpriced model"),
    ({"packet": "dry-run:" + ai.NOVA_2_LITE}, "unpriced model"),
], ids=["unknown-job", "unpriced-model", "dry-run-id"])
def test_override_refuses_unknown_jobs_and_unpriced_models(mapping, match):
    ai.set_route_override({"policy": ai.HAIKU_4_5})
    with pytest.raises(ValueError, match=match):
        ai.set_route_override(mapping)
    with pytest.raises(ValueError, match=match):
        with ai.route_override(mapping):
            pass
    assert ai._route_override == {"policy": ai.HAIKU_4_5}  # a refused mapping changes nothing


def test_override_mapping_is_copied():
    mapping = {"packet": ai.NOVA_2_LITE}
    ai.set_route_override(mapping)
    mapping["packet"] = ai.SONNET_4_6
    assert ai._route_override == {"packet": ai.NOVA_2_LITE}


def test_call_without_override_routes_per_the_routes_table(store, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    for job in ai.ROUTES:
        assert ai.call(job, "packet", "{}", mock=lambda: "{}").model_id == f"dry-run:{ai.ROUTES[job]}"
    with ai.route_override({"form_read": ai.SONNET_4_6}):
        assert ai.call("form_read", "form_read", "{}", mock=lambda: "{}").model_id == f"dry-run:{ai.SONNET_4_6}"
        assert ai.call("structure", "form_read", "{}", mock=lambda: "{}").model_id == f"dry-run:{ai.NOVA_2_LITE}"
    assert ai.call("form_read", "form_read", "{}", mock=lambda: "{}").model_id == f"dry-run:{ai.NOVA_2_LITE}"


# ------------------------------------------------------------------ fallback semantics

def test_overridden_job_never_falls_back_and_records_no_cost(store, monkeypatch):
    called = _stub(monkeypatch, fail={ai.NOVA_2_LITE})
    with ai.route_override({"packet": ai.NOVA_2_LITE}):
        with pytest.raises(RuntimeError, match="bedrock down"):
            ai.call("packet", "packet", "{}", mock=lambda: "{}")
    assert called == [ai.NOVA_2_LITE]  # no Haiku attempt
    assert store.list_costs() == [] and not [e for e in store.list_audit(100) if e.action == "ai.call"]


def test_overridden_job_on_success_records_the_requested_model(store, monkeypatch):
    called = _stub(monkeypatch, fail=set())
    with ai.route_override({"packet": ai.NOVA_2_LITE}):
        res = ai.call("packet", "packet", "{}", mock=lambda: "{}")
    assert called == [ai.NOVA_2_LITE] and res.model_id == ai.NOVA_2_LITE and not res.dry_run
    assert [(c["job"], c["model_id"], c["dry_run"]) for c in store.list_costs()] == [
        ("packet", ai.NOVA_2_LITE, False)]
    assert res.usd == ai.estimate_usd(ai.NOVA_2_LITE, 10, 5)


def test_job_not_overridden_still_falls_back_to_haiku(store, monkeypatch):
    called = _stub(monkeypatch, fail={ai.SONNET_4_6, ai.NOVA_2_LITE})
    with ai.route_override({"form_read": ai.SONNET_4_6}):  # another job's override changes nothing here
        res = ai.call("packet", "packet", "{}", mock=lambda: "{}")
        checker = ai.call("checker", "checker", "{}", mock=lambda: "{}")
    assert called == [ai.SONNET_4_6, ai.FALLBACK_MODEL, ai.NOVA_2_LITE, ai.FALLBACK_MODEL]
    assert res.model_id == checker.model_id == ai.FALLBACK_MODEL
    assert [(c["job"], c["model_id"]) for c in store.list_costs()] == [
        ("packet", ai.FALLBACK_MODEL), ("checker", ai.FALLBACK_MODEL)]


def test_haiku_failure_without_override_propagates_as_before(store, monkeypatch):
    called = _stub(monkeypatch, fail={ai.FALLBACK_MODEL, ai.SONNET_4_6})
    with pytest.raises(RuntimeError):
        ai.call("packet", "packet", "{}", mock=lambda: "{}")
    assert called == [ai.SONNET_4_6, ai.FALLBACK_MODEL] and store.list_costs() == []


def test_dry_run_under_override_is_labeled_with_the_requested_model(store, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    with ai.route_override({"policy": ai.NOVA_2_LITE}):
        res = ai.call("policy", "policy", json.dumps({"q": 1}), mock=lambda: "{}")
    assert res.model_id == f"dry-run:{ai.NOVA_2_LITE}" and res.dry_run and res.usd == 0.0
    assert store.list_costs()[0]["model_id"] == f"dry-run:{ai.NOVA_2_LITE}"
