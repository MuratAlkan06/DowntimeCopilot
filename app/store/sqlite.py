"""Local store: stdlib sqlite3, no ORM. SQLite is the local source of truth (docs/decisions.md);
cloud mirrors in app/sync/ copy from here.

TODO(encryption at rest): wrap `_enc`/`_dec` with an envelope key (e.g. Fernet keyed from the OS
keychain or KMS data key). They are pass-throughs today; README "Limitations" says demonstrated,
not hardened.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from contracts import AuditEvent, CapturedRecord, Discrepancy, DowntimePacket, PatientSnapshot

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
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _enc(text: str) -> str:  # TODO(encryption at rest)
    return text


def _dec(text: str) -> str:  # TODO(encryption at rest)
    return text


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

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
    def save_record(self, record: CapturedRecord) -> None:
        with self._lock:
            row = self._db.execute("SELECT seq FROM captured WHERE record_id = ?", (record.record_id,)).fetchone()
            seq = row[0] if row else (self._db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM captured").fetchone()[0])
            self._db.execute("INSERT OR REPLACE INTO captured (record_id, seq, body) VALUES (?, ?, ?)",
                             (record.record_id, seq, _enc(record.model_dump_json())))
            self._db.commit()

    def get_record(self, record_id: str) -> CapturedRecord | None:
        rows = self._exec("SELECT body FROM captured WHERE record_id = ?", (record_id,))
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

    def list_discrepancies(self) -> list[Discrepancy]:
        return [Discrepancy.model_validate_json(_dec(r[0])) for r in self._exec("SELECT body FROM discrepancies")]

    # ------------------------------------------------------------ audit (append only)
    def audit(self, actor_role: str, action: str, target_type: str, target_id: str | None = None,
              details: dict[str, Any] | None = None) -> AuditEvent:
        ev = AuditEvent(event_id=f"ev-{uuid.uuid4().hex[:16]}", ts=utcnow(), actor_role=actor_role,
                        action=action, target_type=target_type, target_id=target_id, details=details or {})
        self._exec("INSERT INTO audit (event_id, body, synced) VALUES (?, ?, 0)",
                   (ev.event_id, _enc(ev.model_dump_json())))
        return ev

    def list_audit(self, limit: int = 500) -> list[AuditEvent]:
        rows = self._exec("SELECT body, synced FROM audit ORDER BY seq DESC LIMIT ?", (limit,))
        return [AuditEvent.model_validate_json(_dec(b)).model_copy(update={"synced": bool(s)}) for b, s in rows]

    def unsynced_audit(self) -> list[AuditEvent]:
        rows = self._exec("SELECT body FROM audit WHERE synced = 0 ORDER BY seq")
        return [AuditEvent.model_validate_json(_dec(r[0])) for r in rows]

    def mark_synced(self, event_ids: list[str]) -> None:
        for eid in event_ids:
            self._exec("UPDATE audit SET synced = 1 WHERE event_id = ?", (eid,))

    # ------------------------------------------------------------ costs
    def record_cost(self, job: str, model_id: str, tokens_in: int, tokens_out: int, latency_ms: int,
                    usd: float, dry_run: bool) -> None:
        self._exec(
            "INSERT INTO costs (ts, job, model_id, tokens_in, tokens_out, latency_ms, usd, dry_run) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (utcnow().isoformat(), job, model_id, tokens_in, tokens_out, latency_ms, usd, int(dry_run)),
        )

    def list_costs(self) -> list[dict[str, Any]]:
        cols = ("ts", "job", "model_id", "tokens_in", "tokens_out", "latency_ms", "usd", "dry_run")
        rows = self._exec(f"SELECT {', '.join(cols)} FROM costs ORDER BY seq")
        return [dict(zip(cols, r)) | {"dry_run": bool(r[-1])} for r in rows]
