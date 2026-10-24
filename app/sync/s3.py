"""The one app bucket (AUDIO_BUCKET, docs/decisions.md S9 + S10), one prefix per use:
  audio/      verbal-order read-backs for Transcribe (app/capture/transcribe.py), expire after 1 day
  snapshots/  the newest snapshot pull, mirrored by the sync worker, expire after 7 days
  exports/    manual audit exports (POST /sync/export, `make export-audit`), expire after 7 days

Bucket lifecycle: create if missing, harden (SSE-S3 default encryption, Block Public Access x4, lifecycle
rules MERGED into whatever the bucket already has; S9's global 1-day rule is scoped to audio/), and teardown
(empty + delete). Call shapes checked against the installed botocore S3 model (1.43.x).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from app import config

# Error codes that mean "this principal may not do that" (S3, Transcribe, STS-style). HeadBucket has no
# body, so a denied HEAD surfaces only as its HTTP status ("403" / "Forbidden").
DENIED_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "403", "Forbidden"}
EXPIRE_DAYS = 1         # audio/
MIRROR_EXPIRE_DAYS = 7  # snapshots/ and exports/
AUDIO_PREFIX, SNAPSHOT_PREFIX, EXPORT_PREFIX = "audio/", "snapshots/", "exports/"
AUDIO_RULE_ID = f"expire-after-{EXPIRE_DAYS}-day"  # S9's id, kept so the existing rule is scoped, not duplicated


def error_code(exc: Exception) -> str:
    return str(((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code", ""))


def is_denied(exc: Exception) -> bool:
    return error_code(exc) in DENIED_CODES


def _rule(rule_id: str, prefix: str, days: int) -> dict:
    return {"ID": rule_id, "Filter": {"Prefix": prefix}, "Status": "Enabled", "Expiration": {"Days": days},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": days}}


LIFECYCLE_RULES = (
    _rule(AUDIO_RULE_ID, AUDIO_PREFIX, EXPIRE_DAYS),
    _rule(f"expire-snapshots-after-{MIRROR_EXPIRE_DAYS}-days", SNAPSHOT_PREFIX, MIRROR_EXPIRE_DAYS),
    _rule(f"expire-exports-after-{MIRROR_EXPIRE_DAYS}-days", EXPORT_PREFIX, MIRROR_EXPIRE_DAYS),
)


def _is_global(rule: dict) -> bool:
    flt = rule.get("Filter")
    return (flt is None or flt == {} or flt == {"Prefix": ""}) and not rule.get("Prefix")


def merge_lifecycle(existing: list[dict]) -> list[dict]:
    """Existing rules are kept as they are, in order, with one exception: the audio rule (AUDIO_RULE_ID), if
    global, is scoped to audio/ (its expiry unchanged), so it no longer expires snapshots and exports. Each of
    LIFECYCLE_RULES whose ID is missing is appended. Idempotent: merge(merge(x)) == merge(x)."""
    out = []
    for rule in existing:
        if rule.get("ID") == AUDIO_RULE_ID and _is_global(rule):
            rule = {k: v for k, v in rule.items() if k != "Prefix"} | {"Filter": {"Prefix": AUDIO_PREFIX}}
        out.append(rule)
    have = {r.get("ID") for r in out}
    return out + [dict(r) for r in LIFECYCLE_RULES if r["ID"] not in have]


def ensure_lifecycle(s3, bucket: str) -> list[dict]:
    """Read the bucket's lifecycle, merge ours in, write it back only if it changed. Returns the rules."""
    from botocore.exceptions import ClientError

    try:
        existing = s3.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules") or []
    except ClientError as exc:
        if error_code(exc) != "NoSuchLifecycleConfiguration":
            raise
        existing = []
    rules = merge_lifecycle(existing)
    if rules != existing:
        s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": rules})
    return rules


def harden_bucket(s3, bucket: str) -> None:
    """Idempotent: SSE-S3 default encryption, all four Block Public Access switches, merged lifecycle."""
    s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    ensure_lifecycle(s3, bucket)


def ensure_bucket(s3, bucket: str, region: str) -> bool:
    """Create `bucket` if it does not exist, then harden it. True when it was created by this call.
    us-east-1 takes no LocationConstraint (S3 rejects it); every other region needs one.
    BucketAlreadyOwnedByYou (a create race with ourselves) counts as success. Other errors raise."""
    from botocore.exceptions import ClientError

    try:
        s3.head_bucket(Bucket=bucket)
        return False
    except ClientError as exc:
        if error_code(exc) not in ("404", "NoSuchBucket", "NotFound"):
            raise
    extra = {} if region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": region}}
    try:
        s3.create_bucket(Bucket=bucket, **extra)
    except ClientError as exc:
        if error_code(exc) != "BucketAlreadyOwnedByYou":
            raise
    harden_bucket(s3, bucket)
    return True


def empty_and_delete(s3, bucket: str) -> int:
    """Teardown: delete every object version and delete marker, then the bucket. Returns objects deleted."""
    deleted = 0
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
        keys = [{"Key": o["Key"], "VersionId": o["VersionId"]}
                for o in (page.get("Versions") or []) + (page.get("DeleteMarkers") or [])]
        for i in range(0, len(keys), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys[i:i + 1000], "Quiet": True})
        deleted += len(keys)
    s3.delete_bucket(Bucket=bucket)
    return deleted


# ---------------------------------------------------------------- sync mirrors (S10)

_ready: set[str] = set()   # buckets whose lifecycle this process has merged
_snapshot_retry_at = 0.0   # monotonic; a failed snapshot put waits SNAPSHOT_RETRY_S before the next try
SNAPSHOT_RETRY_S = 30.0


def client():
    """S3 client for the sync worker and exports: same short timeouts as the DynamoDB mirror."""
    import boto3
    from botocore.config import Config

    from app.sync import dynamodb
    return boto3.client("s3", region_name=config.aws_region(), config=Config(
        connect_timeout=dynamodb.CONNECT_TIMEOUT_S, read_timeout=dynamodb.READ_TIMEOUT_S,
        retries={"total_max_attempts": dynamodb.TOTAL_ATTEMPTS, "mode": "standard"}))


def _prepare(s3, bucket: str) -> None:
    if bucket not in _ready:
        ensure_bucket(s3, bucket, config.aws_region())
        ensure_lifecycle(s3, bucket)  # an existing bucket (ensure_bucket skipped hardening) gets the merge too
        _ready.add(bucket)


def mirror_snapshot(store, s3=None, bucket: str | None = None) -> str | None:
    """Put the newest snapshot pull under snapshots/ once (kv sync.s3_snapshot holds the last key put).
    Returns the key put, or None (nothing new, DRY_RUN, no bucket). Raises AWS errors (the worker logs them)."""
    bucket = bucket or config.audio_bucket()
    pull = store.newest_pull()
    if config.dry_run() or not bucket or not pull:
        return None
    pull_id, kind, pulled_at, patients = pull
    key = f"{SNAPSHOT_PREFIX}{kind}-{datetime.fromisoformat(pulled_at):%Y%m%dT%H%M%SZ}-pull{pull_id}.json"
    if store.get_kv("sync.s3_snapshot") == key:
        return None
    s3 = s3 or client()
    _prepare(s3, bucket)
    body = json.dumps([p.model_dump(mode="json") for p in patients]).encode()
    s3.put_object(Bucket=bucket, Key=key, Body=body, ServerSideEncryption="AES256", ContentType="application/json")
    store.set_kv("sync.s3_snapshot", key)
    return key


def snapshot_step(store) -> None:
    """Worker hook: mirror_snapshot, never raising; a failure is recorded and retried after SNAPSHOT_RETRY_S."""
    global _snapshot_retry_at
    if time.monotonic() < _snapshot_retry_at:
        return
    try:
        mirror_snapshot(store)
    except Exception as exc:  # noqa: BLE001 - the worker must never die
        _snapshot_retry_at = time.monotonic() + SNAPSHOT_RETRY_S
        store.set_kv("sync.last_error", f"s3: {error_code(exc) or type(exc).__name__}")


def export_key(at: datetime) -> str:
    return f"{EXPORT_PREFIX}audit-{at.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"


def export_audit(store, at: datetime, s3=None, bucket: str | None = None) -> dict:
    """Audit events stamped at or before `at` (whole seconds, unit clock) as JSONL under exports/. Idempotent per
    timestamp key: the same `at` always names the same object with the same bytes (the `synced` flag, which
    changes later, is left out), and an existing object is not written again. DRY_RUN or no bucket: the same
    key as a file under the local export directory (app.sync.base.export_root)."""
    from botocore.exceptions import ClientError

    from app.sync.base import export_root

    at = at.astimezone(timezone.utc).replace(microsecond=0)
    key = export_key(at)
    events = [e for _, e in store.audit_log() if e.ts <= at]
    body = "".join(e.model_dump_json(exclude={"synced"}) + "\n" for e in events).encode()
    bucket = bucket or config.audio_bucket()
    out = {"target": "local", "key": key, "created": False, "events": len(events)}
    if config.dry_run() or not bucket:
        path = export_root() / key
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            out["created"] = True
        return out
    s3 = s3 or client()
    _prepare(s3, bucket)
    out["target"] = "s3"
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return out
    except ClientError as exc:
        if error_code(exc) not in ("404", "NoSuchKey", "NotFound"):
            raise
    s3.put_object(Bucket=bucket, Key=key, Body=body, ServerSideEncryption="AES256", ContentType="application/x-ndjson")
    out["created"] = True
    return out
