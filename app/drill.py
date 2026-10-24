"""Readiness drill (S13, docs/decisions.md): a simulated outage run-through on a throwaway in-process store.

Isolation:
- Every drill builds a fresh `Store(":memory:")`. `app.store.use_store` points get_store() at it for this thread
  only, so the drill's snapshot pulls, packets, ai.call cost and audit rows and kv writes all land there and
  vanish with it. The ephemeral store has no sync worker, and the live store is only READ (the checklist
  audit, before the override), so the live store gains no row, no outbox item and nothing to mirror.
- The simulator is only read: GET /clock and GET /bca/snapshot. The outage and the restore are synthetic
  transitions inside the ephemeral world (kv phase "downtime", then a second read-only pull saved as the
  "restored" kind and diffed against the first). POST /outage/start and /outage/restore are never called, so
  the simulator's EHR state and clock never move.
- Packets are always DRY_RUN (config.forced_dry_run, this thread only): canned template SBAR, zero spend.
  Only the two-sentence summary follows the process DRY_RUN; its cost row lands in the ephemeral store, and
  its model id and spend are reported on the drill result instead.
- The score comes from app.readiness alone (pure); the model only words it. A summary that fails
  readiness.check_summary is replaced by the deterministic canned one.
- The last result is kept in memory (null after a restart). A drill that fails part way keeps the previous one.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime

from app import checklists, config, readiness, sim_client
from app.ai import client as ai
from app.ai.packets import generate_packet, is_omission
from app.api.backload import diff_restored
from app.rules import RecoveryContext, recovery_discrepancies, snapshot_alerts
from app.store import Store, get_store, use_store
from contracts import DowntimePacket

log = logging.getLogger(__name__)

ACTOR = "drill"
_lock = threading.Lock()
_last: dict | None = None


class DrillError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def empty() -> dict:
    return {"score": None, "components": {}, "last_drill_at": None, "summary": None, "drill": None}


def last() -> dict:
    return _last or empty()


def acknowledged_roles(live: Store) -> frozenset[str]:
    """Roles whose own checklist someone in that role opened (audit checklist.view, actor == role, found).
    Read-only on the live store."""
    return frozenset(e.target_id for _, e in live.audit_log()
                     if e.action == "checklist.view" and e.details.get("found") and e.actor_role == e.target_id)


def template_ready(form_type: str) -> bool:
    forms = config.data_dir() / "forms"
    html, layout = forms / f"{form_type}.html", forms / f"{form_type}.layout.json"
    try:
        return bool(html.read_text(encoding="utf-8").strip()) and bool(
            json.loads(layout.read_text(encoding="utf-8")).get("fields"))
    except (OSError, ValueError, AttributeError):
        return False


def packet_ok(packet: DowntimePacket) -> bool:
    lines = [i for sec in ("situation", "background", "assessment", "recommendation")
             for i in getattr(packet.sbar, sec) if not is_omission(i)]
    return packet.guardrail.status == "passed" and bool(lines)


def summarize(result: dict) -> dict:
    """Two sentences via the `readiness` job (Sonnet; canned in DRY_RUN), checked; never changes the score."""
    canned = readiness.canned_summary(result)
    payload = json.dumps({"score": result["score"], "scale": readiness.SCALE, "components": result["components"]})
    meta = {"model_id": None, "usd": 0.0, "source": "canned", "check": []}
    try:
        res = ai.call("readiness", "readiness", payload, mock=lambda: canned, max_tokens=200, actor_role=ACTOR)
    except Exception as exc:  # noqa: BLE001 - the summary is optional; the score stands without it
        log.warning("readiness summary failed (%s); canned summary used", type(exc).__name__)
        return {"text": canned, **meta, "check": [f"model call failed: {type(exc).__name__}"]}
    text = " ".join(res.text.split())
    reasons = readiness.check_summary(text, result)
    meta |= {"model_id": res.model_id, "usd": res.usd, "check": reasons}
    if reasons:
        log.warning("readiness summary failed the check (%s); canned summary used", "; ".join(reasons))
        return {"text": canned, **meta}
    return {"text": text, **meta, "source": "dry_run" if res.dry_run else "model"}


def run() -> dict:
    global _last
    if not _lock.acquire(blocking=False):
        raise DrillError(409, "a readiness drill is already running")
    try:
        result = _run()
    except DrillError:
        raise
    except Exception as exc:  # the live store is untouched either way; the previous result stays
        log.exception("readiness drill failed")
        raise DrillError(500, f"drill failed part way ({type(exc).__name__}); live state untouched") from exc
    finally:
        _lock.release()
    _last = result
    return result


def _run() -> dict:
    live = get_store()  # read-only from here on
    acked = acknowledged_roles(live)
    try:
        status = sim_client.read_clock()
    except sim_client.SimUnavailable as exc:
        raise DrillError(503, f"drill needs the BCA feed: {exc}") from exc
    if status.get("outage"):
        raise DrillError(409, "the simulator is in an outage; run the drill while the EHR is up")
    now = datetime.fromisoformat(status["now"])
    steps: list[dict] = []
    eph = Store(":memory:", now=lambda: now)
    try:
        with use_store(eph):
            with config.forced_dry_run():
                try:
                    pre = sim_client.pull_snapshot()
                except sim_client.SimUnavailable as exc:
                    raise DrillError(503, f"drill snapshot pull failed: {exc}") from exc
                eph.save_snapshot(pre, "pre")
                steps.append({"name": "snapshot", "ok": bool(pre), "detail": f"{len(pre)} patients pulled read-only"})
                eph.set_kv("phase", "downtime")  # synthetic outage: the simulator is not touched
                packets = {}
                for p in pre:
                    packet = generate_packet(p, snapshot_alerts(p, now), now, actor_role=ACTOR)
                    eph.save_packet(packet)
                    packets[p.patient_id] = packet_ok(packet)
                steps.append({"name": "packets", "ok": all(packets.values()) and bool(packets),
                              "detail": f"{sum(packets.values())} of {len(pre)} packets generated (DRY_RUN)"})
                steps.append(_restore_check(eph, pre, now))
            roles = tuple(r for r in readiness.ACK_ROLES if checklists.load().get(r))
            inputs = readiness.Inputs(
                snapshot_times={p.patient_id: p.snapshot_time for p in pre}, packets_ok=packets,
                roles_required=roles, roles_acknowledged=acked,
                templates={f: template_ready(f) for f in readiness.TEMPLATE_FORMS})
            result = readiness.score(inputs, clock=lambda: now)
            summary = summarize(result)
    finally:
        eph.close()
    return {**result, "last_drill_at": now.isoformat(), "summary": summary["text"],
            "drill": {"steps": steps, "summary_source": summary["source"], "summary_model_id": summary["model_id"],
                      "summary_usd": summary["usd"], "summary_check": summary["check"]}}


def _restore_check(eph: Store, pre: list, now: datetime) -> dict:
    """Synthetic restore in the ephemeral world: a second read-only pull stands in for the restored EHR, then
    the recovery diff and rules run against it. Passes when every pre-downtime patient comes back."""
    try:
        restored = sim_client.pull_snapshot()
    except sim_client.SimUnavailable as exc:
        return {"name": "restore", "ok": False, "detail": f"restored pull failed: {exc}"}
    eph.save_snapshot(restored, "restored")
    before = {p.patient_id: p for p in pre}
    after = {p.patient_id: p for p in restored}
    found = diff_restored(before, after) + recovery_discrepancies(RecoveryContext(before, [], now=now))
    eph.save_discrepancies(found)
    missing = sorted(set(before) - set(after))
    return {"name": "restore", "ok": not missing,
            "detail": f"{len(after)} of {len(before)} patients back after a synthetic restore, "
                      f"{len(found)} discrepancies"}
