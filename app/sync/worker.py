"""Background drain of the sync outbox (docs/decisions.md S10).

The request path only appends outbox rows (app.store.sqlite, same transaction as the audit row or record
save). This daemon thread pushes them to the active mirror (app.sync.base), oldest first, and deletes each
row after its put succeeds, so the outbox survives a restart and a put is never lost. On an error it records
`sync.last_error` (operation and error code only, no item content) and backs off: interval x 2^failures,
capped at BACKOFF_MAX_S. An AccessDenied latches LocalFallback, writes one `sync.fallback` audit event, and
drains on locally. Nothing here raises into a handler; POST /sync drains through the same lock.
"""
from __future__ import annotations

import logging
import threading

from app.store import get_store
from app.sync import base
from app.sync.dynamodb import Denied

log = logging.getLogger(__name__)

BATCH = 25
BACKOFF_MAX_S = 15.0
_drain_lock = threading.Lock()


def describe(exc: Exception) -> str:
    from app.sync.s3 import error_code
    return error_code(exc) or type(exc).__name__


def drain(store=None, mirror=None, limit: int = BATCH) -> int:
    """Push up to `limit` outbox rows; returns how many were mirrored. Raises the first mirror error after
    recording it (rows already mirrored stay mirrored; the failed one and the rest stay queued)."""
    store = store or get_store()
    with _drain_lock:
        mirror = mirror or base.get_mirror()
        done = 0
        for seq, pk, sk, body in store.outbox(limit):
            try:
                mirror.put(pk, sk, body)
            except Denied as exc:
                if mirror.name == "dynamodb":
                    base.deny(exc.reason)
                    log.warning("DynamoDB denied (%s); sync falls back to the local export", exc.reason)
                    store.audit("system", "sync.fallback", "sync", "local", {"reason": exc.reason})
                store.set_kv("sync.last_error", exc.reason)
                raise
            except Exception as exc:
                store.set_kv("sync.last_error", f"{mirror.name}: {describe(exc)}")
                raise
            done += store.outbox_done(seq, pk, sk)
        return done


def drain_all(store=None) -> tuple[int, str | None]:
    """Drain until empty or the first error: (mirrored, error). Never raises (POST /sync)."""
    total = 0
    try:
        while (n := drain(store)) == BATCH:
            total += n
        return total + n, None
    except Denied as exc:  # latched: the next call drains locally
        return total, exc.reason
    except Exception as exc:  # noqa: BLE001 - a sync failure never reaches the handler
        return total, describe(exc)


def status(store=None) -> dict:
    """GET /sync/status. Same keys in every mode; counts, a unit-clock stamp and an error code only (no PII)."""
    store = store or get_store()
    return {
        "mode": base.mode(),
        "mirrored": int(store.get_kv("sync.mirrored", "0")),
        "pending": store.outbox_pending(),
        "last_success_ts": store.get_kv("sync.last_success_ts"),
        "last_error": store.get_kv("sync.last_error") or base.denied(),
    }


def run(stop: threading.Event, interval: float) -> None:
    failures = 0
    while not stop.is_set():
        try:
            n = drain()
            failures, wait = 0, (0 if n == BATCH else interval)
        except Denied:
            failures, wait = 0, 0  # LocalFallback is latched; drain locally now
        except Exception as exc:  # noqa: BLE001 - the worker must never die
            failures += 1
            wait = min(interval * 2 ** failures, BACKOFF_MAX_S)
            log.info("sync drain failed (%s); retry in %.0f s", describe(exc), wait)
        stop.wait(wait)


def start(interval: float) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    t = threading.Thread(target=run, args=(stop, interval), name="sync-worker", daemon=True)
    t.start()
    return t, stop
