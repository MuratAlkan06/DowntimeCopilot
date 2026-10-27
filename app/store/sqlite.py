"""Local store: stdlib sqlite3, no ORM. SQLite is the local source of truth (docs/decisions.md);
cloud mirrors in app/sync/ copy from here.

TODO(encryption at rest): wrap `_enc`/`_dec` with an envelope key (e.g. Fernet keyed from the OS
keychain or KMS data key). They are pass-throughs today; README "Limitations" says demonstrated,
not hardened.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from contracts import AuditEvent, CapturedRecord, Discrepancy, DowntimePacket, PatientSnapshot

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS pulls (pull_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, pulled_at TEXT);
CREATE TABLE IF NOT EXISTS snapshots (pull_id INTEGER, patient_id TEXT, body TEXT, PRIMARY KEY (pull_id, patient_id));
CREATE TABLE IF NOT EXISTS packets (patient_id TEXT PRIMARY KEY, body TEXT);
CREATE TABLE IF NOT EXISTS captured (record_id TEXT PRIMARY KEY, seq INTEGER, body TEXT);
CREATE TABLE IF NOT EXISTS blobs (ref TEXT PRIMARY KEY, content_type TEXT, data BLOB);
CREATE TABLE IF NOT EXISTS discrepancies (discrepancy_id TEXT PRIMARY KEY, body TEXT);
CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE, body TEXT,
                                  synced INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS costs (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, job TEXT, model_id TEXT,
                                  tokens_in INTEGER, tokens_out INTEGER, latency_ms INTEGER, usd REAL,
                                  dry_run INTEGER);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sync_outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, pk TEXT NOT NULL, sk TEXT NOT NULL,
                                        body TEXT NOT NULL);
"""

# Cloud-mirror outbox (docs/decisions.md S10). Every audit event and every captured-record save adds one row in
# the same local transaction; app.sync.worker drains it oldest first in a background thread. Rows are never
# coalesced: the item key (pk, sk) is stable, so a later put of the same record overwrites the earlier one.
SYNC_AUDIT, SYNC_RECORD = "AuditEvent", "CapturedRecord"


def sync_key(ts: datetime, item_id: str) -> str:
    """DynamoDB sort key "<iso-ts>#<id>": one item per event / record, whatever the number of puts."""
    return f"{ts.isoformat()}#{item_id}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _enc(text: str) -> str:  # TODO(encryption at rest)
    return text


def _dec(text: str) -> str:  # TODO(encryption at rest)
    return text


class Store:
    def __init__(self, path: str, now: Callable[[], datetime] = utcnow) -> None:
        """`now` stamps audit events and cost rows (app.store.get_store passes the unit clock)."""
        self.path = path
        self.now = now
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        # Storage only, not part of CapturedRecord; added in place so older stores keep working.
        # client_id: offline-queue idempotency key (S7); NULLs never collide in a SQLite unique index.
        # text_source: where a record's read text came from (app.capture.pipeline.TEXT_SOURCES), set at capture.
        have = {r[1] for r in self._db.execute("PRAGMA table_info(captured)")}
        for col in ("client_id", "text_source"):
            if col not in have:
                self._db.execute(f"ALTER TABLE captured ADD COLUMN {col} TEXT")  # fixed names, not user input
        self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS captured_client_id ON captured (client_id)")
        self._db.commit()

    def close(self) -> None:
        """Close the connection (the readiness drill's ephemeral store, app/drill.py)."""
        with self._lock:
            self._db.close()

    def _enqueue(self, pk: str, sk: str, body: str) -> None:
        """Caller holds the lock and commits. A local SQLite insert only: a failure is logged, never raised,
        so the audit row or record save it rides with still lands (the handler never sees a sync problem).
        Each such failure adds one to kv `sync.not_mirrored` (GET /sync/status), best effort."""
        try:
            self._db.execute("INSERT INTO sync_outbox (pk, sk, body) VALUES (?, ?, ?)", (pk, sk, body))
        except sqlite3.Error as exc:
            log.warning("sync outbox enqueue failed (%s); item not mirrored", type(exc).__name__)
            # If the whole transaction rolls back (FULL/IOERR), this count rolls back with the save: #51 F6, harness.
            try:
                self._db.execute("INSERT INTO kv (key, value) VALUES ('sync.not_mirrored', '1') ON CONFLICT (key) "
                                 "DO UPDATE SET value = CAST(value AS INTEGER) + 1")
            except sqlite3.Error:
                pass  # a dying DB never raises out of the trade-off path; the warning above is the record

    def _exec(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            cur = self._db.execute(sql, args)
            rows = cur.fetchall()
            self._db.commit()
            return rows

    def clear(self) -> None:
        """Delete every row (demo reset: `python -m simulator.seed --fresh`). The schema stays, so a
        running app keeps its open connection and simply sees an empty store."""
        with self._lock:
            tables = [r[0] for r in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            for t in tables:
                self._db.execute(f"DELETE FROM {t}")  # names come from sqlite_master, not user input
            self._db.commit()

    # ------------------------------------------------------------ kv
    def set_kv(self, key: str, value: str | None) -> None:
        self._exec("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))

    def get_kv(self, key: str, default: str | None = None) -> str | None:
        rows = self._exec("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0][0] if rows and rows[0][0] is not None else default

    # ------------------------------------------------------------ snapshots
    def save_snapshot(self, patients: list[PatientSnapshot], kind: str = "pre") -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO pulls (kind, pulled_at) VALUES (?, ?)", (kind, utcnow().isoformat()))
            pull_id = cur.lastrowid
            self._db.executemany(
                "INSERT INTO snapshots (pull_id, patient_id, body) VALUES (?, ?, ?)",
                [(pull_id, p.patient_id, _enc(p.model_dump_json())) for p in patients],
            )
            self._db.commit()
        return pull_id

    def newest_pull(self) -> tuple[int, str, str, list[PatientSnapshot]] | None:
        """(pull_id, kind, pulled_at, patients) of the newest pull of any kind (S3 snapshot mirror)."""
        rows = self._exec("SELECT pull_id, kind, pulled_at FROM pulls ORDER BY pull_id DESC LIMIT 1")
        if not rows:
            return None
        pull_id, kind, pulled_at = rows[0]
        bodies = self._exec("SELECT body FROM snapshots WHERE pull_id = ? ORDER BY patient_id", (pull_id,))
        return pull_id, kind, pulled_at, [PatientSnapshot.model_validate_json(_dec(b)) for (b,) in bodies]

    def latest_snapshot(self, kind: str = "pre") -> list[PatientSnapshot]:
        rows = self._exec(
            "SELECT s.body FROM snapshots s WHERE s.pull_id = (SELECT MAX(pull_id) FROM pulls WHERE kind = ?)",
            (kind,),
        )
        return [PatientSnapshot.model_validate_json(_dec(r[0])) for r in rows]

    # ------------------------------------------------------------ packets
    def save_packet(self, packet: DowntimePacket) -> None:
        self._exec("INSERT OR REPLACE INTO packets (patient_id, body) VALUES (?, ?)",
                   (packet.patient_id, _enc(packet.model_dump_json())))

    def get_packet(self, patient_id: str) -> DowntimePacket | None:
        rows = self._exec("SELECT body FROM packets WHERE patient_id = ?", (patient_id,))
        return DowntimePacket.model_validate_json(_dec(rows[0][0])) if rows else None

    def list_packets(self) -> list[DowntimePacket]:
        return [DowntimePacket.model_validate_json(_dec(r[0])) for r in self._exec("SELECT body FROM packets")]

    # ------------------------------------------------------------ captured records + blobs
    def save_record(self, record: CapturedRecord, client_id: str | None = None, text_source: str | None = None) -> None:
        """`client_id`: the device's id for this submission (offline queue). `text_source`: where the read
        text came from, known at capture only. Later saves (verify, edit, entered) keep both."""
        with self._lock:
            row = self._db.execute("SELECT seq FROM captured WHERE record_id = ?", (record.record_id,)).fetchone()
            seq = row[0] if row else (self._db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM captured").fetchone()[0])
            self._db.execute(
                "INSERT INTO captured (record_id, seq, body, client_id, text_source) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (record_id) DO UPDATE SET body = excluded.body, "
                "client_id = COALESCE(excluded.client_id, captured.client_id), "
                "text_source = COALESCE(excluded.text_source, captured.text_source)",
                (record.record_id, seq, _enc(record.model_dump_json()), client_id, text_source))
            self._enqueue(SYNC_RECORD, sync_key(record.captured_at, record.record_id), record.model_dump_json())
            self._db.commit()

    def get_record(self, record_id: str) -> CapturedRecord | None:
        rows = self._exec("SELECT body FROM captured WHERE record_id = ?", (record_id,))
        return CapturedRecord.model_validate_json(_dec(rows[0][0])) if rows else None

    def text_source(self, record_id: str) -> str | None:
        """None for an unknown record or one captured before the column existed."""
        rows = self._exec("SELECT text_source FROM captured WHERE record_id = ?", (record_id,))
        return rows[0][0] if rows else None

    def record_by_client_id(self, client_id: str) -> CapturedRecord | None:
        rows = self._exec("SELECT body FROM captured WHERE client_id = ?", (client_id,))
        return CapturedRecord.model_validate_json(_dec(rows[0][0])) if rows else None

    def list_records(self) -> list[CapturedRecord]:
        return [CapturedRecord.model_validate_json(_dec(r[0]))
                for r in self._exec("SELECT body FROM captured ORDER BY seq")]

    def save_blob(self, data: bytes, content_type: str, ref: str | None = None) -> str:
        """`ref` lets a caller use a derivable name (per-field crops: app/capture/crops.crop_ref)."""
        ref = ref or f"blob:{uuid.uuid4().hex}"
        self._exec("INSERT INTO blobs (ref, content_type, data) VALUES (?, ?, ?)", (ref, content_type, data))
        return ref

    def get_blob(self, ref: str) -> tuple[bytes, str] | None:
        rows = self._exec("SELECT data, content_type FROM blobs WHERE ref = ?", (ref,))
        return (rows[0][0], rows[0][1]) if rows else None

    # ------------------------------------------------------------ discrepancies
    def save_discrepancies(self, items: list[Discrepancy]) -> None:
        for d in items:
            self._exec("INSERT OR IGNORE INTO discrepancies (discrepancy_id, body) VALUES (?, ?)",
                       (d.discrepancy_id, _enc(d.model_dump_json())))

    def update_discrepancy(self, d: Discrepancy) -> None:
        self._exec("UPDATE discrepancies SET body = ? WHERE discrepancy_id = ?", (_enc(d.model_dump_json()), d.discrepancy_id))

    def list_discrepancies(self) -> list[Discrepancy]:
        return [Discrepancy.model_validate_json(_dec(r[0])) for r in self._exec("SELECT body FROM discrepancies")]

    # ------------------------------------------------------------ audit (append only)
    def audit(self, actor_role: str, action: str, target_type: str, target_id: str | None = None,
              details: dict[str, Any] | None = None) -> AuditEvent:
        with self._lock:  # stamp under the insert lock, so ts order matches seq order under parallel writers
            ev = AuditEvent(event_id=f"ev-{uuid.uuid4().hex[:16]}", ts=self.now(), actor_role=actor_role,
                            action=action, target_type=target_type, target_id=target_id, details=details or {})
            self._db.execute("INSERT INTO audit (event_id, body, synced) VALUES (?, ?, 0)",
                             (ev.event_id, _enc(ev.model_dump_json())))
            self._enqueue(SYNC_AUDIT, sync_key(ev.ts, ev.event_id), ev.model_dump_json())
            self._db.commit()
        return ev

    def list_audit(self, limit: int = 500) -> list[AuditEvent]:
        rows = self._exec("SELECT body, synced FROM audit ORDER BY seq DESC LIMIT ?", (limit,))
        return [AuditEvent.model_validate_json(_dec(b)).model_copy(update={"synced": bool(s)}) for b, s in rows]

    def audit_log(self) -> list[tuple[int, AuditEvent]]:
        """Every event with its store sequence number, oldest first. `seq` is the provenance handle the
        digest and the incident report cite (`audit.<seq>`); AuditEvent itself has no seq field."""
        rows = self._exec("SELECT seq, body, synced FROM audit ORDER BY seq")
        return [(seq, AuditEvent.model_validate_json(_dec(b)).model_copy(update={"synced": bool(s)}))
                for seq, b, s in rows]

    def unsynced_audit(self) -> list[AuditEvent]:
        rows = self._exec("SELECT body FROM audit WHERE synced = 0 ORDER BY seq")
        return [AuditEvent.model_validate_json(_dec(r[0])) for r in rows]

    def mark_synced(self, event_ids: list[str]) -> None:
        for eid in event_ids:
            self._exec("UPDATE audit SET synced = 1 WHERE event_id = ?", (eid,))

    # ------------------------------------------------------------ sync outbox (app.sync.worker drains it)
    def outbox(self, limit: int = 25) -> list[tuple[int, str, str, str]]:
        """Oldest first: (seq, pk, sk, body)."""
        return self._exec("SELECT seq, pk, sk, body FROM sync_outbox ORDER BY seq LIMIT ?", (limit,))

    def outbox_pending(self) -> int:
        return self._exec("SELECT COUNT(*) FROM sync_outbox")[0][0]

    def outbox_done(self, seq: int, pk: str, sk: str) -> bool:
        """One mirrored put, in one transaction: drop the row, count it, stamp last success, clear the last
        error, and mark an audit event synced. False (nothing counted) if the row was already gone."""
        with self._lock:
            if self._db.execute("DELETE FROM sync_outbox WHERE seq = ?", (seq,)).rowcount != 1:
                self._db.commit()
                return False
            if pk == SYNC_AUDIT:
                self._db.execute("UPDATE audit SET synced = 1 WHERE event_id = ?", (sk.rsplit("#", 1)[-1],))
            self._db.execute("INSERT INTO kv (key, value) VALUES ('sync.mirrored', '1') ON CONFLICT (key) "
                             "DO UPDATE SET value = CAST(value AS INTEGER) + 1")
            self._db.executemany("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                                 [("sync.last_success_ts", self.now().isoformat()), ("sync.last_error", None)])
            self._db.commit()
        return True

    # ------------------------------------------------------------ costs
    def record_cost(self, job: str, model_id: str, tokens_in: int, tokens_out: int, latency_ms: int,
                    usd: float, dry_run: bool) -> None:
        self._exec(
            "INSERT INTO costs (ts, job, model_id, tokens_in, tokens_out, latency_ms, usd, dry_run) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.now().isoformat(), job, model_id, tokens_in, tokens_out, latency_ms, usd, int(dry_run)),
        )

    def list_costs(self) -> list[dict[str, Any]]:
        cols = ("ts", "job", "model_id", "tokens_in", "tokens_out", "latency_ms", "usd", "dry_run")
        rows = self._exec(f"SELECT {', '.join(cols)} FROM costs ORDER BY seq")
        return [dict(zip(cols, r)) | {"dry_run": bool(r[-1])} for r in rows]
