"""HTTP client for the simulated BCA feed. Tests replace `make_client` with a TestClient."""
from __future__ import annotations

from datetime import datetime

import httpx

from app import config
from app.store import get_store
from contracts import PatientSnapshot


class SimUnavailable(RuntimeError):
    pass


def make_client() -> httpx.Client:
    return httpx.Client(base_url=config.sim_url(), timeout=3.0)


def _call(method: str, path: str) -> httpx.Response:
    try:
        r = make_client().request(method, path)
    except httpx.HTTPError as exc:
        raise SimUnavailable(f"BCA feed unreachable: {exc}") from exc
    if r.status_code >= 400:
        raise SimUnavailable(f"BCA feed {path} returned {r.status_code}: {r.text[:200]}")
    return r


def pull_snapshot() -> list[PatientSnapshot]:
    return [PatientSnapshot.model_validate(p) for p in _call("GET", "/bca/snapshot").json()]


def now() -> datetime:
    """Simulated clock; falls back to the last clock seen, then the latest snapshot time."""
    store = get_store()
    try:
        value = _call("GET", "/clock").json()["now"]
        store.set_kv("sim_now", value)
        return datetime.fromisoformat(value)
    except SimUnavailable:
        cached = store.get_kv("sim_now")
        if cached:
            return datetime.fromisoformat(cached)
        snap = store.latest_snapshot()
        if snap:
            return max(p.snapshot_time for p in snap)
        from app.store import utcnow
        return utcnow()


def outage_start() -> bool:
    try:
        _call("POST", "/outage/start")
        return True
    except SimUnavailable:
        return False


def restore() -> tuple[list[PatientSnapshot], list[dict]]:
    changes = _call("POST", "/outage/restore").json().get("changes", [])
    return pull_snapshot(), changes
