"""Readiness score (S13, docs/decisions.md): a pure, deterministic 0-100 score of how ready the unit is for a
downtime. No model calls, no network, no store and no clock of its own: the drill (app/drill.py) gathers the
inputs and injects the clock. Same inputs and the same clock give the same score.

Components and weights (the weights sum to 100):
  snapshot_freshness   35  share of snapshot patients whose snapshot is at most 30 unit-minutes old at clock()
                           (the R-STALE-01 threshold). Every packet stands on this data.
  packet_coverage      30  share of snapshot patients with a usable drill packet: guardrail passed and at least
                           one verified (non-omitted) SBAR line.
  checklist_ack        15  share of unit roles with a policy checklist (charge nurse, RN, pharmacy, unit clerk)
                           whose checklist was deliberately opened (not a background fetch). The weakest-measured
                           signal, so the lowest weight.
  forms_templates      20  share of printable paper forms (vitals flowsheet, downtime MAR, admission note) whose
                           template and layout map are present and readable.

Component score = 100 x ratio, rounded half up. An empty denominator scores 0: nothing to stand on is not ready.
Overall score = sum(weight x ratio), rounded half up once, from the exact ratios (fractions, so no float drift
can move a .5 boundary).

The summary checker lives here too (pure): exactly two sentences, and every number the summary quotes must
appear in the scorer's output (or be the 100 of the scale).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from typing import Callable

from app.rules.engine import STALE_AFTER

WEIGHTS: dict[str, int] = {
    "snapshot_freshness": 35,
    "packet_coverage": 30,
    "checklist_ack": 15,
    "forms_templates": 20,
}
LABELS: dict[str, str] = {
    "snapshot_freshness": "Snapshot freshness",
    "packet_coverage": "Packet coverage",
    "checklist_ack": "Checklist acknowledgment",
    "forms_templates": "Forms templates",
}
# Unit roles expected to open their checklist at activation (app.api.actor_role, web/js/state.js ROLES).
ACK_ROLES: tuple[str, ...] = ("charge_nurse", "rn", "pharmacy", "unit_clerk")
# Paper forms with a printed template (verbal orders are spoken: no template, app.capture.layouts).
TEMPLATE_FORMS: tuple[str, ...] = ("vitals_flowsheet", "downtime_mar", "admission_note")
SCALE = 100
STALE_MINUTES = int(STALE_AFTER.total_seconds() // 60)


@dataclass(frozen=True)
class Inputs:
    snapshot_times: dict[str, datetime] = field(default_factory=dict)  # patient_id -> snapshot_time
    packets_ok: dict[str, bool] = field(default_factory=dict)  # patient_id -> usable drill packet
    roles_required: tuple[str, ...] = ()  # ACK_ROLES that have a non-empty policy checklist
    roles_acknowledged: frozenset[str] = frozenset()
    templates: dict[str, bool] = field(default_factory=dict)  # form_type -> template + layout readable


def _ratio(n: int, d: int) -> Fraction:
    return Fraction(n, d) if d else Fraction(0)


def half_up(x: Fraction) -> int:
    return math.floor(x + Fraction(1, 2))


def _component(name: str, n: int, d: int, detail: str) -> tuple[Fraction, dict]:
    ratio = _ratio(n, d)
    return ratio, {"score": half_up(SCALE * ratio), "weight": WEIGHTS[name], "detail": detail}


def score(inputs: Inputs, *, clock: Callable[[], datetime]) -> dict:
    """{score, components: {name: {score, weight, detail}}}. `clock` is the unit clock (required: the scorer
    never reads time by itself)."""
    now = clock()
    pids = sorted(inputs.snapshot_times)
    fresh = sum(1 for pid in pids if now - inputs.snapshot_times[pid] <= STALE_AFTER)
    covered = sum(1 for pid in pids if inputs.packets_ok.get(pid))
    required = list(inputs.roles_required)
    acked = sum(1 for r in required if r in inputs.roles_acknowledged)
    forms = list(TEMPLATE_FORMS)
    available = sum(1 for f in forms if inputs.templates.get(f))
    parts = {
        "snapshot_freshness": _component(
            "snapshot_freshness", fresh, len(pids),
            f"{fresh} of {len(pids)} patient snapshots are at most {STALE_MINUTES} min old"),
        "packet_coverage": _component(
            "packet_coverage", covered, len(pids), f"{covered} of {len(pids)} patients have a verified packet"),
        "checklist_ack": _component(
            "checklist_ack", acked, len(required), f"{acked} of {len(required)} roles have opened their checklist"),
        "forms_templates": _component(
            "forms_templates", available, len(forms), f"{available} of {len(forms)} paper form templates are ready"),
    }
    total = half_up(sum(WEIGHTS[name] * ratio for name, (ratio, _) in parts.items()))
    return {"score": total, "components": {name: comp for name, (_, comp) in parts.items()}}


# ------------------------------------------------------------------ summary (canned text + checker)

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def canned_summary(result: dict) -> str:
    """Deterministic two-sentence summary (DRY_RUN, and the fallback when the model's fails the check)."""
    comps = result["components"]
    weakest = min(comps, key=lambda n: (comps[n]["score"], -comps[n]["weight"]))
    listed = ", ".join(f"{LABELS[n].lower()} {c['score']}" for n, c in comps.items())
    return (f"Readiness scored {result['score']} of {SCALE}: {listed}. "
            f"Lowest is {LABELS[weakest].lower()} at {comps[weakest]['score']}, as "
            f"{comps[weakest]['detail']}.")


def sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text.strip()) if s.strip()]


def allowed_numbers(result: dict) -> set[float]:
    nums = {float(SCALE), float(result["score"])}
    for comp in result["components"].values():
        nums |= {float(comp["score"]), float(comp["weight"])}
        nums |= {float(n) for n in _NUMBER.findall(comp["detail"])}
    return nums


def check_summary(text: str, result: dict) -> list[str]:
    """Reasons the summary fails; [] passes. Exactly two sentences, ending in terminal punctuation, and no
    number that differs from the scorer's output."""
    reasons = []
    parts = sentences(text)
    if len(parts) != 2 or not text.strip().endswith((".", "!", "?")):
        reasons.append(f"expected 2 sentences, got {len(parts)}")
    allowed = allowed_numbers(result)
    divergent = sorted({n for n in _NUMBER.findall(text) if float(n) not in allowed})
    if divergent:
        reasons.append(f"numbers not in the score: {', '.join(divergent)}")
    return reasons
