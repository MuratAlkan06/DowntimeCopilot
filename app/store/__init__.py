"""Process-wide store handle. `reset_store()` lets tests point at a fresh file."""
from __future__ import annotations

import contextlib
from contextvars import ContextVar

from app import clock, config
from app.store.sqlite import Store, utcnow  # noqa: F401

_store: Store | None = None
_override: ContextVar[Store | None] = ContextVar("store_override", default=None)


def get_store() -> Store:
    global _store
    if (scoped := _override.get()) is not None:
        return scoped
    if _store is None or _store.path != config.store_path():
        _store = Store(config.store_path(), now=clock.unit_now)
    return _store


def reset_store() -> None:
    global _store
    _store = None


@contextlib.contextmanager
def use_store(store: Store):
    """Point get_store() at `store` for the current context (thread) only: the readiness drill's ephemeral
    store (app/drill.py). Every other thread, the sync worker included, keeps the live store."""
    token = _override.set(store)
    try:
        yield store
    finally:
        _override.reset(token)
