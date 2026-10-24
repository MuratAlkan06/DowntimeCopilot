"""Unit clock for server-side stamps (audit events, cost rows, digest generated_at).

The unit runs on the simulator's clock. Every successful sim_client read of /clock calls `sync()`;
`unit_now()` is that simulator time plus the real time elapsed since the sync (time.monotonic), so
stamps between syncs stay in the unit-clock era the packets, captured records and shift window use.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

_lock = threading.Lock()
_synced: tuple[datetime, float] | None = None  # (simulator now, time.monotonic() at that read)


def sync(sim_now: datetime) -> None:
    global _synced
    with _lock:
        _synced = (sim_now, time.monotonic())


def unit_now() -> datetime:
    with _lock:
        synced = _synced
    if synced is None:
        # Never synced in this process (the seed CLI, or the app before its first /clock read):
        # fall back to wall-clock UTC rather than invent a unit time.
        return datetime.now(timezone.utc)
    sim_now, at = synced
    return sim_now + timedelta(seconds=time.monotonic() - at)


def reset() -> None:
    """Forget the last sync (tests)."""
    global _synced
    with _lock:
        _synced = None
