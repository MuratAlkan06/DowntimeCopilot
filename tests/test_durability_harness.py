"""tools/durability (#51, docs/durability.md): the harness imports cleanly, the report generator is pure, and
the shim is inert without DURABILITY_SHIM=1. Fast: no uvicorn, no DynamoDB Local, no node."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tools.durability import harness, proxy, queue_cycles, report

ROOT = Path(__file__).resolve().parent.parent
SHIM = ROOT / "tools" / "durability" / "shim"


def test_modules_import_without_side_effects():
    assert "F6rb" in harness.SERVER_FAULTS and "poison" in queue_cycles.NODE_FAULTS
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
        base | {"cycle": 3, "fault": "F7a", "seed": 13, "drain": {"drain_s": 0.25},
                "expected_fail": {"S5": ["dur-x-3-1"]}},  # the mechanism stays for future tracked failures
    ]
    for c in cycles:
        (d / "server" / f"c{c['cycle']:03d}.json").write_text(json.dumps(c))
    (d / "node" / "c001.json").write_text(json.dumps(
        base | {"cycle": 1, "fault": "poison", "seed": 21, "stall": 2, "dead_letter": ["p"]}))
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
    node = first["sides"]["node"]
    assert node["stall_skipped_attempts"] == [2]
    assert node["dead_letter"] == 1 and node["known_windows"] == {} and s["dead_letter"] == 0  # #55: not a window
    assert "| Dead-lettered poison items (kept, retryable) | 1 |" in md


def test_report_fails_on_a_violation(tmp_path):
    _canned_run(tmp_path)
    p = tmp_path / "server" / "c001.json"
    p.write_text(json.dumps(json.loads(p.read_text()) | {"violations": {"lost_acked": ["dur-x-1-1"]}}))
    r = report.build(tmp_path)
    assert r["verdict"] == "FAIL" and r["sides"]["server"]["records_lost"] == 1
    assert r["sides"]["server"]["failing_cycles"][0]["seed"] == 11


def _rec(**kw) -> dict:
    return {"lost_acked": [], "unacked_not_in_sqlite": [], "missing_mirror_records": [], "missing_mirror_audits": [],
            "not_mirrored_kv": 0, "dup_sqlite_client_id": [], "dup_ddb_item_id": [], "dup_ddb_client_id": [],
            "record_without_create_audit": []} | kw


def test_f6_rollback_is_a_normal_invariant_since_54():
    """F6rb left expected_fail when #54 was fixed: the save surviving unmirrored is a known window, a loss fails."""
    drained = {"drained": True}
    ok = harness.classify("F6rb", _rec(missing_mirror_records=["rec-1"], not_mirrored_kv=1), drained)
    assert ok == {"violations": {}, "known_windows": {"not_mirrored": ["rec-1"]}, "expected_fail": {}}
    lost = harness.classify("F6rb", _rec(lost_acked=["dur-x-1-2"]), drained)
    assert lost["violations"] == {"lost_acked": ["dur-x-1-2"]} and lost["expected_fail"] == {}


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


def _ev(**kw) -> dict:
    return {"life": 1} | kw


def test_node_reconcile_f2_and_poison_since_55():
    """F2 and poison are passing checks since #55: the 5xx'd paper lands once under its own id, a resubmit duplicate
    is a violation (no known window), the poison item dead-letters, and one still live after the heal fails."""
    rows = [("r1", "q-p1"), ("r2", "q-p2")]
    ok_events = [_ev(ev="attempt", cid="q-p1", n=1, fault="real_5xx"), _ev(ev="queued", cid="q-p1", paper="q-p1"),
                 _ev(ev="ack", cid="q-p1", status=200, via="drain"), _ev(ev="ack", cid="q-p2", status=201, via="submit"),
                 _ev(ev="queued", cid="q-poison", poison=True), _ev(ev="left", ids=[], dead=["q-poison"])]
    ok = queue_cycles.reconcile("F2", "q", ok_events, rows, {"q-p1"})
    assert ok["violations"] == {} and ok["known_windows"] == {} and ok["dead_letter"] == ["q-poison"]
    dup = queue_cycles.reconcile("F2", "q", ok_events, rows + [("r3", "q-p1-r1")], {"q-p1"})
    assert dup["violations"] == {"dup_paper": ["q-p1"], "f2_not_exactly_once": ["q-p1"]}
    assert "f2_resubmit_duplicates" not in dup["known_windows"]
    stuck = ok_events[:-1] + [_ev(ev="left", ids=["q-poison"], dead=[])]
    assert queue_cycles.reconcile("poison", "q", stuck, rows, set())["violations"] == {"queue_not_drained": ["q-poison"]}
    wrong = ok_events[:-1] + [_ev(ev="left", ids=[], dead=["q-p2", "q-poison"])]
    assert queue_cycles.reconcile("poison", "q", wrong, rows, set())["violations"] == {"dead_letter_unexpected": ["q-p2"]}


# ------------------------------------------------------------------ --live (S4): guard, scoping, hygiene
import pytest  # noqa: E402

LIVE_TRIO = ["--table", "t", "--write-profile", "w", "--read-profile", "r"]


@pytest.mark.parametrize("argv", [
    ["--live"],                                                    # no trio
    ["--live", "--table", "t", "--write-profile", "w"],            # no read profile
    ["--live", "--table", "", "--write-profile", "w", "--read-profile", "r"],  # empty is not explicit
    ["--live", "--table", "t", "--write-profile", "w", "--read-profile", "w"],  # no read/write split
    LIVE_TRIO,                                                     # trio without the flag
    ["--endpoint-override", "http://127.0.0.1:1"],                 # test hook without the flag
    ["--live", *LIVE_TRIO, "--faults", "F3"],                      # proxy faults are not live-safe
    ["--live", *LIVE_TRIO, "--faults", "none,F4"],
    ["--live", *LIVE_TRIO, "--faults", "F6rb"],
    ["--live", *LIVE_TRIO, "--replay", "F7b:1"],
])
def test_live_refuses_before_any_aws_session(argv, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("an AWS session was opened before the guard refused")
    monkeypatch.setattr(harness, "session_client", boom)
    with pytest.raises(SystemExit) as exc:
        harness.main(["--run-dir", str(tmp_path / "run"), "--cycles", "1", *argv])
    assert exc.value.code == 2 and not (tmp_path / "run").exists()


def test_live_fault_menu_is_the_proxyless_subset():
    assert harness.LIVE_FAULTS == ("none", "F1", "F1die", "F2", "F4k", "F7r", "poison")
    assert not {"F3", "F4", "F4fd", "F5", "F6", "F6rb", "F7a", "F7b"} & set(harness.LIVE_FAULTS)


class _FakeReader:
    """A read-profile DynamoDB client stand-in that records every call."""

    def __init__(self, items: list[dict]) -> None:
        self.items, self.calls = items, []

    def describe_table(self, TableName):
        self.calls.append("DescribeTable")
        return {"Table": {"TableName": TableName, "TableStatus": "ACTIVE",
                          "TableArn": f"arn:aws:dynamodb:us-east-1:123456789012:table/{TableName}"}}

    def get_paginator(self, op):
        self.calls.append(op)
        items = self.items
        return type("P", (), {"paginate": lambda self, **kw: [{"Items": [
            {k: {"S": v} for k, v in i.items()} for i in items]}]})()


def _item(pk, sk, body) -> dict:
    return {"pk": pk, "sk": sk, "body": json.dumps(body)}


def test_live_preflight_reads_only_with_the_read_profile_and_refuses_dur_items(tmp_path, monkeypatch):
    october = [_item("AuditEvent", f"2026-10-0{i}T00:00:00+00:00#ev-oct{i}",
                     {"action": "capture.create", "details": {"client_id": f"oct-{i}"}}) for i in range(1, 4)]
    opened: list[str] = []
    reader = _FakeReader(october)
    monkeypatch.setattr(harness, "session_client", lambda profile, region, ep=None: opened.append(profile) or reader)
    run = tmp_path / "run"
    assert harness.main(["--run-dir", str(run), "--live", *LIVE_TRIO, "--cycles", "0", "--seed", "1"]) == 0
    assert opened == ["r"] and set(reader.calls) == {"DescribeTable", "scan"}
    meta = (run / "meta.json").read_text()
    assert json.loads(meta)["live"]["preexisting_items"] == 3 and "123456789012" not in meta
    assert "arn:<masked>" in meta and "arn:aws" not in meta
    reader.items = october + [_item("AuditEvent", "2026-10-09T00:00:00+00:00#ev-x",
                                    {"action": "capture.create", "details": {"client_id": "dur-old-001-1"}})]
    assert harness.main(["--run-dir", str(tmp_path / "run2"), "--live", *LIVE_TRIO, "--cycles", "0"]) == 3
    assert not (tmp_path / "run2").exists()


def test_live_cycle_json_is_arn_masked(tmp_path):
    arn = "arn:aws:iam::123456789012:assumed-role/downtime-runtime/botocore-session-1"
    cycle = {"cycle": 1, "fault": "F4k", "seed": 4294967295, "steps": [f"AccessDenied for {arn}"],
             "drain": {"status": {"mode": "local", "detail": f"User: {arn} is not authorized"}},
             "reconcile": {"missing_mirror_records": ["rec-0a1b2c3d-123456789012"]}, "account": "123456789012"}
    p = tmp_path / "c001.json"
    harness.write_json(p, cycle)
    text = p.read_text()
    assert "arn:aws" not in text and ":123456789012:" not in text and '"123456789012"' not in text
    assert "assumed-role" not in text
    out = json.loads(text)
    assert out["account"] == "<account>" and out["seed"] == 4294967295
    assert out["reconcile"]["missing_mirror_records"] == ["rec-0a1b2c3d-123456789012"]  # ids are not accounts


def _store_with_one_capture(store_path: Path) -> dict:
    """A real Store: one capture.create audit (its outbox row dropped: already drained) plus a raw record row."""
    import sqlite3
    from app.store.sqlite import Store
    st = Store(str(store_path))
    ev = st.audit("rn", "capture.create", "captured_record", "rec-1", {"client_id": "dur-r-001-1"})
    st.close()
    db = sqlite3.connect(store_path)
    db.execute("DELETE FROM sync_outbox")
    db.execute("INSERT INTO captured (record_id, seq, body, client_id) VALUES ('rec-1', 1, '{}', 'dur-r-001-1')")
    db.commit()
    db.close()
    return json.loads(ev.model_dump_json())


def test_reconcile_ignores_foreign_items(tmp_path):
    """October's items in a shared table: foreign dup ids / client_ids are ignored, the run's own are not."""
    store_path = tmp_path / "store.sqlite"
    ev = _store_with_one_capture(store_path)
    own = [_item("AuditEvent", f"{ev['ts']}#{ev['event_id']}", ev),
           _item("CapturedRecord", "2026-10-27T00:00:00+00:00#rec-1", {"record_id": "rec-1"})]
    foreign = [_item("AuditEvent", "2026-10-01T00:00:00+00:00#ev-oct", {"action": "x"}),
               _item("AuditEvent", "2026-10-02T00:00:00+00:00#ev-oct", {"action": "x"}),  # foreign dup id
               *[_item("AuditEvent", f"2026-10-0{d}T00:00:00+00:00#ev-{d}",
                       {"action": "capture.create", "details": {"client_id": "oct-1"}}) for d in (3, 4)]]
    ledger = [{"client_id": "dur-r-001-1", "status": 201, "record_id": "rec-1"}]
    rec = harness.reconcile(store_path, own + foreign, tmp_path / "none.jsonl", ledger)
    assert rec["dup_ddb_item_id"] == [] and rec["dup_ddb_client_id"] == []
    assert rec["ddb_items"] == 2 and rec["ddb_items_foreign"] == 4
    assert rec["missing_mirror_records"] == [] and rec["missing_mirror_audits"] == []
    assert harness.classify("none", rec, {"drained": True})["violations"] == {}
    ghost = _item("AuditEvent", "2026-10-27T00:00:01+00:00#ev-ghost",
                  {"action": "capture.create", "details": {"client_id": "dur-r-001-1"}})
    dup_own = _item("CapturedRecord", "2026-10-27T00:00:02+00:00#rec-1", {"record_id": "rec-1"})
    rec = harness.reconcile(store_path, own + foreign + [ghost, dup_own], tmp_path / "none.jsonl", ledger)
    assert rec["dup_ddb_client_id"] == ["dur-r-001-1"] and rec["dup_ddb_item_id"] == ["rec-1"]


def test_cleanup_keys_follow_sync_key(tmp_path):
    from app.store.sqlite import Store
    store_path = tmp_path / "store.sqlite"
    st = Store(str(store_path))
    evs = [st.audit("rn", "capture.create", "captured_record", f"rec-{i}", {}) for i in range(2)]
    st.close()
    assert harness.cleanup_keys(store_path) == [{"pk": "AuditEvent", "sk": f"{e.ts.isoformat()}#{e.event_id}"}
                                                for e in evs]
