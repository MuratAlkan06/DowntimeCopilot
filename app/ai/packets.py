"""Downtime packet generation (Bedrock job 1) with the faithfulness check.

Every SBAR line must carry the snapshot field path(s) it came from (one path, or several separated by
commas). A line is kept only if EVERY path resolves into the snapshot; rule alerts are never a source.
Contract behavior on a rejected line: log it, regenerate the packet ONCE (telling the model which
lines failed), and if lines are still unverifiable, drop them and show an omission item in their place
(see OMISSION_TEXT / OMISSION_SOURCE). All counts go to guardrail.reasons.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from app.ai import client as ai
from app.ai.guardrail import get_guardrail
from contracts import Alert, Cost, DowntimePacket, Guardrail, PatientSnapshot, Sbar, SbarItem

log = logging.getLogger(__name__)

_TOKEN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")
SECTIONS = ("situation", "background", "assessment", "recommendation")

# A dropped (still unverifiable after one regeneration) line is replaced in its own SBAR section by a
# contract-shaped SbarItem so the reader sees that something was withheld. Its source_path names the
# faithfulness check itself: it is not a snapshot path, so it never resolves and never reads as a fact.
OMISSION_TEXT = "omitted: unverifiable line"
OMISSION_SOURCE = "faithfulness-check"


def is_omission(item: SbarItem) -> bool:
    return item.source_path == OMISSION_SOURCE


def split_paths(source_path: str) -> list[str]:
    """'medications[0].next_due, medications[0].high_alert' -> both paths; empty parts are kept so they fail."""
    return [p.strip() for p in str(source_path or "").split(",")]


def resolve(doc: Any, source_path: str) -> bool:
    """True if every comma-separated path (e.g. 'medications[0].next_due') exists in the snapshot dict."""
    paths = split_paths(source_path)
    return bool(paths) and all(_resolve_one(doc, p) for p in paths)


def _resolve_one(doc: Any, path: str) -> bool:
    if not path:
        return False
    if re.match(r"alerts\b", path):  # rules-engine alerts are derived, never a snapshot source
        return False
    cur = doc
    for part in path.split("."):
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


def faithful(sbar_raw: dict, snapshot: dict) -> tuple[dict[str, list[SbarItem]], list[dict]]:
    """Split model lines into kept SbarItems (every path resolves) and rejected raw lines (with their section)."""
    kept: dict[str, list[SbarItem]] = {s: [] for s in SECTIONS}
    rejected: list[dict] = []
    for section in SECTIONS:
        for item in sbar_raw.get(section, []) or []:
            if isinstance(item, dict) and item.get("text") and resolve(snapshot, str(item.get("source_path", ""))):
                path = ", ".join(split_paths(item["source_path"]))
                kept[section].append(SbarItem(text=str(item["text"]), source_path=path))
            else:
                line = item if isinstance(item, dict) else {"text": str(item)}
                rejected.append({"section": section, "text": str(line.get("text", "")),
                                 "source_path": str(line.get("source_path", ""))})
    return kept, rejected


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


def _parse_sbar(text: str) -> dict | None:
    try:
        raw = ai.extract_json(text)
    except ValueError:
        return None
    raw = raw.get("sbar", raw) if isinstance(raw, dict) else None
    return raw if isinstance(raw, dict) else None


def generate_packet(p: PatientSnapshot, alerts: list[Alert], now: datetime, actor_role: str = "system") -> DowntimePacket:
    """`now` is the simulated unit clock; it stamps generated_at so the packet header never mixes clocks."""
    snapshot = json.loads(p.model_dump_json())
    canned = template_sbar(p)
    payload = {"now": now.isoformat(), "snapshot": snapshot, "alerts": [a.model_dump() for a in alerts]}

    def generate(extra: dict | None = None) -> ai.AIResult:
        user = json.dumps({**payload, **(extra or {})})
        return ai.call("packet", "packet", user, mock=lambda: json.dumps({"sbar": canned}), actor_role=actor_role)

    results = [generate()]
    reasons: list[str] = []
    raw = _parse_sbar(results[0].text)
    if raw is None:
        raw, reasons = canned, ["model output unparseable; deterministic packet used"]
    kept, rejected = faithful(raw, snapshot)
    if rejected:
        reasons.append(f"faithfulness: rejected {len(rejected)} line(s) without a resolvable source_path")
        log.warning("packet %s: faithfulness rejected %d line(s) %s; regenerating once", p.patient_id,
                    len(rejected), [r["source_path"] for r in rejected])
        results.append(generate({"rejected": rejected}))
        retry = _parse_sbar(results[-1].text)
        if retry is not None:
            kept, rejected = faithful(retry, snapshot)
        if rejected:
            log.warning("packet %s: %d line(s) still unverifiable after regeneration; omitted", p.patient_id,
                        len(rejected))
            for r in rejected:
                kept[r["section"]].append(SbarItem(text=OMISSION_TEXT, source_path=OMISSION_SOURCE))
        reasons.append(f"faithfulness: regenerated once; omitted {len(rejected)} unverifiable line(s)")
    sbar = Sbar(**kept)
    text = "\n".join(i.text for sec in SECTIONS for i in getattr(sbar, sec) if not is_omission(i))
    g = get_guardrail().check(text, grounding=json.dumps(snapshot))
    return DowntimePacket(
        patient_id=p.patient_id, generated_at=now, snapshot_time=p.snapshot_time, model_id=results[-1].model_id,
        sbar=sbar, alerts=alerts, guardrail=Guardrail(status=g.status, reasons=[*g.reasons, *reasons]),
        cost=Cost(tokens_in=sum(r.tokens_in for r in results), tokens_out=sum(r.tokens_out for r in results),
                  usd=round(sum(r.usd for r in results), 6)),
    )


def priority(packet: DowntimePacket) -> tuple:
    """Acuity order for the packet list: allergy conflicts, due high-alert meds, then the rest."""
    ids = [a.rule_id for a in packet.alerts]
    return (-ids.count("R-ALG-01"), -ids.count("R-DUE-01"), -len(ids), packet.patient_id)
