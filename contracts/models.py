"""Frozen data contracts (README "Data contracts"). Frozen after the 7:30 PM checkpoint.

Changing anything here means a PR that updates every consumer, re-runs
`make schemas`, and a message to the team.

Interpretations of shapes the README leaves open (decided at freeze):
- `reads.<reader>.fields` maps field name -> raw value; `confidence` maps field name -> 0..1.
  A reader that did not run (e.g. Textract denied) is `null`, which yields agreement "single".
- `crop` is a normalized bounding box on the source image (same convention as Textract BoundingBox).
- `shift_window` is {start, end}.
- Lifecycle fields (verified_by, verified_at, entered_at) are null until the event happens.
- Free-text classifiers the README does not enumerate (code_status, guardrail.status,
  actor_role, ...) are plain strings.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    """Base for every contract model: unknown keys are rejected so drift fails loudly."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- enums

class FormType(str, Enum):
    vitals_flowsheet = "vitals_flowsheet"
    downtime_mar = "downtime_mar"
    admission_note = "admission_note"
    verbal_order = "verbal_order"


class Agreement(str, Enum):
    agree = "agree"
    disagree = "disagree"
    single = "single"


class FieldFlag(str, Enum):
    unreadable = "unreadable"
    disagreement = "disagreement"
    out_of_range = "out_of_range"


class RecordStatus(str, Enum):
    needs_review = "needs_review"
    verified = "verified"
    entered = "entered"
    rejected = "rejected"


class BackloadGroup(str, Enum):
    nursing_med_admin = "nursing_med_admin"
    nursing_vitals = "nursing_vitals"
    nursing_io = "nursing_io"
    pharmacy_orders = "pharmacy_orders"
    unit_registration = "unit_registration"


class Severity(str, Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


class DiscrepancyStatus(str, Enum):
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"


# ---------------------------------------------------------------- shared nested shapes

class Cost(Contract):
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    usd: float = Field(ge=0)


class Guardrail(Contract):
    status: str
    reasons: list[str] = Field(default_factory=list)


class SbarItem(Contract):
    text: str
    source_path: str


class Sbar(Contract):
    situation: list[SbarItem] = Field(default_factory=list)
    background: list[SbarItem] = Field(default_factory=list)
    assessment: list[SbarItem] = Field(default_factory=list)
    recommendation: list[SbarItem] = Field(default_factory=list)


# ---------------------------------------------------------------- PatientSnapshot

class Allergy(Contract):
    substance: str
    reaction: str
    severity: str


class Medication(Contract):
    name: str
    dose: str
    route: str
    frequency: str
    next_due: datetime | None = None
    high_alert: bool = False
    last_given: datetime | None = None


class PatientSnapshot(Contract):
    patient_id: str
    mrn: str
    name: str
    dob: date
    age: int = Field(ge=0)
    sex: str
    bed: str
    admitting_dx: str
    code_status: str
    isolation: str | None = None
    allergies: list[Allergy] = Field(default_factory=list)
    medications: list[Medication] = Field(default_factory=list)
    lines_drains: list[str] = Field(default_factory=list)
    pending_labs: list[str] = Field(default_factory=list)
    fall_risk: str
    weight_kg: float | None = None
    snapshot_time: datetime
    source: Literal["bca-feed-sim"] = "bca-feed-sim"


# ---------------------------------------------------------------- DowntimePacket

class Alert(Contract):
    rule_id: str
    text: str


class DowntimePacket(Contract):
    patient_id: str
    generated_at: datetime
    snapshot_time: datetime
    model_id: str
    sbar: Sbar
    alerts: list[Alert] = Field(default_factory=list)
    guardrail: Guardrail
    cost: Cost


# ---------------------------------------------------------------- CapturedRecord

class ReaderOutput(Contract):
    fields: dict[str, str | None] = Field(default_factory=dict)
    confidence: dict[str, float] = Field(default_factory=dict)


class Reads(Contract):
    textract: ReaderOutput | None = None
    bedrock: ReaderOutput | None = None


class Crop(Contract):
    left: float = Field(ge=0, le=1)
    top: float = Field(ge=0, le=1)
    width: float = Field(ge=0, le=1)
    height: float = Field(ge=0, le=1)


class CapturedField(Contract):
    name: str
    value: str | None = None
    confidence: float = Field(ge=0, le=1)
    agreement: Agreement
    crop: Crop | None = None
    flag: FieldFlag | None = None


class CapturedRecord(Contract):
    record_id: str
    patient_id: str | None = None
    form_type: FormType
    image_ref: str | None = None
    audio_ref: str | None = None
    captured_at: datetime
    captured_by_role: str
    reads: Reads = Field(default_factory=Reads)
    fields: list[CapturedField] = Field(default_factory=list)
    status: RecordStatus = RecordStatus.needs_review
    backload_group: BackloadGroup
    verified_by: str | None = None
    verified_at: datetime | None = None
    entered_at: datetime | None = None

    @model_validator(mode="after")
    def _needs_source(self) -> CapturedRecord:
        if self.image_ref is None and self.audio_ref is None:
            raise ValueError("CapturedRecord needs image_ref or audio_ref")
        return self


# ---------------------------------------------------------------- PolicyAnswer

class Citation(Contract):
    section_id: str
    title: str
    quote: str


class PolicyAnswer(Contract):
    question: str
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    model_id: str
    grounded: bool
    guardrail: Guardrail
    cost: Cost


# ---------------------------------------------------------------- Discrepancy

class Discrepancy(Contract):
    discrepancy_id: str
    rule_id: str
    patient_id: str
    record_id: str | None = None
    description: str
    severity: Severity
    status: DiscrepancyStatus = DiscrepancyStatus.open


# ---------------------------------------------------------------- ShiftDigest

class ShiftWindow(Contract):
    start: datetime
    end: datetime


class PatientDigest(Contract):
    patient_id: str
    sbar: Sbar


class ShiftDigest(Contract):
    shift_window: ShiftWindow
    generated_at: datetime
    model_id: str
    unit_summary: str
    per_patient: list[PatientDigest] = Field(default_factory=list)
    open_items: list[str] = Field(default_factory=list)
    cost: Cost


# ---------------------------------------------------------------- AuditEvent

class AuditEvent(Contract):
    event_id: str
    ts: datetime
    actor_role: str
    action: str
    target_type: str
    target_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    synced: bool = False


TOP_LEVEL_MODELS: tuple[type[Contract], ...] = (
    PatientSnapshot,
    DowntimePacket,
    CapturedRecord,
    PolicyAnswer,
    Discrepancy,
    ShiftDigest,
    AuditEvent,
)
