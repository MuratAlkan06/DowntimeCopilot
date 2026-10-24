"""Deterministic simulated unit state: seeded patients, a simulated clock, drift, outage, restore.

Everything is derived from data/patients/*.json plus SEED, so two runs with the same seed and
the same sequence of control calls return identical snapshots.
"""
from __future__ import annotations

import json
import os
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from contracts import Allergy, Medication, PatientSnapshot

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DEFAULT_SEED = 20261024
# Last good snapshot is taken at 02:45; the outage in the demo starts at 02:47.
CLOCK_START = datetime(2026, 10, 24, 2, 45, tzinfo=timezone.utc)
# A patient file whose snapshot_time is earlier than CLOCK_START models a lagging feed for that patient
# (e.g. a stuck interface): the feed keeps reporting data that much older than the unit clock. The seed
# has exactly one (pt-14, 40 min), so the readiness panel and R-STALE-01 have a real example.

_INTERVAL = re.compile(r"q(\d+)h", re.IGNORECASE)

# Patients the scripted demo writes paper records for (DEMO.md: Bed 12 vitals, Bed 2 MAR, Bed 7 verbal
# order). The changed-allergy restore delta never lands on them, so it never interacts with those beats.
DEMO_CRITICAL = frozenset({"pt-12", "pt-07", "pt-02"})
# Reported by the patient during downtime and charted in the EHR on restore; no seeded patient has it.
NEW_ALLERGY = Allergy(substance="cephalexin", reaction="rash", severity="moderate")


def frequency_hours(frequency: str) -> int | None:
    """Scheduled interval in hours for 'qNh' frequencies; None for PRN-only, continuous, etc."""
    m = _INTERVAL.search(frequency)
    if m:
        return int(m.group(1))
    if "daily" in frequency.lower():
        return 24
    return None


def load_patients(data_dir: Path = DATA_DIR) -> list[PatientSnapshot]:
    files = sorted((data_dir / "patients").glob("*.json"))
    return [PatientSnapshot.model_validate(json.loads(f.read_text())) for f in files]


class SimState:
    def __init__(self, seed: int | None = None) -> None:
        self.reset(seed)

    def reset(self, seed: int | None = None) -> None:
        self.seed = int(seed if seed is not None else os.environ.get("SEED", DEFAULT_SEED))
        self.rng = random.Random(self.seed)
        self.now = CLOCK_START
        self.outage = False
        self.restored = False
        self.patients = {p.patient_id: p for p in load_patients()}
        self.feed_lag = {pid: CLOCK_START - p.snapshot_time for pid, p in self.patients.items()
                         if p.snapshot_time < CLOCK_START}
        self.restore_changes: list[dict] = []

    # ------------------------------------------------------------ clock
    def advance(self, minutes: int) -> datetime:
        self.now = self.now + timedelta(minutes=minutes)
        return self.now

    # ------------------------------------------------------------ drift
    def _drift_medication(self, m: Medication, as_of: datetime) -> Medication:
        """Roll scheduled doses forward as the clock passes them (doses given on time while up)."""
        hours = frequency_hours(m.frequency)
        if m.next_due is None or hours is None or self.outage or self.restored:
            return m
        next_due, last_given = m.next_due, m.last_given
        while next_due <= as_of:
            last_given = next_due
            next_due = next_due + timedelta(hours=hours)
        return m.model_copy(update={"next_due": next_due, "last_given": last_given})

    def snapshot(self) -> list[PatientSnapshot]:
        out = []
        for p in self.patients.values():
            as_of = self.now - self.feed_lag.get(p.patient_id, timedelta(0))  # a lagging feed reports older data
            meds = [self._drift_medication(m, as_of) for m in p.medications]
            out.append(p.model_copy(update={"medications": meds, "snapshot_time": as_of}))
        return sorted(out, key=lambda p: int(re.sub(r"\D", "", p.bed) or 0))

    # ------------------------------------------------------------ outage controls
    def start_outage(self) -> None:
        # Freeze the drifted state so the restored EHR starts from what was true at outage time.
        self.patients = {p.patient_id: p for p in self.snapshot()}
        self.outage = True

    def restore(self) -> list[dict]:
        """Bring the EHR back with deliberate differences for the recovery diff: one pharmacy-backloaded
        order and one patient transfer (seeded choice, never Bed 12), and one changed allergy list (a new
        documented allergy, seeded choice outside the demo-critical patients and the other two picks)."""
        if not self.outage:
            self.start_outage()
        self.outage = False
        if self.restored:
            return self.restore_changes
        self.restored = True
        ids = sorted(pid for pid in self.patients if pid != "pt-12")
        order_pid, transfer_pid = self.rng.sample(ids, 2)  # drawn first: keeps the earlier seeded picks stable
        allergy_pid = self.rng.choice(sorted(pid for pid in self.patients
                                             if pid not in DEMO_CRITICAL | {order_pid, transfer_pid}))

        p = self.patients[order_pid]
        new_med = Medication(
            name="pantoprazole", dose="40 mg", route="IV", frequency="daily",
            next_due=self.now + timedelta(hours=6), high_alert=False, last_given=None,
        )
        self.patients[order_pid] = p.model_copy(update={"medications": [*p.medications, new_med]})

        t = self.patients[transfer_pid]
        new_bed = "ICU 4"
        self.patients[transfer_pid] = t.model_copy(update={"bed": new_bed})

        a = self.patients[allergy_pid]
        self.patients[allergy_pid] = a.model_copy(update={"allergies": [*a.allergies, NEW_ALLERGY]})

        self.restore_changes = [
            {"kind": "pharmacy_backloaded_order", "patient_id": order_pid, "medication": new_med.name},
            {"kind": "transfer", "patient_id": transfer_pid, "from_bed": t.bed, "to_bed": new_bed},
            {"kind": "allergy_added", "patient_id": allergy_pid, "substance": NEW_ALLERGY.substance},
        ]
        return self.restore_changes
