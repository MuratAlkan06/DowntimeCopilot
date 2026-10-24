"""Mirror interface and selection (docs/decisions.md S10). SQLite stays the source of truth; the outbox in the
store (app.store.sqlite) feeds whichever mirror is active, one item at a time:

  DRY_RUN                       -> DryRunMirror  ("dry_run": acknowledges, zero network)
  SYNC_TABLE set, not denied    -> DynamoDBMirror ("dynamodb")
  otherwise / DynamoDB DENIED   -> LocalFallback ("local": JSONL export under logs/ next to the SQLite file)

A DynamoDB AccessDenied (CreateTable, DescribeTable or PutItem) latches LocalFallback for the rest of the
process; `deny()` records why and `denied()` reports it (GET /sync/status shows it as last_error).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from app import config


class Mirror(Protocol):
    name: str

    def put(self, pk: str, sk: str, body: str) -> None: ...


class DryRunMirror:
    name = "dry_run"

    def put(self, pk: str, sk: str, body: str) -> None:
        return None


class LocalFallback:
    name = "local"

    def __init__(self, export_dir: str | None = None) -> None:
        self.export_dir = Path(export_dir or export_root())

    def put(self, pk: str, sk: str, body: str) -> None:
        path = self.export_dir / "sync-export.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps({"pk": pk, "sk": sk, "body": body}) + "\n")


def export_root() -> Path:
    """Local export directory: SYNC_EXPORT_DIR, else logs/ next to the SQLite file (git-ignored)."""
    return Path(os.environ.get("SYNC_EXPORT_DIR") or Path(config.store_path()).parent / "logs")


def sync_table() -> str | None:
    """DynamoDB table `downtime-copilot-sync-<suffix>`; tools/sync_table.py persists it in .env as SYNC_TABLE."""
    return os.environ.get("SYNC_TABLE", "").strip() or None


_denied: str | None = None  # "<Operation> <ErrorCode>" of the AccessDenied that latched LocalFallback
_mirrors: dict[str, Mirror] = {}  # one DynamoDBMirror per table per process (keeps its client and table check)


def deny(reason: str) -> None:
    global _denied
    _denied = reason


def denied() -> str | None:
    return _denied


def reset() -> None:
    """Forget the latch and cached mirrors (tests)."""
    global _denied
    _denied = None
    _mirrors.clear()


def mode() -> str:
    if config.dry_run():
        return DryRunMirror.name
    return "dynamodb" if sync_table() and not _denied else LocalFallback.name


def get_mirror() -> Mirror:
    m = mode()
    if m == "dynamodb":
        from app.sync.dynamodb import DynamoDBMirror
        table = sync_table()
        return _mirrors.setdefault(table, DynamoDBMirror(table))
    return DryRunMirror() if m == DryRunMirror.name else LocalFallback()
