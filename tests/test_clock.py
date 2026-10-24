"""Unit clock (app.clock): audit, cost and digest stamps follow the simulator clock once it has been read."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import clock, sim_client
from app.ai.digest import build_digest
from app.store import get_store, reset_store

SIM_NOW = datetime(2026, 10, 24, 3, 0, tzinfo=timezone.utc)  # demo unit clock after the +15 min advance
CLOSE = timedelta(seconds=5)


@pytest.fixture()
def unit(tmp_path, monkeypatch):
    """Store on a temp file and a mocked simulator whose /clock reports SIM_NOW."""
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("STORE_PATH", str(tmp_path / "clock.sqlite"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/clock"
        return httpx.Response(200, json={"now": SIM_NOW.isoformat(), "outage": False, "restored": False})

    monkeypatch.setattr(sim_client, "make_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler), base_url="http://sim"))
    clock.reset()
    reset_store()
    yield get_store()
    clock.reset()
    reset_store()


def wall() -> datetime:
    return datetime.now(timezone.utc)


def test_never_synced_falls_back_to_wall_clock(unit):
    assert abs(clock.unit_now() - wall()) < CLOSE
    ev = unit.audit("rn", "test.event", "test")
    assert abs(ev.ts - wall()) < CLOSE


def test_unit_now_is_sim_time_plus_elapsed(monkeypatch):
    mono = iter([100.0, 130.0])
    monkeypatch.setattr(clock.time, "monotonic", lambda: next(mono))
    clock.sync(SIM_NOW)
    try:
        assert clock.unit_now() == SIM_NOW + timedelta(seconds=30)
    finally:
        clock.reset()


@pytest.mark.parametrize("read", [sim_client.now, sim_client.clock])
def test_audit_and_cost_stamps_follow_sim_clock_after_sync(unit, read):
    read()  # any successful /clock read syncs the unit clock
    ev = unit.audit("rn", "test.event", "test")
    assert abs(ev.ts - SIM_NOW) < CLOSE  # 03:00 unit time, not the wall clock
    assert unit.list_audit(1)[0].ts == ev.ts
    unit.record_cost("digest", "dry-run:model", 1, 1, 1, 0.0, True)
    assert abs(datetime.fromisoformat(unit.list_costs()[-1]["ts"]) - SIM_NOW) < CLOSE


def test_digest_generated_at_on_unit_clock(unit):
    end = sim_client.now()
    d = build_digest([], [], [], end - timedelta(minutes=15), end)
    assert abs(d.generated_at - SIM_NOW) < CLOSE
