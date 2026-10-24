"""DynamoDB action-log mirror (thin boto3 sketch). docs/decisions.md: create table + put item OK."""
from __future__ import annotations

from app import config
from contracts import AuditEvent


class DynamoDBMirror:
    name = "dynamodb"

    def __init__(self, table: str) -> None:
        self.table = table

    def push_audit(self, events: list[AuditEvent]) -> int:
        if config.dry_run():
            return len(events)
        import boto3
        tbl = boto3.resource("dynamodb", region_name=config.aws_region()).Table(self.table)
        # TODO(role 5, P1): table creation (pk event_id), retries, and sync-status reporting to the UI.
        with tbl.batch_writer() as batch:
            for e in events:
                batch.put_item(Item={"event_id": e.event_id, "ts": e.ts.isoformat(), "body": e.model_dump_json()})
        return len(events)
