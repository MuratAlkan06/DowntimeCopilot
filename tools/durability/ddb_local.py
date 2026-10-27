"""DynamoDB Local launch helper (docs/durability.md). Stdlib only.

The jar is NOT vendored. Get it once (AWS-hosted, checksum published next to it):

  curl -fsSLO https://d1ni2b6xgvw0s0.cloudfront.net/v2.x/dynamodb_local_latest.tar.gz
  curl -fsSLO https://d1ni2b6xgvw0s0.cloudfront.net/v2.x/dynamodb_local_latest.tar.gz.sha256
  shasum -a 256 -c dynamodb_local_latest.tar.gz.sha256 && tar -xzf dynamodb_local_latest.tar.gz -C <dir>

and point DDB_LOCAL_DIR at <dir> (holds DynamoDBLocal.jar and DynamoDBLocal_lib/). DDB_LOCAL_ENDPOINT
skips the launch and uses an already running instance (the S3 CI service container). Without java or the
jar, `unavailable()` says why and the harness skips (same idea as the node skipif in tests/test_web_queue.py).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from tools.durability.proxy import free_port


def jar_dir() -> Path | None:
    raw = os.environ.get("DDB_LOCAL_DIR", "").strip()
    return Path(raw) if raw else None


def unavailable() -> str | None:
    """None when a DynamoDB Local can be used, else the skip reason."""
    if os.environ.get("DDB_LOCAL_ENDPOINT", "").strip():
        return None
    if shutil.which("java") is None:
        return "java not installed: DynamoDB Local needs a JRE; durability server cycles skipped"
    d = jar_dir()
    if d is None or not (d / "DynamoDBLocal.jar").is_file():
        return "DynamoDBLocal.jar not found: set DDB_LOCAL_DIR (see tools/durability/ddb_local.py); skipped"
    return None


def _wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return  # any HTTP answer (DynamoDB Local says 400 to a bare GET) means it is up
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"DynamoDB Local did not answer at {url}")


class DynamoDBLocal:
    """In-memory, shared-db instance on a free high port. Use as a context manager."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.endpoint = os.environ.get("DDB_LOCAL_ENDPOINT", "").strip()

    def __enter__(self) -> "DynamoDBLocal":
        if self.endpoint:
            self.port = int(self.endpoint.rsplit(":", 1)[-1].strip("/"))
            return self
        d = jar_dir()
        self.port = free_port()
        self.proc = subprocess.Popen(
            ["java", f"-Djava.library.path={d / 'DynamoDBLocal_lib'}", "-jar", str(d / "DynamoDBLocal.jar"),
             "-inMemory", "-sharedDb", "-port", str(self.port)],
            cwd=d, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.endpoint = f"http://127.0.0.1:{self.port}"
        _wait_http(self.endpoint)
        return self

    def __exit__(self, *exc) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
