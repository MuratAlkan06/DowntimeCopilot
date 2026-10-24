"""S3 mirror (thin boto3 sketch): audit trail JSONL with SSE-S3. docs/decisions.md: SSE-S3 put OK."""
from __future__ import annotations

from app import config
from app.store import utcnow
from contracts import AuditEvent


class S3Mirror:
    name = "s3"

    def __init__(self, bucket: str) -> None:
        self.bucket = bucket

    def push_audit(self, events: list[AuditEvent]) -> int:
        if config.dry_run() or not events:
            return len(events)
        import boto3
        body = "\n".join(e.model_dump_json() for e in events).encode()
        # TODO(role 2/5, P1): snapshots, images, and audio mirrors; SSE-KMS if permitted.
        boto3.client("s3", region_name=config.aws_region()).put_object(
            Bucket=self.bucket, Key=f"audit/{utcnow():%Y%m%dT%H%M%S}.jsonl", Body=body, ServerSideEncryption="AES256",
        )
        return len(events)
