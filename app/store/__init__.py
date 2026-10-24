"""Process-wide store handle. `reset_store()` lets tests point at a fresh file."""
from __future__ import annotations

from app import config
from app.store.sqlite import Store, utcnow  # noqa: F401

_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None or _store.path != config.store_path():
        _store = Store(config.store_path())
    return _store


def reset_store() -> None:
    global _store
    _store = None
