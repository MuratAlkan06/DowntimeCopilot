"""Incident report: a deterministic timeline filtered from the audit log, plus a two-paragraph summary
(Sonnet, `prompts/incident.md`) written from the same filtered rows and guardrail-checked.

Filter rules (docs/decisions.md, S6): an allowlist of lifecycle actions, in store sequence order. Views
(`*.view`, `*.list`), AI call bookkeeping, rules evaluations, snapshot pulls, sync and status reads are
noise and never appear. `downtime.start` adds a "packets generated" row; the latest `recovery.restore`
adds one row per stored discrepancy (same seq, severity order). Text uses display names (app.ai.digest).
"""
from __future__ import annotations

import json
from datetime import datetime

from app.ai import client as ai
from app.ai.digest import FORM_LABELS, ROLE_LABELS, display
from app.ai.guardrail import get_guardrail
from contracts import AuditEvent, CapturedRecord, Cost, Discrepancy, Guardrail, PatientSnapshot

LIFECYCLE = ("downtime.start", "capture.create", "backload.edit", "backload.verify", "backload.verify_rejected",
             "backload.entered", "recovery.restore", "recovery.restore_failed")
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def timeline(events: list[tuple[int, AuditEvent]], records: list[CapturedRecord], discs: list[Discrepancy],
             patients: list[PatientSnapshot]) -> list[dict]:
    beds = {p.patient_id: p.bed for p in patients}
    recs = {r.record_id: r for r in records}
    last_restore = max((s for s, e in events if e.action == "recovery.restore"), default=None)
    rows: list[dict] = []

    def row(seq: int, e: AuditEvent, event: str, detail: str = "", patient_id: str | None = None) -> None:
        rows.append({"seq": seq, "ts": e.ts.isoformat(), "role": ROLE_LABELS.get(e.actor_role, e.actor_role),
                     "action": e.action, "event": event, "patient": beds.get(patient_id) if patient_id else None,
                     "detail": display(detail, beds)})

    for seq, e in events:
        if e.action not in LIFECYCLE:
            continue
        rec = recs.get(e.target_id or "")
        form = FORM_LABELS[rec.form_type] if rec else "Paper record"
        pid = rec.patient_id if rec else None
        d = e.details
        if e.action == "downtime.start":
            row(seq, e, "Downtime declared")
            row(seq, e, "Downtime packets generated", f"{d.get('packets', 0)} packets")
        elif e.action == "capture.create":
            row(seq, e, "Paper form captured", form, d.get("patient_id") or pid)
        elif e.action == "backload.edit":
            row(seq, e, "Reviewer corrected a field", f"{form}: {d.get('field')} {d.get('before')} -> {d.get('after')}",
                pid)
        elif e.action == "backload.verify":
            step, need = d.get("step", 1), d.get("required", 1)
            label = "Verified" if step == need else f"Verification {step} of {need}"
            row(seq, e, label, f"{form}, by {d.get('verified_by')}", pid)
        elif e.action == "backload.verify_rejected":
            row(seq, e, "Second verification rejected", f"{form}: {d.get('reason')}", pid)
        elif e.action == "backload.entered":
            row(seq, e, "Marked entered in the EHR", form, pid)
        elif e.action == "recovery.restore":
            row(seq, e, "Systems restored", f"{d.get('discrepancies', 0)} discrepancies flagged")
            if seq == last_restore:
                for x in sorted(discs, key=lambda x: (SEVERITY_ORDER.get(x.severity.value, 3), x.rule_id,
                                                      beds.get(x.patient_id, x.patient_id))):
                    row(seq, e, f"Discrepancy {x.rule_id}", f"{x.severity.value}: {x.description}", x.patient_id)
        elif e.action == "recovery.restore_failed":
            row(seq, e, "Restore failed", str(d.get("error", "")))
    return rows


def _hhmm(iso: str | None) -> str:
    return datetime.fromisoformat(iso).strftime("%H:%M") if iso else "not yet"


def counts(rows: list[dict]) -> dict:
    ev = [r["event"] for r in rows]
    disc = [r for r in rows if r["event"].startswith("Discrepancy ")]
    return {"captures": ev.count("Paper form captured"), "verified": ev.count("Verified"),
            "entered": ev.count("Marked entered in the EHR"), "corrections": ev.count("Reviewer corrected a field"),
            "discrepancies": len(disc),
            "critical": sum(r["detail"].startswith("critical") for r in disc)}


def _n(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _canned(rows: list[dict]) -> list[str]:
    c = counts(rows)
    declared = next((r["ts"] for r in rows if r["event"] == "Downtime declared"), None)
    restored = next((r["ts"] for r in reversed(rows) if r["event"] == "Systems restored"), None)
    packets = next((r["detail"] for r in rows if r["event"] == "Downtime packets generated"), "no packets")
    return [
        f"Downtime was declared at {_hhmm(declared)} and systems were restored at {_hhmm(restored)}. "
        f"{packets.capitalize()} generated at declaration; staff captured {_n(c['captures'], 'paper form')} "
        "during the outage.",
        f"{_n(c['verified'], 'record')} {'was' if c['verified'] == 1 else 'were'} verified and {c['entered']} "
        f"marked entered in the EHR, with {_n(c['corrections'], 'reviewer correction')}. The restore diff flagged "
        f"{c['discrepancies']} discrepanc{'y' if c['discrepancies'] == 1 else 'ies'} ({c['critical']} critical) "
        "for reconciliation. Every statement comes from the audit log timeline below.",
    ]


def summarize(rows: list[dict], beds: dict[str, str], actor_role: str = "system"
              ) -> tuple[list[str], str, Guardrail, Cost]:
    canned = _canned(rows)
    user = json.dumps({"timeline": [{k: r[k] for k in ("ts", "role", "event", "patient", "detail")} for r in rows],
                       "counts": counts(rows)})
    res = ai.call("incident", "incident", user, mock=lambda: json.dumps({"paragraphs": canned}),
                  actor_role=actor_role, max_tokens=800)
    try:
        paras = [str(p).strip() for p in ai.extract_json(res.text).get("paragraphs", []) if str(p).strip()]
    except (ValueError, AttributeError, TypeError):
        paras = []
    paras = [display(p, beds) for p in paras[:2]] if len(paras) >= 2 else canned  # raw ids never survive
    grounding = "\n".join(f"{r['ts']} {r['role']} {r['event']} {r['patient'] or ''} {r['detail']}" for r in rows)
    verdict = get_guardrail().check("\n\n".join(paras), grounding=grounding)
    if verdict.status != "passed":
        paras = canned
    return paras, res.model_id, verdict, Cost(tokens_in=res.tokens_in, tokens_out=res.tokens_out, usd=res.usd)
