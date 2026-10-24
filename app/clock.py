"""Unit clock for server-side stamps (audit events, cost rows, digest generated_at).

The unit runs on the simulator's clock. Every successful sim_client read of /clock calls `sync()`;
`unit_now()` is that simulator time plus the real time elapsed since the sync (time.monotonic), so
stamps between syncs stay in the unit-clock era the packets, captured records and shift window use.

`unit_now()` never goes backwards within a process. A frozen simulator clock re-reports the same time
on every /clock read, so each sync rewinds the base; without the clamp a later audit event could be
stamped before an earlier one. sync() still moves the base; only the returned value is clamped. The
floor is dropped when a new clock era starts: the first sync after the wall-clock fallback, or a sync
to a strictly earlier simulator time (the simulator was reset, e.g. `simulator.seed --fresh`).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

_lock = threading.Lock()
_synced: tuple[datetime, float] | None = None  # (simulator now, time.monotonic() at that read)
_last: datetime | None = None  # highest value unit_now() has returned since the last reset()


def sync(sim_now: datetime) -> None:
    global _synced, _last
    with _lock:
        if _synced is None or sim_now < _synced[0]:
            _last = None  # new clock era: do not clamp the simulator's time to a wall-clock or pre-reset floor
        _synced = (sim_now, time.monotonic())


def unit_now() -> datetime:
    global _last
    with _lock:
        if _synced is None:
            # Never synced in this process (the seed CLI, or the app before its first /clock read):
            # fall back to wall-clock UTC rather than invent a unit time.
            now = datetime.now(timezone.utc)
        else:
            sim_now, at = _synced
            now = sim_now + timedelta(seconds=time.monotonic() - at)
        if _last is not None and now < _last:
            now = _last  # equal is fine: display order comes from the store's seq, not ts
        _last = now
        return now


def reset() -> None:
    """Forget the last sync and the monotonic floor (tests)."""
    global _synced, _last
    with _lock:
        _synced = None
        _last = None
