"""Terraform outputs (infra/) -> the local .env. Local operator tool: `make infra-env`. CI never runs this.

    python tools/env_sync.py                 # read `terraform output -json` in infra/, upsert .env

Runs `terraform output -json` in infra/ as AWS_PROFILE=downtime-admin (the state lives in the S3 backend), then
upserts these export lines in .env and keeps every other line as it is:

    AWS_PROFILE=downtime-runtime, AWS_DEFAULT_REGION, GUARDRAIL_ID, GUARDRAIL_VERSION, AUDIO_BUCKET, SYNC_TABLE

and removes the AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN lines: static keys in the
environment win over AWS_PROFILE, so the app would never assume the runtime role. Prints variable names and
what it did with each, never their values.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
INFRA_DIR = ROOT / "infra"

ADMIN_PROFILE, RUNTIME_PROFILE = "downtime-admin", "downtime-runtime"
# .env variable -> infra/outputs.tf output name.
OUTPUTS = {
    "AWS_DEFAULT_REGION": "region",
    "GUARDRAIL_ID": "guardrail_id",
    "GUARDRAIL_VERSION": "guardrail_version",
    "AUDIO_BUCKET": "audio_bucket",
    "SYNC_TABLE": "sync_table",
}
STATIC_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
HEADER = "# AWS resources from Terraform (make infra-env, infra/outputs.tf). Re-run after every apply."
_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=")


class SyncError(Exception):
    pass


def terraform_outputs(infra_dir: Path, profile: str, terraform: str = "terraform", run=subprocess.run) -> dict:
    """`terraform output -json` in infra_dir as `profile`, flattened to {name: value}."""
    env = {k: v for k, v in os.environ.items() if k not in STATIC_KEYS} | {"AWS_PROFILE": profile}
    try:
        proc = run([terraform, "output", "-json"], cwd=infra_dir, env=env, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise SyncError(f"{terraform} not found on PATH") from exc
    if proc.returncode != 0:
        raise SyncError(f"terraform output failed (exit {proc.returncode}):\n{proc.stderr.strip()}")
    try:
        raw = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise SyncError("terraform output -json did not return JSON") from exc
    return {name: (out or {}).get("value") for name, out in raw.items()}


def wanted(outputs: dict, runtime_profile: str) -> dict[str, str]:
    """The .env values to write, in write order. Raises SyncError naming any missing or empty output."""
    missing = [out for out in OUTPUTS.values() if outputs.get(out) in (None, "")]
    if missing:
        raise SyncError("missing terraform outputs: " + ", ".join(missing)
                        + " (apply infra/ first, or check infra/outputs.tf)")
    return {"AWS_PROFILE": runtime_profile} | {var: str(outputs[out]) for var, out in OUTPUTS.items()}


def upsert(text: str, values: dict[str, str], remove=STATIC_KEYS) -> tuple[str, list[tuple[str, str]]]:
    """New .env text and [(name, action)], action in added / updated / unchanged / removed. The first assignment
    of each name in `values` is rewritten in place as `export NAME=value`; later duplicates and every assignment
    of a name in `remove` are dropped. Names still missing are appended under HEADER. Comments, blank lines and
    every other assignment are kept byte for byte. Idempotent: upsert(upsert(t)) changes nothing."""
    lines = text.splitlines()
    out, changes, seen = [], [], set()
    for line in lines:
        m = _ASSIGN.match(line)
        name = m.group(1) if m else None
        if name in remove:
            changes.append((name, "removed"))
            continue
        if name in values:
            if name in seen:
                changes.append((name, "removed duplicate"))
                continue
            seen.add(name)
            new = f"export {name}={shlex.quote(values[name])}"
            changes.append((name, "unchanged" if line == new else "updated"))
            out.append(new)
            continue
        out.append(line)
    added = [name for name in values if name not in seen]
    if added:
        if HEADER not in out:
            out += ([""] if out and out[-1].strip() else []) + [HEADER]
        out += [f"export {name}={shlex.quote(values[name])}" for name in added]
        changes += [(name, "added") for name in added]
    return "\n".join(out) + "\n", changes


def main(argv: list[str] | None = None, run=subprocess.run) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", type=Path, default=ENV_FILE)
    ap.add_argument("--infra-dir", type=Path, default=INFRA_DIR)
    ap.add_argument("--admin-profile", default=ADMIN_PROFILE, help="profile that reads the Terraform state")
    ap.add_argument("--runtime-profile", default=RUNTIME_PROFILE, help="AWS_PROFILE written to .env")
    ap.add_argument("--terraform", default=os.environ.get("TF", "terraform"))
    args = ap.parse_args(argv)
    try:
        values = wanted(terraform_outputs(args.infra_dir, args.admin_profile, args.terraform, run),
                        args.runtime_profile)
    except SyncError as exc:
        print(f"env_sync: {exc}", file=sys.stderr)
        return 1
    existed = args.env_file.exists()
    text, changes = upsert(args.env_file.read_text() if existed else "", values)
    args.env_file.write_text(text)
    if not existed:
        args.env_file.chmod(0o600)
    for name, action in changes:
        print(f"{args.env_file.name}: {name} {action}")
    if any(action == "removed" for _, action in changes):
        print("Shells that sourced the old .env still hold static keys: "
              "unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN")
    print(f"Next: `source .env`. Profile {args.runtime_profile} needs role_arn = "
          "`terraform -chdir=infra output -raw runtime_role_arn` in ~/.aws/config.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
