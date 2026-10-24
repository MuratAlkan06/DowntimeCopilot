"""S3 mirror (thin boto3 sketch): audit trail JSONL with SSE-S3. docs/decisions.md: SSE-S3 put OK.

Also the audio bucket lifecycle for verbal-order read-backs (S9, app/capture/transcribe.py): create if
missing, harden (SSE-S3 default encryption, Block Public Access x4, expire objects after 1 day), and
teardown (empty + delete). Call shapes checked against the installed botocore S3 model.
"""
from __future__ import annotations

from app import config
from app.store import utcnow
from contracts import AuditEvent

# Error codes that mean "this principal may not do that" (S3, Transcribe, STS-style). HeadBucket has no
# body, so a denied HEAD surfaces only as its HTTP status ("403" / "Forbidden").
DENIED_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "403", "Forbidden"}
EXPIRE_DAYS = 1


def error_code(exc: Exception) -> str:
    return str(((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code", ""))


def is_denied(exc: Exception) -> bool:
    return error_code(exc) in DENIED_CODES


def harden_bucket(s3, bucket: str) -> None:
    """Idempotent: SSE-S3 default encryption, all four Block Public Access switches, 1-day expiry."""
    s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [{
        "ID": f"expire-after-{EXPIRE_DAYS}-day", "Filter": {"Prefix": ""}, "Status": "Enabled",
        "Expiration": {"Days": EXPIRE_DAYS}, "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": EXPIRE_DAYS}}]})


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
