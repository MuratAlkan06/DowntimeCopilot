"""--live mechanics (S4, #51) end to end against DynamoDB Local posing as the live table, through the TEST-ONLY
--endpoint-override hook (the only caller). Two throwaway AWS profiles in a temp config point nowhere real; every
call goes to DynamoDB Local. Proves: the preflight refusal, the proxyless fault subset, the read/write profile
split, reconciliation that ignores planted foreign items, the cleanup key list, and ARN masking on disk.
Skipped without DynamoDB Local (DDB_LOCAL_DIR or DDB_LOCAL_ENDPOINT, see tools/durability/ddb_local.py)."""
from __future__ import annotations

import json

import pytest

from tools.durability import ddb_local, harness, report

pytestmark = pytest.mark.skipif(ddb_local.unavailable() is not None, reason=str(ddb_local.unavailable()))

TABLE, DIRTY = "smoke-live", "smoke-dirty"
FOREIGN = [  # October-like items: a duplicated trailing id and a duplicated capture.create client_id
    ("AuditEvent", "2026-10-01T00:00:00+00:00#ev-oct", {"action": "x"}),
    ("AuditEvent", "2026-10-02T00:00:00+00:00#ev-oct", {"action": "x"}),
    ("AuditEvent", "2026-10-03T00:00:00+00:00#ev-a", {"action": "capture.create", "details": {"client_id": "oct-1"}}),
    ("AuditEvent", "2026-10-04T00:00:00+00:00#ev-b", {"action": "capture.create", "details": {"client_id": "oct-1"}}),
]


def _put(ddb, table, pk, sk, body):
    ddb.put_item(TableName=table, Item={"pk": {"S": pk}, "sk": {"S": sk}, "body": {"S": json.dumps(body)}})


@pytest.fixture
def local(tmp_path, monkeypatch):
    cfg, creds = tmp_path / "aws-config", tmp_path / "aws-credentials"
    cfg.write_text("[profile smoke-write]\nregion = us-east-1\n[profile smoke-read]\nregion = us-east-1\n")
    creds.write_text("[smoke-write]\naws_access_key_id = smokewrite\naws_secret_access_key = smokewrite\n"
                     "[smoke-read]\naws_access_key_id = smokeread\naws_secret_access_key = smokeread\n")
    for k in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds))
    with ddb_local.DynamoDBLocal() as d:
        admin = harness.ddb_client(d.endpoint)
        for t in (TABLE, DIRTY):
            if t in admin.list_tables()["TableNames"]:
                admin.delete_table(TableName=t)
            harness.create_table(admin, t)
        for pk, sk, body in FOREIGN:
            _put(admin, TABLE, pk, sk, body)
        _put(admin, DIRTY, "AuditEvent", "2026-10-09T00:00:00+00:00#ev-old",
             {"action": "capture.create", "details": {"client_id": "dur-old-001-1"}})
        yield d.endpoint, admin


def _argv(run, endpoint, table, *extra):
    return ["--run-dir", str(run), "--live", "--table", table, "--write-profile", "smoke-write",
            "--read-profile", "smoke-read", "--region", "us-east-1", "--endpoint-override", endpoint, *extra]


def test_live_mode_against_ddb_local(local, tmp_path, monkeypatch):
    endpoint, admin = local
    # preflight refusal: a table already holding a dur- client_id stops the run before anything is written
    assert harness.main(_argv(tmp_path / "dirty", endpoint, DIRTY, "--cycles", "1")) == 3
    assert not (tmp_path / "dirty").exists()

    envs, profiles, ops = [], [], []
    real_start, real_session = harness.App.start, harness.session_client

    def start(self, **extra):
        envs.append(self.env | extra)
        return real_start(self, **extra)

    def session(profile, region, ep=None):
        profiles.append(profile)
        c = real_session(profile, region, ep)
        c.meta.events.register("before-call.dynamodb", lambda model, **_: ops.append(model.name))
        return c

    monkeypatch.setattr(harness.App, "start", start)
    monkeypatch.setattr(harness, "session_client", session)
    run = tmp_path / "run"
    faults = ",".join(harness.LIVE_FAULTS)
    assert harness.main(_argv(run, endpoint, TABLE, "--faults", faults, "--cycles", str(len(harness.LIVE_FAULTS)),
                              "--seed", "7")) == 0

    # read/write split: the app only ever had the write profile; the harness only read, through the read profile
    assert profiles == ["smoke-read"] and set(ops) == {"DescribeTable", "Scan"}
    assert envs and all(e["AWS_PROFILE"] == "smoke-write" and "AWS_ACCESS_KEY_ID" not in e
                        and "smoke-read" not in json.dumps(e) for e in envs)
    assert any(e["SYNC_ENDPOINT_URL"] != endpoint for e in envs)  # F7r: dead endpoint, then the healed restart
    assert sorted(admin.list_tables()["TableNames"]) == sorted([TABLE, DIRTY])  # no CreateTable under --live

    cycles = [json.loads((run / "server" / f"c{n:03d}.json").read_text()) for n in range(1, 8)]
    assert sorted(c["fault"] for c in cycles) == sorted(harness.LIVE_FAULTS)
    for c in cycles:
        assert c["violations"] == {}, c
        assert c["proxy"] is None and c["reconcile"]["ddb_items_foreign"] >= len(FOREIGN)
    poison = next(c for c in cycles if c["fault"] == "poison")
    assert any(s.startswith("poison status=422") for s in poison["steps"])

    # cleanup keys: exactly the run's items, none of the planted foreign ones
    keys = {(k["pk"], k["sk"]) for k in map(json.loads, (run / "cleanup-keys.jsonl").read_text().splitlines())}
    table_keys = {(i["pk"], i["sk"]) for i in harness.scan(admin, TABLE)}
    assert keys == table_keys - {(pk, sk) for pk, sk, _ in FOREIGN}

    # hygiene: the DescribeTable ARN went to meta.json masked; nothing on disk names the (local) account
    meta = json.loads((run / "meta.json").read_text())
    assert meta["live"]["table_arn"] == "arn:<masked>" and meta["live"]["preexisting_items"] == len(FOREIGN)
    r = report.write(run)
    assert r["sides"]["server"]["cycles"] == 7 and r["sides"]["server"]["violations"] == {}
    for p in run.rglob("*"):
        if p.is_file() and p.suffix in (".json", ".jsonl", ".md", ".log"):
            text = p.read_text(errors="replace")
            assert "arn:aws" not in text and "000000000000" not in text, p
