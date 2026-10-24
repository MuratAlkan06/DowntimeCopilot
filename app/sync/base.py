"""Mirror interface + LocalFallback, and selection per docs/decisions.md:
DynamoDB ALLOWED -> DynamoDBMirror when DDB_TABLE is set; S3 ALLOWED -> S3Mirror when S3_BUCKET is set;
otherwise (or DRY_RUN) LocalFallback appends a JSONL export under logs/ next to the SQLite file."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from app import config
from contracts import AuditEvent


class Mirror(Protocol):
    name: str

    def push_audit(self, events: list[AuditEvent]) -> int: ...


class LocalFallback:
    name = "local"

    def __init__(self, export_dir: str | None = None) -> None:
        default = Path(config.store_path()).parent / "logs"  # logs/ is git-ignored
        self.export_dir = Path(export_dir or os.environ.get("SYNC_EXPORT_DIR", default))

    def push_audit(self, events: list[AuditEvent]) -> int:
        path = self.export_dir / "audit-export.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            for e in events:
                fh.write(e.model_dump_json() + "\n")
        return len(events)


def get_mirror() -> Mirror:
    if config.dry_run():
        return LocalFallback()
    if os.environ.get("DDB_TABLE"):
        from app.sync.dynamodb import DynamoDBMirror
        return DynamoDBMirror(os.environ["DDB_TABLE"])
    if os.environ.get("S3_BUCKET"):
        from app.sync.s3 import S3Mirror
        return S3Mirror(os.environ["S3_BUCKET"])
    return LocalFallback()
