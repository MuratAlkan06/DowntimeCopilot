"""DynamoDB sync table (docs/decisions.md S10). Local operator tool; needs AWS credentials.

    python tools/sync_table.py setup      # name it once, persist SYNC_TABLE in .env, create if missing (ACTIVE)
    python tools/sync_table.py teardown   # delete the table (then drop SYNC_TABLE from .env)

The name is `downtime-copilot-sync-<random suffix>`: generated once, appended to the local, git-ignored .env as
`export SYNC_TABLE=...`, and reused after that (same pattern as tools/audio_bucket.py). On-demand billing, keys
pk (entity type) + sk ("<iso-ts>#<id>"). The app also creates it if missing, from the worker thread. Prints the
table name and what it did, never credentials. CI never runs this.
"""
from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.sync import base, dynamodb  # noqa: E402

ENV_FILE = ROOT / ".env"
PREFIX = "downtime-copilot-sync-"
_LINE = re.compile(r"^\s*(?:export\s+)?SYNC_TABLE=([A-Za-z0-9_.-]+)\s*$", re.M)


def table_name(env_file: Path = ENV_FILE, create: bool = True) -> str | None:
    """SYNC_TABLE from the environment, else the .env line, else (create=True) a new name appended to .env."""
    if name := base.sync_table():
        return name
    text = env_file.read_text() if env_file.exists() else ""
    if m := _LINE.search(text):
        return m.group(1)
    if not create:
        return None
    name = PREFIX + secrets.token_hex(4)
    with env_file.open("a") as f:
        f.write(("" if not text or text.endswith("\n") else "\n")
                + "\n# DynamoDB sync mirror table (tools/sync_table.py, docs/decisions.md S10)\n"
                + f"export SYNC_TABLE={name}\n")
    return name


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("setup", "teardown"))
    args = ap.parse_args(argv)
    ddb = dynamodb.client()
    if args.action == "setup":
        name = table_name()
        try:
            made = dynamodb.ensure_table(ddb, name)
        except dynamodb.Denied as exc:
            print(f"{name}: DENIED ({exc.reason}); the app uses the local fallback")
            return 1
        print(f"{name}: {made}, ACTIVE, on-demand (pk/sk)")
        return 0
    name = table_name(create=False)
    if not name:
        print("no SYNC_TABLE in the environment or .env; nothing to tear down")
        return 1
    dynamodb.delete_table(ddb, name)
    print(f"{name}: deleted; remove the SYNC_TABLE line from .env")
    return 0


if __name__ == "__main__":
    sys.exit(main())
