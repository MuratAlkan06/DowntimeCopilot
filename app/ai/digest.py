"""Shift digest (Bedrock job 4): SBAR handoff for the oncoming shift, verified facts only.

Sources (docs/decisions.md, S6):
- the patient roster (latest snapshot) for who and where: `snapshot.<patient_id>.<field>`;
- captured records a person has verified or marked entered: `captured.<record_id>`;
- open discrepancies that do not depend on an unverified record: `discrepancy.<discrepancy_id>`;
- lifecycle audit events (declare, restore, captures still awaiting verification): `audit.<seq>`.
Records still `needs_review` contribute no content, only the audit fact that they await verification.

Every SBAR line is `{text, source_path}` (frozen ShiftDigest). `source_path` holds the provenance: one or
more of the refs above, comma-separated, each of which must resolve to a fact given to the model. Lines
that do not resolve are dropped; patients the model leaves out get the deterministic SBAR. Prose uses
display names (Bed 12, SpO2, RN), never raw ids (pt-12, spo2, rn): the prompt asks for it and
`display()` enforces it on every line.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from app import clock, config
from app.ai import client as ai
from app.ai.guardrail import get_guardrail
from contracts import (AuditEvent, CapturedRecord, Cost, Discrepancy, FieldFlag, FormType, Guardrail, PatientDigest,
                       PatientSnapshot, RecordStatus, Sbar, SbarItem, ShiftDigest, ShiftWindow)

SECTIONS = ("situation", "background", "assessment", "recommendation")
DONE = (RecordStatus.verified, RecordStatus.entered)

# Same display names as the web client (web/js/dom.js FIELD_LABELS, state.js role labels). Field labels live
# in app/config.py so the rules engine writes discrepancy descriptions with them too (single source).
FIELD_LABELS = config.FIELD_LABELS
FORM_LABELS = {FormType.vitals_flowsheet: "Vitals flowsheet", FormType.downtime_mar: "Downtime MAR",
               FormType.verbal_order: "Verbal order", FormType.admission_note: "Admission note"}
ROLE_LABELS = {"charge_nurse": "Charge nurse", "rn": "RN", "pharmacy": "Pharmacy", "unit_clerk": "Unit clerk",
               "system": "System"}
HIDDEN_FIELDS = {"bed", "initials"}
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}

_REF = r"(?:snapshot|captured|discrepancy|audit)\.[A-Za-z0-9_:-]+(?:\.[A-Za-z0-9_:-]+)*"
_REFS_PAREN = re.compile(rf"\s*[(\[]\s*(?:(?:source|ref)s?:\s*)?{_REF}(?:\s*,\s*{_REF})*\s*[)\]]")
_REFS_BARE = re.compile(rf"\b{_REF}")
_PID = re.compile(r"\bpt-\d+\b")
_RECORD = re.compile(r"\brec-[0-9a-f]{6,}\b")
# Only keys that are unmistakably raw (snake_case or with digits: spo2, resp_rate); plain words such as
# "response" or "history" are left alone, so ordinary prose is never re-cased.
_FIELD = re.compile(r"\b(" + "|".join(sorted((k for k in FIELD_LABELS if "_" in k or any(c.isdigit() for c in k)),
                                              key=len, reverse=True)) + r")\b")
_FORM = re.compile(r"\b(" + "|".join(f.value for f in FormType) + r")\b")
_ROLE = re.compile(r"\b(charge_nurse|unit_clerk|rn)\b")


def display(text: str, beds: dict[str, str]) -> str:
    """Raw ids -> display names: provenance refs are dropped from prose (they belong in source_path; live
    Sonnet appended "(discrepancy.d-…)" to open items), patient ids -> bed, field keys, form types and
    roles -> labels."""
    text = _REFS_BARE.sub("", _REFS_PAREN.sub("", text))
    text = re.sub(r"\s{2,}", " ", re.sub(r"\s+([.,;:])", r"\1", text)).strip()
    text = _PID.sub(lambda m: beds.get(m.group(0), "a patient"), text)
    text = _RECORD.sub("a paper record", text)
    text = _FIELD.sub(lambda m: FIELD_LABELS[m.group(1)], text)
    text = _FORM.sub(lambda m: FORM_LABELS[FormType(m.group(1))], text)
    return _ROLE.sub(lambda m: ROLE_LABELS[m.group(1)], text)


def _fields_text(r: CapturedRecord) -> str:
    parts = []
    for f in r.fields:
        if f.name in HIDDEN_FIELDS:
            continue
        label = FIELD_LABELS.get(f.name, f.name.replace("_", " ").capitalize())
        if f.value is None:
            parts.append(f"{label} unreadable")
        else:
            parts.append(f"{label} {f.value}" + (" (out of range)" if f.flag == FieldFlag.out_of_range else ""))
    return "; ".join(parts)


def facts(patients: list[PatientSnapshot], records: list[CapturedRecord], discs: list[Discrepancy],
          events: list[tuple[int, AuditEvent]]) -> list[dict]:
    """Every fact the digest may state, each with its provenance ref, already in display names."""
    beds = {p.patient_id: p.bed for p in patients}
    by_id = {r.record_id: r for r in records}
    out: list[dict] = []
    for p in patients:
        out.append({"ref": f"snapshot.{p.patient_id}.admitting_dx", "patient_id": p.patient_id, "kind": "roster",
                    "text": f"{p.bed}: {p.name}, {p.admitting_dx}"})
    for r in records:
        if r.status not in DONE or not r.patient_id:
            continue
        state = "entered in the EHR" if r.status == RecordStatus.entered else "verified, not yet entered"
        out.append({"ref": f"captured.{r.record_id}", "patient_id": r.patient_id, "kind": r.status.value,
                    "text": f"{FORM_LABELS[r.form_type]} captured {r.captured_at:%H:%M}, {state} "
                            f"(verified by {r.verified_by}): {_fields_text(r)}"})
    for d in discs:
        rec = by_id.get(d.record_id) if d.record_id else None
        if d.status != "open" or (d.record_id and (rec is None or rec.status not in DONE)):
            continue  # tied to a record nobody has verified yet: held back with it
        out.append({"ref": f"discrepancy.{d.discrepancy_id}", "patient_id": d.patient_id, "kind": "discrepancy",
                    "severity": d.severity.value, "text": f"{d.rule_id} ({d.severity.value}): {d.description}"})
    for seq, e in events:
        ref, at = f"audit.{seq}", f"{e.ts:%H:%M}"
        if e.action == "downtime.start":
            out.append({"ref": ref, "patient_id": None, "kind": "lifecycle",
                        "text": f"Downtime declared at {at}; {e.details.get('packets', 0)} downtime packets generated"})
        elif e.action == "recovery.restore":
            out.append({"ref": ref, "patient_id": None, "kind": "lifecycle",
                        "text": f"Systems restored at {at}; {e.details.get('discrepancies', 0)} discrepancies flagged"})
        elif e.action == "capture.create" and e.target_id:
            rec = by_id.get(e.target_id)
            if rec is not None and rec.status == RecordStatus.needs_review and rec.patient_id:
                out.append({"ref": ref, "patient_id": rec.patient_id, "kind": "pending",
                            "text": f"{FORM_LABELS[rec.form_type]} captured {at} is awaiting verification; "
                                    "its content is not included until a person verifies it"})
    for f in out:
        f["text"] = display(f["text"], beds)
    return out


def _line(f: dict) -> dict:
    return {"text": f["text"], "source_path": f["ref"]}


def _canned(patients: list[PatientSnapshot], fx: list[dict], held: int) -> dict:
    who = {p.patient_id: f"{p.bed} · {p.name}" for p in patients}  # open items lead with the patient
    per = []
    for p in patients:
        mine = [f for f in fx if f["patient_id"] == p.patient_id]
        per.append({"patient_id": p.patient_id, "sbar": {
            "situation": [_line(f) for f in mine if f["kind"] == "roster"],
            "background": [_line(f) for f in mine if f["kind"] in ("verified", "entered")],
            "assessment": [_line(f) for f in mine if f["kind"] == "discrepancy"],
            "recommendation": [_line(f) for f in mine if f["kind"] == "pending"]
            + [{"text": "Mark the verified record entered once it is in the EHR", "source_path": f["ref"]}
               for f in mine if f["kind"] == "verified"],
        }})
    count = {k: sum(f["kind"] == k for f in fx) for k in ("verified", "entered", "pending", "discrepancy")}
    life = " ".join(f"{f['text']}." for f in fx if f["kind"] == "lifecycle")
    done = count["verified"] + count["entered"]
    summary = (f"{len(patients)} patients. {life} {done} paper record{'' if done == 1 else 's'} verified "
               f"({count['entered']} entered), {count['pending']} awaiting verification and not included. "
               f"{count['discrepancy']} open discrepanc{'y' if count['discrepancy'] == 1 else 'ies'} shown"
               + (f"; {held} more are held back until their records are verified." if held else ".")).replace("  ", " ")
    # Safety items first, critical -> warning -> info (stable within a severity), then records to verify.
    discs = sorted((f for f in fx if f["kind"] == "discrepancy"), key=lambda f: SEVERITY_ORDER.get(f["severity"], 3))
    items = [f"{who.get(f['patient_id'], 'Unit')} — {f['text']}" for f in discs] + \
            [f"{who.get(f['patient_id'], 'Unit')} — {f['text'].split(' captured ')[0]}: verify before backload"
             for f in fx if f["kind"] == "pending"]
    return {"unit_summary": summary.strip(), "per_patient": per, "open_items": items}


def _keep(items: list, refs: set[str], beds: dict[str, str]) -> list[SbarItem]:
    """Lines whose every comma-separated source_path ref resolves to a given fact, in display names."""
    out = []
    for x in items:
        if not isinstance(x, dict) or not str(x.get("text", "")).strip():
            continue
        paths = [s.strip() for s in str(x.get("source_path", "")).split(",") if s.strip()]
        if paths and all(s in refs for s in paths):
            out.append(SbarItem(text=display(str(x["text"]), beds), source_path=",".join(paths)))
    return out


def build_digest_checked(patients: list[PatientSnapshot], records: list[CapturedRecord], discs: list[Discrepancy],
                         start: datetime, end: datetime, actor_role: str = "system",
                         events: list[tuple[int, AuditEvent]] | None = None) -> tuple[ShiftDigest, Guardrail]:
    beds = {p.patient_id: p.bed for p in patients}
    fx = facts(patients, records, discs, events or [])
    shown = {f["ref"] for f in fx}
    held = sum(1 for d in discs if d.status == "open" and f"discrepancy.{d.discrepancy_id}" not in shown)
    canned = _canned(patients, fx, held)
    user = json.dumps({"patients": [{"patient_id": p.patient_id, "label": p.bed} for p in patients],
                       "facts": [{k: f[k] for k in ("ref", "patient_id", "text")} for f in fx],
                       "held_back_discrepancies": held})
    res = ai.call("digest", "digest", user, mock=lambda: json.dumps(canned), actor_role=actor_role, max_tokens=4000)
    fallback = {x["patient_id"]: Sbar.model_validate(x["sbar"]) for x in canned["per_patient"]}
    try:
        raw = ai.extract_json(res.text)
        model = {str(x["patient_id"]): Sbar(**{s: _keep(x["sbar"].get(s, []), shown, beds) for s in SECTIONS})
                 for x in raw.get("per_patient", []) if isinstance(x, dict) and isinstance(x.get("sbar"), dict)}
        summary = display(str(raw.get("unit_summary", "")), beds) or canned["unit_summary"]
        items = [display(str(i), beds) for i in raw.get("open_items", [])]
    except (ValueError, KeyError, TypeError, AttributeError):
        model, summary, items = {}, canned["unit_summary"] + " (deterministic digest: model output unparseable)", \
            canned["open_items"]
    per = [PatientDigest(patient_id=p.patient_id,
                         sbar=model[p.patient_id] if any(getattr(model.get(p.patient_id), s, None) for s in SECTIONS)
                         else fallback[p.patient_id]) for p in patients]
    grounding = "\n".join(f"[{f['ref']}] {f['text']}" for f in fx)
    verdict = get_guardrail().check("\n".join([summary, *items]), grounding=grounding)
    if verdict.status != "passed":  # never hand over unchecked prose: fall back to the deterministic text
        summary = canned["unit_summary"] + " (deterministic summary: the safety check did not pass the model text)"
        items = canned["open_items"]
    return ShiftDigest(shift_window=ShiftWindow(start=start, end=end), generated_at=clock.unit_now(),
                       model_id=res.model_id, unit_summary=summary, per_patient=per, open_items=items,
                       cost=Cost(tokens_in=res.tokens_in, tokens_out=res.tokens_out, usd=res.usd)), verdict


def build_digest(patients: list[PatientSnapshot], records: list[CapturedRecord], discs: list[Discrepancy],
                 start: datetime, end: datetime, actor_role: str = "system",
                 events: list[tuple[int, AuditEvent]] | None = None) -> ShiftDigest:
    return build_digest_checked(patients, records, discs, start, end, actor_role, events)[0]
