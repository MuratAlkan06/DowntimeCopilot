"""S10 sync: SQLite outbox, DynamoDB mirror (stubbed boto), LocalFallback on denial, background worker,
GET /sync/status. No network: every AWS client here is a stub."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi.testclient import TestClient

from app import clock, sim_client
from app.store import get_store, reset_store
from app.store.sqlite import SYNC_AUDIT, Store
from app.sync import base, dynamodb, worker
from simulator import main as sim

TABLE = "downtime-copilot-sync-test"
STATUS_KEYS = {"mode", "mirrored", "pending", "not_mirrored", "last_success_ts", "last_error"}


def err(code: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, op)


class FakeDynamo:
    """Low-level DynamoDB client stub: a dict table keyed by (pk, sk). `deny` = operation that raises
    AccessDeniedException; `exists` = table present (ACTIVE) at start."""

    def __init__(self, exists=True, deny=None, down=False):
        self.exists, self.deny, self.down = exists, deny, down
        self.items: dict[tuple[str, str], dict] = {}
        self.calls: list[str] = []
        self.waited: list[str] = []
        self.created: dict | None = None

    def _gate(self, op):
        self.calls.append(op)
        if self.down:
            raise EndpointConnectionError(endpoint_url="https://dynamodb.invalid")
        if self.deny == op:
            raise err("AccessDeniedException", op)

    def describe_table(self, TableName):
        self._gate("DescribeTable")
        if not self.exists:
            raise err("ResourceNotFoundException", "DescribeTable")
        return {"Table": {"TableName": TableName, "TableStatus": "ACTIVE"}}

    def create_table(self, **kw):
        self._gate("CreateTable")
        self.created, self.exists = kw, True
        return {"TableDescription": {"TableStatus": "CREATING"}}

    def get_waiter(self, name):
        fake = self

        class W:
            def wait(self, **kw):
                fake.waited.append(name)
        return W()

    def put_item(self, TableName, Item):
        self._gate("PutItem")
        self.items[(Item["pk"]["S"], Item["sk"]["S"])] = Item


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setenv("STORE_PATH", str(tmp_path / "sync.sqlite"))
    monkeypatch.setenv("SYNC_EXPORT_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SYNC_TABLE", TABLE)
    base.reset()
    reset_store()
    yield get_store()
    base.reset()
    reset_store()


def use(monkeypatch, fake: FakeDynamo) -> None:
    monkeypatch.setattr(dynamodb, "client", lambda: fake)


def test_item_key_is_stable_and_puts_are_idempotent(store, monkeypatch):
    fake = FakeDynamo()
    use(monkeypatch, fake)
    ev = store.audit("rn", "capture.create", "record", "rec-1")
    assert store.outbox_pending() == 1
    (seq, pk, sk, body), = store.outbox()
    assert pk == SYNC_AUDIT and sk == f"{ev.ts.isoformat()}#{ev.event_id}"
    m = dynamodb.DynamoDBMirror(TABLE, fake)
    m.put(pk, sk, body)
    first = dict(fake.items)
    m.put(pk, sk, body)  # same key -> same item, no second item
    assert fake.items == first and len(fake.items) == 1
    assert fake.items[(pk, sk)]["id"] == {"S": ev.event_id}
    assert fake.calls.count("DescribeTable") == 1  # table checked once per mirror


def test_record_saves_share_one_key(store):
    from contracts import BackloadGroup, CapturedRecord, FormType
    rec = CapturedRecord(record_id="rec-9", form_type=FormType.vitals_flowsheet, image_ref="blob:x",
                         captured_at=datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc), captured_by_role="rn",
                         backload_group=BackloadGroup.nursing_vitals)
    store.save_record(rec)
    store.save_record(rec.model_copy(update={"verified_by": "charge_nurse"}))
    keys = {(pk, sk) for _, pk, sk, _ in store.outbox()}
    assert keys == {("CapturedRecord", "2026-10-24T09:00:00+00:00#rec-9")}
    assert store.outbox_pending() == 2  # not coalesced; the later put overwrites the same item


def test_failed_enqueue_keeps_the_save_and_counts_it(store):
    """The Store._enqueue trade-off (#51): an outbox insert failure lands the save unmirrored, and status counts it."""
    from contracts import BackloadGroup, CapturedRecord, FormType
    rec = CapturedRecord(record_id="rec-1", form_type=FormType.vitals_flowsheet, image_ref="blob:x",
                         captured_at=datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc), captured_by_role="rn",
                         backload_group=BackloadGroup.nursing_vitals)
    store.save_record(rec)
    st = worker.status(store)
    assert st["not_mirrored"] == 0 and st["pending"] == 1  # happy path: queued, nothing lost
    # A statement-level failure on the outbox insert only (the save's transaction stays open and commits).
    store._db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON sync_outbox BEGIN SELECT RAISE(ABORT, 'x'); END")
    store.save_record(rec.model_copy(update={"record_id": "rec-2"}))
    st = worker.status(store)
    assert store.get_record("rec-2") is not None and st["not_mirrored"] == 1 and st["pending"] == 1
    store._db.execute("DROP TABLE kv")  # the count is best effort: a failing increment never raises out either
    store.save_record(rec.model_copy(update={"record_id": "rec-3"}))
    assert store.get_record("rec-3") is not None


def test_ensure_table_creates_on_demand_with_waiter():
    fake = FakeDynamo(exists=False)
    assert dynamodb.ensure_table(fake, TABLE) == "created"
    assert fake.created["BillingMode"] == "PAY_PER_REQUEST"
    assert fake.created["KeySchema"] == [{"AttributeName": "pk", "KeyType": "HASH"},
                                         {"AttributeName": "sk", "KeyType": "RANGE"}]
    assert fake.waited == ["table_exists"]
    active = FakeDynamo()
    assert dynamodb.ensure_table(active, TABLE) == "exists" and active.waited == [] and "CreateTable" not in active.calls


def test_create_table_denied_falls_back_to_local_and_is_recorded(store, monkeypatch, tmp_path):
    fake = FakeDynamo(exists=False, deny="CreateTable")
    use(monkeypatch, fake)
    store.audit("rn", "capture.create", "record", "rec-1")
    assert base.mode() == "dynamodb"
    with pytest.raises(dynamodb.Denied):
        worker.drain(store)
    assert base.mode() == "local" and base.denied() == "CreateTable AccessDeniedException"
    st = worker.status(store)
    assert st["mode"] == "local" and st["last_error"] == "CreateTable AccessDeniedException"
    assert [e.details for e in store.list_audit() if e.action == "sync.fallback"] == [
        {"reason": "CreateTable AccessDeniedException"}]
    assert worker.drain(store) == 2  # the queued event and the fallback record, exported locally
    assert store.outbox_pending() == 0 and fake.items == {}
    assert len((tmp_path / "logs" / "sync-export.jsonl").read_text().splitlines()) == 2


def test_outbox_survives_store_reopen(store, tmp_path):
    for i in range(3):
        store.audit("rn", "capture.create", "record", f"rec-{i}")
    reopened = Store(str(tmp_path / "sync.sqlite"))
    assert reopened.outbox_pending() == 3
    assert worker.drain(reopened, base.DryRunMirror()) == 3
    st = worker.status(reopened)
    assert st["pending"] == 0 and st["mirrored"] == 3 and st["last_success_ts"]
    assert all(e.synced for e in reopened.list_audit())


def test_dead_endpoint_keeps_items_queued_then_reconciles(store, monkeypatch):
    fake = FakeDynamo(down=True)
    use(monkeypatch, fake)
    for i in range(4):
        store.audit("rn", "capture.create", "record", f"rec-{i}")
    with pytest.raises(EndpointConnectionError):
        worker.drain(store)
    st = worker.status(store)
    assert st["pending"] == 4 and st["mirrored"] == 0 and st["last_error"] == "dynamodb: EndpointConnectionError"
    fake.down = False
    assert worker.drain_all(store) == (4, None)
    st = worker.status(store)
    assert st["pending"] == 0 and st["mirrored"] == 4 and st["last_error"] is None and len(fake.items) == 4


def test_worker_thread_drains_and_backs_off(store, monkeypatch):
    fake = FakeDynamo()
    use(monkeypatch, fake)
    for i in range(30):  # more than one batch
        store.audit("rn", "capture.create", "record", f"rec-{i}")
    t, stop = worker.start(0.02)
    deadline = time.monotonic() + 5
    while store.outbox_pending() and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    t.join(2)
    assert store.outbox_pending() == 0 and len(fake.items) == 30 and not t.is_alive()

    fake.down = True
    store.audit("rn", "capture.create", "record", "rec-x")
    waits: list[float] = []

    class Stop(threading.Event):
        def wait(self, timeout=None):
            waits.append(timeout)
            if len(waits) == 6:
                self.set()
            return self.is_set()

    worker.run(Stop(), 1.0)
    assert waits == [2.0, 4.0, 8.0, 15.0, 15.0, 15.0]  # exponential, capped at BACKOFF_MAX_S
    assert store.outbox_pending() == 1


# ------------------------------------------------------------------ through the API

@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("STORE_PATH", str(tmp_path / "api.sqlite"))
    monkeypatch.setenv("SNAPSHOT_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("SYNC_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("SYNC_EXPORT_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(sim_client, "make_client", lambda: TestClient(sim.app))
    sim.state.reset(7)
    clock.reset()
    base.reset()
    reset_store()
    from app.main import app
    with TestClient(app) as c:
        yield c
    clock.reset()
    base.reset()
    reset_store()


def test_status_shape_in_dry_run(api):
    st = api.get("/sync/status").json()
    assert set(st) == STATUS_KEYS and st["mode"] == "dry_run" and st["pending"] == 0
    assert api.post("/snapshot/pull").status_code == 200
    assert api.get("/sync/status").json()["pending"] >= 1
    assert api.post("/sync").json()["pending"] == 0
    st = api.get("/sync/status").json()
    assert set(st) == STATUS_KEYS and st["mirrored"] >= 1 and st["last_error"] is None and st["not_mirrored"] == 0
    assert not any(e.action.startswith("sync.status") for e in get_store().list_audit())  # polls are not audited


def test_handlers_never_raise_on_sync_failure(api, monkeypatch):
    class Broken:
        name = "dynamodb"

        def put(self, pk, sk, body):
            raise EndpointConnectionError(endpoint_url="https://dynamodb.invalid")

    monkeypatch.setattr(base, "get_mirror", lambda: Broken())
    assert api.post("/snapshot/pull").status_code == 200
    before = api.get("/sync/status").json()["pending"]
    r = api.post("/sync")
    assert r.status_code == 200 and r.json()["pushed"] == 0 and r.json()["error"] == "EndpointConnectionError"
    assert api.post("/downtime/start").status_code == 200
    st = api.get("/sync/status").json()
    assert st["pending"] > before and st["last_error"] == "dynamodb: EndpointConnectionError"
    assert st["not_mirrored"] == 0

    saved = len(get_store().audit_log())
    get_store()._db.execute("DROP TABLE sync_outbox")  # even a broken local outbox never fails a handler
    assert api.get("/costs").status_code == 200
    assert any(e.action == "costs.view" for e in get_store().list_audit())
    # ...and every save it could not queue is counted (status itself reads the outbox, so read the kv here)
    assert int(get_store().get_kv("sync.not_mirrored")) == len(get_store().audit_log()) - saved >= 1


# ------------------------------------------------------------------ S3: lifecycle merge, snapshots, exports

from app.sync import s3 as s3sync  # noqa: E402

S9_AUDIO_RULE = {"ID": "expire-after-1-day", "Filter": {"Prefix": ""}, "Status": "Enabled",
                 "Expiration": {"Days": 1}, "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}
FOREIGN_RULE = {"ID": "someone-elses", "Filter": {"Prefix": "tmp/"}, "Status": "Enabled", "Expiration": {"Days": 3}}


class FakeS3:
    def __init__(self, rules=None, objects=()):
        self.rules, self.objects, self.calls = rules, dict.fromkeys(objects, b""), []

    def head_bucket(self, **kw):
        self.calls.append(("head_bucket", kw))

    def get_bucket_lifecycle_configuration(self, Bucket):
        self.calls.append(("get_bucket_lifecycle_configuration", {}))
        if self.rules is None:
            raise err("NoSuchLifecycleConfiguration", "GetBucketLifecycleConfiguration")
        return {"Rules": [dict(r) for r in self.rules]}

    def put_bucket_lifecycle_configuration(self, Bucket, LifecycleConfiguration):
        self.calls.append(("put_bucket_lifecycle_configuration", {}))
        self.rules = LifecycleConfiguration["Rules"]

    def head_object(self, Bucket, Key):
        self.calls.append(("head_object", {"Key": Key}))
        if Key not in self.objects:
            raise err("404", "HeadObject")

    def put_object(self, **kw):
        self.calls.append(("put_object", kw))
        self.objects[kw["Key"]] = kw["Body"]

    def ops(self, op):
        return [kw for o, kw in self.calls if o == op]


def test_lifecycle_merge_scopes_audio_rule_and_appends_ours():
    merged = s3sync.merge_lifecycle([S9_AUDIO_RULE, FOREIGN_RULE])
    audio, foreign, snaps, exports = merged
    assert audio == S9_AUDIO_RULE | {"Filter": {"Prefix": "audio/"}}  # same id and 1-day expiry, now scoped
    assert foreign == FOREIGN_RULE
    assert snaps["Filter"] == {"Prefix": "snapshots/"} and snaps["Expiration"] == {"Days": 7}
    assert exports["Filter"] == {"Prefix": "exports/"} and exports["Expiration"] == {"Days": 7}
    assert s3sync.merge_lifecycle(merged) == merged  # idempotent
    assert not any(s3sync._is_global(r) for r in merged)


def test_ensure_lifecycle_reads_merges_and_writes_only_on_change():
    fake = FakeS3(rules=[S9_AUDIO_RULE])
    rules = s3sync.ensure_lifecycle(fake, "b")
    assert [r["ID"] for r in rules] == ["expire-after-1-day", "expire-snapshots-after-7-days",
                                        "expire-exports-after-7-days"]
    assert fake.rules == rules and len(fake.ops("put_bucket_lifecycle_configuration")) == 1
    s3sync.ensure_lifecycle(fake, "b")
    assert len(fake.ops("put_bucket_lifecycle_configuration")) == 1  # already merged: no second write
    empty = FakeS3(rules=None)
    assert len(s3sync.ensure_lifecycle(empty, "b")) == 3


def test_snapshot_mirror_puts_newest_pull_once(store, monkeypatch):
    from contracts import PatientSnapshot
    monkeypatch.setenv("AUDIO_BUCKET", "bkt")
    s3sync._ready.clear()
    assert s3sync.mirror_snapshot(store, FakeS3(rules=[])) is None  # no pull yet
    sim.state.reset(7)
    pts = [PatientSnapshot.model_validate(p) for p in TestClient(sim.app).get("/bca/snapshot").json()[:2]]
    store.save_snapshot(pts, "pre")
    fake = FakeS3(rules=[S9_AUDIO_RULE])
    key = s3sync.mirror_snapshot(store, fake)
    assert key.startswith("snapshots/pre-") and key.endswith(".json")
    (put,) = fake.ops("put_object")
    assert put["Bucket"] == "bkt" and put["ServerSideEncryption"] == "AES256" and put["Key"] == key
    assert s3sync.mirror_snapshot(store, fake) is None and len(fake.ops("put_object")) == 1
    assert [r["Filter"]["Prefix"] for r in fake.rules] == ["audio/", "snapshots/", "exports/"]


def test_audit_export_to_s3_is_idempotent_per_timestamp(store, monkeypatch):
    monkeypatch.setenv("AUDIO_BUCKET", "bkt")
    s3sync._ready.clear()
    store.audit("rn", "capture.create", "record", "rec-1")
    at = datetime(2100, 1, 1, 9, 30, 15, 999, tzinfo=timezone.utc)
    fake = FakeS3(rules=[])
    first = s3sync.export_audit(store, at, fake)
    assert first == {"target": "s3", "key": "exports/audit-21000101T093015Z.jsonl", "created": True, "events": 1}
    again = s3sync.export_audit(store, at, fake)
    assert again["created"] is False and len(fake.ops("put_object")) == 1  # same timestamp key: no second copy
    (put,) = fake.ops("put_object")
    assert put["ServerSideEncryption"] == "AES256" and b'"synced"' not in put["Body"]


def test_export_endpoint_and_dry_run_local_file(api, tmp_path):
    assert api.post("/snapshot/pull").status_code == 200
    r1 = api.post("/sync/export", params={"at": "2100-01-01T00:00:00Z"})
    assert r1.status_code == 200, r1.text
    out = r1.json()
    assert out["target"] == "local" and out["created"] is True and out["key"] == "exports/audit-21000101T000000Z.jsonl"
    path = tmp_path / "logs" / out["key"]
    body = path.read_bytes()
    assert out["events"] == len(body.splitlines()) >= 1
    r2 = api.post("/sync/export", params={"at": "2100-01-01T00:00:00Z"}).json()
    assert r2["created"] is False and r2["key"] == out["key"] and path.read_bytes() == body
    assert [e.details["created"] for e in get_store().list_audit() if e.action == "sync.export"] == [False, True]
