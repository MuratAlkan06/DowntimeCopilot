"""Snapshot scheduler on the simulated clock, and `simulator.seed --fresh`."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.snapshot import scheduled_pull, should_pull
from app.store import get_store
from simulator import main as sim
from simulator import seed
from tests.test_api import api  # noqa: F401 - shared DRY_RUN app fixture

T0 = datetime(2026, 10, 24, 2, 45, tzinfo=timezone.utc)


@pytest.mark.parametrize("phase, minutes_since, ehr_up, expected", [
    ("before", None, True, True),        # nothing stored yet
    ("before", 14, True, False),         # under 15 simulated minutes
    ("before", 15, True, True),          # exactly 15
    ("before", 45, True, True),
    ("before", 30, False, False),        # EHR down (sim outage / restored)
    ("downtime", 30, True, False),       # never overwrite the pre-outage baseline
    ("downtime", None, True, False),
    ("recovery", 120, True, False),
])
def test_should_pull_phase_and_sim_minute_gating(phase, minutes_since, ehr_up, expected):
    newest = None if minutes_since is None else T0
    now = T0 + timedelta(minutes=minutes_since or 0)
    assert should_pull(phase, now, newest, ehr_up=ehr_up) is expected


def _pulls() -> int:
    return sum(e.action == "snapshot.pull" for e in get_store().list_audit(1000))


def test_scheduled_pull_follows_sim_clock_and_keeps_baseline(api):  # noqa: F811
    simc = TestClient(sim.app)
    assert scheduled_pull() is True and _pulls() == 1          # empty store, "before"
    assert scheduled_pull() is False                            # 0 sim-min later
    simc.post("/clock/advance", json={"minutes": 14})
    assert scheduled_pull() is False
    simc.post("/clock/advance", json={"minutes": 1})
    assert scheduled_pull() is True and _pulls() == 2
    baseline = get_store().latest_snapshot("pre")
    assert api.post("/downtime/start").status_code == 200
    simc.post("/clock/advance", json={"minutes": 60})
    assert scheduled_pull() is False and _pulls() == 2          # downtime: no pull
    assert get_store().latest_snapshot("pre") == baseline


def test_seed_fresh_clears_store(api, monkeypatch, capsys):  # noqa: F811
    api.post("/snapshot/pull")
    api.post("/downtime/start")
    assert get_store().list_packets() and get_store().get_kv("phase") == "downtime"
    simc = TestClient(sim.app)
    monkeypatch.setenv("SIM_URL", "http://sim")
    monkeypatch.setenv("API_URL", "http://api")

    def fake_post(url: str, **_kw) -> httpx.Response:
        host, path = url.split("//", 1)[1].split("/", 1)
        return (simc if host == "sim" else api).post(f"/{path}")

    monkeypatch.setattr(seed.httpx, "post", fake_post)
    assert seed.main(["--fresh"]) == 0
    store = get_store()
    assert store.list_packets() == [] and store.get_kv("phase") is None
    assert len(store.latest_snapshot("pre")) == 12
    status = api.get("/status").json()
    assert status["phase"] == "before" and status["outage_started_at"] is None and status["patients"] == 12
    assert "seeded 12 patients" in capsys.readouterr().out
