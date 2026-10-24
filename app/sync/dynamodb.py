"""DynamoDB mirror of the audit log and captured records (docs/decisions.md S10).

Table `downtime-copilot-sync-<suffix>` (SYNC_TABLE), on-demand billing, keys pk (HASH, the entity type
"AuditEvent" | "CapturedRecord") and sk (RANGE, "<iso-ts>#<id>"). An item is {pk, sk, id, body} and depends on
the outbox row only, so a repeated put of the same key leaves the same item (idempotent). Create-if-missing:
DescribeTable, else CreateTable and the table_exists waiter (ACTIVE). AccessDenied on any call raises Denied;
the worker then latches LocalFallback. Short client timeouts (connect 2 s, read 5 s, one retry) keep a dead
network from holding the worker; the request path never calls this module. SYNC_ENDPOINT_URL overrides the
endpoint (the live kill/restore check). Call shapes checked against the installed botocore model (1.43.x).
"""
from __future__ import annotations

import os

from app import config
from app.sync.s3 import error_code, is_denied

CONNECT_TIMEOUT_S, READ_TIMEOUT_S, TOTAL_ATTEMPTS = 2, 5, 2  # TOTAL_ATTEMPTS = the call + 1 retry
WAITER = {"Delay": 2, "MaxAttempts": 30}  # up to 60 s for a new table to reach ACTIVE


class Denied(Exception):
    """AccessDenied from DynamoDB; `reason` is "<Operation> <ErrorCode>" (no message text)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def client():
    import boto3
    from botocore.config import Config

    cfg = Config(connect_timeout=CONNECT_TIMEOUT_S, read_timeout=READ_TIMEOUT_S,
                 retries={"total_max_attempts": TOTAL_ATTEMPTS, "mode": "standard"})
    return boto3.client("dynamodb", region_name=config.aws_region(), config=cfg,
                        endpoint_url=os.environ.get("SYNC_ENDPOINT_URL", "").strip() or None)


def _call(op: str, fn, **kw):
    from botocore.exceptions import ClientError

    try:
        return fn(**kw)
    except ClientError as exc:
        if is_denied(exc):
            raise Denied(f"{op} {error_code(exc)}") from exc
        raise


def ensure_table(ddb, table: str) -> str:
    """'exists' or 'created'. Waits for ACTIVE either way. Raises Denied, or the AWS error as is."""
    from botocore.exceptions import ClientError

    try:
        status = _call("DescribeTable", ddb.describe_table, TableName=table)["Table"].get("TableStatus")
        made = "exists"
    except ClientError as exc:
        if error_code(exc) != "ResourceNotFoundException":
            raise
        _call("CreateTable", ddb.create_table, TableName=table, BillingMode="PAY_PER_REQUEST",
              AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                    {"AttributeName": "sk", "AttributeType": "S"}],
              KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}])
        status, made = "CREATING", "created"
    if status != "ACTIVE":
        ddb.get_waiter("table_exists").wait(TableName=table, WaiterConfig=WAITER)
    return made


def item(pk: str, sk: str, body: str) -> dict:
    return {"pk": {"S": pk}, "sk": {"S": sk}, "id": {"S": sk.rsplit("#", 1)[-1]}, "body": {"S": body}}


def delete_table(ddb, table: str) -> None:
    ddb.delete_table(TableName=table)
    ddb.get_waiter("table_not_exists").wait(TableName=table, WaiterConfig=WAITER)


class DynamoDBMirror:
    name = "dynamodb"

    def __init__(self, table: str, ddb=None) -> None:
        self.table, self._ddb, self.ready = table, ddb, False

    def put(self, pk: str, sk: str, body: str) -> None:
        if self._ddb is None:
            self._ddb = client()
        if not self.ready:
            ensure_table(self._ddb, self.table)
            self.ready = True
        _call("PutItem", self._ddb.put_item, TableName=self.table, Item=item(pk, sk, body))
