"""tools/durability (#51, docs/durability.md): the harness imports cleanly, the report generator is pure, and
the shim is inert without DURABILITY_SHIM=1. Fast: no uvicorn, no DynamoDB Local, no node."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tools.durability import harness, proxy, report

ROOT = Path(__file__).resolve().parent.parent
SHIM = ROOT / "tools" / "durability" / "shim"


def test_modules_import_without_side_effects():
    assert "F6rb" in harness.EXPECTED_FAIL
    plan = harness.schedule(42, 12, list(harness.SERVER_FAULTS))
    assert plan == harness.schedule(42, 12, list(harness.SERVER_FAULTS))  # seeded, replayable
    assert {f for f, _ in plan[:len(harness.SERVER_FAULTS)]} == set(harness.SERVER_FAULTS)  # stratified
    req = b"POST / HTTP/1.1\r\nContent-Length: 2\r\n\r\nab"
    assert proxy._http_request_end(req + b"POST") == len(req)  # framing for the deny and fwd_drop modes
    assert proxy._http_request_end(b"POST / HTTP/1.1\r\nContent-Length: 2\r\n\r\na") is None


def _canned_run(d: Path) -> None:
    (d / "server").mkdir(parents=True)
    (d / "node").mkdir()
    (d / "meta.json").write_text(json.dumps({"run_id": "canned", "master_seed": 1, "sync_interval_s": 0.25}))
    base = {"violations": {}, "known_windows": {}, "expected_fail": {}, "reconcile": {"not_mirrored_kv": 0}}
    cycles = [
        base | {"cycle": 1, "fault": "none", "seed": 11, "drain": {"drain_s": 0.5}},
        base | {"cycle": 2, "fault": "F6", "seed": 12, "drain": {"drain_s": 1.5},
                "known_windows": {"not_mirrored": ["rec-1"]}, "reconcile": {"not_mirrored_kv": 1}},
        base | {"cycle": 3, "fault": "F6rb", "seed": 13, "drain": {"drain_s": 0.25},
                "expected_fail": {"S5": ["dur-x-3-1"]}},
    ]
    for c in cycles:
        (d / "server" / f"c{c['cycle']:03d}.json").write_text(json.dumps(c))
    (d / "node" / "c001.json").write_text(json.dumps(
        base | {"cycle": 1, "fault": "poison", "seed": 21, "stall": 2, "known_windows": {"stall": ["p"]}}))
    (d / "node" / "c001.queue.json").write_text("{}")  # the persisted queue store file: not a cycle result


def test_report_is_a_pure_function_of_the_run_dir(tmp_path):
    _canned_run(tmp_path)
    first = report.write(tmp_path)
    md = (tmp_path / "report.md").read_text()
    again = report.write(tmp_path)
    assert first == again and md == (tmp_path / "report.md").read_text()
    s = first["sides"]["server"]
    assert first["verdict"] == "PASS" and s["records_lost"] == 0 and s["records_duplicated"] == 0
    assert s["not_mirrored"] == 1 and s["known_windows"] == {"not_mirrored": 1}
    assert s["expected_fail"] == {"S5": 1}
    assert s["drain_s"]["F6"] == {"n": 1, "p50": 1.5, "max": 1.5}
    assert first["sides"]["node"]["stall_skipped_attempts"] == [2]


def test_report_fails_on_a_violation(tmp_path):
    _canned_run(tmp_path)
    p = tmp_path / "server" / "c001.json"
    p.write_text(json.dumps(json.loads(p.read_text()) | {"violations": {"lost_acked": ["dur-x-1-1"]}}))
    r = report.build(tmp_path)
    assert r["verdict"] == "FAIL" and r["sides"]["server"]["records_lost"] == 1
    assert r["sides"]["server"]["failing_cycles"][0]["seed"] == 11


def _probe(env_extra: dict) -> dict:
    code = ("import json, app.ai.client as c, app.store.sqlite as s; "
            "print(json.dumps({'converse': c._converse.__module__, 'save': s.Store.save_record.__module__, "
            "'enqueue': s.Store._enqueue.__module__}))")
    env = {k: v for k, v in os.environ.items() if k != "DURABILITY_SHIM"} | env_extra
    env["PYTHONPATH"] = os.pathsep.join([str(SHIM), str(ROOT)])
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, env=env,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_shim_is_inert_without_the_flag():
    assert _probe({}) == {"converse": "app.ai.client", "save": "app.store.sqlite", "enqueue": "app.store.sqlite"}


def test_shim_patches_only_with_the_flag():
    assert set(_probe({"DURABILITY_SHIM": "1"}).values()) == {"sitecustomize"}
