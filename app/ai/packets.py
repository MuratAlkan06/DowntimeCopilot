"""Downtime packet generation (Bedrock job 1) with the faithfulness check.

Every SBAR line must carry the snapshot field path it came from; lines whose source_path does not
resolve into the snapshot are rejected and counted in guardrail.reasons.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from app.ai import client as ai
from app.ai.guardrail import get_guardrail
from app.store import utcnow
from contracts import Alert, Cost, DowntimePacket, Guardrail, PatientSnapshot, Sbar, SbarItem

_TOKEN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")
SECTIONS = ("situation", "background", "assessment", "recommendation")


def resolve(doc: Any, path: str) -> bool:
    """True if `path` like 'medications[0].next_due' exists in the snapshot dict."""
    if not path or not path.strip():
        return False
    cur = doc
    for part in path.strip().split("."):
        tokens = _TOKEN.findall(part)
        if not tokens or "".join(n or f"[{i}]" for n, i in tokens) != part:
            return False
        for name, idx in tokens:
            if name:
                if not isinstance(cur, dict) or name not in cur:
                    return False
                cur = cur[name]
            else:
                if not isinstance(cur, list) or int(idx) >= len(cur):
                    return False
                cur = cur[int(idx)]
    return True


def faithful(sbar_raw: dict, snapshot: dict) -> tuple[Sbar, int]:
    """Keep only lines with a resolvable source_path. Returns (sbar, rejected_count)."""
    kept: dict[str, list[SbarItem]] = {s: [] for s in SECTIONS}
    rejected = 0
    for section in SECTIONS:
        for item in sbar_raw.get(section, []) or []:
            if isinstance(item, dict) and item.get("text") and resolve(snapshot, str(item.get("source_path", ""))):
                kept[section].append(SbarItem(text=str(item["text"]), source_path=str(item["source_path"])))
            else:
                rejected += 1
    return Sbar(**kept), rejected


def template_sbar(p: PatientSnapshot) -> dict:
    """Deterministic SBAR from the snapshot (DRY_RUN canned output and offline fallback)."""
    s: dict[str, list[dict]] = {k: [] for k in SECTIONS}
    s["situation"].append({"text": f"{p.bed}: {p.name}, {p.age} {p.sex}, {p.admitting_dx}", "source_path": "admitting_dx"})
    s["situation"].append({"text": f"Code status: {p.code_status}", "source_path": "code_status"})
    if p.isolation:
        s["situation"].append({"text": f"Isolation: {p.isolation}", "source_path": "isolation"})
    if p.allergies:
        for i, a in enumerate(p.allergies):
            s["situation"].append({"text": f"ALLERGY {a.substance}: {a.reaction} ({a.severity})",
                                   "source_path": f"allergies[{i}]"})
    else:
        s["situation"].append({"text": "No known allergies recorded", "source_path": "allergies"})
    for i, m in enumerate(p.medications):
        due = f", next due {m.next_due:%H:%M}" if m.next_due else ""
        flag = " [HIGH-ALERT]" if m.high_alert else ""
        s["background"].append({"text": f"{m.name} {m.dose} {m.route} {m.frequency}{due}{flag}",
                                "source_path": f"medications[{i}]"})
    for i, line in enumerate(p.lines_drains):
        s["background"].append({"text": f"Line/drain: {line}", "source_path": f"lines_drains[{i}]"})
    if p.weight_kg is not None:
        s["background"].append({"text": f"Weight {p.weight_kg} kg", "source_path": "weight_kg"})
    s["assessment"].append({"text": f"Fall risk: {p.fall_risk}", "source_path": "fall_risk"})
    for i, lab in enumerate(p.pending_labs):
        s["assessment"].append({"text": f"Pending lab: {lab}", "source_path": f"pending_labs[{i}]"})
    for i, m in enumerate(p.medications):
        if m.high_alert and m.next_due:
            s["recommendation"].append({"text": f"Independent double-check before {m.name} at {m.next_due:%H:%M} "
                                                "(policy 5.2)", "source_path": f"medications[{i}].next_due"})
    return s


def generate_packet(p: PatientSnapshot, alerts: list[Alert], now: datetime, actor_role: str = "system") -> DowntimePacket:
    snapshot = json.loads(p.model_dump_json())
    canned = template_sbar(p)
    user = json.dumps({"now": now.isoformat(), "snapshot": snapshot,
                       "alerts": [a.model_dump() for a in alerts]})
    res = ai.call("packet", "packet", user, mock=lambda: json.dumps({"sbar": canned}), actor_role=actor_role)
    reasons: list[str] = []
    try:
        raw = ai.extract_json(res.text)
        raw = raw.get("sbar", raw)
    except ValueError:
        raw, reasons = canned, ["model output unparseable; deterministic packet used"]
    sbar, rejected = faithful(raw, snapshot)
    if rejected:
        reasons.append(f"faithfulness: rejected {rejected} line(s) without a resolvable source_path")
    text = "\n".join(i.text for sec in SECTIONS for i in getattr(sbar, sec))
    g = get_guardrail().check(text, grounding=json.dumps(snapshot))
    return DowntimePacket(
        patient_id=p.patient_id, generated_at=utcnow(), snapshot_time=p.snapshot_time, model_id=res.model_id,
        sbar=sbar, alerts=alerts, guardrail=Guardrail(status=g.status, reasons=[*g.reasons, *reasons]),
        cost=Cost(tokens_in=res.tokens_in, tokens_out=res.tokens_out, usd=res.usd),
    )


def priority(packet: DowntimePacket) -> tuple:
    """Acuity order for the packet list: allergy conflicts, due high-alert meds, then the rest."""
    ids = [a.rule_id for a in packet.alerts]
    return (-ids.count("R-ALG-01"), -ids.count("R-DUE-01"), -len(ids), packet.patient_id)
