"""Durability-harness shim (docs/durability.md "Shim"). Injected from OUTSIDE the app: the harness prepends
this directory to PYTHONPATH of its uvicorn subprocess, so Python imports it at startup. It does NOTHING
unless DURABILITY_SHIM=1 (tests/test_durability_harness.py proves the no-op), and nothing under app/
knows it exists.

With the flag it patches, in that process only:
  - app.ai.client._converse -> a canned, deterministic Converse response (no Bedrock call). The text is the
    app's own dry-run structuring of the typed read-back (app.ai.forms.mock_verbal_order), so the record
    has the same shape as in DRY_RUN while the sync mirror runs live (DRY_RUN=0).
  - Store.save_record -> F5 barrier: when DUR_F5_BARRIER is set, after the record save COMMITS for the
    client_id in DUR_F5_CLIENT_ID, write that signal file and block; the harness SIGKILLs the process there,
    between the save and the capture.create audit.
  - Store._enqueue -> F6: the DUR_F6_NTH-th enqueue of a CapturedRecord fails.
      DUR_F6_MODE=statement: the outbox INSERT itself raises sqlite3.OperationalError inside the real
        _enqueue, so the app's own failure path runs (log + kv sync.not_mirrored).
      DUR_F6_MODE=rollback (SIMULATED): the shim rolls the open transaction back and swallows, standing in
        for SQLITE_FULL/IOERR/BUSY rolling back the record save under a 201. Labeled SIMULATED in the report.
"""
from __future__ import annotations

import os


def _chain_shadowed() -> None:
    """Being first on PYTHONPATH shadows any interpreter-level sitecustomize (Homebrew Python ships one);
    run it, so the shim is transparent with or without the flag."""
    import importlib.machinery
    import importlib.util
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.machinery.PathFinder.find_spec(
        "sitecustomize", [p for p in sys.path if os.path.abspath(p or ".") != here])
    if spec and spec.loader and spec.origin and os.path.abspath(spec.origin) != os.path.abspath(__file__):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


_chain_shadowed()

if os.environ.get("DURABILITY_SHIM") == "1":
    import json
    import sqlite3
    import threading
    import time
    from pathlib import Path

    import app.ai.client as _client
    from app.store.sqlite import SYNC_RECORD, Store

    def _canned_converse(model_id, system, user_text, image, image_format, max_tokens):  # noqa: ARG001
        from app.ai.forms import mock_verbal_order
        try:
            transcript = json.loads(user_text).get("transcript") or ""
        except (ValueError, AttributeError):
            transcript = ""
        text = mock_verbal_order(transcript).model_dump_json() if transcript else "{}"
        return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}},
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "stopReason": "end_turn"}

    _client._converse = _canned_converse

    _barrier = os.environ.get("DUR_F5_BARRIER", "").strip()
    _barrier_cid = os.environ.get("DUR_F5_CLIENT_ID", "").strip()
    _orig_save = Store.save_record

    def _save_record(self, record, client_id=None, text_source=None):
        _orig_save(self, record, client_id=client_id, text_source=text_source)
        if _barrier and client_id and client_id == _barrier_cid:
            Path(_barrier).write_text(record.record_id)  # committed; the harness kills us now
            while True:
                time.sleep(3600)

    Store.save_record = _save_record

    _f6_nth = int(os.environ.get("DUR_F6_NTH", "0") or 0)
    _f6_mode = os.environ.get("DUR_F6_MODE", "statement").strip()
    _f6_seen = [0]
    _f6_lock = threading.Lock()
    _orig_enqueue = Store._enqueue

    class _FailingOutbox:
        """Connection stand-in for one _enqueue call: the outbox INSERT raises, everything else delegates."""

        def __init__(self, db):
            self._db = db

        def execute(self, sql, *args):
            if sql.lstrip().upper().startswith("INSERT INTO SYNC_OUTBOX"):
                raise sqlite3.OperationalError("durability harness: injected outbox enqueue failure")
            return self._db.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._db, name)

    def _enqueue(self, pk, sk, body):
        hit = False
        if _f6_nth and pk == SYNC_RECORD:
            with _f6_lock:
                _f6_seen[0] += 1
                hit = _f6_seen[0] == _f6_nth
        if not hit:
            return _orig_enqueue(self, pk, sk, body)
        if _f6_mode == "rollback":
            self._db.rollback()  # SIMULATED: the whole save rolls back, the handler still answers 201
            return None
        real, self._db = self._db, _FailingOutbox(self._db)
        try:
            return _orig_enqueue(self, pk, sk, body)
        finally:
            self._db = real

    Store._enqueue = _enqueue
