"""Audio bucket for verbal-order Transcribe (docs/decisions.md S9). Local operator tool; needs AWS credentials.

    python tools/audio_bucket.py setup      # name it once, persist AUDIO_BUCKET in .env, create if missing, harden
    python tools/audio_bucket.py teardown   # empty + delete the bucket (then drop AUDIO_BUCKET from .env)

The name is `downtime-copilot-audio-<random suffix>`: generated once, appended to the local, git-ignored .env
as `export AUDIO_BUCKET=...`, and reused after that (from the environment or the .env line). Hardening is
idempotent: SSE-S3 default encryption, Block Public Access (all four), lifecycle rules merged into the existing
ones (audio/ expires after 1 day; snapshots/ and exports/ after 7, docs/decisions.md S10). Prints the bucket
name and what it did, never credentials. CI never runs this.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.sync import s3 as s3sync  # noqa: E402

ENV_FILE = ROOT / ".env"
PREFIX = "downtime-copilot-audio-"
_LINE = re.compile(r"^\s*(?:export\s+)?AUDIO_BUCKET=([a-z0-9.-]+)\s*$", re.M)


def bucket_name(env_file: Path = ENV_FILE, create: bool = True) -> str | None:
    """AUDIO_BUCKET from the environment, else the .env line, else (create=True) a new name appended to .env."""
    if name := config.audio_bucket():
        return name
    text = env_file.read_text() if env_file.exists() else ""
    if m := _LINE.search(text):
        return m.group(1)
    if not create:
        return None
    name = PREFIX + secrets.token_hex(4)
    with env_file.open("a") as f:
        f.write(("" if not text or text.endswith("\n") else "\n")
                + f"\n# Verbal-order audio bucket for Transcribe (tools/audio_bucket.py, docs/decisions.md S9)\n"
                + f"export AUDIO_BUCKET={name}\n")
    return name


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("setup", "teardown"))
    args = ap.parse_args(argv)
    import boto3

    region = config.aws_region()
    s3 = boto3.client("s3", region_name=region)
    if args.action == "setup":
        name = bucket_name()
        created = s3sync.ensure_bucket(s3, name, region)
        s3sync.harden_bucket(s3, name)  # again for an existing bucket: idempotent
        rules = ", ".join(f"{r['Filter'].get('Prefix', '') or '*'} {r['Expiration']['Days']}d"
                          for r in s3sync.ensure_lifecycle(s3, name) if "Filter" in r and "Expiration" in r)
        print(f"{name}: {'created' if created else 'exists'}, hardened (SSE-S3, Block Public Access x4, "
              f"lifecycle: {rules}) in {region}")
        return 0
    name = bucket_name(create=False)
    if not name:
        print("no AUDIO_BUCKET in the environment or .env; nothing to tear down")
        return 1
    n = s3sync.empty_and_delete(s3, name)
    print(f"{name}: emptied ({n} object versions) and deleted; remove the AUDIO_BUCKET line from .env")
    return 0


if __name__ == "__main__":
    sys.exit(main())
