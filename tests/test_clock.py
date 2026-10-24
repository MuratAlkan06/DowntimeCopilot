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


class Mono:
    """Controllable stand-in for time.monotonic()."""

    def __init__(self, t: float = 100.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_frozen_sim_resyncs_never_rewind_unit_now(monkeypatch):
    """A frozen simulator re-reports the same time on every /clock read; interleaved stamps stay ordered."""
    mono = Mono()
    monkeypatch.setattr(clock.time, "monotonic", mono)
    clock.reset()
    try:
        seen = []
        for _ in range(5):
            clock.sync(SIM_NOW)  # every status poll re-bases on the same frozen sim time
            mono.t += 7.0
            seen.append(clock.unit_now())
            mono.t += 3.0
            seen.append(clock.unit_now())
        assert seen == sorted(seen)
        assert seen[0] == SIM_NOW + timedelta(seconds=7)
        assert seen[-1] >= seen[1] == SIM_NOW + timedelta(seconds=10)  # clamped, not rewound to SIM_NOW + 7s
    finally:
        clock.reset()


def test_advancing_sim_clock_unchanged_by_clamp(monkeypatch):
    mono = Mono()
    monkeypatch.setattr(clock.time, "monotonic", mono)
    clock.reset()
    try:
        clock.sync(SIM_NOW)
        mono.t += 30
        assert clock.unit_now() == SIM_NOW + timedelta(seconds=30)
        clock.sync(SIM_NOW + timedelta(minutes=45))  # POST /clock/advance +45
        mono.t += 2
        assert clock.unit_now() == SIM_NOW + timedelta(minutes=45, seconds=2)
    finally:
        clock.reset()


def test_simulator_reset_starts_a_new_era(monkeypatch):
    """seed --fresh rewinds the simulator: stamps follow it rather than the previous run's high-water mark."""
    mono = Mono()
    monkeypatch.setattr(clock.time, "monotonic", mono)
    clock.reset()
    try:
        clock.sync(SIM_NOW + timedelta(hours=1))
        late = clock.unit_now()
        clock.sync(SIM_NOW)  # strictly earlier sim time = reset, not a frozen re-read
        mono.t += 1
        assert clock.unit_now() == SIM_NOW + timedelta(seconds=1) < late
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


def test_frozen_clock_audit_is_newest_first_by_seq_with_ordered_stamps(unit):
    """GET /audit order (the UI shows it as-is): newest first by store seq; stamps never run backwards."""
    for action in ("backload.verify", "backload.entered", "audit.list"):
        sim_client.now()  # status polls re-sync to the same frozen sim time between events
        unit.audit("rn", action, "captured_record", "rec-1")
    events = unit.list_audit()
    assert [e.action for e in events[:3]] == ["audit.list", "backload.entered", "backload.verify"]
    stamps = [e.ts for e in reversed(events)]
    assert stamps == sorted(stamps)


def test_digest_generated_at_on_unit_clock(unit):
    end = sim_client.now()
    d = build_digest([], [], [], end - timedelta(minutes=15), end)
    assert abs(d.generated_at - SIM_NOW) < CLOSE
