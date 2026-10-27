"""Server-side durability cycles (docs/durability.md): real uvicorn subprocess, real SQLite outbox, real sync
worker, DynamoDB Local behind tools.durability.proxy, faults from a seeded schedule, then reconcile.

  python -m tools.durability.harness --run-dir DIR [--cycles N] [--seed S] [--faults all|none|F3,F4,...]
  python -m tools.durability.harness --run-dir DIR --replay FAULT:CYCLE_SEED   # one cycle, same schedule

Writes DIR/server/<n>.json per cycle and DIR/meta.json; tools.durability.report turns DIR into the report.
Needs the app's venv (boto3 is already a pinned app dependency; the harness adds none).
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import random
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from tools.durability import ddb_local
from tools.durability.proxy import Proxy, free_port

ROOT = Path(__file__).resolve().parents[2]
SHIM_DIR = Path(__file__).resolve().parent / "shim"
SERVER_FAULTS = ("none", "F3", "F4", "F4fd", "F5", "F6", "F6rb", "F7a", "F7b")
POLL_S = 0.25
DRAIN_TIMEOUT_S = 90.0
STARTUP_TIMEOUT_S = 30.0
EXPECTED_FAIL = {"F6rb": "S5: F6-rollback (SIMULATED transaction rollback under a 201; fix issue to be filed)"}
DUMMY_AWS = {"AWS_ACCESS_KEY_ID": "durabilityharness", "AWS_SECRET_ACCESS_KEY": "durabilityharness",
             "AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1"}
READBACK = "Verbal order read back: heparin 5000 units subcut q12h"

_live: set[subprocess.Popen] = set()


def _kill_all() -> None:
    for p in list(_live):
        _killpg(p, signal.SIGKILL)


atexit.register(_kill_all)


def _killpg(p: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(p.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


# ------------------------------------------------------------------ app process
class App:
    def __init__(self, env: dict[str, str], log_path: Path) -> None:
        self.env, self.log_path, self.port = env, log_path, 0
        self.proc: subprocess.Popen | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, **extra: str) -> "App":
        self.port = free_port()
        env = self.env | extra
        log = self.log_path.open("a")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.port),
             "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
        _live.add(self.proc)
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"app exited at startup (see {self.log_path})")
            if status(self) is not None:
                return self
            time.sleep(0.1)
        raise TimeoutError(f"app did not answer /sync/status (see {self.log_path})")

    def stop(self, sig: int = signal.SIGTERM) -> None:
        if self.proc is None:
            return
        _killpg(self.proc, sig)
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _killpg(self.proc, signal.SIGKILL)
            self.proc.wait(timeout=5)
        _live.discard(self.proc)
        self.proc = None


def status(app: App, timeout: float = 1.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"{app.base}/sync/status", timeout=timeout) as r:
            return json.loads(r.read()) if r.status == 200 else None
    except (OSError, ValueError):
        return None


def capture(app: App, client_id: str, timeout: float = 30.0) -> tuple[int | None, str | None]:
    """POST /capture, typed verbal order (multipart). (status, record_id); (None, None) when no response."""
    boundary = f"dur{random.getrandbits(64):x}"
    fields = {"form_type": "verbal_order", "patient_id": "pt-07", "readback_text": READBACK,
              "client_id": client_id}
    body = b"".join(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
                    for k, v in fields.items()) + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{app.base}/capture", data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}", "X-Role": "rn"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read()).get("record_id")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (OSError, ValueError):
        return None, None


# ------------------------------------------------------------------ DynamoDB side
def ddb_client(endpoint: str):
    import boto3
    return boto3.client("dynamodb", endpoint_url=endpoint, region_name="us-east-1",
                        aws_access_key_id=DUMMY_AWS["AWS_ACCESS_KEY_ID"],
                        aws_secret_access_key=DUMMY_AWS["AWS_SECRET_ACCESS_KEY"])


def create_table(ddb, table: str) -> None:
    ddb.create_table(TableName=table, BillingMode="PAY_PER_REQUEST",
                     AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                           {"AttributeName": "sk", "AttributeType": "S"}],
                     KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                                {"AttributeName": "sk", "KeyType": "RANGE"}])


def scan(ddb, table: str) -> list[dict]:
    out = []
    for page in ddb.get_paginator("scan").paginate(TableName=table, ConsistentRead=True):
        out += [{"pk": i["pk"]["S"], "sk": i["sk"]["S"], "body": i["body"]["S"]} for i in page["Items"]]
    return out


# ------------------------------------------------------------------ quiesce + metrics
def quiesce(app: App, healed_at: float) -> dict:
    """Poll ONLY GET /sync/status (POLL_S) until pending == 0 on 3 consecutive polls.
    t0 = first 200 after the heal/restart, t1 = first pending == 0 poll."""
    t0 = t1 = None
    zeros, polls, last = 0, 0, None
    deadline = time.monotonic() + DRAIN_TIMEOUT_S
    while time.monotonic() < deadline:
        st = status(app)
        polls += 1
        if st is not None:
            last = st
            now = time.monotonic()
            t0 = t0 if t0 is not None else now
            if st["pending"] == 0:
                t1 = t1 if t1 is not None else now
                zeros += 1
                if zeros >= 3:
                    return {"drained": True, "drain_s": round(t1 - t0, 3), "t0_after_heal_s": round(t0 - healed_at, 3),
                            "polls": polls, "status": st}
            else:
                zeros, t1 = 0, None
        time.sleep(POLL_S)
    return {"drained": False, "drain_s": None, "polls": polls, "status": last}


def wait_for(pred, timeout: float, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return False


# ------------------------------------------------------------------ reconcile (app stopped: DEAD sqlite)
def reconcile(store_path: Path, ddb_items: list[dict], jsonl_path: Path, ledger: list[dict]) -> dict:
    db = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    records = db.execute("SELECT record_id, client_id FROM captured").fetchall()
    audits = [json.loads(b) for (b,) in db.execute("SELECT body FROM audit ORDER BY seq")]
    kv = dict(db.execute("SELECT key, value FROM kv").fetchall())
    outbox_left = db.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0]
    db.close()

    jsonl: dict[tuple[str, str], dict] = {}
    jsonl_lines = 0
    if jsonl_path.is_file():
        for line in jsonl_path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                jsonl_lines += 1
                jsonl[(row["pk"], row["sk"])] = row  # deduped by (pk, sk): repeats are never violations
    mirrored_ids = Counter(i["sk"].rsplit("#", 1)[-1] for i in ddb_items)
    local_ids = {sk.rsplit("#", 1)[-1] for (_, sk) in jsonl}

    by_cid = Counter(c for _, c in records if c)
    rec_ids = {r for r, _ in records}
    create_audit = {a["target_id"] for a in audits if a["action"] == "capture.create"}
    missing_records = sorted(r for r in rec_ids if r not in mirrored_ids and r not in local_ids)
    missing_audits = sorted(a["event_id"] for a in audits
                            if a["event_id"] not in mirrored_ids and a["event_id"] not in local_ids)
    ddb_create_cids = Counter(json.loads(i["body"]).get("details", {}).get("client_id") for i in ddb_items
                              if i["pk"] == "AuditEvent" and json.loads(i["body"]).get("action") == "capture.create")
    ddb_create_cids.pop(None, None)

    acked = {e["client_id"] for e in ledger if e["status"] in (200, 201)}
    unacked = {e["client_id"] for e in ledger if e["status"] is None} - acked
    return {
        "sqlite_records": len(records), "sqlite_audit": len(audits), "outbox_left": outbox_left,
        "ddb_items": len(ddb_items), "jsonl_lines": jsonl_lines, "jsonl_unique": len(jsonl),
        "not_mirrored_kv": int(kv.get("sync.not_mirrored") or 0),
        "acked": len(acked),
        "lost_acked": sorted(c for c in acked if c not in by_cid),
        "unacked_not_in_sqlite": sorted(c for c in unacked if c not in by_cid),
        "unacked_in_sqlite": sorted(c for c in unacked if c in by_cid),
        "dup_sqlite_client_id": sorted(c for c, n in by_cid.items() if n > 1),
        "dup_ddb_item_id": sorted(i for i, n in mirrored_ids.items() if n > 1),
        "dup_ddb_client_id": sorted(c for c, n in ddb_create_cids.items() if n > 1),
        "missing_mirror_records": missing_records,
        "missing_mirror_audits": missing_audits,
        "record_without_create_audit": sorted(r for r in rec_ids if r not in create_audit),
    }


def classify(fault: str, rec: dict, drain: dict) -> dict:
    """Violations vs known windows vs expected failures (docs/durability.md "Expected outcomes")."""
    violations: dict[str, list] = {}
    known: dict[str, list] = {}
    expected: dict[str, list] = {}
    lost = list(rec["lost_acked"])
    if fault in EXPECTED_FAIL and lost:
        expected[EXPECTED_FAIL[fault]] = lost
        lost = []
    if lost:
        violations["lost_acked"] = lost
    if rec["unacked_not_in_sqlite"]:
        known["unacked_inflight"] = rec["unacked_not_in_sqlite"]
    missing = rec["missing_mirror_records"] + rec["missing_mirror_audits"]
    accounted = min(len(missing), rec["not_mirrored_kv"])
    if accounted:
        known["not_mirrored"] = missing[:accounted] if len(missing) == accounted else [f"{accounted} of {len(missing)}"]
    if len(missing) > accounted:
        violations["lost_mirror"] = missing
    for key in ("dup_sqlite_client_id", "dup_ddb_item_id", "dup_ddb_client_id"):
        if rec[key]:
            violations[key] = rec[key]
    if rec["record_without_create_audit"]:
        bucket = known if fault == "F5" else violations
        bucket["record_without_create_audit"] = rec["record_without_create_audit"]
    if not drain["drained"]:
        violations["drain_timeout"] = [drain.get("status")]
    return {"violations": violations, "known_windows": known, "expected_fail": expected}


# ------------------------------------------------------------------ one cycle
def run_cycle(n: int, fault: str, seed: int, run_id: str, run_dir: Path, ddb_endpoint: str, ddb_port: int,
              sync_interval: float) -> dict:
    rnd = random.Random(seed)
    cdir = run_dir / "server" / f"c{n:03d}"
    cdir.mkdir(parents=True, exist_ok=True)
    store_path = cdir / "store.sqlite"
    table = f"dur-{run_id}-{n:03d}"
    ddb = ddb_client(ddb_endpoint)
    create_table(ddb, table)
    proxy = Proxy(free_port(), ddb_port).start()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AWS_", "SYNC_", "DUR_", "GUARDRAIL_")) and k not in (
               "STORE_PATH", "DRY_RUN", "AUDIO_BUCKET", "TEXTRACT_ENABLED", "SIM_URL", "PYTHONPATH")}
    env |= DUMMY_AWS | {
        "DRY_RUN": "0", "TEXTRACT_ENABLED": "0", "SYNC_TABLE": table, "STORE_PATH": str(store_path),
        "SYNC_ENDPOINT_URL": f"http://127.0.0.1:{proxy.port}", "SNAPSHOT_INTERVAL_SECONDS": "0",
        "SYNC_INTERVAL_SECONDS": str(sync_interval), "DURABILITY_SHIM": "1",
        "SIM_URL": f"http://127.0.0.1:{free_port()}",  # nothing listens: no simulator, wall-clock fallback
        "PYTHONPATH": os.pathsep.join([str(SHIM_DIR), str(ROOT)]),
    }
    app = App(env, cdir / "app.log")
    ledger: list[dict] = []
    seq = [0]

    def submit(cid: str | None = None) -> dict:
        if cid is None:
            seq[0] += 1
            cid = f"dur-{run_id}-{n:03d}-{seq[0]}"
        code, rid = capture(app, cid)
        e = {"client_id": cid, "status": code, "record_id": rid}
        ledger.append(e)
        return e

    k = rnd.randint(3, 7)
    steps: list[str] = []
    try:
        if fault in ("F7a",):
            proxy.set_mode("refuse")
        if fault == "F7b":
            proxy.set_mode("deny")
        f6 = {}
        if fault in ("F6", "F6rb"):
            f6 = {"DUR_F6_NTH": str(rnd.randint(1, k)), "DUR_F6_MODE": "rollback" if fault == "F6rb" else "statement"}
            steps.append(f"f6 nth={f6['DUR_F6_NTH']} mode={f6['DUR_F6_MODE']}")
        app.start(**f6)

        if fault in ("none", "F6", "F6rb"):
            for _ in range(k):
                submit()
        elif fault == "F3":
            proxy.set_mode("refuse")
            for _ in range(k):
                submit()
            proxy.set_mode("pass")
            base = (status(app) or {}).get("mirrored", 0)
            hit = wait_for(lambda: (status(app) or {}).get("mirrored", 0) > base, 20)
            proxy.set_mode("drop")  # cut mid-drain: live connections closed, new ones dropped
            steps.append(f"cut after mirrored>{base}: {hit}")
            for _ in range(rnd.randint(1, 2)):
                submit()
            time.sleep(rnd.uniform(0.5, 1.5))
        elif fault in ("F4", "F4fd"):
            submit()  # mirror ready (DescribeTable done), so the frozen call is a PutItem
            wait_for(lambda: (status(app) or {}).get("pending") == 0, 20)
            mode, stat = ("hold", "held") if fault == "F4" else ("fwd_drop", "fwd_dropped")
            proxy.set_mode(mode)
            for _ in range(k):
                submit()
            hit = wait_for(lambda: proxy.stats[stat] >= 1, 20)
            app.stop(signal.SIGKILL)  # mid-batch
            steps.append(f"SIGKILL with proxy {mode} ({stat}={proxy.stats[stat]}, waited={hit})")
        elif fault == "F5":
            j = rnd.randint(1, k)
            for _ in range(j - 1):
                submit()
            app.stop()
            seq[0] += 1
            target = f"dur-{run_id}-{n:03d}-{seq[0]}"
            barrier = cdir / "f5.signal"
            app.start(DUR_F5_BARRIER=str(barrier), DUR_F5_CLIENT_ID=target)
            box: dict = {}
            th = threading.Thread(target=lambda: box.update(e=submit(target)), daemon=True)
            th.start()
            hit = wait_for(barrier.exists, 20)
            app.stop(signal.SIGKILL)  # after the record save committed, before capture.create
            th.join(timeout=10)
            steps.append(f"F5 barrier hit={hit} target={target}")
            app.start()
            e = submit(target)  # the client retries its un-acked submission: the 200 replay case
            steps.append(f"F5 replay status={e['status']}")
            for _ in range(k - j):
                submit()
        elif fault == "F7a":
            for _ in range(k):
                submit()
            app.stop()
            app.start()  # still refused across the restart
            submit()
            time.sleep(rnd.uniform(0.5, 2.0))
        elif fault == "F7b":
            for _ in range(k):
                submit()
            latched = wait_for(lambda: (st := status(app)) is not None and st["mode"] == "local"
                               and st["pending"] == 0, 30)
            steps.append(f"denied latch drained locally: {latched}")
            app.stop()
            proxy.set_mode("pass")
            app.start()  # a new process: the latch is gone, DynamoDB again
            for _ in range(rnd.randint(1, 3)):
                submit()

        # heal: faults stop, the app runs, drain measured from here
        proxy.set_mode("pass")
        if app.proc is None:
            app.start()
        healed = time.monotonic()
        drain = quiesce(app, healed)
        app.stop()
        items = scan(ddb, table)
        rec = reconcile(store_path, items, store_path.parent / "logs" / "sync-export.jsonl", ledger)
    finally:
        app.stop(signal.SIGKILL)
        proxy.close()
    result = {"cycle": n, "fault": fault, "seed": seed, "captures": len(ledger), "steps": steps,
              "proxy": proxy.stats, "drain": drain, "reconcile": rec} | classify(fault, rec, drain)
    (run_dir / "server" / f"c{n:03d}.json").write_text(json.dumps(result, indent=2, default=str))
    return result


def schedule(master: int, cycles: int, faults: list[str]) -> list[tuple[str, int]]:
    """Stratified: one cycle per fault type first (seeded order), then a seeded random mix."""
    rnd = random.Random(master)
    first = faults[:]
    rnd.shuffle(first)
    plan = first[:cycles] + [rnd.choice(faults) for _ in range(max(0, cycles - len(first)))]
    return [(f, rnd.randrange(2 ** 32)) for f in plan]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--cycles", type=int, default=25)
    ap.add_argument("--seed", default="auto")
    ap.add_argument("--faults", default="all", help="all | none | comma list of " + ",".join(SERVER_FAULTS))
    ap.add_argument("--replay", help="FAULT:CYCLE_SEED, one cycle")
    ap.add_argument("--sync-interval", type=float, default=0.25)
    args = ap.parse_args(argv)

    if (why := ddb_local.unavailable()) is not None:
        print(f"durability: SKIP server cycles: {why}")
        return 0
    master = random.SystemRandom().randrange(2 ** 32) if args.seed == "auto" else int(args.seed)
    faults = list(SERVER_FAULTS) if args.faults == "all" else args.faults.split(",")
    plan = ([(args.replay.split(":")[0], int(args.replay.split(":")[1]))] if args.replay
            else schedule(master, args.cycles, faults))
    run_id = re.sub(r"[^a-z0-9-]", "-", args.run_dir.name.lower())[-24:] or "run"
    args.run_dir.mkdir(parents=True, exist_ok=True)
    meta_path = args.run_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    meta |= {"run_id": run_id, "master_seed": master, "server_cycles": len(plan), "server_faults": faults,
             "sync_interval_s": args.sync_interval, "replay": args.replay}
    meta_path.write_text(json.dumps(meta, indent=2))
    print(f"durability: master seed {master}, {len(plan)} server cycles, run dir {args.run_dir}")
    with ddb_local.DynamoDBLocal() as ddb:
        for n, (fault, seed) in enumerate(plan, 1):
            r = run_cycle(n, fault, seed, run_id, args.run_dir, ddb.endpoint, ddb.port, args.sync_interval)
            print(f"  cycle {n:3d} {fault:5s} seed {seed:10d}: captures={r['captures']} "
                  f"violations={sum(map(len, r['violations'].values()))} known={sorted(r['known_windows'])} "
                  f"expected_fail={len(r['expected_fail'])} drain_s={r['drain'].get('drain_s')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
