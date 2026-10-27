"""Browser-queue durability cycles (docs/durability.md "Node side"): the REAL web/js/queue.js and api.js in
`node --input-type=module` subprocesses against a real uvicorn app (DRY_RUN=1: the server is the dedupe
oracle here, the outbox is the server harness's job). A file-backed store stands in for IndexedDB
(put/list/delete; real IDB semantics are out of scope); "tab death" is a fresh subprocess over the same file.

  python -m tools.durability.queue_cycles --run-dir DIR [--cycles N] [--seed S]

Writes DIR/node/c<n>.json per cycle; tools.durability.report reads them.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

from tools.durability.harness import ROOT, App, free_port, schedule

NODE = shutil.which("node")
NODE_TIMEOUT_S = 60  # per tab life (tests/test_web_queue.py:21 precedent uses 20 for one short script)
NODE_FAULTS = ("none", "F1", "F1die", "F2", "offline", "tabdeath", "twotab", "poison")
JS_FILES = ("queue.js", "api.js", "state.js")

DRIVER = r"""
import fs from 'node:fs';
const cfg = JSON.parse(process.env.DUR_CFG);
const mem = new Map();
globalThis.localStorage = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, String(v)), removeItem: (k) => mem.delete(k) };
Object.defineProperty(globalThis, 'navigator', { value: { onLine: true }, configurable: true, writable: true });
globalThis.window = { addEventListener() {} };
globalThis.location = { href: cfg.base + '/' };
const log = (ev) => fs.appendFileSync(cfg.ledger, JSON.stringify({ life: cfg.life, ...ev }) + '\n');

// Fake fetch over the real one: per-cycle fault schedule keyed by (client_id, attempt number in this life).
const realFetch = globalThis.fetch;
const attempts = new Map();
globalThis.fetch = async (path, init = {}) => {
  const cid = init.body instanceof FormData ? init.body.get('client_id') : null;
  let f = null;
  if (cid) {
    const n = (attempts.get(cid) || 0) + 1; attempts.set(cid, n);
    f = cfg.faults.find((x) => x.cid === cid && x.attempt === n) || null;
    log({ ev: 'attempt', cid, n, fault: f?.kind || null });
  }
  if (f?.kind === 'cut_pre') throw new TypeError('fetch failed (harness: cut before send)');
  if (f?.kind === 'die_pre') { log({ ev: 'died', cid }); process.exit(0); }
  if (f?.kind === 'offline_result') return new Response('{}', { status: 503, headers: { 'x-dc-offline': '1', 'content-type': 'application/json' } });
  const res = await realFetch(new URL(path, cfg.base + '/').href, init);
  if (f) await res.arrayBuffer();
  if (f?.kind === 'cut_post') throw new TypeError('fetch failed (harness: response dropped after commit)');
  if (f?.kind === 'die_post') { log({ ev: 'died', cid }); process.exit(0); }
  if (f?.kind === 'real_5xx') return new Response('{"detail":"harness 5xx after commit"}', { status: 500, headers: { 'content-type': 'application/json' } });
  return res;
};

// File-backed store: the queue.js store interface (put upsert by id, list oldest first, delete).
const load = () => { try { return JSON.parse(fs.readFileSync(cfg.store, 'utf8')); } catch { return {}; } };
const save = (m) => { fs.writeFileSync(cfg.store + '.tmp', JSON.stringify(m)); fs.renameSync(cfg.store + '.tmp', cfg.store); };
const fileStore = {
  put: async (item) => { const m = load(); m[item.id] = item; save(m); },
  list: async () => Object.values(load()).sort((a, b) => String(a.queued_at ?? '').localeCompare(String(b.queued_at ?? ''))),
  delete: async (id) => { const m = load(); delete m[id]; save(m); },
};

const A = await import(cfg.js + '/api.js');
const Q1 = await import(cfg.js + '/queue.js?tab=1');
const Q2 = await import(cfg.js + '/queue.js?tab=2'); // a second module instance: the second tab

// REPLICA of web/js/during.js buildForm + sendQueued (drift risk: update if those change).
function buildForm(meta) {
  const fd = new FormData();
  fd.append('form_type', meta.form_type); fd.append('captured_by_role', meta.role); fd.append('role', meta.role);
  if (meta.patient_id) fd.append('patient_id', meta.patient_id);
  if (meta.readback_text) fd.append('readback_text', meta.readback_text);
  if (meta.id) fd.append('client_id', meta.id);
  return fd;
}
const sendQueued = (item) => A.api('/capture', { method: 'POST', headers: { 'X-Role': item.role }, form: buildForm(item) });
const onSent = async (item, r) => log({ ev: 'ack', cid: item.id, status: r.status, via: 'drain' });
// REPLICA of the 3-line gate of web/js/during.js flushQueue (DRIFT RISK: if that gate changes, this lies).
async function flushQueue(Q) {
  if (A.isOffline()) { log({ ev: 'skipped' }); return { skipped: true }; }
  return Q.drain(fileStore, sendQueued, { onSent, locks: null }); // locks passed explicitly, always
}
// REPLICA of web/js/during.js submitCapture's branches (DRIFT RISK: if they change, this lies): ok / offline or
// RETRYABLE (5xx, 408, 429; keep in step with during.js) -> queue.put under the SAME id (#55) / else (other 4xx)
// an error, and the nurse taps submit again, which mints a NEW id. Before #55 a 5xx took that last branch:
// the F2 duplicate.
async function submit(paper, attempt = 0) {
  const meta = { id: attempt ? `${paper}-r${attempt}` : paper, form_type: 'verbal_order', patient_id: 'pt-07', role: 'rn', readback_text: cfg.readback };
  const r = await A.api('/capture', { method: 'POST', form: buildForm(meta) });
  if (r.ok && r.data?.record_id) { log({ ev: 'ack', cid: meta.id, paper, status: r.status, via: 'submit' }); return; }
  if (r.offline || r.status >= 500 || r.status === 408 || r.status === 429) { await fileStore.put({ ...meta, blob: null, queued_at: new Date().toISOString() }); log({ ev: 'queued', cid: meta.id, paper }); return; }
  log({ ev: 'error', cid: meta.id, paper, status: r.status });
  if (attempt < 1) await submit(paper, attempt + 1);
}

for (const op of cfg.ops) {
  if (op.op === 'submit') await submit(op.paper);
  else if (op.op === 'tick') await flushQueue(Q1);
  else if (op.op === 'poll') await A.api('/sync/status'); // the page's own GET: a non-cached 2xx clears the latch
  else if (op.op === 'poison') { await fileStore.put({ id: op.cid, form_type: 'vitals_flowsheet', role: 'rn', blob: null, poison: true, queued_at: '2000-01-01T00:00:00Z' }); log({ ev: 'queued', cid: op.cid, poison: true }); }
  else if (op.op === 'twotab') await Promise.all([flushQueue(Q1), flushQueue(Q2)]);
  else if (op.op === 'heal') { // enough drains for a poison item to reach Q.MAX_ATTEMPTS and dead-letter
    for (let i = 0; i < 40; i++) {
      await A.api('/sync/status');
      await flushQueue(Q1);
      if ((await Q1.counts(fileStore)).active === 0) break;
    }
  }
}
const rest = await fileStore.list();
log({ ev: 'left', ids: rest.filter((x) => !Q1.isDead(x)).map((x) => x.id), dead: rest.filter(Q1.isDead).map((x) => x.id) });
"""


def plan_cycle(fault: str, rnd: random.Random, prefix: str) -> tuple[list[dict], set[str]]:
    """Lives (each: ops + faults + twoTab) for one cycle, then a fault-free heal life. Returns (lives, f2 papers)."""
    k = rnd.randint(2, 5)
    papers = [f"{prefix}-p{i}" for i in range(1, k + 1)]
    sub = [{"op": "submit", "paper": p} for p in papers]
    f2: set[str] = set()
    lives: list[dict] = []
    if fault == "none":
        lives.append({"ops": sub + [{"op": "tick"}], "faults": []})
    elif fault == "F1":
        faults = [{"cid": p, "attempt": 1, "kind": rnd.choice(["cut_pre", "cut_post"])} for p in papers]
        faults += [{"cid": p, "attempt": 2, "kind": "cut_post"} for p in papers if rnd.random() < 0.5]
        lives.append({"ops": sub + [{"op": "tick"}, {"op": "tick"}, {"op": "poll"}, {"op": "tick"}], "faults": faults})
    elif fault == "F1die":
        victim = papers[-1]
        lives.append({"ops": sub, "faults": [{"cid": victim, "attempt": 1, "kind": rnd.choice(["die_pre", "die_post"])}]})
    elif fault == "F2":
        j = rnd.choice(papers)
        f2.add(j)
        faults = [{"cid": j, "attempt": 1, "kind": "real_5xx"}]
        q = rnd.choice(papers)  # and one queued item whose drain send gets a 5xx: same id retried, no dup
        if q != j:
            faults += [{"cid": q, "attempt": 1, "kind": "cut_pre"}, {"cid": q, "attempt": 2, "kind": "real_5xx"}]
        lives.append({"ops": sub + [{"op": "tick"}, {"op": "poll"}, {"op": "tick"}], "faults": faults})
    elif fault == "offline":
        faults = [{"cid": p, "attempt": 1, "kind": "offline_result"} for p in papers]
        faults += [{"cid": papers[0], "attempt": 2, "kind": "offline_result"}]
        lives.append({"ops": sub + [{"op": "tick"}, {"op": "tick"}, {"op": "poll"}, {"op": "tick"}], "faults": faults})
    elif fault == "tabdeath":
        faults = [{"cid": p, "attempt": 1, "kind": "cut_pre"} for p in papers]
        faults += [{"cid": rnd.choice(papers), "attempt": 2, "kind": rnd.choice(["die_pre", "die_post"])}]
        lives.append({"ops": sub + [{"op": "poll"}, {"op": "tick"}], "faults": faults})
    elif fault == "twotab":
        faults = [{"cid": p, "attempt": 1, "kind": "cut_pre"} for p in papers]
        lives.append({"ops": sub, "faults": faults})
        lives.append({"ops": [{"op": "poll"}, {"op": "twotab"}], "faults": []})
    elif fault == "poison":
        faults = [{"cid": p, "attempt": 1, "kind": "cut_pre"} for p in papers]
        lives.append({"ops": [{"op": "poison", "cid": f"{prefix}-poison"}] + sub
                      + [{"op": "tick"}, {"op": "poll"}, {"op": "tick"}], "faults": faults})
    lives.append({"ops": [{"op": "heal"}], "faults": []})
    return lives, f2


def run_life(js_dir: Path, base: str, store: Path, ledger: Path, life: int, spec: dict, readback: str) -> None:
    cfg = {"js": js_dir.as_uri(), "base": base, "store": str(store), "ledger": str(ledger), "life": life,
           "readback": readback, **spec}
    out = subprocess.run([NODE, "--input-type=module", "-e", DRIVER], capture_output=True, text=True,
                         timeout=NODE_TIMEOUT_S, env=os.environ | {"DUR_CFG": json.dumps(cfg)})
    if out.returncode != 0:
        raise RuntimeError(f"node life {life} failed: {out.stderr[-2000:]}")


def reconcile(fault: str, prefix: str, events: list[dict], rows: list[tuple[str, str]], f2: set[str]) -> dict:
    cids = Counter(c for _, c in rows if c and c.startswith(prefix + "-"))
    acked = {e["cid"] for e in events if e["ev"] == "ack"}
    queued = {e["cid"] for e in events if e["ev"] == "queued" and not e.get("poison")}
    poison = {e["cid"] for e in events if e["ev"] == "queued" and e.get("poison")}
    attempted = {e["cid"] for e in events if e["ev"] == "attempt"}
    last = next((e for e in reversed(events) if e["ev"] == "left"), {})
    left, dead = last.get("ids", []), last.get("dead", [])
    violations: dict[str, list] = {}
    known: dict[str, list] = {}
    if lost := sorted(c for c in acked if c not in cids):
        violations["lost_acked"] = lost
    if lost := sorted(c for c in queued if c not in cids):
        violations["lost_queued"] = lost
    if stuck := sorted(left):  # a poison item still live after the heal never reached the dead-letter cap
        violations["queue_not_drained"] = stuck
    if bad := sorted(c for c in dead if c not in poison):  # only a deterministic refusal may dead-letter
        violations["dead_letter_unexpected"] = bad
    if dup := sorted(c for c, n in cids.items() if n > 1):
        violations["dup_sqlite_client_id"] = dup
    per_paper = Counter(re.sub(r"-r\d+$", "", c) for c in cids)
    if dup := sorted(p for p, n in per_paper.items() if n > 1):  # F2 included since #55: no known window left
        violations["dup_paper"] = dup
    if not_once := sorted(p for p in f2 if per_paper[p] != 1 or cids[p] != 1):  # the same id, landed once
        violations["f2_not_exactly_once"] = not_once
    if un := sorted(c for c in attempted - acked - queued - poison if c not in cids):
        known["unacked_inflight"] = un
    skipped = sum(1 for e in events if e["ev"] == "skipped")
    return {"violations": violations, "known_windows": known, "expected_fail": {}, "stall": skipped,
            "dead_letter": sorted(c for c in dead if c in poison),
            "reconcile": {"acked": len(acked), "queued": len(queued), "sqlite_records": sum(cids.values()),
                          "left_in_queue": left}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--cycles", type=int, default=25)
    ap.add_argument("--seed", default="auto")
    ap.add_argument("--faults", default="all")
    args = ap.parse_args(argv)
    args.run_dir = args.run_dir.resolve()  # `make durability` passes a relative dir; js_dir.as_uri() needs absolute
    if NODE is None:
        print("durability: SKIP node cycles: node not installed")
        return 0
    meta_path = args.run_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    master = (int(args.seed) if args.seed != "auto" else meta.get("master_seed")
              or random.SystemRandom().randrange(2 ** 32))
    faults = list(NODE_FAULTS) if args.faults == "all" else args.faults.split(",")
    plan = schedule(master + 1, args.cycles, faults)
    run_id = meta.get("run_id") or "run"
    ndir = args.run_dir / "node"
    js_dir = ndir / "js"
    js_dir.mkdir(parents=True, exist_ok=True)
    for name in JS_FILES:
        shutil.copy(ROOT / "web" / "js" / name, js_dir / name)
    meta |= {"run_id": run_id, "master_seed": master, "node_cycles": len(plan), "node_faults": faults}
    args.run_dir.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2))
    store_path = ndir / "store.sqlite"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "SYNC_", "DUR_"))
           and k not in ("STORE_PATH", "DURABILITY_SHIM", "PYTHONPATH")}
    env |= {"DRY_RUN": "1", "STORE_PATH": str(store_path), "SNAPSHOT_INTERVAL_SECONDS": "0",
            "SYNC_INTERVAL_SECONDS": "0", "SIM_URL": f"http://127.0.0.1:{free_port()}", "PYTHONPATH": str(ROOT)}
    app = App(env, ndir / "app.log")
    print(f"durability: master seed {master}, {len(plan)} node cycles")
    results = []
    try:
        app.start()
        for n, (fault, seed) in enumerate(plan, 1):
            rnd = random.Random(seed)
            prefix = f"dq-{run_id}-{n:03d}"
            lives, f2 = plan_cycle(fault, rnd, prefix)
            store, ledger = ndir / f"c{n:03d}.queue.json", ndir / f"c{n:03d}.ledger.jsonl"
            for life, spec in enumerate(lives, 1):
                run_life(js_dir, app.base, store, ledger, life, {"twoTab": False} | spec,
                         "Verbal order read back: heparin 5000 units subcut q12h")
            results.append((n, fault, seed, prefix, f2, ledger))
    finally:
        app.stop()
    db = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    rows = db.execute("SELECT record_id, client_id FROM captured").fetchall()
    db.close()
    for n, fault, seed, prefix, f2, ledger in results:
        events = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
        r = {"cycle": n, "fault": fault, "seed": seed} | reconcile(fault, prefix, events, rows, f2)
        (ndir / f"c{n:03d}.json").write_text(json.dumps(r, indent=2))
        print(f"  node cycle {n:3d} {fault:8s} seed {seed:10d}: violations={sum(map(len, r['violations'].values()))} "
              f"known={sorted(r['known_windows'])} dead_letter={len(r['dead_letter'])} skipped={r['stall']}",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
